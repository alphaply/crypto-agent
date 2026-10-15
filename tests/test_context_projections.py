import json
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from backend import database
from backend.agent import context_projections as projections, memory_agent, memory_service, memory_updates
from backend.agent.decision_context import load_decision_memory
from backend.agent.memory_workflow import memory_review_lease, get_review_result
from backend.database_schema import initialize_schema
from backend.utils.trade_context import project_trade_state, render_orders
from test_trade_tool_integration import simulation, real_short, call, recover


CFG = dict(config_id='cfg', symbol='ETH/USDT', mode='STRATEGY', exit_mode='independent_exits', model='offline')
THESIS = '等四小时收盘确认前不加仓；支撑95，突破110再评估。'
LEGACY = THESIS + '旧 unknown 待核验，禁止加仓。'


@pytest.fixture
def sources(simulation, monkeypatch):
    from backend.config import config
    monkeypatch.setattr(config, 'get_config_by_id', lambda cid: CFG if cid == 'cfg' else None)
    with database.get_db_conn() as conn:
        record = {'messages': [], 'execution_results': [{'tool': 'close', 'tool_call_id': 'old-close',
                  'result': json.dumps({'operation_id': 'old-close', 'status': 'unknown'})}]}
        sid = conn.execute('INSERT INTO summaries(config_id,symbol,timestamp,strategy_logic,decision_json) VALUES(?,?,?,?,?)',
                           ('cfg', 'ETH/USDT', '2026-10-01 01:00:00', LEGACY, json.dumps(record))).lastrowid
        conn.execute('INSERT INTO trade_action_runs VALUES(?,?,?,?,?,?,?,?)',
                     ('cfg', 'old-close', 'ETH/USDT', 'hash', 'unknown', json.dumps({'operation_id': 'old-close', 'status': 'unknown', 'side': 'LONG'}), 0, 0))
        conn.commit()
    database.save_short_memory('2026-10-10 00:00:00', '2026-10-10 04:00:00', 'ETH/USDT', 'cfg', LEGACY, '', 1,
                               source_summary_ids=[sid])
    monkeypatch.setattr(memory_agent, 'invoke_with_retry', lambda op, **_: op())
    monkeypatch.setattr(memory_agent, '_start_audit', lambda *a: None)
    monkeypatch.setattr(memory_agent, '_finish_audit', lambda *a, **k: None)
    return sid


def response_for(context, *, thesis=THESIS):
    def draft(refs):
        return dict(strategy_text=thesis, execution_refs=refs,
                    unlinked_execution_notes=['旧0.37疑虑无独立身份，不能用0.30替代核验'])
    return dict(summary=thesis, memory=draft(context['all_refs']), projections=[
        {**{k: s[k] for k in ('source_kind', 'source_id', 'source_hash')},
         **draft(context['candidates'][s['source_kind'] + ':' + s['source_id']])} for s in context['sources']])


def model(monkeypatch, *, failure=False):
    captured = []
    def invoke(messages):
        captured.append(messages)
        if failure:
            raise RuntimeError('model offline')
        context = projections.prepare_review('cfg')
        return AIMessage(content=json.dumps(response_for(context), ensure_ascii=False))
    monkeypatch.setattr(memory_agent, 'build_chat_model', lambda **_: Mock(invoke=invoke))
    return captured


def test_additive_schema_hash_manual_edit_and_original_audit(sources, monkeypatch):
    with database.get_db_conn() as conn:
        initialize_schema(conn)
        initialize_schema(conn)
    original = database.get_short_memories('cfg', 1)[0]
    calls = model(monkeypatch)
    assert projections.process_projection_updates('cfg', CFG)
    assert len(calls) == 1
    memory = load_decision_memory('cfg', CFG)
    assert THESIS in memory.short_memory_text and '旧 unknown' not in memory.short_memory_text
    assert THESIS in memory.recent_summaries_text and '旧 unknown' not in memory.recent_summaries_text
    assert 'old-close' in memory.execution_evidence
    assert database.get_short_memories('cfg', 1)[0] == original
    database.update_short_memory('cfg', original['bucket_start'], '新人工观点；旧 unknown 禁止交易', '')
    edited = load_decision_memory('cfg', CFG)
    assert '尚无已发布' in edited.short_memory_text
    assert '新人工观点' not in edited.short_memory_text
    assert len(calls) == 1  # Decision reads never invoke a model.
    assert projections.process_projection_updates('cfg', CFG)
    assert len(calls) == 2


