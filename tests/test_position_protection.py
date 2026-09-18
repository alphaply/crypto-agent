from types import SimpleNamespace
from unittest.mock import patch

import ccxt
import pytest

import backend.database as database
from backend.agent.agent_models import OpenOrderReal
from backend.utils.position_protection import PositionProtection


class Exchange:
    id = "binance"

    def __init__(self):
        self.orders = {}
        self.calls = []
        self.quantity = 0
        self.price = 100
        self.hedged = True
        self.fail_kind = None
        self.timeout_kind = None
        self.contract_size = 1
        self.visibility_delays = {}
        self.status_delays = {}

    def load_markets(self):
        pass

    def market(self, symbol):
        return dict(symbol="ETH/USDT:USDT", contract=True, swap=True, linear=True, contractSize=self.contract_size)

    def amount_to_precision(self, symbol, value):
        return str(round(value, 4))

    def price_to_precision(self, symbol, value):
        return str(round(value, 2))

    def fetch_position_mode(self, symbol):
        return {"hedged": self.hedged}

    def fetch_positions(self, symbols):
        return [dict(side="long", contracts=self.quantity, hedged=self.hedged)] if self.quantity else []

    def fetch_ticker(self, symbol):
        return {"last": self.price}

    def create_order(self, symbol, type, side, amount, price, params):
        kind = "sl" if "stopLossPrice" in params else "tp" if "takeProfitPrice" in params else "entry" if type == "limit" else "exit"
        self.calls.append(("create", kind, dict(params), amount))
        if self.fail_kind == kind:
            raise ccxt.InvalidOrder("rejected")
        oid = str(len(self.orders) + 1)
        order = dict(id=oid, status="open", filled=0, clientOrderId=params["clientOrderId"], kind=kind)
        if kind == "exit":
            order.update(status="closed", filled=self.quantity)
            self.quantity = 0
        self.orders[oid] = order
        if self.timeout_kind == kind:
            self.timeout_kind = None
            raise ccxt.RequestTimeout("response lost")
        return dict(order)

    def fetch_order(self, id, symbol, params):
        if id is not None and id in self.orders:
            kind = self.orders[id]['kind']
            if self.visibility_delays.get(kind, 0) > 0:
                self.visibility_delays[kind] -= 1
                raise ccxt.OrderNotFound("not visible yet")
            result = dict(self.orders[id])
            if self.status_delays.get(kind, 0) > 0:
                self.status_delays[kind] -= 1
                result['status'] = None
            return result
        cid = params.get("clientOrderId") or params.get("clientAlgoId")
        for order in self.orders.values():
            if order["clientOrderId"] == cid:
                return dict(order)
        raise ccxt.OrderNotFound("not found")

    def cancel_order(self, id, symbol, params):
        self.calls.append(("cancel", self.orders[id]["kind"]))
        self.orders[id]["status"] = "canceled"
        return dict(self.orders[id])


@pytest.fixture
def service(tmp_path):
    with patch.object(database, "DB_NAME", str(tmp_path / "test.db")):
        database.init_db()
        exchange = Exchange()
        yield PositionProtection(SimpleNamespace(exchange=exchange, config_id="test")), exchange


def entry(**kwargs):
    return OpenOrderReal(action="BUY_LIMIT", entry_price=100, amount=1, stop_loss=90, take_profit=120,
                         reason="test", **kwargs)


def test_pending_then_partial_fill_installs_full_position_protection(service):
    svc, ex = service
    result = svc.open("ETH/USDT", entry())
    assert result["protection_state"] == "WAITING"
    assert len(ex.calls) == 1
    ex.orders[result["id"]]["filled"] = .3
    ex.quantity = .3
    assert svc.reconcile_all()[0]["state"] == "ACTIVE"
    assert [c[1] for c in ex.calls] == ["entry", "sl", "tp"]
    assert all(c[2]["closePosition"] and c[3] is None for c in ex.calls[1:])
    ex.quantity = 1
    svc.reconcile_all()
    assert len(ex.calls) == 3  # closePosition covers incremental fills, no duplicate legs.


