import sqlite3
from contextlib import contextmanager

from langchain_core.messages import HumanMessage

from backend.app.services import chat_service
from backend.agent import chat_graph
from backend.database_chat import ChatSessionStore
from backend.database_schema import initialize_schema


def _runtime_snapshot():
    return {
        "llm_providers": [
            {
                "provider_id": "llm-a",
                "name": "Qwen",
                "model": "qwen-test",
                "api_base": "https://llm.example/v1",
                "api_key": "llm-secret",
                "extra_body": {},
                "thinking_enabled": True,
                "reasoning_effort": "high",
                "system_prompt_role": "user",
            }
        ],
        "exchange_profiles": [
            {
                "profile_id": "exchange-a",
                "name": "Binance main",
                "exchange": "binance",
                "api_key": "exchange-key",
                "secret": "exchange-secret",
            }
        ],
    }


def test_temporary_runtime_stores_references_but_not_secrets(monkeypatch):
    monkeypatch.setattr(chat_service, "_runtime_snapshot", _runtime_snapshot)

    stored, effective = chat_service._resolve_temporary_runtime(
        {
            "exchange_profile_id": "exchange-a",
            "market_type": "swap",
            "symbol": "BTC/USDT:USDT",
            "llm_provider_id": "llm-a",
            "global_requirement": "focus on risk",
        }
    )

    assert stored["exchange_profile_id"] == "exchange-a"
    assert stored["llm_provider_id"] == "llm-a"
    assert stored["global_requirement"] == "focus on risk"
    assert stored["system_prompt_role"] == "user"
    assert "api_key" not in stored
    assert "secret" not in stored
    assert effective["api_key"] == "llm-secret"
    assert effective["exchange_profile"]["secret"] == "exchange-secret"


def test_market_symbol_catalogue_is_cached_and_filtered(monkeypatch):
    calls = []

    class FakeMarketTool:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def list_symbols(self, market_type):
            return [
                {"symbol": "BTC/USDT:USDT", "base": "BTC", "quote": "USDT", "market_type": market_type, "display_name": "BTC"},
                {"symbol": "ETH/USDT:USDT", "base": "ETH", "quote": "USDT", "market_type": market_type, "display_name": "ETH"},
            ]

    monkeypatch.setattr(chat_service, "_runtime_snapshot", _runtime_snapshot)
    monkeypatch.setattr(chat_service, "MarketTool", FakeMarketTool)
    chat_service._market_symbol_cache.clear()

    first = chat_service.list_market_symbols_payload("exchange-a", "swap", "btc")
    second = chat_service.list_market_symbols_payload("exchange-a", "swap", "eth")

    assert [item["symbol"] for item in first["symbols"]] == ["BTC/USDT:USDT"]
    assert [item["symbol"] for item in second["symbols"]] == ["ETH/USDT:USDT"]
    assert len(calls) == 1


def test_chat_session_schema_persists_temporary_runtime(tmp_path):
    db_path = tmp_path / "chat.db"
    conn = sqlite3.connect(db_path)
    initialize_schema(conn)
    conn.commit()
    conn.close()

    @contextmanager
    def connect():
        db_conn = sqlite3.connect(db_path)
        db_conn.row_factory = sqlite3.Row
        try:
            yield db_conn
        finally:
            db_conn.close()

    store = ChatSessionStore(connect, lambda: "2026-07-10 10:00:00")
    store.create_session(
        "session-a",
        "",
        "BTC/USDT",
        "Temporary chat",
        session_type="temporary",
        runtime_json='{"exchange_profile_id":"exchange-a"}',
    )

    row = store.get_session("session-a")
    assert row["session_type"] == "temporary"
    assert row["runtime_json"] == '{"exchange_profile_id":"exchange-a"}'


