"""Focused PostgreSQL regressions for attribution, bounded reads and transactions."""
from contextlib import contextmanager
import json
import math

import pytest

from backend import database, storage
from backend.database.statistics import TradingStatisticsStore


def _trade(conn, identity, config, pnl, symbol="BTC/USDT"):
    conn.execute("""INSERT INTO trade_history(trade_id, config_id, symbol, realized_pnl)
        VALUES(%s, %s, %s, %s)""", (identity, config, symbol, pnl))


def _mock(conn, identity, config, pnl, *, status="CLOSED", symbol="BTC/USDT"):
    conn.execute("""INSERT INTO mock_orders(order_id, config_id, symbol, status, realized_pnl)
        VALUES(%s, %s, %s, %s, %s)""", (identity, config, symbol, status, pnl))


def test_batch_preserves_source_precedence_scope_unknown_and_flat_pnl(pg, monkeypatch):
    modes = {"cycle": "REAL", "position": "REAL", "fill": "REAL",
             "mock": "STRATEGY", "spot": "SPOT_DCA", "legacy": ""}
    monkeypatch.setattr("backend.config.config.get_config_by_id", lambda key: {"mode": modes.get(key, "")})
    with database.get_db_conn() as conn:
        for index, pnl in enumerate([None, 0]):
            conn.execute("""INSERT INTO execution_position_history
                (position_id, account_scope, config_id, symbol, realized_pnl, payload)
                VALUES(%s, 'account', 'cycle', 'BTC/USDT:USDT', %s, '{}')""", (str(index), pnl))
        for owner, status, pnl in [("cycle", "CLOSED", 999), ("position", "CLOSED", 3.5),
                                   ("position", "OPEN", 999)]:
            conn.execute("""INSERT INTO position_history
                (config_id, position_key, symbol, status, realized_pnl)
                VALUES(%s, %s, 'BTC/USDT:USDT', %s, %s)""", (owner, owner + status, status, pnl))
        for owner, pnl in [("cycle", 999), ("position", 999), ("fill", -2),
                           ("mock", 999), ("spot", 0), ("spot", 4), ("legacy", -1), ("outside", 999)]:
            _trade(conn, owner + str(pnl), owner, pnl)
        for owner, pnl in [("mock", 3), ("mock", None), ("spot", 999), ("legacy", 5)]:
            _mock(conn, owner + str(pnl), owner, pnl)
        _mock(conn, "still-open", "mock", 999, status="OPEN")
        _trade(conn, "other-symbol", "spot", 999, "ETH/USDT")
        conn.commit()

    result = database.get_history_pnl_stats_for_configs("BTC/USDT", [*modes, "spot", "", None])
    assert result == {"total_trades": 10, "total_pnl": 12.5, "win_count": 4,
                      "lose_count": 2, "decided_trades": 6, "win_rate": 4 / 6 * 100}
    # A preferred source with unknown or flat PnL must not fall through to fills.
    assert database.get_history_pnl_stats("BTC/USDT", "cycle")["win_rate"] is None
    assert database.get_history_pnl_stats_for_configs("BTC/USDT", [])["win_rate"] == 0


def test_batch_query_count_does_not_grow_with_number_of_tasks(pg):
    queries = []

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, query, args=None):
            queries.append(query)
            return self.conn.execute(query, args)

    @contextmanager
    def connect():
        with database.get_db_conn() as conn:
            yield Connection(conn)

    store = TradingStatisticsStore(connect)
    store.history_for_configs("BTC/USDT", [("one", "REAL")])
    small_count = len(queries)
    queries.clear()
    store.history_for_configs("BTC/USDT", [(str(index), "REAL") for index in range(100)])
    assert len(queries) == small_count
    assert small_count <= 2


