import copy
import json
from types import SimpleNamespace

import ccxt
import pytest

from backend import database
from backend.database_schema import initialize_schema
from backend.utils.independent_exits import IndependentExits
from backend.utils.position_adoption import OKX_ALGO_ORDER_TYPES, adopt_position, find_adoption_receipt
from backend.utils.trade_operations import run_once


class ReadOnlyExchange:
    id = 'binance'
    apiKey = 'offline-adoption-account'

    def __init__(self):
        self.contract_size = 1.0
        self.server_time = 1_750_000_000_000
        self.positions = [{'symbol': 'ETH/USDT:USDT', 'side': 'short', 'contracts': .37,
                           'entryPrice': 2400.0, 'hedged': True}]
        self.trades = [{'id': 'manual-fill', 'order': 'manual-order', 'symbol': 'ETH/USDT:USDT',
                        'timestamp': self.server_time - 1000, 'side': 'sell', 'amount': .37, 'price': 2400.0}]
        self.orders = {False: [], True: []}
        self.reads = []
        self.writes = []

    def load_markets(self):
        pass

    def market(self, symbol):
        return {'symbol': symbol if ':' in symbol else symbol + ':USDT',
                'contract': True, 'swap': True, 'linear': True, 'contractSize': self.contract_size}

    def fetch_time(self):
        self.reads.append('time')
        return self.server_time

    def fetch_positions(self, symbols):
        self.reads.append('positions')
        return copy.deepcopy(self.positions)

    def fetch_open_orders(self, symbol, params):
        self.reads.append('trigger_orders' if params.get('trigger') else 'orders')
        return copy.deepcopy(self.orders[bool(params.get('trigger'))])

    def fetch_my_trades(self, symbol, since, limit):
        self.reads.append('trades')
        assert since == self.server_time - 60_000 and limit == 1000
        return copy.deepcopy(self.trades)

    def create_order(self, *args, **kwargs):
        self.writes.append('create')
        raise AssertionError('Adoption must not create exchange orders')

    def cancel_order(self, *args, **kwargs):
        self.writes.append('cancel')
        raise AssertionError('Adoption must not cancel exchange orders')

    def edit_order(self, *args, **kwargs):
        self.writes.append('edit')
        raise AssertionError('Adoption must not edit exchange orders')


@pytest.fixture
def market_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'adoption.db'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    exchange = ReadOnlyExchange()
    return SimpleNamespace(exchange=exchange, config_id='cfg', market_type='swap',
                           runtime_config={'mode': 'REAL', 'market_type': 'swap', 'exit_mode': 'independent_exits'})


def adopt(mt, **changes):
    args = {'symbol': 'ETH/USDT', 'pos_side': 'SHORT', 'expected_amount': .37,
            'reason': 'Manage the manually opened short position', 'operation_id': 'adopt-manual-short'}
    args.update(changes)
    return adopt_position(mt, **args)


def assert_no_adoption(mt):
    assert mt.exchange.writes == []
    with database.get_db_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM real_protection_events WHERE config_id='cfg'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM real_protection_plans WHERE config_id='cfg'").fetchone()[0] == 0


def test_adoption_records_position_baseline_without_fake_fills_or_orders(market_tool):
    result = adopt(market_tool)
    plan = IndependentExits(market_tool)._load('ETH/USDT:USDT', 'SHORT')
    assert result['status'] == 'completed' and result['amount'] == .37
    assert plan['state'] == 'ACTIVE' and plan['ever_filled']
    assert plan['entries'] == [] and plan['exits'] == [] and plan['legs'] == []
    baseline = plan['adoption']
    assert baseline['contracts'] == .37 and baseline['contract_size'] == 1
    assert baseline['side'] == 'SHORT' and baseline['entry_price'] == 2400
    assert baseline['since_ms'] == market_tool.exchange.server_time - 60_000
    assert baseline['observed_trade_ids'] == ['manual-fill']
    assert baseline['result'] == result
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM execution_order_links').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM execution_fills').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM execution_position_history').fetchone()[0] == 0
    assert market_tool.exchange.writes == []


def test_adoption_amount_uses_base_units_not_contracts(market_tool):
    market_tool.exchange.contract_size = .01
    market_tool.exchange.positions[0]['contracts'] = 37
    market_tool.exchange.trades[0]['amount'] = 37
    assert adopt(market_tool)['amount'] == .37
    assert IndependentExits(market_tool)._load('ETH/USDT:USDT', 'SHORT')['adoption']['contracts'] == 37


