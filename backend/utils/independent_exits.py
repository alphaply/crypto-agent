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

from backend.utils.position_protection import PositionProtection, TERMINAL, _lock


EXIT_TYPES = {"market", "take_profit_limit", "stop_market"}


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
    def _new(self, symbol, side, sl=None, tp=None):
        plan = super()._new(symbol, side, None, None)
        plan.update(execution_mode="independent_exits", exits=[], ever_filled=False)
        return plan

    def _save(self, plan):
        super()._save(plan)
        from backend.utils.execution_ledger import register_order
        for record in plan.get("exits", []):
            for oid in {record.get("id"), record.get("execution_order_id")} - {None}:
                register_order(self.account_scope, plan["symbol"], oid, self.config_id,
                               "agent_exit" if record["exit_type"] == "market" else
                               "stop_loss" if record["exit_type"] == "stop_market" else "take_profit",
                               episode_id=plan["episode_id"], side=plan["side"],
                               trigger_price=record.get("trigger_price"),
                               planned_exit=record.get("price"))

    def _client_id(self, operation_id, prefix="cai"):
        value = f"{self.config_id}:{operation_id}" if operation_id else uuid.uuid4().hex
        return prefix + hashlib.sha256(value.encode()).hexdigest()[:28]

    def _remaining(self, record):
        return max(float(record.get("amount", 0)) - float(record.get("filled", 0)), 0)

    def _existing_operation(self, plan, operation_id):
        if operation_id:
            return next((record for record in plan.get("entries", []) + plan.get("exits", [])
                         if record.get("operation_id") == operation_id), None)
        return None

    def _result(self, plan, record):
        contract_size = float(self._market(plan["symbol"]).get("contractSize") or 1)
        return {"id": record.get("id"), "client_id": record.get("client_id"),
                "status": record.get("status"), "episode_id": plan["episode_id"],
                "amount": float(record.get("amount", 0)) * contract_size,
                "remaining": self._remaining(record) * contract_size,
                "exit_type": record.get("exit_type"), "price": record.get("price"),
                "trigger_price": record.get("trigger_price"),
                "protection_state": plan["state"], "error": plan.get("error"),
                "pending": not record.get("id") or record.get("status") in {None, "submitting"}}

    def _active_plan(self, symbol, side, *, create=False):
        plan = self._load(symbol, side)
        if plan and plan["state"] != "DONE":
            if plan.get("execution_mode") != "independent_exits":
                raise ValueError("An attached protection cycle is still active")
            self._reconcile(plan)
        if not plan or plan["state"] == "DONE":
            if not create:
                raise ValueError("No active independent position cycle")
            plan = self._new(symbol, side)
        if plan.get("error") or plan["state"] == "EXITING":
            raise ValueError("Independent orders require reconciliation before another action")
        return plan

    def open(self, symbol, op, operation_id=None):
        from backend.mcp.guard import assert_entry_allowed
        assert_entry_allowed(self.mt, symbol)
        if op.stop_loss is not None or op.take_profit is not None:
            raise ValueError("Independent mode uses the close tool for exits; entry TP/SL must be omitted")
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
            record = dict(client_id=self._client_id(operation_id, "cae"), operation_id=operation_id,
                          id=None, status="submitting", amount=amount, price=price,
                          filled=0, created_at=time.time())
            plan["entries"].append(record)
            self._save(plan)
            params = {**self._params(side, hedged), "clientOrderId": record["client_id"], "timeInForce": "GTC"}
            self._submit(plan, record, "limit", "buy" if side == "LONG" else "sell", params)
            self._reconcile(plan)
            return self._result(plan, record)

    def _submit(self, plan, record, order_type, side, params):
        try:
            order = self.ex.create_order(plan["symbol"], order_type, side,
                                         record["amount"], record.get("price"), params)
            if not order or not order.get("id"):
                raise RuntimeError("Submission outcome unknown; reconcile by client ID")
            record.update(id=str(order["id"]), status=order.get("status") or "open",
                          filled=float(order.get("filled") or 0))
            self._save(plan)
        except ccxt.ExchangeError as exc:
            record["status"] = "rejected"
            plan["error"] = str(exc)
            self._save(plan)
            raise ValueError(f"Exchange rejected order: {exc}") from exc
        except Exception as exc:
            plan["error"] = f"Submission outcome unknown: {exc}"
            self._save(plan)
            raise

    def _submit_exit(self, plan, record, pos):
        hedged = bool(pos.get("hedged", self.ex.fetch_position_mode(plan["symbol"]).get("hedged")))
        params = {**self._params(plan["side"], hedged, closing=True), "clientOrderId": record["client_id"]}
        if not hedged:
            params["reduceOnly"] = True
        if record["exit_type"] == "stop_market":
            params["stopLossPrice"] = record["trigger_price"]
            if self.ex.id in {"binance", "binanceusdm"}:
                params["workingType"] = "CONTRACT_PRICE"
        if record["exit_type"] == "take_profit_limit":
            params["timeInForce"] = "GTC"
        # In particular, never set closePosition / closeFraction, even for full exits.
        self._submit(plan, record, "limit" if record["exit_type"] == "take_profit_limit" else "market",
                     "sell" if plan["side"] == "LONG" else "buy", params)

    def close(self, symbol, pos_side, amount, exit_type="market", price=None,
              trigger_price=None, reason="", operation_id=None):
        with _lock(self.account_scope):
            market = self._market(symbol)
            symbol = market["symbol"]
            previous = self._load(symbol, pos_side)
            existing = self._existing_operation(previous or {}, operation_id)
            if existing:
                return self._result(previous, existing)
            plan = self._active_plan(symbol, pos_side)
            pos = self._position(plan)
            if not pos:
                raise ValueError("Exit orders require an already filled position")
            if price is not None:
                price = float(self.ex.price_to_precision(symbol, positive(price, "price")))
            if trigger_price is not None:
                trigger_price = float(self.ex.price_to_precision(symbol, positive(trigger_price, "trigger price")))
            validate_exit(exit_type, pos_side, self._trigger_reference(plan), price, trigger_price)
            amount = positive(amount, "amount", zero=True)
            available = float(pos["contracts"])
            requested = amount / float(market.get("contractSize") or 1) if amount else available
            if requested > available + 1e-12:
                raise ValueError("Exit quantity exceeds the current position")
            quantity = float(self.ex.amount_to_precision(symbol, requested))
            positive(quantity, "rounded amount")
            outstanding = sum(self._remaining(r) for r in plan["exits"]
                              if r["exit_type"] == exit_type and r.get("status") not in TERMINAL)
            if exit_type != "market" and outstanding + quantity > available + 1e-12:
                raise ValueError("Outstanding exits of this type would exceed the position")
            record = dict(client_id=self._client_id(operation_id), operation_id=operation_id,
                          id=None, status="submitting", amount=quantity, filled=0,
                          exit_type=exit_type, price=price, trigger_price=trigger_price,
                          reason=reason, created_at=time.time())
            plan["exits"].append(record)
            self._save(plan)
            self._submit_exit(plan, record, pos)
            self._reconcile(plan)
            return self._result(plan, record)

    def _find_order(self, symbol, order_id):
        symbol = self._market(symbol)["symbol"]
        for side in ("LONG", "SHORT"):
            plan = self._load(symbol, side)
            if not plan or plan.get("execution_mode") != "independent_exits":
                continue
            for record in plan.get("entries", []) + plan.get("exits", []):
                if str(record.get("id")) == str(order_id):
                    return plan, record
        raise ValueError("Order is not owned by this independent configuration")

    def cancel(self, symbol, order_id, operation_id=None):
        with _lock(self.account_scope):
            plan, record = self._find_order(symbol, order_id)
            successors = [item for item in plan.get('exits', [])
                          if str(item.get('replaces')) == str(order_id)]
            if record.get('replacement') or successors:
                raise ValueError('Order has a replacement intent or successor; reconcile and cancel the current exit ID')
            if operation_id:
                record['cancel_operation_id'] = operation_id
                self._save(plan)
            self._cancel(record, plan, record.get("exit_type") == "stop_market")
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
            self._save(plan)
        self._cancel(record, plan, record["exit_type"] == "stop_market")
        pos = self._position(plan)
        if record.get("status") == "closed" or not pos:
            record.pop("replacement", None)
            self._save(plan)
            return record
        quantity = max(request["remaining"] - max(float(record.get("filled", 0)) - request["original_filled"], 0), 0)
        others = sum(self._remaining(r) for r in plan["exits"] if r is not record and
                     r["exit_type"] == record["exit_type"] and r.get("status") not in TERMINAL)
        quantity = min(quantity, max(float(pos["contracts"]) - others, 0))
        quantity = float(self.ex.amount_to_precision(plan["symbol"], quantity))
        record.pop("replacement", None)
        if quantity <= 0:
            self._save(plan)
            return record
        replacement = dict(client_id=request["client_id"], operation_id=request["operation_id"],
                           id=None, status="submitting", filled=0, amount=quantity,
                           exit_type=record["exit_type"], price=request["price"],
                           trigger_price=request["trigger_price"], reason=request["reason"],
                           replaces=record["id"], created_at=time.time())
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
            if not record.get("exit_type") or record["exit_type"] == "market" or record.get("status") in TERMINAL:
                raise ValueError("Only an open independent TP/SL exit can be amended")
            if plan.get("error") or not self._position(plan):
                raise ValueError("Reconcile the position and exit orders first")
            price = record.get("price") if price is None else float(self.ex.price_to_precision(plan["symbol"], positive(price, "price")))
            trigger_price = record.get("trigger_price") if trigger_price is None else float(self.ex.price_to_precision(plan["symbol"], positive(trigger_price, "trigger price")))
            validate_exit(record["exit_type"], plan["side"], self._trigger_reference(plan), price, trigger_price)
            contract_size = float(self._market(plan["symbol"]).get("contractSize") or 1)
            remaining = self._remaining(record) if amount is None else positive(amount, "remaining amount") / contract_size
            remaining = float(self.ex.amount_to_precision(plan['symbol'],remaining))
            positive(remaining, "remaining amount")
            minimum = (self._market(plan['symbol']).get('limits') or {}).get('amount',{}).get('min')
            if minimum is not None and remaining < float(minimum):
                raise ValueError('Amended remaining amount is below the exchange minimum')
            pos = self._position(plan)
            others = sum(self._remaining(r) for r in plan["exits"] if r is not record and
                         r["exit_type"] == record["exit_type"] and r.get("status") not in TERMINAL)
            if others + remaining > float(pos["contracts"]) + 1e-12:
                raise ValueError("Amended exit quantities exceed position")
            result = self._replace(plan, record, remaining, price=price, trigger_price=trigger_price,
                                   reason=reason, operation_id=operation_id)
            self._reconcile(plan)
            return self._result(plan, result)

    def amend_entry(self, symbol, order_id, price=None, amount=None, reason="", pos_side=None, operation_id=None,
                    entry_price=None):
        # Native edit intent is persisted by the shared entry amendment implementation.
        return super().amend_entry(symbol, order_id, entry_price if entry_price is not None else price,
                                   amount, reason, pos_side, operation_id=operation_id)

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
            plan.update(state="ACTIVE", error="Entry filled during cleanup; residual position requires management")
        elif plan.get('cleanup_fill_unseen'):
            # A cancellation acknowledgement proving a new fill is stronger than
            # one eventually consistent empty position response. Keep polling;
            # never mark the cycle DONE while that residual has not been observed.
            plan.update(state='EXITING',error='Late entry fill confirmed but residual position not yet visible; awaiting reconciliation')
        else:
            plan.update(state="DONE", error=None)
            from backend.utils.execution_metrics import observe
            observe(self,plan,None)
        self._save(plan)

    def _reconcile(self, plan):
        if plan["state"] == "DONE":
            return
        if plan.get("execution_mode") != "independent_exits":
            return super()._reconcile(plan)
        try:
            plan["error"] = None
            for record in plan.get("entries", []) + plan.get("exits", []):
                if record.get("status") not in TERMINAL:
                    previous_filled = float(record.get('filled',0))
                    if self._query(record, plan["symbol"], record.get("exit_type") == "stop_market") is None:
                        raise RuntimeError(f"Order outcome unresolved: {record['client_id']}")
                    if not record.get('exit_type'):
                        new_fill = max(float(record.get('filled',0))-previous_filled,0)
                        if new_fill:
                            plan['unobserved_entry_fills'] = float(plan.get('unobserved_entry_fills',0)) + new_fill
                    if record.get("status") in {None, "submitting"}:
                        raise RuntimeError(f"Order state unresolved: {record['client_id']}")
                    if record.get("amendment", {}).get("state") == "pending":
                        raise RuntimeError("Entry amendment outcome unresolved")
            pos = self._position(plan)
            if not pos and plan.get('unobserved_entry_fills'):
                plan['cleanup_fill_unseen'] = float(plan.get('cleanup_fill_unseen',0)) + plan.pop('unobserved_entry_fills')
            elif pos:
                plan.pop('cleanup_fill_unseen',None)
                plan.pop('unobserved_entry_fills',None)
            if pos or any(float(record.get('filled', 0)) > 0 for record in plan.get('entries', [])):
                plan["ever_filled"] = True
            if plan["state"] == "EXITING" or (plan.get("ever_filled") and not pos):
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
                self._cancel(record, plan, record['exit_type'] == 'stop_market')
            for record in replacements:
                self._replace(plan, record, record["replacement"]["remaining"])
            for kind in ("take_profit_limit", "stop_market"):
                live = [r for r in plan["exits"] if r["exit_type"] == kind and r.get("status") not in TERMINAL]
                total = sum(self._remaining(r) for r in live)
                quantity = float(self._position(plan).get("contracts", 0)) if self._position(plan) else 0
                if total > quantity + 1e-12:
                    ratio = quantity / total
                    # Cancel all affected orders before replacing. This prevents the
                    # first replacement from stealing capacity from later siblings.
                    targets = [(r, self._remaining(r) * ratio) for r in live]
                    for record, target in targets:
                        record["replacement"] = dict(remaining=target, original_filled=float(record.get("filled", 0)),
                            price=record.get("price"), trigger_price=record.get("trigger_price"),
                            reason="position reduced; proportional quantity correction", operation_id=None,
                            client_id=self._client_id(None))
                    self._save(plan)
                    for record, _ in targets:
                        self._cancel(record, plan, kind == "stop_market")
                    for record, target in targets:
                        self._replace(plan, record, target)
            plan["verified_at"] = time.time()
            self._save(plan)
        except Exception as exc:
            plan["error"] = str(exc)
            self._save(plan)
            raise

    def snapshot(self, symbol):
        symbol = self._market(symbol)["symbol"]
        result = {"mode": "independent_exits", "exits": [], "uncovered": {"LONG": 0.0, "SHORT": 0.0}, "pending": False}
        contract_size = float(self._market(symbol).get("contractSize") or 1)
        for side in ("LONG", "SHORT"):
            plan = self._load(symbol, side)
            if not plan or plan["state"] == "DONE" or plan.get("execution_mode") != "independent_exits":
                continue
            position = self._position(plan)
            quantity = float((position or {}).get("contracts", 0)) * contract_size
            stops = 0.0
            for record in plan.get("exits", []):
                if record.get("status") in TERMINAL:
                    continue
                item = self._result(plan, record)
                item.update(order_id=record.get("id"), pos_side=side)
                result["exits"].append(item)
                if record["exit_type"] == "stop_market" and record.get("status") == "open":
                    stops += item["remaining"]
                result["pending"] |= item["pending"] or bool(record.get("replacement"))
            result["uncovered"][side] = max(quantity - stops, 0)
            result["pending"] |= bool(plan.get("error")) or plan["state"] == "EXITING"
        return result
