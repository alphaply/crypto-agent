"""Offline v3 contracts, ordering, durable identity, recovery and model binding."""
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ValidationError

from backend import database
from backend.agent import agent_tools, chat_graph, independent_tools, tool_registry, unified_trading as unified
from backend.agent.trading_contracts import UnifiedTradeActionsSchema
from backend.config import config
from backend.database_schema import initialize_schema
from test_spot_trade_execution import spot_runtime


CFG = {'config_id': 'cfg', 'mode': 'STRATEGY', 'exit_mode': 'independent_exits', 'symbol': 'ETH/USDT'}


def batch(*actions, **extra):
    return {'contract_version': 3, 'actions': list(actions), **extra}


def cancel(identity='regular:1', **extra):
    return {'action': 'cancel', 'order_id': identity, 'reason': 'remove', **extra}


def opening(**extra):
    return {'action': 'open', 'side': 'LONG', 'amount': .2, 'price': 100, 'reason': 'entry', **extra}


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'unified.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: dict(CFG))
    monkeypatch.setattr(agent_tools, 'MarketTool', lambda **_: SimpleNamespace(exchange=SimpleNamespace(fetch_ticker=lambda _: {'last': 100})))


def execute(payload, dispatch, parent='parent', **extra):
    return unified.execute_v3_actions(payload, config_id='cfg', symbol='ETH/USDT', operation_id=parent,
                                      config=CFG, dispatch=dispatch, **extra)


@pytest.mark.parametrize('version', [None, 1, 2, True, '3', 3.0, 4])
def test_fresh_version_is_explicit_and_strict(version):
    with pytest.raises(ValidationError):
        UnifiedTradeActionsSchema.model_validate(batch(cancel(), contract_version=version))


@pytest.mark.parametrize('actions', [[], [cancel()] * 21])
def test_bounded_action_array(actions):
    with pytest.raises(ValidationError):
        UnifiedTradeActionsSchema.model_validate(batch(*actions))


@pytest.mark.parametrize('action', [
    opening(amount=True), opening(amount='1'), opening(amount=float('inf')), opening(amount=0),
    opening(quote_amount=20), opening(amount=None), opening(side='BUY'), opening(tool='execute_trade'),
    opening(symbol='${results[0].symbol}'), cancel(config_id='other'), cancel(engine='paper'),
    {'action': 'edit', 'order_id': 'regular:1', 'reason': 'edit'},
    {'action': 'edit', 'order_id': 'regular:1', 'reason': 'edit', 'remaining_amount': 0},
    {'action': 'close', 'side': 'LONG', 'order_type': 'market', 'reason': 'close', 'amount': 1, 'close_all': True},
    {'action': 'close', 'side': 'LONG', 'order_type': 'market', 'reason': 'close', 'close_all': False},
    {'action': 'close', 'side': 'LONG', 'order_type': 'limit', 'reason': 'close', 'amount': 1},
    {'action': 'protect', 'side': 'LONG', 'reason': 'protect'},
])
def test_unknown_fields_invalid_sizing_and_quantity_fail(action):
    with pytest.raises(ValidationError):
        UnifiedTradeActionsSchema.model_validate(batch(action))


@pytest.mark.parametrize('dependencies', [None, [True], ['0'], [-1], [1], [0, 0]])
def test_dependency_indices_cannot_be_ambiguous(dependencies):
    with pytest.raises(ValidationError):
        UnifiedTradeActionsSchema.model_validate(batch(cancel(), cancel('regular:2', depends_on=dependencies), execution_policy='independent'))


@pytest.mark.parametrize('mode,exit_mode,protect', [
    ('REAL', 'independent_exits', False), ('STRATEGY', 'independent_exits', False),
    ('REAL', 'attached_required', True), ('REAL', 'attached_optional', True),
    ('STRATEGY', 'attached_required', True), ('STRATEGY', 'attached_optional', True),
    ('SPOT_DCA', 'independent_exits', True),
])
def test_published_mode_schema_and_default_catalog(mode, exit_mode, protect):
    cfg = {'mode': mode, 'exit_mode': exit_mode, 'symbol': 'ETH/USDT'}
    tools = tool_registry.get_trade_tools_for_mode(mode, cfg)
    assert [tool.name for tool in tools] == ['execute_trade_actions', 'get_trade_operation']
    schema = tools[0].args_schema
    mapping = schema['properties']['actions']['items']['discriminator']['mapping']
    assert set(mapping) == {'open', 'close', 'edit', 'cancel'} | ({'protect'} if protect else set())
    entry = schema['$defs']['UnifiedOpenAction']
    assert entry['properties']['order_type']['const'] == 'limit'
    assert 'quote_amount' not in entry['properties'] and 'exchange' not in entry['properties']
    assert ('stop_loss' in entry['properties']) == (mode != 'SPOT_DCA' and exit_mode != 'independent_exits')
    assert ('stop_loss' in entry['required']) == (mode != 'SPOT_DCA' and exit_mode == 'attached_required')
    assert schema['properties']['contract_version']['const'] == 3
    assert [tool.name for tool in chat_graph._get_chat_tools(cfg)] == [tool.name for tool in tools]
    assert not chat_graph._get_chat_tools({**cfg, 'read_only': True})


@pytest.mark.parametrize('engine,market,sizing', [('paper', 'spot', {'quote_amount': 20}), ('paper', 'swap', {'margin_amount': 20, 'leverage': 5})])
def test_paper_market_capability_does_not_expand_live(engine, market, sizing):
    action = {'action': 'open', 'side': 'LONG', 'order_type': 'market', 'reason': 'buy', 'symbol': 'ETH/USDT', 'market_type': market, **sizing}
    parsed = unified.validate_for_config(batch(action), {'mode': 'PAPER', 'markets': ['spot', 'swap']}, engine=engine)
    assert parsed.actions[0].order_type == 'market'
    with pytest.raises(ValueError):
        unified.validate_for_config(batch(action), CFG)


@pytest.mark.parametrize('action', [
    {'action': 'open', 'side': 'LONG', 'amount': 1, 'order_type': 'market', 'reason': 'buy', 'market_type': 'swap'},
    {'action': 'open', 'side': 'LONG', 'symbol': 'ETH/USDT', 'amount': 1, 'order_type': 'market', 'reason': 'buy', 'market_type': 'swap', 'leverage': 1.5},
    {'action': 'open', 'side': 'SHORT', 'symbol': 'ETH/USDT', 'amount': 1, 'order_type': 'market', 'reason': 'buy', 'market_type': 'spot'},
])
def test_known_paper_capabilities_are_static(action):
    with pytest.raises(ValueError):
        unified.validate_for_config(batch(action), {'mode': 'PAPER', 'markets': ['spot', 'swap']}, engine='paper')


