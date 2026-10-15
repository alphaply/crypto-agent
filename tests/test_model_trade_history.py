"""Prompt projection does not restore removed tools or mutate financial audit."""
from copy import deepcopy
import json
import pytest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from backend.agent.model_trade_history import project_trade_messages, project_generated_text
from backend.utils.trade_summary import receipt_bytes


def test_retired_query_pairs_are_removed_without_mutating_saved_messages():
    user = HumanMessage(content='What happened to get_operation?')
    assistant = AIMessage(content='Close accepted. Call get_operation next.', tool_calls=[
        {'name':'close','id':'write','args':{'amount':.03}},
        {'name':'get_operation','id':'read','args':{'operation_id':'write'}}],
        additional_kwargs={'tool_calls':[{'function':{'name':'get_operation'}}]})
    raw = {'receipt_version':1,'operation_id':'write','action':'close','status':'submitted',
           'filled_amount':0,'order_status':'waiting_trigger',
           'continuation':{'previous_assessment':{'recovery':{'tool':'get_operation'}}}}
    result = ToolMessage(tool_call_id='write',content=json.dumps(raw))
    query = ToolMessage(tool_call_id='read',content='Error: Conflicting trusted context: config_id')
    messages = [user,assistant,result,query]
    before = deepcopy(messages)
    projected = project_trade_messages(messages)
    assert projected[0] is user
    assert 'Close accepted.' in projected[1].content
    assert [call['name'] for call in projected[1].tool_calls] == ['close']
    assert 'get_operation' not in json.dumps(projected[1].model_dump())
    assert len(projected) == 3
    receipt = projected[2].content
    assert '提交成功' in receipt and '已成交数量0' in receipt
    assert 'get_operation' not in receipt
    assert messages == before



def test_current_exact_operation_projection_is_local_and_uses_trusted_task(monkeypatch):
    calls = []
    def current(config_id, operation_id, original):
        calls.append((config_id,operation_id))
        return {**original,'status':'completed','filled_amount':.03}
    monkeypatch.setattr('backend.utils.trade_recovery.current_receipt_projection',current)
    original = {'operation_id':'old-operation','status':'unknown'}
    message = ToolMessage(tool_call_id='old-operation',content=json.dumps(original))
    projected = project_trade_messages([message],'trusted-task')
    assert calls == [('trusted-task','old-operation')]
    assert '成功' in projected[0].content and '已成交数量0.03' in projected[0].content
    assert json.loads(message.content)['status'] == 'unknown'


def test_retired_query_only_turn_keeps_useful_text_without_an_orphan_result():
    message = AIMessage(content='Already submitted. 查询 get_trade_operation。',tool_calls=[
        {'name':'get_trade_operation','id':'old-query','args':{}}])
    result = project_trade_messages([message,ToolMessage(tool_call_id='old-query',content='failed')])
    assert len(result) == 1 and not result[0].tool_calls
    assert 'Already submitted.' in result[0].content
    assert 'get_trade_operation' not in result[0].content
    assert 'get_operation' not in project_generated_text('历史：调用 get_operation。其余事实保留。')


def test_provider_content_blocks_cannot_restore_a_removed_call_or_query_guidance():
    message=AIMessage(content=[{'type':'text','text':'Accepted. Call get_operation.'},
                              {'type':'tool_use','name':'get_operation','id':'old','input':{}}],tool_calls=[
                                  {'name':'get_operation','id':'old','args':{}}])
    projected=project_trade_messages([message,ToolMessage(tool_call_id='old',content='failed')])
    assert len(projected)==1 and not projected[0].tool_calls
    assert 'get_operation' not in json.dumps(projected[0].model_dump())
    assert len(message.content)==2


@pytest.mark.parametrize('state', ['available', 'missing', 'scope_mismatch', 'oversize'])
def test_exact_local_projection_marks_unavailable_without_rewriting_historical_facts(tmp_path, monkeypatch, state):
    from backend import database
    from backend.utils.trade_recovery import current_receipt_projection
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'history.sqlite'))
    monkeypatch.setattr('backend.utils.trade_evidence.configured_account_scope', lambda _: 'trusted-account')
    monkeypatch.setenv('RUNTIME_FINANCIAL_PAYLOAD_BYTES', '4096')
    original = {'operation_id': 'old-close', 'status': 'completed', 'filled_amount': .03,
                'order_id': 'algo:123', 'observed_at': 10}
    saved = {**original, 'status': 'submitted', 'filled_amount': None, 'observed_at': 20}
    if state == 'oversize':
        saved['diagnostic'] = 'x' * 5000
    with database.get_db_conn() as conn:
        conn.execute('CREATE TABLE trade_action_runs(config_id TEXT,operation_id TEXT,result TEXT,updated_at REAL)')
        conn.execute('CREATE TABLE trade_operation_evidence(config_id TEXT,operation_id TEXT,account_scope TEXT)')
        if state != 'missing':
            conn.execute('INSERT INTO trade_action_runs VALUES(?,?,?,?)', ('trusted-task', 'old-close', json.dumps(saved), 30))
            conn.execute('INSERT INTO trade_operation_evidence VALUES(?,?,?)',
                         ('trusted-task', 'old-close', 'other-account' if state == 'scope_mismatch' else 'trusted-account'))
        conn.commit()
    baseline = deepcopy(original)
    result = current_receipt_projection('trusted-task', 'old-close', original)
    assert original == baseline
    if state == 'available':
        assert result['current_evidence_status'] == 'available'
        assert result['evidence_source'] == 'local_execution_evidence'
        assert result['status'] == 'submitted' and result['filled_amount'] is None
        assert result['observed_at'] == 20 and result['receipt_updated_at'] == 30
    else:
        assert result['current_evidence_status'] == 'unavailable'
        assert result['evidence_source'] == 'historical_cached'
        assert result['status'] == 'completed' and result['filled_amount'] == .03
        assert result['order_id'] == 'algo:123' and result['observed_at'] == 10
