"""Offline checks for multi-symbol routing, budget sharing and uncertain writes."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from backend import database
from backend.agent import agent_tools, tool_registry
from backend.database_schema import initialize_schema


@pytest.fixture
def spot_runtime(tmp_path, monkeypatch):
    path = tmp_path / 'spot.sqlite'
    with sqlite3.connect(path) as conn:
        initialize_schema(conn)
    monkeypatch.setattr(database, 'DB_NAME', str(path))
    config = {'config_id': 'spot', 'mode': 'SPOT_DCA', 'model': 'offline',
              'symbol': 'BTC/USDT', 'symbols': ['BTC/USDT', 'ETH/USDT'],
              'dca_amount': 100, 'dca_budget': 1000}
    monkeypatch.setattr('backend.config.config.get_config_by_id', lambda _cid: config)
    calls = []
    constructed = []
    responses = []
    verified = {'status': 'canceled', 'filled': 0, 'cost': 0}
    available = [1000]
    recovery_orders = []

    class FakeMarket:
        def __init__(self, **kwargs):
            constructed.append(kwargs)
            self.exchange = SimpleNamespace(fetch_order=lambda *_args: verified)
            self.exchange.fetch_open_orders = lambda *a, **k: recovery_orders
            self.exchange.fetch_closed_orders = lambda *a, **k: []

        def get_account_status(self, symbol, **_kwargs):
            return {'real_open_orders': [], 'error': None, 'available_balance': available[0]}

        def place_real_order(self, symbol, action, params, **kwargs):
            calls.append((symbol, action, params))
            if responses:
                response = responses.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response
            return {'id': f'order-{len(calls)}', 'status': 'open'}

    monkeypatch.setattr(agent_tools, 'MarketTool', FakeMarket)
    return SimpleNamespace(config=config, calls=calls, constructed=constructed,
                           responses=responses, verified=verified, available=available,
                           recovery_orders=recovery_orders)


def order(symbol='BTC/USDT', cost=40, **changes):
    return {'symbol': symbol, 'action': 'BUY_LIMIT', 'entry_price': 10,
            'amount': cost / 10, 'reason': 'offline test', **changes}


def buy(orders, operation='buy', cycle='cycle-1', **kwargs):
    from backend.config import config
    from backend.utils.spot_execution import ensure_spot_budget_cycle
    if cycle:
        try:
            ensure_spot_budget_cycle('spot', config.get_config_by_id('spot'), cycle, 'test')
        except ValueError:
            pass  # The execution path must independently reject invalid budgets.
    return json.loads(tool_registry.run_trade_tool(
        'open_position_spot_dca', {'orders': orders, **kwargs}, 'spot', 'BTC/USDT',
        operation_id=operation, cycle_id=cycle))


def test_two_symbols_route_each_order_and_preserve_receipt(spot_runtime):
    result = buy([order(), order('ETH/USDT')])
    assert result['status'] == 'submitted'
    assert [call[0] for call in spot_runtime.calls] == ['BTC/USDT', 'ETH/USDT']
    assert [item['symbol'] for item in result['results']] == ['BTC/USDT', 'ETH/USDT']
    assert buy([order(), order('ETH/USDT')]) == result
    assert len(spot_runtime.calls) == 2
    with database.get_db_conn() as conn:
        rows = conn.execute('SELECT symbol, amount FROM orders ORDER BY id').fetchall()
    assert [(row['symbol'], row['amount']) for row in rows] == [('BTC/USDT', 4), ('ETH/USDT', 4)]


def test_legacy_single_order_defaults_to_configured_primary(spot_runtime):
    spot_runtime.config.pop('symbols')
    item = order()
    item.pop('symbol')
    assert buy([item])['status'] == 'submitted'
    assert spot_runtime.calls[0][0] == 'BTC/USDT'


@pytest.mark.parametrize('symbol', ['DOGE/USDT', 'BTC/USDT:USDT', 'BTC/USDC'])
def test_entire_batch_allowlist_checked_before_external_client(spot_runtime, symbol):
    result = buy([order(), order(symbol)])
    assert result['status'] == 'failed'
    assert not spot_runtime.constructed
    assert not spot_runtime.calls


@pytest.mark.parametrize('field,value', [('amount', 0), ('amount', -1), ('amount', float('nan')),
                                        ('entry_price', float('inf')), ('entry_price', 0)])
def test_nonpositive_or_nonfinite_inputs_fail_before_external_client(spot_runtime, field, value):
    assert buy([order(**{field: value})])['status'] == 'failed'
    assert not spot_runtime.constructed


def test_one_batch_cannot_multiply_allowance_by_symbol(spot_runtime):
    result = buy([order(cost=60), order('ETH/USDT', cost=50)])
    assert result['status'] == 'failed'
    assert '本轮预算' in result['error']
    assert not spot_runtime.calls


@pytest.mark.parametrize('field', ['dca_amount', 'dca_budget'])
def test_explicit_zero_budget_never_falls_back_to_default(spot_runtime, field):
    spot_runtime.config[field] = 0
    assert buy([order(cost=1)])['status'] == 'failed'
    assert not spot_runtime.constructed


def test_separate_tools_in_one_cycle_share_allowance(spot_runtime):
    assert buy([order(cost=60)], operation='one')['status'] == 'submitted'
    result = buy([order('ETH/USDT', cost=50)], operation='two')
    assert result['status'] == 'failed'
    assert len(spot_runtime.calls) == 1


def test_legacy_calls_without_cycle_cannot_create_allowance(spot_runtime):
    assert buy([order(cost=60)], operation='one', cycle=None)['status'] == 'failed'
    assert buy([order('ETH/USDT', cost=50)], operation='two', cycle=None)['status'] == 'failed'


def test_lifetime_budget_counts_filled_and_pending_across_symbols(spot_runtime):
    spot_runtime.config['dca_budget'] = 100
    spot_runtime.config['dca_amount'] = 200
    database.save_order_log('filled', 'BTC/USDT', 'spot', 'buy', 10, 0, 0, 'filled',
                            trade_mode='SPOT_DCA', config_id='spot', amount=6, status='FILLED')
    database.upsert_spot_order_fill('filled', 'spot', 'BTC/USDT', 'FILLED', 6, 60, 10)
    database.save_order_log('pending', 'ETH/USDT', 'spot', 'buy', 10, 0, 0, 'pending',
                            trade_mode='SPOT_DCA', config_id='spot', amount=3)
    result = buy([order(cost=20)])
    assert result['status'] == 'failed'
    assert '任务总预算' in result['error']
    assert not spot_runtime.calls


def test_task_budget_survives_new_cycle_and_optional_budget_never_resets_period(spot_runtime):
    spot_runtime.config['dca_budget'] = 100
    assert buy([order(cost=60)], operation='one')['status'] == 'submitted'
    assert buy([order('ETH/USDT', cost=50)], operation='two', cycle='cycle-2')['status'] == 'failed'
    spot_runtime.config.pop('dca_budget')
    assert buy([order('ETH/USDT', cost=50)], operation='three', cycle='cycle-3')['status'] == 'submitted'
    assert buy([order('ETH/USDT', cost=40)], operation='four', cycle='cycle-4')['status'] == 'submitted'


def test_unknown_write_stops_remaining_orders_and_cannot_be_retried(spot_runtime):
    spot_runtime.responses.append(TimeoutError('exchange request timed out'))
    result = buy([order(), order('ETH/USDT')])
    assert result['status'] == 'unknown'
    assert result['results'][1]['status'] == 'not_executed'
    assert len(spot_runtime.calls) == 1
    assert buy([order(), order('ETH/USDT')]) == result
    assert buy([order()], operation='new-id', cycle='new-cycle')['status'] == 'failed'
    assert len(spot_runtime.calls) == 1


def test_definite_rejection_stops_batch_and_releases_allowance(spot_runtime):
    import ccxt
    spot_runtime.responses.append(ccxt.InsufficientFunds('rejected'))
    result = buy([order(), order('ETH/USDT')])
    assert result['status'] == 'failed'
    assert result['results'][1]['status'] == 'not_executed'
    assert buy([order(cost=100)], operation='second')['status'] == 'submitted'


def test_spot_cancel_selects_symbol_and_rejects_foreign_orders(spot_runtime):
    database.save_order_log('foreign', 'ETH/USDT', 'other', 'buy', 10, 0, 0, 'test',
                            trade_mode='SPOT_DCA', config_id='other', amount=2)
    result = json.loads(tool_registry.run_trade_tool('cancel_orders_real',
        {'order_id': 'foreign', 'symbol': 'ETH/USDT', 'reason': 'cancel'}, 'spot', 'BTC/USDT'))
    assert result['status'] == 'failed'
    assert not spot_runtime.constructed
    database.save_order_log('owned', 'ETH/USDT', 'spot', 'buy', 10, 0, 0, 'test',
                            trade_mode='SPOT_DCA', config_id='spot', amount=2)
    result = json.loads(tool_registry.run_trade_tool('cancel_orders_real',
        {'order_id': 'owned', 'symbol': 'ETH/USDT', 'reason': 'cancel'}, 'spot', 'BTC/USDT'))
    assert result['status'] == 'completed'
    assert spot_runtime.calls[0][0:2] == ('ETH/USDT', 'CANCEL')


def test_cancellation_retains_partial_fill_cost_and_releases_remaining_budget(spot_runtime):
    spot_runtime.config['dca_budget'] = 100
    assert buy([order('ETH/USDT', cost=80)])['status'] == 'submitted'
    spot_runtime.verified.update(filled=2, cost=20, average=10)
    cancelled = json.loads(tool_registry.run_trade_tool('cancel_orders_real',
        {'order_id': 'order-1', 'symbol': 'ETH/USDT', 'reason': 'cancel'}, 'spot', 'BTC/USDT'))
    assert cancelled['status'] == 'completed'
    assert buy([order(cost=80)], operation='new', cycle='cycle-2')['status'] == 'submitted'
    assert buy([order(cost=1)], operation='over', cycle='cycle-3')['status'] == 'failed'


def test_futures_call_cannot_override_injected_symbol(spot_runtime, monkeypatch):
    spot_runtime.config.update(mode='REAL')
    seen = []
    monkeypatch.setitem(tool_registry._TOOL_BY_NAME, 'cancel_orders_real',
                        SimpleNamespace(func=lambda **kwargs: seen.append(kwargs) or 'ok'))
    tool_registry.run_trade_tool('cancel_orders_real',
        {'order_id': '1', 'symbol': 'ETH/USDT', 'reason': 'test'}, 'spot', 'BTC/USDT')
    assert seen[0]['symbol'] == 'BTC/USDT'


@pytest.mark.parametrize('status,qty,cost', [('OPEN', 0, 0), ('PARTIAL', 1, 10)])
def test_known_id_requires_matching_verified_evidence_before_recovery(spot_runtime, status, qty, cost):
    assert buy([order()])['status'] == 'submitted'
    with database.get_db_conn() as conn:
        conn.execute("UPDATE spot_budget_reservations SET status='unknown'")
        conn.commit()
    assert buy([order()], operation='blocked', cycle='cycle-2')['status'] == 'failed'
    database.upsert_spot_order_fill('order-1', 'spot', 'BTC/USDT', status, qty, cost, 10 if qty else 0)
    assert buy([order('ETH/USDT')], operation='recovered', cycle='cycle-2')['status'] == 'submitted'
    assert len(spot_runtime.calls) == 2


def test_task_deletion_cleans_reservations_but_history_cleanup_preserves_them(spot_runtime):
    from backend.app.services.database_service import CLEANABLE

    assert buy([order()])['status'] == 'submitted'
    database.save_trade_history([{'id': 'fill-1', 'order': 'order-1', 'symbol': 'BTC/USDT',
                                 'timestamp': 1700000000000, 'side': 'buy', 'price': 10, 'amount': 4, 'cost': 40}],
                                config_id='spot')
    assert 'spot_budget_reservations' not in CLEANABLE
    assert database.get_config_dependency_counts('spot')['spot_budget_reservations'] == 1
    database.delete_summaries_by_symbol('BTC/USDT')
    database.soft_delete_config_runtime_data('spot')
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM spot_budget_reservations').fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM orders WHERE trade_mode='SPOT_DCA'").fetchone()[0] == 1
    cleanup = database.purge_config_all_data('spot')
    assert cleanup['spot_budget_reservations_deleted'] == 1
    assert cleanup['trade_history_deleted'] == 1
    assert buy([order()], operation='buy')['status'] == 'submitted'


def test_database_export_preserves_budget_reservations(spot_runtime):
    from contextlib import closing
    from pathlib import Path
    from backend.app.services.database_export import database_file_response

    assert buy([order()])['status'] == 'submitted'
    response = database_file_response()
    try:
        with closing(sqlite3.connect(response.path)) as conn:
            row = conn.execute('SELECT quote_cost,status FROM spot_budget_reservations').fetchone()
        assert row == (40, 'submitted')
    finally:
        Path(response.path).unlink()


def test_new_run_gets_its_own_allowance_and_legacy_still_counts_lifetime(spot_runtime):
    from backend.utils.spot_execution import spot_budget_status
    assert buy([order(cost=70)], operation='one', cycle='old-random-uuid')['status'] == 'submitted'
    with database.get_db_conn() as conn:
        conn.execute("UPDATE spot_budget_reservations SET cycle_id='legacy-run-id'")
        conn.commit()
    result = buy([order('ETH/USDT', cost=40)], operation='two', cycle='another-uuid')
    assert result['status'] == 'submitted'
    status = spot_budget_status('spot', spot_runtime.config, 'another-uuid')
    assert status['period_remaining'] == 60
    assert status['lifetime_committed'] == 110


def test_week_period_boundaries_use_china_time():
    from datetime import datetime
    from backend.utils.spot_execution import spot_budget_period
    sunday = database.TZ_CN.localize(datetime(2026, 10, 4, 23, 59))
    monday = database.TZ_CN.localize(datetime(2026, 10, 5))
    assert spot_budget_period({'dca_freq': '1w'}, sunday)[0] == 'week:2026-09-28'
    assert spot_budget_period({'dca_freq': '1w'}, monday)[0] == 'week:2026-10-05'


def test_allowance_refreshes_next_calendar_period_without_erasing_lifetime(spot_runtime, monkeypatch):
    from backend.utils import spot_execution
    assert buy([order(cost=90)])['status'] == 'submitted'
    monkeypatch.setattr(spot_execution, 'spot_budget_period', lambda _: ('day:next', 9999999999))
    assert buy([order('ETH/USDT', cost=90)], operation='next', cycle='next-run')['status'] == 'submitted'
    assert spot_execution.spot_budget_status('spot', spot_runtime.config)['lifetime_committed'] == 180


@pytest.mark.parametrize('available', [70, 0, float('nan'), float('inf'), -1])
def test_whole_batch_checked_against_live_free_quote_before_write(spot_runtime, available):
    spot_runtime.available[0] = available
    result = buy([order(cost=40), order('ETH/USDT', cost=40)])
    assert result['status'] == 'failed'
    assert not spot_runtime.calls
    with database.get_db_conn() as conn:
        assert set(row[0] for row in conn.execute('SELECT status FROM spot_budget_reservations')) == {'released'}


def test_multi_symbol_missing_explicit_order_symbol_is_rejected(spot_runtime):
    item = order()
    item.pop('symbol')
    assert buy([item])['status'] == 'failed'
    assert not spot_runtime.constructed
    assert buy([item], operation='explicit', symbol='ETH/USDT')['status'] == 'submitted'
    assert spot_runtime.calls[0][0] == 'ETH/USDT'


def test_unknown_write_recovers_only_exact_client_id_and_never_replays(spot_runtime):
    spot_runtime.responses.append(TimeoutError('lost acknowledgment'))
    assert buy([order()])['status'] == 'unknown'
    client_id = spot_runtime.calls[0][2]['client_order_id']
    spot_runtime.recovery_orders.append({'id': 'recovered', 'clientOrderId': 'foreign',
        'symbol': 'BTC/USDT', 'side': 'buy', 'status': 'open', 'filled': 0, 'amount': 4, 'price': 10})
    assert buy([order('ETH/USDT')], operation='foreign')['status'] == 'failed'
    spot_runtime.recovery_orders[0]['clientOrderId'] = client_id
    assert buy([order('ETH/USDT')], operation='recovered')['status'] == 'submitted'
    assert len(spot_runtime.calls) == 2
    with database.get_db_conn() as conn:
        assert conn.execute("SELECT status,order_id FROM spot_budget_reservations WHERE operation_id='buy:0'").fetchone()[:] == ('submitted', 'recovered')
        assert conn.execute("SELECT COUNT(*) FROM orders WHERE order_id='recovered'").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM trade_action_runs WHERE operation_id='buy'").fetchone()[0] == 'submitted'


def test_stale_execution_fingerprint_rejected_before_network(spot_runtime):
    from backend.utils.spot_config_guard import spot_execution_fingerprint
    fingerprint = spot_execution_fingerprint(spot_runtime.config)
    spot_runtime.config['dca_amount'] = 200
    for previous in (fingerprint, ''):
        result = json.loads(tool_registry.run_trade_tool('open_position_spot_dca', {'orders': [order()]},
            'spot', 'BTC/USDT', expected_spot_fingerprint=previous))
        assert result['status'] == 'failed'
    assert not spot_runtime.constructed


def test_global_account_rotation_invalidates_captured_fingerprint(spot_runtime, monkeypatch):
    from backend.config import config
    from backend.utils.spot_config_guard import spot_execution_fingerprint
    monkeypatch.setattr(config, 'global_binance_api_key', 'old-offline-key')
    monkeypatch.setattr(config, 'global_binance_secret', 'old-offline-secret')
    fingerprint = spot_execution_fingerprint(spot_runtime.config)
    monkeypatch.setattr(config, 'global_binance_api_key', 'new-offline-key')
    result = json.loads(tool_registry.run_trade_tool('open_position_spot_dca', {'orders': [order()]},
        'spot', 'BTC/USDT', expected_spot_fingerprint=fingerprint))
    assert result['status'] == 'failed'
    assert not spot_runtime.constructed


@pytest.mark.parametrize('field', ['dca_amount', 'dca_budget'])
def test_zero_budget_status_allows_analysis_but_blocks_new_buys(spot_runtime, field):
    from backend.utils.spot_execution import spot_budget_status
    spot_runtime.config[field] = 0
    status = spot_budget_status('spot', spot_runtime.config)
    assert status['available'] == 0
    assert status['period_remaining' if field == 'dca_amount' else 'lifetime_remaining'] == 0
    assert status['pending_unknown'] is False
    assert buy([order(cost=1)])['status'] == 'failed'
    assert not spot_runtime.constructed