def test_trusted_symbol_boundary_and_unique_default():
    assert unified.validate_for_config(batch(cancel()), CFG).actions[0].symbol == 'ETH/USDT'
    with pytest.raises(ValueError, match='multi-symbol'):
        unified.validate_for_config(batch(cancel()), {**CFG, 'symbols': ['ETH/USDT', 'BTC/USDT']}, default_symbol='ETH/USDT')
    with pytest.raises(ValueError, match='scope'):
        unified.validate_for_config(batch(cancel(symbol='BTC/USDT')), CFG)
    unrestricted = {**CFG, 'mode': 'REAL', 'symbol': None, 'mcp_symbol_scope': 'all'}
    assert unified.validate_for_config(batch(cancel(symbol='BTC/USDT:USDT')), unrestricted).actions[0].symbol == 'BTC/USDT:USDT'
    assert unified.validate_for_config(batch(cancel(symbol='ETH/USDT:USDT')), {**CFG, 'mode': 'REAL'}).actions[0].symbol == 'ETH/USDT:USDT'


def test_cancel_first_dynamic_rejection_keeps_completed_prefix(ledger):
    dispatch = Mock(side_effect=[{'receipt_version': 1, 'status': 'completed', 'order_status': 'canceled'},
                                 {'receipt_version': 1, 'status': 'failed', 'error_code': 'entry_gate'}])
    payload = batch(cancel(), opening(), cancel('regular:3'))
    result = execute(payload, dispatch)
    assert [item['status'] for item in result['results']] == ['completed', 'failed', 'not_executed']
    assert result['partial_execution'] and result['results'][2]['blocked_by_index'] == 1
    assert [call.args[0].action for call in dispatch.call_args_list] == ['cancel', 'open']
    assert execute(payload, dispatch) == result
    assert dispatch.call_count == 2


def test_whole_batch_static_failure_has_zero_dispatch_and_claim(ledger):
    dispatch = Mock()
    result = execute(batch(cancel(), opening(symbol='BTC/USDT')), dispatch)
    assert result['status'] == 'failed' and result['phase'] == 'preflight'
    dispatch.assert_not_called()
    with database.get_db_conn() as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='unified_trade_children'").fetchone()


@pytest.mark.parametrize('status', ['failed', 'pending', 'unknown'])
def test_default_error_stop_and_uncertain_replay(ledger, status):
    dispatch = Mock(side_effect=[{'receipt_version': 1, 'status': status}])
    payload = batch(cancel(), cancel('regular:2'))
    result = execute(payload, dispatch)
    assert [row['status'] for row in result['results']] == [status, 'not_executed']
    assert execute(payload, dispatch)['results'] == result['results']
    dispatch.assert_called_once()


@pytest.mark.parametrize('first,expected_calls', [('completed', 3), ('submitted', 2)])
def test_explicit_dependency_requires_completed_and_independent_continues(ledger, first, expected_calls):
    dispatch = Mock(return_value={'receipt_version': 1, 'status': first})
    payload = batch(cancel(), cancel('regular:2', depends_on=[0]), cancel('regular:3'), execution_policy='independent')
    result = execute(payload, dispatch)
    assert dispatch.call_count == expected_calls
    if first == 'submitted':
        assert result['results'][1]['status'] == 'not_executed'
        assert result['results'][1]['blocked_by_indices'] == [0]


def test_long_external_parent_has_bounded_domain_specific_children(ledger):
    parent = 'a' * 160
    result = execute(batch(cancel(), cancel('regular:2')), Mock(return_value={'receipt_version': 1, 'status': 'completed'}), parent)
    assert all(len(row['operation_id']) <= 160 for row in result['results'])
    assert unified.stable_child_operation_id('other', parent, 0) != result['results'][0]['operation_id']
    with database.get_db_conn() as conn:
        rows = conn.execute('SELECT action_index,child_operation_id FROM unified_trade_children ORDER BY action_index').fetchall()
    assert [row['child_operation_id'] for row in rows] == [row['operation_id'] for row in result['results']]


def test_conflicting_parent_intent_and_context_never_dispatch(ledger):
    dispatch = Mock(return_value={'receipt_version': 1, 'status': 'completed'})
    execute(batch(cancel()), dispatch)
    with pytest.raises(ValueError):
        execute(batch(cancel('regular:changed')), dispatch)
    with pytest.raises(ValueError):
        unified.execute_v3_actions(batch(cancel()), config_id='cfg', symbol='BTC/USDT', config=CFG, operation_id='parent', dispatch=dispatch)
    dispatch.assert_called_once()


def test_inflight_duplicate_never_executes_or_observes_unstarted_child_as_done(ledger):
    competing = Mock(side_effect=AssertionError('duplicate financial callback'))
    def first(action, index, child, symbol):
        duplicate = execute(batch(cancel(), cancel('regular:2')), competing)
        assert duplicate['status'] == 'pending'
        assert duplicate['results'][index]['status'] == 'pending'
        assert all(row['status'] == 'completed' for row in duplicate['results'][:index])
        return {'receipt_version': 1, 'status': 'completed'}
    result = execute(batch(cancel(), cancel('regular:2')), first)
    assert result['status'] == 'completed'
    competing.assert_not_called()


def test_worker_crash_recovers_attempted_items_without_tail_replay(ledger):
    dispatch = Mock(side_effect=[{'receipt_version': 1, 'status': 'completed'}, SystemExit('worker stopped')])
    payload = batch(cancel(), cancel('regular:2'), cancel('regular:3'))
    with pytest.raises(SystemExit):
        execute(payload, dispatch)
    recovered = unified.query_unified_operation('cfg', 'ETH/USDT', 'parent', read_exchange=False)
    assert [row['status'] for row in recovered['results']] == ['completed', 'unknown', 'not_executed']
    execute(payload, dispatch)
    assert dispatch.call_count == 2


def test_crashed_v3_direct_replay_bypasses_legacy_parent_merger(ledger):
    from backend.database_independent import initialize_independent_schema
    def dispatch(action, index, child, symbol):
        if index:
            raise SystemExit('stopped')
        with database.get_db_conn() as conn:
            initialize_independent_schema(conn.cursor())
            conn.execute('INSERT INTO mock_trade_operations(config_id,symbol,operation_id,result,request_json) VALUES(?,?,?,?,?)',
                         ('cfg', symbol, child, json.dumps({'status': 'completed'}), '{}'))
            conn.commit()
        return {'receipt_version': 1, 'status': 'completed'}
    payload = batch(cancel(), cancel('regular:2'), cancel('regular:3'))
    with pytest.raises(SystemExit):
        execute(payload, dispatch)
    replay = execute(payload, Mock(side_effect=AssertionError('tail must not resume')))
    assert replay['contract_version'] == 3
    assert [row['status'] for row in replay['results']] == ['completed', 'unknown', 'not_executed']


