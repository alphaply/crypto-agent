"""Exercise public tool dispatch with the real simulated ledger, without an exchange."""
import json
import time
from types import SimpleNamespace

import pytest

from backend import database
from backend.agent import agent_tools, tool_registry
from backend.config import config
from backend.database_independent import MockIndependentTrading
from backend.database_schema import initialize_schema


@pytest.fixture
def simulation(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'tools.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    cfg = {'config_id': 'cfg', 'symbol': 'ETH/USDT', 'mode': 'STRATEGY', 'exit_mode': 'independent_exits'}
    monkeypatch.setattr(config, 'get_config_by_id', lambda cid: cfg if cid == 'cfg' else None)
    monkeypatch.setattr(agent_tools, 'MarketTool', lambda **_: SimpleNamespace(exchange=SimpleNamespace(fetch_ticker=lambda _: {'last': 100})))
    return MockIndependentTrading('cfg', 'ETH/USDT')


def call(name, args, operation_id):
    return json.loads(tool_registry.run_trade_tool(name, args, 'cfg', 'ETH/USDT', operation_id=operation_id))


@pytest.fixture
def real_short(simulation, monkeypatch):
    """Public dispatch against a deterministic exchange, never a live account."""
    from test_independent_exits import Exchange, entry
    from backend.utils.independent_exits import IndependentExits

    class ShortExchange(Exchange):
        missing_cancel_fill = False
        position_failure = False

        def fetch_positions(self, symbols):
            if self.position_failure:
                raise TimeoutError('position read unavailable')
            return [dict(side='short', contracts=self.quantity, hedged=True)] if self.quantity else []

        def cancel_order(self, oid, symbol, params):
            result = super().cancel_order(oid, symbol, params)
            if self.missing_cancel_fill:
                self.orders[str(oid)]['filled'] = None
                result['filled'] = None
            return result

    exchange = ShortExchange()
    service = IndependentExits(SimpleNamespace(exchange=exchange, config_id='cfg'))
    opening = entry()
    opening.action = 'SELL_LIMIT'
    first = service.open('ETH/USDT', opening, operation_id='short-entry', contract_version=2)
    exchange.orders[first['id']].update(status='closed', filled=1)
    exchange.quantity = 1
    service.reconcile_all()
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: {
        'config_id': 'cfg', 'mode': 'REAL', 'exit_mode': 'independent_exits'})
    monkeypatch.setattr(agent_tools, '_independent_service', lambda *_: (service, True))
    return service, exchange


def test_market_short_close_keeps_fill_when_cleanup_ack_has_no_fill(real_short):
    service, exchange = real_short
    stop = call('close', dict(side='SHORT', order_type='stop_market', amount=1,
                             trigger_price=110, reason='protect'), 'protect-short')
    exchange.missing_cancel_fill = True
    result = call('close', dict(side='SHORT', order_type='market', close_all=True,
                               reason='exit'), 'market-close-short')
    assert result['status'] == 'completed'
    assert result['filled_amount'] == 1
    assert result['operation_id'] == 'market-close-short'
    assert result['cleanup']['status'] == 'pending'
    assert stop['order_id'] in [item['order_id'] for item in result['cleanup']['orders']]
    assert result['recovery']['target_operation_id'] == 'market-close-short'
    assert sum(row['type'] == 'market' and 'stopLossPrice' not in row['params']
               for row in exchange.calls) == 1


def test_market_close_preflight_read_failure_is_not_submitted(real_short):
    _, exchange = real_short
    exchange.position_failure = True
    before = len(exchange.calls)
    result = call('close', dict(side='SHORT', order_type='market', close_all=True,
                               reason='exit'), 'read-failed-close')
    assert result['status'] == 'failed'
    assert result['execution']['submission_state'] == 'not_submitted'
    assert len(exchange.calls) == before


