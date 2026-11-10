"""Independent internal calls keep execution facts, approvals and v3 identities."""
import copy
import json
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from backend import database, storage
from backend.agent import agent_graph, chat_graph, tool_registry, unified_trading
from backend.agent.agent_models import AgentState
from backend.agent.single_trade_tools import SingleTradeParameterError, prepare_single_trade_request
from backend.config import config
from backend.utils.decision_record import is_transient_schema_error
from test_unified_trading import ledger


CFG = {'config_id': 'cfg', 'mode': 'REAL', 'exit_mode': 'independent_exits', 'symbol': 'ETH/USDT'}
OPEN = {'side': 'LONG', 'amount': .1, 'price': 100, 'reason': 'entry'}
EDIT = {'order_id': 'regular:current', 'remaining_amount': .5, 'reason': 'resize'}
CANCEL = {'order_id': 'regular:current', 'reason': 'remove'}


def test_close_tool_explains_every_conditional_price_and_rejects_missing_trigger():
    tools = {tool.name:tool for tool in tool_registry.get_trade_tools_for_mode('REAL',CFG)}
    close = tools['close_position']
    for kind in ('stop_market','take_profit_market','stop_limit','take_profit_limit'):
        assert f'{kind}: ' in close.description
        examples = [row for row in close.args_schema['examples'] if row['order_type']==kind]
        assert len(examples)==1 and 'trigger_price' in examples[0]
        prepare_single_trade_request('close_position',examples[0],CFG,'ETH/USDT')
    args = dict(side='SHORT',amount=.1,order_type='take_profit_market',reason='止盈')
    with pytest.raises(SingleTradeParameterError) as exc:
        prepare_single_trade_request('close_position',args,CFG,'ETH/USDT')
    assert 'trigger_price 是止盈/止损的触发价' in str(exc.value)
    assert '补齐缺参后重新调用' in str(exc.value)
    assert '本次平仓未提交' in str(exc.value)


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: dict(CFG))
    monkeypatch.setattr(agent_graph, '_emit_task_progress', lambda *args, **kwargs: None)
    approve = Mock(return_value=True)
    monkeypatch.setattr(chat_graph, 'interrupt', approve)
    return approve


def run_node(surface, calls):
    message = AIMessage(content='', tool_calls=calls)
    context = {'configurable': {'config_id': 'cfg', 'agent_config': CFG}}
    if surface == 'chat':
        return chat_graph.tools_node({'messages': [message]}, context)['messages']
    state = AgentState(symbol='ETH/USDT', market_context={}, account_context={}, messages=[message])
    return agent_graph.tools_node(state, context).messages[1:]


@pytest.mark.parametrize('surface', ['agent', 'chat'])
@pytest.mark.parametrize('failure', ['invalid_parameters', 'explicit_rejection'])
def test_failed_second_call_does_not_stop_third_or_repeat_first(runtime, monkeypatch, surface, failure):
    seen = []

    def execute(payload, **context):
        action = payload['actions'][0]['action']
        assert len(payload['actions']) == 1
        seen.append((context['operation_id'], action))
        return {'status': {'cancel': 'completed', 'edit': 'failed', 'open': 'submitted'}[action],
                'operation_id': context['operation_id'], 'error': 'explicit rejection' if action == 'edit' else None}

    monkeypatch.setattr(unified_trading, 'execute_v3_actions', execute)
    edit = dict(EDIT) if failure == 'explicit_rejection' else {**CANCEL, 'amount': .5, 'price': 100}
    calls = [{'id': 'first', 'name': 'cancel_order', 'args': CANCEL},
             {'id': 'second', 'name': 'edit_order', 'args': edit},
             {'id': 'third', 'name': 'open_position', 'args': OPEN}]
    original = copy.deepcopy(calls)
    messages = run_node(surface, calls)
    receipts = [json.loads(message.content) for message in messages]
    assert [receipt['status'] for receipt in receipts] == ['completed', 'failed', 'submitted']
    assert [message.tool_call_id for message in messages] == ['first', 'second', 'third']
    assert seen == ([('first', 'cancel'), ('second', 'edit'), ('third', 'open')]
                    if failure == 'explicit_rejection' else [('first', 'cancel'), ('third', 'open')])
    if failure == 'invalid_parameters':
        assert 'remaining_amount' in receipts[1]['error'] and '本次改单未提交' in receipts[1]['error']
        assert '本批' not in receipts[1]['error'] and receipts[1]['submitted'] is False
        assert is_transient_schema_error(messages[1].content)
    if surface == 'chat':
        assert runtime.call_count == (3 if failure == 'explicit_rejection' else 2)
    assert calls == original


