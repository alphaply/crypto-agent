import json
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from backend import database, database_rules as rules
from backend.agent import memory_agent as memory
from backend.app.services import trading_rules_service as service


CONFIG = {"config_id": "cfg", "symbol": "BTC/USDT", "model": "mock-model", "api_key": "mock-key"}
SOURCE = "截至2026-10-06；窗口10-01至10-06；旧记忆2026-10-01：待确认。完整平仓T1/T2因未确认提前挂单，费用未知。"
START_AUDIT = memory._start_audit
FINISH_AUDIT = memory._finish_audit


def final(summary="样本不足；保留待确认条件。", conclusion="证据不足", reason="T1/T2窗口费用缺失。"):
    return AIMessage(content=json.dumps({"summary": summary, "rule_review": conclusion, "reason": reason}, ensure_ascii=False),
                     usage_metadata={"input_tokens": 12, "output_tokens": 8, "total_tokens": 20})


def call(action="list", changes=None, *, name="manage_trading_rules", **extra):
    return AIMessage(content="", tool_calls=[{"name": name, "args": {"action": action, "changes": changes or [], **extra}, "id": "tool1"}])


def add_change():
    return {"action": "add", "content": "确认未到前不挂可能成交的入场单。", "reason": "2026-10-01至10-06的T1/T2重复未确认挂单。"}


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_NAME", str(tmp_path / "memory.sqlite"))
    with database.get_db_conn() as conn:
        rules.initialize_trading_rules_schema(conn.cursor())
        conn.commit()
    monkeypatch.setattr(service.config, "get_config_by_id", lambda cid: {"config_id": cid} if cid in {"cfg", "other"} else None)
    monkeypatch.setattr(memory, "invoke_with_retry", lambda operation, **kwargs: operation())
    monkeypatch.setattr(memory, "_start_audit", lambda *args: str(uuid.uuid4()))
    monkeypatch.setattr(memory, "_finish_audit", lambda *args, **kwargs: None)
    usage = []
    monkeypatch.setattr(database, "save_token_usage", lambda **kwargs: usage.append(kwargs))
    for key in ("model", "api_key", "api_base"):
        monkeypatch.setattr(memory.global_config, f"global_summarizer_{key}", "")
        monkeypatch.delenv(f"GLOBAL_SUMMARIZER_{key.upper()}", raising=False)
    return usage


def model(monkeypatch, *responses):
    class FakeModel:
        def __init__(self):
            self.calls = []
            self.tools = []
            self.settings = {}

        def bind_tools(self, toolset):
            raise AssertionError("Memory must not bind any tools")

        def invoke(self, messages):
            self.calls.append(list(messages))
            response = responses[len(self.calls) - 1]
            if isinstance(response, Exception):
                raise response
            return response

    fake = FakeModel()

    def build(**kwargs):
        fake.settings = kwargs
        return fake

    monkeypatch.setattr(memory, "build_chat_model", build)
    return fake


def test_memory_only_one_call_no_rules_and_cost(environment, monkeypatch):
    locked = rules.change_trading_rules("cfg", [{"action": "add", "content": "人工规则"}], actor="human")[0]
    disabled = rules.change_trading_rules("cfg", [{"action": "add", "content": "停用规则", "enabled": False}], actor="model")[0]
    fake = model(monkeypatch, final())
    result = memory.run_memory_review(SOURCE, CONFIG, operation_id="window1")
    assert result.status == "completed" and result.error == ""
    assert result.summary == "样本不足；保留待确认条件。"
    assert len(fake.calls) == 1 and fake.tools == [] and result.rule_receipts == []
    supplied = fake.calls[0][-1].content
    assert SOURCE in supplied
    assert locked["rule_id"] not in supplied and disabled["rule_id"] not in supplied
    assert "当前完整规则" not in supplied
    assert environment == [{"symbol": "BTC/USDT", "config_id": "cfg", "model": "mock-model", "prompt_tokens": 12, "completion_tokens": 8}]