def test_resolved_reference_disappears_even_while_memory_model_fails(sources, monkeypatch):
    model(monkeypatch)
    assert projections.process_projection_updates('cfg', CFG)
    before = load_decision_memory('cfg', CFG)
    state = lambda m: project_trade_state({'independent_exits': {'positions': [], 'exits': []}},
        config_id='cfg', account_scope='mock:cfg', symbol='ETH/USDT', plans=[], referenced_evidence=m.execution_evidence)
    assert any(i['operation_id'] == 'old-close' for i in state(before).issues)
    with database.get_db_conn() as conn:
        conn.execute("UPDATE trade_action_runs SET status='completed',result=? WHERE operation_id='old-close'",
                     (json.dumps({'operation_id': 'old-close', 'status': 'completed', 'side': 'LONG', 'cleanup': {'status': 'completed'}}),))
        conn.commit()
    failed_model = model(monkeypatch, failure=True)
    after = load_decision_memory('cfg', CFG)
    assert not state(after).issues
    assert THESIS in after.short_memory_text and THESIS in after.recent_summaries_text
    assert '禁止加仓' not in after.short_memory_text + after.recent_summaries_text + render_orders(state(after))
    assert failed_model == []


@pytest.mark.parametrize('corruption', ['source', 'hash', 'reference', 'permission', 'free_text_ban'])
def test_model_cannot_forge_scope_sources_references_or_permissions(sources, corruption):
    context = projections.prepare_review('cfg')
    value = response_for(context)
    if corruption == 'source':
        value['projections'][0]['source_id'] = 'other-task-source'
    elif corruption == 'hash':
        value['projections'][0]['source_hash'] = 'changed'
    elif corruption == 'reference':
        value['memory']['execution_refs'] = ['unrelated-.30']
    elif corruption == 'permission':
        value['memory']['allowed'] = True
    else:
        value['memory']['strategy_text'] = '旧 unknown 待核验，所以禁止加仓。'
    with pytest.raises(ValueError):
        projections.validate_review(value, context)


def test_one_normal_review_saves_new_memory_and_recent_projections_once(sources, monkeypatch):
    calls = model(monkeypatch)
    with memory_review_lease('cfg') as acquired:
        assert acquired
        result = memory_service.organize_memory('完整证据', CFG, operation_id='normal-new-run')
        memory_service.save_short_memory('2026-10-12 01:00:00', '2026-10-12 01:00:00', 'ETH/USDT', 'cfg', result, '', 1,
                                         source_summary_ids=[sources], run_id='new-run')
    assert len(calls) == 1
    stored = get_review_result('normal-new-run')
    assert stored['projection_metadata']['projection_version'] == projections.VERSION
    assert all(s['projection']['status'] == 'ready' for s in projections.ensure_sources('cfg'))
    assert not projections.process_projection_updates('cfg', CFG)
    assert len(calls) == 1


def test_long_strategy_projection_is_not_sliced(sources):
    context = projections.prepare_review('cfg')
    long_text = THESIS * 1000 + 'END条件'
    metadata = projections.validate_review(response_for(context, thesis=long_text), context)
    projections.persist_projections('cfg', metadata)
    assert long_text in load_decision_memory('cfg', CFG).short_memory_text


def test_task_symbol_change_does_not_reuse_previous_symbol_strategy(sources, monkeypatch):
    from backend.config import config
    model(monkeypatch)
    assert projections.process_projection_updates('cfg', CFG)
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: {**CFG, 'symbol': 'BTC/USDT'})
    memory = load_decision_memory('cfg', {**CFG, 'symbol': 'BTC/USDT'})
    assert THESIS not in memory.short_memory_text + memory.recent_summaries_text
    assert '尚无已发布' in memory.short_memory_text