def test_add_inherits_and_partial_update_changes_whole_position(service):
    svc, ex = service
    first = svc.open('ETH/USDT', entry())
    ex.orders[first['id']].update(status='closed', filled=1)
    ex.quantity = 1
    svc.reconcile_all()
    add = OpenOrderReal(action='BUY_LIMIT', entry_price=100, amount=.5, reason='add')
    result = svc.open('ETH/USDT', add)
    assert (result['stop_loss'], result['take_profit']) == (90, 120)
    assert [c[1] for c in ex.calls].count('sl') == 1
    add.take_profit = 125
    result = svc.open('ETH/USDT', add)
    assert (result['stop_loss'], result['take_profit']) == (90, 125)
    assert [c[1] for c in ex.calls if c[0] == 'create'][-2:] == ['tp', 'entry']


def test_failed_protection_update_does_not_add_exposure(service):
    svc, ex = service
    ex.quantity = 1
    svc.open('ETH/USDT', entry())
    ex.fail_kind = 'tp'
    count = len([c for c in ex.calls if c[:2] == ('create', 'entry')])
    add = OpenOrderReal(action='BUY_LIMIT', entry_price=100, amount=1, take_profit=125, reason='add')
    with pytest.raises(ValueError, match='no additional entry'):
        svc.open('ETH/USDT', add)
    assert len([c for c in ex.calls if c[:2] == ('create', 'entry')]) == count


def test_stale_revision_and_conflicting_clear_are_rejected(service):
    svc, ex = service
    svc.open('ETH/USDT', entry())
    with pytest.raises(RuntimeError, match='刷新'):
        svc.adjust('ETH/USDT', 'LONG', sl=95, expected_revision=0)
    with pytest.raises(ValueError, match='同时'):
        svc.adjust('ETH/USDT', 'LONG', sl=95, clear_sl=True)


def test_add_does_not_change_opposite_waiting_plan(service):
    svc, ex = service
    svc.open('ETH/USDT', entry())
    svc.open('ETH/USDT', OpenOrderReal(action='SELL_LIMIT', entry_price=100, amount=1,
                                     stop_loss=110, take_profit=80, reason='short'))
    svc.open('ETH/USDT', OpenOrderReal(action='BUY_LIMIT', entry_price=100, amount=.2, reason='add long'))
    assert svc._load('ETH/USDT:USDT', 'SHORT')['take_profit'] == 80
    assert svc._load('ETH/USDT:USDT', 'LONG')['take_profit'] == 120


def test_zero_rounded_add_does_not_modify_protection(service):
    svc, ex = service
    svc.open('ETH/USDT', entry())
    with pytest.raises(ValueError, match='rounds to zero'):
        svc.open('ETH/USDT', OpenOrderReal(action='BUY_LIMIT', entry_price=100, amount=.0000001,
                                         take_profit=125, reason='too small'))
    assert svc._load('ETH/USDT:USDT', 'LONG')['take_profit'] == 120


def test_conflict_cancel_timeout_preserves_old_leg_and_stops_retry(service):
    svc, ex = service
    ex.quantity = 1
    svc.open('ETH/USDT', entry())
    original = ex.create_order
    def conflict(*args):
        if 'takeProfitPrice' in args[-1]:
            raise ccxt.ExchangeError('-4130 closePosition in the direction is existing')
        return original(*args)
    ex.create_order = conflict
    def timeout(*args, **kwargs):
        raise ccxt.RequestTimeout('cancel timeout')
    ex.cancel_order = timeout
    with patch('backend.utils.position_protection.time.sleep'), pytest.raises(RuntimeError, match='Cancellation not confirmed'):
        svc.adjust('ETH/USDT', 'LONG', tp=125)
    plan = svc._load('ETH/USDT:USDT', 'LONG')
    assert any(l['kind'] == 'tp' and l['trigger_price'] == 120 and l['status'] == 'open' for l in plan['legs'])
    assert plan['error']
    assert not any(l['kind'] == 'tp' and l['trigger_price'] == 125 and l['status'] == 'submitting' for l in plan['legs'])