@pytest.mark.parametrize('surface', ['agent', 'chat'])
@pytest.mark.parametrize('status', ['pending', 'unknown'])
def test_uncertain_second_call_stops_tail_without_erasing_first(runtime, monkeypatch, surface, status):
    execute = Mock(side_effect=[{'status': 'completed'}, {'status': status, 'operation_id': 'second'}])
    monkeypatch.setattr(unified_trading, 'execute_v3_actions', execute)
    calls = [{'id': 'first', 'name': 'cancel_order', 'args': CANCEL},
             {'id': 'second', 'name': 'open_position', 'args': OPEN},
             {'id': 'third', 'name': 'edit_order', 'args': EDIT}]
    receipts = [json.loads(message.content) for message in run_node(surface, calls)]
    assert [receipt['status'] for receipt in receipts] == ['completed', status, 'not_executed']
    assert execute.call_count == 2
    if surface == 'chat':
        assert runtime.call_count == 2


@pytest.mark.parametrize('surface', ['agent', 'chat'])
def test_unexpected_execution_exception_is_not_a_confirmed_rejection(runtime, monkeypatch, surface):
    monkeypatch.setattr(unified_trading, 'execute_v3_actions', Mock(side_effect=RuntimeError('transport interrupted')))
    calls = [{'id': 'first', 'name': 'open_position', 'args': OPEN},
             {'id': 'second', 'name': 'cancel_order', 'args': CANCEL}]
    receipts = [json.loads(message.content) for message in run_node(surface, calls)]
    assert [receipt['status'] for receipt in receipts] == ['unknown', 'not_executed']
    assert receipts[0]['operation_id'] == 'first'


def test_chat_rejection_stops_later_approvals_and_writes(runtime, monkeypatch):
    runtime.side_effect = [True, False]
    execute = Mock(return_value={'status': 'completed'})
    monkeypatch.setattr(unified_trading, 'execute_v3_actions', execute)
    calls = [{'id': 'first', 'name': 'cancel_order', 'args': CANCEL},
             {'id': 'second', 'name': 'edit_order', 'args': EDIT},
             {'id': 'third', 'name': 'open_position', 'args': OPEN}]
    messages = run_node('chat', calls)
    assert execute.call_count == 1 and runtime.call_count == 2
    assert messages[1].content == 'Rejected by user.'
    assert json.loads(messages[2].content)['status'] == 'not_executed'


@pytest.mark.parametrize('field,value', [('action', 'cancel'), ('actions', []), ('depends_on', [0]),
                                        ('execution_policy', 'independent'), ('contract_version', 3),
                                        ('config_id', 'other'), ('engine', 'paper'), ('profile_id', 'other')])
def test_single_tool_cannot_accept_batch_controls_or_override_context(runtime, monkeypatch, field, value):
    execute = Mock(side_effect=AssertionError('invalid input reached execution'))
    monkeypatch.setattr(unified_trading, 'execute_v3_actions', execute)
    result = json.loads(tool_registry.run_trade_tool('open_position', {**OPEN, field: value}, 'cfg', 'ETH/USDT', 'invalid'))
    assert result['status'] == 'failed' and result['submitted'] is False
    assert field in result['error'] and '本批' not in result['error']
    execute.assert_not_called()


def test_multi_symbol_schema_and_runtime_require_the_explicit_authorized_target(runtime, monkeypatch):
    cfg = {**CFG, 'symbols': ['ETH/USDT', 'BTC/USDT']}
    for tool in tool_registry.get_trade_tools_for_mode('REAL', cfg):
        assert 'symbol' in tool.args_schema['required']
    with pytest.raises(SingleTradeParameterError, match='symbol'):
        prepare_single_trade_request('cancel_order', CANCEL, cfg, 'ETH/USDT')
    with pytest.raises(SingleTradeParameterError, match='scope'):
        prepare_single_trade_request('cancel_order', {**CANCEL, 'symbol': 'SOL/USDT'}, cfg, 'ETH/USDT')
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: cfg)
    execute = Mock(return_value={'status': 'completed'})
    monkeypatch.setattr(unified_trading, 'execute_v3_actions', execute)
    run_node('chat', [{'id': 'btc', 'name': 'cancel_order', 'args': {**CANCEL, 'symbol': 'BTC/USDT'}}])
    assert runtime.call_args.args[0]['symbol'] == 'BTC/USDT'
    assert execute.call_args.args[0]['actions'][0]['symbol'] == 'BTC/USDT'