def test_temporary_chat_renders_full_technical_context_without_task_history(monkeypatch):
    calls = []

    class FakeMarketTool:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def get_market_analysis(self, symbol, mode, timeframes):
            calls.append({"symbol": symbol, "mode": mode, "timeframes": timeframes})
            return {
                "symbol": symbol,
                "sentiment": {"funding_rate": 0.0001, "open_interest": 1234567},
                "analysis": {
                    "15m": {
                        "price": 100000,
                        "trend": {"adx": 28, "di_plus": 31, "di_minus": 16},
                        "atr": 850,
                        "ema": {"ema_20": 99800, "ema_50": 99000, "ema_100": 98000, "ema_200": 96000},
                        "rsi_analysis": {"rsi": 58.4},
                        "macd": {"diff": 120, "hist": 80, "momentum": "expanding"},
                        "bollinger": {"up": 101000, "low": 97000, "width": 0.04},
                        "volume_analysis": {"status": "Normal"},
                        "vp": {"poc": 99500, "vah": 100200, "val": 98700, "hvns": [99500]},
                        "recent_opens": [99000],
                        "recent_highs": [100500],
                        "recent_lows": [98500],
                        "recent_closes": [100000],
                        "smc": {},
                        "liquidity_sweep_ifvg": {},
                        "vwap": 99500,
                    }
                },
            }

        def get_account_status(self, *_args, **_kwargs):
            return {"balance": 1000, "available_balance": 800, "real_positions": [], "real_open_orders": []}

    monkeypatch.setattr(chat_graph, "MarketTool", FakeMarketTool)
    monkeypatch.setattr(chat_graph, "fetch_news_risk_context", lambda *_args, **_kwargs: {"risk_level": "normal", "headlines": ["BTC market headline"], "source": "test"})

    updates = chat_graph._start_temporary_chat(
        {"q": "Analyze BTC"},
        {},
        {
            "symbol": "BTC/USDT:USDT",
            "market_type": "swap",
            "exchange": "binance",
            "exchange_profile": {},
        },
    )

    prompt = updates["system_prompt"]
    assert calls[1]["timeframes"] == list(chat_graph.TEMPORARY_CHAT_TIMEFRAMES)
    assert "1w" in calls[1]["timeframes"]
    assert "[15m] ADX=28" in prompt
    assert "EMA: 20=99800" in prompt
    assert "RSI=58.4" in prompt
    assert "Volume Profile: POC=99500" in prompt
    assert "{formatted_market_data}" not in prompt
    assert "BTC market headline" in prompt
    assert "## Analysis limitations" in prompt
    assert "temporary chats do not use task short-term memory" in prompt
    assert updates["messages"][0].content == "Analyze BTC"


def test_temporary_chat_marks_failed_account_data_as_unavailable():
    context = chat_graph._temporary_account_context_text(
        {"available": False, "error": "exchange request timed out"}
    )

    assert "unavailable" in context
    assert "Balance: 0" not in context


def test_temporary_chat_retry_does_not_append_the_same_user_message(monkeypatch):
    class FakeMarketTool:
        def __init__(self, **_kwargs):
            pass

        def get_market_analysis(self, *_args, **_kwargs):
            return {"analysis": {"15m": {"price": 100}}}

        def get_account_status(self, *_args, **_kwargs):
            return {"balance": 1, "available_balance": 1, "real_positions": [], "real_open_orders": []}

    monkeypatch.setattr(chat_graph, "MarketTool", FakeMarketTool)
    monkeypatch.setattr(chat_graph, "fetch_news_risk_context", lambda *_args, **_kwargs: {"headlines": ["headline"]})

    updates = chat_graph._start_temporary_chat(
        {"q": "Analyze BTC", "retry_last": True},
        {},
        {"symbol": "BTC/USDT", "market_type": "spot", "exchange": "binance", "exchange_profile": {}},
    )

    assert "messages" not in updates


def test_temporary_chat_retry_can_replace_the_failed_user_message(monkeypatch):
    class FakeMarketTool:
        def __init__(self, **_kwargs):
            pass

        def get_market_analysis(self, *_args, **_kwargs):
            return {"analysis": {"15m": {"price": 100}}}

        def get_account_status(self, *_args, **_kwargs):
            return {"balance": 1, "available_balance": 1, "real_positions": [], "real_open_orders": []}

    original = HumanMessage(content="Analyze BTC", id="user-message-1")
    monkeypatch.setattr(chat_graph, "MarketTool", FakeMarketTool)
    monkeypatch.setattr(chat_graph, "fetch_news_risk_context", lambda *_args, **_kwargs: {"headlines": ["headline"]})

    updates = chat_graph._start_temporary_chat(
        {"q": "Analyze ETH", "retry_last": True, "replace_last_user_message": True, "messages": [original]},
        {},
        {"symbol": "BTC/USDT", "market_type": "spot", "exchange": "binance", "exchange_profile": {}},
    )

    assert updates["messages"][0].content == "Analyze ETH"
    assert updates["messages"][0].id == "user-message-1"