def test_all_without_current_configs_preserves_prefix_and_unowned_rows(pg, monkeypatch):
    monkeypatch.setattr("backend.config.config.get_configs_by_symbol", lambda _: [])
    with database.get_db_conn() as conn:
        _trade(conn, "external", None, 4, "BTC/USDT:USDT")
        _trade(conn, "historical", "removed", -1)
        _trade(conn, "other", None, 100, "ETH/USDT")
        _mock(conn, "mock", "removed", 0)
        _mock(conn, "not-exact", "removed", 100, symbol="BTC/USDT:USDT")
        conn.commit()
    result = database.get_history_pnl_stats("BTC/USDT")
    assert result["total_trades"] == 3
    assert result["total_pnl"] == 3
    assert result["win_rate"] == 50


def test_history_nan_is_not_a_win_and_missing_pnl_remains_counted(pg):
    with database.get_db_conn() as conn:
        _trade(conn, "nan", "task", float("nan"))
        _trade(conn, "unknown", "task", None)
        conn.commit()
    result = database._statistics_store.history_for_configs("BTC/USDT", [("task", "SPOT_DCA")])
    assert result["total_trades"] == 2
    assert math.isnan(result["total_pnl"])
    assert result["win_count"] == result["lose_count"] == 0
    assert result["win_rate"] is None


def test_financial_page_keeps_all_fill_denominator_and_balance_window(pg):
    from backend.app.services.stats_service import get_financial_stats_payload

    with database.get_db_conn() as conn:
        for index, pnl in enumerate([6, -2, 0]):
            _trade(conn, str(index), "task", pnl)
        _trade(conn, "not-exact", "task", 999, "BTC/USDT:USDT")
        conn.cursor().executemany("""INSERT INTO balance_history
            (symbol, timestamp, total_balance, total_equity) VALUES('BTC/USDT', %s, %s, %s)""",
            [(f"2026-01-01 00:{index // 60:02}:{index % 60:02}", index, index + 1) for index in range(201)])
        conn.commit()
    result = get_financial_stats_payload("BTC/USDT")
    assert len(result["balance_history"]) == 200
    assert result["daily_equity"] == [{"day": "2026-01-01", "total_equity": 201}]
    assert result["summary"] == {"total_trades": 3, "total_pnl": 4, "win_rate": 33.33,
                                 "win_count": 1, "lose_count": 1, "latest_equity": 200, "latest_balance": 199}
    with database.get_db_conn() as conn:
        _trade(conn, "missing", "task", None)
        conn.commit()
    with pytest.raises(TypeError):
        get_financial_stats_payload("BTC/USDT")


def test_spot_fill_observation_joins_transaction_and_rolls_back_together(pg):
    with database.get_db_conn() as conn:
        conn.execute("""INSERT INTO orders(order_id, config_id, symbol, status)
            VALUES('order', 'task', 'BTC/USDT', 'OPEN')""")
        conn.commit()
    with pytest.raises(RuntimeError):
        with database.get_db_conn() as conn:
            conn.execute("UPDATE orders SET status='FILLED' WHERE order_id='order'")
            database.upsert_spot_order_fill("order", "task", "BTC/USDT", "FILLED", 1, 10, 10, connection=conn)
            raise RuntimeError("fail after both observation writes")
    with database.get_db_conn() as conn:
        assert conn.execute("SELECT status FROM orders WHERE order_id='order'").fetchone()[0] == "OPEN"
        assert conn.execute("SELECT count(*) FROM spot_order_fills").fetchone()[0] == 0
    database.upsert_spot_order_fill("order", "task", "BTC/USDT", "FILLED", 1, 10, 10)
    with database.get_db_conn() as conn:
        assert conn.execute("SELECT filled_qty FROM spot_order_fills").fetchone()[0] == 1


