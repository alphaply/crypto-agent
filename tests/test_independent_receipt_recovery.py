import json
from unittest.mock import Mock

import pytest

from backend import database
from backend.database_schema import initialize_schema
from backend.agent import tool_registry
from backend.utils.trade_operations import reconcile_pending_trade_operations, run_once
from test_trade_tool_integration import simulation, real_short, call, recover, native_order_id


def close_args():
    return dict(side='SHORT', order_type='market', close_all=True, reason='exit')


def test_identity_reader_has_no_page_limit_and_preserves_batch_tail(real_short):
    from backend.utils.trade_recovery import operation_evidence
    service, exchange = real_short
    exchange.timeout_next = True
    first = call('execute_trade_actions', {'contract_version': 2, 'actions': [
        {'action': 'close', **close_args()},
        {'action': 'open', 'side': 'SHORT', 'amount': 1, 'price': 120, 'reason': 'later'}]}, 'old-batch')
    current = recover('old-batch')
    with database.get_db_conn() as conn:
        for index in range(25):
            conn.execute('INSERT INTO trade_action_runs (config_id,operation_id,symbol,request_hash,status,result,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                         ('cfg', f'unrelated-{index}', 'ETH/USDT', 'hash', 'completed',
                          json.dumps({'status': 'completed', 'filled_amount': .30}), 0, 9999999999))
        conn.commit()
    refs = ['old-batch', 'old-batch:0', 'old-batch:1', 'missing-.37']
    values = operation_evidence('cfg', 'ETH/USDT', refs, account_scope=service.account_scope)
    assert first['results'][0]['status'] == 'unknown'
    assert values['old-batch']['latest']['results'] == current['results']
    assert values['old-batch:0']['latest']['status'] == 'completed'
    assert values['old-batch:1']['latest']['status'] == 'not_executed'
    assert values['missing-.37']['resolution'] == 'unresolved'
    assert operation_evidence('cfg', 'ETH/USDT', refs, account_scope='other')['old-batch']['resolution'] == 'account_scope_mismatch'
    assert operation_evidence('other-task', 'ETH/USDT', refs, account_scope=service.account_scope)['old-batch']['resolution'] == 'unresolved'


def test_local_maintenance_time_and_durable_watermark(real_short, monkeypatch):
    from backend.utils.trade_recovery import recover_operation
    service, exchange = real_short
    original = call('close', close_args(), 'retired-close')
    for name in ('create_order', 'cancel_order', 'edit_order', 'load_markets', 'fetch_positions', 'fetch_order'):
        monkeypatch.setattr(exchange, name, lambda *a, **k: pytest.fail('local projection contacted exchange'))
    local = recover_operation('cfg', 'ETH/USDT', 'retired-close', read_exchange=False)
    assert local['status'] == original['status'] == 'completed'
    assert local['query']['status'] == 'not_requested'
    assert local['query']['observed_at'] == local['verified_at']
    assert local['continuation']['rules'] == local['continuation']['blocked_actions'] == []
    with database.get_db_conn() as conn:
        stamp = conn.execute("SELECT updated_at FROM trade_action_runs WHERE operation_id='retired-close'").fetchone()[0]
        count = conn.execute("SELECT count(*) FROM trade_receipt_revisions WHERE operation_id='retired-close'").fetchone()[0]
    for _ in range(3):
        assert reconcile_pending_trade_operations('cfg') == 0
    with database.get_db_conn() as conn:
        assert conn.execute("SELECT updated_at FROM trade_action_runs WHERE operation_id='retired-close'").fetchone()[0] == stamp
        assert conn.execute("SELECT count(*) FROM trade_receipt_revisions WHERE operation_id='retired-close'").fetchone()[0] == count


