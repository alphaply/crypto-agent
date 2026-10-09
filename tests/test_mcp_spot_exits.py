import json
from copy import deepcopy
from types import SimpleNamespace

import ccxt
import pytest

from backend import database
from backend.database_schema import initialize_schema
from backend.mcp import spot_orders as exits


class FakeSpotExchange:
    def __init__(self, name='binance'):
        self.id, self.apiKey = name, 'owned-test-account'
        self.orders = {'buy-1': {'id': 'buy-1', 'symbol': 'BTC/USDT', 'side': 'buy',
                                 'status': 'closed', 'amount': 10, 'filled': 10}}
        self.trades = [{'id': 'fill-buy', 'order': 'buy-1', 'symbol': 'BTC/USDT', 'side': 'buy',
                        'amount': 10, 'fee': {'cost': 0.01, 'currency': 'BTC'}}]
        self.groups = {}
        self.algos = {}
        self.writes = []
        self.error = None
        self.balance_extra = 0
        self.extra_used = 0
        self.lookup_failure = False
        self.market_info = {'orderTypes': ['MARKET', 'LIMIT', 'STOP_LOSS', 'STOP_LOSS_LIMIT',
                                          'TAKE_PROFIT', 'TAKE_PROFIT_LIMIT'], 'ocoAllowed': True}

    def market(self, symbol):
        return {'id': 'BTCUSDT' if self.id == 'binance' else 'BTC-USDT', 'symbol': symbol,
                'base': 'BTC', 'quote': 'USDT', 'spot': True, 'contract': False, 'info': self.market_info}

    def amount_to_precision(self, symbol, amount):
        return str(amount)

    def price_to_precision(self, symbol, price):
        return str(price)

    def fetch_ticker(self, symbol):
        return {'last': 100}

    def fetch_order(self, order_id, symbol, params=None):
        if self.lookup_failure and order_id != 'buy-1':
            raise ccxt.NetworkError('unavailable')
        if params and params.get('clientOrderId'):
            return deepcopy(next(order for order in self.orders.values() if order.get('clientOrderId') == params['clientOrderId']))
        return deepcopy(self.orders[order_id])

    def fetch_my_trades(self, symbol, since=None, limit=1000):
        return deepcopy(self.trades)

    def fetch_balance(self):
        filled = sum(order['filled'] for order in self.orders.values() if order['side'] == 'sell')
        total = 9.99 - filled + self.balance_extra
        used = self.extra_used
        groups_seen = set()
        for order in self.orders.values():
            if order['side'] != 'sell' or order['status'] != 'open':
                continue
            group = order.get('group')
            if group and group in groups_seen:
                continue
            if group:
                groups_seen.add(group)
            used += order['amount'] - order['filled']
        return {'BTC': {'total': total, 'free': total - used, 'used': used}}

    def _new_order(self, amount, client_id, group=None):
        order_id = 'sell-' + str(len(self.orders))
        self.orders[order_id] = {'id': order_id, 'symbol': 'BTC/USDT', 'side': 'sell', 'status': 'open',
                                 'amount': float(amount), 'filled': 0, 'clientOrderId': client_id, 'group': group}
        return self.orders[order_id]

    def create_order(self, symbol, kind, side, amount, price, params):
        self.writes.append(('create', kind, side, amount, price, deepcopy(params)))
        if isinstance(self.error, ccxt.InvalidOrder):
            raise self.error
        result = self._new_order(amount, params['clientOrderId'])
        if self.error:
            raise self.error
        return deepcopy(result)

    def cancel_order(self, order_id, symbol):
        self.writes.append(('cancel', order_id))
        self.orders[order_id]['status'] = 'canceled'

    def privatePostOrderListOco(self, request):
        self.writes.append(('oco', deepcopy(request)))
        group_id = str(len(self.groups) + 1)
        a = self._new_order(request['quantity'], request['aboveClientOrderId'], group_id)
        b = self._new_order(request['quantity'], request['belowClientOrderId'], group_id)
        result = {'orderListId': group_id, 'listClientOrderId': request['listClientOrderId'], 'symbol': 'BTCUSDT',
                  'listOrderStatus': 'EXECUTING', 'orders': [{'orderId': a['id']}, {'orderId': b['id']}]}
        self.groups[group_id] = result
        if self.error:
            raise self.error
        return deepcopy(result)

    def privateGetOrderList(self, request):
        if self.lookup_failure:
            raise ccxt.NetworkError('unavailable')
        return deepcopy(self.groups[request['orderListId']] if request.get('orderListId') else
                        next(group for group in self.groups.values() if group['listClientOrderId'] == request['origClientOrderId']))

    def privateDeleteOrderList(self, request):
        self.writes.append(('cancel_oco', deepcopy(request)))
        group = self.groups[request['orderListId']]
        group['listOrderStatus'] = 'ALL_DONE'
        for order in group['orders']:
            self.orders[order['orderId']]['status'] = 'canceled'
        return deepcopy(group)

    def privatePostTradeOrderAlgo(self, request):
        self.writes.append(('algo', deepcopy(request)))
        identity = str(len(self.algos) + 1)
        self.algos[identity] = {'algoId': identity, 'algoClOrdId': request['algoClOrdId'], 'instId': 'BTC-USDT',
                                'side': 'sell', 'sz': request['sz'], 'state': 'live', 'ordIdList': [], 'actualSz': '0'}
        return {'code': '0', 'data': [{'sCode': '0', 'algoId': identity}]}

    def privateGetTradeOrderAlgo(self, request):
        return {'code': '0', 'data': [deepcopy(self.algos[request['algoId']] if request.get('algoId') else
                                               next(row for row in self.algos.values() if row['algoClOrdId'] == request['algoClOrdId']))]}

    def privatePostTradeCancelAlgos(self, request):
        self.writes.append(('cancel_algo', deepcopy(request)))
        self.algos[request[0]['algoId']]['state'] = 'canceled'
        return {'code': '0', 'data': [{'sCode': '0', 'algoId': request[0]['algoId']}]}

    def fill(self, order_id, amount):
        order = self.orders[order_id]
        order['filled'] = amount
        if amount == order['amount']:
            order['status'] = 'closed'
        self.trades.append({'id': 'fill-' + order_id, 'order': order_id, 'symbol': 'BTC/USDT', 'side': 'sell',
                            'amount': amount, 'fee': {'cost': 0.01, 'currency': 'USDT'}})


