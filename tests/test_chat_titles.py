import sqlite3
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph

from backend import database, database_agent_runs
from backend.agent import chat_graph, chat_titles
from backend.app.services import chat_service
from backend.database_schema import initialize_schema


@pytest.fixture
def local_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_NAME", str(tmp_path / "titles.sqlite"))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    database.create_chat_session("session", "cfg", "BTC/USDT", "保留原始标题")
    return "session"


@pytest.fixture
def cfg():
    return {
        "config_id": "cfg", "symbol": "BTC/USDT", "model": "deepseek-reasoner",
        "api_key": "offline-test", "api_base": "https://example.test/v1",
        "temperature": 0.5, "thinking_enabled": True, "reasoning_effort": "high",
        "compatibility_mode": "deepseek", "llm_provider_id": "reasoning-provider", "read_only": True,
        "extra_body": {"max_tokens": 8192},
    }


@pytest.fixture
def compacting_app(local_db, cfg, monkeypatch):
    workflow = StateGraph(chat_graph.ChatState)
    workflow.add_node("start", lambda state: {"messages": [HumanMessage(content=state["q"])]})
    workflow.add_node("model", chat_graph.model_node)
    workflow.set_entry_point("start")
    workflow.add_edge("start", "model")
    workflow.add_edge("model", END)
    app = workflow.compile(checkpointer=InMemorySaver())
    monkeypatch.setattr(chat_graph, "chat_app", app)
    monkeypatch.setattr(chat_graph, "sync_langsmith_environment", lambda: {})
    monkeypatch.setattr(chat_graph, "_resolve_chat_config", lambda _: cfg)
    monkeypatch.setattr(chat_graph, "CHAT_CONTEXT_RECENT_MESSAGES", 2)
    monkeypatch.setattr(chat_graph, "CHAT_CONTEXT_COMPACTION_BATCH", 2)
    monkeypatch.setattr(chat_graph, "invoke_with_retry", lambda operation, **_: operation())
    monkeypatch.setattr(chat_titles, "invoke_with_retry", lambda operation, **_: operation())
    history = []
    for index in range(3):
        history += [HumanMessage(content=f"投资组合目标 {index}"), AIMessage(content=f"历史分析 {index}")]
    config = {"configurable": {"thread_id": local_db, "config_id": "cfg"}}
    app.update_state(config, {"messages": history}, as_node="model")
    summary_llm = Mock(invoke=Mock(return_value=AIMessage(content="用户关注 BTC 现货组合与风险控制。")))
    answer_llm = Mock(stream=Mock(return_value=iter([AIMessageChunk(content="继续分析市场。")])))
    title_llm = Mock(invoke=Mock(return_value=AIMessage(content="BTC组合风险分析")))
    monkeypatch.setattr(chat_graph, "build_chat_model", lambda **kwargs: answer_llm if kwargs.get("streaming") else summary_llm)
    monkeypatch.setattr(chat_titles, "build_chat_model", lambda **_: title_llm)
    return app, config, summary_llm, title_llm


@pytest.mark.parametrize("role", ["system", "user"])
def test_title_uses_final_text_and_keeps_reasoning_provider_settings(local_db, cfg, monkeypatch, role):
    cfg["system_prompt_role"] = role
    response = AIMessage(
        content=[{"type": "reasoning", "reasoning": "私有推理内容"}, {"type": "text", "text": "“BTC组合分析”"}],
        usage_metadata={"input_tokens": 20, "output_tokens": 80, "total_tokens": 100},
    )
    llm = Mock(invoke=Mock(return_value=response))
    build = Mock(return_value=llm)
    monkeypatch.setattr(chat_titles, "build_chat_model", build)
    monkeypatch.setattr(chat_titles, "invoke_with_retry", lambda operation, **_: operation())
    assert chat_titles.generate_chat_title("完整对话资料", cfg, session_id=local_db, config_id="cfg") == "BTC组合分析"
    kwargs = build.call_args.kwargs
    assert kwargs["thinking_enabled"] is True and kwargs["reasoning_effort"] == "high"
    assert kwargs["compatibility_mode"] == "deepseek" and kwargs["extra_body"] == {"max_tokens": 8192}
    assert "max_tokens" not in kwargs and kwargs["streaming"] is False
    messages = llm.invoke.call_args.args[0]
    assert isinstance(messages[-1], HumanMessage)
    assert isinstance(messages[0], HumanMessage if role == "user" else SystemMessage)
    assert "完整对话资料" in messages[-1].content
    runs = database_agent_runs.list_agent_runs(config_id="cfg")["runs"]
    assert len(runs) == 1 and runs[0]["status"] == "success"
    assert runs[0]["prompt_tokens"] == 20 and runs[0]["completion_tokens"] == 80


