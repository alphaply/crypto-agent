import sqlite3
from contextlib import contextmanager
from unittest.mock import Mock

from backend.app.services.dashboard_service import _scheduler_timestamp_iso
from backend.database_schema import initialize_schema
from backend.database_summary import SummaryStore


def test_reasoning_tokens_migrate_and_persist_with_summaries():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            symbol TEXT,
            timeframe TEXT,
            content TEXT,
            strategy_logic TEXT
        )
        """
    )
    initialize_schema(conn)

    @contextmanager
    def temp_conn():
        yield conn

    store = SummaryStore(temp_conn, lambda: "2026-08-13 12:00:00", Mock())
    store.save_summary(
        "BTC/USDT",
        "model-a",
        "answer",
        "strategy",
        config_id="cfg-a",
        reasoning_content="risk check",
        reasoning_tokens=456,
    )

    row = conn.execute(
        "SELECT reasoning_content, reasoning_tokens FROM summaries WHERE config_id = ?",
        ("cfg-a",),
    ).fetchone()
    conn.close()

    assert row == ("risk check", 456)


def test_dashboard_scheduler_timestamp_includes_china_timezone():
    assert _scheduler_timestamp_iso("2026-08-13 12:00:00") == "2026-08-13T12:00:00+08:00"