def test_same_operation_replays_without_private_exchange_reads_after_cycle_replaced(market_tool):
    result = adopt(market_tool)
    count = len(market_tool.exchange.reads)
    assert adopt(market_tool) == result
    assert len(market_tool.exchange.reads) == count
    service = IndependentExits(market_tool)
    plan = service._load('ETH/USDT:USDT', 'SHORT')
    plan['state'] = 'DONE'
    service._save(plan)
    replacement = service._new('ETH/USDT:USDT', 'SHORT')
    service._save(replacement)
    market_tool.exchange.positions = []
    assert adopt(market_tool) == result
    assert len(market_tool.exchange.reads) == count
    assert find_adoption_receipt('cfg', 'adopt-manual-short', 'ETH/USDT') == result
    assert find_adoption_receipt('cfg', 'adopt-manual-short', 'BTC/USDT') is None
    assert find_adoption_receipt('other', 'adopt-manual-short') is None


@pytest.mark.parametrize('changes', [{'expected_amount': .38}, {'pos_side': 'LONG'}, {'reason': 'different'}])
def test_operation_id_rejects_changed_request(market_tool, changes):
    adopt(market_tool)
    count = len(market_tool.exchange.reads)
    with pytest.raises(ValueError, match='different request'):
        adopt(market_tool, **changes)
    assert len(market_tool.exchange.reads) == count


def test_operation_id_cannot_reassign_different_exchange_account(market_tool):
    adopt(market_tool)
    market_tool.exchange.apiKey = 'another-offline-account'
    with pytest.raises(ValueError, match='different request or account'):
        adopt(market_tool)


@pytest.mark.parametrize('field,value', [('mode', 'STRATEGY'), ('mode', 'SPOT_DCA'),
                                        ('exit_mode', 'attached_optional'), ('market_type', 'spot')])
def test_adoption_requires_real_independent_perpetual_task(market_tool, field, value):
    market_tool.runtime_config[field] = value
    with pytest.raises(ValueError):
        adopt(market_tool)
    assert_no_adoption(market_tool)


@pytest.mark.parametrize('amount', [0, -.37, float('nan'), float('inf'), True, '.37', None])
def test_adoption_requires_explicit_finite_positive_number(market_tool, amount):
    with pytest.raises(ValueError):
        adopt(market_tool, expected_amount=amount)
    assert_no_adoption(market_tool)


@pytest.mark.parametrize('positions', [[],
    [{'symbol': 'ETH/USDT:USDT', 'side': 'short', 'contracts': .4, 'entryPrice': 2400, 'hedged': True}],
    [{'symbol': 'ETH/USDT:USDT', 'side': 'long', 'contracts': .37, 'entryPrice': 2400, 'hedged': True}],
    [{'symbol': 'ETH/USDT:USDT', 'side': 'short', 'contracts': float('nan'), 'entryPrice': 2400}],
    [{'symbol': 'ETH/USDT:USDT', 'side': 'short', 'contracts': .37, 'entryPrice': None}],
])
def test_unverified_or_different_position_is_not_adopted(market_tool, positions):
    market_tool.exchange.positions = positions
    with pytest.raises(ValueError):
        adopt(market_tool)
    assert_no_adoption(market_tool)


def test_another_direction_is_not_silently_combined(market_tool):
    market_tool.exchange.positions.append({'symbol': 'ETH/USDT:USDT', 'side': 'long', 'contracts': .1})
    with pytest.raises(ValueError, match='another or unknown direction'):
        adopt(market_tool)
    assert_no_adoption(market_tool)


@pytest.mark.parametrize('trigger', [False, True])
def test_existing_orders_require_review_and_are_not_cancelled(market_tool, trigger):
    market_tool.exchange.orders[trigger] = [{'id': 'manual-protection'}]
    with pytest.raises(ValueError, match='orders must be reviewed'):
        adopt(market_tool)
    assert_no_adoption(market_tool)


@pytest.mark.parametrize('method', ['fetch_time', 'fetch_positions', 'fetch_open_orders', 'fetch_my_trades'])
def test_failed_read_never_creates_adoption(market_tool, method):
    def unavailable(*args, **kwargs):
        raise ccxt.RequestTimeout('offline read timeout')
    setattr(market_tool.exchange, method, unavailable)
    with pytest.raises(ccxt.RequestTimeout):
        adopt(market_tool)
    assert_no_adoption(market_tool)


