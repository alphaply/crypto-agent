import copy
import json

import ccxt
import pytest

from backend.utils.independent_admission import assess_admission, continuation_for
from test_trade_tool_integration import simulation, real_short, call


def clean_plan():
    return dict(side='SHORT', state='ACTIVE', entries=[], exits=[], verified_at=100,
                position_observation=dict(status='verified', amount=1, observed_at=100))


def rejected():
    return dict(id=None, client_id='rejected', operation_id='rejected-stop', status='rejected',
                submission_state='not_submitted', exit_type='stop_market', filled=0,
                submission_evidence=dict(source='exchange_rejection', no_execution=True))


@pytest.mark.parametrize('action', ['open', 'close', 'edit', 'cancel'])
def test_unrelated_failure_cannot_change_valid_admission(action):
    plan = clean_plan()
    target = dict(id='target', client_id='target', status='open', fill_verified=True)
    before = assess_admission(plan, action, hedged=True, target=target)
    plan['exits'].extend(copy.deepcopy(rejected()) for _ in range(10))
    plan['error'] = 'old arbitrary diagnostic'
    after = assess_admission(plan, action, hedged=True, target=target)
    assert before['allowed'] and after['allowed']
    assert after['decision'] == 'allow_with_warning'


def test_target_cancel_ignores_unrelated_missing_position():
    plan = clean_plan()
    plan['position_error'] = 'unavailable'
    plan['exits'] = [dict(id=None, client_id='other', status='submitting')]
    target = dict(id='owned', client_id='owned', status='open')
    assert assess_admission(plan, 'cancel', hedged=True, target=target)['allowed']
    assert not assess_admission(plan, 'close', hedged=True)['allowed']
    assert continuation_for(plan)['assessment_state'] == 'needs_refresh'


@pytest.mark.parametrize('query_first', [False, True])
def test_corrected_stop_after_definite_rejection_needs_no_unlock(real_short, query_first):
    service, exchange = real_short
    create = exchange.create_order
    attempts = []
    def reject(*args, **kwargs):
        attempts.append(args)
        raise ccxt.InvalidOrder('binance {"code":-2021,"msg":"rejected"}')
    exchange.create_order = reject
    args = dict(side='SHORT', order_type='stop_market', amount=.2, trigger_price=110, reason='protect')
    first = call('close', args, 'rejected-stop')
    assert first['status'] == 'failed'
    exchange.create_order = create
    if query_first:
        query = call('get_trade_operation', {'target_operation_id': 'rejected-stop'}, 'read-rejected')
        assert query['status'] == 'failed'
        assert query['query']['status'] == 'completed'
        assert 'close' not in query['continuation']['blocked_actions']
    second = call('close', {**args, 'trigger_price': 115}, 'corrected-stop')
    assert second['status'] == 'submitted'
    assert second['admission']['allowed']
    assert len(attempts) == 1 and len(exchange.calls) == 2
    assert service.snapshot('ETH/USDT')['unresolved_orders'] == []


def test_ambiguous_invalid_order_does_not_become_definite_rejection(real_short):
    _, exchange = real_short
    calls = []
    def ambiguous(*args, **kwargs):
        calls.append(args)
        raise ccxt.InvalidOrder('connection interrupted, outcome unknown')
    exchange.create_order = ambiguous
    args = dict(side='SHORT', order_type='market', amount=.2, reason='reduce')
    assert call('close', args, 'ambiguous')['status'] == 'unknown'
    exchange.hide = True
    assert call('close', args, 'another')['admission']['decision'] == 'deny'
    assert len(calls) == 1


def test_missing_cycle_with_confirmed_rejection_preserves_history(real_short):
    from backend import database
    from backend.utils.independent_cycle_recovery import diagnose_cycle
    service, _ = real_short
    plan = service._load('ETH/USDT:USDT', 'SHORT')
    plan['exits'].append(rejected())
    service._save(plan)
    with database.get_db_conn() as conn:
        conn.execute('DELETE FROM real_protection_plans')
        conn.commit()
    diagnosis, candidate = diagnose_cycle(service, 'ETH/USDT:USDT', 'SHORT')
    assert diagnosis['restorable']
    assert candidate['episode_id'] == plan['episode_id']
    assert candidate['exits'][0]['operation_id'] == 'rejected-stop'


