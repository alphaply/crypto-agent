import pytest
from backend import storage

from backend.app.services import stats_service


class StubConfig:
    def get_all_symbol_configs(self):
        return [
            {"config_id": "strategy-a", "symbol": "BTC/USDT", "mode": "STRATEGY", "enabled": True},
            {"config_id": "strategy-empty", "symbol": "BTC/USDT", "mode": "STRATEGY", "enabled": True},
        ]


def test_equity_compare_keeps_empty_series_and_scopes_mock_rows_to_symbol(tmp_path, monkeypatch):
    db_path = tmp_path / "equity.db"
    conn = storage.connect()
    None  # Production schema is installed by pg.
    conn.execute(
        "INSERT INTO mock_balance_history (timestamp, symbol, config_id, balance, total_equity) VALUES (%s, %s, %s, %s, %s)",
        ("2026-07-01 10:00:00", "ETH/USDT", "strategy-a", 10000, 10100),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(stats_service, "global_config", StubConfig())

    payload = stats_service.get_equity_compare_payload("BTC/USDT", "strategy-a,strategy-empty")
    by_id = {item["config_id"]: item for item in payload["series"]}

    assert set(by_id) == {"strategy-a", "strategy-empty"}
    assert by_id["strategy-a"]["points"] == []
    assert by_id["strategy-a"]["data_state"] == "no_data"
    assert by_id["strategy-empty"]["point_count"] == 0
    assert by_id["strategy-empty"]["data_source"]["display_label"] == "策略模拟权益快照"


pytestmark = pytest.mark.usefixtures("pg")
