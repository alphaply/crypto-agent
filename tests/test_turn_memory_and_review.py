from backend.agent import memory_service, decision_context
import json
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage
import pytest

import backend.database as database
from backend.database_schema import initialize_schema
from backend.agent import agent_graph
from backend.agent import memory_updates
from test_trade_tool_integration import simulation, real_short, call


def run_persisted_memory(config_id, cfg, strategy, messages):
    from uuid import uuid4
    report = {'execution_results': [message.content for message in messages if isinstance(message, ToolMessage)]}
    run_id = str(uuid4())
    database.save_summary(cfg.get('symbol', 'ETH/USDT'), 'test', strategy, strategy,
                          config_id=config_id, run_id=run_id, report_json=json.dumps(report))
    return memory_updates.process_memory_update(config_id, run_id, {**cfg, 'config_id': config_id})


@pytest.mark.parametrize('writer', ['per_run', 'rolling'])
def test_actual_memory_model_receives_latest_identity_in_both_writers(real_short, monkeypatch, writer):
    from backend.agent import memory_agent, context_projections
    from test_context_projections import response_for
    from unittest.mock import Mock
    from datetime import datetime
    _, exchange = real_short
    exchange.timeout_next = True
    original = call('close', dict(side='SHORT', close_all=True, order_type='market', reason='exit'), 'memory-close')
    raw = json.dumps({'messages': [], 'execution_results': [
        {'tool': 'close', 'tool_call_id': 'memory-close', 'result': json.dumps(original)}]})
    call('get_trade_operation', {'target_operation_id': 'memory-close'}, 'read-memory-close')
    stamp = '2026-10-13 10:00:00'
    with database.get_db_conn() as conn:
        sid = conn.execute('INSERT INTO summaries(config_id,symbol,timestamp,strategy_logic,decision_json) VALUES(?,?,?,?,?)',
                           ('cfg', 'ETH/USDT', stamp, '本轮策略条件只出现一次', raw)).lastrowid
        conn.commit()
    cfg = dict(config_id='cfg', symbol='ETH/USDT', mode='REAL', exit_mode='independent_exits', model='offline')
    monkeypatch.setattr(memory_service.global_config, 'get_config_by_id', lambda cid: cfg if cid == 'cfg' else None)
    sent = []
    def invoke(messages):
        sent.append(messages[-1].content)
        return AIMessage(content=json.dumps(response_for(context_projections.prepare_review('cfg'))))
    monkeypatch.setattr(memory_agent, 'build_chat_model', lambda **_: Mock(invoke=invoke))
    monkeypatch.setattr(memory_agent, 'invoke_with_retry', lambda op, **_: op())
    monkeypatch.setattr(memory_service, 'format_recent_position_history_for_memory', lambda *a: '本地业绩证据')
    if writer == 'per_run':
        memory_updates.enqueue_memory_update('cfg', 'evidence-run', sid)
        assert memory_updates.process_memory_update('cfg', 'evidence-run', cfg)
    else:
        assert memory_service.generate_rolling_short_memory_for_config('cfg', cfg,
            now_cn=memory_service.TZ_CN.localize(datetime(2026, 10, 13, 10, 1)))
    assert len(sent) == 1
    assert sent[0].count('本轮策略条件只出现一次') == 1
    assert '实际工具回执' not in sent[0]
    assert 'memory-close' not in sent[0]
    assert 'resolution' not in sent[0]
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT decision_json FROM summaries WHERE id=?', (sid,)).fetchone()[0] == raw


@pytest.fixture
def local_db(tmp_path):
    with patch.object(database, 'DB_NAME', str(tmp_path / 'test.db')):
        with database.get_db_conn() as conn:
            initialize_schema(conn)
        yield


@pytest.mark.parametrize('summary_type', ['strategy'])
@pytest.mark.parametrize('custom', [False, True])
def test_summary_preserves_facts_without_forcing_rule_review(monkeypatch, summary_type, custom):
    from unittest.mock import Mock
    from backend.utils.trading_policy import SUMMARY_FACT_POLICY

    llm = Mock()
    llm.invoke.return_value = AIMessage(content='规则保留，尚无新增完整平仓样本。')
    monkeypatch.setattr(agent_graph, 'build_chat_model', lambda **_: llm)
    cfg = {'model': 'offline-test', 'summarizer': {}}
    if custom:
        cfg['summarizer'][f'{summary_type}_prompt'] = 'Custom summary: {content}'
    result = agent_graph.summarize_content('source with no rule update receipt', cfg, summary_type)
    sent = llm.invoke.call_args.args[0][0].content
    assert 'source with no rule update receipt' in sent
    assert sent.count(SUMMARY_FACT_POLICY) == 1
    assert '未提供复盘结论时注明未记录' not in sent
    assert ('Custom summary:' in sent) == custom
    assert result == '规则保留，尚无新增完整平仓样本。'


