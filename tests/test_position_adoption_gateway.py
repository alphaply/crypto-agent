"""Real adoption/exit dispatch against an in-memory perpetual exchange."""
import json
import time
from copy import deepcopy
from types import SimpleNamespace

import ccxt
import pytest

from backend import database
from backend.agent import agent_tools, tool_registry
from backend.config import config
from backend.database_schema import initialize_schema
from backend.mcp import service, settings as cfg, store
from backend.utils import market_data


SYMBOL = 'ETH/USDT:USDT'
ADOPTION = {'pos_side': 'SHORT', 'expected_amount': 0.37, 'reason': '用户要求管理手动空仓'}


class AdoptionExchange:
    id = 'binance'
    apiKey = 'offline-adoption-account'

    def __init__(self):
        self.quantity = 37.0
        self.orders = {}
        self.reads = []
        self.writes = []
        self.now = int(time.time() * 1000)
        self.trades = [self.trade('manual-before', 'manual-order', 37, 'sell', self.now - 500)]

    @staticmethod
    def trade(trade_id, order_id, amount, side, timestamp):
        return {'id': trade_id, 'order': order_id, 'symbol': SYMBOL, 'amount': amount,
                'price': 100, 'cost': amount, 'side': side, 'timestamp': timestamp,
                'info': {'positionSide': 'SHORT'}, 'fee': {'currency': 'USDT', 'cost': 0}}

    def load_markets(self):
        return {SYMBOL: self.market(SYMBOL)}

    def market(self, symbol):
        assert symbol in {SYMBOL, 'ETH/USDT'}
        return {'symbol': SYMBOL, 'id': 'ETHUSDT', 'base': 'ETH', 'quote': 'USDT',
                'settle': 'USDT', 'contract': True, 'swap': True, 'linear': True,
                'active': True, 'contractSize': 0.01}

    def fetch_time(self):
        self.reads.append(('time',))
        self.now = max(self.now + 1, int(time.time() * 1000))
        return self.now

    def fetch_positions(self, symbols=None, params=None):
        self.reads.append(('positions',))
        if not self.quantity:
            return []
        return [{'symbol': SYMBOL, 'side': 'short', 'contracts': self.quantity,
                 'contractSize': 0.01, 'entryPrice': 100, 'markPrice': 100,
                 'hedged': True, 'leverage': 2, 'marginMode': 'cross'}]

    def fetch_my_trades(self, symbol, since=None, limit=None, params=None):
        assert symbol == SYMBOL
        self.reads.append(('trades', since))
        until = (params or {}).get('until', (params or {}).get('endTime', float('inf')))
        rows = [row for row in self.trades if (since is None or row['timestamp'] >= since)
                and row['timestamp'] <= until]
        return deepcopy(rows[:limit] if limit else rows)

    def fetch_open_orders(self, symbol, since=None, limit=None, params=None):
        assert symbol == SYMBOL
        trigger = bool((params or {}).get('trigger'))
        self.reads.append(('open_orders', trigger))
        return [deepcopy(row) for row in self.orders.values()
                if row['status'] == 'open' and bool(row['params'].get('stopLossPrice')) == trigger]

    def fetch_position_mode(self, symbol):
        self.reads.append(('position_mode',))
        return {'hedged': True}

    def fetch_leverage(self, symbol):
        self.reads.append(('leverage',))
        return {'longLeverage': 2, 'shortLeverage': 2}

    def fetch_ticker(self, symbol):
        self.reads.append(('ticker',))
        return {'symbol': SYMBOL, 'last': 100}

    def amount_to_precision(self, symbol, amount):
        return str(round(amount, 8))

    def price_to_precision(self, symbol, price):
        return str(round(price, 2))

    def create_order(self, symbol, order_type, side, amount, price=None, params=None):
        params = dict(params or {})
        self.writes.append(('create', symbol, order_type, side, amount, params))
        order_id = 'managed-' + str(len(self.orders) + 1)
        row = {'id': order_id, 'symbol': symbol, 'type': order_type, 'side': side,
               'amount': amount, 'filled': 0, 'price': price, 'status': 'open',
               'clientOrderId': params['clientOrderId'], 'params': params}
        if order_type == 'market' and 'stopLossPrice' not in params:
            assert side == 'buy' and amount <= self.quantity
            row.update(status='closed', filled=amount)
            self.quantity -= amount
            self.now = max(self.now + 1, int(time.time() * 1000))
            self.trades.append(self.trade('fill-' + order_id, order_id, amount, side, self.now))
        self.orders[order_id] = row
        return deepcopy(row)

    def fetch_order(self, order_id, symbol, params=None):
        self.reads.append(('order', order_id))
        client_id = (params or {}).get('clientOrderId') or (params or {}).get('clientAlgoId')
        for row in self.orders.values():
            if row['id'] == order_id or (client_id and client_id == row['clientOrderId']):
                return deepcopy(row)
        raise ccxt.OrderNotFound('Unknown offline order')

    def cancel_order(self, order_id, symbol, params=None):
        self.writes.append(('cancel', order_id))
        self.orders[order_id]['status'] = 'canceled'
        return deepcopy(self.orders[order_id])

    def external_change(self, change):
        changes = {'increase': [10], 'decrease': [-10], 'net_zero': [10, -10]}
        for index, quantity in enumerate(changes[change]):
            self.now = max(self.now + 10, int(time.time() * 1000))
            self.trades.append(self.trade(f'external-{index}', f'external-order-{index}',
                                          abs(quantity), 'sell' if quantity > 0 else 'buy', self.now))
            self.quantity += quantity


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'adoption-gateway.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    account = {'profile_id': 'swap-account', 'exchange': 'binance', 'market_type': 'swap',
               'api_key': AdoptionExchange.apiKey, 'secret': 'offline-secret'}
    monkeypatch.setattr(cfg, 'exchange_profile', lambda _: dict(account))
    cfg.save_profile(cfg.MCPProfile(profile_id='manual', name='Manual position',
                                   exchange_profile_id='swap-account', market_type='swap',
                                   symbols=[SYMBOL], exit_mode='independent_exits'))
    store.put('settings', 'global', {'enabled': True, 'public_url': 'http://localhost:7860'})
    web = {**account, 'config_id': 'web-task', 'mode': 'REAL',
           'exit_mode': 'independent_exits', 'symbol': SYMBOL, 'exchange_profile_id': 'swap-account'}
    monkeypatch.setattr(config, 'get_config_by_id',
                        lambda cid: web if cid == 'web-task' else cfg.get_runtime_profile(cid))
    exchange = AdoptionExchange()

    def market_tool(config_id):
        return SimpleNamespace(exchange=exchange, config_id=config_id, market_type='swap',
                               runtime_config=config.get_config_by_id(config_id))

    monkeypatch.setattr(service, 'market_tool', lambda profile_id: market_tool('mcp:' + profile_id))
    monkeypatch.setattr(agent_tools, 'MarketTool', market_tool)
    monkeypatch.setattr(market_data, 'MarketTool', market_tool)
    caller = {'id': 'key:adoption-test', 'scopes': ['read', 'trade', 'cancel'], 'profile_ids': ['manual']}
    return SimpleNamespace(exchange=exchange, caller=caller, market_tool=market_tool)