def test_background_projection_lease_backoff_restart_and_retry_bound(sources, monkeypatch):
    calls = model(monkeypatch, failure=True)
    with memory_review_lease('cfg') as acquired:
        assert acquired and not projections.process_projection_updates('cfg', CFG)
    assert not calls
    for attempt in range(1, 4):
        assert not projections.process_projection_updates('cfg', CFG)
        rows = projections.ensure_sources('cfg')
        assert all(s['projection']['attempts'] == attempt for s in rows)
        assert all(s['projection']['next_attempt'] > projections.time.time() for s in rows)
        assert not projections.process_projection_updates('cfg', CFG)
        assert len(calls) == attempt
        with database.get_db_conn() as conn:
            conn.execute('UPDATE decision_context_projections SET next_attempt=0')
            conn.commit()
    assert all(s['projection']['status'] == 'failed' for s in projections.ensure_sources('cfg'))
    assert not projections.process_projection_updates('cfg', CFG) and len(calls) == 3
    memory = load_decision_memory('cfg', CFG)
    assert '尚无已发布' in memory.short_memory_text
    assert LEGACY not in memory.short_memory_text + memory.recent_summaries_text


def test_projection_restart_after_result_saved_reuses_same_model_result(sources, monkeypatch):
    calls = model(monkeypatch)
    persist = projections.persist_projections
    failures = []
    def crash_once(*args):
        if not failures:
            failures.append(1)
            raise RuntimeError('simulated process interrupted after durable review result')
        return persist(*args)
    monkeypatch.setattr(projections, 'persist_projections', crash_once)
    assert not projections.process_projection_updates('cfg', CFG)
    with database.get_db_conn() as conn:
        conn.execute('UPDATE decision_context_projections SET next_attempt=0')
        conn.commit()
    assert projections.process_projection_updates('cfg', CFG)
    assert len(calls) == 1
    assert all(s['projection']['status'] == 'ready' for s in projections.ensure_sources('cfg'))


def test_existing_worker_queues_projection_without_new_summary_job(sources, monkeypatch):
    from backend.config import config
    submit = Mock(return_value=Mock(done=lambda: False))
    process = Mock(return_value=True)
    monkeypatch.setattr(projections, 'process_projection_updates', process)
    monkeypatch.setattr(memory_updates, '_executor', Mock(submit=submit))
    monkeypatch.setattr(memory_updates, '_active', {})
    monkeypatch.setattr(config, 'get_all_symbol_configs', lambda: [CFG])
    memory_updates.tick_memory_updates()
    assert submit.call_count == 1
    queued_function, *queued_args = submit.call_args.args
    queued_function(*queued_args)
    process.assert_called_once_with('cfg', CFG)
    memory_updates.tick_memory_updates()
    assert submit.call_count == 1


def test_old_identity_from_memory_uses_background_evidence_and_explicit_unavailable_state(real_short, monkeypatch):
    from backend.utils.decision_record import append_execution_receipts
    service, exchange = real_short
    exchange.timeout_next = True
    original = call('close', dict(side='SHORT', close_all=True, order_type='market', reason='exit'), 'old-close')
    record = {'messages': [], 'execution_results': [{'tool': 'close', 'tool_call_id': 'old-close', 'result': json.dumps(original)}]}
    summary = dict(config_id='cfg', symbol='ETH/USDT', decision_json=json.dumps(record))
    recover('old-close')
    text = append_execution_receipts('旧摘要', summary)
    from backend.utils.decision_record import memory_execution_evidence
    evidence = memory_execution_evidence(summary)[0]
    assert text == '旧摘要'
    assert json.loads(evidence['result'])['status'] == 'unknown'
    assert evidence['current_evidence']['latest']['status'] == 'completed'
    assert json.loads(summary['decision_json']) == record
    monkeypatch.setattr('backend.utils.trade_recovery.operation_evidence', lambda *a, **k: {'old-close': {'resolution': 'unavailable'}})
    assert append_execution_receipts('', summary) == ''
    assert memory_execution_evidence(summary)[0]['current_evidence']['resolution'] == 'unavailable'