def test_market_close_lost_ack_retains_original_query_identity(real_short):
    _, exchange = real_short
    exchange.timeout_next = True
    result = call('close', dict(side='SHORT', order_type='market', close_all=True,
                               reason='exit'), 'timeout-close')
    assert result['status'] == 'unknown'
    assert result['execution']['submission_state'] == 'unknown'
    assert result['client_id']
    assert result['filled_amount'] is None
    assert result['recovery']['target_operation_id'] == 'timeout-close'


def test_confirmed_close_with_late_entry_keeps_residual_separate(real_short):
    service, exchange = real_short
    add = call('open', dict(side='SHORT', amount=.3, price=120, reason='add'), 'late-add')
    cancel = exchange.cancel_order
    def late_fill(oid, symbol, params):
        if str(oid) == add['id']:
            exchange.orders[str(oid)]['filled'] = .3
            exchange.quantity += .3
        return cancel(oid, symbol, params)
    exchange.cancel_order = late_fill
    result = call('close', dict(side='SHORT', order_type='market', close_all=True, reason='exit'), 'residual-close')
    assert result['status'] == 'completed' and result['filled_amount'] == 1
    assert result['position']['amount'] == pytest.approx(.3)
    assert result['cleanup']['status'] == 'pending'
    assert len(exchange.calls) == 3


@pytest.mark.parametrize('action', ['open', 'close'])
@pytest.mark.parametrize('failure,expected', [('timeout', 'unknown'), ('generic_exchange', 'unknown'), ('rejected', 'failed')])
def test_submission_stage_classification_retains_identity(real_short, action, failure, expected):
    import ccxt
    _, exchange = real_short
    calls = []
    error = {'timeout': ccxt.RequestTimeout, 'generic_exchange': ccxt.ExchangeError, 'rejected': ccxt.InvalidOrder}[failure]
    def create(*args, **kwargs):
        calls.append((args, kwargs))
        raise error('synthetic submission failure')
    exchange.create_order = create
    args = dict(side='SHORT', amount=.2, price=120, reason='add') if action == 'open' else dict(side='SHORT', order_type='market', close_all=True, reason='exit')
    result = call(action, args, 'classification')
    assert result['status'] == expected
    assert result['client_id']
    assert result['execution']['phase'] == 'submit'
    assert result['execution']['submission_state'] == ('not_submitted' if expected == 'failed' else 'unknown')
    assert len(calls) == 1


def test_accepted_close_survives_post_submission_persistence_failure(real_short, monkeypatch):
    service, exchange = real_short
    save = service._save
    def fail_after_accept(plan):
        if any(r.get('operation_id') == 'save-failed' and r.get('id') for r in plan['exits']):
            raise OSError('synthetic database failure')
        save(plan)
    monkeypatch.setattr(service, '_save', fail_after_accept)
    result = call('close', dict(side='SHORT', order_type='market', close_all=True, reason='exit'), 'save-failed')
    assert result['status'] == 'completed' and result['filled_amount'] == 1
    assert result['client_id'] and result['exchange_order_id']
    monkeypatch.setattr(service, '_save', save)
    recovered = call('get_trade_operation', {'target_operation_id': 'save-failed'}, 'read-after-save-failure')
    assert recovered['status'] == 'completed'
    assert len(exchange.calls) == 2


def test_one_call_multi_exit_and_replay(simulation):
    opening = {'orders': [{'action': 'BUY_LIMIT', 'entry_price': 100, 'amount': 2, 'reason': 'test'}]}
    first = call('open_position_strategy', opening, 'entry-call')
    assert call('open_position_strategy', opening, 'entry-call') == first
    assert len(simulation.snapshot()['entries']) == 1
    simulation.monitor(110, 90, int((time.time() + 60) * 1000))
    payload = {'actions': [
        {'action': 'close', 'order': {'pos_side': 'LONG', 'exit_type': 'take_profit_limit', 'price': 120, 'amount': 1, 'reason': 'first target'}},
        {'action': 'close', 'order': {'pos_side': 'LONG', 'exit_type': 'take_profit_limit', 'price': 130, 'amount': 1, 'reason': 'second target'}},
        {'action': 'close', 'order': {'pos_side': 'LONG', 'exit_type': 'stop_market', 'trigger_price': 90, 'amount': 0, 'reason': 'invalidation'}},
    ]}
    result = call('execute_trade_actions', payload, 'exit-call')
    assert all(r['status'] == 'submitted' for r in result['results'])
    assert call('execute_trade_actions', payload, 'exit-call') == result
    assert len(simulation.snapshot()['exits']) == 3
    market = {'orders': [{'pos_side': 'LONG', 'exit_type': 'market', 'amount': .5, 'reason': 'reduce'}]}
    call('close_position_strategy', market, 'reduce-call')
    assert sorted(x['remaining'] for x in simulation.snapshot()['exits']) == [1, 1, 2]
    assert simulation.snapshot()['positions'][0]['amount'] == 1.5