@pytest.mark.parametrize('persistent', [False, True])
def test_query_reloads_concurrent_evidence_without_exchange_writes(real_short, monkeypatch, persistent):
    service, exchange = real_short
    stop = call('close', dict(side='SHORT', order_type='limit', amount=.2, price=90, reason='tp'), 'concurrent-stop')
    reads = []
    collect = service.collect_readonly
    def concurrent_collect(plan):
        collect(plan)
        reads.append(1)
        if persistent or len(reads) == 1:
            fresh = service._load(plan['symbol'], plan['side'])
            fresh['concurrent_marker'] = len(reads)
            service._save(fresh)
        return plan
    monkeypatch.setattr(service, 'collect_readonly', concurrent_collect)
    for name in ('create_order', 'cancel_order', 'edit_order'):
        monkeypatch.setattr(exchange, name, lambda *a, **k: pytest.fail('query submitted a financial write'))
    result = recover('concurrent-stop')
    assert result['status'] == stop['status']
    assert len(reads) == (3 if persistent else 2)
    assert service._load('ETH/USDT:USDT', 'SHORT')['concurrent_marker'] == (3 if persistent else 1)
    if persistent:
        assert result['query']['status'] == 'failed'
        assert result['continuation']['assessment_state'] == 'needs_refresh'
        assert result['continuation']['rules'] == []
        assert result['continuation']['blocked_actions'] == []
    else:
        assert result['query']['status'] == 'completed'


def test_query_failure_does_not_publish_cached_trade_denials(real_short, monkeypatch):
    from backend.utils import trade_recovery
    from backend.utils.trade_operations import refresh_operation_receipt
    previous = {'status': 'failed', 'order_status': 'rejected', 'continuation': {
        'blocked_actions': ['open', 'close', 'edit'], 'rules': [{'action': 'edit', 'target': 'old'}]}}
    monkeypatch.setattr(trade_recovery, 'recover_operation', Mock(side_effect=RuntimeError('cache unavailable')))
    result = refresh_operation_receipt('cfg', 'ETH/USDT', 'old', previous)
    assert result['status'] == 'failed'
    assert result['continuation']['blocked_actions'] == result['continuation']['rules'] == []
    assert result['continuation']['previous_assessment'] == previous['continuation']
    assert result['continuation']['requires_action_assessment']


def agent_call(name, args, operation_id):
    from langchain_core.messages import AIMessage
    from backend.agent.agent_graph import tools_node, AgentState
    message = AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': operation_id}])
    state = AgentState(symbol='ETH/USDT', messages=[message], market_context={}, account_context={})
    result = tools_node(state, {'configurable': {'config_id': 'cfg', 'agent_config': {'mode': 'REAL'}}})
    return json.loads(result.messages[-1].content)


def test_lost_ack_query_is_read_only_and_same_id_never_replays(real_short, monkeypatch):
    service, exchange = real_short
    exchange.timeout_next = True
    original = call('close', close_args(), 'lost-close')
    assert original['status'] == 'unknown'
    writes = len(exchange.calls)
    for name in ['create_order', 'cancel_order', 'edit_order']:
        monkeypatch.setattr(exchange, name, lambda *a, **k: pytest.fail('read recovery wrote to exchange'))
    current = recover('lost-close')
    assert current['status'] == 'completed'
    assert current['filled_amount'] == 1
    replay = call('close', close_args(), 'lost-close')
    assert replay['status'] == 'completed'
    assert len(exchange.calls) == writes
    with database.get_db_conn() as conn:
        initial = conn.execute('SELECT payload FROM trade_receipt_revisions WHERE operation_id=%s AND revision=0', ('lost-close',)).fetchone()[0]
        assert json.loads(json.loads(initial))['status'] == 'unknown'
        assert not conn.execute("SELECT 1 FROM trade_action_runs WHERE operation_id='query-1'").fetchone()


def test_close_cleanup_recovery_then_explicit_new_entry_once(real_short):
    service, exchange = real_short
    stop = call('close', dict(side='SHORT', order_type='stop_market', amount=1, trigger_price=110, reason='protect'), 'stop-1')
    exchange.missing_cancel_fill = True
    original = agent_call('close', close_args(), 'close-1')
    assert original['status'] == 'completed' and original['cleanup']['status'] == 'pending'
    service.reconcile_all()  # Independent maintenance continues after the ACK returned.
    pending = service.snapshot('ETH/USDT')
    assert [r['order_id'] for r in pending['unresolved_orders']] == [stop['order_id']]
    assert pending['exits'] == []
    blocked = call('open', dict(side='SHORT', amount=1, price=100, reason='new cycle'), 'blocked-entry')
    assert blocked['status'] == 'failed'
    exchange.orders[native_order_id(stop)]['filled'] = 0
    query = recover('close-1')
    assert query['status'] == 'completed'
    assert len(exchange.calls) == 3  # entry, protective stop, one market close
    service.reconcile_all()
    reconcile_pending_trade_operations('cfg')
    query = recover('close-1')
    assert query['cleanup']['status'] == 'completed'
    entry_args = dict(side='SHORT', amount=1, price=100, reason='explicit new cycle')
    opened = agent_call('open', entry_args, 'new-entry')
    assert opened['status'] == 'submitted'
    assert opened['episode_id'] != original['episode_id']
    assert call('open', entry_args, 'new-entry')['status'] == 'submitted'
    assert len(exchange.calls) == 4
    historical = recover('close-1')
    assert historical['episode_id'] == original['episode_id']
    assert historical['status'] == 'completed'
    assert historical['current_scope']['episode_id'] == opened['episode_id']
    assert historical['position']['episode_id'] == original['episode_id']
    assert historical['position']['status'] == 'historical'
    assert len(exchange.calls) == 4