@pytest.mark.parametrize("response", [
    AIMessage(content="", additional_kwargs={"reasoning_content": "只有思考没有答案"}),
    AIMessage(content=[{"type": "reasoning", "reasoning": "Only reasoning"}]),
    AIMessage(content="看似完整的标题", response_metadata={"finish_reason": "length"}),
    AIMessage(content="不完整标题", response_metadata={"status": "incomplete"}),
    AIMessage(content="", additional_kwargs={"refusal": "拒绝"}),
    AIMessage(content="标题", tool_calls=[{"name": "buy", "args": {}, "id": "call1"}]),
    AIMessage(content="这是解释。\n标题在第二行"),
])
def test_invalid_title_preserves_existing_title_and_audits_usage(local_db, cfg, monkeypatch, response):
    response.usage_metadata = {"input_tokens": 10, "output_tokens": 30, "total_tokens": 40}
    monkeypatch.setattr(chat_titles, "build_chat_model", lambda **_: Mock(invoke=Mock(return_value=response)))
    monkeypatch.setattr(chat_titles, "invoke_with_retry", lambda operation, **_: operation())
    monkeypatch.setattr(chat_service, "get_chat_state", lambda *_, **__: {"messages": [HumanMessage(content="讨论组合风险")]})
    monkeypatch.setattr(chat_service, "_session_llm_config", lambda _: cfg)
    result = chat_service.summarize_chat_title_payload(local_db)
    assert result == {"title": "保留原始标题", "updated": False}
    assert database.get_chat_session(local_db)["title"] == "保留原始标题"
    runs = database_agent_runs.list_agent_runs(config_id="cfg")["runs"]
    assert len(runs) == 1 and runs[0]["status"] == "error"
    assert runs[0]["prompt_tokens"] == 10 and runs[0]["completion_tokens"] == 30


@pytest.mark.parametrize("role", ["system", "user"])
def test_manual_compaction_names_only_after_checkpoint_save_and_once_per_batch(compacting_app, cfg, monkeypatch, role):
    app, config, summary_llm, title_llm = compacting_app
    cfg["system_prompt_role"] = role
    seen_cursors = []

    def title_response(_messages):
        seen_cursors.append(app.get_state(config).values["conversation_summary_cursor"])
        return AIMessage(content="新的组合分析标题")

    title_llm.invoke.side_effect = title_response
    memory = chat_graph.compact_chat_memory("session", config_id="cfg")
    assert memory["summarized_message_count"] == 4 and seen_cursors == [4]
    assert database.get_chat_session("session")["title"] == "新的组合分析标题"
    messages = summary_llm.invoke.call_args.args[0]
    assert isinstance(messages[-1], HumanMessage)
    assert isinstance(messages[0], HumanMessage if role == "user" else SystemMessage)
    chat_graph.compact_chat_memory("session", config_id="cfg")
    assert title_llm.invoke.call_count == 1 and summary_llm.invoke.call_count == 1

    app.update_state(config, {"messages": [HumanMessage(content="新的投资约束"), AIMessage(content="新的分析")]}, as_node="model")
    chat_graph.compact_chat_memory("session", config_id="cfg")
    assert title_llm.invoke.call_count == 2 and seen_cursors == [4, 6]


def test_automatic_compaction_emits_saved_title_once(compacting_app):
    app, config, _, title_llm = compacting_app
    events = list(chat_graph.stream_chat("session", {"config_id": "cfg", "q": "继续检查风险"}))
    assert [event for event in events if event.get("type") == "session_title"] == [
        {"type": "session_title", "session_id": "session", "title": "BTC组合风险分析"}
    ]
    assert app.get_state(config).values["conversation_summary_cursor"] == 4
    chat_graph._refresh_compacted_chat_title(config)
    assert title_llm.invoke.call_count == 1


def test_failed_compaction_does_not_name_and_failure_is_audited(compacting_app):
    _, _, summary_llm, title_llm = compacting_app
    summary_llm.invoke.return_value = AIMessage(
        content="Partial memory", response_metadata={"finish_reason": "length"},
        usage_metadata={"input_tokens": 11, "output_tokens": 12, "total_tokens": 23},
    )
    assert chat_graph.compact_chat_memory("session", config_id="cfg")["summarized_message_count"] == 0
    title_llm.invoke.assert_not_called()
    assert database.get_chat_session("session")["title_summary_cursor"] == 0
    runs = database_agent_runs.list_agent_runs(config_id="cfg")["runs"]
    assert len(runs) == 1 and runs[0]["status"] == "error" and runs[0]["completion_tokens"] == 12


