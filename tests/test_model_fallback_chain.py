import tempfile
import sqlite3
import os
from pathlib import Path
from unittest.mock import Mock, patch
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage

from backend.agent.agent_graph import (
    agent_node,
    adapt_messages_for_prompt_role,
    resolve_decision_model_chain,
)
from backend.agent.agent_models import AgentState
from backend.utils.llm_utils import LLMInvocationError


@pytest.fixture(autouse=True)
def refresh_snapshot_stub():
    # Model-chain unit tests isolate market I/O; refresh integration is covered separately.
    with patch("backend.agent.agent_graph.refresh_decision_context", side_effect=lambda state, config: state):
        yield


class _MockModel:
    def __init__(self, name="model", fail_times=0, error_message="Network error", tool_calls=None):
        self.name = name
        self.fail_times = fail_times
        self.error_message = error_message
        self.calls = 0
        self.tool_calls = tool_calls or []

    def bind_tools(self, tools):
        return self

    def stream(self, messages, config=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise LLMInvocationError(
                f"{self.name} failed: {self.error_message}",
                error_type="api_error",
                retryable=True,
                attempts=1,
            )
        if self.tool_calls:
            yield AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {
                        "id": "call-1",
                        "name": "market_tool",
                        "args": '{"symbol":"BTCUSDT"}',
                        "index": 0,
                    }
                ],
            )
        else:
            yield AIMessageChunk(content=f"Success from {self.name}")


def test_primary_model_succeeds_without_fallback():
    primary = _MockModel("primary-model", fail_times=0)
    fallback = _MockModel("fallback-model", fail_times=0)

    models_map = {"primary-model": primary, "fallback-model": fallback}

    def mock_build_chat_model(model, **kwargs):
        return models_map[model]

    progress = []
    state = AgentState(
        symbol="BTC/USDT",
        messages=[HumanMessage(content="analyze", additional_kwargs={"is_instruction": True})],
        market_context={},
        account_context={},
        history_context=[],
    )
    agent_config = {
        "model": "primary-model",
        "fallback_models": [{"model": "fallback-model"}],
    }
    config = {
        "configurable": {
            "agent_config": agent_config,
            "progress_callback": progress.append,
        }
    }

    with patch("backend.agent.agent_graph.build_chat_model", side_effect=mock_build_chat_model), \
         patch("backend.agent.agent_graph.get_trade_tools_for_mode", return_value=[]), \
         patch("backend.utils.llm_utils.get_llm_max_retries", return_value=1), \
         patch("backend.utils.llm_utils.time.sleep"):
        res = agent_node(state, config)

    assert primary.calls == 1
    assert fallback.calls == 0
    assert res.active_model_idx == 0
    assert res.active_model_name == "primary-model"
    assert "Success from primary-model" in res.messages[-1].content


def test_primary_model_fails_fallback_1_succeeds():
    primary = _MockModel("primary-model", fail_times=10, error_message="Rate limit 429")
    fallback = _MockModel("fallback-model", fail_times=0)

    models_map = {"primary-model": primary, "fallback-model": fallback}

    def mock_build_chat_model(model, **kwargs):
        return models_map[model]

    progress = []
    state = AgentState(
        symbol="BTC/USDT",
        messages=[HumanMessage(content="analyze", additional_kwargs={"is_instruction": True})],
        market_context={},
        account_context={},
        history_context=[],
    )
    agent_config = {
        "model": "primary-model",
        "fallback_models": [{"model": "fallback-model"}],
    }
    config = {
        "configurable": {
            "agent_config": agent_config,
            "progress_callback": progress.append,
        }
    }

    with patch("backend.agent.agent_graph.build_chat_model", side_effect=mock_build_chat_model), \
         patch("backend.agent.agent_graph.get_trade_tools_for_mode", return_value=[]), \
         patch("backend.utils.llm_utils.get_llm_max_retries", return_value=2), \
         patch("backend.utils.llm_utils.time.sleep"):
        res = agent_node(state, config)

    assert primary.calls == 3
    assert fallback.calls == 1
    assert res.active_model_idx == 1
    assert res.active_model_name == "fallback-model"
    assert "Success from fallback-model" in res.messages[-1].content

    fallback_events = [e for e in progress if "切换至兜底模型" in e.get("message", "")]
    assert len(fallback_events) >= 1
    assert "fallback-model" in fallback_events[0]["message"]


def test_primary_and_fallback_1_fail_fallback_2_succeeds():
    primary = _MockModel("m1", fail_times=10)
    fb1 = _MockModel("m2", fail_times=10)
    fb2 = _MockModel("m3", fail_times=0)

    models_map = {"m1": primary, "m2": fb1, "m3": fb2}

    def mock_build_chat_model(model, **kwargs):
        return models_map[model]

    progress = []
    state = AgentState(
        symbol="ETH/USDT",
        messages=[HumanMessage(content="analyze", additional_kwargs={"is_instruction": True})],
        market_context={},
        account_context={},
        history_context=[],
    )
    agent_config = {
        "model": "m1",
        "fallback_models": [{"model": "m2"}, {"model": "m3"}],
    }
    config = {
        "configurable": {
            "agent_config": agent_config,
            "progress_callback": progress.append,
        }
    }

    with patch("backend.agent.agent_graph.build_chat_model", side_effect=mock_build_chat_model), \
         patch("backend.agent.agent_graph.get_trade_tools_for_mode", return_value=[]), \
         patch("backend.utils.llm_utils.get_llm_max_retries", return_value=1), \
         patch("backend.utils.llm_utils.time.sleep"):
        res = agent_node(state, config)

    assert primary.calls == 2
    assert fb1.calls == 2
    assert fb2.calls == 1
    assert res.active_model_idx == 2
    assert res.active_model_name == "m3"
    assert "Success from m3" in res.messages[-1].content