def test_amend_exit_dispatch_and_cancel(simulation):
    call('open_position_strategy', {'orders': [{'action': 'BUY_LIMIT', 'entry_price': 100, 'amount': 1, 'reason': 'test'}]}, 'entry')
    simulation.monitor(110, 90, int((time.time() + 60) * 1000))
    call('close_position_strategy', {'orders': [{'pos_side': 'LONG', 'exit_type': 'stop_market', 'trigger_price': 90, 'amount': 1, 'reason': 'test'}]}, 'exit')
    order_id = simulation.snapshot()['exits'][0]['order_id']
    updated = call('update_exit_order', {'order_id': order_id, 'trigger_price': 95, 'amount': .5, 'reason': 'revise'}, 'amend')
    assert updated['trigger_price'] == 95 and updated['remaining'] == .5
    call('cancel_orders_strategy', {'order_id': updated['order_id'], 'reason': 'cancel'}, 'cancel')
    assert simulation.snapshot()['exits'] == []


def test_existing_protection_api_cannot_write_independent_mode(simulation, monkeypatch):
    from backend.app.services import stats_service
    monkeypatch.setattr(stats_service.global_config, 'get_config_by_id', config.get_config_by_id)
    with pytest.raises(ValueError, match='独立退出'):
        stats_service.update_position_protection_payload('cfg', 'ETH/USDT', 'LONG', stop_loss=90)


def test_canonical_cross_round_stages_all_edit_and_cancel(simulation):
    first = call('open', {'side': 'LONG', 'amount': .4, 'price': 100, 'reason': 'first stage'}, 'v2-entry-1')
    second = call('open', {'side': 'LONG', 'amount': .6, 'price': 99, 'reason': 'second stage'}, 'v2-entry-2')
    assert first['status'] == second['status'] == 'submitted'
    assert first['order_id'].startswith('regular:')
    assert simulation.snapshot()['exits'] == []
    simulation.monitor(110, 90, int((time.time() + 60) * 1000))
    payload = {'contract_version': 2, 'actions': [
        {'action': 'close', 'side': 'LONG', 'order_type': 'limit', 'amount': .2, 'price': 120, 'reason': 'first TP'},
        {'action': 'close', 'side': 'LONG', 'order_type': 'limit', 'amount': .3, 'price': 130, 'reason': 'second TP'},
        {'action': 'close', 'side': 'LONG', 'order_type': 'take_profit_market', 'close_all': True, 'trigger_price': 140, 'reason': 'remaining TP'},
        {'action': 'close', 'side': 'LONG', 'order_type': 'stop_market', 'close_all': True, 'trigger_price': 90, 'reason': 'remaining SL'},
    ]}
    receipt = call('execute_trade_actions', payload, 'v2-stage-exits')
    assert receipt['status'] == 'submitted'
    assert len(receipt['results']) == 4
    outcomes = [row['result'] for row in receipt['results']]
    assert [row['quantity_mode'] for row in outcomes] == ['fixed', 'fixed', 'all', 'all']
    assert outcomes[2]['order_status'] == 'waiting_trigger'
    assert outcomes[2]['requested_amount'] is None
    assert call('execute_trade_actions', payload, 'v2-stage-exits') == receipt
    changed = call('edit', {'order_id': outcomes[2]['order_id'], 'trigger_price': 145, 'reason': 'new final target'}, 'v2-edit-final')
    assert changed['quantity_mode'] == 'all' and changed['edit_method'] == 'cancel_replace'
    assert changed['order_id'] != outcomes[2]['order_id']
    call('cancel', {'order_id': outcomes[3]['order_id'], 'reason': 'replace SL thesis'}, 'v2-cancel-sl')
    simulation.monitor(125, 100, int((time.time() + 120) * 1000))
    assert simulation.snapshot()['positions'][0]['amount'] == pytest.approx(.8)
    simulation.monitor(135, 100, int((time.time() + 180) * 1000))
    assert simulation.snapshot()['positions'][0]['amount'] == pytest.approx(.5)
    simulation.monitor(150, 140, int((time.time() + 240) * 1000))
    assert simulation.snapshot()['positions'] == []
    assert simulation.snapshot()['exits'] == []