@pytest.mark.parametrize("extra", [{"config_id": "other"}, {"symbol": "ETH/USDT"}])
def test_cannot_supply_ownership(environment, monkeypatch, extra):
    fake = model(monkeypatch, call("apply", [add_change()], **extra))
    result = memory.run_memory_review(SOURCE, CONFIG, operation_id="ownership")
    assert result.status == "failed" and not result.summary
    assert rules.list_trading_rules("cfg") == rules.list_trading_rules("other") == []
    assert len(fake.calls) == 1


def test_unknown_trade_tool_rejected_before_any_write(environment, monkeypatch):
    response = call("apply", [add_change()])
    response.tool_calls.append({"name": "place_order", "args": {}, "id": "trade1"})
    fake = model(monkeypatch, response)
    result = memory.run_memory_review(SOURCE, CONFIG, operation_id="trade")
    assert result.status == "failed" and "disabled" in result.error
    assert rules.list_trading_rules("cfg") == [] and len(fake.calls) == 1


def test_missing_config_never_builds_model(environment, monkeypatch):
    fake = model(monkeypatch)
    result = memory.run_memory_review(SOURCE, {"model": "mock"}, operation_id="missing")
    assert result.status == "failed" and "config_id" in result.error and not fake.calls


def test_cross_task_rule_and_human_lock_are_preserved(environment, monkeypatch):
    locked = rules.change_trading_rules("cfg", [{"action": "add", "content": "人工规则"}], actor="human")[0]
    other = rules.change_trading_rules("other", [{"action": "add", "content": "Other task"}], actor="model")[0]
    for rule in (locked, other):
        model(monkeypatch, call("apply", [add_change(), {"action": "disable", "rule_id": rule["rule_id"], "expected_revision": 1, "reason": "T1/T2"}]))
        result = memory.run_memory_review(SOURCE, CONFIG, operation_id=rule["rule_id"])
        assert result.status == "failed" and not result.summary and not result.rule_receipts
    assert rules.list_trading_rules("cfg") == [locked]
    assert rules.list_trading_rules("other") == [other]


def test_long_memory_preserves_evidence_and_ignores_legacy_rule_reason(environment, monkeypatch):
    summary = "正文开头\n" + "长记忆；" * 4000 + "\n正文末尾：必须等日线收盘确认。"
    reason = "完整理由\n" + "证据交易ID与时间；" * 100 + "\n理由末尾T-last。"
    source = SOURCE + "\n" + "完整证据；" * 4000 + "\n证据末尾。"
    fake = model(monkeypatch, final(summary=summary, reason=reason))
    result = memory.run_memory_review(source, {**CONFIG, "summarizer": {"short_memory_prompt": "自定义整理格式"}}, operation_id="long")
    assert result.status == "completed"
    assert result.summary == summary
    assert source in fake.calls[0][-1].content and "自定义整理格式" in fake.calls[0][-1].content
    assert "长期规则自动维护已暂停" in fake.calls[0][0].content
    assert "300–500字" in fake.calls[0][0].content and "不逐轮追加日志" in fake.calls[0][0].content


def test_model_fallback_and_custom_file(environment, monkeypatch, tmp_path):
    prompt = tmp_path / "memory.txt"
    prompt.write_text("自定义文件：{content}", encoding="utf-8")
    monkeypatch.setattr(memory.global_config, "global_summarizer_model", "global-model")
    monkeypatch.setenv("GLOBAL_SUMMARIZER_API_BASE", "https://example.invalid")
    fake = model(monkeypatch, final())
    result = memory.run_memory_review(SOURCE, {**CONFIG, "summarizer": {"model": "custom-model", "short_memory_prompt_file": str(prompt)}}, operation_id="custom")
    assert result.status == "completed"
    assert fake.settings["model"] == "custom-model"
    assert fake.settings["base_url"] == "https://example.invalid" and fake.settings["api_key"] == "mock-key"
    assert "自定义文件：" in fake.calls[0][-1].content


def test_memory_review_preserves_configured_provider_extra_body(environment, monkeypatch):
    options = {"thinking": {"type": "disabled"}, "custom_provider_option": "requested"}
    fake = model(monkeypatch, final())
    result = memory.run_memory_review(
        SOURCE, {**CONFIG, "summarizer": {"extra_body": options}}, operation_id="provider-options",
    )
    assert result.status == "completed"
    assert fake.settings["extra_body"] == options