def test_all_models_fail_emits_error_summary():
    primary = _MockModel("m1", fail_times=10, error_message="m1 timeout")
    fb1 = _MockModel("m2", fail_times=10, error_message="m2 500 error")

    models_map = {"m1": primary, "m2": fb1}

    def mock_build_chat_model(model, **kwargs):
        return models_map[model]

    progress = []
    state = AgentState(
        symbol="SOL/USDT",
        messages=[HumanMessage(content="analyze", additional_kwargs={"is_instruction": True})],
        market_context={},
        account_context={},
        history_context=[],
    )
    agent_config = {
        "model": "m1",
        "fallback_models": [{"model": "m2"}],
    }
    config = {
        "configurable": {
            "agent_config": agent_config,
            "progress_callback": progress.append,
        }
    }

    with patch("backend.agent.agent_graph.build_chat_model", side_effect=mock_build_chat_model), \
         patch("backend.agent.agent_graph.get_trade_tools_for_mode", return_value=[]), \
         patch("backend.utils.llm_utils.get_llm_max_retries", return_value=2), \
         patch("backend.utils.llm_utils.time.sleep"):
        res = agent_node(state, config)

    assert primary.calls == 3
    assert fb1.calls == 3
    last_msg = res.messages[-1]
    assert last_msg.additional_kwargs.get("invocation_failed") is True
    assert "决策模型链路失败或中止" in last_msg.content
    assert "m1" in last_msg.content
    assert "m2" in last_msg.content


def test_each_model_retries_configured_global_times():
    primary = _MockModel("m1", fail_times=10)
    fb1 = _MockModel("m2", fail_times=10)
    models_map = {"m1": primary, "m2": fb1}

    def mock_build_chat_model(model, **kwargs):
        return models_map[model]

    state = AgentState(
        symbol="BTC/USDT",
        messages=[HumanMessage(content="analyze", additional_kwargs={"is_instruction": True})],
        market_context={},
        account_context={},
        history_context=[],
    )
    agent_config = {
        "model": "m1",
        "fallback_models": [{"model": "m2"}],
    }
    config = {
        "configurable": {
            "agent_config": agent_config,
        }
    }

    with patch("backend.agent.agent_graph.build_chat_model", side_effect=mock_build_chat_model), \
         patch("backend.agent.agent_graph.get_trade_tools_for_mode", return_value=[]), \
         patch("backend.utils.llm_utils.get_llm_max_retries", return_value=3), \
         patch("backend.utils.llm_utils.time.sleep"):
        res = agent_node(state, config)

    assert primary.calls == 4
    assert fb1.calls == 4


def test_adapt_messages_for_prompt_role():
    sys_msg = SystemMessage(content="You are a trader", additional_kwargs={"is_instruction": True})
    human_msg = HumanMessage(content="User input")
    msgs = [sys_msg, human_msg]

    adapted_to_user = adapt_messages_for_prompt_role(msgs, "user")
    assert isinstance(adapted_to_user[0], HumanMessage)
    assert adapted_to_user[0].content == "You are a trader"
    assert adapted_to_user[0].additional_kwargs["is_instruction"] is True
    assert adapted_to_user[1] == human_msg

    adapted_to_sys = adapt_messages_for_prompt_role(adapted_to_user, "system")
    assert isinstance(adapted_to_sys[0], SystemMessage)
    assert adapted_to_sys[0].content == "You are a trader"


def test_resolve_decision_model_chain():
    cfg = {
        "model": "claude-3-7-sonnet",
        "api_key": "key1",
        "temperature": 0.7,
        "fallback_models": [
            {"model": "deepseek-chat", "api_key": "key2", "temperature": 0.2},
            {"model": "gpt-4o", "api_key": "key3"},
        ],
    }
    chain = resolve_decision_model_chain(cfg)
    assert len(chain) == 3
    assert chain[0]["model"] == "claude-3-7-sonnet"
    assert chain[0]["api_key"] == "key1"
    assert chain[0]["temperature"] == 0.7

    assert chain[1]["model"] == "deepseek-chat"
    assert chain[1]["api_key"] == "key2"
    assert chain[1]["temperature"] == 0.2

    assert chain[2]["model"] == "gpt-4o"
    assert chain[2]["api_key"] == "key3"
    assert chain[2]["temperature"] == 0.7


def test_config_store_fallback_llm_provider_ids():
    import backend.config_store as cs
    raw_agents = [
        {
            "config_id": "btc-task",
            "symbol": "BTC/USDT",
            "model": "m1",
            "llm_provider_id": "p1",
            "fallback_llm_provider_ids": ["p2", "p3", ""],
        }
    ]
    normalized = cs._normalize_agents(raw_agents)
    assert normalized[0]["fallback_llm_provider_ids"] == ["p2", "p3"]