def test_title_failure_keeps_compaction_and_does_not_repeat_batch(compacting_app):
    app, config, _, title_llm = compacting_app
    title_llm.invoke.return_value = AIMessage(content="", additional_kwargs={"reasoning_content": "no final text"})
    memory = chat_graph.compact_chat_memory("session", config_id="cfg")
    assert memory["summarized_message_count"] == 4
    assert app.get_state(config).values["conversation_summary"] == memory["summary"]
    assert database.get_chat_session("session")["title"] == "保留原始标题"
    chat_graph._refresh_compacted_chat_title(config)
    assert title_llm.invoke.call_count == 1


def test_title_source_uses_rolling_memory_and_final_recent_messages():
    state = {
        "conversation_summary": "原始用户目标的摘要", "conversation_summary_cursor": 1,
        "messages": [HumanMessage(content="已摘要的原始消息"),
                     AIMessage(content=[{"type": "reasoning", "reasoning": "不要用于标题"}, {"type": "text", "text": "可见的答案"}]),
                     ToolMessage(content="无需传入原始工具内容", tool_call_id="1")],
    }
    source = chat_titles.chat_title_source(state)
    assert "原始用户目标的摘要" in source and "可见的答案" in source
    assert "已摘要的原始消息" not in source and "不要用于标题" not in source and "原始工具内容" not in source


def test_claim_handles_deleted_session_branch_and_clear_without_stale_overwrite(local_db, monkeypatch):
    old_token = database.claim_chat_title_summary(local_db, 4)
    assert old_token and database.claim_chat_title_summary(local_db, 4) is None
    assert database.claim_chat_title_summary("deleted", 4) is None
    database.create_chat_session("branch", "cfg", "BTC/USDT", "分支", parent_session_id=local_db)
    assert database.claim_chat_title_summary("branch", 4)
    monkeypatch.setattr(chat_service, "delete_chat_threads", lambda _: None)
    chat_service.clear_chat_messages_payload(local_db)
    assert database.get_chat_session(local_db)["title_summary_cursor"] == 0
    new_token = database.claim_chat_title_summary(local_db, 4)
    assert new_token and new_token != old_token
    assert not database.update_chat_session_title(local_db, "过期标题", summary_token=old_token)
    assert database.update_chat_session_title(local_db, "当前标题", summary_token=new_token)


def test_existing_chat_session_schema_migrates_title_claim_fields(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE chat_sessions (session_id TEXT PRIMARY KEY, title TEXT, config_id TEXT, symbol TEXT, created_at TEXT, updated_at TEXT)")
        conn.execute("INSERT INTO chat_sessions VALUES ('legacy','旧标题','cfg','BTC/USDT','old','old')")
        initialize_schema(conn)
    monkeypatch.setattr(database, "DB_NAME", str(path))
    assert database.get_chat_session("legacy")["title_summary_cursor"] == 0
    assert database.claim_chat_title_summary("legacy", 4)


def test_manual_compact_payload_returns_latest_title(compacting_app):
    payload = chat_service.compact_chat_memory_payload("session")
    assert payload["session"]["title"] == "BTC组合风险分析"


def test_stream_done_returns_latest_title_and_saved_memory(compacting_app):
    events = list(chat_service.stream_chat_events("session", user_input="继续检查风险"))
    done = events[-1]
    assert done["type"] == "done" and done["persisted"] is True
    assert done["session"]["title"] == "BTC组合风险分析"
    assert done["conversation_memory"]["summarized_message_count"] == 4
    assert done["messages"][-1]["content"] == "继续分析市场。"


def test_optional_title_readback_failure_keeps_persisted_done(compacting_app, monkeypatch):
    original = chat_service.get_chat_session
    reads = 0

    def get_session(session_id):
        nonlocal reads
        reads += 1
        if reads > 1:
            raise RuntimeError("Session metadata temporarily unavailable")
        return original(session_id)

    monkeypatch.setattr(chat_service, "get_chat_session", get_session)
    done = list(chat_service.stream_chat_events("session", user_input="继续检查风险"))[-1]
    assert done["type"] == "done" and done["persisted"] is True
    assert done["persistence_error"] is None
    assert done["messages"][-1]["content"] == "继续分析市场。"