def execute(gateway, tool, arguments, operation_id, channel='mcp'):
    if channel == 'mcp':
        return service.execute(gateway.caller, 'manual', SYMBOL, tool, arguments, operation_id)
    return json.loads(tool_registry.run_trade_tool(tool, arguments, 'web-task', SYMBOL,
                                                  operation_id=operation_id))


def close_arguments(exit_type='stop_market'):
    return {'orders': [{'pos_side': 'SHORT', 'amount': 0, 'exit_type': exit_type,
                        'reason': '用户要求设置止损' if exit_type == 'stop_market' else '用户要求全平',
                        **({'trigger_price': 110} if exit_type == 'stop_market' else {})}]}


def current_plan(config_id):
    with database.get_db_conn() as conn:
        row = conn.execute('SELECT payload FROM real_protection_plans WHERE config_id=? AND symbol=? AND side=?',
                           (config_id, SYMBOL, 'SHORT')).fetchone()
    return json.loads(row['payload']) if row else None


def test_gateway_adopts_manual_position_then_sets_stop_and_closes(gateway):
    positions = service.query(gateway.caller, 'positions', profile_id='manual', symbol=SYMBOL)
    assert positions[0]['contracts'] == 37 and positions[0]['base_amount'] == .37
    result = execute(gateway, 'adopt_position_real', ADOPTION, 'adopt-manual-0001')
    assert result['status'] == 'completed', result
    plan = current_plan('mcp:manual')
    assert plan['state'] == 'ACTIVE' and plan['entries'] == [] and plan['exits'] == []
    assert plan['adoption']['contracts'] == 37
    assert not gateway.exchange.writes
    assert ('open_orders', False) in gateway.exchange.reads
    assert ('open_orders', True) in gateway.exchange.reads
    with store.connection() as conn:
        assert conn.execute('SELECT state FROM mcp_operations').fetchone()[0] == 'completed'
        assert conn.execute('SELECT status FROM trade_action_runs').fetchone()[0] == 'completed'
    reads = list(gateway.exchange.reads)
    assert execute(gateway, 'adopt_position_real', ADOPTION, 'adopt-manual-0001') == result
    assert gateway.exchange.reads == reads

    stop = execute(gateway, 'close_position_real', close_arguments(), 'set-stop-manual-0001')
    assert stop['status'] == 'submitted', stop
    stop_order = next(iter(gateway.exchange.orders.values()))
    assert stop_order['amount'] == 37 and stop_order['params']['positionSide'] == 'SHORT'
    assert stop_order['params']['stopLossPrice'] == 110
    assert gateway.exchange.quantity == 37

    close = execute(gateway, 'close_position_real', close_arguments('market'), 'close-manual-0001')
    assert close['status'] == 'completed', close
    assert gateway.exchange.quantity == 0
    assert current_plan('mcp:manual')['state'] == 'DONE'
    assert gateway.exchange.orders[stop_order['id']]['status'] == 'canceled'
    assert [write[0] for write in gateway.exchange.writes] == ['create', 'create', 'cancel']
    reads = list(gateway.exchange.reads)
    assert execute(gateway, 'adopt_position_real', ADOPTION, 'adopt-manual-0001') == result
    assert gateway.exchange.reads == reads
    assert current_plan('mcp:manual')['state'] == 'DONE'