@pytest.mark.parametrize('field', ['id', 'timestamp'])
def test_fill_boundary_requires_stable_id_and_timestamp(market_tool, field):
    market_tool.exchange.trades[0].pop(field)
    with pytest.raises(ValueError):
        adopt(market_tool)
    assert_no_adoption(market_tool)


def test_full_boundary_page_is_not_assumed_complete(market_tool):
    original = market_tool.exchange.trades[0]
    market_tool.exchange.trades = [{**original, 'id': str(i)} for i in range(1000)]
    with pytest.raises(ValueError, match='exceeds one verifiable page'):
        adopt(market_tool)
    assert_no_adoption(market_tool)


def test_new_fill_in_same_millisecond_during_verification_rejects_adoption(market_tool):
    exchange = market_tool.exchange
    original = exchange.fetch_my_trades
    calls = 0
    def changed(*args, **kwargs):
        nonlocal calls
        calls += 1
        rows = original(*args, **kwargs)
        if calls == 2:
            rows.append({**rows[0], 'id': 'second-same-millisecond-fill'})
        return rows
    exchange.fetch_my_trades = changed
    with pytest.raises(ValueError, match='changed during verification'):
        adopt(market_tool)
    assert_no_adoption(market_tool)


def test_changed_position_during_verification_rejects_adoption(market_tool):
    exchange = market_tool.exchange
    original = exchange.fetch_positions
    calls = 0
    def changed(*args, **kwargs):
        nonlocal calls
        calls += 1
        rows = original(*args, **kwargs)
        if calls == 2:
            rows[0]['entryPrice'] = 2401
        return rows
    exchange.fetch_positions = changed
    with pytest.raises(ValueError, match='changed during verification'):
        adopt(market_tool)
    assert_no_adoption(market_tool)


@pytest.mark.parametrize('owner,side', [('other', 'SHORT'), ('cfg', 'LONG'), ('cfg', 'SHORT')])
def test_existing_cycle_in_either_direction_blocks_adoption(market_tool, owner, side):
    other = SimpleNamespace(**{**vars(market_tool), 'config_id': owner})
    service = IndependentExits(other)
    plan = service._new('ETH/USDT:USDT', side)
    service._save(plan)
    with pytest.raises(ValueError, match='already owns'):
        adopt(market_tool)
    assert market_tool.exchange.writes == []
    assert not service._load('ETH/USDT:USDT', side).get('adoption')


@pytest.mark.parametrize('status', ['running', 'unknown', 'pending'])
def test_unknown_local_operation_blocks_adoption(market_tool, status):
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO trade_action_runs VALUES(?,?,?,?,?,?,?,?)',
                     ('cfg', 'old-operation', 'ETH/USDT', 'hash', status, None, 0, 0))
        conn.commit()
    with pytest.raises(ValueError, match='unresolved operation'):
        adopt(market_tool)
    assert_no_adoption(market_tool)


def test_current_run_once_operation_is_the_only_local_exception(market_tool):
    result = run_once('cfg', 'ETH/USDT', 'adopt-manual-short', {'action': 'adopt'}, lambda: adopt(market_tool))
    assert result['status'] == 'completed'
    assert market_tool.exchange.writes == []


def test_known_foreign_fill_cannot_be_reclassified_as_manual(market_tool):
    service = IndependentExits(market_tool)
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO execution_order_links VALUES(?,?,?,?,?,?)',
                     (service.account_scope, 'ETH/USDT:USDT', 'manual-order', 'another-task', 'entry', '{}'))
        conn.commit()
    with pytest.raises(ValueError, match='Another task owns fills'):
        adopt(market_tool)
    assert_no_adoption(market_tool)


@pytest.mark.parametrize('competing_side', ['SHORT', 'LONG'])
def test_cycle_created_after_adoption_preflight_cannot_be_overwritten(market_tool, monkeypatch, competing_side):
    original_new = IndependentExits._new

    def race_with_new_cycle(service, symbol, side, *args, **kwargs):
        competing = original_new(service, symbol, competing_side)
        service._save(competing)
        return original_new(service, symbol, side, *args, **kwargs)

    monkeypatch.setattr(IndependentExits, '_new', race_with_new_cycle)
    with pytest.raises(ValueError, match='active position cycle'):
        adopt(market_tool)
    plan = IndependentExits(market_tool)._load('ETH/USDT:USDT', competing_side)
    assert plan['state'] == 'WAITING' and not plan.get('adoption')
    assert market_tool.exchange.writes == []