@pytest.mark.parametrize("top_prompt,nested_prompt,expected", [
    ("TOP_INLINE\n{content}", "NESTED_INLINE\n{content}", "TOP_INLINE"),
    ("", "NESTED_INLINE\n{content}", "NESTED_INLINE"),
    ("TOP_WITHOUT_PLACEHOLDER", "NESTED_INLINE\n{content}", "TOP_WITHOUT_PLACEHOLDER"),
])
def test_top_level_memory_prompt_matches_editor_precedence(
    environment, monkeypatch, top_prompt, nested_prompt, expected,
):
    fake = model(monkeypatch, final())
    result = memory.run_memory_review(
        SOURCE,
        {**CONFIG, "short_memory_prompt": top_prompt,
         "summarizer": {"short_memory_prompt": nested_prompt}},
        operation_id="top-level-inline",
    )
    assert result.status == "completed"
    supplied = fake.calls[0][-1].content
    assert expected in supplied and SOURCE in supplied
    if top_prompt:
        assert "NESTED_INLINE" not in supplied


@pytest.mark.parametrize("top_file,nested_prompt,expected", [
    (True, "", "TOP_FILE"),
    (False, "", "NESTED_FILE"),
    (True, "NESTED_INLINE\n{content}", "NESTED_INLINE"),
])
def test_memory_prompt_file_precedence_matches_strategy_summary(
    environment, monkeypatch, tmp_path, top_file, nested_prompt, expected,
):
    top_path = tmp_path / "top-memory.txt"
    nested_path = tmp_path / "nested-memory.txt"
    top_path.write_text("TOP_FILE\n{content}", encoding="utf-8")
    nested_path.write_text("NESTED_FILE\n{content}", encoding="utf-8")
    fake = model(monkeypatch, final())
    result = memory.run_memory_review(
        SOURCE,
        {**CONFIG, "short_memory_prompt_file": str(top_path) if top_file else "",
         "summarizer": {"short_memory_prompt": nested_prompt, "short_memory_prompt_file": str(nested_path)}},
        operation_id="top-level-file",
    )
    assert result.status == "completed"
    supplied = fake.calls[0][-1].content
    assert expected in supplied and SOURCE in supplied
    for marker in {"TOP_FILE", "NESTED_FILE", "NESTED_INLINE"} - {expected}:
        assert marker not in supplied


def test_actual_audit_includes_input_raw_output_and_no_rule_tools(environment, monkeypatch):
    from backend import database_agent_runs as audit

    monkeypatch.setattr(memory, "_start_audit", START_AUDIT)
    monkeypatch.setattr(memory, "_finish_audit", FINISH_AUDIT)
    summary = "长记忆开头：" + "全部证据与条件。" * 1500 + "长记忆末尾。"
    reason = "理由开头：" + "证据引用。" * 100 + "理由末尾。"
    source = SOURCE + "\n" + "完整来源。" * 2000 + "来源末尾。"
    response = final(summary=summary, reason=reason, conclusion="更新")
    model(monkeypatch, response)
    result = memory.run_memory_review(source, CONFIG, operation_id="audit-window")
    assert result.status == "completed"
    runs = audit.list_agent_runs(config_id="cfg", purpose="memory_review")["runs"]
    assert len(runs) == 1 and all(run["status"] == "success" for run in runs)
    latest = audit.get_agent_run(runs[0]["run_id"])
    assert latest["output"] == response.content
    assert latest["details"]["memory_summary"] == result.summary
    assert result.summary == summary and reason not in result.summary
    assert latest["details"]["rule_receipts"] == result.rule_receipts
    assert latest["tools"] == []
    assert source in latest["messages"][1]["content"]