def test_child_financial_commit_survives_lost_outer_receipt(ledger):
    from backend.database_independent import initialize_independent_schema
    def dispatch(action, index, child, symbol):
        with database.get_db_conn() as conn:
            initialize_independent_schema(conn.cursor())
            conn.execute('INSERT INTO mock_trade_operations(config_id,symbol,operation_id,result,request_json) VALUES(?,?,?,?,?)',
                         ('cfg', symbol, child, json.dumps({'status': 'completed', 'filled_amount': .2}), '{}'))
            conn.commit()
        raise TimeoutError('outer response lost')
    execute(batch(cancel(), cancel('regular:2')), dispatch)
    result = unified.query_unified_operation('cfg', 'ETH/USDT', 'parent', read_exchange=False)
    assert result['results'][0]['status'] == 'completed'
    assert result['results'][0]['result']['filled_amount'] == .2
    assert result['results'][1]['status'] == 'not_executed'


def test_account_summary_failure_preserves_financial_status_and_is_single(ledger):
    dispatch = Mock(return_value={'receipt_version': 1, 'status': 'completed', 'account': {'balance': 99}})
    summary = Mock(side_effect=TimeoutError('read failed'))
    result = execute(batch(*[cancel(str(i)) for i in range(20)], include=['account']), dispatch, account_summary=summary)
    assert result['status'] == 'completed' and result['account_summary']['status'] == 'failed'
    summary.assert_called_once()
    compact = unified.compact_trade_receipt(result)
    assert 'account' not in compact['results'][0]['result']
    assert sum('account_summary' in row for row in compact['results']) == 0


def test_compact_projection_keeps_unknown_identity_quantity_and_cleanup_truth():
    raw = {'operation_id': 'parent', 'status': 'unknown', 'receipt_version': 1, 'huge_market_context': 'x' * 10000,
           'results': [{'index': 0, 'action': 'close', 'operation_id': 'parent:0', 'status': 'unknown', 'result': {
               'operation_id': 'parent:0', 'status': 'unknown', 'order_status': 'filled', 'filled_amount': .2,
               'quantity_mode': 'all', 'client_id': 'original', 'cleanup': {'status': 'pending', 'blocking_order_ids': ['algo:old']},
               'recovery': {'target_operation_id': 'parent:0'}, 'account': {'balance': 123}}},
                       {'index': 1, 'action': 'open', 'status': 'not_executed', 'blocked_by_indices': [0]}]}
    compact = unified.compact_trade_receipt(raw)
    assert compact['results'][0]['result']['status'] == 'unknown'
    assert compact['results'][0]['result']['order_status'] == 'filled'
    assert compact['results'][0]['result']['cleanup']['blocking_order_ids'] == ['algo:old']
    assert compact['results'][1]['blocked_by_indices'] == [0]
    assert len(json.dumps(compact)) < len(json.dumps(raw)) / 4
    assert raw['results'][0]['result']['account']['balance'] == 123


def test_attached_strategy_open_edit_protect_cancel_replay_is_atomic(ledger, monkeypatch):
    cfg = {**CFG, 'exit_mode': 'attached_optional'}
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: cfg)
    def call(payload, identity):
        return json.loads(tool_registry.run_trade_tool('execute_trade_actions', payload, 'cfg', 'ETH/USDT', identity))
    opened = call(batch(opening(stop_loss=90, take_profit=110)), 'open')
    assert opened['status'] == 'submitted'
    order = opened['results'][0]['result']['order_id']
    edited = call(batch({'action': 'edit', 'order_id': order, 'remaining_amount': .3, 'reason': 'resize'}), 'edit')
    assert edited['status'] == 'completed'
    protected = call(batch({'action': 'protect', 'order_id': order, 'side': 'LONG', 'take_profit': 120, 'reason': 'protect'}), 'protect')
    assert protected['results'][0]['result']['stop_loss'] == 90
    canceled = call(batch(cancel(order)), 'cancel')
    assert canceled['results'][0]['result']['order_status'] == 'canceled'
    assert call(batch(opening(stop_loss=90, take_profit=110)), 'open') == opened
    with database.get_db_conn() as conn:
        row = conn.execute('SELECT amount,price,stop_loss,take_profit,status FROM mock_orders').fetchone()
        assert list(row) == [.3, 100, 90, 120, 'CANCELLED']
        assert conn.execute('SELECT COUNT(*) FROM mock_trade_operations').fetchone()[0] == 4


def test_deleted_task_cannot_default_new_v3_write_to_simulation(ledger, monkeypatch):
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: None)
    result = json.loads(tool_registry.run_trade_tool('execute_trade_actions', batch(opening(stop_loss=90, take_profit=110)),
                                                     'retired-real', 'ETH/USDT', 'new-after-delete'))
    assert result['status'] == 'failed'
    assert result['submitted'] is False
    assert result['error_code'] == 'configuration_unavailable'
    assert result['results'][0]['status'] == 'not_executed'
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM mock_orders').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM mock_trade_operations').fetchone()[0] == 0


def test_deleted_task_v3_replay_uses_original_intent_without_new_write(ledger, monkeypatch):
    payload = batch(opening())
    dispatch = Mock(return_value={'status': 'submitted', 'order_id': 'regular:original'})
    original = unified.execute_v3_actions(payload, config_id='cfg', symbol='ETH/USDT',
                                          operation_id='before-delete', dispatch=dispatch)
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: None)
    repeated = unified.execute_v3_actions(payload, config_id='cfg', symbol='ETH/USDT',
                                          operation_id='before-delete', dispatch=dispatch)
    assert repeated['results'][0]['result']['order_id'] == 'regular:original'
    assert repeated['results'][0]['operation_id'] == original['results'][0]['operation_id']
    assert dispatch.call_count == 1
    with pytest.raises(ValueError, match='操作 ID'):
        unified.execute_v3_actions(batch(opening(amount=.3)), config_id='cfg', symbol='ETH/USDT',
                                   operation_id='before-delete', dispatch=dispatch)