@pytest.mark.parametrize('summary_type', ['daily', 'short_memory'])
def test_strategy_summarizer_rejects_retired_modes_before_model_call(monkeypatch, summary_type):
    from unittest.mock import Mock
    model = Mock(side_effect=AssertionError('Must not construct a model for retired summary modes'))
    monkeypatch.setattr(agent_graph, 'build_chat_model', model)
    with pytest.raises(ValueError, match='Unsupported summary type'):
        agent_graph.summarize_content('evidence', {}, summary_type)
    model.assert_not_called()


def test_explicit_turn_review_updates_bounded_memory_with_execution_evidence(local_db):
    database.save_short_memory('2026-01-01', '2026-01-01', 'ETH/USDT', 'cfg', 'old short plan', '', 1)
    messages = [AIMessage(content='new long plan', tool_calls=[{'id': 'c', 'name': 'open_position_real', 'args': {}}]),
                ToolMessage(content='Order rejected: no funds', tool_call_id='c')]
    with patch.object(memory_service, 'organize_memory', return_value='new plan; entry rejected') as summarize:
        assert run_persisted_memory('cfg', {'symbol': 'ETH/USDT'}, 'reverse to long', messages)
    source = summarize.call_args.args[0]
    assert '收益与回撤' not in source
    assert '起点收益率' not in source
    assert 'old short plan' not in source and 'reverse to long' in source
    assert '最多600字' not in source
    assert '分为【当前假设】' not in source
    assert source.startswith('过去4h滚动窗口：')
    assert 'Order rejected' not in source
    assert database.get_short_memories('cfg', 1)[0]['market_summary'] == 'new plan; entry rejected'


def test_memory_failure_preserves_previous_memory_without_strategy_fallback(local_db):
    database.save_short_memory('2026-01-01', '2026-01-02', 'ETH/USDT', 'cfg', 'previous verified memory', 'previous evidence', 1)
    previous = database.get_short_memories('cfg', 10)
    with patch.object(memory_service, 'organize_memory', return_value=''):
        assert not run_persisted_memory('cfg', {}, 'old trade invalidated', [])
    assert database.get_short_memories('cfg', 10) == previous