def test_audit_unavailable_does_not_block_review(environment, monkeypatch):
    from backend import database_agent_runs as audit

    def unavailable(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(memory, "_start_audit", START_AUDIT)
    monkeypatch.setattr(memory, "_finish_audit", FINISH_AUDIT)
    monkeypatch.setattr(audit, "start_agent_run", unavailable)
    model(monkeypatch, final())
    assert memory.run_memory_review(SOURCE, CONFIG, operation_id="no-audit").status == "completed"


def test_provider_retries_share_the_three_call_budget(environment, monkeypatch):
    fake = model(monkeypatch, RuntimeError("offline"), RuntimeError("offline"), RuntimeError("offline"))

    def aggressive_retry(operation, **kwargs):
        for _ in range(5):
            try:
                return operation()
            except RuntimeError:
                continue

    monkeypatch.setattr(memory, "invoke_with_retry", aggressive_retry)
    result = memory.run_memory_review(SOURCE, CONFIG, operation_id="retry-budget")
    assert result.status == "failed" and "budget" in result.error and len(fake.calls) == 3


@pytest.mark.parametrize("summarizer,agent_role,expected", [
    ({}, "system", SystemMessage),
    ({}, "user", HumanMessage),
    ({"system_prompt_role": "user"}, "system", HumanMessage),
    ({"system_prompt_role": "system"}, "user", SystemMessage),
])
def test_system_prompt_role_compatibility(environment, monkeypatch, summarizer, agent_role, expected):
    fake = model(monkeypatch, final())
    result = memory.run_memory_review(SOURCE, {**CONFIG, "system_prompt_role": agent_role, "summarizer": summarizer}, operation_id="roles")
    assert result.status == "completed"
    assert isinstance(fake.calls[0][0], expected)
    if expected is HumanMessage:
        assert len(fake.calls[0]) == 1 and SOURCE in fake.calls[0][0].content
        assert "长期规则自动维护已暂停" in fake.calls[0][0].content
    assert "system_prompt_role" not in fake.settings


@pytest.mark.parametrize("metadata", [
    {"finish_reason": "length"}, {"finish_reason": "content_filter"},
    {"stop_reason": "max_tokens"}, {"status": "incomplete"},
    {"finish_reason": "max_output_tokens"},
    {"incomplete_details": {"reason": "max_output_tokens"}},
])
def test_complete_json_with_incomplete_provider_status_is_rejected(environment, monkeypatch, metadata):
    response = final()
    response.response_metadata = metadata
    fake = model(monkeypatch, response)
    result = memory.run_memory_review(SOURCE, CONFIG, operation_id="truncated-json")
    assert result.status == "failed" and result.summary == "" and "incomplete" in result.error
    assert len(fake.calls) == 1


def test_complete_tool_arguments_with_truncated_response_never_execute(environment, monkeypatch):
    response = call("apply", [add_change()])
    response.response_metadata = {"finish_reason": "length"}
    model(monkeypatch, response)
    result = memory.run_memory_review(SOURCE, CONFIG, operation_id="truncated-tool")
    assert result.status == "failed" and not result.rule_receipts
    assert rules.list_trading_rules("cfg") == []


@pytest.mark.parametrize('action', ['list', 'apply'])
def test_rule_tool_response_never_executes_or_starts_second_model_turn(environment, monkeypatch, action):
    fake = model(monkeypatch, call(action, [add_change()] if action == 'apply' else []), final())
    result = memory.run_memory_review(SOURCE, CONFIG, operation_id='no-rule-tools')
    assert result.status == 'failed' and 'disabled' in result.error
    assert len(fake.calls) == 1 and result.rule_receipts == []
    assert rules.list_trading_rules('cfg') == []


@pytest.mark.parametrize('enabled', [False, True])
def test_memory_follows_main_tracing_regardless_of_auxiliary_switch(environment, monkeypatch, enabled):
    from langsmith import tracing_context, utils
    monkeypatch.setattr(memory.global_config, 'langchain_background_tracing', enabled)
    fake = model(monkeypatch, final())
    invoke = fake.invoke
    def checked(messages):
        assert utils.tracing_is_enabled() == 'local'
        return invoke(messages)
    monkeypatch.setattr(fake, 'invoke', checked)
    with tracing_context(enabled='local'):
        result = memory.run_memory_review(SOURCE, CONFIG, operation_id='trace-memory')
    assert result.status == 'completed' and len(fake.calls) == 1