def test_current_task_deletion_rejects_stale_v3_dispatch_before_market_tool(ledger, monkeypatch):
    from backend.agent.unified_dispatch import dispatch_live_action
    stale = {**CFG, 'mode': 'REAL'}
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: None)
    market_tool = Mock(side_effect=AssertionError('Must reject before exchange or management access'))
    monkeypatch.setattr(agent_tools, 'MarketTool', market_tool)
    action = UnifiedTradeActionsSchema.model_validate(batch(opening())).actions[0]
    result = dispatch_live_action(action, 'cfg', 'ETH/USDT', stale, 'old-worker:0')
    assert result['status'] == 'failed'
    assert result['submission_state'] == 'not_submitted'
    assert result['error_code'] == 'configuration_unavailable'
    market_tool.assert_not_called()


def test_v3_strategy_close_keeps_original_fill_after_outer_receipt_loss(ledger, monkeypatch):
    cfg = {**CFG, 'exit_mode': 'attached_optional'}
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: cfg)
    opened = json.loads(tool_registry.run_trade_tool('execute_trade_actions', batch(opening()), 'cfg', 'ETH/USDT', 'open'))
    with database.get_db_conn() as conn:
        conn.execute('UPDATE mock_orders SET is_filled=1')
        conn.commit()
    payload = batch({'action': 'close', 'side': 'LONG', 'order_type': 'market', 'close_all': True, 'reason': 'exit'})
    result = json.loads(tool_registry.run_trade_tool('execute_trade_actions', payload, 'cfg', 'ETH/USDT', 'close'))
    assert result['status'] == 'completed'
    assert result['results'][0]['result']['filled_amount'] == .2
    assert json.loads(tool_registry.run_trade_tool('execute_trade_actions', payload, 'cfg', 'ETH/USDT', 'close')) == result


def test_bound_chat_v3_approves_each_action_and_reject_stops_tail_but_query_available(ledger, monkeypatch):
    monkeypatch.setattr(chat_graph, '_resolve_chat_config', lambda _: CFG)
    approvals = Mock(side_effect=[True, False])
    monkeypatch.setattr(chat_graph, 'interrupt', approvals)
    dispatched = Mock(return_value={'receipt_version': 1, 'status': 'completed'})
    monkeypatch.setattr('backend.agent.unified_dispatch.dispatch_live_action', lambda action, cid, symbol, cfg, oid: dispatched(action.action))
    payload = batch(cancel(), cancel('regular:2'), cancel('regular:3'))
    calls = [{'id': 'chat-parent', 'name': 'execute_trade_actions', 'args': payload},
             {'id': 'chat-query', 'name': 'get_trade_operation', 'args': {'target_operation_id': 'chat-parent'}}]
    result = chat_graph.tools_node({'messages': [AIMessage(content='', tool_calls=calls)]}, {'configurable': {'config_id': 'cfg'}})
    receipt = json.loads(result['messages'][0].content)
    assert [row['status'] for row in receipt['results']] == ['completed', 'not_executed', 'not_executed']
    assert json.loads(result['messages'][1].content)['query']['status'] == 'completed'
    assert approvals.call_count == 2 and dispatched.call_count == 1
    assert [call.args[0]['action_index'] for call in approvals.call_args_list] == [0, 1]


def test_workflow_json_v3_examples_use_actual_capability_validator():
    document = (Path(__file__).parents[1] / 'docs/TRADING_WORKFLOW.md').read_text(encoding='utf-8')
    examples = [json.loads(value) for value in re.findall(r'```json\s*(.*?)\s*```', document, flags=re.S)]
    unified_examples = [value for value in examples if value.get('contract_version') == 3]
    assert len(unified_examples) >= 2
    for value in unified_examples:
        assert unified.validate_for_config(value, {**CFG, 'mode': 'REAL'})


LEGACY_FIXTURES = json.loads((Path(__file__).parent / 'fixtures/legacy_trading_operations.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('fixture', LEGACY_FIXTURES, ids=lambda value: value['name'])
def test_saved_legacy_fingerprints_receipts_and_tail_replay_are_unchanged(ledger, monkeypatch, fixture):
    from backend.agent import trade_batch
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: {**CFG, 'config_id': 'fixture-task'})
    monkeypatch.setattr('backend.mcp.guard.preflight_tool', lambda *a, **kw: None)
    if fixture.get('tool') == 'close':
        dispatch = Mock(return_value=json.dumps(fixture['receipt']))
        monkeypatch.setattr(independent_tools, 'execute_action', dispatch)
    elif fixture.get('tool'):
        dispatch = Mock(return_value=fixture['receipt'])
        monkeypatch.setattr(agent_tools.cancel_orders_strategy, 'func', dispatch)
    else:
        receipts = [row['result'] for row in fixture['receipt']['results'] if 'result' in row]
        dispatch = Mock(side_effect=[json.dumps(row) for row in receipts] if fixture['name'] == 'v2' else receipts)
        monkeypatch.setattr(independent_tools, 'execute_action', dispatch)
        monkeypatch.setattr(trade_batch, '_dispatch', dispatch)
    name = fixture.get('tool', 'execute_trade_actions')
    first = json.loads(tool_registry.run_trade_tool(name, fixture['payload'], 'fixture-task', 'ETH/USDT', fixture['name']))
    assert first == fixture['receipt']
    assert json.loads(tool_registry.run_trade_tool(name, fixture['payload'], 'fixture-task', 'ETH/USDT', fixture['name'])) == first
    assert dispatch.call_count == (1 if fixture.get('tool') else 2)
    with database.get_db_conn() as conn:
        rows = {row['operation_id']: dict(row) for row in conn.execute('SELECT * FROM trade_action_runs')}
        intents = {row['operation_id']: json.loads(row['request_json']) for row in conn.execute('SELECT * FROM trade_operation_requests')}
    assert rows[fixture['name']]['request_hash'] == fixture['fingerprint']
    assert intents[fixture['name']] == fixture['parent_request']
    for child in fixture.get('children', []):
        assert rows[child['operation_id']]['request_hash'] == child['fingerprint']
        assert intents[child['operation_id']] == child['request']
    if fixture.get('children'):
        assert fixture['name'] + ':2' not in rows


@pytest.mark.parametrize('node_name', ['agent_node', 'small_agent_node'])
@pytest.mark.parametrize('mode,exit_mode', [('REAL', 'independent_exits'), ('REAL', 'attached_required'),
                                           ('STRATEGY', 'attached_optional'), ('SPOT_DCA', 'independent_exits')])
def test_actual_main_and_small_bind_exactly_two_tools(ledger, monkeypatch, node_name, mode, exit_mode):
    from backend.agent import agent_graph
    from backend.agent.agent_models import AgentState
    from langchain_core.messages import HumanMessage, AIMessageChunk
    class Model:
        tools = None
        def bind_tools(self, tools):
            self.tools = tools
            return self
        def stream(self, *args, **kwargs):
            yield AIMessageChunk(content='observed')
    model = Model()
    monkeypatch.setattr(agent_graph, 'build_chat_model', lambda **kw: model)
    monkeypatch.setattr(agent_graph, 'refresh_decision_context', lambda state, cfg: state)
    state = AgentState(symbol='ETH/USDT', market_context={}, account_context={}, messages=[HumanMessage(content='analyze')])
    result = getattr(agent_graph, node_name)(state, {'configurable': {'config_id': 'cfg', 'agent_config': {
        'model': 'offline-fixture', 'mode': mode, 'exit_mode': exit_mode}}})
    assert [tool.name for tool in model.tools] == ['execute_trade_actions', 'get_trade_operation']
    assert model.tools[0].args_schema['properties']['contract_version']['const'] == 3
    assert result.messages[-1].content == 'observed'


def test_real_attached_close_unknown_uses_saved_client_identity_and_never_resubmits(ledger, monkeypatch):
    from backend.agent import unified_dispatch
    from test_position_protection import Exchange
    cfg = {**CFG, 'mode': 'REAL', 'exit_mode': 'attached_optional'}
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: cfg)
    exchange = Exchange()
    exchange.quantity = 1
    exchange.timeout_kind = 'exit'
    mt = SimpleNamespace(exchange=exchange, config_id='cfg')
    monkeypatch.setattr(agent_tools, 'MarketTool', lambda **kw: mt)
    from contextlib import contextmanager
    @contextmanager
    def already_managed(*args, **kw):
        yield {}
    monkeypatch.setattr('backend.utils.position_management.management_action', already_managed)
    payload = batch({'action': 'close', 'side': 'LONG', 'order_type': 'market', 'close_all': True, 'reason': 'exit'}, cancel('regular:unattempted'))
    first = json.loads(tool_registry.run_trade_tool('execute_trade_actions', payload, 'cfg', 'ETH/USDT', 'real-close'))
    assert first['results'][0]['status'] == 'unknown'
    assert first['results'][0]['result']['client_id']
    assert exchange.quantity == 0 and len(exchange.calls) == 1
    recovered = unified.query_unified_operation('cfg', 'ETH/USDT', 'real-close')
    assert recovered['results'][0]['status'] == 'completed'
    assert recovered['results'][0]['result']['filled_amount'] == 1
    assert recovered['results'][1]['status'] == 'not_executed'
    tool_registry.run_trade_tool('execute_trade_actions', payload, 'cfg', 'ETH/USDT', 'real-close')
    assert len(exchange.calls) == 1


