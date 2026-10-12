"""Durable quantity-limited exits for one aggregate perpetual position.

Independent exits share ownership and entry reconciliation with protection plans,
but never enter the attached-bracket ``legs`` / emergency-flatten lifecycle.
"""
from __future__ import annotations

import hashlib
import math
import time
import uuid

import ccxt

from backend.utils.trade_receipts import bind_attempt, current_attempt, project_receipt, unresolved_orders

from backend.utils.position_protection import PositionProtection, TERMINAL, _lock
from backend.utils.exit_orders import (
    LIMIT_TYPES, canonical_order_type, exit_group, is_conditional,
    normalize_close, observed_order_status, order_reference, parse_order_reference,
    positive_number, validate_exit_order,
)


EXIT_TYPES = {"market", "take_profit_limit", "stop_market"}
FLAT_CONFIRMATION_SECONDS = 2.0


class IndependentPositionError(ValueError):
    """A verified position cannot safely be managed by this independent cycle."""

    def __init__(self, message, *, code, details):
        super().__init__(message)
        self.code = code
        self.details = details


def positive(value, name, *, zero=False):
    result = float(value)
    if not math.isfinite(result) or result < 0 or (not zero and result == 0):
        raise ValueError(f"{name} must be finite and {'nonnegative' if zero else 'positive'}")
    return result


def validate_exit(exit_type, side, reference, price=None, trigger_price=None):
    if side not in {"LONG", "SHORT"} or exit_type not in EXIT_TYPES:
        raise ValueError("Invalid position side or exit type")
    positive(reference, "current price")
    if exit_type == "market":
        if price is not None or trigger_price is not None:
            raise ValueError("Market exits do not accept a limit or trigger price")
    elif exit_type == "take_profit_limit":
        positive(price, "limit price")
        if trigger_price is not None:
            raise ValueError("Limit exits do not accept trigger_price")
        if not (price > reference if side == "LONG" else price < reference):
            raise ValueError("TP limit must be on the profit side of the current price")
    else:
        positive(trigger_price, "trigger price")
        if price is not None:
            raise ValueError("Stop-market exits do not accept a limit price")
        if not (trigger_price < reference if side == "LONG" else trigger_price > reference):
            raise ValueError("SL trigger must be on the loss side of the current price")


