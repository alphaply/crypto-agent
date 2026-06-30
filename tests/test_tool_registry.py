from types import SimpleNamespace

import pytest

from backend.agent import chat_graph
from backend.agent import agent_tools
from backend.agent import tool_registry


def _tool_names(tools):
    return [tool.name for tool in tools]


def test_trade_tools_for_real_mode_include_cancel_real():
    assert _tool_names(tool_registry.get_trade_tools_for_mode("REAL")) == [
        "open_position_real",
        "close_position_real",
        "cancel_orders_real",
    ]


def test_trade_tools_for_spot_dca_mode_include_cancel_real():
    assert _tool_names(tool_registry.get_trade_tools_for_mode("SPOT_DCA")) == [
        "open_position_spot_dca",
        "cancel_orders_real",
    ]


def test_trade_tools_for_strategy_mode_include_cancel_strategy_and_close():
    assert _tool_names(tool_registry.get_trade_tools_for_mode("STRATEGY")) == [
        "open_position_strategy",
        "cancel_orders_strategy",
        "close_position_strategy",
    ]


def test_chat_tools_for_real_mode_include_cancel_real():
    assert "cancel_orders_real" in _tool_names(chat_graph._get_chat_tools({"mode": "REAL"}))


def test_cancel_real_tool_schema_uses_single_order_id():
    cancel_tool = next(
        tool for tool in tool_registry.get_trade_tools_for_mode("REAL")
        if tool.name == "cancel_orders_real"
    )

    assert list(cancel_tool.args_schema.model_fields.keys()) == ["order_id", "reason"]
    assert "order_ids" not in cancel_tool.args


def test_cancel_strategy_tool_schema_uses_order_id_and_reason():
    cancel_tool = next(
        tool for tool in tool_registry.get_trade_tools_for_mode("STRATEGY")
        if tool.name == "cancel_orders_strategy"
    )

    assert list(cancel_tool.args_schema.model_fields.keys()) == ["order_id", "reason"]
    assert "order_ids" not in cancel_tool.args


def test_run_trade_tool_injects_config_and_symbol(monkeypatch):
    calls = []

    def fake_func(**kwargs):
        calls.append(kwargs)
        return "ok"

    monkeypatch.setitem(
        tool_registry._TOOL_BY_NAME,
        "cancel_orders_real",
        SimpleNamespace(func=fake_func),
    )

    result = tool_registry.run_trade_tool(
        "cancel_orders_real",
        {"order_id": "order-1", "reason": "price invalidated"},
        config_id="cfg-real",
        symbol="ETH/USDT",
    )

    assert result == "ok"
    assert calls == [
        {
            "order_id": "order-1",
            "reason": "price invalidated",
            "config_id": "cfg-real",
            "symbol": "ETH/USDT",
        }
    ]


def test_run_trade_tool_unknown_tool_returns_error():
    assert tool_registry.run_trade_tool("missing_tool", {}, "cfg", "ETH/USDT") == (
        "Error: Tool 'missing_tool' not found."
    )


@pytest.mark.parametrize("tool_name", ["cancel_orders_real", "cancel_orders_strategy"])
def test_run_trade_tool_expands_legacy_cancel_list_args(monkeypatch, tool_name):
    calls = []

    def fake_func(**kwargs):
        calls.append(kwargs)
        return "cancelled"

    monkeypatch.setitem(tool_registry._TOOL_BY_NAME, tool_name, SimpleNamespace(func=fake_func))

    result = tool_registry.run_trade_tool(tool_name, ["order-1", "order-2"], "cfg", "BTC/USDT")

    assert result == "cancelled\ncancelled"
    assert [call["order_id"] for call in calls] == ["order-1", "order-2"]
    assert [call["reason"] for call in calls] == ["未提供撤单原因", "未提供撤单原因"]


def test_run_trade_tool_expands_legacy_cancel_order_ids_dict(monkeypatch):
    calls = []

    def fake_func(**kwargs):
        calls.append(kwargs)
        return kwargs["order_id"]

    monkeypatch.setitem(
        tool_registry._TOOL_BY_NAME,
        "cancel_orders_real",
        SimpleNamespace(func=fake_func),
    )

    result = tool_registry.run_trade_tool(
        "cancel_orders_real",
        {"order_ids": ["order-1", "order-2"]},
        "cfg",
        "BTC/USDT",
    )

    assert result == "order-1\norder-2"
    assert [call["order_id"] for call in calls] == ["order-1", "order-2"]
    assert [call["reason"] for call in calls] == ["未提供撤单原因", "未提供撤单原因"]