def test_real_attached_fixed_close_cannot_silently_become_all(ledger, monkeypatch):
    from backend.agent import unified_dispatch
    from test_position_protection import Exchange
    from backend.utils.position_protection import PositionProtection
    from backend.agent.trading_contracts import UnifiedCloseAction
    exchange = Exchange()
    exchange.quantity = .2
    mt = SimpleNamespace(exchange=exchange, config_id='cfg')
    action = UnifiedCloseAction.model_validate({'action': 'close', 'side': 'LONG', 'order_type': 'market', 'amount': .3, 'reason': 'fixed'})
    with pytest.raises(ValueError, match='Fixed'):
        unified_dispatch._attached_close(action, 'cfg', 'ETH/USDT', 'fixed', mt, PositionProtection(mt))
    assert not exchange.calls


def test_attached_real_protect_preserves_whole_position_and_omitted_leg(ledger, monkeypatch):
    from backend.agent import unified_dispatch
    from backend.agent.trading_contracts import UnifiedProtectAction
    from backend.utils.position_protection import PositionProtection
    from test_position_protection import Exchange, entry
    exchange = Exchange()
    mt = SimpleNamespace(exchange=exchange, config_id='cfg')
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: {**CFG, 'mode': 'REAL', 'exit_mode': 'attached_optional'})
    service = PositionProtection(mt)
    opened = service.open('ETH/USDT', entry(), operation_id='attached-open')
    exchange.orders[opened['id']].update(filled=1, status='closed')
    exchange.quantity = 1
    service.reconcile_all()
    action = UnifiedProtectAction.model_validate({'action': 'protect', 'side': 'LONG', 'take_profit': 130, 'reason': 'raise target'})
    result = unified_dispatch._attached_real(action, 'cfg', 'ETH/USDT', {'mode': 'REAL'}, 'protection', mt)
    assert result['status'] == 'completed'
    assert result['stop_loss'] == 90 and result['take_profit'] == 130
    assert len(result['order_ids']) == 2


def test_attached_strategy_query_target_channel_is_not_interchangeable(ledger, monkeypatch):
    cfg = {**CFG, 'exit_mode': 'attached_optional'}
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: cfg)
    opened = json.loads(tool_registry.run_trade_tool('execute_trade_actions', batch(opening()), 'cfg', 'ETH/USDT', 'open'))
    identity = opened['results'][0]['result']['order_id'].replace('regular:', 'algo:')
    result = json.loads(tool_registry.run_trade_tool('execute_trade_actions', batch(cancel(identity)), 'cfg', 'ETH/USDT', 'wrong-channel'))
    assert result['status'] == 'failed'
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT status FROM mock_orders').fetchone()[0] == 'OPEN'


def hold_batch_lock(db_path, ready, release):
    from backend import database as child_database
    from backend.utils.management_lock import scope_file_lock
    child_database.DB_NAME = db_path
    with scope_file_lock('unified-batch:cfg', 'parent', 'BOTH'):
        ready.set()
        release.wait(20)


def test_foreign_worker_lock_keeps_unattempted_children_pending(ledger):
    import multiprocessing
    from backend.utils.trade_operations import run_once
    def interrupted():
        parsed = unified.validate_for_config(batch(cancel(), cancel('regular:2')), CFG)
        unified._prepare_children('cfg', 'parent', parsed, 'ETH/USDT')
        raise SystemExit()
    request = {'tool': 'execute_trade_actions', 'contract_version': 3, 'args': batch(cancel(), cancel('regular:2'))}
    with pytest.raises(SystemExit):
        run_once('cfg', 'ETH/USDT', 'parent', request, interrupted)
    context = multiprocessing.get_context('spawn')
    ready, release = context.Event(), context.Event()
    worker = context.Process(target=hold_batch_lock, args=(database.DB_NAME, ready, release))
    worker.start()
    try:
        assert ready.wait(15)
        current = unified.query_unified_operation('cfg', 'ETH/USDT', 'parent', read_exchange=False)
        assert [row['status'] for row in current['results']] == ['pending', 'pending']
    finally:
        release.set()
        worker.join(10)
        if worker.is_alive():
            worker.terminate()
            worker.join()
    finished = unified.query_unified_operation('cfg', 'ETH/USDT', 'parent', read_exchange=False)
    assert [row['status'] for row in finished['results']] == ['not_executed', 'not_executed']