@pytest.mark.parametrize('channel', ['mcp', 'web'])
@pytest.mark.parametrize('change', ['increase', 'decrease', 'net_zero'])
@pytest.mark.parametrize('existing_stop', [False, True])
def test_external_fills_after_adoption_block_all_exit_writes(gateway, channel, change, existing_stop):
    adopted = execute(gateway, 'adopt_position_real', ADOPTION, 'adopt-external-0001', channel)
    assert adopted['status'] == 'completed', adopted
    if existing_stop:
        stop = execute(gateway, 'close_position_real', close_arguments(), 'existing-stop-0001', channel)
        assert stop['status'] == 'submitted', stop
    writes = deepcopy(gateway.exchange.writes)
    gateway.exchange.external_change(change)
    result = execute(gateway, 'close_position_real', close_arguments('market' if existing_stop else 'stop_market'),
                     'blocked-exit-0001', channel)
    assert result['status'] == 'failed', result
    assert gateway.exchange.writes == writes
    assert all(order['status'] == 'open' for order in gateway.exchange.orders.values())
    config_id = 'mcp:manual' if channel == 'mcp' else 'web-task'
    plan = current_plan(config_id)
    assert plan['state'] != 'DONE' and len(plan['exits']) == int(existing_stop)


@pytest.mark.parametrize('pending_kind', ['gateway', 'trade'])
def test_only_the_current_adoption_operation_is_exempt_from_pending_gate(gateway, pending_kind):
    if pending_kind == 'gateway':
        store.reserve_operation('manual', 'unresolved-other-0001', gateway.caller['id'],
                                {'tool': 'close_position_real', 'symbol': SYMBOL, 'arguments': close_arguments()})
    else:
        with database.get_db_conn() as conn:
            conn.execute('INSERT INTO trade_action_runs VALUES(?,?,?,?,?,?,?,?)',
                         ('mcp:manual', 'other-trade-0001', SYMBOL, 'offline-hash', 'running', None,
                          time.time(), time.time()))
            conn.commit()
    result = execute(gateway, 'adopt_position_real', ADOPTION, 'adopt-pending-0001')
    assert result['status'] == 'failed', result
    assert current_plan('mcp:manual') is None
    assert gateway.exchange.writes == []


