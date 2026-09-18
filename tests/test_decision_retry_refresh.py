from unittest.mock import Mock, patch

import httpx
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage
from openai import APIConnectionError

from backend.agent import agent_graph as graph
from backend.agent.agent_models import AgentState
from backend.utils import llm_utils


@pytest.fixture
def fresh_data(monkeypatch):
    market = Mock()
    market.get_market_analysis.return_value = {
        "analysis": {"15m": {"price": 200, "atr": 7, "ema": {}, "macd": {}, "bollinger": {}}},
        "timestamp": 123, "sentiment": {},
    }
    market.get_account_status.return_value = {
        "balance": 900, "available_balance": 800, "mock_open_orders": [], "error": None,
    }
    monkeypatch.setattr(graph, "MarketTool", Mock(return_value=market))
    monkeypatch.setattr(graph, "resolve_market_timeframes", lambda _: ["15m"])
    monkeypatch.setattr(graph, "fetch_news_risk_context", lambda _: {})
    monkeypatch.setattr(graph.database, "save_news_snapshot", Mock())
    monkeypatch.setattr(graph, "get_daily_summaries", Mock(return_value=[]))
    monkeypatch.setattr(graph, "format_short_memory_for_llm", Mock(return_value="memory"))
    monkeypatch.setattr(graph, "resolve_prompt_template", Mock(return_value="{short_memory_text}"))
    monkeypatch.setattr(graph, "render_prompt", lambda template, **kw: f"price={kw['current_price']} atr={kw['atr_15m']} balance={kw['balance']}")
    monkeypatch.setattr(graph, "get_trade_tools_for_mode", Mock(return_value=[]))
    monkeypatch.setattr(llm_utils, "get_llm_max_retries", lambda: 1)
    monkeypatch.setattr(llm_utils.time, "sleep", Mock())
    return market


def initial_state():
    return AgentState(symbol="BTC/USDT", messages=[SystemMessage(content="price=100 atr=2"),
        HumanMessage(content="analyze"),
        AIMessage(content="", tool_calls=[{"id": "executed", "name": "trade", "args": {}}]),
        ToolMessage(content="already placed", tool_call_id="executed")],
        market_context={}, account_context={}, history_context=[], human_message="analyze")


@pytest.mark.parametrize("node", [graph.agent_node, graph.small_agent_node])
def test_disconnect_refreshes_prompt_and_preserves_completed_tools(fresh_data, monkeypatch, node):
    requests = []

    class Model:
        def bind_tools(self, tools):
            return self

        def stream(self, messages, config=None):
            requests.append(list(messages))
            if len(requests) == 1:
                yield AIMessageChunk(content="partial")
                raise APIConnectionError(request=httpx.Request("POST", "https://example.test"))
            yield AIMessageChunk(content="fresh decision")

    monkeypatch.setattr(graph, "build_chat_model", Mock(return_value=Model()))
    trade = Mock()
    monkeypatch.setattr(graph, "run_trade_tool", trade)
    result = node(initial_state(), {"configurable": {"agent_config": {"model": "test"}}})
    assert len(requests) == 2
    assert "price=100" in requests[0][0].content
    assert "price=200 atr=7 balance=800" in requests[1][0].content
    assert sum(isinstance(msg, ToolMessage) for msg in requests[1]) == 1
    assert not any(msg.content == "partial" for msg in requests[1])
    assert result.market_context["analysis"]["15m"]["atr"] == 7
    assert result.account_context["available_balance"] == 800
    assert result.messages[-1].content == "fresh decision"
    fresh_data.get_market_analysis.assert_called_once()
    fresh_data.get_account_status.assert_called_once()
    trade.assert_not_called()


def test_fallback_gets_another_fresh_snapshot_and_correct_role(fresh_data, monkeypatch):
    monkeypatch.setattr(llm_utils, "get_llm_max_retries", lambda: 0)
    requests = []

    class Model:
        def bind_tools(self, tools):
            return self

        def stream(self, messages, config=None):
            requests.append(list(messages))
            if len(requests) == 1:
                raise httpx.RemoteProtocolError("disconnected")
            yield AIMessageChunk(content="fallback result")

    monkeypatch.setattr(graph, "build_chat_model", Mock(return_value=Model()))
    result = graph.agent_node(initial_state(), {"configurable": {"agent_config": {
        "model": "primary", "fallback_models": [{"model": "fallback", "system_prompt_role": "user"}],
    }}})
    assert result.active_model_name == "fallback"
    assert isinstance(requests[1][0], HumanMessage)
    assert "price=200" in requests[1][0].content
    fresh_data.get_market_analysis.assert_called_once()


@pytest.mark.parametrize("failure", ["missing_timeframe", "account_error", "exception", "stale"])
def test_refresh_failure_stops_model_chain(fresh_data, monkeypatch, failure):
    if failure == "missing_timeframe":
        fresh_data.get_market_analysis.return_value = {"analysis": {}}
    elif failure == "account_error":
        fresh_data.get_account_status.return_value = {"error": "offline"}
    elif failure == "stale":
        fresh_data.get_market_analysis.return_value["analysis"]["15m"]["data_quality"] = {"stale": True}
    else:
        fresh_data.get_market_analysis.side_effect = RuntimeError("offline")
    model = Mock()
    model.bind_tools.return_value = model
    model.stream.side_effect = httpx.RemoteProtocolError("disconnected")
    build = Mock(return_value=model)
    monkeypatch.setattr(graph, "build_chat_model", build)
    result = graph.agent_node(initial_state(), {"configurable": {"agent_config": {
        "model": "primary", "fallback_models": [{"model": "fallback"}],
    }}})
    assert result.messages[-1].additional_kwargs["invocation_failed"]
    assert "刷新失败" in result.messages[-1].content
    assert model.stream.call_count == 1
    assert build.call_count == 1


def test_every_transport_attempt_uses_admin_timeout_without_sdk_retries(monkeypatch):
    monkeypatch.setattr(graph.global_config, "llm_timeout_seconds", 1200)
    monkeypatch.setattr(graph.global_config, "llm_max_retries", 2)
    monkeypatch.setattr(llm_utils.time, "sleep", Mock())
    requests = []

    def disconnected(request):
        requests.append(request)
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")

    model = llm_utils.build_chat_model(model="test", api_key="test", base_url="https://example.test/v1")
    assert model.max_retries == 0
    assert model.request_timeout == 1200
    with httpx.Client(transport=httpx.MockTransport(disconnected), timeout=5) as client:
        model.root_client._client = client
        with pytest.raises(llm_utils.LLMInvocationError):
            llm_utils.invoke_with_retry(lambda: model.invoke("hello"), logger=Mock(), context="test")
    assert len(requests) == 3
    for request in requests:
        assert request.extensions["timeout"]["read"] == 1200


def test_repeated_refresh_replaces_snapshot_and_notice(fresh_data):
    config = {"configurable": {"agent_config": {"model": "test", "system_prompt_role": "user"}}}
    first = graph.refresh_decision_context(initial_state(), config)
    fresh_data.get_market_analysis.return_value["analysis"]["15m"]["price"] = 300
    second = graph.refresh_decision_context(first, config)
    assert "price=300" in second.messages[0].content
    assert "analyze" in second.messages[0].content
    assert sum(isinstance(msg, ToolMessage) for msg in second.messages) == 1
    assert sum(bool(msg.additional_kwargs.get("decision_refresh_notice")) for msg in second.messages) == 1
    assert not any("price=200" in msg.content for msg in second.messages)