def test_short_memory_includes_local_7d_closed_positions_and_stats(local_db):
    now = database.datetime.now(database.TZ_CN)
    opened = now - database.timedelta(hours=2)
    with database.get_db_conn() as conn:
        conn.execute(
            """INSERT INTO execution_position_history(
                   position_id,account_scope,config_id,symbol,side,opened_at_ms,closed_at_ms,
                   entry_price,close_price,amount,take_profit,stop_loss,realized_pnl,
                   fees_json,exit_reason,updated_at_ms,payload
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                'test_pos_1', 'scope', 'cfg', 'ETH/USDT:USDT', 'SHORT',
                int(opened.timestamp() * 1000), int(now.timestamp() * 1000),
                2500.0, 2450.0, 0.1, 2440.0, 2520.0, 5.0,
                '{}', 'take_profit', int(now.timestamp() * 1000), '{}',
            ),
        )
        conn.commit()

    with patch.object(memory_service, 'organize_memory', return_value='updated memory') as summarize:
        assert run_persisted_memory('cfg', {'mode': 'REAL', 'symbol': 'ETH/USDT'}, 'wait for pullbacks', [])

    source = summarize.call_args.args[0]
    assert '过去7天平仓记录' not in source and '2500.00' not in source
    assert 'wait for pullbacks' in source
    saved = database.get_short_memories('cfg', 1)[0]
    assert saved['position_summary'] == ''
    details = database.get_closed_positions_7d('cfg', 'ETH/USDT', mode='REAL')
    assert details[0]['entry_price'] == 2500.0 and details[0]['realized_pnl'] == 5



@pytest.mark.parametrize('writer', ['rolling'])
def test_all_memory_writers_receive_equity_and_ledger_once(local_db, monkeypatch, writer):
    now = agent_graph.TZ_CN.localize(__import__('datetime').datetime(2026, 9, 28, 10, 0))
    cfg = {'config_id': 'cfg', 'symbol': 'ETH/USDT', 'mode': 'REAL'}
    monkeypatch.setattr(agent_graph.global_config, 'get_all_symbol_configs', lambda: [cfg])
    with database.get_db_conn() as conn:
        conn.executemany(
            'INSERT INTO balance_history(timestamp,symbol,config_id,total_equity) VALUES(?,?,?,?)',
            [('2026-09-01', 'ETH/USDT', 'cfg', 100),
             ('2026-09-02', 'ETH/USDT', 'cfg', 80),
             ('2026-09-03', 'ETH/USDT', 'cfg', 92),
             ('2026-09-04', 'BTC/USDT', 'cfg', 5000),
             ('2026-09-04', 'ETH/USDT', 'other', 8000)],
        )
        if writer != 'empty_bucket':
            conn.execute(
                'INSERT INTO summaries(timestamp,symbol,config_id,strategy_logic) VALUES(?,?,?,?)',
                ('2026-09-28 09:00:00', 'ETH/USDT', 'cfg', 'new support'),
            )
        conn.commit()
    database.save_short_memory('2026-09-28 00:00:00', '2026-09-28 04:00:00',
                               'ETH/USDT', 'cfg', 'previous strategy and lessons', '', 1)
    with patch.object(memory_service, 'organize_memory', return_value='compressed strategy and performance') as summarize:
        changed = memory_service.generate_rolling_short_memory_for_config('cfg', cfg, now_cn=now)
    assert changed
    source = summarize.call_args.args[0]
    assert '权益' not in source and '平仓记录' not in source and '战绩统计' not in source
    assert 'new support' in source
    saved = database.get_short_memories('cfg', 1)[0]
    assert saved['position_summary'] == ''
    assert saved['market_summary'] == 'compressed strategy and performance'



@pytest.mark.parametrize('bad_summary', ['', 'Previous short memory: prompt echo'])
def test_memory_compression_failure_does_not_replace_previous_review(local_db, monkeypatch, bad_summary):
    evidence = (
        '截至快照: 2026-09-28 09:00 | 权益: 92.00 USDT | 样本: 3\n'
        '起点收益率: -8.00% | 当前回撤: 8.00% | 快照最大回撤: 20.00%\n'
        '【过去7天平仓记录（本地成交账本）】\n- detail-only-position-id\n'
        '完整平仓周期: 2 笔 | 胜率: 50% | 已确认累计盈亏（手续费前）: -1.00 USDT'
    )
    monkeypatch.setattr(memory_service, 'format_recent_position_history_for_memory', lambda *_: evidence)
    monkeypatch.setattr(memory_service, 'organize_memory', lambda *_, **__: bad_summary)
    database.save_short_memory('2026-09-28 00:00:00', '2026-09-28 04:00:00',
                               'ETH/USDT', 'cfg', 'previous reviewed risk', 'previous evidence', 1)
    previous = database.get_short_memories('cfg', 10)
    assert not run_persisted_memory('cfg', {'symbol': 'ETH/USDT'}, 'cancel invalid entry', [])
    assert database.get_short_memories('cfg', 10) == previous


def test_empty_window_failed_compression_keeps_previous_memory(local_db, monkeypatch):
    cfg = {'config_id': 'cfg', 'symbol': 'ETH/USDT', 'mode': 'REAL'}
    monkeypatch.setattr(agent_graph.global_config, 'get_all_symbol_configs', lambda: [cfg])
    monkeypatch.setattr(memory_service, 'organize_memory', lambda *_, **__: '')
    database.save_short_memory('2026-09-28 00:00:00', '2026-09-28 04:00:00',
                               'ETH/USDT', 'cfg', 'prior memory and risk', '', 1)
    now = agent_graph.TZ_CN.localize(__import__('datetime').datetime(2026, 9, 28, 10, 0))
    assert not memory_service.generate_rolling_short_memory_for_config('cfg', now_cn=now)
    assert database.get_short_memories('cfg', 1)[0]['market_summary'] == 'prior memory and risk'
    assert 'prior memory and risk' not in decision_context.format_short_memory_for_llm('cfg')