@pytest.mark.parametrize('lost_ack', [False, True])
@pytest.mark.parametrize('stale_reads', [3, 8])
def test_adjust_waits_for_cancel_visibility_in_one_call(service, lost_ack, stale_reads):
    svc, ex = service
    ex.quantity = 1
    svc.open('ETH/USDT', entry())
    cancel, query = ex.cancel_order, ex.fetch_order
    delayed = {}
    def delayed_cancel(oid, symbol, params):
        result = cancel(oid, symbol, params)
        delayed[oid] = stale_reads
        if lost_ack:
            raise ccxt.RequestTimeout('ACK lost after successful cancel')
        return result
    def delayed_query(oid, symbol, params):
        result = query(oid, symbol, params)
        if delayed.get(oid, 0):
            delayed[oid] -= 1
            result['status'] = 'open'
        return result
    ex.cancel_order, ex.fetch_order = delayed_cancel, delayed_query
    with patch('backend.utils.position_protection.time.sleep'):
        plan = svc.adjust('ETH/USDT', 'LONG', sl=95, tp=125)
    assert plan['state'] == 'ACTIVE' and not plan['error']
    assert len([c for c in ex.calls if c[0] == 'cancel']) == 2
    assert len([l for l in plan['legs'] if l['status'] == 'open']) == 2


def test_binance_algo_cancel_ack_completes_replacement_despite_stale_reads(service):
    svc, ex = service
    ex.quantity = 1
    svc.open('ETH/USDT', entry())
    old_sl = next(dict(o) for o in ex.orders.values() if o['kind'] == 'sl')
    original_create, original_cancel, original_query = ex.create_order, ex.cancel_order, ex.fetch_order

    def create(*args):
        if 'stopLossPrice' in args[-1] and ex.orders[old_sl['id']]['status'] == 'open':
            raise ccxt.ExchangeError('-4130 closePosition in the direction is existing')
        return original_create(*args)

    def cancel(oid, symbol, params):
        original_cancel(oid, symbol, params)
        # Exercise the installed CCXT parser with Binance's documented response.
        return ccxt.binanceusdm().parse_order({
            'algoId': int(oid), 'clientAlgoId': old_sl['clientOrderId'],
            'code': '200', 'msg': 'success',
        })

    def query(oid, symbol, params):
        if oid == old_sl['id']:
            return dict(old_sl)  # GET continues returning stale NEW indefinitely.
        return original_query(oid, symbol, params)

    ex.create_order, ex.cancel_order, ex.fetch_order = create, cancel, query
    ex.fetch_open_orders = lambda *args, **kwargs: [{
        **old_sl, 'side': 'sell',
        'info': {'positionSide': 'LONG', 'closePosition': True, 'orderType': 'STOP_MARKET'},
    }]
    with patch('backend.utils.position_protection.time.sleep') as sleep:
        plan = svc.adjust('ETH/USDT', 'LONG', sl=95)
    assert plan['state'] == 'ACTIVE' and plan['error'] is None
    assert len([c for c in ex.calls if c[0] == 'cancel']) == 1
    assert next(l for l in plan['legs'] if l.get('id') == old_sl['id'])['status'] == 'canceled'
    assert len([l for l in plan['legs'] if l['status'] == 'open']) == 2
    sleep.assert_not_called()


@pytest.mark.parametrize('response', [
    {'id': 'wrong-order', 'status': 'canceled'},
    {'info': {'code': '200', 'msg': 'success'}},
    {'id': '2', 'info': {'algoId': 2, 'code': '-2011'}},
])
def test_cancel_does_not_accept_unverified_ack(service, response):
    svc, ex = service
    ex.quantity = 1
    svc.open('ETH/USDT', entry())
    plan = svc._load('ETH/USDT:USDT', 'LONG')
    leg = next(l for l in plan['legs'] if l['kind'] == 'sl')
    ex.cancel_order = lambda *args, **kwargs: response
    with patch('backend.utils.position_protection.time.sleep'), pytest.raises(RuntimeError, match='Cancellation not confirmed'):
        svc._cancel(leg, plan, True)
    assert leg['status'] == 'open'