def test_attached_strategy_readonly_query_observes_fill_without_claim_or_reexecution(ledger, monkeypatch):
    cfg = {**CFG, 'exit_mode': 'attached_optional'}
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: cfg)
    opened = json.loads(tool_registry.run_trade_tool('execute_trade_actions', batch(opening()), 'cfg', 'ETH/USDT', 'fill-query'))
    assert opened['status'] == 'submitted'
    with database.get_db_conn() as conn:
        conn.execute('UPDATE mock_orders SET is_filled=1')
        conn.commit()
    query = json.loads(tool_registry.run_trade_tool('get_trade_operation', {'target_operation_id': 'fill-query'}, 'cfg', 'ETH/USDT'))
    assert query['results'][0]['status'] == 'completed'
    assert query['results'][0]['result']['order_status'] == 'filled'
    assert query['results'][0]['result']['filled_amount'] == .2
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM mock_orders').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM visible_management_claims').fetchone()[0] == 0


def test_attached_protection_lost_ack_recovery_keeps_new_leg_and_old_cleanup_separate(ledger, monkeypatch):
    from backend.utils.position_protection import PositionProtection
    from test_position_protection import Exchange, entry
    from contextlib import contextmanager
    cfg = {**CFG, 'mode': 'REAL', 'exit_mode': 'attached_optional'}
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: cfg)
    exchange = Exchange()
    mt = SimpleNamespace(exchange=exchange, config_id='cfg')
    service = PositionProtection(mt)
    opened = service.open('ETH/USDT', entry(), operation_id='initial-entry')
    exchange.orders[opened['id']].update(status='closed', filled=1)
    exchange.quantity = 1
    service.reconcile_all()
    exchange.timeout_kind = 'tp'
    monkeypatch.setattr(agent_tools, 'MarketTool', lambda **kw: mt)
    @contextmanager
    def already_managed(*args, **kw):
        yield {}
    monkeypatch.setattr('backend.utils.position_management.management_action', already_managed)
    payload = batch({'action': 'protect', 'side': 'LONG', 'take_profit': 130, 'reason': 'new target'})
    first = json.loads(tool_registry.run_trade_tool('execute_trade_actions', payload, 'cfg', 'ETH/USDT', 'protect-timeout'))
    assert first['status'] == 'unknown'
    count = len(exchange.calls)
    recovered = unified.query_unified_operation('cfg', 'ETH/USDT', 'protect-timeout')
    assert recovered['results'][0]['status'] == 'completed'
    assert recovered['results'][0]['result']['cleanup']['status'] == 'pending'
    assert len(exchange.calls) == count
    assert len(recovered['results'][0]['result']['order_ids']) == 2


@pytest.mark.parametrize('order_type,prices', [('market', {}), ('limit', {'price': 120}),
    ('stop_market', {'trigger_price': 90}), ('take_profit_market', {'trigger_price': 120}),
    ('stop_limit', {'trigger_price': 90, 'price': 89}), ('take_profit_limit', {'trigger_price': 120, 'price': 119})])
def test_v3_independent_preserves_six_existing_exit_types(ledger, order_type, prices):
    from backend.database_independent import MockIndependentTrading
    opened = json.loads(tool_registry.run_trade_tool('execute_trade_actions', batch(opening(amount=1)), 'cfg', 'ETH/USDT', 'entry'))
    store = MockIndependentTrading('cfg', 'ETH/USDT')
    import time
    store.monitor(110, 90, int((time.time() + 120) * 1000))
    payload = batch({'action': 'close', 'side': 'LONG', 'order_type': order_type, 'amount': .2, 'reason': 'stage', **prices})
    result = json.loads(tool_registry.run_trade_tool('execute_trade_actions', payload, 'cfg', 'ETH/USDT', 'exit'))
    assert result['status'] == ('completed' if order_type == 'market' else 'submitted')
    assert result['results'][0]['result']['quantity_mode'] == 'fixed'
    assert json.loads(tool_registry.run_trade_tool('execute_trade_actions', payload, 'cfg', 'ETH/USDT', 'exit')) == result


def test_v3_independent_remaining_edit_uses_latest_partial_fill(ledger):
    from backend.database_independent import MockIndependentTrading
    result = json.loads(tool_registry.run_trade_tool('execute_trade_actions', batch(opening(amount=1)), 'cfg', 'ETH/USDT', 'entry'))
    identity = result['results'][0]['result']['order_id']
    with database.get_db_conn() as conn:
        conn.execute('UPDATE mock_orders SET filled_amount=.2')
        conn.commit()
    edited = json.loads(tool_registry.run_trade_tool('execute_trade_actions', batch({
        'action': 'edit', 'order_id': identity, 'remaining_amount': .3, 'reason': 'resize'}), 'cfg', 'ETH/USDT', 'resize'))
    assert edited['status'] == 'completed'
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT amount FROM mock_orders').fetchone()[0] == .5


@pytest.mark.parametrize('cfg,engine,action,valid', [
    (CFG, 'live', opening(), True), (CFG, 'live', opening(stop_loss=90), False),
    ({**CFG, 'exit_mode': 'attached_required'}, 'live', opening(), False),
    ({**CFG, 'exit_mode': 'attached_required'}, 'live', opening(stop_loss=90, take_profit=110), True),
    ({**CFG, 'mode': 'SPOT_DCA'}, 'live', {'action': 'close', 'side': 'LONG', 'order_type': 'market', 'close_all': True, 'reason': 'all'}, False),
    (CFG, 'live', {'action': 'close', 'side': 'LONG', 'order_type': 'limit', 'close_all': True, 'price': 120, 'reason': 'all'}, False),
    (CFG, 'live', {'action': 'close', 'side': 'LONG', 'order_type': 'stop_market', 'amount': .2, 'reason': 'protect'}, False),
    (CFG, 'live', {'action': 'close', 'side': 'LONG', 'order_type': 'stop_market', 'close_all': True, 'trigger_price': 90, 'reason': 'all'}, True),
    ({'mode': 'PAPER'}, 'paper', {'action': 'open', 'symbol': 'ETH/USDT', 'market_type': 'swap', 'side': 'LONG', 'order_type': 'market', 'margin_amount': 20, 'leverage': 5, 'reason': 'buy'}, True),
    ({'mode': 'PAPER'}, 'paper', {'action': 'open', 'symbol': 'ETH/USDT', 'market_type': 'swap', 'side': 'LONG', 'order_type': 'market', 'amount': 1, 'margin_amount': 20, 'reason': 'buy'}, False),
    ({'mode': 'PAPER'}, 'paper', {'action': 'open', 'symbol': 'ETH/USDT', 'market_type': 'swap', 'side': 'LONG', 'order_type': 'market', 'amount': 1, 'leverage': 1.5, 'reason': 'buy'}, False),
])
def test_published_json_schema_rejects_known_unsupported_shapes(cfg, engine, action, valid):
    from jsonschema import validate, ValidationError as SchemaError
    if valid:
        validate(batch(action), unified.unified_schema_for_config(cfg, engine=engine))
        assert unified.validate_for_config(batch(action), cfg, engine=engine)
    else:
        with pytest.raises(SchemaError):
            validate(batch(action), unified.unified_schema_for_config(cfg, engine=engine))
        with pytest.raises(ValueError):
            unified.validate_for_config(batch(action), cfg, engine=engine)