def test_closing_only_later_addition_is_not_proof_adopted_baseline_was_closed(market_tool):
    adopt(market_tool)
    service = IndependentExits(market_tool)
    plan = service._load('ETH/USDT:USDT', 'SHORT')
    # A later add and exit balance each other, but the original .37 remains.
    plan['entries'] = [{'filled': .1}]
    plan['exits'] = [{'filled': .1, 'exit_type': 'market'}]
    assert service._flat_confirmed(plan) is False
    assert plan['state'] == 'ACTIVE'


def test_receipt_lookup_on_legacy_database_without_history_tables(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'legacy.db'))
    assert find_adoption_receipt('cfg', 'unknown-operation') is None


@pytest.mark.parametrize('payload', ['not-json', '[]', '{"adoption": "invalid"}'])
def test_receipt_lookup_ignores_corrupt_unrelated_history(market_tool, payload):
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO real_protection_events(timestamp,config_id,symbol,payload) VALUES(?,?,?,?)',
                     ('test', 'cfg', 'ETH/USDT:USDT', payload))
        conn.commit()
    assert find_adoption_receipt('cfg', 'unknown-operation') is None
    with pytest.raises(ValueError, match='history cannot be verified'):
        adopt(market_tool)


def test_corrupt_exact_adoption_receipt_never_repeats_adoption(market_tool):
    adopt(market_tool)
    with database.get_db_conn() as conn:
        for table in ('real_protection_plans', 'real_protection_events'):
            rows = conn.execute(f'SELECT rowid AS record_id,payload FROM {table}').fetchall()
            for row in rows:
                plan = json.loads(row['payload'])
                plan['adoption']['result'] = None
                conn.execute(f'UPDATE {table} SET payload=? WHERE rowid=?', (json.dumps(plan), row['record_id']))
        conn.commit()
    count = len(market_tool.exchange.reads)
    assert find_adoption_receipt('cfg', 'adopt-manual-short') is None
    with pytest.raises(ValueError, match='receipt is incomplete'):
        adopt(market_tool)
    assert len(market_tool.exchange.reads) == count


def test_adoption_does_not_exempt_a_prefix_of_current_operation(market_tool):
    result = run_once('cfg', 'ETH/USDT', 'adopt-manual-short', {'action': 'adopt'},
                      lambda: adopt(market_tool, operation_id='adopt-manual-short:child'))
    assert result['status'] != 'completed'
    assert 'unresolved operation' in result['error']
    assert_no_adoption(market_tool)


@pytest.mark.parametrize('order_type', OKX_ALGO_ORDER_TYPES)
def test_okx_all_algo_books_must_be_empty_before_adoption(market_tool, order_type):
    exchange = market_tool.exchange
    exchange.id = 'okx'
    seen = []

    def orders(symbol, params):
        seen.append(dict(params))
        if params.get('ordType') == order_type:
            assert params['trigger'] is True
            return [{'id': 'manual-algo', 'type': order_type}]
        return []

    exchange.fetch_open_orders = orders
    with pytest.raises(ValueError, match='orders must be reviewed'):
        adopt(market_tool)
    assert {'trigger': True, 'ordType': order_type} in seen
    assert_no_adoption(market_tool)


def test_okx_checks_every_algo_book_in_both_stable_snapshots(market_tool):
    market_tool.exchange.id = 'okx'
    seen = []

    def orders(symbol, params):
        seen.append(dict(params))
        return []

    market_tool.exchange.fetch_open_orders = orders
    assert adopt(market_tool)['status'] == 'completed'
    assert seen.count({}) == 2
    for order_type in OKX_ALGO_ORDER_TYPES:
        assert seen.count({'trigger': True, 'ordType': order_type}) == 2
    assert market_tool.exchange.writes == []


def test_okx_unavailable_algo_book_is_not_assumed_empty(market_tool):
    market_tool.exchange.id = 'okx'

    def orders(symbol, params):
        if params.get('ordType') == 'oco':
            raise ccxt.NotSupported('offline unsupported order book')
        return []

    market_tool.exchange.fetch_open_orders = orders
    with pytest.raises(ccxt.NotSupported):
        adopt(market_tool)
    assert_no_adoption(market_tool)
