"""Compatibility adoption receipts backed by ordinary shared management."""
from __future__ import annotations

import hashlib
import json
import math
import time

from backend import database
from backend.utils.exit_policy import effective_exit_mode
from backend.utils.independent_exits import IndependentExits, positive
from backend.utils.position_protection import _lock


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
        tables = {row[0] for row in conn.execute("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname=current_schema()")}
        sources = [table for table in ('real_protection_plans', 'real_protection_events') if table in tables]
        if not sources:
            return []
        rows = conn.execute(' UNION ALL '.join(f'SELECT payload FROM {table} WHERE config_id=%s' for table in sources),
                            [config_id] * len(sources)).fetchall()
    records = []
    for row in rows:
        try:
            plan = json.loads(row["payload"])
            if not isinstance(plan, dict) or not isinstance(plan.get('adoption', {}), dict):
                raise ValueError('Invalid position adoption history')
            baseline = plan.get('management_adoption') or plan.get("adoption") or {}
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


def adopt_position(market_tool, symbol: str, pos_side: str, expected_amount: float,
                   reason: str, operation_id: str) -> dict:
    """Verify shared management without submitting or modifying orders.

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
        request = {"symbol": canonical, "side": pos_side, "expected_amount": expected_amount,
                   "reason": reason, "account_scope": service.account_scope}
        request_hash = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        result = _saved_result(service, operation_id, request_hash)
        if result is not None:
            return result
        from backend.utils.position_management import management_action
        with management_action(market_tool, canonical, pos_side, operation_id) as claim:
            # The old explicit tool now uses the same enrollment and account
            # coordination as every other action. Existing orders need not be
            # cleared, and another authorized actor does not veto management.
            result = _saved_result(service, operation_id, request_hash)
            if result is not None:
                claim['outcome_status'] = 'completed'
                return result
            position = _position_snapshot(service.ex, canonical, pos_side, expected_amount, contract_size)
            plan = service._load(canonical, pos_side)
            if not plan:
                raise ValueError('Current position management evidence is unavailable')
            adopted_at = time.time()
            result = {"status": "completed", "action": "adopt_position", "operation_id": operation_id,
                      "symbol": canonical, "pos_side": pos_side, "amount": position["amount"],
                      "entry_price": position["entry_price"], "episode_id": plan["episode_id"],
                      "protection_state": "ACTIVE", "adopted_at": adopted_at,
                      "message": "Position verified for shared management; no exchange orders were placed or changed"}
            # Keep this legacy operation's replay evidence without replacing the
            # current management baseline or attributing manual fills to it.
            evidence = {**plan, 'management_adoption': {
                'operation_id': operation_id, 'request_hash': request_hash,
                'account_scope': service.account_scope, 'symbol': canonical, 'result': result}}
            with database.get_db_conn() as conn:
                conn.execute('INSERT INTO real_protection_events(timestamp,config_id,symbol,payload) VALUES(%s,%s,%s,%s)',
                             (database._current_timestamp(), service.config_id, canonical, json.dumps(evidence)))
                conn.commit()
            claim['outcome_status'] = 'completed'
            return dict(result)
