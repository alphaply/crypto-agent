"""Batch the per-task details needed by dashboard and public workspace views."""
from __future__ import annotations

from backend import storage


def load_recent_orders(connection, config_ids: list[str], limit: int = 10) -> dict:
    """Keep each task's newest orders and full count without borrowing more connections."""
    result = {config_id: {"orders": [], "total": 0} for config_id in config_ids}
    if not result:
        return result

    # LATERAL limits rows per task before returning them; a global LIMIT would
    # let a busy task consume the entire page for every other task. Counting
    # in the same statement keeps totals and rows on one snapshot while new
    # orders are being committed.
    for row in connection.execute(
        "SELECT recent.*, selected.order_total AS dashboard_order_total "
        "FROM (SELECT config_id, COUNT(*) AS order_total FROM orders "
        "WHERE config_id = ANY(%s) GROUP BY config_id) selected "
        "CROSS JOIN LATERAL (SELECT * FROM orders "
        "WHERE config_id = selected.config_id ORDER BY id DESC LIMIT %s) recent "
        "ORDER BY recent.config_id, recent.id DESC",
        (config_ids, limit),
    ):
        order = storage.record(row)
        result[row["config_id"]]["total"] = order.pop("dashboard_order_total")
        result[row["config_id"]]["orders"].append(order)
    return result


def load_memory_updates(connection, latest_summaries: dict) -> dict:
    """Read only the exact config/run pairs represented by the latest summaries."""
    pairs = [(config_id, row["run_id"]) for config_id, row in latest_summaries.items()
             if row.get("run_id")]
    if not pairs:
        return {}
    placeholders = ",".join(["(%s,%s)"] * len(pairs))
    rows = connection.execute(
        "SELECT config_id, run_id, status, attempts, error FROM memory_update_jobs "
        f"WHERE (config_id, run_id) IN ({placeholders})",
        tuple(value for pair in pairs for value in pair),
    )
    return {
        (row["config_id"], row["run_id"]): {
            key: row[key] for key in ("status", "attempts", "error")
        }
        for row in rows
    }