def test_canonical_batch_cannot_exit_unfilled_entry(simulation):
    payload = {'contract_version': 2, 'actions': [
        {'action': 'open', 'side': 'LONG', 'amount': 1, 'price': 100, 'reason': 'entry'},
        {'action': 'close', 'side': 'LONG', 'order_type': 'market', 'close_all': True, 'reason': 'not filled'},
        {'action': 'open', 'side': 'SHORT', 'amount': 1, 'price': 110, 'reason': 'must not execute'},
    ]}
    receipt = call('execute_trade_actions', payload, 'v2-no-fill')
    assert [row['status'] for row in receipt['results']] == ['submitted', 'failed', 'not_executed']
    assert len(simulation.snapshot()['entries']) == 1


def test_stats_show_aggregate_position_and_exit_quantities(simulation, monkeypatch):
    from backend.app.services import stats_service
    monkeypatch.setattr(stats_service, 'MarketTool', agent_tools.MarketTool)
    call('open_position_strategy', {'orders': [{'action': 'BUY_LIMIT', 'entry_price': 100, 'amount': 1, 'reason': 'test'}]}, 'entry')
    simulation.monitor(110, 90, int((time.time() + 60) * 1000))
    call('close_position_strategy', {'orders': [{'pos_side': 'LONG', 'exit_type': 'stop_market', 'trigger_price': 90, 'amount': .5, 'reason': 'test'}]}, 'exit')
    stats = stats_service.get_position_stats_payload('cfg')
    assert stats['positions'][0]['qty'] == 1
    assert stats['exit_management']['mode'] == 'independent_exits'
    assert stats['exit_management']['exits'][0]['remaining'] == .5
    assert stats['exit_management']['uncovered']['LONG'] == .5


def test_legacy_cancel_list_stops_after_unknown_result(monkeypatch):
    calls = []

    def cancel(**kwargs):
        calls.append(kwargs['order_id'])
        return '{"status":"unknown","error":"response lost"}'

    monkeypatch.setitem(tool_registry._TOOL_BY_NAME, 'cancel_orders_strategy', SimpleNamespace(func=cancel))
    result = tool_registry.run_trade_tool('cancel_orders_strategy', ['one', 'two'], 'missing-config', 'ETH/USDT')
    assert calls == ['one']
    assert 'not_executed' in result


def test_legacy_multi_order_reports_unexecuted_after_failure(simulation):
    result = call('close_position_strategy', {'orders': [
        {'pos_side': 'LONG', 'exit_type': 'market', 'amount': 1, 'reason': 'no position'},
        {'pos_side': 'SHORT', 'exit_type': 'market', 'amount': 1, 'reason': 'must not execute'},
    ]}, 'failed-multi-close')
    assert result['status'] == 'failed'
    assert [row['status'] for row in result['results']] == ['failed', 'not_executed']
    assert [row['index'] for row in result['results']] == [0, 1]
    assert result['results'][1]['blocked_by_index'] == 0