def test_gateway_amount_mismatch_does_not_adopt_or_write(gateway):
    result = execute(gateway, 'adopt_position_real', {**ADOPTION, 'expected_amount': 37}, 'wrong-unit-0001')
    assert result['status'] == 'failed', result
    assert current_plan('mcp:manual') is None
    assert not gateway.exchange.writes


@pytest.mark.parametrize('entrypoint', ['get_operation', 'execute'])
@pytest.mark.parametrize('lifecycle', ['ACTIVE', 'DONE', 'REPLACED'])
def test_crashed_gateway_receipt_recovers_from_adoption_evidence_without_exchange(gateway, entrypoint, lifecycle):
    operation_id = 'adopt-crash-0001'
    original = execute(gateway, 'adopt_position_real', ADOPTION, operation_id)
    assert original['status'] == 'completed', original
    original_episode = current_plan('mcp:manual')['episode_id']
    if lifecycle != 'ACTIVE':
        closed = execute(gateway, 'close_position_real', close_arguments('market'), 'close-before-crash-0001')
        assert closed['status'] == 'completed', closed
        assert current_plan('mcp:manual')['state'] == 'DONE'
    if lifecycle == 'REPLACED':
        exchange = gateway.exchange
        exchange.quantity = 37
        exchange.now = max(exchange.now + 10, int(time.time() * 1000))
        exchange.trades.append(exchange.trade('manual-next', 'manual-next-order', 37, 'sell', exchange.now))
        next_cycle = execute(gateway, 'adopt_position_real', ADOPTION, 'adopt-next-cycle-0001')
        assert next_cycle['status'] == 'completed', next_cycle
        assert current_plan('mcp:manual')['episode_id'] != original_episode

    stable_id = 'mcp:' + store.digest(gateway.caller['id'] + ':' + operation_id)
    with store.connection() as conn:
        conn.execute("UPDATE mcp_operations SET state='started',result=NULL WHERE profile_id=? AND operation_id=?",
                     ('manual', operation_id))
        conn.execute("UPDATE trade_action_runs SET status='unknown',result=? WHERE config_id=? AND operation_id=?",
                     (json.dumps({'status': 'unknown', 'error': 'simulated lost receipt'}), 'mcp:manual', stable_id))
        conn.commit()
    reads, writes = deepcopy(gateway.exchange.reads), deepcopy(gateway.exchange.writes)
    if entrypoint == 'get_operation':
        inspected = service.operation(gateway.caller, 'manual', operation_id)
        assert inspected['state'] == 'completed', inspected
        recovered = inspected['result']
    else:
        recovered = execute(gateway, 'adopt_position_real', ADOPTION, operation_id)
    assert recovered['status'] == 'completed', recovered
    assert recovered['result']['episode_id'] == original_episode
    assert recovered['result']['amount'] == 0.37
    assert gateway.exchange.reads == reads
    assert gateway.exchange.writes == writes
    with store.connection() as conn:
        assert conn.execute('SELECT state FROM mcp_operations WHERE profile_id=? AND operation_id=?',
                            ('manual', operation_id)).fetchone()[0] == 'completed'
        assert conn.execute('SELECT status FROM trade_action_runs WHERE config_id=? AND operation_id=?',
                            ('mcp:manual', stable_id)).fetchone()[0] == 'completed'
