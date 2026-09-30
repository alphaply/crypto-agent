"""Exit policy defaults and configuration transition checks."""
from __future__ import annotations

import json

EXIT_MODES = {"attached_required", "attached_optional", "independent_exits"}


class ExitModeConflict(ValueError):
    """An exit mode cannot change while the previous lifecycle is active."""


def effective_exit_mode(config: dict | None) -> str:
    config = config or {}
    value = config.get("exit_mode")
    if value is not None:
        if value not in EXIT_MODES:
            raise ValueError(f"Unknown exit_mode: {value}")
        return value
    return "attached_optional" if config.get("mode", "STRATEGY").upper() == "REAL" else "attached_required"


def validate_entry_protection(config: dict, order) -> None:
    mode = effective_exit_mode(config)
    sl, tp = order.stop_loss, order.take_profit
    if mode == "independent_exits" and (sl is not None or tp is not None):
        raise ValueError("独立退出模式开仓不接受附带 TP/SL；成交后通过 close 设置独立退出单")
    if mode == "attached_required" and (sl is None or tp is None):
        raise ValueError("当前设置要求开仓同时提供 stop_loss 和 take_profit")


def assert_exit_mode_change_allowed(previous: dict, updated: dict) -> None:
    if effective_exit_mode(previous) == effective_exit_mode(updated):
        return
    from backend import database

    cid = previous["config_id"]
    from backend.utils.trade_operations import reconcile_pending_trade_operations
    reconcile_pending_trade_operations(cid)
    blockers = []
    with database.get_db_conn() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "real_protection_plans" in tables:
            for row in conn.execute("SELECT payload FROM real_protection_plans WHERE config_id=?", (cid,)):
                try:
                    plan = json.loads(row[0])
                    if not isinstance(plan, dict):
                        raise ValueError('Plan is not an object')
                except (ValueError, TypeError):
                    raise ExitModeConflict('保护计划无法解析，需先核验已有持仓和挂单')
                if plan.get("state") != "DONE":
                    blockers.append(f"{plan.get('side')}: {plan.get('state', 'pending')}")
        if "mock_orders" in tables and str(previous.get("mode")).upper() == "STRATEGY":
            blockers.extend(str(r[0]) for r in conn.execute(
                "SELECT order_id FROM mock_orders WHERE config_id=? AND status='OPEN'", (cid,)))
        if str(previous.get('mode')).upper() == 'STRATEGY':
            if 'mock_positions' in tables:
                blockers.extend(str(r[0]) for r in conn.execute(
                    'SELECT episode_id FROM mock_positions WHERE config_id=? AND quantity>0', (cid,)))
            if 'mock_exit_orders' in tables:
                blockers.extend(str(r[0]) for r in conn.execute(
                    "SELECT order_id FROM mock_exit_orders WHERE config_id=? AND status IN ('OPEN','PENDING','UNKNOWN')", (cid,)))
        if "trade_action_runs" in tables:
            blockers.extend(str(r[0]) for r in conn.execute(
                "SELECT operation_id FROM trade_action_runs WHERE config_id=? AND status IN ('running','unknown','pending')", (cid,)))
    if blockers:
        raise ExitModeConflict("退出模式切换需先结束持仓及挂单/待核验操作：" + ", ".join(blockers[:10]))
    if str(previous.get("mode")).upper() == "REAL":
        from backend.utils.market_data import MarketTool
        try:
            mt = MarketTool(config_id=cid)
            positions = mt.exchange.fetch_positions([previous["symbol"]])
            orders = mt.exchange.fetch_open_orders(previous["symbol"])
            orders += mt.exchange.fetch_open_orders(previous["symbol"], params={"trigger": True})
            if any(float(p.get("contracts") or 0) for p in positions) or orders:
                raise ExitModeConflict("账户仍有持仓或挂单，暂不能切换退出模式")
        except ExitModeConflict:
            raise
        except Exception as exc:
            raise ExitModeConflict(f"无法确认账户已清空，未切换退出模式：{exc}") from exc