@pytest.mark.parametrize('hedged,allowed', [(True, True), (False, False)])
def test_opposite_unknown_only_blocks_shared_net_position(hedged, allowed):
    plan, opposite = clean_plan(), clean_plan()
    opposite.update(side='LONG', entries=[dict(id=None, client_id='pending', operation_id='other', status='submitting')])
    decision = assess_admission(plan, 'close', hedged=hedged, opposite=opposite)
    assert decision['allowed'] is allowed
    if not allowed:
        assert decision['blockers'][0]['operation_id'] == 'other'
    assert assess_admission(plan, 'edit', hedged=hedged)['decision'] == 'not_evaluated'


def test_target_cancel_survives_position_read_failure_and_no_prewrite_maintenance(real_short, monkeypatch):
    service, exchange = real_short
    stop = call('close', dict(side='SHORT', order_type='stop_market', amount=.2, trigger_price=110, reason='protect'), 'stop')
    exchange.position_failure = True
    maintenance = []
    original = service._reconcile
    def reconcile(plan, **kwargs):
        maintenance.append(exchange.orders[stop['id']]['status'])
        return original(plan, **kwargs)
    monkeypatch.setattr(service, '_reconcile', reconcile)
    result = call('cancel', {'order_id': stop['order_id'], 'reason': 'remove'}, 'cancel-stop')
    assert result['status'] == 'completed'
    assert maintenance == ['canceled']
    assert result['admission']['allowed']
    assert len(exchange.calls) == 2


def test_direct_edit_refreshes_its_old_query_error_in_same_call(real_short):
    service, exchange = real_short
    pending = call('open', dict(side='SHORT', amount=.2, price=120, reason='add'), 'add')
    plan, record = service._find_order('ETH/USDT', pending['order_id'])
    record['query_error'] = 'previous transient outage'
    service._save(plan)
    result = call('edit', dict(order_id=pending['order_id'], remaining_amount=.3, price=121, reason='adjust'), 'edit')
    assert result['status'] == 'completed'
    assert result['admission']['allowed']
    assert exchange.orders[pending['id']]['amount'] == .3


def test_pre_submit_generation_conflict_refreshes_once_without_resending(real_short, monkeypatch):
    from backend.utils.position_protection import ConcurrentProtectionUpdate
    service, exchange = real_short
    save = service._save
    conflicts = []
    def conflict_once(plan):
        if not conflicts and any(r.get('operation_id') == 'new-stop' for r in plan['exits']):
            conflicts.append(1)
            raise ConcurrentProtectionUpdate('test stale generation')
        return save(plan)
    monkeypatch.setattr(service, '_save', conflict_once)
    result = call('close', dict(side='SHORT', order_type='stop_market', amount=.2, trigger_price=110, reason='protect'), 'new-stop')
    assert result['status'] == 'submitted' and len(conflicts) == 1
    assert len(exchange.calls) == 2


def test_persistent_pre_submit_conflict_stops_after_one_refresh(real_short, monkeypatch):
    from backend.utils.position_protection import ConcurrentProtectionUpdate
    service, exchange = real_short
    conflicts = []
    def conflict(plan):
        conflicts.append(1)
        raise ConcurrentProtectionUpdate('test repeated conflict')
    monkeypatch.setattr(service, '_save', conflict)
    result = call('close', dict(side='SHORT', order_type='market', amount=.2, reason='reduce'), 'conflict')
    assert result['execution']['submission_state'] == 'not_submitted'
    assert len(conflicts) == 2 and len(exchange.calls) == 1


def test_verified_finished_cleanup_opens_new_cycle_without_scheduler_or_query(real_short):
    service, exchange = real_short
    closed = call('close', dict(side='SHORT', order_type='market', close_all=True, reason='exit'), 'finish-cycle')
    plan = service._load('ETH/USDT:USDT', 'SHORT')
    plan.update(state='EXITING', error='old cleanup diagnostic')
    service._save(plan)
    result = call('open', dict(side='SHORT', amount=.2, price=110, reason='new cycle'), 'new-cycle')
    assert result['status'] == 'submitted' and result['episode_id'] != closed['episode_id']
    assert len(exchange.calls) == 3