def test_read_failure_does_not_prevent_other_order_evidence(real_short):
    service, exchange = real_short
    first = call('close', dict(side='SHORT', order_type='limit', amount=.3, price=90, reason='TP1'), 'tp-1')
    second = call('close', dict(side='SHORT', order_type='limit', amount=.3, price=80, reason='TP2'), 'tp-2')
    fetch = exchange.fetch_order
    def query(oid, *args, **kwargs):
        if str(oid) == native_order_id(first):
            raise TimeoutError('first read unavailable')
        return fetch(oid, *args, **kwargs)
    exchange.fetch_order = query
    exchange.orders[native_order_id(second)].update(status='closed', filled=.3)
    exchange.quantity = .7
    result = recover('tp-2')
    assert result['status'] == 'completed'
    assert result['filled_amount'] == .3
    assert result['position']['amount'] == .7
    assert result['query']['status'] == 'partial'


def test_read_tool_trusted_context_and_unknown_id(real_short):
    assert recover('someone-else')['error_code'] == 'operation_not_found'
    removed=json.loads(tool_registry.run_trade_tool('get_trade_operation', {'target_operation_id':'any','config_id':'other'},'cfg','ETH/USDT'))
    assert removed['error_code']=='tool_removed'
    with pytest.raises(ValueError,match='trusted context'):
        call('close',{**close_args(),'config_id':'other'},'conflicting-close')


def test_failed_query_preserves_confirmed_execution_and_marks_advice_stale(real_short, monkeypatch):
    from backend.utils import trade_recovery
    result = call('close', close_args(), 'confirmed-close')
    assert result['status'] == 'completed'
    monkeypatch.setattr(trade_recovery, 'recover_operation', Mock(side_effect=RuntimeError('read cache conflict')))
    query = recover('confirmed-close')
    assert query['status'] == 'completed' and query['filled_amount'] == result['filled_amount']
    assert query['query']['status'] == 'failed'
    assert query['continuation']['assessment_state'] == 'needs_refresh'


def test_cancel_waits_for_own_terminal_fill_and_edit_for_successor(real_short):
    service, exchange = real_short
    stop = call('close', dict(side='SHORT', order_type='stop_market', amount=1, trigger_price=110, reason='protect'), 'stop-cancel')
    exchange.missing_cancel_fill = True
    canceled = call('cancel', dict(order_id=stop['order_id'], reason='cancel'), 'cancel-op')
    assert canceled['status'] == 'completed'  # Exchange cancellation ACK, independent of fill accounting.
    assert recover('cancel-op')['status'] == 'pending'
    exchange.orders[native_order_id(stop)]['filled'] = 0
    assert recover('cancel-op')['status'] == 'completed'
    service.reconcile_all()
    exchange.missing_cancel_fill = False
    stop = call('close', dict(side='SHORT', order_type='stop_market', amount=1, trigger_price=110, reason='protect'), 'stop-edit')
    exchange.timeout_next = True
    edited = call('edit', dict(order_id=stop['order_id'], trigger_price=115, reason='replace'), 'edit-op')
    assert edited['status'] == 'unknown'
    assert exchange.orders[native_order_id(stop)]['status'] == 'canceled'
    writes = len(exchange.calls)
    exchange.hide = True
    assert recover('edit-op')['status'] == 'unknown'
    exchange.hide = False
    recovered = recover('edit-op')
    assert recovered['status'] == 'completed'
    assert recovered['order_id'] != stop['order_id']
    assert len(exchange.calls) == writes


