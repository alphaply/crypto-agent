"""Explicit, read-only adoption of a verified manual perpetual position.

The adoption baseline is not an exchange order or a synthetic fill. Ownership
checks start with its contracts and only accept subsequent, linked task fills.
"""
from __future__ import annotations

import hashlib
import json
import math
import time

from backend import database
from backend.utils.exit_policy import effective_exit_mode
from backend.utils.independent_exits import IndependentExits, positive
from backend.utils.order_ownership import symbol_aliases
from backend.utils.position_protection import _lock
from backend.utils.trade_operations import current_operation_id


TRADE_LIMIT = 1000
BOUNDARY_LOOKBACK_MS = 60_000
OKX_ALGO_ORDER_TYPES = ('conditional', 'oco', 'chase', 'trigger', 'move_order_stop',
                        'iceberg', 'twap', 'smart_iceberg')


def _number_equal(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-12)


def _number(value, name: str, *, zero: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number, not a boolean")
    try:
        return positive(value, name, zero=zero)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{name} must be a verified finite {'nonnegative' if zero else 'positive'} number") from exc


def _adoption_records(config_id: str, operation_id: str, *, strict: bool = False) -> list[dict]:
    with database.get_db_conn() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        sources = [table for table in ('real_protection_plans', 'real_protection_events') if table in tables]
        if not sources:
            return []
        rows = conn.execute(' UNION ALL '.join(f'SELECT payload FROM {table} WHERE config_id=?' for table in sources),
                            [config_id] * len(sources)).fetchall()
    records = []
    for row in rows:
        try:
            plan = json.loads(row["payload"])
            if not isinstance(plan, dict) or not isinstance(plan.get('adoption', {}), dict):
                raise ValueError('Invalid position adoption history')
            baseline = plan.get("adoption") or {}
        except (ValueError, TypeError):
            if strict:
                raise ValueError('Position adoption history cannot be verified; reconcile before retrying') from None
            continue
        if baseline.get("operation_id") == operation_id:
            records.append(baseline)
    return records


def find_adoption_receipt(config_id: str, operation_id: str, symbol: str | None = None) -> dict | None:
    """Read an exact operation's historical receipt without re-running adoption."""
    from backend.utils.trade_operations import _same_symbol
    for baseline in _adoption_records(config_id, operation_id):
        if symbol is not None and not _same_symbol(symbol, baseline.get("symbol")):
            continue
        result = baseline.get("result")
        if (isinstance(result, dict) and result.get("status") == "completed"
                and result.get('action') == 'adopt_position' and result.get('operation_id') == operation_id):
            return dict(result)
    return None


def _saved_result(service: IndependentExits, operation_id: str, request_hash: str) -> dict | None:
    # Events preserve the receipt after DONE or a later cycle replaces the plan.
    for baseline in _adoption_records(service.config_id, operation_id, strict=True):
        if baseline.get("request_hash") != request_hash or baseline.get("account_scope") != service.account_scope:
            raise ValueError("Adoption operation ID was already used for a different request or account")
        result = baseline.get("result")
        if (not isinstance(result, dict) or result.get("status") != "completed"
                or result.get('action') != 'adopt_position' or result.get('operation_id') != operation_id):
            raise ValueError("Saved adoption receipt is incomplete; reconcile it before retrying")
        return dict(result)
    return None


def _assert_no_local_conflicts(service: IndependentExits, aliases: tuple[str, ...], operation_id: str) -> None:
    placeholders = ",".join("?" for _ in aliases)
    current = current_operation_id.get()
    with database.get_db_conn() as conn:
        for row in conn.execute(
            f"SELECT config_id,payload FROM real_protection_plans WHERE symbol IN ({placeholders})", aliases
        ):
            plan = json.loads(row["payload"])
            if plan.get("state") == "DONE":
                continue
            if (row["config_id"] == service.config_id or not plan.get("account_scope")
                    or plan["account_scope"] == service.account_scope):
                raise ValueError("An active or unresolved position cycle already owns this account and symbol; adoption denied")
        for row in conn.execute(
            f"SELECT config_id,operation_id,status FROM trade_action_runs WHERE symbol IN ({placeholders}) "
            "AND status IN ('running','unknown','pending')", aliases,
        ):
            own_call = (row["status"] == "running" and row["config_id"] == service.config_id
                        and current and row["operation_id"] == current
                        and operation_id == current)
            if not own_call:
                # Old operation receipts lack an account scope. Do not assume an
                # unresolved same-symbol submission belongs to a different account.
                raise ValueError("An unresolved operation on this symbol must be reconciled before adoption")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'mcp_operations' in tables:
            for row in conn.execute("SELECT profile_id,principal,operation_id,state FROM mcp_operations "
                                    "WHERE state IN ('started','running','unknown','pending')"):
                stable_id = 'mcp:' + hashlib.sha256(f"{row['principal']}:{row['operation_id']}".encode()).hexdigest()
                own_call = (row['state'] == 'started' and service.config_id == 'mcp:' + row['profile_id']
                            and current == stable_id and operation_id == stable_id)
                if own_call:
                    continue
                profile_row = conn.execute("SELECT payload FROM mcp_records WHERE kind='profile' AND id=?",
                                           (row['profile_id'],)).fetchone() if 'mcp_records' in tables else None
                profile = json.loads(profile_row[0]) if profile_row else None
                if profile and profile.get('market_type') == 'spot':
                    continue
                if profile and profile.get('symbol_scope', 'selected') != 'all':
                    from backend.utils.trade_operations import _same_symbol
                    if profile.get('symbols') and not any(_same_symbol(value, alias) for value in profile['symbols'] for alias in aliases):
                        continue
                raise ValueError("An unresolved MCP operation may affect this position; reconcile it before adoption")
        if conn.execute(
            f"SELECT 1 FROM execution_position_history WHERE account_scope=? AND symbol IN ({placeholders}) "
            "AND config_id!=? AND closed_at_ms IS NULL AND amount>0 LIMIT 1",
            (service.account_scope, *aliases, service.config_id),
        ).fetchone():
            raise ValueError("Another task has an unclosed position history for this account and symbol; adoption denied")


def _position_snapshot(exchange, symbol: str, side: str, expected_amount: float, contract_size: float) -> dict:
    rows = exchange.fetch_positions([symbol])
    if not isinstance(rows, list):
        raise ValueError("Position snapshot is unavailable; adoption denied")
    selected = []
    for row in rows:
        if not isinstance(row, dict) or row.get("contracts") is None:
            raise ValueError("Position quantity cannot be verified; adoption denied")
        contracts = _number(row["contracts"], "position contracts", zero=True)
        if row.get("symbol") and row["symbol"] != symbol:
            raise ValueError("Position snapshot contains a different symbol; adoption denied")
        if not contracts:
            continue
        if str(row.get("side", "")).upper() != side:
            raise ValueError("A position in another or unknown direction exists; adopt only a single verified direction")
        selected.append(row)
    if len(selected) != 1:
        raise ValueError("Adoption requires exactly one existing position in the requested direction")
    position = selected[0]
    contracts = _number(position["contracts"], "position contracts")
    amount = _number(contracts * contract_size, "position amount")
    if not _number_equal(amount, expected_amount):
        raise ValueError(f"Position amount changed or does not match: expected {expected_amount:g}, observed {amount:g}; adoption denied")
    entry_price = _number(position.get("entryPrice"), "position entry price")
    hedged = position.get("hedged")
    if not isinstance(hedged, bool):
        mode = exchange.fetch_position_mode(symbol)
        hedged = mode.get("hedged") if isinstance(mode, dict) else None
    if not isinstance(hedged, bool):
        raise ValueError("Position mode cannot be verified; adoption denied")
    return {"contracts": contracts, "amount": amount, "entry_price": entry_price, "hedged": hedged}


def _assert_no_open_orders(exchange, symbol: str) -> None:
    # OKX trigger=True defaults to ordType=trigger and does not include its
    # conditional/OCO/trailing/advanced algo books. Query each documented type.
    params_list = [{}, *({"trigger": True, "ordType": kind} for kind in OKX_ALGO_ORDER_TYPES)] if exchange.id == 'okx' else [{}, {"trigger": True}]
    for params in params_list:
        orders = exchange.fetch_open_orders(symbol, params=params)
        if not isinstance(orders, list):
            raise ValueError("Existing orders cannot be verified; adoption denied")
        if orders:
            raise ValueError("Existing regular or trigger orders must be reviewed before adoption; no orders were cancelled or changed")


def _trade_boundary(exchange, symbol: str, since_ms: int) -> dict[str, dict]:
    trades = exchange.fetch_my_trades(symbol, since=since_ms, limit=TRADE_LIMIT)
    if not isinstance(trades, list) or len(trades) >= TRADE_LIMIT:
        raise ValueError("Adoption fill boundary is incomplete or exceeds one verifiable page")
    observed = {}
    for trade in trades:
        if not isinstance(trade, dict) or trade.get("id") is None or not str(trade["id"]).strip():
            raise ValueError("Adoption fill boundary lacks a stable trade ID")
        timestamp = _number(trade.get("timestamp"), "trade timestamp")
        if not timestamp.is_integer() or timestamp < since_ms:
            raise ValueError("Adoption fill boundary has an invalid timestamp")
        if trade.get("symbol") and trade["symbol"] != symbol:
            raise ValueError("Adoption fill boundary contains a different symbol")
        side = str(trade.get("side", "")).lower()
        if side not in {"buy", "sell"}:
            raise ValueError("Adoption fill direction cannot be verified")
        value = {"timestamp": int(timestamp), "order": str(trade["order"]) if trade.get("order") is not None else None,
                 "side": side, "amount": _number(trade.get("amount"), "trade amount"),
                 "price": _number(trade["price"], "trade price") if trade.get("price") is not None else None}
        trade_id = str(trade["id"])
        if trade_id in observed and observed[trade_id] != value:
            raise ValueError("Conflicting fills share a trade ID; adoption denied")
        observed[trade_id] = value
    return observed


def _assert_unclaimed_boundary(service: IndependentExits, aliases: tuple[str, ...], observed: dict[str, dict]) -> None:
    placeholders = ",".join("?" for _ in aliases)
    with database.get_db_conn() as conn:
        links = {str(row["order_id"]): row["config_id"] for row in conn.execute(
            f"SELECT order_id,config_id FROM execution_order_links WHERE account_scope=? AND symbol IN ({placeholders})",
            (service.account_scope, *aliases),
        )}
        if any(value["order"] in links and links[value["order"]] != service.config_id for value in observed.values()):
            raise ValueError("Another task owns fills at the adoption boundary; adoption denied")


def adopt_position(market_tool, symbol: str, pos_side: str, expected_amount: float,
                   reason: str, operation_id: str) -> dict:
    """Record a manual position baseline without submitting or modifying orders.

    ``expected_amount`` is the entire position in base-asset units, not contracts.
    Identical operation IDs replay the original durable receipt, including after
    the adopted cycle has ended. A changed request must use an explicit new ID.
    """
    config = getattr(market_tool, "runtime_config", None)
    if not isinstance(config, dict) or str(config.get("mode", "")).upper() != "REAL":
        raise ValueError("Position adoption requires a REAL trading task")
    if (getattr(market_tool, "market_type", None) == "spot" or config.get("market_type") == "spot"
            or effective_exit_mode(config) != "independent_exits"):
        raise ValueError("Position adoption requires a perpetual task using independent_exits")
    if not getattr(market_tool, "config_id", None):
        raise ValueError("Position adoption requires a task configuration")
    if pos_side not in {"LONG", "SHORT"}:
        raise ValueError("Position side must be LONG or SHORT")
    if isinstance(expected_amount, bool) or not isinstance(expected_amount, (int, float)):
        raise ValueError("Expected position amount must be a number, not a string or boolean")
    expected_amount = _number(expected_amount, "expected position amount")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("An explicit adoption reason is required")
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise ValueError("An explicit adoption operation ID is required")
    service = IndependentExits(market_tool)
    with _lock(service.account_scope):
        market = service._market(symbol)
        canonical = market["symbol"]
        contract_size = _number(market.get("contractSize"), "contract size")
        aliases = symbol_aliases(market_tool, canonical)
        request = {"symbol": canonical, "side": pos_side, "expected_amount": expected_amount,
                   "reason": reason, "account_scope": service.account_scope}
        request_hash = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        result = _saved_result(service, operation_id, request_hash)
        if result is not None:
            return result
        _assert_no_local_conflicts(service, aliases, operation_id)

        server_time = _number(service.ex.fetch_time(), "exchange server time")
        if not server_time.is_integer():
            raise ValueError("Exchange server time is invalid; adoption denied")
        since_ms = max(0, int(server_time) - BOUNDARY_LOOKBACK_MS)
        position = _position_snapshot(service.ex, canonical, pos_side, expected_amount, contract_size)
        _assert_no_open_orders(service.ex, canonical)
        observed = _trade_boundary(service.ex, canonical, since_ms)
        confirmed_position = _position_snapshot(service.ex, canonical, pos_side, expected_amount, contract_size)
        confirmed_trades = _trade_boundary(service.ex, canonical, since_ms)
        if position != confirmed_position or observed != confirmed_trades:
            raise ValueError("Position or fills changed during verification; adoption denied, verify again before a new request")
        _assert_no_open_orders(service.ex, canonical)
        _assert_unclaimed_boundary(service, aliases, observed)
        _assert_no_local_conflicts(service, aliases, operation_id)

        adopted_at = time.time()
        plan = service._new(canonical, pos_side)
        result = {"status": "completed", "action": "adopt_position", "operation_id": operation_id,
                  "symbol": canonical, "pos_side": pos_side, "amount": position["amount"],
                  "entry_price": position["entry_price"], "episode_id": plan["episode_id"],
                  "protection_state": "ACTIVE", "adopted_at": adopted_at,
                  "message": "Manual position adopted for this task; no exchange orders were placed or changed"}
        plan.update(state="ACTIVE", ever_filled=True, verified_at=adopted_at,
                    adoption={"version": 1, "operation_id": operation_id, "request_hash": request_hash,
                              "reason": reason, "account_scope": service.account_scope,
                              "symbol": canonical, "side": pos_side, "contracts": position["contracts"],
                              "contract_size": contract_size, "amount": position["amount"],
                              "entry_price": position["entry_price"], "hedged": position["hedged"],
                              "adopted_at": adopted_at, "since_ms": since_ms,
                              "observed_trade_ids": sorted(observed), "result": result})
        service._save(plan)
        return dict(result)