def test_actual_spot_v3_multi_buy_shares_one_parent_allowance(spot_runtime, monkeypatch):
    from contextlib import contextmanager
    from backend.mcp import spot_orders
    @contextmanager
    def managed(*args, **kw):
        yield {}
    monkeypatch.setattr('backend.utils.position_management.management_action', managed)
    monkeypatch.setattr(spot_orders, '_tool', lambda config_id, symbol: (agent_tools.MarketTool(config_id=config_id), {'symbol': symbol}))
    payload = batch(opening(symbol='BTC/USDT', amount=6, price=10), opening(symbol='ETH/USDT', amount=6, price=10))
    result = json.loads(tool_registry.run_trade_tool('execute_trade_actions', payload, 'spot', 'BTC/USDT', 'shared-budget'))
    assert [row['status'] for row in result['results']] == ['submitted', 'failed']
    assert '预算' in result['results'][1]['result']['error']
    assert len(spot_runtime.calls) == 1
    assert result['results'][0]['result']['order_id'] == 'order-1'
    assert result['results'][0]['result']['client_id']
    with database.get_db_conn() as conn:
        cycles = conn.execute('SELECT cycle_id,budget FROM spot_budget_cycles').fetchall()
        assert [list(row) for row in cycles] == [['shared-budget', 100]]
        assert conn.execute('SELECT COUNT(DISTINCT cycle_id) FROM spot_budget_reservations').fetchone()[0] == 1
    assert json.loads(tool_registry.run_trade_tool('execute_trade_actions', payload, 'spot', 'BTC/USDT', 'shared-budget')) == result
    assert len(spot_runtime.calls) == 1


def test_spot_cancel_first_is_not_blocked_by_later_zero_allowance(spot_runtime, monkeypatch):
    from backend.utils.spot_execution import current_spot_cycle_id
    spot_runtime.config['dca_amount'] = 0
    dispatched = Mock(return_value={'receipt_version': 1, 'status': 'completed'})
    result = unified.execute_v3_actions(batch(cancel(symbol='BTC/USDT'), opening(symbol='BTC/USDT')), config_id='spot',
        symbol='BTC/USDT', operation_id='zero-budget', config=spot_runtime.config, dispatch=dispatched)
    assert [row['status'] for row in result['results']] == ['completed', 'failed']
    dispatched.assert_called_once()
    assert current_spot_cycle_id.get() is None


def test_spot_unknown_buy_queries_exact_original_client_without_rebuy(spot_runtime, monkeypatch):
    from contextlib import contextmanager
    from backend.mcp import spot_orders
    @contextmanager
    def managed(*args, **kw):
        yield {}
    monkeypatch.setattr('backend.utils.position_management.management_action', managed)
    monkeypatch.setattr(spot_orders, '_tool', lambda config_id, symbol: (agent_tools.MarketTool(config_id=config_id), {'symbol': symbol}))
    spot_runtime.responses.append(TimeoutError('exchange accepted but response lost'))
    payload = batch(opening(symbol='BTC/USDT', amount=2, price=10))
    first = json.loads(tool_registry.run_trade_tool('execute_trade_actions', payload, 'spot', 'BTC/USDT', 'buy-timeout'))
    child = first['results'][0]['result']
    assert first['status'] == 'unknown'
    assert child['execution_operation_id'] == 'buy-timeout:0:0'
    spot_runtime.verified.update(id='accepted-buy', clientOrderId=child['client_id'], symbol='BTC/USDT',
                                 status='closed', filled=2, remaining=0)
    result = unified.query_unified_operation('spot', 'BTC/USDT', 'buy-timeout')
    assert result['results'][0]['status'] == 'completed'
    assert result['results'][0]['result']['order_id'] == 'regular:accepted-buy'
    assert result['results'][0]['result']['filled_amount'] == 2
    tool_registry.run_trade_tool('execute_trade_actions', payload, 'spot', 'BTC/USDT', 'buy-timeout')
    assert len(spot_runtime.calls) == 1


def test_current_step_entry_gate_precedes_claim_and_does_not_block_prior_cancel(ledger, monkeypatch):
    from backend.agent import unified_dispatch
    from contextlib import contextmanager
    cfg = {**CFG, 'mode': 'REAL', 'exit_mode': 'attached_optional'}
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: cfg)
    claim_entries = []
    @contextmanager
    def managed(*args, **kw):
        claim_entries.append(args[3])
        yield {}
    monkeypatch.setattr('backend.utils.position_management.management_action', managed)
    gate = Mock(side_effect=ValueError('current leverage exceeds limit'))
    monkeypatch.setattr('backend.mcp.guard.assert_entry_allowed', gate)
    monkeypatch.setattr(unified_dispatch, '_attached_real', lambda *args: {'receipt_version': 1, 'status': 'completed'})
    result = json.loads(tool_registry.run_trade_tool('execute_trade_actions', batch(cancel(), opening()), 'cfg', 'ETH/USDT', 'guarded'))
    assert [row['status'] for row in result['results']] == ['completed', 'failed']
    assert claim_entries == ['guarded:0']
    assert gate.call_args.kwargs['only_capability'] is True


