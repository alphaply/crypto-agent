import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from backend import database, database_rules as rules
from backend.agent import memory_agent as memory
from backend.app.services import trading_rules_service as service
from backend.utils.trade_operations import current_operation_id


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
    monkeypatch.setattr(memory, "_start_audit", lambda *args: "audit")
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
            self.tools = toolset
            return self

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


def test_no_change_one_call_full_rules_and_cost(environment, monkeypatch):
    locked = rules.change_trading_rules("cfg", [{"action": "add", "content": "人工规则"}], actor="human")[0]
    disabled = rules.change_trading_rules("cfg", [{"action": "add", "content": "停用规则", "enabled": False}], actor="model")[0]
    fake = model(monkeypatch, final())
    result = memory.run_memory_review(SOURCE, CONFIG, operation_id="window1")
    assert result.status == "completed" and result.error == ""
    assert "规则复盘：证据不足" in result.summary
    assert len(fake.calls) == 1 and [tool.name for tool in fake.tools] == ["manage_trading_rules"]
    supplied = fake.calls[0][-1].content
    assert SOURCE in supplied
    assert locked["rule_id"] in supplied and disabled["rule_id"] in supplied
    assert '"locked": true' in supplied and '"enabled": false' in supplied
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
    assert result.status == "failed" and "not authorized" in result.error
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


def test_apply_receipt_stable_id_and_atomic_version_checks(environment, monkeypatch):
    outer = current_operation_id.set("outer")
    try:
        for _ in range(2):
            fake = model(monkeypatch, call("apply", [add_change()]), final(conclusion="更新"))
            result = memory.run_memory_review(SOURCE, CONFIG, operation_id="same-window")
            assert result.status == "completed" and len(result.rule_receipts) == 1
            assert result.rule_receipts[0]["operation_id"] == "same-window:rules"
            assert isinstance(fake.calls[1][-1], ToolMessage)
        assert current_operation_id.get() == "outer"
    finally:
        current_operation_id.reset(outer)
    saved = rules.list_trading_rules("cfg")
    assert len(saved) == 1
    model(monkeypatch, call("apply", [add_change(), {"action": "disable", "rule_id": saved[0]["rule_id"], "expected_revision": 99, "reason": "T1/T2"}]))
    failed = memory.run_memory_review(SOURCE, CONFIG, operation_id="stale")
    assert failed.status == "failed" and not failed.rule_receipts
    assert rules.list_trading_rules("cfg") == saved


def test_successful_apply_then_model_failure_preserves_receipt(environment, monkeypatch):
    fake = model(monkeypatch, call("apply", [add_change()]), RuntimeError("model offline"))
    result = memory.run_memory_review(SOURCE, CONFIG, operation_id="partial")
    assert result.status == "partial" and result.summary == "" and "offline" in result.error
    assert len(result.rule_receipts) == len(rules.list_trading_rules("cfg")) == 1
    assert len(fake.calls) == 2


def test_two_apply_calls_rejected_without_partial_write(environment, monkeypatch):
    response = call("apply", [add_change()])
    response.tool_calls.append({**response.tool_calls[0], "id": "tool2"})
    model(monkeypatch, response)
    result = memory.run_memory_review(SOURCE, CONFIG, operation_id="double")
    assert result.status == "failed" and "one apply" in result.error
    assert rules.list_trading_rules("cfg") == []


def test_later_second_apply_cannot_mutate_again(environment, monkeypatch):
    fake = model(monkeypatch, call("apply", [add_change()]), call("apply", [add_change()]))
    result = memory.run_memory_review(SOURCE, CONFIG, operation_id="double-round")
    assert result.status == "partial" and not result.summary and "one apply" in result.error
    assert len(rules.list_trading_rules("cfg")) == 1 and len(fake.calls) == 2


def test_repeated_list_has_hard_model_budget(environment, monkeypatch):
    fake = model(monkeypatch, call(), call(), call())
    result = memory.run_memory_review(SOURCE, CONFIG, operation_id="loop")
    assert result.status == "failed" and not result.summary and len(fake.calls) == 3


def test_final_update_without_receipt_is_rejected(environment, monkeypatch):
    model(monkeypatch, final(conclusion="更新"))
    result = memory.run_memory_review(SOURCE, CONFIG, operation_id="lie")
    assert result.status == "failed" and not result.summary and "without" in result.error