@pytest.mark.parametrize(('kwargs', 'expected_kinds'), [
    ({'stop_loss': 90}, ['entry', 'sl']),
    ({'take_profit': 120}, ['entry', 'tp']),
    ({}, ['entry']),
])
def test_real_entry_protection_is_optional(service, kwargs, expected_kinds):
    svc, ex = service
    order = OpenOrderReal(action='BUY_LIMIT', entry_price=100, amount=1, reason='optional protection', **kwargs)
    result = svc.open('ETH/USDT', order)
    ex.orders[result['id']]['filled'] = 1
    ex.quantity = 1
    plan = svc.reconcile_all()[0]
    assert plan['state'] == 'ACTIVE' and plan['error'] is None
    assert [call[1] for call in ex.calls] == expected_kinds


def test_optional_protection_still_validates_each_side():
    with pytest.raises(ValueError, match='止损'):
        OpenOrderReal(action='BUY_LIMIT', entry_price=100, amount=1, stop_loss=101, reason='bad')
    with pytest.raises(ValueError, match='止盈'):
        OpenOrderReal(action='SELL_LIMIT', entry_price=100, amount=1, take_profit=101, reason='bad')


def test_restart_reconciles_entry_timeout_without_duplicate(service):
    svc, ex = service
    ex.timeout_kind = "entry"
    with pytest.raises(ccxt.RequestTimeout):
        svc.open("ETH/USDT", entry())
    ex.quantity = 1
    restarted = PositionProtection(svc.mt)
    assert restarted.reconcile_all()[0]["state"] == "ACTIVE"
    assert [x[1] for x in ex.calls].count("entry") == 1


def test_protection_timeout_is_queried_and_not_submitted_twice(service):
    svc, ex = service
    ex.quantity = 1
    ex.timeout_kind = "sl"
    result = svc.open("ETH/USDT", entry())
    assert result["protection_error"]
    assert svc.reconcile_all()[0]["state"] == "ACTIVE"
    assert [x[1] for x in ex.calls].count("sl") == 1


def test_protection_confirmation_retries_exchange_visibility_lag_without_resubmit(service, monkeypatch):
    svc, ex = service
    ex.quantity = 1
    ex.visibility_delays = {'sl': 2, 'tp': 1}
    ex.status_delays = {'sl': 1, 'tp': 2}
    monkeypatch.setattr('backend.utils.position_protection.time.sleep', lambda _: None)
    result = svc.open('ETH/USDT', entry())
    assert result['protection_state'] == 'ACTIVE'
    assert result['protection_error'] is None
    assert [call[1] for call in ex.calls].count('sl') == 1
    assert [call[1] for call in ex.calls].count('tp') == 1


def test_adjust_installs_before_cancelling_previous_orders(service):
    svc, ex = service
    ex.quantity = 1
    svc.open("ETH/USDT", entry())
    ex.calls.clear()
    plan = svc.adjust("ETH/USDT", "LONG", sl=95)
    assert plan["stop_loss"] == 95 and plan["take_profit"] == 120
    assert [x[:2] for x in ex.calls] == [("create", "sl"), ("cancel", "sl")]


def test_rejected_replacement_retains_old_stop(service):
    svc, ex = service
    ex.quantity = 1
    svc.open("ETH/USDT", entry())
    ex.calls.clear()
    ex.fail_kind = "sl"
    with pytest.raises(ccxt.InvalidOrder):
        svc.adjust("ETH/USDT", "LONG", sl=95)
    assert not any(x[0] == "cancel" for x in ex.calls)
    assert any(x["kind"] == "sl" and x["status"] == "open" for x in ex.orders.values())


