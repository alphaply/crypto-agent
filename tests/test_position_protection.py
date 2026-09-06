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
            return dict(self.orders[id])
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