def test_run_trade_tool_adds_default_cancel_reason_for_legacy_string(monkeypatch):
    calls = []

    def fake_func(**kwargs):
        calls.append(kwargs)
        return "ok"

    monkeypatch.setitem(
        tool_registry._TOOL_BY_NAME,
        "cancel_orders_real",
        SimpleNamespace(func=fake_func),
    )

    result = tool_registry.run_trade_tool("cancel_orders_real", "order-1", "cfg", "BTC/USDT")

    assert result == "ok"
    assert calls[0]["order_id"] == "order-1"
    assert calls[0]["reason"] == "未提供撤单原因"


class _FakeConn:
    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, *_args, **_kwargs):
        return self

    def fetchone(self):
        if self.rows:
            return self.rows.pop(0)
        return None

    def commit(self):
        return None


def test_cancel_orders_real_logs_user_reason(monkeypatch):
    saved = {}

    class FakeMarketTool:
        def __init__(self, **_kwargs):
            pass

        def place_real_order(self, *_args, **_kwargs):
            return {"status": "cancelled"}

    monkeypatch.setattr("backend.config.config.get_config_by_id", lambda _config_id: {"model": "model-a"})
    monkeypatch.setattr(agent_tools, "MarketTool", FakeMarketTool)
    monkeypatch.setattr(agent_tools.database, "get_db_conn", lambda: _FakeConn([None, None]))
    monkeypatch.setattr(
        agent_tools.database,
        "save_order_log",
        lambda *args, **kwargs: saved.update({"args": args, "kwargs": kwargs}),
    )

    result = agent_tools.cancel_orders_real.func(
        order_id="order-1",
        reason="structure invalidated",
        config_id="cfg-a",
        symbol="BTC/USDT",
    )

    assert "Cancelled Real" in result
    assert saved["args"][7] == "撤单成功: order-1 | structure invalidated"
    assert saved["kwargs"]["event_type"] == "CANCELLED"


def test_cancel_orders_real_attempts_exchange_cancel_when_local_status_closed(monkeypatch):
    calls = []

    class FakeMarketTool:
        def __init__(self, **_kwargs):
            pass

        def place_real_order(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {"status": "cancelled"}

    monkeypatch.setattr("backend.config.config.get_config_by_id", lambda _config_id: {"model": "model-a"})
    monkeypatch.setattr(agent_tools, "MarketTool", FakeMarketTool)
    monkeypatch.setattr(
        agent_tools.database,
        "get_db_conn",
        lambda: _FakeConn([{"side": "BUY", "status": "CLOSED"}, {"side": "BUY"}]),
    )
    monkeypatch.setattr(agent_tools.database, "save_order_log", lambda *args, **kwargs: None)

    result = agent_tools.cancel_orders_real.func(
        order_id="stop-1",
        reason="position already closed",
        config_id="cfg-a",
        symbol="BTC/USDT",
    )

    assert "Cancelled Real" in result
    assert calls
    assert calls[0][0][:3] == ("BTC/USDT", "CANCEL", {"cancel_order_id": "stop-1"})


def test_cancel_orders_strategy_logs_user_reason(monkeypatch):
    saved = {}

    monkeypatch.setattr(agent_tools.database, "get_db_conn", lambda: _FakeConn([{"side": "BUY"}, None]))
    monkeypatch.setattr(agent_tools.database, "cancel_mock_order", lambda _order_id: True)
    monkeypatch.setattr(
        agent_tools.database,
        "save_order_log",
        lambda *args, **kwargs: saved.update({"args": args, "kwargs": kwargs}),
    )

    result = agent_tools.cancel_orders_strategy.func(
        order_id="ST-1",
        reason="setup expired",
        config_id="cfg-a",
        symbol="BTC/USDT",
    )

    assert "Cancelled Strategy" in result
    assert saved["args"][7] == "[Strategy] 撤单成功: ST-1 | setup expired"
    assert saved["kwargs"]["event_type"] == "CANCELLED"
