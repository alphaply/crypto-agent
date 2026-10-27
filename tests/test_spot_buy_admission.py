"""Exercise the real v3 spot buy path with an isolated ledger and fake venue."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from backend import database
from backend.agent.unified_trading import execute_v3_actions
from backend.utils import visible_management as management
from backend.utils.market_data import MarketTool


@pytest.fixture
def spot_buys(financial_pg, monkeypatch):
    cfg = {'config_id': 'spot', 'mode': 'SPOT_DCA', 'market_type': 'spot',
           'symbol': 'BTC/USDT', 'symbols': ['BTC/USDT', 'BNB/USDT'],
           'enabled': True, 'dca_amount': 30, 'dca_budget': 300, 'model': 'offline'}

    class Venue:
        id, apiKey = 'binance', 'offline-spot-test'
        options = {'defaultType': 'spot'}

        def __init__(self):
            self.markets = {symbol: {'id': symbol.replace('/', ''), 'symbol': symbol,
                                    'base': symbol.split('/')[0], 'quote': 'USDT',
                                    'spot': True, 'contract': False} for symbol in cfg['symbols']}
            self.orders, self.writes = {}, []
            self.available = 100.0

        def market(self, symbol):
            return self.markets[symbol]

        def fetch_balance(self):
            return {'USDT': {'total': 100, 'free': self.available, 'used': 100 - self.available},
                    'BTC': {'total': 0, 'free': 0, 'used': 0},
                    'BNB': {'total': 0, 'free': 0, 'used': 0}}

        def fetch_open_orders(self, symbol, **kwargs):
            return [deepcopy(o) for o in self.orders.values() if o['symbol'] == symbol]

        def fetch_order(self, identity, symbol, params=None):
            return deepcopy(self.orders[identity])

        def fetch_my_trades(self, *args, **kwargs):
            return []

        def amount_to_precision(self, symbol, value):
            return str(value)

        def price_to_precision(self, symbol, value):
            return str(value)

        def create_order(self, symbol, kind, side, amount, price, params):
            identity = str(len(self.writes) + 1)
            order = {'id': identity, 'symbol': symbol, 'type': 'limit', 'side': side,
                     'amount': amount, 'price': price, 'status': 'open', 'filled': 0,
                     'remaining': amount, 'cost': 0, 'clientOrderId': params['clientOrderId']}
            self.writes.append(deepcopy(order))
            self.orders[identity] = order
            self.available -= amount * price
            return deepcopy(order)

    venue = Venue()
    mt = object.__new__(MarketTool)
    mt.config_id, mt.runtime_config, mt.exchange, mt.market_type = 'spot', cfg, venue, 'spot'
    mt.get_account_status = lambda symbol, **kwargs: {
        'available_balance': venue.available, 'real_open_orders': venue.fetch_open_orders(symbol)}
    monkeypatch.setattr('backend.config.config.get_config_by_id', lambda _: cfg)
    monkeypatch.setattr('backend.agent.agent_tools.MarketTool', lambda **kwargs: mt)
    monkeypatch.setattr('backend.utils.market_data.MarketTool', lambda **kwargs: mt)
    return SimpleNamespace(cfg=cfg, mt=mt, venue=venue)


def opening(symbol='BNB/USDT', amount=.008, price=726):
    return {'action': 'open', 'symbol': symbol, 'side': 'LONG', 'order_type': 'limit',
            'amount': amount, 'price': price, 'reason': 'offline regression'}


def execute(runtime, actions, operation='spot-regression'):
    return execute_v3_actions({'contract_version': 3, 'actions': actions},
                             config_id='spot', symbol='BTC/USDT', config=runtime.cfg,
                             operation_id=operation)


def old_action(runtime, state, legacy=False):
    claim = management.claim_management(runtime.mt, 'BNB/USDT', 'BASE')
    with database.get_db_conn() as conn:
        if legacy:
            conn.execute('INSERT INTO position_management_actions '
                         '(account_scope,symbol,side,owner,operation_id,state,updated_at) '
                         'VALUES(%s,%s,%s,%s,%s,%s,%s)',
                         (management._legacy_scope(runtime.mt), 'BNB/USDT', 'BASE',
                          'spot', 'old-bnb-operation', state, 0))
        else:
            conn.execute('INSERT INTO visible_management_actions '
                         '(claim_id,owner,version,operation_id,state,payload) VALUES(%s,%s,%s,%s,%s,%s)',
                         (claim['claim_id'], 'spot', claim['version'], 'old-bnb-operation', state,
                          json.dumps({'symbol': 'BNB/USDT'})))
        conn.commit()
    return 'position_management_actions' if legacy else 'visible_management_actions'


@pytest.mark.parametrize('state,legacy', [('unknown', False), ('pending', False),
                                        ('running', False), ('unresolved', True)])
def test_old_bnb_action_does_not_block_new_spot_buys_or_erase_original(spot_buys, state, legacy):
    table = old_action(spot_buys, state, legacy)
    actions = [opening('BTC/USDT', .0001, 79400), opening('BTC/USDT', .00012, 77600), opening()]
    result = execute(spot_buys, actions)
    assert [row['status'] for row in result['results']] == ['submitted'] * 3
    assert [(o['symbol'], o['amount'], o['price']) for o in spot_buys.venue.writes] == [
        ('BTC/USDT', .0001, 79400), ('BTC/USDT', .00012, 77600), ('BNB/USDT', .008, 726)]
    assert execute(spot_buys, actions) == result
    assert len(spot_buys.venue.writes) == 3
    with database.get_db_conn() as conn:
        original = conn.execute(f'SELECT state FROM {table} WHERE operation_id=%s',
                                ('old-bnb-operation',)).fetchone()
        assert original['state'] == state
        assert conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0] == 3


@pytest.mark.parametrize('boundary', ['budget', 'lifetime_budget', 'balance', 'read_only', 'disabled', 'symbol'])
def test_new_spot_buy_keeps_existing_limits(spot_buys, boundary):
    old_action(spot_buys, 'unknown')
    action = opening()
    if boundary == 'budget':
        spot_buys.cfg['dca_amount'] = 1
    elif boundary == 'lifetime_budget':
        spot_buys.cfg['dca_budget'] = 1
    elif boundary == 'balance':
        spot_buys.venue.available = 1
    elif boundary == 'read_only':
        spot_buys.cfg['read_only'] = True
    elif boundary == 'disabled':
        spot_buys.cfg['enabled'] = False
    else:
        action['symbol'] = 'ETH/USDT'
    result = execute(spot_buys, [action])
    assert result['status'] == 'failed'
    assert not spot_buys.venue.writes
    assert 'requires verification' not in json.dumps(result)


def test_spot_sell_still_requires_original_operation_verification(spot_buys):
    old_action(spot_buys, 'unknown')
    result = execute(spot_buys, [{'action': 'close', 'symbol': 'BNB/USDT', 'side': 'LONG',
                                'order_type': 'market', 'amount': .008, 'reason': 'offline sell'}])
    assert result['status'] == 'failed'
    assert 'requires verification' in result['results'][0]['result']['error']
    assert not spot_buys.venue.writes


def test_spot_buy_keeps_same_price_deduplication(spot_buys):
    old_action(spot_buys, 'unknown')
    assert execute(spot_buys, [opening()])['status'] == 'submitted'
    second = execute(spot_buys, [opening()], operation='different-request')
    assert second['results'][0]['status'] == 'skipped'
    assert len(spot_buys.venue.writes) == 1


def test_spot_buys_keep_shared_batch_budget(spot_buys):
    old_action(spot_buys, 'unknown')
    spot_buys.cfg['dca_amount'] = 20
    result = execute(spot_buys, [opening('BTC/USDT', .0001, 79400),
                                opening('BTC/USDT', .00012, 77600), opening()])
    assert [row['status'] for row in result['results']] == ['submitted', 'submitted', 'failed']
    assert 'requires verification' not in json.dumps(result)
    assert len(spot_buys.venue.writes) == 2


def test_spot_buy_keeps_process_lock(spot_buys):
    from backend.utils.management_lock import ManagementLockBusy, scope_file_lock
    from backend.utils.trade_receipts import TradeAttempt, current_attempt

    claim = management.claim_management(spot_buys.mt, 'BNB/USDT', 'BASE')
    token = current_attempt.set(TradeAttempt('new-buy', 'open'))
    try:
        with scope_file_lock(claim['physical_account'], 'spot', claim['resource']):
            with pytest.raises(ManagementLockBusy):
                with management.financial_reservation(spot_buys.mt, claim, 'new-buy'):
                    pytest.fail('Concurrent buy acquired the same resource lock')
    finally:
        current_attempt.reset(token)


def test_futures_open_keeps_original_verification_gate(spot_buys):
    from backend.utils.trade_receipts import TradeAttempt, current_attempt
    from test_visible_position_management import SYMBOL, tool

    mt = tool()
    claim = management.claim_management(mt, SYMBOL, 'LONG')
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO visible_management_actions '
                     '(claim_id,owner,version,operation_id,state,payload) VALUES(%s,%s,%s,%s,%s,%s)',
                     (claim['claim_id'], mt.config_id, claim['version'], 'original-futures',
                      'unknown', json.dumps({'symbol': SYMBOL})))
        conn.commit()
    token = current_attempt.set(TradeAttempt('new-futures', 'open'))
    try:
        with pytest.raises(ValueError, match='requires verification'):
            with management.financial_reservation(mt, claim, 'new-futures'):
                pytest.fail('Futures pending operation was ignored')
    finally:
        current_attempt.reset(token)
