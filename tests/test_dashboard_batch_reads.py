from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.app.services import dashboard_read_model, dashboard_service
from backend.database import get_paginated_orders


def test_empty_dashboard_details_do_not_query_database():
    connection = Mock()
    assert dashboard_read_model.load_recent_orders(connection, []) == {}
    assert dashboard_read_model.load_memory_updates(connection, {}) == {}
    assert dashboard_read_model.load_memory_updates(connection, {"task": {"run_id": None}}) == {}
    connection.execute.assert_not_called()


def test_batched_orders_preserve_each_tasks_page_and_total(pg):
    with pg() as conn:
        for config_id, count in (("first", 17), ("second", 12), ("not-visible", 30)):
            for index in range(count):
                conn.execute(
                    "INSERT INTO orders (config_id, order_id, symbol, side) VALUES (%s,%s,%s,%s)",
                    (config_id, f"{config_id}-{index}", "BTC/USDT", "BUY"),
                )
        conn.commit()
        result = dashboard_read_model.load_recent_orders(conn, ["first", "second", "empty"])

    assert set(result) == {"first", "second", "empty"}
    for config_id in result:
        orders, total = get_paginated_orders(config_id, page=1, per_page=10)
        assert result[config_id] == {"orders": orders, "total": total}
    assert len(result["first"]["orders"]) == len(result["second"]["orders"]) == 10
    assert result["empty"] == {"orders": [], "total": 0}


def test_memory_updates_keep_exact_config_run_pairs(pg):
    with pg() as conn:
        for config_id, run_id, status in (
            ("first", "run-a", "completed"),
            ("first", "run-b", "failed"),
            ("second", "run-b", "pending"),
            ("second", "run-a", "failed"),
        ):
            conn.execute(
                "INSERT INTO memory_update_jobs (config_id, run_id, summary_id, status, attempts, error) "
                "VALUES (%s,%s,1,%s,2,'detail')",
                (config_id, run_id, status),
            )
        result = dashboard_read_model.load_memory_updates(conn, {
            "first": {"run_id": "run-a"}, "second": {"run_id": "run-b"},
            "missing": {"run_id": "run-c"}, "legacy": {"run_id": None},
        })
    assert result == {
        ("first", "run-a"): {"status": "completed", "attempts": 2, "error": "detail"},
        ("second", "run-b"): {"status": "pending", "attempts": 2, "error": "detail"},
    }


def test_orders_and_total_share_a_snapshot_while_new_orders_arrive(pg):
    with pg() as conn:
        class InsertAfterRead:
            inserted = False

            def execute(self, query, params=()):
                result = conn.execute(query, params)
                if not self.inserted:
                    self.inserted = True
                    # Commit after the first read has obtained its snapshot.
                    # Separate COUNT/list statements would return total=0
                    # with one order after this concurrent insertion.
                    with pg() as writer:
                        writer.execute("INSERT INTO orders (config_id,order_id) VALUES ('task','arrived')")
                        writer.commit()
                return result

        result = dashboard_read_model.load_recent_orders(InsertAfterRead(), ["task"])
        assert result["task"] == {"orders": [], "total": 0}
        current = dashboard_read_model.load_recent_orders(conn, ["task"])
        assert current["task"]["total"] == len(current["task"]["orders"]) == 1


@pytest.mark.parametrize("task_count,mode", [(1, "STRATEGY"), (25, "STRATEGY"), (1, "SPOT_DCA")])
def test_dashboard_query_count_does_not_grow_per_task(pg, monkeypatch, task_count, mode):
    configs = [{"config_id": f"task-{i}", "symbol": "BTC/USDT", "mode": mode}
               for i in range(task_count)]
    with pg() as conn:
        for cfg in configs:
            conn.execute("INSERT INTO summaries (config_id, symbol, run_id) VALUES (%s,%s,%s)",
                         (cfg["config_id"], cfg["symbol"], "latest-run"))
        conn.commit()

    statements = []
    connections = []

    class CountedConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, query, params=()):
            statements.append(query)
            return self.connection.execute(query, params)

    @contextmanager
    def connection():
        connections.append(True)
        with pg() as conn:
            yield CountedConnection(conn)

    monkeypatch.setattr(dashboard_service, "get_db_conn", connection)
    monkeypatch.setattr(dashboard_service, "global_config", SimpleNamespace(
        get_all_symbol_configs=lambda: configs, get_leverage=lambda _: 1))
    monkeypatch.setattr(dashboard_service, "get_scheduler_status", lambda: True)
    monkeypatch.setattr(dashboard_service, "get_paginated_orders", Mock(
        side_effect=AssertionError("Dashboard must not borrow a connection per task")))
    if mode == "SPOT_DCA":
        import backend.database as database
        from backend.app.core import scheduler
        # Guarantee a due slot independently of the wall clock.
        monkeypatch.setattr(scheduler, "latest_dca_slot", lambda cfg, now: now)
        monkeypatch.setattr(database, "get_db_conn", Mock(
            side_effect=AssertionError("DCA status must reuse the dashboard connection")))

    result = dashboard_service.get_dashboard_data("BTC/USDT")

    assert [row["config_id"] for row in result] == [cfg["config_id"] for cfg in configs]
    assert all(row["all_orders"] == [] and row["order_total"] == 0 for row in result)
    assert all(row["memory_update"] is None for row in result)
    assert len(connections) == 1
    assert len(statements) == 7 + (task_count if mode == "SPOT_DCA" else 0)


def test_recent_task_lookups_use_filter_and_order_indexes(pg):
    def indexes(plan):
        result = {plan["Index Name"]} if plan.get("Index Name") else set()
        for child in plan.get("Plans", []):
            result.update(indexes(child))
        return result

    with pg() as conn:
        for table in ("orders", "summaries"):
            # The requested task's rows are oldest, so scanning the primary key
            # backwards would visit thousands of unrelated rows first.
            conn.execute(
                f"INSERT INTO {table} (config_id) "
                "SELECT CASE WHEN n <= 20 THEN 'requested' ELSE 'unrelated' END "
                "FROM generate_series(1,3000) n")
            conn.execute(f"ANALYZE {table}")
            plan = conn.execute(
                f"EXPLAIN (ANALYZE, FORMAT JSON) SELECT * FROM {table} "
                "WHERE config_id = %s ORDER BY id DESC LIMIT 10",
                ("requested",),
            ).fetchone()[0][0]["Plan"]
            assert f"idx_{table}_config_id_desc" in indexes(plan)
            assert plan["Actual Rows"] == 10