def test_batch_query_keeps_unexecuted_tail_and_hash(real_short):
    _, exchange = real_short
    exchange.timeout_next = True
    args = {'contract_version': 2, 'actions': [
        {'action': 'close', **close_args()},
        {'action': 'open', 'side': 'SHORT', 'amount': 1, 'price': 120, 'reason': 'later'}]}
    first = call('execute_trade_actions', args, 'recovery-batch')
    assert [item['status'] for item in first['results']] == ['unknown', 'not_executed']
    result = recover('recovery-batch')
    assert result['results'][0]['status'] == 'completed'
    assert result['results'][1] == first['results'][1]
    replay = call('execute_trade_actions', args, 'recovery-batch')
    assert replay['results'][1]['status'] == 'not_executed'
    assert len(exchange.calls) == 2


def test_missing_cycle_query_diagnoses_but_does_not_restore(real_short):
    service, exchange = real_short
    call('close', dict(side='SHORT', order_type='market', amount=.2, reason='reduce'), 'partial-close')
    with database.get_db_conn() as conn:
        conn.execute("DELETE FROM real_protection_plans WHERE config_id='cfg'")
        conn.commit()
    before = len(exchange.calls)
    result = recover('partial-close')
    assert result['cycle_diagnosis']['restorable'] is True
    with database.get_db_conn() as conn:
        assert not conn.execute("SELECT 1 FROM real_protection_plans WHERE config_id='cfg'").fetchone()
    assert len(exchange.calls) == before
    closed = call('close', dict(side='SHORT', order_type='market', close_all=True, reason='finish residual'), 'restored-close')
    assert closed['status'] == 'completed'
    assert closed['episode_id'] == result['episode_id']
    assert closed['filled_amount'] == pytest.approx(.8)
    assert len(exchange.calls) == before + 1


@pytest.mark.parametrize('case', ['quantity', 'history', 'ownership'])
def test_missing_cycle_rejects_incomplete_or_conflicting_evidence(real_short, case):
    service, exchange = real_short
    from backend.utils.independent_cycle_recovery import diagnose_cycle
    if case == 'quantity':
        exchange.quantity = 2
    with database.get_db_conn() as conn:
        conn.execute("DELETE FROM real_protection_plans WHERE config_id='cfg'")
        if case == 'history':
            stored = conn.execute('SELECT payload FROM trade_cycle_evidence').fetchone()[0]
            plan = json.loads(stored)
            plan['history_complete'] = False
            conn.execute('UPDATE trade_cycle_evidence SET payload=%s', (json.dumps(plan),))
        if case == 'ownership':
            conn.execute('DELETE FROM execution_order_links')
        conn.commit()
    before = len(exchange.calls)
    result = call('close', close_args(), 'missing-bad-close')
    assert result['status'] == 'failed'
    assert result['error_code'] == 'independent_position_cycle_missing'
    assert result['details']['reason_code'] == {'quantity': 'quantity_mismatch', 'history': 'incomplete_history', 'ownership': 'incomplete_ownership_evidence'}[case]
    assert len(exchange.calls) == before


@pytest.mark.parametrize('failure', ['rejection', 'timeout'])
def test_native_edit_refusal_or_lost_ack_preserves_original_order(real_short, failure):
    import ccxt
    service, exchange = real_short
    opening = call('open', dict(side='SHORT', amount=.2, price=120, reason='add'), 'native-entry')
    edit = exchange.edit_order
    count = []
    def failing_edit(*args, **kwargs):
        count.append(args[0])
        if failure == 'rejection':
            raise ccxt.InvalidOrder('{"code":-2010,"msg":"native amendment rejected"}')
        edit(*args, **kwargs)
        raise ccxt.RequestTimeout('native amendment ACK lost')
    exchange.edit_order = failing_edit
    result = call('edit', dict(order_id=opening['order_id'], price=125, reason='adjust'), 'native-edit')
    assert result['status'] == ('failed' if failure == 'rejection' else 'unknown')
    assert result['order_id'] == opening['order_id']
    assert result['execution']['phase'] == 'amend'
    current = recover('native-edit')
    assert current['status'] == ('failed' if failure == 'rejection' else 'completed')
    assert count == [native_order_id(opening)] and len(exchange.calls) == 2


def test_changed_account_cannot_reuse_original_evidence(real_short, monkeypatch):
    from backend.agent import agent_tools
    from backend.utils.independent_exits import IndependentExits
    from types import SimpleNamespace
    _, exchange = real_short
    result = call('close', close_args(), 'old-account-close')
    assert result['status'] == 'completed'
    exchange.apiKey = 'different-offline-account'
    replacement = IndependentExits(SimpleNamespace(exchange=exchange, config_id='cfg'))
    monkeypatch.setattr(agent_tools, '_independent_service', lambda *_: (replacement, True))
    result = recover('old-account-close')
    assert result['error_code'] == 'account_scope_mismatch' and result['status'] == 'unknown'
    assert len(exchange.calls) == 2


