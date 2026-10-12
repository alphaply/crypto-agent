"""Intercept the installed CCXT adapter's real requests; never use the network."""
from types import SimpleNamespace

import ccxt
import pytest

from backend.utils.independent_exits import IndependentExits


@pytest.fixture
def adapter():
    exchange = ccxt.binanceusdm()
    market = dict(id='ETHUSDT', symbol='ETH/USDT:USDT', base='ETH', quote='USDT', settle='USDT',
                  baseId='ETH', quoteId='USDT', settleId='USDT', type='swap', spot=False, margin=False,
                  swap=True, future=False, option=False, contract=True, linear=True, inverse=False,
                  active=True, contractSize=1, precision={'price': .01, 'amount': .001},
                  limits={'amount': {'min': .001, 'max': None}},
                  info={'orderTypes': ['LIMIT', 'MARKET', 'STOP', 'STOP_MARKET', 'TAKE_PROFIT', 'TAKE_PROFIT_MARKET']})
    exchange.set_markets([market])
    calls = []

    def request(path, api='public', method='GET', params=None, *args, **kwargs):
        params = dict(params or {})
        calls.append((path, api, method, params))
        if 'algo' in path.lower():
            return dict(algoId=params.get('algoId', '42'), clientAlgoId=params.get('clientAlgoId'),
                        algoStatus='NEW', symbol='ETHUSDT', orderType=params.get('type', 'STOP_MARKET'),
                        quantity=params.get('quantity'), price=params.get('price'), triggerPrice=params.get('triggerPrice'),
                        positionSide=params.get('positionSide', 'LONG'), actualOrderId='0')
        return dict(orderId=params.get('orderId', '42'), symbol='ETHUSDT', status='NEW',
                    type=params.get('type', 'LIMIT'), side=params.get('side', 'SELL'),
                    origQty=params.get('quantity', '1'), executedQty='0', price=params.get('price', '110'))

    exchange.request = request
    return exchange, calls


@pytest.mark.parametrize('kind,native', [('market', 'MARKET'), ('limit', 'LIMIT'), ('stop_market', 'STOP_MARKET'),
                                       ('stop_limit', 'STOP'), ('take_profit_market', 'TAKE_PROFIT_MARKET'),
                                       ('take_profit_limit', 'TAKE_PROFIT')])
@pytest.mark.parametrize('hedged', [False, True])
@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_six_fixed_types_and_directions_have_correct_wire_channels(adapter, kind, native, hedged, side):
    exchange, calls = adapter
    exchange.fetch_position_mode = lambda _: {'hedged': hedged}
    service = IndependentExits(SimpleNamespace(exchange=exchange, config_id='wire-test'))
    record = dict(id=None, order_type=kind, contract_version=2, quantity_mode='fixed',
                  amount=.5, price=105 if 'limit' in kind else None, trigger_price=100 if kind not in {'limit', 'market'} else None,
                  client_id='independent-v2-fixed', trigger_basis='last')
    plan = dict(symbol='ETH/USDT:USDT', side=side, trigger_basis='last')
    service._submit = lambda plan, record, kind, side, params: exchange.create_order(
        plan['symbol'], kind, side, record['amount'], record['price'], params)
    service._submit_exit(plan, record, {'hedged': hedged})
    path, api, method, payload = calls[-1]
    conditional = kind not in {'market', 'limit'}
    assert path == ('algoOrder' if conditional else 'order') and method == 'POST'
    assert payload['type'] == native
    assert payload['side'] == ('SELL' if side == 'LONG' else 'BUY')
    assert payload['quantity'] == '0.5'
    assert payload['positionSide'] == (side if hedged else 'BOTH')
    assert ('reduceOnly' not in payload) if hedged else payload['reduceOnly'] is True
    assert 'closePosition' not in payload
    if conditional:
        assert payload['algoType'] == 'CONDITIONAL'
        assert payload['workingType'] == 'CONTRACT_PRICE' and payload['triggerPrice'] == '100'
        assert payload['clientAlgoId'] == record['client_id']
    if 'limit' in kind:
        assert payload['price'] == '105'


