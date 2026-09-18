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
        if getattr(self.mt, 'market_type', None) == 'spot':
            raise ValueError('Spot accounts cannot enter the perpetual protection lifecycle')
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
            plan['account_scope'] = self.account_scope
            def audit_view(value):
                # account_scope is a one-way hash and is required to safely recover
                # historical order ownership after credentials or configs change.
                return {k: v for k, v in value.items() if k not in {'updated_at', 'verified_at', 'record_version'}}
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
        from backend.utils.execution_ledger import rebuild_execution_position_history, register_order
        common_history = {
            'side': plan['side'],
            'episode_id': plan.get('episode_id'),
            'stop_loss': plan.get('stop_loss'),
            'take_profit': plan.get('take_profit'),
        }
        for record in plan.get('entries', []):
            register_order(self.account_scope, plan['symbol'], record.get('id'), self.config_id, 'entry',
                           refresh=False, planned_entry=record.get('price'), **common_history)
        for record in plan.get('legs', []):
            # Conditional/algo ID can differ from the actual execution order ID.
            register_order(self.account_scope, plan['symbol'], record.get('execution_order_id') or record.get('id'),
                           self.config_id, 'stop_loss' if record['kind'] == 'sl' else 'take_profit',
                           refresh=False,
                           trigger_price=record.get('trigger_price'), trigger_basis=plan.get('trigger_basis', 'last'),
                           **common_history)
        if plan.get('exit_order'):
            register_order(self.account_scope, plan['symbol'], plan['exit_order'].get('id'), self.config_id,
                           plan.get('exit_reason', 'emergency_exit'), refresh=False, **common_history)
        rebuild_execution_position_history(self.config_id, self.account_scope, plan['symbol'])

    def _new(self, symbol, side, sl, tp):
        previous = self._load(symbol, side) or {}
        return dict(symbol=symbol, side=side, stop_loss=sl, take_profit=tp,
                    state="WAITING", entries=[], legs=[], revision=1, error=None,
                    record_version=previous.get('record_version', 0), trigger_basis='last',
                    episode_id=uuid.uuid4().hex, account_scope=self.account_scope)

    def _trigger_reference(self, plan):
        """Never mix a mark-trigger exchange order with a last-price local exit."""
        if plan.get('trigger_basis', 'last') == 'mark':
            ticker = self.ex.fetch_mark_price(plan['symbol'])
            value = ticker.get('markPrice')
        else:
            ticker = self.ex.fetch_ticker(plan['symbol'])
            value = ticker.get('last')
        import math
        if value is None or not math.isfinite(float(value)) or float(value) <= 0:
            raise ValueError('Configured trigger reference unavailable')
        if ticker.get('timestamp') and time.time() * 1000 - float(ticker['timestamp']) > 120000:
            raise ValueError('Trigger price snapshot stale')
        return float(value)

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
        info = order.get('info') or {}
        execution_id = info.get('actualOrderId')
        if not execution_id and self.ex.id == 'okx':
            execution_id = info.get('ordId')
        if execution_id and str(execution_id) != '0':
            record['execution_order_id'] = str(execution_id)
        amendment = record.get('amendment')
        if amendment and amendment.get('state') == 'pending':
            matches = (order.get('price') is not None and order.get('amount') is not None
                       and float(order['price']) == amendment['price']
                       and float(order['amount']) == amendment['amount'])
            if matches:
                record.update(price=amendment['price'], amount=amendment['amount'])
                amendment['state'] = 'confirmed'
            elif record['status'] in TERMINAL:
                amendment['state'] = 'terminal_unconfirmed'
        return order

    def _confirm_visible(self, record, symbol, trigger=False, required_status=None):
        """Allow brief exchange read-after-write lag without resubmitting the order."""
        for delay in (0, 0.15, 0.35, 0.75):
            if delay:
                time.sleep(delay)
            order = self._query(record, symbol, trigger)
            if order is not None and (required_status is None or record.get('status') == required_status):
                record['confirmed_at'] = time.time()
                return order
            if order is not None and record.get('status') in TERMINAL:
                return order
        return None

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
        if not math.isfinite(float(reference)) or float(reference) <= 0:
            raise ValueError("Entry must be a finite positive price")
        for name, value in (("SL", sl), ("TP", tp)):
            if value is not None and (not math.isfinite(float(value)) or float(value) <= 0):
                raise ValueError(f"{name} must be a finite positive price")
        if sl is not None and not (sl < reference if side == "LONG" else sl > reference):
            raise ValueError("LONG requires SL < reference; SHORT requires reference < SL")
        if tp is not None and not (reference < tp if side == "LONG" else tp < reference):
            raise ValueError("LONG requires reference < TP; SHORT requires TP < reference")

    def open(self, symbol, op):
        """Open with protection intent persisted before submitting the entry order."""
        with _lock(self.account_scope):
            market = self._market(symbol)
            symbol = market["symbol"]
            side = "LONG" if op.action == "BUY_LIMIT" else "SHORT"
            protection_changed = False
            price = float(self.ex.price_to_precision(symbol, op.entry_price))
            sl = float(self.ex.price_to_precision(symbol, op.stop_loss)) if op.stop_loss is not None else None
            tp = float(self.ex.price_to_precision(symbol, op.take_profit)) if op.take_profit is not None else None
            self._validate(side, price, sl, tp)
            amount = float(self.ex.amount_to_precision(symbol, op.amount / float(market.get("contractSize") or 1)))
            if amount <= 0:
                raise ValueError("Order quantity rounds to zero")
            plan = self._load(symbol, side)
            if plan and plan["state"] != "DONE":
                self._reconcile(plan)
            if plan and plan["state"] != "DONE":
                if plan.get("error") or plan["state"] == "EXITING":
                    raise ValueError("Existing protection needs reconciliation before adding exposure")
                if any(not e.get("id") and e.get('status') not in TERMINAL for e in plan["entries"]):
                    raise ValueError("An entry submission is unresolved; do not duplicate it")
                if any(e.get('amendment', {}).get('state') == 'pending' for e in plan['entries']):
                    raise ValueError('An entry amendment is unresolved; wait for reconciliation')
                sl = plan['stop_loss'] if sl is None else sl
                tp = plan['take_profit'] if tp is None else tp
                self._validate(side, price, sl, tp)
                if (plan["stop_loss"], plan["take_profit"]) != (sl, tp):
                    try:
                        plan = self.adjust(symbol, side, sl, tp)
                    except Exception as exc:
                        raise ValueError(f'Protection update failed; no additional entry submitted: {exc}') from exc
                    if plan.get('error') or plan['state'] in {'EXITING', 'DONE'}:
                        raise ValueError('Protection update not verified; no additional entry submitted: '
                                         + str(plan.get('error') or plan['state']))
                    protection_changed = True
            if not plan or plan["state"] == "DONE":
                plan = self._new(symbol, side, sl, tp)
            if self._position(plan):
                self._validate(side, self._trigger_reference(plan), sl, tp)
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
                if protection_changed:
                    raise RuntimeError(f'整仓保护已更新（SL={sl}, TP={tp}），加仓委托被拒绝: {exc}') from exc
                raise
            except Exception as exc:
                plan["error"] = f"Entry submission unresolved: {exc}"
                self._save(plan)
                if protection_changed:
                    raise RuntimeError(f'整仓保护已更新（SL={sl}, TP={tp}），加仓结果待核验，不可重复提交: {exc}') from exc
                raise
            try:
                self._reconcile(plan)
            except Exception as exc:
                plan["error"] = str(exc)
                self._save(plan)
            return {**order, "protection_state": plan["state"], "protection_error": plan.get("error"),
                    "stop_loss": sl, "take_profit": tp}

    def amend_entry(self, symbol, order_id, price=None, amount=None, reason=''):
        """Amend a managed perpetual limit entry. Amount means TOTAL base quantity."""
        if price is None and amount is None:
            raise ValueError('Provide a new entry price or total quantity')
        if not str(reason).strip():
            raise ValueError('A changed entry condition and amendment reason are required')
        with _lock(self.account_scope):
            market = self._market(symbol)
            symbol = market['symbol']
            plan = entry = None
            for side in ('LONG', 'SHORT'):
                candidate = self._load(symbol, side)
                if not candidate or candidate['state'] in {'DONE', 'EXITING'}:
                    continue
                match = next((e for e in candidate['entries'] if str(e.get('id')) == str(order_id)), None)
                if match:
                    plan, entry = candidate, match
                    break
            if entry is None:
                raise ValueError('Only this configuration\'s managed perpetual entry may be amended')
            self._reconcile(plan)
            if plan['state'] in {'DONE', 'EXITING'} or entry.get('status') in TERMINAL:
                raise ValueError('Order already ended; do not reopen it as an amendment')
            if entry.get('amendment', {}).get('state') == 'pending':
                raise ValueError('Previous amendment outcome is unresolved; query rather than resubmit')
            if plan.get('error'):
                raise ValueError('Protection/entry reconciliation must complete before another amendment')
            current = self._query(entry, symbol)
            if not current or entry.get('status') in TERMINAL:
                raise ValueError('Entry is no longer open')
            contract_size = float(market.get('contractSize') or 1)
            new_price = float(self.ex.price_to_precision(symbol, price if price is not None else entry['price']))
            new_amount = float(self.ex.amount_to_precision(symbol, amount / contract_size if amount is not None else entry['amount']))
            import math
            if not math.isfinite(new_amount) or new_amount <= float(current.get('filled') or 0):
                raise ValueError('Total quantity must exceed already filled quantity; use close_position to reduce a position')
            self._validate(plan['side'], new_price, plan['stop_loss'], plan['take_profit'])
            if new_price == entry['price'] and new_amount == entry['amount']:
                return {'id': str(order_id), 'amendment_state': 'unchanged', 'protection_state': plan['state']}
            entry['amendment'] = dict(state='pending', price=new_price, amount=new_amount,
                                      reason=reason, requested_at=time.time())
            entry['last_amended_at'] = entry['amendment']['requested_at']
            self._save(plan)  # Persist intent before the exchange write, including timeouts.
            try:
                self.ex.edit_order(str(order_id), symbol, 'limit',
                                   'buy' if plan['side'] == 'LONG' else 'sell', new_amount, new_price)
            except ccxt.ExchangeError:
                entry['amendment']['state'] = 'rejected'
                self._save(plan)
                raise
            except Exception as exc:
                plan['error'] = f'Amendment outcome unresolved: {exc}'
                self._save(plan)
                raise
            self._query(entry, symbol)
            entry['last_amended_at'] = time.time()
            self._save(plan)
            self._reconcile(plan)
            return {'id': str(order_id), 'amendment_state': entry['amendment']['state'],
                    'order_status': entry.get('status'),
                    'price': entry['price'], 'total_base_amount': entry['amount'] * contract_size,
                    'filled_contracts': entry.get('filled', 0), 'protection_state': plan['state'],
                    'error': plan.get('error')}

    def adjust(self, symbol, side, sl=None, tp=None, clear_sl: bool = False, clear_tp: bool = False,
               expected_revision: int | None = None):
        """Replace desired position-wide prices, installing the new SL before retiring the old one."""
        with _lock(self.account_scope):
            symbol = self._market(symbol)["symbol"]
            plan = self._load(symbol, side)
            current_revision = plan.get('revision', 0) if plan and plan['state'] != 'DONE' else 0
            if expected_revision is not None and current_revision != expected_revision:
                raise ConcurrentProtectionUpdate('保护计划已更新，请刷新后重新调整')
            if (clear_sl and sl is not None) or (clear_tp and tp is not None):
                raise ValueError('不能同时修改和取消同一项保护')
            if plan and plan['state'] != 'DONE':
                self._reconcile(plan)
            if not plan or plan["state"] == "DONE":
                if sl is None and tp is None and not (clear_sl or clear_tp):
                    raise ValueError("First protection setup requires stop_loss and/or take_profit")
                plan = self._new(symbol, side, sl, tp)
            if plan["state"] == "EXITING":
                raise ValueError("Exit cleanup is in progress")
            pos = self._position(plan)
            reference = self._trigger_reference(plan) if pos else None
            if reference is None:
                pending = [e for e in plan["entries"] if e.get("status") not in TERMINAL]
                if not pending:
                    raise ValueError("No position or managed pending entry")
                references = [e["price"] for e in pending]
            else:
                references = [reference] + [e['price'] for e in plan['entries'] if e.get('status') not in TERMINAL]
            references += [e['amendment']['price'] for e in plan['entries']
                           if e.get('amendment', {}).get('state') == 'pending']

            if clear_sl:
                sl_value = None
            else:
                sl_value = sl if sl is not None else plan["stop_loss"]

            if clear_tp:
                tp_value = None
            else:
                tp_value = tp if tp is not None else plan["take_profit"]

            sl = float(self.ex.price_to_precision(symbol, sl_value)) if sl_value is not None else None
            tp = float(self.ex.price_to_precision(symbol, tp_value)) if tp_value is not None else None
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
        if not record.get('cancel_requested_at') or time.time() - record['cancel_requested_at'] >= 15:
            record['cancel_requested_at'] = time.time()
            self._save(plan)
            try:
                result = self.ex.cancel_order(record["id"], plan["symbol"], params={"trigger": True} if trigger else {})
            except ccxt.NetworkError:
                pass  # A lost ACK is ambiguous; query without sending another cancel.
            except Exception:
                record.pop('cancel_requested_at', None)
                self._save(plan)
                raise
            else:
                # Binance algo cancellation returns code=200 and algoId, but no
                # status. CCXT preserves these in info; do not discard this proof
                # and require an eventually consistent GET to confirm it again.
                result = result or {}
                info = result.get('info') or {}
                same_order = str(result.get('id') or info.get('algoId') or '') == str(record['id'])
                status = result.get('status')
                if (same_order and trigger and self.ex.id in {'binance', 'binanceusdm'}
                        and str(info.get('code')) == '200' and status is None):
                    status = 'canceled'
                if same_order and status in TERMINAL:
                    record['status'] = status
                    if result.get('filled') is not None:
                        record['filled'] = float(result['filled'])
                    record['cancel_confirmed_at'] = time.time()
                    self._save(plan)
                    return
        # Exchange reads may lag a successful cancel. Finish confirmation in this call.
        # A filled order is terminal too; callers must handle an exit racing replacement.
        for delay in (0, 0.15, 0.35, 0.75, 1.5, 2, 2, 2, 3, 3):
            if delay:
                time.sleep(delay)
            try:
                confirmed = self._query(record, plan['symbol'], trigger)
            except ccxt.NetworkError:
                continue
            if confirmed and record.get('status') in TERMINAL:
                record['cancel_confirmed_at'] = time.time()
                self._save(plan)
                return
        raise RuntimeError(f"Cancellation not confirmed: {record['id']}")

    def _finish(self, plan, reason=None):
        if reason:
            plan['exit_reason'] = reason
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
        from backend.utils.execution_metrics import observe
        observe(self, plan, None)
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

    def _cancel_conflicting_exchange_triggers(self, plan, kind):
        """Cancel orphaned exchange trigger orders of the same kind and side on Binance."""
        if self.ex.id not in {"binance", "binanceusdm"}:
            return
        if not hasattr(self.ex, 'fetch_open_orders'):
            return
        symbol = plan["symbol"]
        hedged = bool(self.ex.fetch_position_mode(symbol).get("hedged"))
        target_pos_side = plan["side"] if hedged else "BOTH"
        open_orders = self.ex.fetch_open_orders(symbol, params={"trigger": True})
        for o in (open_orders or []):
            info = o.get("info", {})
            pos_side = str(info.get("positionSide") or o.get("positionSide") or "BOTH").upper()
            if pos_side != target_pos_side:
                continue
            expected_side = 'sell' if plan['side'] == 'LONG' else 'buy'
            if str(o.get('side') or info.get('side') or '').lower() != expected_side:
                continue
            close_all = str(info.get("closePosition", "")).lower() == "true" or o.get("closePosition") is True
            if not close_all:
                continue
            order_type = str(info.get("orderType") or info.get('type') or o.get("type") or "").upper()
            is_sl = "STOP" in order_type and "TAKE" not in order_type
            is_tp = "TAKE_PROFIT" in order_type
            if (kind == "sl" and is_sl) or (kind == "tp" and is_tp):
                oid = str(o.get("id"))
                # The open-order list can lag the cancellation ACK as well.
                if any(str(leg.get('id')) == oid and leg.get('status') in TERMINAL
                       for leg in plan.get('legs', [])):
                    continue
                self._cancel({'id': oid, 'status': 'open'}, plan, True)

    def _cleanup_conflicting_legs(self, plan, kind, current_leg):
        """Clean up older legs of the same kind and side when conflicting with exchange constraints."""
        for old in list(plan.get("legs", [])):
            if old is not current_leg and old.get("kind") == kind and old.get("status") not in TERMINAL:
                self._cancel(old, plan, True)
                if old.get('status') == 'closed':
                    raise RuntimeError('保护单在撤换时触发，等待退出核验')
        self._cancel_conflicting_exchange_triggers(plan, kind)

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
            if self._confirm_visible(leg, symbol, True, 'open'):
                self._save(plan)
                if leg["status"] != "open":
                    raise RuntimeError("Protection changed state during reconciliation")
                return leg
            # A missing response is not proof a previous write failed. Never blindly repeat it.
            raise RuntimeError(f"Protection submission unresolved: {leg['client_id']}")
        params = self._params(plan["side"], hedged, closing=True)
        params["clientOrderId"] = leg["client_id"]
        params["stopLossPrice" if kind == "sl" else "takeProfitPrice"] = plan["stop_loss" if kind == "sl" else "take_profit"]
        basis = plan.get('trigger_basis', 'last')
        if self.ex.id in {"binance", "binanceusdm"}:
            params['workingType'] = 'MARK_PRICE' if basis == 'mark' else 'CONTRACT_PRICE'
            params["closePosition"] = True
            amount = None
        else:
            params['slTriggerPxType' if kind == 'sl' else 'tpTriggerPxType'] = basis
            if not hedged:
                params["closeFraction"] = "1"
                amount = None
        leg["status"] = "submitting"
        self._save(plan)
        try:
            order = self.ex.create_order(symbol, "market", "sell" if plan["side"] == "LONG" else "buy", amount, None, params)
        except ccxt.ExchangeError as exc:
            if ("-4130" in str(exc) or "closePosition in the direction is existing" in str(exc)) and self.ex.id in {"binance", "binanceusdm"}:
                leg['status'] = 'rejected'  # Explicit rejection; no ambiguous submission to recover.
                self._save(plan)
                self._cleanup_conflicting_legs(plan, kind, leg)
                leg['status'] = 'submitting'
                self._save(plan)
                try:
                    order = self.ex.create_order(symbol, "market", "sell" if plan["side"] == "LONG" else "buy", amount, None, params)
                except ccxt.ExchangeError:
                    leg["status"] = "rejected"
                    self._save(plan)
                    raise
            else:
                # Explicit rejection is safe to retry with a fresh intent; timeouts are not.
                leg["status"] = "rejected"
                self._save(plan)
                raise
        if not order or not order.get("id"):
            raise RuntimeError("Protection submission outcome unknown")
        leg.update(id=str(order["id"]), status=order.get("status") or "open")
        self._save(plan)
        confirmed = self._confirm_visible(leg, symbol, True, 'open')
        if not confirmed or leg["status"] != "open":
            raise RuntimeError("Protection submitted but not yet visible; maintenance will keep querying it without resubmission")
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
                    plan['exit_reason'] = 'stop_loss' if leg['kind'] == 'sl' else 'take_profit'
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
            price = self._trigger_reference(plan)
            from backend.utils.execution_metrics import observe
            observe(self, plan, pos, price)
            sl_crossed = plan['stop_loss'] is not None and (
                price <= plan['stop_loss'] if plan['side'] == 'LONG' else price >= plan['stop_loss'])
            tp_crossed = plan['take_profit'] is not None and (
                price >= plan['take_profit'] if plan['side'] == 'LONG' else price <= plan['take_profit'])
            crossed = sl_crossed or tp_crossed
            if crossed:
                self._finish(plan, 'local_stop_loss' if sl_crossed else 'local_take_profit')
                return
            sl = tp = None
            if plan['stop_loss'] is not None:
                try:
                    sl = self._ensure_leg(plan, pos, "sl")
                except ccxt.ExchangeError:
                    if not any(x["kind"] == "sl" and x.get("status") == "open" for x in plan["legs"]):
                        self._finish(plan, 'protection_failure')
                    raise
            if plan['take_profit'] is not None:
                tp = self._ensure_leg(plan, pos, "tp")
            for old in plan["legs"]:
                if old is not sl and old is not tp and old.get("status") not in TERMINAL:
                    self._cancel(old, plan, True)
            plan["state"] = "ACTIVE"
            plan['verified_at'] = time.time()
            if unresolved_entries:
                plan["error"] = f"Entry unresolved: {unresolved_entries}"
            if any(e.get('amendment', {}).get('state') == 'pending' for e in plan['entries']):
                plan['error'] = 'Entry amendment unresolved; protection retained, no repeat amendments'
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
                if plan['stop_loss'] is not None and not confirmed_sl and plan['state'] != 'EXITING':
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
                        self._finish(plan, 'protection_failure')
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