@pytest.mark.parametrize('hedged', [True, False])
def test_admission_isolates_hedge_but_blocks_one_way_shared_unknown(real_short, hedged):
    from backend.utils.independent_admission import assess_admission
    clean = {'side': 'LONG', 'state': 'ACTIVE', 'entries': [], 'exits': [],
             'position_observation': {'status': 'verified', 'amount': 1}}
    blocked = {'side': 'SHORT', 'state': 'EXITING', 'exits': [
        {'id': 'old-all', 'quantity_mode': 'all', 'order_type': 'stop_market', 'status': 'canceled',
         'fill_pending': True, 'operation_id': 'old-stop'}]}
    result = assess_admission(clean, 'open', hedged=hedged, opposite=blocked)
    assert result['allowed'] is hedged
    if not hedged:
        assert result['reason_code'] == 'one_way_shared_position_unverified'
    assert not assess_admission(blocked, 'open', hedged=hedged)['allowed']


def test_late_verified_residual_can_be_explicitly_managed(real_short):
    service, exchange = real_short
    opening = call('open', dict(side='SHORT', amount=.3, price=120, reason='add'), 'residual-add')
    cancel = exchange.cancel_order
    def late(oid, *args, **kwargs):
        if str(oid) == native_order_id(opening):
            exchange.orders[str(oid)]['filled'] = .3
            exchange.quantity += .3
        return cancel(oid, *args, **kwargs)
    exchange.cancel_order = late
    acknowledgement=call('close',close_args(),'residual-first-close')
    assert acknowledgement['position']['amount'] is None
    assert acknowledgement['position']['status']=='stale'
    service.reconcile_all()
    assert recover('residual-first-close')['position']['amount']==pytest.approx(.3)
    stop = call('close', dict(side='SHORT', order_type='stop_market', amount=.3, trigger_price=110, reason='protect residual'), 'residual-protect')
    assert stop['status'] == 'submitted'
    final = call('close', close_args(), 'residual-explicit-close')
    assert final['status'] == 'completed' and final['filled_amount'] == pytest.approx(.3)
    market_calls = [r for r in exchange.calls if r['type'] == 'market' and 'stopLossPrice' not in r['params']]
    assert [r['amount'] for r in market_calls] == [1, .3]


def test_permission_denial_is_preflight_without_financial_write(real_short, monkeypatch):
    from backend.mcp import guard
    _, exchange = real_short
    def deny(*args):
        raise PermissionError('synthetic permission denied')
    monkeypatch.setattr(guard, 'preflight_tool', deny)
    result = call('close', close_args(), 'denied-close')
    assert result['status'] == 'failed' and result['execution']['submission_state'] == 'not_submitted'
    assert result['execution']['phase'] == 'preflight' and len(exchange.calls) == 1


@pytest.mark.parametrize('case', ['successor', 'competing_owner', 'read_failure'])
def test_missing_cycle_never_restores_conflicting_or_unreadable_history(real_short, case):
    from backend.utils.independent_cycle_recovery import diagnose_cycle
    service, exchange = real_short
    current = service._load('ETH/USDT:USDT', 'SHORT')
    before = len(exchange.calls)
    if case == 'successor':
        current = {**current, 'episode_id': 'later-cycle', 'state': 'DONE'}
    elif case == 'competing_owner':
        with database.get_db_conn() as conn:
            conn.execute('INSERT INTO real_protection_plans (config_id,symbol,side,payload) VALUES(%s,%s,%s,%s)',
                         ('other-task', current['symbol'], 'SHORT', json.dumps(current)))
            conn.commit()
    else:
        exchange.hide = True
    diagnosis, restored = diagnose_cycle(service, current['symbol'], 'SHORT', current)
    assert restored is None and diagnosis['restorable'] is False
    assert diagnosis['reason_code'] == {
        'successor': 'successor_cycle_exists', 'competing_owner': 'competing_ownership',
        'read_failure': 'history_read_unverified'}[case]
    assert len(exchange.calls) == before