@pytest.mark.parametrize('kind', ['stop_market', 'take_profit_market'])
@pytest.mark.parametrize('hedged', [False, True])
@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_native_all_omits_quantity_and_reduce_only(adapter, kind, hedged, side):
    exchange, calls = adapter
    exchange.fetch_position_mode = lambda _: {'hedged': hedged}
    service = IndependentExits(SimpleNamespace(exchange=exchange, config_id='wire-test'))
    record = dict(order_type=kind, contract_version=2, quantity_mode='all', amount=None, price=None,
                  trigger_price=100, client_id='independent-v2-all', trigger_basis='last')
    plan = dict(symbol='ETH/USDT:USDT', side=side)
    service._submit = lambda plan, record, kind, side, params: exchange.create_order(
        plan['symbol'], kind, side, record['amount'], record['price'], params)
    service._submit_exit(plan, record, {'hedged': hedged})
    payload = calls[-1][-1]
    assert calls[-1][0] == 'algoOrder' and payload['closePosition'] is True
    assert 'quantity' not in payload and 'reduceOnly' not in payload
    assert payload['positionSide'] == (side if hedged else 'BOTH')


def test_queries_cancellation_and_native_edit_use_exact_channels(adapter):
    exchange, calls = adapter
    symbol = 'ETH/USDT:USDT'
    exchange.fetch_order('42', symbol, {'trigger': True})
    assert calls[-1][0:3:2] == ('algoOrder', 'GET') and calls[-1][-1]['algoId'] == '42'
    exchange.fetch_order(None, symbol, {'trigger': True, 'clientAlgoId': 'same-client'})
    assert calls[-1][-1]['clientAlgoId'] == 'same-client'
    exchange.cancel_order('42', symbol, {'trigger': True})
    assert calls[-1][0:3:2] == ('algoOrder', 'DELETE')
    exchange.fetch_order('42', symbol)
    assert calls[-1][0:3:2] == ('order', 'GET') and calls[-1][-1]['orderId'] == '42'
    exchange.cancel_order('42', symbol)
    assert calls[-1][0:3:2] == ('order', 'DELETE')
    exchange.edit_order('42', symbol, 'limit', 'sell', .7, 120)
    assert calls[-1][0:3:2] == ('order', 'PUT')
    assert calls[-1][-1]['quantity'] == '0.7' and calls[-1][-1]['price'] == '120'


def test_cancel_parent_child_share_one_verification_budget(adapter):
    import time
    exchange, calls = adapter
    request = exchange.request
    deletes, timed_reads = [], []
    def wire(path, api='public', method='GET', params=None, *args, **kwargs):
        if method == 'DELETE':
            deletes.append(path)
            if path == 'algoOrder':
                calls.append((path, api, method, dict(params or {})))
                return dict(algoId='42', algoStatus='FINISHED', actualOrderId='42', symbol='ETHUSDT')
            calls.append((path, api, method, dict(params or {})))
            return dict(orderId='42', status='CANCELED', symbol='ETHUSDT')
        if deletes and method == 'GET':
            timed_reads.append(exchange.timeout)
        return request(path, api, method, params, *args, **kwargs)
    exchange.request = wire
    original_timeout = exchange.timeout
    service = IndependentExits(SimpleNamespace(exchange=exchange, config_id='budget'))
    service._save = lambda _: None
    record = dict(id='42', order_type='stop_limit', exit_type='stop_limit', contract_version=2,
                  status='open', filled=0, amount=1, client_id='synthetic-parent', fill_verified=True)
    plan = dict(symbol='ETH/USDT:USDT', side='LONG', exits=[record])
    started = time.monotonic()
    with pytest.raises(RuntimeError, match='cumulative fill'):
        service._cancel(record, plan)
    assert time.monotonic() - started < 2
    assert 1 <= len(timed_reads) <= 3 and all(0 < value <= 2000 for value in timed_reads)
    assert exchange.timeout == original_timeout
    assert deletes == ['algoOrder', 'order']
    assert not any(row[2] in {'POST', 'PUT'} for row in calls)
    assert record['fill_pending'] and record['status'] == 'canceled'


def test_old_waiting_parent_cannot_erase_terminal_unverified_fill(adapter):
    exchange, _ = adapter
    fetch = exchange.fetch_order
    exchange.fetch_order = lambda *a, **k: {**fetch(*a, **k), 'filled': None}
    service = IndependentExits(SimpleNamespace(exchange=exchange, config_id='stale-parent'))
    record = dict(id='42', order_type='stop_market', contract_version=2, status='closed',
                  filled=.2, fill_verified=True, fill_pending=True, amount=1, client_id='synthetic-parent')
    with pytest.raises(RuntimeError, match='terminal order response'):
        service._query(record, 'ETH/USDT:USDT')
    assert record['status'] == 'closed' and record['fill_pending']
    assert record['filled'] == .2