@pytest.mark.parametrize('custom_placeholders', [True, False])
def test_agent_and_bound_chat_use_current_state_without_history_or_old_bans(sources, monkeypatch, tmp_path, custom_placeholders):
    from backend.agent import agent_graph, chat_graph
    from backend.agent.agent_models import AgentState
    from langchain_core.messages import AIMessageChunk, ToolMessage, HumanMessage
    model(monkeypatch)
    assert projections.process_projection_updates('cfg', CFG)
    with database.get_db_conn() as conn:
        conn.execute("UPDATE trade_action_runs SET status='completed',result=? WHERE operation_id='old-close'",
                     (json.dumps({'operation_id': 'old-close', 'status': 'completed', 'cleanup': {'status': 'completed'}}),))
        for n in range(14):
            conn.execute('INSERT INTO trade_action_runs VALUES(?,?,?,?,?,?,?,?)',
                         ('cfg', f'irrelevant-history-{n}', 'ETH/USDT', 'h', 'completed',
                          json.dumps({'receipt_version': 1, 'status': 'completed', 'old_text': 'HISTORICAL_RECEIPT'}), 0, n))
        conn.commit()
    snapshot = {'positions': [dict(side='LONG', amount=.15, price=100)], 'entries': [
        dict(order_id='mock:entry-A', pos_side='LONG', amount=.10, remaining=.10, price=95, status='OPEN')],
        'exits': [dict(order_id='mock:' + identity, pos_side='LONG', order_type=kind, quantity_mode='fixed',
                      remaining=quantity, trigger_price=price, status='WAITING_TRIGGER') for identity, kind, quantity, price in [
            ('sl-A', 'stop_market', .15, 90), ('tp-A', 'take_profit_market', .08, 110), ('tp-B', 'take_profit_market', .07, 120)]]}
    market = Mock()
    market.get_market_analysis.return_value = {'analysis': {'15m': {'price': 100}}, 'sentiment': {}}
    market.get_account_status.return_value = {'balance': 1000, 'mock_open_orders': [], 'independent_exits': snapshot}
    monkeypatch.setattr(agent_graph, 'MarketTool', lambda **_: market)
    monkeypatch.setattr(agent_graph, 'fetch_news_risk_context', lambda _: {'headline': 'NEWS_RETAINED'})
    monkeypatch.setattr(agent_graph, 'format_market_data_to_text', lambda _: 'MARKET_RETAINED NEWS_RETAINED')
    monkeypatch.setattr(agent_graph.global_config, 'get_leverage', lambda _: 1)
    template = '用户策略纪律\n{formatted_market_data}\n'
    if custom_placeholders:
        template += '{positions_text}\n{orders_text}\n{short_memory_text}\n{recent_summaries_text}'
    path = tmp_path / 'custom.txt'
    path.write_text(template, encoding='utf-8')
    cfg = {**CFG, 'api_key': 'offline', 'prompt_file': str(path)}
    failed_memory_calls = model(monkeypatch, failure=True)
    monkeypatch.setattr(chat_graph, '_resolve_chat_config', lambda _: cfg)
    config = {'configurable': {'config_id': 'cfg', 'agent_config': cfg}}
    prompt = agent_graph.start_node(AgentState(symbol='ETH/USDT', messages=[], market_context={}, account_context={}), config).messages[0].content
    chat = chat_graph.start_node({'q': '检查当前仓位'}, config)
    assert prompt == chat['system_prompt']
    for identity in ('mock:entry-A', 'mock:sl-A', 'mock:tp-A', 'mock:tp-B'):
        assert prompt.count(identity) == 1
    for forbidden in ('HISTORICAL_RECEIPT', 'irrelevant-history', '旧 unknown', '禁止加仓', '最新原操作核验记录', '[当前交易核验记录]'):
        assert forbidden not in prompt
    assert 'MARKET_RETAINED NEWS_RETAINED' in prompt and THESIS in prompt and '多仓 0.15' in prompt
    assert path.read_text(encoding='utf-8') == template
    with database.get_db_conn() as conn:
        for n in range(100):
            conn.execute('INSERT INTO trade_action_runs VALUES(?,?,?,?,?,?,?,?)',
                         ('cfg', f'more-history-{n}', 'ETH/USDT', 'h', 'completed',
                          json.dumps({'receipt_version': 1, 'status': 'completed'}), 0, n))
        conn.commit()
    assert agent_graph.start_node(AgentState(symbol='ETH/USDT', messages=[], market_context={}, account_context={}), config).messages[0].content == prompt
    assert chat_graph.start_node({'q': '再次检查'}, config)['system_prompt'] == prompt
    assert failed_memory_calls == []
    # The same turn's new tool result remains after the starting snapshot.
    captured = []
    llm = Mock()
    llm.bind_tools.return_value = llm
    llm.stream.side_effect = lambda messages, **_: captured.append(messages) or iter([AIMessageChunk(content='当前新事实已读取')])
    monkeypatch.setattr(chat_graph, 'build_chat_model', lambda **_: llm)
    monkeypatch.setattr(chat_graph, 'invoke_with_retry', lambda op, **_: op())
    monkeypatch.setattr(chat_graph, 'audited_invoke', lambda op, **_: op())
    monkeypatch.setattr('backend.utils.trade_recovery.recovery_context_text', Mock(side_effect=AssertionError('No repeated history injection')))
    history = [HumanMessage(content='调整'), AIMessage(content='', tool_calls=[{'name': 'close', 'args': {}, 'id': 'fresh'}]),
               ToolMessage(content='本轮新事实 FRESH_TOOL_RECEIPT', tool_call_id='fresh')]
    chat_graph.model_node({**chat, 'messages': history}, config)
    assert captured[0][0].content == prompt
    assert captured[0][-1].content == history[-1].content
    assert len(history) == 3