@pytest.mark.parametrize('tool_name', ['close_position_real', 'execute_trade_actions'])
def test_real_close_skips_confirmed_empty_side_and_continues_batch(simulation, monkeypatch, tool_name):
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: {
        'config_id': 'cfg', 'mode': 'REAL', 'exit_mode': 'independent_exits',
    })
    calls = []

    def close(symbol, **kwargs):
        calls.append((symbol, kwargs['pos_side']))
        if kwargs['pos_side'] == 'LONG':
            return {'status': 'skipped', 'reason': 'no_position', 'symbol': symbol, 'pos_side': 'LONG'}
        return {'status': 'filled', 'id': 'short-exit', 'filled': 1}

    monkeypatch.setattr(agent_tools, '_independent_service', lambda *_: (SimpleNamespace(close=close), True))
    orders = [
        {'pos_side': side, 'exit_type': 'market', 'amount': 0, 'reason': 'close held positions'}
        for side in ('LONG', 'SHORT')
    ]
    payload = {'orders': orders} if tool_name == 'close_position_real' else {
        'actions': [{'action': 'close', 'order': order} for order in orders],
    }
    result = call(tool_name, payload, 'close-both-sides')
    assert result['status'] == 'completed'
    assert [row['index'] for row in result['results']] == [0, 1]
    assert all('blocked_by_index' not in row for row in result['results'])
    if tool_name == 'close_position_real':
        assert [row['status'] for row in result['results']] == ['skipped', 'filled']
        assert result['results'][0]['reason'] == 'no_position'
    else:
        first = json.loads(result['results'][0]['result'])
        assert first['results'][0]['status'] == 'skipped'
        assert first['results'][0]['reason'] == 'no_position'
    assert calls == [('ETH/USDT', 'LONG'), ('ETH/USDT', 'SHORT')]
    assert call(tool_name, payload, 'close-both-sides') == result
    assert len(calls) == 2


@pytest.mark.parametrize('tool_name', ['close_position_real', 'execute_trade_actions'])
@pytest.mark.parametrize('uncertain', [False, True])
def test_real_close_preserves_diagnostic_and_blocks_following_items(simulation, monkeypatch, tool_name, uncertain):
    from backend.utils.independent_exits import IndependentPositionError

    monkeypatch.setattr(config, 'get_config_by_id', lambda _: {
        'config_id': 'cfg', 'mode': 'REAL', 'exit_mode': 'independent_exits',
    })
    details = {
        'symbol': 'ETH/USDT:USDT', 'pos_side': 'LONG', 'cycle_state': None,
        'position_amount': 1, 'next_action': 'Verify this task and position ownership before retrying.',
    }
    calls = []

    def close(symbol, **kwargs):
        calls.append((symbol, kwargs['pos_side']))
        if uncertain:
            raise TimeoutError('Position query unavailable')
        raise IndependentPositionError(
            'No active independent position cycle',
            code='independent_position_cycle_missing', details=details,
        )

    monkeypatch.setattr(agent_tools, '_independent_service', lambda *_: (SimpleNamespace(close=close), True))
    orders = [
        {'pos_side': side, 'exit_type': 'market', 'amount': 0, 'reason': 'close held positions'}
        for side in ('LONG', 'SHORT')
    ]
    payload = {'orders': orders} if tool_name == 'close_position_real' else {
        'actions': [{'action': 'close', 'order': order} for order in orders],
    }
    result = call(tool_name, payload, 'blocked-close')
    expected = 'unknown' if uncertain else 'failed'
    assert result['status'] == expected
    assert [row['status'] for row in result['results']] == [expected, 'not_executed']
    assert [row['index'] for row in result['results']] == [0, 1]
    assert result['results'][1]['blocked_by_index'] == 0
    first = result['results'][0]
    if tool_name == 'execute_trade_actions':
        first = json.loads(first['result'])['results'][0]
    if uncertain:
        assert first['error'] == 'Position query unavailable'
        assert 'error_code' not in first
    else:
        assert first['error_code'] == 'independent_position_cycle_missing'
        assert first['details'] == details
    assert calls == [('ETH/USDT', 'LONG')]
    assert call(tool_name, payload, 'blocked-close') == result
    assert len(calls) == 1