def test_schema_check_keeps_retired_plans_and_does_not_commit_caller_work(pg, monkeypatch):
    from backend.database.schema import initialize_schema

    # This fixture isolates business tables in its own schema, not public.
    monkeypatch.setattr("backend.storage.migrate.check_schema", lambda conn: conn.execute("SELECT 1"))
    with database.get_db_conn() as conn:
        conn.execute("""INSERT INTO real_protection_plans(config_id, symbol, side, payload)
            VALUES('retired-task', 'BTC/USDT', 'LONG', '{"operation_id":"unresolved"}')""")
        conn.execute("""INSERT INTO agent_configs(config_id, symbol, mode, data_json, updated_at)
            VALUES('active-task', 'BTC/USDT', 'REAL', '{}', '2026-01-01')""")
        conn.commit()
        _trade(conn, "uncommitted", "task", 5)
        initialize_schema(conn)
        assert conn.execute("SELECT count(*) FROM real_protection_plans").fetchone()[0] == 1
    with database.get_db_conn() as conn:
        assert conn.execute("SELECT count(*) FROM trade_history").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM real_protection_plans").fetchone()[0] == 1


def test_retention_keeps_queued_running_and_linked_scheduler_records(pg, monkeypatch):
    from backend.app.services import database_service

    monkeypatch.setattr(database_service, "_backup", lambda: "isolated-test.dump")
    with database.get_db_conn() as conn:
        for index, status in enumerate(["QUEUED", "RUNNING", "SUCCESS", "FAILED"]):
            row = conn.execute("""INSERT INTO scheduler_runs
                (config_id, job_type, scheduled_at, status, created_at, updated_at)
                VALUES(%s, 'hourly', '2020-01-01', %s, '2020-01-01', '2020-01-01') RETURNING id""",
                (f"task-{index}", status)).fetchone()
            if index == 2:
                conn.execute("""INSERT INTO agent_run_queue
                    (config_id, trigger_key, trigger_type, run_id, scheduled_at, scope_hash, status, scheduler_run_id)
                    VALUES('task-2', 'old-trigger', 'hourly', 'old-run', '2020-01-01', 'scope', 'CLAIMED', %s)""", (row[0],))
        conn.commit()
    preview = database_service.preview_cleanup(["scheduler_runs"], "2021-01-01")
    assert preview["counts"] == {"scheduler_runs": 1}
    result = database_service.clean_database(["scheduler_runs"], "2021-01-01", preview["preview_token"])
    assert result["deleted"] == {"scheduler_runs": 1}
    with database.get_db_conn() as conn:
        assert {row[0] for row in conn.execute("SELECT config_id FROM scheduler_runs")} == {"task-0", "task-1", "task-2"}
        assert conn.execute("SELECT scheduler_run_id FROM agent_run_queue").fetchone()[0] is not None