def test_memory_limit_and_custom_template_still_has_evidence(environment, monkeypatch):
    fake = model(monkeypatch, final(summary="长" * 1600))
    result = memory.run_memory_review(SOURCE, {**CONFIG, "summarizer": {"short_memory_prompt": "自定义整理格式"}}, operation_id="long")
    assert result.status == "completed" and len(result.summary) <= 1200
    assert SOURCE in fake.calls[0][-1].content and "自定义整理格式" in fake.calls[0][-1].content
    assert "人工锁定" in fake.calls[0][0].content


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


def test_actual_audit_includes_input_tools_raw_output_and_receipts(environment, monkeypatch):
    from backend import database_agent_runs as audit

    monkeypatch.setattr(memory, "_start_audit", START_AUDIT)
    monkeypatch.setattr(memory, "_finish_audit", FINISH_AUDIT)
    response = final(conclusion="更新")
    model(monkeypatch, call("apply", [add_change()]), response)
    result = memory.run_memory_review(SOURCE, CONFIG, operation_id="audit-window")
    assert result.status == "completed"
    runs = audit.list_agent_runs(config_id="cfg", purpose="memory_review")["runs"]
    assert len(runs) == 2 and all(run["status"] == "success" for run in runs)
    latest = audit.get_agent_run(runs[0]["run_id"])
    assert latest["output"] == response.content
    assert latest["details"]["rule_receipts"] == result.rule_receipts
    assert latest["tools"][0]["function"]["name"] == "manage_trading_rules"
    assert SOURCE in latest["messages"][1]["content"]


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


def test_explicit_tool_failure_never_becomes_success_or_retries(environment, monkeypatch):
    original_tool = memory.manage_trading_rules.func
    applied = []

    def failed_tool(**kwargs):
        if kwargs["action"] == "apply":
            applied.append(kwargs)
            return json.dumps({"success": False, "status": "failed", "error": "Rejected"})
        return original_tool(**kwargs)

    monkeypatch.setattr(memory.manage_trading_rules, "func", failed_tool)
    fake = model(monkeypatch, call("apply", [add_change()]), final(conclusion="更新"))
    result = memory.run_memory_review(SOURCE, CONFIG, operation_id="tool-failure")
    assert result.status == "failed" and not result.summary and not result.rule_receipts
    assert len(applied) == len(fake.calls) == 1 and rules.list_trading_rules("cfg") == []


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
        assert "人工锁定" in fake.calls[0][0].content
    assert "system_prompt_role" not in fake.settings


@pytest.mark.parametrize("metadata", [
    {"finish_reason": "length"}, {"finish_reason": "content_filter"},
    {"stop_reason": "max_tokens"}, {"status": "incomplete"},
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


def test_truncated_summary_after_apply_is_partial(environment, monkeypatch):
    response = final(conclusion="更新")
    response.response_metadata = {"stop_reason": "max_tokens"}
    model(monkeypatch, call("apply", [add_change()]), response)
    result = memory.run_memory_review(SOURCE, CONFIG, operation_id="truncated-after-write")
    assert result.status == "partial" and result.summary == "" and len(result.rule_receipts) == 1


def test_excessive_lists_rejected_before_tool_execution(environment, monkeypatch):
    response = call()
    response.tool_calls *= 3
    fake = model(monkeypatch, response)
    calls = []
    original = memory._tool_result

    def tracked(*args):
        calls.append(args[0].action)
        return original(*args)

    monkeypatch.setattr(memory, "_tool_result", tracked)
    result = memory.run_memory_review(SOURCE, CONFIG, operation_id="too-many-tools")
    assert result.status == "failed" and "two tool calls" in result.error
    assert calls == ["list"] and len(fake.calls) == 1  # Initial program-owned snapshot only.


def test_tool_count_limit_applies_across_model_rounds(environment, monkeypatch):
    response = call("apply", [add_change()])
    response.tool_calls.append({"name": "manage_trading_rules", "args": {"action": "list"}, "id": "extra-list"})
    fake = model(monkeypatch, call(), response)
    result = memory.run_memory_review(SOURCE, CONFIG, operation_id="too-many-round-tools")
    assert result.status == "failed" and "two tool calls" in result.error
    assert rules.list_trading_rules("cfg") == [] and len(fake.calls) == 2


def test_free_text_is_not_a_rule_write_receipt(environment, monkeypatch):
    model(monkeypatch, final(summary="长期规则已更新。", conclusion="保留"))
    result = memory.run_memory_review(SOURCE, CONFIG, operation_id="prose-claim")
    # Do not pretend field validation can prove the meaning of arbitrary prose.
    # The separate program-generated result and structured receipt are definitive.
    assert result.status == "completed" and result.rule_receipts == []
    assert result.summary.endswith("程序回执：本轮未修改长期规则。")
    assert rules.list_trading_rules("cfg") == []
