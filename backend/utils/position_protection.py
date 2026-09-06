"""Durable, position-wide TP/SL lifecycle for real perpetual positions.

Only explicit tool calls change desired prices. Maintenance reconciles fills and
exchange protection orders without an LLM. All exchange writes have durable client
IDs; ambiguous responses are queried before another submission.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
import hashlib

import ccxt

from backend.database import get_db_conn

_locks_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}
TERMINAL = {"closed", "canceled", "cancelled", "expired", "rejected"}


class ConcurrentProtectionUpdate(RuntimeError):
    pass


def _lock(config_id):
    with _locks_guard:
        return _locks.setdefault(config_id, threading.RLock())


class PositionProtection:
    def __init__(self, market_tool):
        self.mt = market_tool
        self.ex = market_tool.exchange
        self.config_id = market_tool.config_id
        account_key = str(getattr(self.ex, 'apiKey', '') or self.config_id)
        self.account_scope = hashlib.sha256(f"{self.ex.id}:{account_key}".encode()).hexdigest()

    def _market(self, symbol):
        self.ex.load_markets()
        market = self.ex.market(symbol)
        if not market.get("contract"):
            market = self.ex.market(f"{symbol}:USDT")
        if not market.get("swap") or not market.get("linear"):
            raise ValueError("TP/SL lifecycle requires a linear perpetual market")
        if self.ex.id not in {"binance", "binanceusdm", "okx"}:
            raise ValueError("Unsupported protection exchange")
        return market

    def _load(self, symbol, side):
        with get_db_conn() as conn:
            row = conn.execute(
                "SELECT payload FROM real_protection_plans WHERE config_id=? AND symbol=? AND side=?",
                (self.config_id, symbol, side),
            ).fetchone()
        plan = json.loads(row["payload"]) if row else None
        if plan and plan['state'] != 'DONE' and plan.get('account_scope') != self.account_scope:
            raise ValueError('Protection plan belongs to different exchange credentials; restore its account configuration')
        return plan

    def _save(self, plan):
        plan["updated_at"] = time.time()
        with get_db_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            for row in conn.execute('SELECT config_id,payload FROM real_protection_plans WHERE symbol=? AND config_id!=?',
                                    (plan['symbol'], self.config_id)).fetchall():
                other = json.loads(row['payload'])
                if other.get('account_scope') == self.account_scope and other['state'] != 'DONE':
                    raise ValueError('Another config owns protection for this exchange account and symbol')
            plan['account_scope'] = self.account_scope
            previous_row = conn.execute('SELECT payload FROM real_protection_plans WHERE config_id=? AND symbol=? AND side=?',
                                        (self.config_id, plan['symbol'], plan['side'])).fetchone()
            previous = json.loads(previous_row['payload']) if previous_row else {}
            if previous.get('record_version', 0) != plan.get('record_version', 0):
                raise ConcurrentProtectionUpdate('Protection plan changed concurrently; reload before another exchange action')
            plan['record_version'] = plan.get('record_version', 0) + 1
            def audit_view(value):
                return {k: v for k, v in value.items() if k not in {'updated_at', 'verified_at', 'account_scope', 'record_version'}}
            if audit_view(previous) != audit_view(plan):
                from backend.database import _current_timestamp
                conn.execute('INSERT INTO real_protection_events(timestamp,config_id,symbol,payload) VALUES(?,?,?,?)',
                             (_current_timestamp(), self.config_id, plan['symbol'], json.dumps(audit_view(plan))))
            conn.execute(
                "INSERT INTO real_protection_plans(config_id,symbol,side,payload) VALUES(?,?,?,?) "
                "ON CONFLICT(config_id,symbol,side) DO UPDATE SET payload=excluded.payload",
                (self.config_id, plan["symbol"], plan["side"], json.dumps(plan)),
            )
            conn.commit()

    def _new(self, symbol, side, sl, tp):
        previous = self._load(symbol, side) or {}
        return dict(symbol=symbol, side=side, stop_loss=sl, take_profit=tp,
                    state="WAITING", entries=[], legs=[], revision=1, error=None,
                    record_version=previous.get('record_version', 0))

    def _position(self, plan):
        for pos in self.ex.fetch_positions([plan["symbol"]]):
            if str(pos.get("side", "")).upper() == plan["side"] and float(pos.get("contracts") or 0) > 0:
                return pos
        return None

    def _query(self, record, symbol, trigger=False):
        params = {"trigger": True} if trigger else {}
        if not record.get("id"):
            if self.ex.id in {"binance", "binanceusdm"} and trigger:
                params["clientAlgoId"] = record["client_id"]
            else:
                params["clientOrderId"] = record["client_id"]
        try:
            order = self.ex.fetch_order(record.get("id"), symbol, params=params)
        except ccxt.OrderNotFound:
            return None
        if not order or not order.get("id"):
            raise RuntimeError("Exchange returned no verifiable order ID")
        record.update(id=str(order["id"]), status=order.get("status"),
                      filled=float(order.get("filled") or 0))
        return order

    def _params(self, side, hedged, closing=False):
        if self.ex.id in {"binance", "binanceusdm"}:
            return {"positionSide": side if hedged else "BOTH"}
        params = {"hedged": hedged}
        if closing:
            params["reduceOnly"] = True
        return params

    @staticmethod
    def _validate(side, reference, sl, tp):
        import math
        if side not in {'LONG', 'SHORT'}:
            raise ValueError('Position side must be LONG or SHORT')
        if not all(math.isfinite(float(x)) and float(x) > 0 for x in (reference, sl, tp)):
            raise ValueError("Entry, TP and SL must be finite positive prices")
        if not (sl < reference < tp if side == "LONG" else tp < reference < sl):
            raise ValueError("LONG requires SL < reference < TP; SHORT requires TP < reference < SL")

    def open(self, symbol, op):
        """Open with protection intent persisted before submitting the entry order."""
        with _lock(self.account_scope):
            market = self._market(symbol)
            symbol = market["symbol"]
            side = "LONG" if op.action == "BUY_LIMIT" else "SHORT"
            price = float(self.ex.price_to_precision(symbol, op.entry_price))
            sl = float(self.ex.price_to_precision(symbol, op.stop_loss))
            tp = float(self.ex.price_to_precision(symbol, op.take_profit))
            self._validate(side, price, sl, tp)
            plan = self._load(symbol, side)
            if plan and plan["state"] != "DONE":
                self._reconcile(plan)
                if plan.get("error") or plan["state"] == "EXITING":
                    raise ValueError("Existing protection needs reconciliation before adding exposure")
                if any(not e.get("id") and e.get('status') not in TERMINAL for e in plan["entries"]):
                    raise ValueError("An entry submission is unresolved; do not duplicate it")
                if (plan["stop_loss"], plan["take_profit"]) != (sl, tp):
                    raise ValueError("Same-side position uses one TP/SL plan; adjust it before adding with different prices")
            if not plan or plan["state"] == "DONE":
                plan = self._new(symbol, side, sl, tp)
            if self._position(plan):
                self._validate(side, float(self.ex.fetch_ticker(symbol)['last']), sl, tp)
            amount = float(self.ex.amount_to_precision(symbol, op.amount / float(market.get("contractSize") or 1)))
            if amount <= 0:
                raise ValueError("Order quantity rounds to zero")
            hedged = bool(self.ex.fetch_position_mode(symbol).get("hedged"))
            if not hedged:
                for position in self.ex.fetch_positions([symbol]):
                    if float(position.get('contracts') or 0) > 0 and str(position.get('side', '')).upper() != side:
                        raise ValueError('One-way mode has opposite exposure; close it explicitly before opening a new direction')
            entry = dict(client_id="cae" + uuid.uuid4().hex[:28], status="submitting", id=None,
                         created_at=time.time(), price=price, amount=amount)
            plan["entries"].append(entry)
            self._save(plan)
            params = {**self._params(side, hedged), "clientOrderId": entry["client_id"], "timeInForce": "GTC"}
            try:
                order = self.ex.create_order(symbol, "limit", "buy" if side == "LONG" else "sell", amount, price, params)
                if not order or not order.get("id"):
                    raise RuntimeError("Entry submission outcome unknown")
                entry.update(id=str(order["id"]), status=order.get("status", "open"))
                self._save(plan)
            except ccxt.ExchangeError as exc:
                entry["status"] = "rejected"
                plan["error"] = f"Entry rejected: {exc}"
                self._save(plan)
                raise
            except Exception as exc:
                plan["error"] = f"Entry submission unresolved: {exc}"
                self._save(plan)
                raise
            try:
                self._reconcile(plan)
            except Exception as exc:
                plan["error"] = str(exc)
                self._save(plan)
            return {**order, "protection_state": plan["state"], "protection_error": plan.get("error"),
                    "stop_loss": sl, "take_profit": tp}

    def adjust(self, symbol, side, sl=None, tp=None):
        """Replace desired position-wide prices, installing the new SL before retiring the old one."""
        with _lock(self.account_scope):
            symbol = self._market(symbol)["symbol"]
            plan = self._load(symbol, side)
            if plan and plan['state'] != 'DONE':
                self._reconcile(plan)
            if not plan or plan["state"] == "DONE":
                if sl is None or tp is None:
                    raise ValueError("First protection setup requires both stop_loss and take_profit")
                plan = self._new(symbol, side, sl, tp)
            if plan["state"] == "EXITING":
                raise ValueError("Exit cleanup is in progress")
            pos = self._position(plan)
            reference = float(self.ex.fetch_ticker(symbol)["last"]) if pos else None
            if reference is None:
                pending = [e for e in plan["entries"] if e.get("status") not in TERMINAL]
                if not pending:
                    raise ValueError("No position or managed pending entry")
                references = [e["price"] for e in pending]
            else:
                references = [reference] + [e['price'] for e in plan['entries'] if e.get('status') not in TERMINAL]
            sl = float(self.ex.price_to_precision(symbol, sl if sl is not None else plan["stop_loss"]))
            tp = float(self.ex.price_to_precision(symbol, tp if tp is not None else plan["take_profit"]))
            for price in references:
                self._validate(side, price, sl, tp)
            plan.update(stop_loss=sl, take_profit=tp, revision=plan["revision"] + 1)
            self._save(plan)
            self._reconcile(plan)
            return plan

    def _cancel(self, record, plan, trigger=False):
        if record.get("status") in TERMINAL:
            return
        if record.get("status") == "prepared":
            record["status"] = "canceled"
            self._save(plan)
            return
        order = self._query(record, plan["symbol"], trigger)
        if order is None:
            raise RuntimeError("Order absent during cleanup; keep plan blocked for reconciliation")
        if record.get("status") in TERMINAL:
            return
        self.ex.cancel_order(record["id"], plan["symbol"], params={"trigger": True} if trigger else {})
        # Do not treat an ACK as proof. Next query also detects fills racing cancellation.
        confirmed = self._query(record, plan["symbol"], trigger)
        if not confirmed or record.get("status") not in TERMINAL:
            raise RuntimeError(f"Cancellation not confirmed: {record['id']}")
        self._save(plan)

    def _finish(self, plan):
        plan["state"] = "EXITING"
        self._save(plan)
        pending_errors = []
        for entry in plan["entries"]:
            try:
                self._cancel(entry, plan)
            except ConcurrentProtectionUpdate:
                raise
            except Exception as exc:
                pending_errors.append(str(exc))
        # A partial fill racing cancellation must not recreate an unprotected position.
        pos = self._position(plan)
        if pos:
            self._emergency_close(plan, pos)
            if pending_errors:
                raise RuntimeError('Exit submitted; entry cancellation unresolved: ' + '; '.join(pending_errors))
            return
        if pending_errors:
            raise RuntimeError('Entry cancellation unresolved: ' + '; '.join(pending_errors))
        for leg in plan["legs"]:
            self._cancel(leg, plan, True)
        plan.update(state="DONE", error=None)
        self._save(plan)

    def _emergency_close(self, plan, pos):
        record = plan.get("exit_order")
        if record and record.get('status') not in {'rejected', 'canceled', 'expired'}:
            if self._query(record, plan["symbol"]) is None:
                raise RuntimeError("Emergency exit outcome unknown; awaiting reconciliation")
            if record.get("status") not in TERMINAL:
                return
            if record.get("status") == "closed":
                # Re-read on a later maintenance pass before sending a residual close.
                plan.pop("exit_order")
                self._save(plan)
                return
        record = dict(client_id="cax" + uuid.uuid4().hex[:28], id=None, status="submitting")
        plan["exit_order"] = record
        self._save(plan)
        params = self._params(plan["side"], bool(pos.get("hedged")), closing=True)
        if self.ex.id in {"binance", "binanceusdm"} and not pos.get("hedged"):
            params["reduceOnly"] = True
        params["clientOrderId"] = record["client_id"]
        try:
            order = self.ex.create_order(plan["symbol"], "market", "sell" if plan["side"] == "LONG" else "buy",
                                         float(pos["contracts"]), None, params)
        except ccxt.ExchangeError:
            record['status'] = 'rejected'
            self._save(plan)
            raise
        if not order or not order.get("id"):
            raise RuntimeError("Emergency close submission outcome unknown")
        record.update(id=str(order["id"]), status=order.get("status") or "open")
        self._save(plan)

    def _ensure_leg(self, plan, pos, kind):
        symbol = plan["symbol"]
        amount = float(pos["contracts"])
        hedged = bool(pos.get("hedged"))
        # Binance closePosition automatically covers partial fills and later additions.
        coverage = 0 if self.ex.id in {"binance", "binanceusdm"} or not hedged else amount
        trigger_price = plan['stop_loss' if kind == 'sl' else 'take_profit']
        leg = next((x for x in plan["legs"] if x["kind"] == kind and x.get('trigger_price') == trigger_price
                    and x["coverage"] == coverage and x.get("status") not in TERMINAL), None)
        if leg is None:
            leg = dict(kind=kind, revision=plan["revision"], coverage=coverage, trigger_price=trigger_price, id=None,
                       client_id="cap" + uuid.uuid4().hex[:28], status="prepared", created_at=time.time())
            plan["legs"].append(leg)
            self._save(plan)
        if leg["status"] == "open":
            return leg
        if leg["status"] == "submitting":
            if self._query(leg, symbol, True):
                self._save(plan)
                if leg["status"] != "open":
                    raise RuntimeError("Protection changed state during reconciliation")
                return leg
            # A missing response is not proof a previous write failed. Never blindly repeat it.
            raise RuntimeError(f"Protection submission unresolved: {leg['client_id']}")
        params = self._params(plan["side"], hedged, closing=True)
        params["clientOrderId"] = leg["client_id"]
        params["stopLossPrice" if kind == "sl" else "takeProfitPrice"] = plan["stop_loss" if kind == "sl" else "take_profit"]
        if self.ex.id in {"binance", "binanceusdm"}:
            params["closePosition"] = True
            amount = None
        elif not hedged:
            params["closeFraction"] = "1"
            amount = None
        leg["status"] = "submitting"
        self._save(plan)
        try:
            order = self.ex.create_order(symbol, "market", "sell" if plan["side"] == "LONG" else "buy", amount, None, params)
        except ccxt.ExchangeError:
            # Explicit rejection is safe to retry with a fresh intent; timeouts are not.
            leg["status"] = "rejected"
            self._save(plan)
            raise
        if not order or not order.get("id"):
            raise RuntimeError("Protection submission outcome unknown")
        leg.update(id=str(order["id"]), status=order.get("status") or "open")
        self._save(plan)
        confirmed = self._query(leg, symbol, True)
        if not confirmed or leg["status"] != "open":
            raise RuntimeError("Protection not confirmed active")
        return leg

    def _reconcile(self, plan):
        if plan["state"] == "DONE":
            return
        plan["error"] = None
        try:
            unresolved_entries = []
            for entry in plan["entries"]:
                if entry.get("status") not in TERMINAL:
                    if self._query(entry, plan["symbol"]) is None:
                        unresolved_entries.append(entry["client_id"])
            for leg in plan["legs"]:
                if leg.get("status") not in TERMINAL and leg["status"] != "prepared":
                    if self._query(leg, plan["symbol"], True) is None:
                        raise RuntimeError(f"Protection unresolved: {leg['client_id']}")
                if leg.get("status") == "closed":
                    plan["state"] = "EXITING"
            self._save(plan)
            pos = self._position(plan)
            if plan["state"] == "EXITING" or (plan["state"] == "ACTIVE" and not pos):
                self._finish(plan)
                return
            if not pos:
                if plan["entries"] and all(e.get("status") in TERMINAL for e in plan["entries"]):
                    self._finish(plan)
                if unresolved_entries:
                    raise RuntimeError(f"Entry unresolved: {unresolved_entries}")
                return
            price = float(self.ex.fetch_ticker(plan["symbol"])["last"])
            crossed = (price <= plan["stop_loss"] or price >= plan["take_profit"] if plan["side"] == "LONG"
                       else price >= plan["stop_loss"] or price <= plan["take_profit"])
            if crossed:
                self._finish(plan)
                return
            try:
                sl = self._ensure_leg(plan, pos, "sl")
            except ccxt.ExchangeError:
                if not any(x["kind"] == "sl" and x.get("status") == "open" for x in plan["legs"]):
                    self._finish(plan)
                raise
            tp = self._ensure_leg(plan, pos, "tp")
            for old in plan["legs"]:
                if old is not sl and old is not tp and old.get("status") not in TERMINAL:
                    self._cancel(old, plan, True)
            plan["state"] = "ACTIVE"
            plan['verified_at'] = time.time()
            if unresolved_entries:
                plan["error"] = f"Entry unresolved: {unresolved_entries}"
            self._save(plan)
        except ConcurrentProtectionUpdate:
            raise
        except Exception as exc:
            plan["error"] = str(exc)
            self._save(plan)
            # If the exchange cannot confirm any SL, resolve an ambiguous submit
            # once by client ID, then reduce known exposure rather than leave it naked.
            try:
                confirmed_sl = any(x['kind'] == 'sl' and x.get('status') == 'open' for x in plan['legs'])
                if not confirmed_sl and plan['state'] != 'EXITING':
                    for leg in plan['legs']:
                        if leg['kind'] == 'sl' and leg.get('status') == 'submitting':
                            try:
                                self._query(leg, plan['symbol'], True)
                            except Exception:
                                continue
                            confirmed_sl = leg.get('status') == 'open'
                            if confirmed_sl:
                                self._save(plan)
                                break
                    if not confirmed_sl and self._position(plan):
                        self._finish(plan)
            except Exception as exit_exc:
                plan['error'] = f'{exc}; failsafe unresolved: {exit_exc}'
                self._save(plan)
            raise

    def reconcile_all(self):
        with _lock(self.account_scope):
            with get_db_conn() as conn:
                rows = conn.execute("SELECT payload FROM real_protection_plans WHERE config_id=?", (self.config_id,)).fetchall()
            results = []
            for row in rows:
                plan = json.loads(row["payload"])
                if plan['state'] != 'DONE' and plan.get('account_scope') != self.account_scope:
                    results.append({'symbol': plan['symbol'], 'side': plan['side'], 'state': plan['state'],
                                    'error': 'Exchange credentials changed; protection plan requires its original account'})
                    continue
                try:
                    self._reconcile(plan)
                except Exception:
                    pass  # Persisted and returned as an actionable maintenance error.
                results.append({"symbol": plan["symbol"], "side": plan["side"], "state": plan["state"], "error": plan.get("error")})
            return results