class IndependentExits(PositionProtection):
    def _load(self, symbol, side):
        try:
            plan = super()._load(symbol, side)
        except ValueError as exc:
            if 'different exchange credentials' not in str(exc):
                raise
            raise IndependentPositionError(str(exc), code='independent_position_cycle_missing', details={
                'symbol': symbol, 'pos_side': side, 'cycle_state': 'ACCOUNT_MISMATCH',
                'reason_code': 'account_scope_mismatch', 'restorable': False}) from exc
        if plan and plan.get('execution_mode') == 'independent_exits':
            for record in plan.get('entries', []) + plan.get('exits', []):
                record.setdefault('contract_version', 1)
                record.setdefault('order_type', canonical_order_type(record))
                record.setdefault('quantity_mode', 'fixed')
                record.setdefault('exchange_channel', 'algo' if is_conditional(record) else 'regular')
                if record.get('id'):
                    record.setdefault('exchange_order_id', str(record['id']))
        return plan

    def _new(self, symbol, side, sl=None, tp=None):
        plan = super()._new(symbol, side, None, None)
        plan.update(execution_mode="independent_exits", exits=[], ever_filled=False)
        return plan

    def _save(self, plan):
        # Persist the exchange's verified conversion factor for restart recovery,
        # which cannot assume that stored contract quantities are base units.
        plan['contract_size'] = positive(self._market(plan['symbol']).get('contractSize') or 1,
                                         'contract size')
        super()._save(plan)
        from backend.utils.execution_ledger import register_order
        for record in plan.get("exits", []):
            # Algo and ordinary numeric IDs occupy different namespaces. Actual
            # fills refer to the ordinary child ID, never the algo parent ID.
            parent = order_reference(record, current=False) if is_conditional(record) else record.get('id')
            for oid in {parent, record.get("execution_order_id")} - {None}:
                register_order(self.account_scope, plan["symbol"], oid, self.config_id,
                               "stop_loss" if exit_group(record) == 'stop_loss' else
                               "take_profit" if exit_group(record) == 'take_profit' else 'agent_exit',
                               episode_id=plan["episode_id"], side=plan["side"],
                               trigger_price=record.get("trigger_price"),
                               planned_exit=record.get("price"))

    def _client_id(self, operation_id, prefix="cai"):
        value = f"{self.config_id}:{operation_id}" if operation_id else uuid.uuid4().hex
        return prefix + hashlib.sha256(value.encode()).hexdigest()[:28]

    def _exit_reference(self, plan, record):
        return self._trigger_reference({**plan, 'trigger_basis': record.get('trigger_basis', plan.get('trigger_basis', 'last'))})

    def _remaining(self, record):
        if record.get('quantity_mode') == 'all':
            return None
        return max(float(record.get("amount", 0)) - float(record.get("filled", 0)), 0)

    @staticmethod
    def _record_fill(record, order, previous_filled):
        """Keep the last verified cumulative fill when a response is incomplete."""
        try:
            raw = order.get("filled")
            if raw is None or isinstance(raw, bool):
                raise ValueError("Missing fill quantity")
            filled = positive(raw, "order fill", zero=True)
            if filled + 1e-12 < previous_filled:
                raise ValueError("Cumulative fill moved backwards")
        except (TypeError, ValueError, OverflowError):
            record.update(filled=previous_filled, fill_pending=True)
            return False
        record["filled"] = max(filled, previous_filled)
        record['fill_verified'] = True
        record['fill_observed_at'] = time.time()
        record.pop("fill_pending", None)
        return True

    def _query(self, record, symbol, trigger=False, *, budget=None):
        previous_filled = float(record.get("filled", 0))
        previous_status = record.get("status")
        child = record.get('execution_order_id')
        conditional = is_conditional(record) and not child
        params = {'trigger': True} if conditional else {}
        if not record.get('id'):
            key = 'clientAlgoId' if conditional and self.ex.id in {'binance', 'binanceusdm'} else 'clientOrderId'
            params[key] = record['client_id']
        try:
            old_timeout = getattr(self.ex, 'timeout', None)
            if budget is not None:
                remaining = budget['deadline'] - time.monotonic()
                if remaining <= 0 or budget['calls'] >= 3:
                    raise TimeoutError('Order verification deadline reached')
                budget['calls'] += 1
                self.ex.timeout = min(old_timeout or 2000, max(1, int(remaining * 1000)))
            try:
                order = self.ex.fetch_order(child or record.get('id'), symbol, params=params)
            finally:
                if budget is not None:
                    if old_timeout is None:
                        del self.ex.timeout
                    else:
                        self.ex.timeout = old_timeout
        except ccxt.OrderNotFound:
            return None
        if not order or not order.get('id'):
            raise RuntimeError('Exchange returned no verifiable order ID')
        record['submission_state'] = 'submitted'
        if not child:
            record.update(id=str(order['id']), exchange_order_id=str(order['id']))
        info = order.get('info') or {}
        if conditional:
            native_status = str(info.get('algoStatus') or info.get('state') or '').upper()
            record['parent_status'] = native_status or order.get('status')
            actual = info.get('actualOrderId') or (info.get('ordId') if self.ex.id == 'okx' else None)
            if str(actual) in {'0', '-1'}:
                actual = None
            if actual and str(actual) not in {'0', '-1'}:
                record.update(execution_order_id=str(actual), parent_algo_id=record['id'], triggered=True)
                return self._query(record, symbol, budget=budget)
            if (native_status in {'NEW', 'LIVE'} and order.get('filled') is None
                    and previous_status not in TERMINAL and not record.get('cancel_confirmed_at')):
                record.update(status='open', waiting_trigger=True, fill_verified=False)
                record.pop('fill_pending', None)
                return order
            if native_status in {'TRIGGERED', 'TRIGGERING', 'FINISHED', 'EFFECTIVE'} and not actual:
                record.update(status='open', triggered=True, fill_pending=True)
                record.pop('waiting_trigger', None)
                raise RuntimeError('Conditional order triggered; execution order identity is unverified')
            if (native_status in {'CANCELED', 'CANCELLED', 'EXPIRED', 'REJECTED'}
                    and not record.get('triggered') and not actual and order.get('filled') is None):
                order = {**order, 'filled': previous_filled,
                         'status': {'CANCELED': 'canceled', 'CANCELLED': 'canceled',
                                    'EXPIRED': 'expired', 'REJECTED': 'rejected'}[native_status]}
            record['waiting_trigger'] = order.get('status') == 'open' and not record.get('triggered')
        else:
            record.pop('waiting_trigger', None)
        record['status'] = order.get('status')
        if (order is not None and previous_status in TERMINAL
                and order.get('status') not in TERMINAL):
            record.update(status=previous_status, filled=previous_filled, fill_pending=True)
            raise RuntimeError("Cancellation fill outcome is unverified; awaiting a terminal order response")
        if order is not None and not self._record_fill(record, order, previous_filled):
            raise RuntimeError("Order fill quantity is unverified; reconcile the existing order ID")
        if (child or not conditional) and order.get('amount') is not None:
            actual_amount = positive(order['amount'], 'execution quantity', zero=True)
            if record.get('quantity_mode') == 'all':
                record['execution_amount'] = actual_amount
                record['resolved_amount'] = actual_amount * float(self._market(symbol).get('contractSize') or 1)
            else:
                record['amount'] = actual_amount
        amendment = record.get('amendment')
        if amendment and amendment.get('state') == 'pending':
            if order.get('price') == amendment['price'] and order.get('amount') == amendment['amount']:
                record.update(price=amendment['price'], amount=amendment['amount'])
                amendment['state'] = 'confirmed'
            elif record['status'] in TERMINAL:
                amendment['state'] = 'terminal_unconfirmed'
        return order

    def _record_cancel_fill(self, record, response):
        info = response.get('info') or {}
        incomplete_algo_ack = is_conditional(record) and info.get('algoId') and 'code' in info and 'executedQty' not in info
        incomplete_regular_ack = (self.ex.id in {'binance', 'binanceusdm'} and info.get('orderId')
                                  and 'executedQty' not in info)
        if incomplete_algo_ack or incomplete_regular_ack:
            # CCXT may synthesize filled=0 for an ACK with no execution fields.
            # It is cancellation proof, not proof of the cumulative child fills.
            response = {**response, 'filled': None}
        return self._record_fill(record, response, float(record.get('filled', 0)))

    def _position(self, plan):
        # An unavailable or malformed response must not become proof of flatness.
        try:
            positions = self.ex.fetch_positions([plan["symbol"]])
        except Exception as exc:
            plan['position_error'] = str(exc)
            raise
        if not isinstance(positions, list):
            raise RuntimeError("Exchange position response is unavailable")
        found = None
        for position in positions:
            if not isinstance(position, dict) or position.get("contracts") is None:
                raise RuntimeError("Exchange position quantity is unavailable")
            quantity = positive(position["contracts"], "exchange position quantity", zero=True)
            side = str(position.get("side", "")).upper()
            if quantity and side not in {"LONG", "SHORT"}:
                raise RuntimeError("Exchange position direction is unavailable")
            if position.get("symbol") and position["symbol"] != plan["symbol"]:
                raise RuntimeError("Exchange returned a different position symbol")
            if quantity and side == plan["side"]:
                if found is not None:
                    raise RuntimeError("Multiple exchange positions require reconciliation")
                found = position
        plan.pop('position_error', None)
        plan['position_observation'] = {
            'side': plan['side'], 'amount': float((found or {}).get('contracts', 0)) * float(plan.get('contract_size') or 1),
            'status': 'verified', 'observed_at': time.time()}
        return found

    def _missing_cycle_result(self, symbol, side, plan):
        position = self._position({"symbol": symbol, "side": side})
        cycle_state = (plan or {}).get("state", "MISSING")
        if not position:
            return {"status": "skipped", "reason": "no_position", "symbol": symbol,
                    "pos_side": side, "cycle_state": cycle_state, "position_amount": 0.0}
        contract_size = positive(self._market(symbol).get("contractSize") or 1, "contract size")
        from backend.utils.independent_cycle_recovery import diagnose_cycle
        diagnosis, _ = diagnose_cycle(self, symbol, side, plan)
        raise IndependentPositionError(
            "An exchange position exists but has no active independent position cycle",
            code="independent_position_cycle_missing",
            details={**diagnosis, "symbol": symbol, "pos_side": side, "cycle_state": cycle_state,
                     "position_amount": float(position["contracts"]) * contract_size,
                     "next_action": "Verify this symbol, position side, exchange account and task ownership; "
                                    "if the user requested management of a manually opened position, use adopt_position_real "
                                    "(MCP: adopt_perpetual_position) with its exact expected_amount first. "
                                    "Otherwise reconcile the original task's entry and exit records. "
                                    "Do not create another entry or retry an unknown order with a new operation ID."},
        )

    def _existing_operation(self, plan, operation_id):
        if operation_id:
            return next((record for record in plan.get("entries", []) + plan.get("exits", [])
                         if record.get("operation_id") == operation_id), None)
        return None

    def _result(self, plan, record):
        contract_size = float(self._market(plan["symbol"]).get("contractSize") or 1)
        pending = record.get("status") != "rejected" and bool(
            not record.get("id") or record.get("status") in {None, "submitting"}
            or record.get("fill_pending") or record.get("replacement")
            or record.get('amendment', {}).get('state') in {'pending', 'terminal_unconfirmed'}
            or plan.get("error") or plan["state"] == "EXITING"
            or plan.get("unobserved_entry_fills") or plan.get("cleanup_fill_unseen"))
        remaining = self._remaining(record)
        amount = record.get('amount')
        all_quantity = record.get('quantity_mode') == 'all'
        actual_total = record.get('execution_amount', record.get('submission_amount')) if all_quantity else amount
        actual_remaining = max(float(actual_total) - float(record.get('filled', 0)), 0) * contract_size if actual_total is not None else None
        result = {"id": record.get("id"), "client_id": record.get("client_id"),
                "status": record.get("status"), "episode_id": plan["episode_id"],
                "amount": float(amount) * contract_size if amount is not None and not all_quantity else None,
                "remaining": remaining * contract_size if remaining is not None else None,
                "exit_type": record.get("exit_type"), "price": record.get("price"),
                "trigger_price": record.get("trigger_price"),
                "protection_state": plan["state"], "error": plan.get("error"),
                "pending": pending}
        status = observed_order_status(record)
        result.update(order_id=order_reference(record), order_status=status,
                      parent_order_id=order_reference(record, current=False) if record.get('execution_order_id') else None,
                      exchange_order_id=record.get('execution_order_id') or record.get('id'),
                      exchange_channel='regular' if record.get('execution_order_id') else
                          record.get('exchange_channel', 'algo' if is_conditional(record) else 'regular'),
                      contract_version=record.get('contract_version', 1),
                      order_type=canonical_order_type(record), quantity_mode=record.get('quantity_mode', 'fixed'),
                      filled_amount=(float(record.get('filled', 0)) * contract_size
                                     if record.get('fill_verified', True) else None),
                      remaining_amount=actual_remaining, actual_remaining=actual_remaining,
                      requested_amount=record.get('requested_amount'),
                      resolved_amount=record.get('resolved_amount'),
                      trigger_basis=record.get('trigger_basis', plan.get('trigger_basis', 'last')),
                      side=plan['side'], pos_side=plan['side'])
        if record.get('replacement') or record.get('replaces'):
            result['edit_method'] = 'cancel_replace'
            result['previous_order_id'] = record.get('replaces_reference') or record.get('replaces')
        if record.get('amendment'):
            result['edit_method'] = 'native'
            result['amendment_state'] = record['amendment']['state']
        if record.get('contract_version', 1) >= 2:
            result['status'] = ('failed' if record.get('status') == 'rejected' or record.get('replacement_error')
                                or record.get('amendment', {}).get('state') == 'rejected' else
                                'pending' if pending else 'completed' if record.get('status') in TERMINAL
                                or record.get('amendment', {}).get('state') == 'confirmed' else 'submitted')
        if record.get('replacement_error'):
            result.update(error=record['replacement_error'], error_code='quantity_conflict')
        if record.get('contract_version', 1) >= 2 or current_attempt.get() is not None:
            attempt = current_attempt.get()
            action = attempt.action if attempt else 'close' if record.get('exit_type') else 'open'
            op_id = attempt.operation_id if attempt else record.get('operation_id')
            return project_receipt(plan, record, operation_id=op_id, action=action, base=result)
        return result

    def _active_plan(self, symbol, side, *, create=False):
        plan = self._load(symbol, side)
        if plan and plan["state"] != "DONE":
            if plan.get("execution_mode") != "independent_exits":
                raise ValueError("An attached protection cycle is still active")
            try:
                self._reconcile(plan)
            except Exception as exc:
                exc.plan = plan
                raise
        if not plan or plan["state"] == "DONE":
            if not create:
                return None
            if self._position({'symbol': symbol, 'side': side}):
                from backend.utils.independent_cycle_recovery import restore_cycle
                restored, _ = restore_cycle(self, symbol, side, plan)
                if restored is None:
                    self._missing_cycle_result(symbol, side, plan)
                plan = restored
            else:
                plan = self._new(symbol, side)
        self._admit(plan, 'open' if create else 'close')
        return plan

    def _admit(self, plan, action):
        from backend.utils.independent_admission import assess_admission
        hedged = bool(self.ex.fetch_position_mode(plan['symbol']).get('hedged'))
        opposite = None
        if not hedged:
            opposite = self._load(plan['symbol'], 'SHORT' if plan['side'] == 'LONG' else 'LONG')
            if opposite and opposite.get('state') != 'DONE':
                self.collect_readonly(opposite)
        assessment = assess_admission(plan, action, hedged=hedged, opposite=opposite)
        if not assessment['allowed']:
            exc = IndependentPositionError('Independent orders require reconciliation before another action',
                                           code='trade_recovery_required', details=assessment)
            exc.plan = plan
            raise exc

    def open(self, symbol, op, operation_id=None, contract_version=1):
        from backend.mcp.guard import assert_entry_allowed
        assert_entry_allowed(self.mt, symbol)
        if op.stop_loss is not None or op.take_profit is not None:
            raise ValueError("Independent mode uses the close tool for exits; entry TP/SL must be omitted")
        if contract_version == 2:
            positive_number(op.amount, 'amount')
            positive_number(op.entry_price, 'price')
        with _lock(self.account_scope):
            market = self._market(symbol)
            symbol = market["symbol"]
            side = "LONG" if op.action == "BUY_LIMIT" else "SHORT"
            previous = self._load(symbol, side)
            existing = self._existing_operation(previous or {}, operation_id)
            if existing:
                return self._result(previous, existing)
            plan = self._active_plan(symbol, side, create=True)
            price = float(self.ex.price_to_precision(symbol, positive(op.entry_price, "entry price")))
            amount = float(self.ex.amount_to_precision(symbol, positive(op.amount, "amount") /
                                                      float(market.get("contractSize") or 1)))
            positive(amount, "rounded amount")
            hedged = bool(self.ex.fetch_position_mode(symbol).get("hedged"))
            if not hedged and any(float(p.get("contracts") or 0) > 0 and
                                  str(p.get("side", "")).upper() != side
                                  for p in self.ex.fetch_positions([symbol])):
                raise ValueError("Close opposite exposure before opening in one-way mode")
            if not hedged:
                opposite = self._load(symbol, 'SHORT' if side == 'LONG' else 'LONG')
                if opposite and opposite.get('state') != 'DONE' and any(
                        entry.get('status') not in TERMINAL for entry in opposite.get('entries', [])):
                    raise ValueError('Opposite entry is pending in one-way mode')
                # Exchange orders cover external entries and another process's
                # in-flight submissions as well as the local plan.
                if hasattr(self.ex, 'fetch_open_orders'):
                    opposite_action = 'sell' if side == 'LONG' else 'buy'
                    for order in self.ex.fetch_open_orders(symbol):
                        info = order.get('info') or {}
                        reduce_only = order.get('reduceOnly', info.get('reduceOnly', False))
                        if (str(order.get('side') or info.get('side') or '').lower() == opposite_action
                                and str(reduce_only).lower() != 'true'):
                            raise ValueError('Opposite entry is pending in one-way mode')
            record = dict(client_id=self._client_id(operation_id, "cae"), operation_id=operation_id,
                          id=None, status="submitting", amount=amount, price=price,
                          filled=0, created_at=time.time(), contract_version=contract_version,
                          order_type='limit', quantity_mode='fixed', exchange_channel='regular')
            plan["entries"].append(record)
            self._save(plan)
            params = {**self._params(side, hedged), "clientOrderId": record["client_id"], "timeInForce": "GTC"}
            self._submit(plan, record, "limit", "buy" if side == "LONG" else "sell", params)
            self._reconcile(plan)
            return self._result(plan, record)

    def _submit(self, plan, record, order_type, side, params):
        attempt = bind_attempt(plan, record, 'submit')
        record['submission_state'] = 'unknown'
        if attempt:
            attempt.submission_state = 'unknown'
        try:
            order = self.ex.create_order(plan["symbol"], order_type, side,
                                         record.get('submission_amount', record["amount"]), record.get("price"), params)
        except (ccxt.InvalidOrder, ccxt.InsufficientFunds, ccxt.AuthenticationError, ccxt.PermissionDenied) as exc:
            record["status"] = "rejected"
            record['submission_state'] = 'not_submitted'
            record['execution_error'] = str(exc)
            if attempt:
                attempt.submission_state = 'not_submitted'
            plan["error"] = str(exc)
            self._save(plan)
            raise ValueError(f"Exchange rejected order: {exc}") from exc
        except Exception as exc:
            record['execution_error'] = str(exc)
            plan["error"] = f"Submission outcome unknown: {exc}"
            self._save(plan)
            raise
        # A persistence or parser failure after acceptance is not a rejection.
        if not order or not order.get('id'):
            raise RuntimeError('Submission outcome unknown; reconcile by client ID')
        previous_filled = float(record.get('filled', 0))
        record.update(id=str(order['id']), exchange_order_id=str(order['id']),
                      status=order.get('status') or 'open', submission_state='submitted')
        if attempt:
            attempt.phase, attempt.submission_state = 'verify', 'submitted'
        if is_conditional(record) and order.get('filled') is None:
            record['fill_verified'] = False
        else:
            self._record_fill(record, order, previous_filled)
        if not record.get('exit_type'):
            new_fill = max(float(record['filled']) - previous_filled, 0)
            if new_fill:
                plan['unobserved_entry_fills'] = float(plan.get('unobserved_entry_fills', 0)) + new_fill
        self._save(plan)

    def _submit_exit(self, plan, record, pos):
        hedged = bool(pos.get("hedged", self.ex.fetch_position_mode(plan["symbol"]).get("hedged")))
        params = {**self._params(plan["side"], hedged, closing=True), "clientOrderId": record["client_id"]}
        kind = canonical_order_type(record)
        dynamic_all = record.get('quantity_mode') == 'all' and is_conditional(record)
        if dynamic_all:
            params.pop('reduceOnly', None)
            if self.ex.id in {'binance', 'binanceusdm'}:
                params['closePosition'] = True
            elif self.ex.id == 'okx' and not hedged:
                params.update(closeFraction='1', reduceOnly=True)
            else:
                raise ValueError('unsupported_combination: adapter does not support dynamic conditional all')
        elif not hedged:
            params["reduceOnly"] = True
        if is_conditional(record):
            params['stopLossPrice' if kind.startswith('stop_') else 'takeProfitPrice'] = record['trigger_price']
            if self.ex.id in {"binance", "binanceusdm"}:
                params['workingType'] = 'MARK_PRICE' if record.get('trigger_basis', plan.get('trigger_basis')) == 'mark' else 'CONTRACT_PRICE'
            elif self.ex.id == 'okx':
                params['slTriggerPxType' if kind.startswith('stop_') else 'tpTriggerPxType'] = record.get('trigger_basis', plan.get('trigger_basis', 'last'))
        if kind in LIMIT_TYPES:
            params["timeInForce"] = "GTC"
        self._submit(plan, record, 'limit' if kind in LIMIT_TYPES else 'market',
                      "sell" if plan["side"] == "LONG" else "buy", params)

    def close(self, symbol, pos_side, amount=None, exit_type="market", price=None,
              trigger_price=None, reason="", operation_id=None, contract_version=1, close_all=False):
        intent = normalize_close(exit_type, amount, close_all, contract_version)
        kind = intent['order_type']
        if contract_version == 2:
            if price is not None:
                positive_number(price, 'price')
            if trigger_price is not None:
                positive_number(trigger_price, 'trigger price')
        with _lock(self.account_scope):
            market = self._market(symbol)
            symbol = market["symbol"]
            if pos_side not in {"LONG", "SHORT"}:
                raise ValueError("Invalid position side")
            previous = self._load(symbol, pos_side)
            existing = self._existing_operation(previous or {}, operation_id)
            if existing:
                return self._result(previous, existing)
            plan = self._active_plan(symbol, pos_side)
            if plan is None:
                current = self._load(symbol, pos_side)
                from backend.utils.independent_cycle_recovery import restore_cycle
                plan, _ = restore_cycle(self, symbol, pos_side, current)
                if plan is None:
                    return self._missing_cycle_result(symbol, pos_side, current)
            pos = self._position(plan)
            if not pos:
                raise ValueError("Exit orders require an already filled position")
            if price is not None:
                price = float(self.ex.price_to_precision(symbol, positive(price, "price")))
            if trigger_price is not None:
                trigger_price = float(self.ex.price_to_precision(symbol, positive(trigger_price, "trigger price")))
            validate_exit_order(kind, pos_side, self._trigger_reference(plan), price, trigger_price)
            available = float(pos["contracts"])
            contract_size = float(market.get('contractSize') or 1)
            requested = amount / contract_size if amount else available
            if requested > available + 1e-12:
                raise ValueError("Exit quantity exceeds the current position")
            dynamic_all = close_all and kind != 'market'
            if dynamic_all:
                hedged = bool(self.ex.fetch_position_mode(symbol).get('hedged'))
                if self.ex.id not in {'binance', 'binanceusdm'} and not (self.ex.id == 'okx' and not hedged):
                    raise ValueError('unsupported_combination: adapter does not support dynamic conditional all')
                from backend.mcp.guard import assert_position_owner
                assert_position_owner(self.mt, symbol, plan_override=plan, adopted_only=True)
                quantity = None
            else:
                quantity = float(self.ex.amount_to_precision(symbol, requested))
                positive(quantity, "rounded amount")
            record = dict(client_id=self._client_id(operation_id), operation_id=operation_id,
                          id=None, status="submitting", amount=None if close_all else quantity, filled=0,
                          exit_type=exit_type, price=price, trigger_price=trigger_price,
                          reason=reason, created_at=time.time(), contract_version=contract_version,
                          order_type=kind, quantity_mode=intent['quantity_mode'],
                          exchange_channel='algo' if kind not in {'market', 'limit'} else 'regular',
                          requested_amount=None if close_all else quantity * contract_size,
                          resolved_amount=quantity * contract_size if close_all and kind == 'market' else None,
                          trigger_basis=plan.get('trigger_basis', 'last'))
            if close_all and kind == 'market':
                record['submission_amount'] = quantity
            plan["exits"].append(record)
            self._save(plan)
            self._submit_exit(plan, record, pos)
            self._reconcile(plan)
            return self._result(plan, record)

    def _find_order(self, symbol, order_id):
        symbol = self._market(symbol)["symbol"]
        channel, raw_id = parse_order_reference(order_id)
        matches = []
        for side in ("LONG", "SHORT"):
            plan = self._load(symbol, side)
            if not plan or plan.get("execution_mode") != "independent_exits":
                continue
            for record in plan.get("entries", []) + plan.get("exits", []):
                identities = [(record.get('exchange_channel') or ('algo' if is_conditional(record) else 'regular'), record.get('id'))]
                if record.get('execution_order_id'):
                    identities.append(('regular', record['execution_order_id']))
                for actual_channel, actual_id in identities:
                    if actual_id is not None and str(actual_id) == raw_id and (channel is None or channel == actual_channel):
                        matches.append((plan, record))
        if len(matches) > 1:
            raise ValueError('Ambiguous order identity; use the qualified order_id returned by query')
        if matches:
            return matches[0]
        raise ValueError("Order is not owned by this independent configuration")

    def describe_order(self, symbol, order_id):
        with _lock(self.account_scope):
            plan, record = self._find_order(symbol, order_id)
            if record.get('status') not in TERMINAL or record.get('fill_pending'):
                self._query(record, plan['symbol'])
                self._save(plan)
            result = self._result(plan, record)
            return {**result, 'kind': 'exit' if record.get('exit_type') else 'entry',
                    'side': plan['side'], 'amount': result['amount'],
                    'filled_amount': result['filled_amount'], 'remaining_amount': result['actual_remaining'],
                    'price': record.get('price'), 'order_id': order_reference(record)}

    def _cancel(self, record, plan, trigger=False, *, allow_child=True, verify_budget=None):
        if record.get('status') in TERMINAL and not record.get('fill_pending'):
            return
        order = self._query(record, plan['symbol'], budget=verify_budget)
        if order is None:
            raise RuntimeError('Order absent during cleanup; keep plan blocked for reconciliation')
        if record.get('status') in TERMINAL:
            return
        if record.get('execution_order_id') and not allow_child:
            return
        target = record.get('execution_order_id') or record['id']
        conditional = is_conditional(record) and not record.get('execution_order_id')
        target_key = ('algo:' if conditional else 'regular:') + str(target)
        if (record.get('cancel_target') != target_key or not record.get('cancel_requested_at')
                or time.time() - record['cancel_requested_at'] >= 15):
            record.update(cancel_requested_at=time.time(), cancel_target=target_key)
            self._save(plan)
            attempt = bind_attempt(plan, record, 'cancel')
            if attempt:
                attempt.submission_state = 'unknown'
            try:
                response = self.ex.cancel_order(target, plan['symbol'], params={'trigger': True} if conditional else {}) or {}
            except ccxt.NetworkError:
                response = None
            except ccxt.OrderNotFound:
                response = None
            else:
                if attempt:
                    attempt.submission_state = 'submitted'
                info = response.get('info') or {}
                actual = info.get('actualOrderId') or (info.get('ordId') if conditional and self.ex.id == 'okx' else None)
                if conditional and actual and str(actual) not in {'0', '-1'}:
                    record.update(execution_order_id=str(actual), parent_algo_id=record['id'], triggered=True)
                    self._save(plan)
                    budget = verify_budget or {'deadline': time.monotonic() + 2, 'calls': 0}
                    return self._cancel(record, plan, allow_child=allow_child, verify_budget=budget)
                if conditional and str(info.get('algoStatus') or '').upper() in {'TRIGGERED', 'TRIGGERING', 'FINISHED'}:
                    budget = verify_budget or {'deadline': time.monotonic() + 2, 'calls': 0}
                    self._query(record, plan['symbol'], budget=budget)
                    return self._cancel(record, plan, allow_child=allow_child, verify_budget=budget)
                status = response.get('status')
                same_order = str(response.get('id') or info.get('algoId') or '') == str(target)
                if same_order and conditional and str(info.get('code')) == '200' and status is None:
                    status = 'canceled'
                if same_order and status in TERMINAL:
                    record.update(status=status, cancel_confirmed_at=time.time())
                    record.pop('waiting_trigger', None)
                    if not self._record_cancel_fill(record, response):
                        plan['error'] = 'Cancellation confirmed but cumulative fill is unverified; reconcile the existing order ID'
                        self._save(plan)
                    else:
                        self._save(plan)
                        return
        budget = verify_budget or {'deadline': time.monotonic() + 2, 'calls': 0}
        for delay in (0, .15, .35):
            if time.monotonic() + delay >= budget['deadline']:
                break
            if delay:
                time.sleep(delay)
            try:
                record['last_attempt_at'] = time.time()
                record['attempt_count'] = record.get('attempt_count', 0) + 1
                self._query(record, plan['symbol'], budget=budget)
            except (ccxt.NetworkError, RuntimeError, TimeoutError) as exc:
                record['query_error'] = str(exc)
                continue
            if conditional and record.get('execution_order_id'):
                return self._cancel(record, plan, allow_child=allow_child, verify_budget=budget)
            if record.get('status') in TERMINAL and not record.get('fill_pending'):
                record['cancel_confirmed_at'] = time.time()
                self._save(plan)
                return
        record['next_retry_at'] = time.time() + min(60, 2 ** min(record.get('attempt_count', 1), 6))
        self._save(plan)
        raise RuntimeError('Cancellation confirmed but cumulative fill is unverified; reconcile the existing order ID'
                           if record.get('cancel_confirmed_at') else
                           'Cancellation not confirmed; reconcile the existing order before continuing')

    def cancel(self, symbol, order_id, operation_id=None):
        with _lock(self.account_scope):
            plan, record = self._find_order(symbol, order_id)
            self._admit(plan, 'cancel')
            successors = [item for item in plan.get('exits', [])
                          if str(item.get('replaces')) == str(record.get('id'))]
            if record.get('replacement') or successors:
                raise ValueError('Order has a replacement intent or successor; reconcile and cancel the current exit ID')
            record.pop('replacement_error', None)
            if operation_id:
                record['cancel_operation_id'] = operation_id
                self._save(plan)
            self._cancel(record, plan)
            self._reconcile(plan)
            return self._result(plan, record)

    def _replace(self, plan, record, remaining, *, price=None, trigger_price=None,
                 reason="quantity correction", operation_id=None):
        if record.get("replacement"):
            request = record["replacement"]
        else:
            request = dict(remaining=remaining, original_filled=float(record.get("filled", 0)),
                           price=price, trigger_price=trigger_price, reason=reason,
                           operation_id=operation_id, client_id=self._client_id(operation_id))
            record["replacement"] = request
        # Keep the attempted edit recoverable even when no successor is needed
        # (for example a trigger race or fills exhausting its target).
        record['last_operation_id'] = request.get('operation_id')
        self._save(plan)
        self._cancel(record, plan, allow_child=False)
        if record.get('execution_order_id'):
            record.pop('replacement', None)
            record['replacement_error'] = 'order_already_triggered: edit the current execution order explicitly'
            self._save(plan)
            return record
        if record.get("status") == "closed" and not record.get("fill_pending"):
            record.pop("replacement", None)
            self._save(plan)
            return record
        try:
            pos = self._position(plan)
            if not pos:
                raise RuntimeError("Position absent during exit replacement; awaiting reconciliation")
        except Exception:
            # The cancellation may be final, but a single read cannot abandon
            # the replacement. Maintenance resumes this exact persisted intent.
            plan["error"] = "Exit replacement pending position verification; do not repeat the order"
            self._save(plan)
            raise
        dynamic_all = record.get('quantity_mode') == 'all'
        quantity = None if dynamic_all else max(request["remaining"] - max(float(record.get("filled", 0)) - request["original_filled"], 0), 0)
        if quantity is not None and quantity > float(pos['contracts']) + 1e-12:
            record.pop('replacement', None)
            record['replacement_error'] = 'Replacement quantity exceeds current position; choose a new explicit amount'
            record['last_operation_id'] = request.get('operation_id')
            self._save(plan)
            return record
        if quantity is not None:
            quantity = float(self.ex.amount_to_precision(plan["symbol"], quantity))
        record.pop("replacement", None)
        if quantity is not None and quantity <= 0:
            self._save(plan)
            return record
        replacement = dict(client_id=request["client_id"], operation_id=request["operation_id"],
                           id=None, status="submitting", filled=0, amount=quantity,
                           exit_type=record["exit_type"], price=request["price"],
                            trigger_price=request["trigger_price"], reason=request["reason"],
                            replaces=record["id"], replaces_reference=order_reference(record, current=False),
                            created_at=time.time(), contract_version=record.get('contract_version', 1),
                            order_type=canonical_order_type(record), quantity_mode=record.get('quantity_mode', 'fixed'),
                            exchange_channel=record.get('exchange_channel', 'algo' if is_conditional(record) else 'regular'),
                            trigger_basis=record.get('trigger_basis', plan.get('trigger_basis', 'last')),
                            requested_amount=quantity * float(self._market(plan['symbol']).get('contractSize') or 1) if quantity is not None else None)
        plan["exits"].append(replacement)
        self._save(plan)
        self._submit_exit(plan, replacement, pos)
        return replacement

    def amend_exit(self, symbol, order_id, amount=None, price=None, trigger_price=None,
                   reason="", operation_id=None):
        with _lock(self.account_scope):
            plan, record = self._find_order(symbol, order_id)
            existing = self._existing_operation(plan, operation_id)
            if existing:
                return self._result(plan, existing)
            self._reconcile(plan)
            self._admit(plan, 'edit')
            if not record.get("exit_type") or record["exit_type"] == "market" or record.get("status") in TERMINAL:
                raise ValueError("Only an open independent TP/SL exit can be amended")
            if plan.get("error") or not self._position(plan):
                raise ValueError("Reconcile the position and exit orders first")
            price = record.get("price") if price is None else float(self.ex.price_to_precision(plan["symbol"], positive(price, "price")))
            trigger_price = record.get("trigger_price") if trigger_price is None else float(self.ex.price_to_precision(plan["symbol"], positive(trigger_price, "trigger price")))
            validate_exit_order(canonical_order_type(record), plan["side"], self._exit_reference(plan, record), price, trigger_price)
            contract_size = float(self._market(plan["symbol"]).get("contractSize") or 1)
            remaining = self._remaining(record) if amount is None else positive(amount, "remaining amount") / contract_size
            remaining = float(self.ex.amount_to_precision(plan['symbol'],remaining))
            positive(remaining, "remaining amount")
            minimum = (self._market(plan['symbol']).get('limits') or {}).get('amount',{}).get('min')
            if minimum is not None and remaining < float(minimum):
                raise ValueError('Amended remaining amount is below the exchange minimum')
            pos = self._position(plan)
            if remaining > float(pos["contracts"]) + 1e-12:
                raise ValueError("Amended exit quantities exceed position")
            result = self._replace(plan, record, remaining, price=price, trigger_price=trigger_price,
                                   reason=reason, operation_id=operation_id)
            self._reconcile(plan)
            return self._result(plan, result)

    def amend_entry(self, symbol, order_id, price=None, amount=None, reason="", pos_side=None, operation_id=None,
                    entry_price=None):
        # Native edit intent is persisted by the shared entry amendment implementation.
        from backend.mcp.guard import assert_entry_allowed
        assert_entry_allowed(self.mt, symbol)
        with _lock(self.account_scope):
            plan, record = self._find_order(symbol, order_id)
            self.collect_readonly(plan)
            self._admit(plan, 'edit')
            if current_attempt.get() is not None:
                factor = float(self._market(plan['symbol']).get('contractSize') or 1)
                remaining = float(amount) - float(record.get('filled') or 0) * factor if amount is not None else None
                return self.edit(symbol, order_id, remaining_amount=remaining,
                                 price=entry_price if entry_price is not None else price,
                                 reason=reason, operation_id=operation_id)
            return super().amend_entry(symbol, order_id, entry_price if entry_price is not None else price,
                                      amount, reason, pos_side, operation_id=operation_id)

    def edit(self, symbol, order_id, remaining_amount=None, price=None, trigger_price=None,
             reason='', operation_id=None):
        """Edit a managed order; the quantity target is its unfilled base amount."""
        if all(value is None for value in (remaining_amount, price, trigger_price)):
            raise ValueError('Provide price, trigger_price or remaining_amount')
        if not str(reason).strip():
            raise ValueError('An edit reason is required')
        for name, value in (('remaining_amount', remaining_amount), ('price', price), ('trigger_price', trigger_price)):
            if value is not None:
                positive_number(value, name)
        with _lock(self.account_scope):
            plan, record = self._find_order(symbol, order_id)
            existing = self._existing_operation(plan, operation_id)
            if existing:
                return self._v2_result(plan, existing)
            if operation_id and record.get('last_operation_id') == operation_id:
                return self._v2_result(plan, record)
            amendment = record.get('amendment') or {}
            if operation_id and amendment.get('operation_id') == operation_id:
                return self._v2_result(plan, record)
            if not record.get('exit_type'):
                from backend.mcp.guard import assert_entry_allowed
                assert_entry_allowed(self.mt, plan['symbol'])
            self._reconcile(plan)
            order = self._query(record, plan['symbol'])
            self._admit(plan, 'edit')
            if order is None:
                raise RuntimeError('Order outcome unresolved; reconcile the original identity')
            requested_channel, requested_id = parse_order_reference(order_id)
            is_child = (record.get('execution_order_id') and requested_id == record['execution_order_id']
                        and requested_channel != 'algo')
            if record.get('execution_order_id') and not is_child:
                record['last_operation_id'] = operation_id
                record['replacement_error'] = 'order_already_triggered: edit the current execution order explicitly'
                self._save(plan)
                return {**self._v2_result(plan, record), 'status': 'failed',
                        'error_code': 'order_already_triggered',
                        'error': 'Condition already triggered; explicitly edit the returned execution order_id'}
            if record.get('status') in TERMINAL:
                raise ValueError('Order already ended; do not reopen it as an edit')
            if record.get('replacement') or record.get('amendment', {}).get('state') == 'pending' or plan.get('error'):
                raise ValueError('Order modification is unresolved; reconcile before another edit')
            kind = canonical_order_type(record)
            effective_kind = ('limit' if kind in LIMIT_TYPES else 'market') if is_child else kind
            if effective_kind == 'market':
                raise ValueError('Market execution orders cannot be edited')
            if record.get('quantity_mode') == 'all' and remaining_amount is not None:
                raise ValueError('Dynamic all orders do not accept remaining_amount')
            new_price = record.get('price') if price is None else float(self.ex.price_to_precision(plan['symbol'], positive(price, 'price')))
            new_trigger = record.get('trigger_price') if trigger_price is None else float(self.ex.price_to_precision(plan['symbol'], positive(trigger_price, 'trigger price')))
            if effective_kind == 'limit':
                if trigger_price is not None:
                    raise ValueError('Regular orders cannot edit trigger_price')
                validate_exit_order('limit', plan['side'], self._exit_reference(plan, record), new_price)
            else:
                validate_exit_order(kind, plan['side'], self._exit_reference(plan, record), new_price, new_trigger)
            contract_size = float(self._market(plan['symbol']).get('contractSize') or 1)
            remaining = self._remaining(record) if remaining_amount is None else positive(remaining_amount, 'remaining amount') / contract_size
            if remaining is not None:
                remaining = float(self.ex.amount_to_precision(plan['symbol'], remaining))
                positive(remaining, 'remaining amount')
                minimum = (self._market(plan['symbol']).get('limits') or {}).get('amount', {}).get('min')
                if minimum is not None and remaining < float(minimum):
                    raise ValueError('Amended remaining amount is below the exchange minimum')
            if record.get('exit_type'):
                pos = self._position(plan)
                if not pos:
                    raise ValueError('Exit edit requires an already filled position')
                if remaining is not None and remaining > float(pos['contracts']) + 1e-12:
                    raise ValueError('Amended exit quantity exceeds position')
            record.pop('replacement_error', None)
            if effective_kind != 'limit':
                result = self._replace(plan, record, remaining, price=new_price, trigger_price=new_trigger,
                                       reason=reason, operation_id=operation_id)
                self._reconcile(plan)
                return self._v2_result(plan, result)
            # Both venues modify the existing LIMIT, preserving its side and
            # close-only semantics. Never fall back to recreation after a timeout.
            if self.ex.id not in {'binance', 'binanceusdm', 'okx'}:
                raise ValueError('unsupported_combination: native LIMIT edit unavailable')
            total = float(self.ex.amount_to_precision(plan['symbol'], float(record.get('filled', 0)) + remaining))
            record['amendment'] = dict(state='pending', price=new_price, amount=total,
                                       remaining=remaining, filled_at_verification=float(record.get('filled', 0)),
                                       operation_id=operation_id, reason=reason, requested_at=time.time())
            self._save(plan)
            target = record.get('execution_order_id') or record['id']
            side = ('sell' if plan['side'] == 'LONG' else 'buy') if record.get('exit_type') else ('buy' if plan['side'] == 'LONG' else 'sell')
            attempt = bind_attempt(plan, record, 'amend')
            if attempt:
                attempt.submission_state = 'unknown'
            try:
                self.ex.edit_order(target, plan['symbol'], 'limit', side, total, new_price)
            except (ccxt.InvalidOrder, ccxt.InsufficientFunds, ccxt.AuthenticationError, ccxt.PermissionDenied) as exc:
                record['amendment']['state'] = 'rejected'
                record['execution_error'] = str(exc)
                if attempt:
                    attempt.submission_state = 'not_submitted'
                self._save(plan)
                raise
            except Exception as exc:
                plan['error'] = f'Amendment outcome unresolved: {exc}'
                self._save(plan)
                raise
            if attempt:
                attempt.submission_state = 'submitted'
            self._query(record, plan['symbol'])
            self._save(plan)
            self._reconcile(plan)
            return self._v2_result(plan, record)

    def _v2_result(self, plan, record):
        result = self._result(plan, record)
        result['contract_version'] = 2
        result['status'] = ('failed' if record.get('replacement_error') or record.get('status') == 'rejected'
                            or record.get('amendment', {}).get('state') == 'rejected' else
                            'pending' if result['pending'] else 'completed')
        if str(record.get('replacement_error', '')).startswith('order_already_triggered'):
            result['error_code'] = 'order_already_triggered'
        return result

    def _cleanup(self, plan):
        plan["state"] = "EXITING"
        self._save(plan)
        for record in plan.get("entries", []) + plan.get("exits", []):
            previous_filled = float(record.get('filled',0))
            self._cancel(record, plan, record.get("exit_type") == "stop_market")
            if not record.get('exit_type'):
                new_fill = max(float(record.get('filled',0))-previous_filled,0)
                if new_fill:
                    plan['cleanup_fill_unseen'] = float(plan.get('cleanup_fill_unseen',0)) + new_fill
                    self._save(plan)
        pos = self._position(plan)
        if pos:
            plan.pop('cleanup_fill_unseen',None)
            plan.pop('flat_candidate', None)
            plan.update(state="ACTIVE", error="Entry filled during cleanup; residual position requires management", residual_verified=True)
        elif plan.get('cleanup_fill_unseen'):
            # A cancellation acknowledgement proving a new fill is stronger than
            # one eventually consistent empty position response. Keep polling;
            # never mark the cycle DONE while that residual has not been observed.
            plan.update(state='EXITING',error='Late entry fill confirmed but residual position not yet visible; awaiting reconciliation')
        else:
            plan.pop('flat_candidate', None)
            plan.update(state="DONE", error=None)
            from backend.utils.execution_metrics import observe
            observe(self,plan,None)
        self._save(plan)

    def _flat_confirmed(self, plan):
        entry_filled = sum(positive(record.get("filled", 0), "entry fill", zero=True)
                           for record in plan.get("entries", []))
        entry_filled += positive((plan.get('adoption') or {}).get('contracts', 0), 'adopted quantity', zero=True)
        exit_filled = sum(positive(record.get("filled", 0), "exit fill", zero=True)
                          for record in plan.get("exits", []))
        if entry_filled > 0 and exit_filled + 1e-12 >= entry_filled:
            return True
        now = time.time()
        candidate = plan.get("flat_candidate")
        if candidate is None:
            candidate = plan["flat_candidate"] = {"first_observed_at": now, "observations": 0}
        candidate["observations"] += 1
        if candidate["observations"] > 1 and now - candidate["first_observed_at"] >= FLAT_CONFIRMATION_SECONDS:
            return True
        plan["error"] = "Position temporarily absent; awaiting a later flat confirmation before cancelling exits"
        self._save(plan)
        return False

    def _reconcile(self, plan, *, respect_backoff=False):
        if plan["state"] == "DONE":
            return
        if plan.get("execution_mode") != "independent_exits":
            return super()._reconcile(plan)
        try:
            plan["error"] = None
            errors = self._collect_order_evidence(plan, respect_backoff=respect_backoff)
            if errors:
                raise RuntimeError('; '.join(errors))
            if plan.get('adoption') or any(r.get('quantity_mode') == 'all' and is_conditional(r)
                                          and r.get('status') not in TERMINAL for r in plan.get('exits', [])):
                from backend.mcp.guard import assert_position_owner
                assert_position_owner(self.mt, plan['symbol'], plan_override=plan, adopted_only=True)
            pos = self._position(plan)
            if not pos and plan.get("ever_filled") and plan["state"] != "EXITING":
                # A second read may already show an eventually consistent
                # position. Neither empty read alone authorizes cancelling exits.
                pos = self._position(plan)
            if not pos and plan.get('unobserved_entry_fills'):
                plan['cleanup_fill_unseen'] = float(plan.get('cleanup_fill_unseen',0)) + plan.pop('unobserved_entry_fills')
            elif pos:
                plan.pop('cleanup_fill_unseen',None)
                plan.pop('unobserved_entry_fills',None)
                plan.pop('flat_candidate', None)
            if pos or any(float(record.get('filled', 0)) > 0 for record in plan.get('entries', [])):
                plan["ever_filled"] = True
            if not pos and plan.get('cleanup_fill_unseen'):
                plan["error"] = "Entry fill confirmed but position not yet visible; awaiting reconciliation"
                self._save(plan)
                return
            if plan["state"] == "EXITING" or (plan.get("ever_filled") and not pos):
                if plan["state"] != "EXITING" and not self._flat_confirmed(plan):
                    return
                self._cleanup(plan)
                return
            if not pos:
                if plan["entries"] and all(r.get("status") in TERMINAL for r in plan["entries"]):
                    self._cleanup(plan)
                else:
                    self._save(plan)
                return
            plan["state"] = "ACTIVE"
            from backend.utils.execution_metrics import observe
            observe(self,plan,pos)
            # Resume cancel/replace intent after restart; a submitting successor is
            # queried above and is never recreated merely because an ACK was lost.
            replacements = [record for record in plan['exits'] if record.get('replacement')]
            for record in replacements:
                self._replace(plan, record, record["replacement"]["remaining"])
            # Quantity conflicts are observable facts, never an automatic
            # allocation policy. Only pre-existing persisted replacements resume.
            plan["verified_at"] = time.time()
            self._save(plan)
        except Exception as exc:
            # Failed/unknown reads break consecutive flat observations.
            plan.pop('flat_candidate', None)
            plan["error"] = str(exc)
            self._save(plan)
            raise

    def _collect_order_evidence(self, plan, *, respect_backoff=False):
        """Read every unresolved order, including when an earlier read fails."""
        errors = []
        for record in plan.get('entries', []) + plan.get('exits', []):
            if record.get('status') in TERMINAL and not record.get('fill_pending'):
                continue
            if respect_backoff and record.get('next_retry_at', 0) > time.time():
                errors.append(record.get('query_error') or 'Order verification backing off')
                continue
            previous = float(record.get('filled') or 0)
            record['last_attempt_at'] = time.time()
            record['attempt_count'] = record.get('attempt_count', 0) + 1
            try:
                if self._query(record, plan['symbol']) is None:
                    raise RuntimeError(f"Order outcome unresolved: {record['client_id']}")
                if record.get('status') in {None, 'submitting'}:
                    raise RuntimeError(f"Order state unresolved: {record['client_id']}")
                if record.get('amendment', {}).get('state') == 'pending':
                    raise RuntimeError('Entry amendment outcome unresolved')
                record.pop('query_error', None)
                record.pop('next_retry_at', None)
            except Exception as exc:
                record['query_error'] = str(exc)
                record['next_retry_at'] = time.time() + min(60, 2 ** min(record['attempt_count'], 6))
                errors.append(str(exc))
            finally:
                if not record.get('exit_type'):
                    delta = max(float(record.get('filled') or 0) - previous, 0)
                    if delta:
                        plan['unobserved_entry_fills'] = float(plan.get('unobserved_entry_fills', 0)) + delta
        plan['read_errors'] = errors
        return errors

    def collect_readonly(self, plan):
        """Refresh evidence without reconciliation, cleanup, replacement or adoption."""
        errors = self._collect_order_evidence(plan)
        try:
            self._position(plan)
        except Exception as exc:
            plan['position_error'] = str(exc)
            errors.append(str(exc))
        plan['read_errors'] = errors
        plan['verified_at'] = time.time()
        return plan

    def snapshot(self, symbol):
        symbol = self._market(symbol)["symbol"]
        result = {"mode": "independent_exits", "exits": [], "entries": [], "adopted_positions": [], 'unresolved_orders': [],
                  "uncovered": {"LONG": 0.0, "SHORT": 0.0}, "pending": False,
                  'quantity_conflict': False, 'quantity_conflicts': [], 'exit_groups': {}}
        contract_size = float(self._market(symbol).get("contractSize") or 1)
        for side in ("LONG", "SHORT"):
            plan = self._load(symbol, side)
            if not plan or plan["state"] == "DONE" or plan.get("execution_mode") != "independent_exits":
                continue
            result['unresolved_orders'].extend(unresolved_orders(plan))
            if plan.get('adoption'):
                baseline = plan['adoption']
                result['adopted_positions'].append({
                    'source': 'manual_adoption', 'pos_side': side, 'amount': baseline.get('amount'),
                    'adopted_at': baseline.get('adopted_at'), 'episode_id': plan['episode_id'],
                })
            position = self._position(plan)
            quantity = float((position or {}).get("contracts", 0)) * contract_size
            stops = 0.0
            for record in plan.get('entries', []):
                if record.get('status') not in TERMINAL:
                    result['entries'].append({**self._result(plan, record), 'kind': 'entry'})
            groups = {group: {'side': side, 'group': group, 'fixed_remaining': 0.0,
                             'all_count': 0, 'coverage_estimate': 0.0, 'position_amount': quantity}
                      for group in ('limit', 'take_profit', 'stop_loss')}
            for record in plan.get("exits", []):
                result["pending"] |= bool(record.get("replacement") or record.get("fill_pending"))
                if record.get("status") in TERMINAL:
                    continue
                item = self._result(plan, record)
                item.update(pos_side=side, kind='exit', execution_guaranteed=False)
                coverage = (quantity if item['quantity_mode'] == 'all' and item['order_status'] == 'waiting_trigger'
                            else min(item['actual_remaining'] or 0, quantity))
                item['coverage_estimate'] = coverage if not item['pending'] else None
                result["exits"].append(item)
                group = groups.get(exit_group(record))
                if group is not None:
                    if item['quantity_mode'] == 'all':
                        group['all_count'] += 1
                    else:
                        group['fixed_remaining'] += item['remaining'] or 0
                    if not item['pending']:
                        group['coverage_estimate'] = min(quantity, group['coverage_estimate'] + coverage)
                if exit_group(record) == 'stop_loss' and record.get("status") == "open" and not item['pending']:
                    stops += coverage
                result["pending"] |= item["pending"] or bool(record.get("replacement"))
            result["uncovered"][side] = max(quantity - stops, 0)
            for group in groups.values():
                group['excess_amount'] = max(group['fixed_remaining'] - quantity, 0)
                group['quantity_conflict'] = group['excess_amount'] > 1e-12
                if group['quantity_conflict']:
                    result['quantity_conflicts'].append(group.copy())
                    result['quantity_conflict'] = True
            result['exit_groups'][side] = groups
            result["pending"] |= bool(plan.get("error") or plan.get('unobserved_entry_fills')
                                      or plan.get('cleanup_fill_unseen') or plan["state"] == "EXITING"
                                      or any(r.get('fill_pending') for r in plan.get('entries', [])))
        return result