@pytest.mark.parametrize('cfg,name,arguments', [
    ({**CFG, 'read_only': True}, 'open_position', OPEN),
    (CFG, 'set_position_protection', {'side': 'LONG', 'stop_loss': 90, 'reason': 'protect'}),
    ({**CFG, 'mode': 'SPOT_DCA'}, 'open_position', {**OPEN, 'side': 'SHORT'}),
    ({**CFG, 'mode': 'SPOT_DCA'}, 'close_position', {'side': 'LONG', 'order_type': 'market', 'close_all': True, 'reason': 'sell'}),
    ({**CFG, 'exit_mode': 'attached_required'}, 'open_position', OPEN),
    (CFG, 'open_position', {**OPEN, 'amount': True}),
    (CFG, 'edit_order', {**EDIT, 'remaining_amount': 0}),
])
def test_modes_permissions_and_quantity_rules_still_reject_before_execution(monkeypatch, cfg, name, arguments):
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: cfg)
    execute = Mock(side_effect=AssertionError('invalid input reached execution'))
    monkeypatch.setattr(unified_trading, 'execute_v3_actions', execute)
    result = json.loads(tool_registry.run_trade_tool(name, arguments, 'cfg', 'ETH/USDT', 'invalid-mode'))
    assert result['status'] == 'failed' and result['submitted'] is False
    execute.assert_not_called()


def test_partial_fill_amendment_and_replay_use_remaining_quantity(ledger):
    opened = json.loads(tool_registry.run_trade_tool('open_position', {**OPEN, 'amount': 1}, 'cfg', 'ETH/USDT', 'entry'))
    order_id = opened['results'][0]['result']['order_id']
    with database.get_db_conn() as conn:
        conn.execute('UPDATE mock_orders SET filled_amount=.2')
        conn.commit()
    arguments = {**EDIT, 'order_id': order_id}
    edited = json.loads(tool_registry.run_trade_tool('edit_order', arguments, 'cfg', 'ETH/USDT', 'edit'))
    assert edited['status'] == 'completed'
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT amount FROM mock_orders').fetchone()[0] == pytest.approx(.7)
        count = conn.execute('SELECT count(*) FROM trade_operation_requests').fetchone()[0]
    storage.close_pool()  # Reopen persisted state, rather than relying on an in-memory tool result.
    assert json.loads(tool_registry.run_trade_tool('edit_order', arguments, 'cfg', 'ETH/USDT', 'edit')) == edited
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT count(*) FROM trade_operation_requests').fetchone()[0] == count
        assert conn.execute('SELECT amount FROM mock_orders').fetchone()[0] == pytest.approx(.7)


def test_unknown_single_submission_replay_never_dispatches_again(ledger, monkeypatch):
    dispatch = Mock(return_value={'status': 'unknown', 'client_id': 'original-client', 'error': 'timeout'})
    monkeypatch.setattr('backend.agent.unified_dispatch.dispatch_live_action', dispatch)
    first = json.loads(tool_registry.run_trade_tool('open_position', OPEN, 'cfg', 'ETH/USDT', 'uncertain'))
    assert first['status'] == 'unknown'
    storage.close_pool()
    repeated = json.loads(tool_registry.run_trade_tool('open_position', OPEN, 'cfg', 'ETH/USDT', 'uncertain'))
    assert repeated == first
    dispatch.assert_called_once()


def test_chat_approval_resume_does_not_repeat_the_previously_approved_trade(ledger):
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import StateGraph, START, END
    from langgraph.types import Command

    workflow = StateGraph(chat_graph.ChatState)
    workflow.add_node('tools', chat_graph.tools_node)
    workflow.add_edge(START, 'tools')
    workflow.add_edge('tools', END)
    graph = workflow.compile(checkpointer=InMemorySaver())
    context = {'configurable': {'thread_id': 'single-approval', 'config_id': 'cfg'}}
    message = AIMessage(content='', tool_calls=[
        {'id': 'approved-first', 'name': 'open_position', 'args': OPEN},
        {'id': 'approved-second', 'name': 'open_position', 'args': {**OPEN, 'price': 99}},
    ])
    first = graph.invoke({'messages': [message]}, context)
    assert first['__interrupt__'][0].value['tool_call_id'] == 'approved-first'
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT count(*) FROM mock_orders').fetchone()[0] == 0
    second = graph.invoke(Command(resume=True), context)
    assert second['__interrupt__'][0].value['tool_call_id'] == 'approved-second'
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT count(*) FROM mock_orders').fetchone()[0] == 1
    completed = graph.invoke(Command(resume=True), context)
    assert '__interrupt__' not in completed
    receipts = [msg for msg in completed['messages'] if msg.type == 'tool']
    assert [msg.tool_call_id for msg in receipts] == ['approved-first', 'approved-second']
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT count(*) FROM mock_orders').fetchone()[0] == 2