def test_stop_failure_cancels_remaining_entry_and_exits(service):
    svc, ex = service
    ex.quantity = .5
    ex.fail_kind = "sl"
    svc.open("ETH/USDT", entry())
    assert ex.quantity == 0
    assert ex.orders["1"]["status"] == "canceled"
    assert svc.reconcile_all()[0]["state"] == "DONE"


def test_tp_completion_cancels_pending_entry_and_sibling(service):
    svc, ex = service
    ex.quantity = .5
    svc.open("ETH/USDT", entry())
    next(x for x in ex.orders.values() if x["kind"] == "tp")["status"] = "closed"
    ex.quantity = 0
    assert svc.reconcile_all()[0]["state"] == "DONE"
    assert ex.orders["1"]["status"] == "canceled"
    assert next(x for x in ex.orders.values() if x["kind"] == "sl")["status"] == "canceled"


def test_crossed_stop_during_fill_detection_exits_instead_of_invalid_trigger(service):
    svc, ex = service
    svc.open("ETH/USDT", entry())
    ex.quantity = .5
    ex.price = 85
    svc.reconcile_all()
    assert ex.quantity == 0
    assert "sl" not in [x[1] for x in ex.calls]


def test_okx_contract_units_and_hedge_protection(service):
    svc, ex = service
    ex.id = "okx"
    ex.contract_size = .1
    ex.quantity = 10
    svc.open("ETH/USDT", entry())
    assert ex.calls[0][3] == 10
    for call in ex.calls[1:]:
        assert call[2]["hedged"] and call[2]["reduceOnly"]
        assert call[3] == 10


def test_okx_net_mode_uses_close_fraction(service):
    svc, ex = service
    ex.id = "okx"
    ex.hedged = False
    ex.quantity = 1
    svc.open("ETH/USDT", entry())
    assert ex.calls[1][2]["closeFraction"] == "1"


def test_invalid_prices_never_submit(service):
    svc, ex = service
    with pytest.raises(ValueError):
        OpenOrderReal(action="BUY_LIMIT", entry_price=100, amount=1, stop_loss=110, take_profit=120, reason="bad")
    assert ex.calls == []


def test_config_isolation(service):
    svc, ex = service
    svc.open("ETH/USDT", entry())
    other = PositionProtection(SimpleNamespace(exchange=ex, config_id="other"))
    assert other.reconcile_all() == []


def test_actual_binanceusdm_adapter_is_supported(service):
    svc, ex = service
    ex.id = 'binanceusdm'
    ex.quantity = 1
    result = svc.open('ETH/USDT', entry())
    assert result['protection_state'] == 'ACTIVE'
    assert ex.calls[1][2]['closePosition'] is True


def test_concurrent_stale_plan_cannot_overwrite_newer_state(service):
    svc, ex = service
    svc.open('ETH/USDT', entry())
    stale = svc._load('ETH/USDT:USDT', 'LONG')
    svc.adjust('ETH/USDT', 'LONG', sl=95)
    with pytest.raises(RuntimeError, match='concurrently'):
        svc._save(stale)
    assert svc._load('ETH/USDT:USDT', 'LONG')['stop_loss'] == 95


def test_same_account_symbol_cannot_be_owned_by_two_configs(service):
    svc, ex = service
    svc.open('ETH/USDT', entry())
    other = PositionProtection(SimpleNamespace(exchange=ex, config_id='other'))
    other.account_scope = svc.account_scope
    calls = len(ex.calls)
    with pytest.raises(ValueError, match='Another config'):
        other.open('ETH/USDT', entry())
    assert len(ex.calls) == calls


