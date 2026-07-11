import sqlite3
from contextlib import contextmanager

from backend.app.services import dashboard_service
from backend.app.services import stats_service


class StubConfig:
    def get_all_symbol_configs(self):
        return [
            {"config_id": "real-a", "symbol": "BTC/USDT", "mode": "REAL", "enabled": True},
            {"config_id": "strategy-a", "symbol": "BTC/USDT", "mode": "STRATEGY", "enabled": True},
            {"config_id": "dca-a", "symbol": "BTC/USDT", "mode": "SPOT_DCA", "enabled": True},
        ]

    def get_leverage(self, config_id=None):
        return 1


def test_equity_compare_uses_config_scoped_sources(tmp_path, monkeypatch):
    db_path = tmp_path / "compare.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE balance_history (
            id INTEGER PRIMARY KEY,
            timestamp TEXT,
            symbol TEXT,
            config_id TEXT,
            total_equity REAL
        );
        CREATE TABLE mock_balance_history (
            id INTEGER PRIMARY KEY,
            timestamp TEXT,
            symbol TEXT,
            config_id TEXT,
            balance REAL,
            total_equity REAL
        );
        CREATE TABLE dca_daily_snapshots (
            id INTEGER PRIMARY KEY,
            snapshot_date TEXT,
            symbol TEXT,
            config_id TEXT,
            total_invested REAL
        );
        """
    )
    conn.execute(
        "INSERT INTO balance_history (timestamp, symbol, config_id, total_equity) VALUES (?, ?, ?, ?)",
        ("2026-07-01 10:00:00", "BTC/USDT", "real-a", 12000),
    )
    conn.execute(
        "INSERT INTO balance_history (timestamp, symbol, config_id, total_equity) VALUES (?, ?, ?, ?)",
        ("2026-07-01 10:00:00", "BTC/USDT", None, 99999),
    )
    conn.execute(
        "INSERT INTO mock_balance_history (timestamp, symbol, config_id, balance, total_equity) VALUES (?, ?, ?, ?, ?)",
        ("2026-07-01 10:00:00", "BTC/USDT", "strategy-a", 10000, 10100),
    )
    conn.execute(
        "INSERT INTO dca_daily_snapshots (snapshot_date, symbol, config_id, total_invested) VALUES (?, ?, ?, ?)",
        ("2026-07-01", "BTC/USDT", "dca-a", 2500),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(stats_service, "DB_NAME", str(db_path))
    monkeypatch.setattr(stats_service, "global_config", StubConfig())

    payload = stats_service.get_equity_compare_payload("BTC/USDT", "real-a,strategy-a,dca-a")
    by_config = {item["config_id"]: item for item in payload["series"]}

    assert set(by_config) == {"real-a", "strategy-a"}
    assert by_config["real-a"]["points"] == [{"date": "2026-07-01", "equity": 12000.0}]
    assert by_config["real-a"]["data_source"]["table"] == "balance_history"
    assert by_config["real-a"]["data_source"]["config_id"] == "real-a"
    assert by_config["strategy-a"]["data_source"]["table"] == "mock_balance_history"


def test_dashboard_data_hides_spot_dca_configs(tmp_path, monkeypatch):
    db_path = tmp_path / "dashboard.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE summaries (
            id INTEGER PRIMARY KEY,
            config_id TEXT,
            agent_name TEXT,
            symbol TEXT,
            content TEXT,
            strategy_logic TEXT,
            timestamp TEXT,
            agent_type TEXT
        )
        """
    )
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

    def fail_dca_stats(config_id, force_sync=False):
        raise AssertionError("SPOT_DCA stats should not be loaded for dashboard cards")

    monkeypatch.setattr(dashboard_service, "global_config", StubConfig())
    monkeypatch.setattr(dashboard_service, "get_db_conn", connect)
    monkeypatch.setattr(dashboard_service, "get_paginated_orders", lambda *args, **kwargs: ([], 0))
    monkeypatch.setattr(dashboard_service, "get_daily_summaries", lambda *args, **kwargs: [])
    monkeypatch.setattr(dashboard_service, "resolve_market_timeframes", lambda config: ["1h"])
    monkeypatch.setattr(dashboard_service, "calculate_dca_stats", fail_dca_stats)

    rows = dashboard_service.get_dashboard_data("BTC/USDT")

    assert [row["config_id"] for row in rows] == ["real-a", "strategy-a"]
    assert all(row["mode"] != "SPOT_DCA" for row in rows)