@pytest.mark.parametrize("queue_status, operation_status, receipt", [
    ("RUNNING", "completed", {}), ("CLAIMED", "completed", {}), (None, "unknown", {}),
    (None, "submitted", {}), (None, "completed", {"results": [{"result": {"status": "unknown"}}]}),
    (None, "completed", {"query": {"status": "partial"}}),
    (None, "completed", {"cleanup": {"status": "pending"}}),
])
def test_task_deletion_keeps_original_unfinished_evidence_but_deletes_completed_history(pg, queue_status, operation_status, receipt):
    with database.get_db_conn() as conn:
        if queue_status:
            conn.execute("""INSERT INTO agent_run_queue
                (config_id, trigger_key, trigger_type, run_id, scheduled_at, scope_hash, status, enqueued_at)
                VALUES('task', 'trigger', 'hourly', 'run', '2020-01-01', 'scope', %s, '2020-01-01')""", (queue_status,))
        for identity, status, updated in [("current", operation_status, 1700000000), ("old", "completed", 1)]:
            conn.execute("""INSERT INTO trade_action_runs
                (config_id, operation_id, symbol, request_hash, status, result, created_at, updated_at)
                VALUES('task', %s, 'BTC/USDT', 'hash', %s, %s, %s, %s)""",
                (identity, status, json.dumps({"order_id": identity, **(receipt if identity == "current" else {})}), updated, updated))
            conn.execute("""INSERT INTO trade_operation_requests(config_id, operation_id, request_json)
                VALUES('task', %s, %s)""", (identity, json.dumps({"order_id": identity})))
            conn.execute("""INSERT INTO orders(config_id, order_id, symbol, status)
                VALUES('task', %s, 'BTC/USDT', 'CLOSED')""", (identity,))
            conn.execute("""INSERT INTO execution_order_links
                (account_scope, symbol, order_id, config_id, role, payload)
                VALUES('account', 'BTC/USDT', %s, 'task', 'entry', %s)""",
                (identity, json.dumps({"operation_id": identity, "episode_id": identity})))
            conn.execute("""INSERT INTO execution_fills
                (account_scope, symbol, trade_id, order_id, config_id, timestamp_ms, payload)
                VALUES('account', 'BTC/USDT', %s, %s, 'task', 1, '{}')""", (identity, identity))
            _trade(conn, identity, "task", 1)
            conn.execute("UPDATE trade_history SET order_id=%s WHERE trade_id=%s", (identity, identity))
        conn.execute("""INSERT INTO summaries(config_id, content) VALUES('task', 'finished display history')""")
        conn.commit()

    result = database.purge_config_all_data("task")
    assert result["trade_action_runs_deleted"] == 1
    assert result["trade_action_runs_retained_for_recovery"] == 1
    assert result["summaries_deleted"] == 1
    with database.get_db_conn() as conn:
        assert [row[0] for row in conn.execute("SELECT operation_id FROM trade_action_runs")] == ["current"]
        assert [row[0] for row in conn.execute("SELECT operation_id FROM trade_operation_requests")] == ["current"]
        assert [row[0] for row in conn.execute("SELECT order_id FROM orders")] == ["current"]
        assert [row[0] for row in conn.execute("SELECT trade_id FROM execution_fills")] == ["current"]
        assert [row[0] for row in conn.execute("SELECT trade_id FROM trade_history")] == ["current"]
        if queue_status:
            assert conn.execute("SELECT count(*) FROM agent_run_queue").fetchone()[0] == 1
            conn.execute("UPDATE agent_run_queue SET status='FINISHED'")
        conn.execute("UPDATE trade_action_runs SET status='completed', result='{\"status\":\"completed\"}'")
        conn.commit()
    # Once resolved, a subsequent explicit cleanup uses the existing deletion
    # behavior. Retention is based on current facts, not a permanent keep flag.
    settled = database.purge_config_all_data("task")
    assert settled["trade_action_runs_deleted"] == 1
    assert settled["execution_fills_deleted"] == 1


def test_task_cleanup_does_not_delete_receipts_changed_after_inspection(pg, monkeypatch):
    from backend.database import cleanup as database_cleanup

    with database.get_db_conn() as conn:
        for identity, status in [("pending", "unknown"), ("changed", "completed")]:
            conn.execute("""INSERT INTO trade_action_runs
                (config_id, operation_id, symbol, request_hash, status, created_at, updated_at)
                VALUES('task', %s, 'BTC/USDT', 'hash', %s, 1, 1)""", (identity, status))
        conn.commit()
    inspect = database_cleanup._recovery_retained_rows

    def new_acknowledgement(conn, config_id, tables, **kwargs):
        result = inspect(conn, config_id, tables, **kwargs)
        with database.get_db_conn() as writer:
            writer.execute("UPDATE trade_action_runs SET status='unknown' WHERE operation_id='changed'")
            writer.execute("""INSERT INTO trade_action_runs
                (config_id, operation_id, symbol, request_hash, status, created_at, updated_at)
                VALUES('task', 'new-ack', 'BTC/USDT', 'hash', 'unknown', 2, 2)""")
            writer.execute("INSERT INTO orders(config_id, order_id, symbol) VALUES('task', 'new-order', 'BTC/USDT')")
            writer.commit()
        return result

    monkeypatch.setattr(database_cleanup, "_recovery_retained_rows", new_acknowledgement)
    database.purge_config_all_data("task")
    with database.get_db_conn() as conn:
        assert {row[0] for row in conn.execute("SELECT operation_id FROM trade_action_runs")} == {"pending", "changed", "new-ack"}
        assert conn.execute("SELECT order_id FROM orders").fetchone()[0] == "new-order"