@pytest.mark.parametrize('exchange_class', [ccxt.binanceusdm, ccxt.okx])
@pytest.mark.parametrize('hedged', [True, False])
def test_ccxt_native_request_contains_correct_close_only_semantics(exchange_class, hedged):
    ex = exchange_class()
    market = dict(id='ETHUSDT' if ex.id == 'binanceusdm' else 'ETH-USDT-SWAP', symbol='ETH/USDT:USDT',
                  base='ETH', quote='USDT', settle='USDT', baseId='ETH', quoteId='USDT', settleId='USDT',
                  type='swap', spot=False, margin=False, swap=True, future=False, option=False,
                  contract=True, linear=True, inverse=False, active=True, contractSize=1,
                  info={'orderTypes': ['LIMIT', 'MARKET', 'STOP_MARKET', 'TAKE_PROFIT_MARKET']},
                  precision={'price': .01, 'amount': .001}, limits={'amount': {'min': .001, 'max': None}})
    ex.set_markets([market])
    for key in ('stopLossPrice', 'takeProfitPrice'):
        params = {'clientOrderId': 'cap123', key: 90 if key == 'stopLossPrice' else 120}
        if ex.id == 'binanceusdm':
            params.update(closePosition=True, positionSide='LONG' if hedged else 'BOTH')
            amount = None
        else:
            params.update(hedged=hedged, reduceOnly=True)
            amount = 1 if hedged else None
            if not hedged:
                params['closeFraction'] = '1'
        request = ex.create_order_request(market['symbol'], 'market', 'sell', amount, None, params)
        if ex.id == 'binanceusdm':
            assert request['closePosition'] is True
            assert 'quantity' not in request and 'reduceOnly' not in request
            assert request['type'] == ('STOP_MARKET' if key == 'stopLossPrice' else 'TAKE_PROFIT_MARKET')
            assert request['clientAlgoId'] == 'cap123'
        elif hedged:
            assert request['posSide'] == 'long'
            assert 'reduceOnly' not in request
            assert request['sz'] == '1'
        else:
            assert request['closeFraction'] == '1' and request['reduceOnly'] is True
            assert 'sz' not in request
    # Exercise the installed adapters' actual native amendment wire format.
    if ex.id == 'binanceusdm':
        request = ex.edit_contract_order_request('123', market['symbol'], 'limit', 'buy', .5, 101)
        assert str(request['orderId']) == '123'
        assert request['quantity'] == '0.5' and request['price'] == '101'
        assert request['side'] == 'BUY'
    else:
        request = ex.edit_order_request('123', market['symbol'], 'limit', 'buy', .5, 101)
        assert request['ordId'] == '123'
        assert request['newSz'] == '0.5' and request['newPx'] == '101'


def test_binance_4130_auto_conflict_resolution(service):
    svc, ex = service
    ex.quantity = 1
    svc.open("ETH/USDT", entry())
    ex.calls.clear()

    orig_create = ex.create_order
    def simulated_create_order(symbol, type, side, amount, price, params):
        kind = "sl" if "stopLossPrice" in params else "tp" if "takeProfitPrice" in params else "entry" if type == "limit" else "exit"
        # Simulate Binance -4130 if another open closePosition order of the same kind exists
        if params.get("closePosition"):
            has_existing = any(
                o["status"] == "open" and o.get("kind") == kind
                for o in ex.orders.values()
            )
            if has_existing:
                raise ccxt.ExchangeError(
                    'binanceusdm {"code":-4130,"msg":"An open stop or take profit order with GTE and closePosition in the direction is existing."}'
                )
        return orig_create(symbol, type, side, amount, price, params)

    ex.create_order = simulated_create_order

    # Agent updates TP from 120 to 115, leaving SL (90) unchanged
    plan = svc.adjust("ETH/USDT", "LONG", tp=115)

    assert plan["take_profit"] == 115
    assert plan["stop_loss"] == 90
    assert plan["state"] == "ACTIVE"
    assert plan["error"] is None

    # SL must remain open and untouched
    sl_orders = [o for o in ex.orders.values() if o["kind"] == "sl"]
    assert len(sl_orders) == 1
    assert sl_orders[0]["status"] == "open"

    # Old TP must be canceled and new TP open
    tp_orders = [o for o in ex.orders.values() if o["kind"] == "tp"]
    assert any(o["status"] == "canceled" for o in tp_orders)
    assert any(o["status"] == "open" for o in tp_orders)