def test_handoff_parent_query_filters_private_symbols_and_sides_before_reads(ledger, monkeypatch):
    cfg = {**CFG, 'symbols': ['ETH/USDT', 'BTC/USDT']}
    actions = [opening(symbol='ETH/USDT'), opening(symbol='BTC/USDT'), opening(symbol='ETH/USDT', side='SHORT')]
    result = unified.execute_v3_actions(batch(*actions, execution_policy='independent', include=['account']),
        config_id='cfg', symbol='ETH/USDT', operation_id='multi', config=cfg,
        dispatch=Mock(return_value={'receipt_version': 1, 'status': 'unknown'}),
        account_summary=lambda: {'private_other_profile': 'hidden'})
    called = []
    def observe(cid, symbol, oid, previous, *, market_tool=None):
        called.append((symbol, oid, market_tool))
        return {**previous, 'status': 'completed', 'side': 'LONG'}
    monkeypatch.setattr('backend.agent.unified_dispatch.refresh_native_operation', observe)
    receiver = object()
    narrowed = unified.query_unified_operation('cfg', 'ETH/USDT:USDT', 'multi', market_tool=receiver,
        recovery_scope={'symbol': 'ETH/USDT:USDT', 'market_type': 'swap', 'side': 'LONG'})
    assert [row['index'] for row in narrowed['results']] == [0]
    assert called == [('ETH/USDT', 'multi:0', receiver)]
    assert 'account_summary' not in narrowed
    with database.get_db_conn() as conn:
        parent = json.loads(conn.execute("SELECT result FROM trade_action_runs WHERE operation_id='multi'").fetchone()[0])
    assert len(parent['results']) == 3


def test_handoff_missing_target_side_does_not_read_other_private_order(ledger, monkeypatch):
    execute(batch(cancel()), Mock(return_value={'receipt_version': 1, 'status': 'unknown'}))
    observe = Mock(side_effect=AssertionError('unaccepted side cannot be observed'))
    monkeypatch.setattr('backend.agent.unified_dispatch.refresh_native_operation', observe)
    result = unified.query_unified_operation('cfg', 'ETH/USDT', 'parent', market_tool=object(),
        recovery_scope={'symbol': 'ETH/USDT', 'market_type': 'swap', 'side': 'LONG'})
    assert result['status'] == 'unknown' and not result['results']
    observe.assert_not_called()


@pytest.mark.parametrize('kind,extra', [('stop_loss', {'stop_loss': 90, 'stop_limit_price': 89}),
    ('take_profit', {'take_profit': 120, 'take_profit_limit_price': 119}),
    ('market', {'stop_loss': 90}), ('limit', {'price': 100, 'take_profit': 120})])
def test_paper_unsupported_native_limit_or_unused_price_fields_reject_entire_batch(ledger, kind, extra):
    action = {'action': 'close', 'symbol': 'ETH/USDT', 'market_type': 'swap', 'side': 'LONG', 'order_type': kind,
              'amount': .2, 'reason': 'close', **extra}
    dispatch = Mock()
    result = unified.execute_v3_actions(batch(cancel(), action), config_id='cfg', symbol='ETH/USDT',
        config={'mode': 'PAPER'}, engine='paper', operation_id='unsupported-paper-close', dispatch=dispatch)
    assert result['status'] == 'failed' and result['phase'] == 'preflight'
    dispatch.assert_not_called()
    from jsonschema import validate, ValidationError as SchemaError
    with pytest.raises(SchemaError):
        validate(batch(cancel(), action), unified.unified_schema_for_config({'mode': 'PAPER'}, engine='paper'))


def test_unknown_native_fill_is_not_converted_to_zero():
    from backend.agent.unified_dispatch import _native_receipt
    from backend.agent.trading_contracts import UnifiedCloseAction
    action = UnifiedCloseAction.model_validate({'action': 'close', 'side': 'LONG', 'order_type': 'market', 'amount': 1, 'reason': 'exit'})
    receipt = _native_receipt({'status': 'pending', 'order_status': 'filled', 'filled': None}, action, 'original', 'ETH/USDT', contract_size=.01)
    assert receipt['status'] == 'pending' and receipt['filled_amount'] is None


def test_spot_native_edit_timeout_queries_saved_total_after_additional_fill(ledger, monkeypatch):
    from copy import deepcopy
    cfg = {'config_id': 'spot', 'mode': 'SPOT_DCA', 'market_type': 'spot', 'symbol': 'ETH/USDT',
           'symbols': ['ETH/USDT'], 'enabled': True, 'dca_amount': 100, 'dca_budget': 1000}
    class Exchange:
        id, apiKey = 'binance', 'offline-spot-only'
        has = {'editOrder': True}
        options = {'defaultType': 'spot'}
        markets = {'ETH/USDT': {'symbol': 'ETH/USDT', 'id': 'ETHUSDT', 'base': 'ETH', 'spot': True, 'contract': False}}
        order = {'id': 'manual-entry', 'symbol': 'ETH/USDT', 'type': 'limit', 'side': 'buy', 'status': 'open',
                 'price': 10, 'amount': 1, 'filled': .2, 'remaining': .8, 'cost': 2}
        unavailable, edits = False, 0
        def market(self, symbol):
            return self.markets[symbol]
        def amount_to_precision(self, symbol, amount):
            return str(amount)
        def price_to_precision(self, symbol, price):
            return str(price)
        def fetch_balance(self):
            return {'ETH': {'total': 2, 'free': 2, 'used': 0}, 'USDT': {'total': 1000, 'free': 1000, 'used': 0}}
        def fetch_open_orders(self, symbol):
            return [deepcopy(self.order)]
        def fetch_order(self, identity, symbol, *a, **kw):
            if self.unavailable:
                raise TimeoutError('read temporarily unavailable')
            assert identity == self.order['id']
            return deepcopy(self.order)
        def edit_order(self, identity, symbol, kind, side, amount, price):
            self.edits += 1
            self.order.update(amount=amount, price=price, remaining=amount-self.order['filled'])
            self.unavailable = True
            raise TimeoutError('ack lost')
    exchange = Exchange()
    mt = SimpleNamespace(config_id='spot', market_type='spot', runtime_config=cfg, exchange=exchange)
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: cfg)
    monkeypatch.setattr(agent_tools, 'MarketTool', lambda **kw: mt)
    monkeypatch.setattr('backend.utils.market_data.MarketTool', lambda **kw: mt)
    payload = batch({'action': 'edit', 'order_id': 'regular:manual-entry', 'remaining_amount': .3, 'price': 12, 'reason': 'resize'})
    first = json.loads(tool_registry.run_trade_tool('execute_trade_actions', payload, 'spot', 'ETH/USDT', 'native-edit'))
    assert first['status'] == 'unknown'
    exchange.unavailable = False
    exchange.order.update(filled=.3, remaining=.2)
    result = unified.query_unified_operation('spot', 'ETH/USDT', 'native-edit')
    assert result['results'][0]['status'] == 'completed'
    assert result['results'][0]['result']['remaining_amount'] == pytest.approx(.2)
    assert result['results'][0]['result']['filled_amount'] == .3
    tool_registry.run_trade_tool('execute_trade_actions', payload, 'spot', 'ETH/USDT', 'native-edit')
    assert exchange.edits == 1