@pytest.fixture
def spot(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'exits.db'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    database.save_order_log('buy-1', 'BTC/USDT', 'MCP', 'buy', 100, 0, 0, 'owned buy',
                            trade_mode='SPOT_DCA', config_id='mcp:test', amount=10, status='FILLED')
    exchange = FakeSpotExchange()
    monkeypatch.setattr(exits, '_tool', lambda config_id, symbol: (SimpleNamespace(config_id=config_id, exchange=exchange), exchange.market(symbol)))
    return exchange


def create(args=None, operation='exit-operation'):
    return exits.execute('create_spot_exit', {'amount': 3, 'exit_type': 'limit', 'price': 110, 'reason': 'test', **(args or {})},
                         'mcp:test', 'BTC/USDT', operation)


def test_owned_inventory_deducts_base_fees_and_reserves_live_sells(spot):
    assert exits.inventory('mcp:test', 'BTC/USDT')['available_quantity'] == pytest.approx(9.99)
    row = create()
    assert row['status'] == 'open' and len(spot.writes) == 1
    stock = exits.inventory('mcp:test', 'BTC/USDT')
    assert stock['owned_quantity'] == pytest.approx(9.99)
    assert stock['reserved_quantity'] == 3
    assert stock['available_quantity'] == pytest.approx(6.99)
    with pytest.raises(ValueError, match='exceeds'):
        create({'amount': 7}, 'oversell-operation')
    assert len(spot.writes) == 1


@pytest.mark.parametrize('change', ['manual_balance', 'withdrawal', 'foreign_fill', 'foreign_lock', 'missing_fee', 'incomplete_fills'])
def test_unproven_or_shared_inventory_cannot_be_sold(spot, change):
    if change == 'manual_balance':
        spot.balance_extra = 1
    elif change == 'withdrawal':
        spot.balance_extra = -1
    elif change == 'foreign_fill':
        spot.trades.append({**spot.trades[0], 'id': 'foreign', 'order': 'manual'})
    elif change == 'foreign_lock':
        spot.extra_used = 1
    elif change == 'missing_fee':
        spot.trades[0]['fee'] = None
    else:
        spot.trades[0]['amount'] = 9
    with pytest.raises(ValueError):
        create()
    assert spot.writes == []


@pytest.mark.parametrize('evidence', ['buy', 'unknown_budget'])
def test_cross_quote_foreign_profile_evidence_blocks_even_equal_balance(spot, monkeypatch, evidence):
    if evidence == 'buy':
        database.save_order_log('foreign-buy', 'BTC/USDC', 'MCP', 'buy', 100, 0, 0, 'foreign buy',
                                trade_mode='SPOT_DCA', config_id='mcp:foreign', amount=1, status='FILLED')
    else:
        from backend.utils.spot_execution import _initialize
        with database.get_db_conn() as conn:
            _initialize(conn)
            conn.execute('''INSERT INTO spot_budget_reservations
                (config_id,operation_id,cycle_id,symbol,quote_cost,status,created_at,updated_at)
                VALUES('mcp:foreign','buy-operation','cycle','BTC/USDC',100,'unknown',0,0)''')
            conn.commit()
    monkeypatch.setattr(exits, '_configured_account_scope', lambda config_id: exits.account_scope(spot, config_id))
    # The visible BTC/USDT trades and current balance still exactly match this
    # profile; persistent BTC/USDC evidence must prevent false exclusivity.
    with pytest.raises(ValueError, match='Another strategy'):
        create()
    assert spot.writes == []


def test_foreign_same_asset_on_verified_other_account_does_not_block(spot, monkeypatch):
    database.save_order_log('foreign-buy', 'BTC/USDC', 'MCP', 'buy', 100, 0, 0, 'different account',
                            trade_mode='SPOT_DCA', config_id='mcp:foreign', amount=1, status='FILLED')
    monkeypatch.setattr(exits, '_configured_account_scope', lambda config_id: 'different-account-scope')
    assert create()['status'] == 'open'


def test_partial_exit_and_cancel_release_only_unfilled_owned_amount(spot):
    row = create()
    spot.fill(row['exchange_id'], 1)
    stock = exits.inventory('mcp:test', 'BTC/USDT')
    assert stock['owned_quantity'] == pytest.approx(8.99)
    assert stock['reserved_quantity'] == 2
    result = exits.execute('cancel_spot_exit', {'exit_id': row['exit_id'], 'reason': 'test'}, 'mcp:test', 'BTC/USDT', 'cancel-operation')
    assert result['status'] == 'cancelled' and result['filled'] == 1
    assert exits.inventory('mcp:test', 'BTC/USDT')['available_quantity'] == pytest.approx(8.99)
    with pytest.raises(ValueError, match='belong'):
        exits.execute('cancel_spot_exit', {'exit_id': row['exit_id'], 'reason': 'test'}, 'mcp:foreign', 'BTC/USDT', 'foreign-cancel')


def test_unknown_submission_does_not_retry_and_can_recover_by_client_identity(spot):
    spot.error = ccxt.RequestTimeout('acknowledgement lost')
    first = create()
    assert first['status'] == 'unknown'
    assert create() == first and len(spot.writes) == 1
    spot.lookup_failure = True
    with pytest.raises(ValueError, match='unresolved'):
        create({'amount': 1}, 'different-operation')
    assert len(spot.writes) == 1
    spot.lookup_failure = False
    stock = exits.inventory('mcp:test', 'BTC/USDT')
    assert stock['reserved_quantity'] == 3
    assert stock['exits'][0]['status'] == 'open'
    assert len(spot.writes) == 1


def test_definite_exchange_rejection_releases_reservation(spot):
    spot.error = ccxt.InvalidOrder('too small https://exchange/private?signature=private-secret')
    row = create()
    assert row['status'] == 'failed'
    assert 'instrument limits' in row['failure_reason']
    assert row['automatic_retry_allowed'] is False
    assert 'private-secret' not in json.dumps(row)
    assert exits.inventory('mcp:test', 'BTC/USDT')['available_quantity'] == pytest.approx(9.99)


@pytest.mark.parametrize('rounded', ['0', 'nan', 'inf', '4'])
def test_precision_cannot_zero_inflate_or_corrupt_reserved_quantity(spot, monkeypatch, rounded):
    monkeypatch.setattr(spot, 'amount_to_precision', lambda *args: rounded)
    with pytest.raises(ValueError, match='precision-adjusted'):
        create()
    assert spot.writes == []


def test_owned_exit_cannot_be_manually_enlarged_without_detection(spot):
    row = create()
    spot.orders[row['exchange_id']]['amount'] = 8
    with pytest.raises(ValueError, match='unresolved'):
        create({'amount': 1}, 'second-operation')
    assert len(spot.writes) == 1


def test_reconciled_fill_between_read_and_reservation_does_not_restore_capacity(spot, monkeypatch):
    first = create({'amount': 3}, 'first-operation')
    original = exits._inventory

    def inventory_then_fill(mt, symbol):
        stock = original(mt, symbol)
        spot.fill(first['exchange_id'], 3)
        row = exits._rows('mcp:test')[0]
        exits._refresh(spot, row)
        return stock

    monkeypatch.setattr(exits, '_inventory', inventory_then_fill)
    # 6.99 was available at the read, but the verified 3-unit fill must never
    # permit a later reservation above the remaining ledger inventory.
    result = create({'amount': 6}, 'next-operation')
    assert result['status'] == 'open'
    assert len(spot.writes) == 2


def test_okx_explicit_rejection_is_failed_not_an_ambiguous_write(spot, monkeypatch):
    spot.id = 'okx'
    monkeypatch.setattr(spot, 'privatePostTradeOrderAlgo', lambda request: {'code': '1', 'data': [{'sCode': '51000'}]})
    result = exits.execute('create_spot_exit', {'amount': 1, 'exit_type': 'stop_loss', 'stop_loss': 90, 'reason': 'test'},
                           'mcp:test', 'BTC/USDT', 'rejected-okx-operation')
    assert result['status'] == 'failed'
    assert exits.inventory('mcp:test', 'BTC/USDT')['available_quantity'] == pytest.approx(9.99)


def test_binance_oco_is_one_native_group_and_one_inventory_reservation(spot):
    row = exits.execute('create_spot_exit', {'amount': 9, 'exit_type': 'oco', 'stop_loss': 90, 'take_profit': 120, 'reason': 'protect'},
                         'mcp:test', 'BTC/USDT', 'oco-operation')
    assert len(spot.writes) == 1 and spot.writes[0][0] == 'oco'
    request = spot.writes[0][1]
    assert request['aboveType'] == 'TAKE_PROFIT' and request['belowType'] == 'STOP_LOSS'
    assert request['quantity'] == '9.0'
    stock = exits.inventory('mcp:test', 'BTC/USDT')
    assert stock['reserved_quantity'] == 9
    assert stock['available_quantity'] == pytest.approx(0.99)
    result = exits.execute('cancel_spot_exit', {'exit_id': row['exit_id'], 'reason': 'test'}, 'mcp:test', 'BTC/USDT', 'cancel-oco')
    assert result['status'] == 'cancelled'
    assert spot.writes[-1][0] == 'cancel_oco'


def test_binance_oco_market_capability_requires_explicit_limit_fallback(spot):
    spot.market_info['orderTypes'] = ['MARKET', 'LIMIT', 'STOP_LOSS_LIMIT', 'TAKE_PROFIT_LIMIT']
    args = {'amount': 1, 'exit_type': 'oco', 'stop_loss': 90, 'take_profit': 120, 'reason': 'test'}
    with pytest.raises(ValueError, match='unsupported'):
        exits.execute('create_spot_exit', args, 'mcp:test', 'BTC/USDT', 'unsupported-operation')
    assert not spot.writes
    exits.execute('create_spot_exit', {**args, 'stop_limit_price': 89, 'take_profit_limit_price': 119},
                  'mcp:test', 'BTC/USDT', 'explicit-limit-operation')
    request = spot.writes[0][1]
    assert request['aboveType'] == 'TAKE_PROFIT_LIMIT' and request['belowType'] == 'STOP_LOSS_LIMIT'
    assert request['abovePrice'] == '119.0' and request['belowPrice'] == '89.0'


@pytest.mark.parametrize('kind', ['stop_loss', 'take_profit', 'oco'])
def test_okx_conditional_and_oco_use_cash_native_algo_orders(spot, kind):
    spot.id = 'okx'
    args = {'amount': 4, 'exit_type': kind, 'reason': 'test'}
    if kind in {'stop_loss', 'oco'}:
        args['stop_loss'] = 90
    if kind in {'take_profit', 'oco'}:
        args['take_profit'] = 120
    row = exits.execute('create_spot_exit', args, 'mcp:test', 'BTC/USDT', 'okx-operation')
    request = spot.writes[0][1]
    assert request['tdMode'] == 'cash' and request['side'] == 'sell' and request['sz'] == '4.0'
    assert request['ordType'] == ('oco' if kind == 'oco' else 'conditional')
    stock = exits.inventory('mcp:test', 'BTC/USDT')
    assert stock['reserved_quantity'] == 4 and stock['exchange_locked'] == 0
    assert stock['available_quantity'] == pytest.approx(5.99)
    result = exits.execute('cancel_spot_exit', {'exit_id': row['exit_id'], 'reason': 'test'}, 'mcp:test', 'BTC/USDT', 'cancel-okx')
    assert result['status'] == 'cancelled' and spot.writes[-1][0] == 'cancel_algo'


def test_triggered_okx_algo_needs_child_fills_before_release(spot):
    spot.id = 'okx'
    row = exits.execute('create_spot_exit', {'amount': 2, 'exit_type': 'stop_loss', 'stop_loss': 90, 'reason': 'test'},
                         'mcp:test', 'BTC/USDT', 'okx-trigger-operation')
    native = spot.algos[row['exchange_id']]
    native['state'] = 'effective'
    native['actualSz'] = '2'
    with pytest.raises(ValueError, match='unresolved'):
        exits.inventory('mcp:test', 'BTC/USDT')
    child = spot._new_order(2, 'child')
    native['ordIdList'] = [child['id']]
    spot.fill(child['id'], 2)
    stock = exits.inventory('mcp:test', 'BTC/USDT')
    assert stock['exits'][0]['status'] == 'filled'
    assert stock['available_quantity'] == pytest.approx(7.99)


@pytest.mark.parametrize('native_size', ['8', None, '', '0'])
def test_okx_algo_unverified_or_enlarged_quantity_blocks_another_exit(spot, native_size):
    spot.id = 'okx'
    args = {'amount': 3, 'exit_type': 'stop_loss', 'stop_loss': 90, 'reason': 'test'}
    row = exits.execute('create_spot_exit', args, 'mcp:test', 'BTC/USDT', 'initial-algo')
    native = spot.algos[row['exchange_id']]
    if native_size is None:
        native.pop('sz')
    else:
        native['sz'] = native_size
    with pytest.raises(ValueError, match='unresolved'):
        exits.execute('create_spot_exit', {**args, 'amount': 6}, 'mcp:test', 'BTC/USDT', 'another-algo')
    assert len(spot.writes) == 1
    assert exits._rows('mcp:test')[0]['state'] == 'unknown'


@pytest.mark.parametrize('state', ['canceled', 'order_failed'])
@pytest.mark.parametrize('actual_size', [None, '', '1'])
def test_okx_terminal_algo_needs_explicit_zero_execution_to_release(spot, state, actual_size):
    spot.id = 'okx'
    row = exits.execute('create_spot_exit', {'amount': 3, 'exit_type': 'stop_loss', 'stop_loss': 90, 'reason': 'test'},
                         'mcp:test', 'BTC/USDT', 'terminal-algo')
    native = spot.algos[row['exchange_id']]
    native['state'] = state
    if actual_size is None:
        native.pop('actualSz')
    else:
        native['actualSz'] = actual_size
    with pytest.raises(ValueError, match='unresolved'):
        exits.inventory('mcp:test', 'BTC/USDT')
    assert exits._rows('mcp:test')[0]['state'] == 'unknown'
    native['actualSz'] = '0'
    stock = exits.inventory('mcp:test', 'BTC/USDT')
    assert stock['reserved_quantity'] == 0
    assert stock['exits'][0]['status'] == 'cancelled'


def test_okx_live_algo_without_execution_size_keeps_reservation(spot):
    spot.id = 'okx'
    row = exits.execute('create_spot_exit', {'amount': 3, 'exit_type': 'stop_loss', 'stop_loss': 90, 'reason': 'test'},
                         'mcp:test', 'BTC/USDT', 'live-algo')
    spot.algos[row['exchange_id']].pop('actualSz')
    stock = exits.inventory('mcp:test', 'BTC/USDT')
    assert stock['reserved_quantity'] == 3 and stock['exits'][0]['status'] == 'open'


def test_reconciliation_maintains_unknown_exit_without_resubmitting(spot):
    spot.error = ccxt.RequestTimeout('lost')
    create()
    exits.reconcile('mcp:test')
    assert exits._rows('mcp:test')[0]['state'] == 'open'
    assert len(spot.writes) == 1


def test_spot_exit_evidence_is_in_profile_lifecycle_and_owned_symbols(spot):
    from backend.mcp.config_guard import active_lifecycle, owned_symbols
    create()
    with database.get_db_conn() as conn:
        conn.execute('DELETE FROM orders')
        profile = {'profile_id': 'test', 'market_type': 'spot'}
        assert owned_symbols(conn, profile) == ['BTC/USDT']
        assert active_lifecycle(conn, profile)


@pytest.mark.parametrize('args', [
    {'amount': 1, 'exit_type': 'market', 'price': 100, 'reason': 'test'},
    {'amount': 1, 'exit_type': 'limit', 'reason': 'test'},
    {'amount': 1, 'exit_type': 'oco', 'stop_loss': 120, 'take_profit': 90, 'reason': 'test'},
    {'amount': 1, 'exit_type': 'market', 'reason': 'test', 'reduceOnly': False},
    {'amount': float('inf'), 'exit_type': 'market', 'reason': 'test'},
])
def test_ambiguous_or_unexpected_financial_arguments_are_rejected(args):
    with pytest.raises(ValueError):
        exits.validate_arguments('create_spot_exit', args)
