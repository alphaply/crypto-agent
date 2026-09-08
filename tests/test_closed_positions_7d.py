import pytest
from unittest.mock import patch
from datetime import datetime, timedelta

import backend.database as database
from backend.database_schema import initialize_schema
from backend.agent.agent_graph import format_recent_position_history_for_memory


@pytest.fixture
def local_db(tmp_path):
    with patch.object(database, 'DB_NAME', str(tmp_path / 'test_7d.db')):
        with database.get_db_conn() as conn:
            initialize_schema(conn)
        yield


def test_get_closed_positions_7d_strategy_mode(local_db):
    now = datetime.now(database.TZ_CN)
    in_window = (now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    open_time = (now - timedelta(days=2, hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    out_of_window = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")

    with database.get_db_conn() as conn:
        cursor = conn.cursor()
        # 1. Closed order within 7 days
        cursor.execute(
            """
            INSERT INTO mock_orders (order_id, timestamp, symbol, config_id, side, price, amount,
                                     stop_loss, take_profit, status, close_price, realized_pnl, close_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("m1", open_time, "BTC/USDT", "strat_cfg", "BUY", 80000.0, 0.1, 79000.0, 82000.0, "CLOSED", 82000.0, 200.0, in_window)
        )
        # 2. Closed order outside 7 days
        cursor.execute(
            """
            INSERT INTO mock_orders (order_id, timestamp, symbol, config_id, side, price, amount,
                                     stop_loss, take_profit, status, close_price, realized_pnl, close_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("m2", out_of_window, "BTC/USDT", "strat_cfg", "SELL", 81000.0, 0.1, 82000.0, 79000.0, "CLOSED", 80000.0, 100.0, out_of_window)
        )
        # 3. Open order within 7 days (should not be included)
        cursor.execute(
            """
            INSERT INTO mock_orders (order_id, timestamp, symbol, config_id, side, price, amount,
                                     stop_loss, take_profit, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("m3", in_window, "BTC/USDT", "strat_cfg", "BUY", 81500.0, 0.1, 80500.0, 83000.0, "OPEN")
        )
        conn.commit()

    with patch.object(database, '_config_mode_for_stats', return_value="STRATEGY"):
        positions = database.get_closed_positions_7d("strat_cfg", "BTC/USDT", days=7)

    assert len(positions) == 1
    p = positions[0]
    assert p["opened_at"] == open_time
    assert p["closed_at"] == in_window
    assert p["symbol"] == "BTC/USDT"
    assert p["side"] == "LONG"
    assert p["entry_price"] == 80000.0
    assert p["close_price"] == 82000.0
    assert p["amount"] == 0.1
    assert p["take_profit"] == 82000.0
    assert p["stop_loss"] == 79000.0
    assert p["realized_pnl"] == 200.0


def test_get_closed_positions_7d_real_mode_uses_execution_position_history(local_db):
    now = datetime.now(database.TZ_CN)
    close_dt = now - timedelta(days=1)
    open_dt = now - timedelta(days=1, hours=4)
    close_time = close_dt.strftime("%Y-%m-%d %H:%M:%S")
    open_time = open_dt.strftime("%Y-%m-%d %H:%M:%S")

    with database.get_db_conn() as conn:
        cursor = conn.cursor()
        # A stale snapshot row must not be presented as a complete trade.
        cursor.execute(
            """
            INSERT INTO position_history (config_id, symbol, position_key, side, status,
                                          closed_at, close_price, amount, realized_pnl, raw_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("real_cfg", "ETH/USDT:USDT", "key1", "SHORT", "CLOSED", close_time, 2300.0, 0.5, 50.0, "{}", close_time)
        )
        cursor.execute(
            """
            INSERT INTO execution_position_history(
                position_id, account_scope, config_id, symbol, side, opened_at_ms, closed_at_ms,
                entry_price, close_price, amount, take_profit, stop_loss, realized_pnl,
                fees_json, exit_reason, updated_at_ms, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ep1", "scope1", "real_cfg", "ETH/USDT:USDT", "SHORT",
                int(open_dt.timestamp() * 1000), int(close_dt.timestamp() * 1000),
                2400.0, 2300.0, 0.5, 2250.0, 2450.0, 50.0,
                '{"USDT": 0.2}', "take_profit", int(now.timestamp() * 1000), "{}",
            ),
        )
        conn.commit()

    with patch.object(database, '_config_mode_for_stats', return_value="REAL"):
        positions = database.get_closed_positions_7d("real_cfg", "ETH/USDT", days=7)

    assert len(positions) == 1
    p = positions[0]
    assert p["closed_at"] == close_time
    assert p["opened_at"] == open_time
    assert p["side"] == "SHORT"
    # Recovered entry price: for SHORT, entry = close + pnl/amount = 2300 + 50/0.5 = 2400.0
    assert p["entry_price"] == 2400.0
    assert p["close_price"] == 2300.0
    assert p["amount"] == 0.5
    assert p["take_profit"] == 2250.0
    assert p["stop_loss"] == 2450.0
    assert p["realized_pnl"] == 50.0


def test_format_closed_positions_summary_stats():
    # 1. Empty positions
    empty_summary = database.format_closed_positions_summary([], days=7)
    assert "暂无已重建的完整平仓周期" in empty_summary
    assert "完整平仓周期: 0 笔" in empty_summary
    assert "胜率: -" in empty_summary
    assert "盈亏比: -" in empty_summary

    # 2. Mixed win/loss positions
    positions = [
        {
            "opened_at": "2026-09-01 10:00:00",
            "closed_at": "2026-09-01 12:00:00",
            "symbol": "ETH/USDT",
            "side": "LONG",
            "entry_price": 2400.0,
            "close_price": 2460.0,
            "amount": 0.5,
            "take_profit": 2480.0,
            "stop_loss": 2380.0,
            "realized_pnl": 30.0,
        },
        {
            "opened_at": "2026-09-02 14:00:00",
            "closed_at": "2026-09-02 16:30:00",
            "symbol": "ETH/USDT",
            "side": "SHORT",
            "entry_price": 2500.0,
            "close_price": 2520.0,
            "amount": 0.5,
            "take_profit": 2450.0,
            "stop_loss": 2520.0,
            "realized_pnl": -10.0,
        },
    ]
    summary = database.format_closed_positions_summary(positions, days=7)
    assert "【过去7天平仓记录（本地成交账本）】" in summary
    assert "ETH/USDT LONG (多)" in summary
    assert "TP: 2480.00 | SL: 2380.00" in summary
    assert "盈利: +30.00 USDT (+2.50%)" in summary
    assert "ETH/USDT SHORT (空)" in summary
    assert "亏损: -10.00 USDT (-0.80%)" in summary

    assert "【7天战绩统计】" in summary
    assert "完整平仓周期: 2 笔" in summary
    assert "胜率: 50.0% (1胜 1负)" in summary
    # profit-loss ratio: 30 / 10 = 3.00
    assert "盈亏比: 3.00" in summary
    assert "平均盈利: +30.00 USDT" in summary
    assert "平均亏损: -10.00 USDT" in summary
    assert "已确认累计盈亏（手续费前）: +20.00 USDT" in summary


def test_format_recent_position_history_for_memory_integration(local_db):
    now = datetime.now(database.TZ_CN)
    opened = now - timedelta(hours=2)
    with database.get_db_conn() as conn:
        conn.execute(
            """INSERT INTO execution_position_history(
                   position_id,account_scope,config_id,symbol,side,opened_at_ms,closed_at_ms,
                   entry_price,close_price,amount,take_profit,stop_loss,realized_pnl,
                   fees_json,exit_reason,updated_at_ms,payload
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "ep-int", "scope-int", "cfg_int", "BTC/USDT:USDT", "LONG",
                int(opened.timestamp() * 1000), int(now.timestamp() * 1000),
                80000.0, 81000.0, 0.1, 81000.0, 79500.0, 100.0,
                "{}", "take_profit", int(now.timestamp() * 1000), "{}",
            ),
        )
        conn.commit()

    with patch.object(database, '_config_mode_for_stats', return_value="REAL"):
        text = format_recent_position_history_for_memory("cfg_int", {"mode": "REAL", "symbol": "BTC/USDT"})

    assert "【过去7天平仓记录（本地成交账本）】" in text
    assert "BTC/USDT:USDT LONG (多)" in text
    assert "TP: 81000.00" in text
    assert "盈利: +100.00 USDT" in text
    assert "胜率: 100.0%" in text