def test_persistent_unknown_blocks_dependent_writes_after_read_query(real_short):
    _, exchange = real_short
    exchange.timeout_next = True
    assert call('close', close_args(), 'still-unknown')['status'] == 'unknown'
    exchange.hide = True
    before = len(exchange.calls)
    for _ in range(2):
        receipt = recover('still-unknown')
        assert receipt['status'] == 'unknown'
        assert receipt['recovery']['last_error']
        assert receipt['continuation']['assessment_state'] == 'needs_refresh'
        assert receipt['continuation']['blocked_actions'] == []
    next_trade = call('open', dict(side='SHORT', amount=1, price=100, reason='blocked'), 'new-after-unknown')
    assert next_trade['status'] == 'failed'
    assert next_trade['execution']['submission_state'] == 'not_submitted'
    assert len(exchange.calls) == before


@pytest.mark.parametrize('triggered', [False, True])
def test_recovered_all_keeps_plan_null_and_actual_fill_separate(tmp_path, monkeypatch, triggered):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'recovery.sqlite'))
    with database.get_db_conn() as conn:
        pass  # PostgreSQL migrations are installed by financial_pg.
    callback = Mock(side_effect=TimeoutError('lost ack'))
    request = {'tool': 'close', 'side': 'LONG', 'order_type': 'stop_market', 'close_all': True}
    run_once('cfg', 'ETH/USDT', 'op', request, callback)
    record = {'id': '42', 'operation_id': 'op', 'status': 'open', 'contract_version': 2,
              'order_type': 'stop_market', 'quantity_mode': 'all', 'amount': None, 'filled': 1 if triggered else 0,
              'waiting_trigger': not triggered, 'fill_verified': triggered, 'triggered': triggered}
    if triggered:
        record.update(execution_order_id='43', execution_amount=3, resolved_amount=.3)
    plan = {'state': 'ACTIVE', 'side': 'LONG', 'execution_mode': 'independent_exits', 'contract_size': .1, 'exits': [record]}
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO real_protection_plans (config_id,symbol,side,payload) VALUES(%s,%s,%s,%s)', ('cfg', 'ETH/USDT:USDT', 'LONG', json.dumps(plan)))
        conn.commit()
    receipt = run_once('cfg', 'ETH/USDT', 'op', request, callback)
    evidence = receipt['reconciliation_evidence'][0]
    assert receipt['status'] == 'submitted' and callback.call_count == 1
    assert evidence['amount'] is None and evidence['remaining'] is None
    assert evidence['quantity_unit'] == 'base'
    assert evidence['order_id'] == ('regular:43' if triggered else 'algo:42')
    assert evidence['order_status'] == ('partially_filled' if triggered else 'waiting_trigger')
    assert evidence['filled_amount'] == (.1 if triggered else None)
    assert evidence['actual_remaining'] == (pytest.approx(.2) if triggered else None)


def test_failed_replacement_does_not_recover_as_successful_cancel(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'replacement.sqlite'))
    with database.get_db_conn() as conn:
        pass  # PostgreSQL migrations are installed by financial_pg.
    callback = Mock(side_effect=TimeoutError('lost outer receipt'))
    request = {'tool': 'edit', 'order_id': 'algo:42', 'remaining_amount': .5}
    run_once('cfg', 'ETH/USDT', 'edit-op', request, callback)
    plan = {'state': 'ACTIVE', 'side': 'LONG', 'execution_mode': 'independent_exits', 'contract_size': 1,
            'exits': [{'id': '42', 'operation_id': 'original', 'last_operation_id': 'edit-op', 'status': 'canceled',
                       'order_type': 'stop_limit', 'contract_version': 2, 'amount': .7, 'filled': 0,
                       'replacement_error': 'Replacement quantity exceeds current position'}]}
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO real_protection_plans (config_id,symbol,side,payload) VALUES(%s,%s,%s,%s)', ('cfg', 'ETH/USDT:USDT', 'LONG', json.dumps(plan)))
        conn.commit()
    receipt = run_once('cfg', 'ETH/USDT', 'edit-op', request, callback)
    assert receipt['status'] == 'failed' and callback.call_count == 1
    assert receipt['reconciliation_evidence'][0]['order_status'] == 'canceled'
    assert 'exceeds current position' in receipt['reconciliation_evidence'][0]['error']


@pytest.fixture(autouse=True)
def _postgres_storage(financial_pg):
    """Run original financial contracts against an isolated PostgreSQL schema."""
    yield