def test_ensure_sources_bounds_pending_sources_and_prepare_review_compacts_evidence(sources, monkeypatch):
    from backend.agent.context_projections import ensure_sources, prepare_review, review_input, MAX_PENDING_SOURCES
    # Insert 15 old pending summaries in database
    with database.get_db_conn() as conn:
        for n in range(15):
            conn.execute('INSERT INTO summaries(id,config_id,symbol,timestamp,strategy_logic) '
                         'VALUES(?,?,?,?,?)',
                         (1000 + n, 'cfg', 'ETH/USDT', f'2026-10-10 10:{n:02d}:00', f'旧策略摘要 {n}'))
        conn.commit()

    all_sources = ensure_sources('cfg')
    pending_sources = [s for s in all_sources if s['projection']['status'] == 'pending']
    # Must be bounded by MAX_PENDING_SOURCES
    assert len(pending_sources) <= MAX_PENDING_SOURCES

    # Test prepare_review evidence is compacted
    context = prepare_review('cfg')
    assert len(context['sources']) <= MAX_PENDING_SOURCES
    # If any evidence is present, ensure continuation and recovery are stripped
    for ref, ev in context['evidence'].items():
        if isinstance(ev.get('latest'), dict):
            assert 'continuation' not in ev['latest']
            assert 'recovery' not in ev['latest']
            assert 'admission' not in ev['latest']

    # Test review_input output is clean
    input_text = review_input(context)
    assert '策略来源' in input_text
    assert 'current_execution_evidence' not in input_text
    assert 'operation_id' not in input_text
    assert 'continuation' not in input_text
    assert 'previous_assessment' not in input_text


def test_canceled_order_without_fill_pending_converges_without_spin(real_short):
    service, exchange = real_short
    plan = {
        'symbol': 'ETH/USDT',
        'side': 'SHORT',
        'account_scope': service.account_scope,
        'entries': [],
        'exits': [
            {
                'id': 'algo-123',
                'client_id': 'c123',
                'status': 'canceled',
                'filled': 0.0,
                'fill_pending': False,
                'exit_type': 'stop_market',
                'attempt_count': 5,
            }
        ]
    }
    # First collection should mark fill_verified and avoid incrementing attempt_count
    service._collect_order_evidence(plan)
    rec = plan['exits'][0]
    assert rec.get('fill_verified') is True
    assert rec.get('attempt_count') == 5
    assert 'next_retry_at' not in rec

