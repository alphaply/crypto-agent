"""Regression coverage for complete memory bodies and selected-window evidence."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import database, database_agent_runs
from backend.app.api.agent_runs import router as runs_router
from backend.app.api.history import router as history_router
from backend.app.api.public import router as public_router
from backend.app.core.deps import get_current_user
from backend.database_schema import initialize_schema
from backend.utils.execution_ledger import ExecutionLedger, recent_activity_data, recent_activity_summary, register_order
from backend.utils.execution_metrics import metrics_context
from backend.utils.trade_review import daily_exchange_evidence, daily_execution_evidence


@pytest.fixture
def memory_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'memory-content.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)


@pytest.fixture
def memory_client(memory_db):
    app = FastAPI()
    app.include_router(history_router)
    app.include_router(public_router)
    app.include_router(runs_router)
    app.dependency_overrides[get_current_user] = lambda: {'username': 'test-admin'}
    with TestClient(app) as client:
        yield client


def long_text(label):
    return f'{label} START\n' + ('风险条件、执行结果与待核验信息。🧠\n' * 2500) + f'{label} END'


def test_long_short_memory_survives_storage_public_read_and_admin_edit(memory_client):
    memory, evidence = long_text('MEMORY'), long_text('EVIDENCE')
    database.save_short_memory('2026-10-06 00:00:00', '2026-10-06 04:00:00', 'ETH/USDT', 'cfg', memory, evidence, 123)
    for path, params in (
        ('/api/public/short-memories', {'config_id': 'cfg', 'symbol': 'ETH/USDT'}),
        ('/api/history/short-memories', {'config_id': 'cfg'}),
    ):
        response = memory_client.get(path, params=params)
        assert response.status_code == 200
        row = response.json()['short_memories'][0]
        assert row['market_summary'] == memory
        assert row['position_summary'] == evidence

    updated = long_text('EDITED')
    response = memory_client.put('/api/history/short-memories', json={
        'config_id': 'cfg', 'bucket_start': '2026-10-06 00:00:00',
        'market_summary': updated, 'position_summary': evidence,
    })
    assert response.status_code == 200
    assert response.json()['updated'] == 1
    saved = database.get_short_memory('cfg', '2026-10-06 00:00:00')
    assert saved['market_summary'] == updated
    assert saved['position_summary'] == evidence


def test_long_daily_memory_survives_edit_list_and_export(memory_client):
    date_str = datetime.now(database.TZ_CN).strftime('%Y-%m-%d')
    summary = long_text('DAILY')
    database.save_daily_summary(date_str, 'ETH/USDT', 'cfg', summary, 123)
    params = {'config_id': 'cfg', 'symbol': 'ETH/USDT'}
    row = memory_client.get('/api/public/daily-summaries', params=params).json()['daily_summaries'][0]
    assert row['summary'] == summary
    updated = long_text('DAILY EDITED')
    response = memory_client.put('/api/history/daily-summaries', json={
        'config_id': 'cfg', 'date': date_str, 'summary': updated,
    })
    assert response.status_code == 200
    row = memory_client.get('/api/history/daily-summaries', params=params).json()['daily_summaries'][0]
    assert row['summary'] == updated
    exported = memory_client.get('/api/history/daily-summaries/export', params=params)
    assert exported.status_code == 200
    assert updated in exported.text


def test_long_memory_run_inputs_outputs_and_receipts_survive_audit_api(memory_client):
    source, output, receipt = long_text('INPUT'), long_text('OUTPUT'), long_text('RECEIPT')
    run_id = database_agent_runs.start_agent_run('cfg', 'memory_review', 'offline-test', [
        {'role': 'user', 'content': source},
        {'role': 'tool', 'content': receipt, 'tool_call_id': 'receipt-1'},
    ])
    database_agent_runs.finish_agent_run(run_id, status='success', output=output,
                                       details={'rule_receipts': [{'result': receipt}]})
    response = memory_client.get(f'/api/agent-runs/{run_id}')
    assert response.status_code == 200
    saved = response.json()['run']
    assert saved['messages'][0]['content'] == source
    assert saved['messages'][1]['content'] == receipt
    assert saved['output'] == output
    assert saved['output_chars'] == len(output)
    assert saved['details']['rule_receipts'][0]['result'] == receipt


@pytest.mark.parametrize('reader', ['recent', 'window', 'daily'])
def test_legacy_decision_sources_restore_full_saved_analysis_without_changing_history(memory_db, reader):
    original = long_text('RESTORED SOURCE')
    clipped_receipt = 'close_position_real [call-1]: rejected because risk condition…（原文截取，非完整总结）'
    old_journal = '【执行状态（非成交证明）】failed=1\n' + clipped_receipt + '\n【决策摘录】' + original[:250] + '…（原文截取，非完整总结）'
    current = '【执行】本轮无工具调用；已有持仓和挂单以最新账户为准。\n【决策原文】\n' + long_text('CURRENT') + '\n引用旧标题【决策摘录】'
    records = [
        ('2026-10-05 01:00:00', old_journal, original),
        ('2026-10-05 02:00:00', 'old model summary with useful risk lesson', original),
        ('2026-10-05 03:00:00', None, original),
        ('2026-10-05 04:00:00', current, 'saved visible analysis'),
        ('2026-10-05 05:00:00', old_journal, None),
        ('2026-10-05 06:00:00', '【执行】本轮无工具调用；已有持仓和挂单以最新账户为准。\n【决策摘录】short complete decision', original),
    ]
    with database.get_db_conn() as conn:
        for timestamp, logic, content in records:
            conn.execute('INSERT INTO summaries(config_id,timestamp,strategy_logic,content) VALUES(?,?,?,?)',
                         ('cfg', timestamp, logic, content))
        conn.execute('INSERT INTO summaries(config_id,timestamp,strategy_logic,content) VALUES(?,?,?,?)',
                     ('other', '2026-10-05 06:00:00', 'other-task', 'other source'))
        conn.execute('INSERT INTO summaries(config_id,timestamp,strategy_logic,content) VALUES(?,?,?,?)',
                     ('cfg', '2026-10-04 06:00:00', 'before-window', 'before source'))
        conn.commit()
    if reader == 'recent':
        rows = database.get_recent_summary_logic('cfg', since_time='2026-10-05 00:00:00')
    elif reader == 'window':
        rows = database.get_summary_logic_between('cfg', '2026-10-05 00:00:00', '2026-10-06 00:00:00')
    else:
        rows = database.get_pending_daily_summary_data('cfg', '2026-10-05')
    assert len(rows) == len(records)
    by_time = {row['timestamp']: row['strategy_logic'] for row in rows}
    restored = by_time[records[0][0]]
    assert clipped_receipt in restored
    assert '旧工具回执自身已被截断' in restored
    assert '完整工具回执无法由分析正文恢复' in restored
    assert original in restored and restored.endswith('RESTORED SOURCE END')
    assert '【决策摘录】' not in restored
    assert by_time[records[1][0]] == records[1][1]
    assert original in by_time[records[2][0]]
    assert by_time[records[3][0]] == current
    assert by_time[records[4][0]] == old_journal
    assert by_time[records[5][0]] == records[5][1]
    assert all('_source_content' not in row and 'content' not in row for row in rows)
    with database.get_db_conn() as conn:
        saved = conn.execute("SELECT timestamp,strategy_logic,content FROM summaries WHERE config_id='cfg' AND timestamp>='2026-10-05' ORDER BY timestamp").fetchall()
    assert [tuple(row) for row in saved] == records


@pytest.mark.parametrize('reader', ['recent', 'window', 'daily', 'decision_prompt'])
def test_configured_model_summary_is_used_without_expanding_raw_analysis(memory_db, monkeypatch, reader):
    from backend.agent import agent_graph
    from backend.agent.agent_models import AgentState

    original = long_text('RAW ANALYSIS MUST STAY IN HISTORY')
    compressed = '\n模型压缩摘要：\n' + ('保留入场条件、风险与失效条件。\n' * 90) + '引用历史标记【决策摘录】及原文截取，非完整总结并不表示当前摘要被截断。\n摘要最后一个有效条件。\n'
    assert len(original) > len(compressed) * 10
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO summaries(config_id,timestamp,strategy_logic,content) VALUES(?,?,?,?)',
                     ('cfg', '2026-10-05 01:00:00', compressed, original))
        conn.commit()
    if reader == 'recent':
        rows = database.get_recent_summary_logic('cfg')
    elif reader == 'window':
        rows = database.get_summary_logic_between('cfg', '2026-10-05 00:00:00', '2026-10-06 00:00:00')
    elif reader == 'daily':
        rows = database.get_pending_daily_summary_data('cfg', '2026-10-05')
    else:
        market = Mock()
        market.get_market_analysis.side_effect = RuntimeError('offline test')
        monkeypatch.setattr(agent_graph, 'MarketTool', lambda **_: market)
        monkeypatch.setattr(agent_graph, 'resolve_prompt_template', lambda *_: 'Task {symbol}\n{recent_summaries_text}')
        monkeypatch.setattr(agent_graph.global_config, 'get_leverage', lambda *_: 2)
        state = AgentState(symbol='ETH/USDT', messages=[], market_context={}, account_context={}, history_context=[])
        result = agent_graph.start_node(state, {'configurable': {'config_id': 'cfg', 'agent_config': {'mode': 'STRATEGY'}}})
        prompt = result.messages[0].content
        assert compressed.strip() in prompt
        assert 'RAW ANALYSIS MUST STAY IN HISTORY' not in prompt
        assert '【历史来源说明】' not in prompt
        return
    assert len(rows) == 1
    assert rows[0]['strategy_logic'] == compressed
    assert '_source_content' not in rows[0] and 'content' not in rows[0]


def test_seven_day_memory_keeps_all_fills_cycles_and_observations(memory_db):
    now = database.TZ_CN.localize(datetime(2026, 10, 6, 12))
    end_ms = int(now.timestamp() * 1000)
    exchange = SimpleNamespace(
        id='offline', apiKey='offline-test', load_markets=lambda: None,
        market=lambda _: {'symbol': 'ETH/USDT:USDT', 'contract': True, 'swap': True, 'linear': True},
    )
    ledger = ExecutionLedger(exchange, 'cfg', 'ETH/USDT')
    register_order(ledger.scope, ledger.symbol, 'entry', 'cfg', 'entry', side='LONG')
    register_order(ledger.scope, ledger.symbol, 'close', 'cfg', 'agent_exit', side='LONG')
    fills = []
    for i in range(25):
        for offset, order, side in ((0, 'entry', 'buy'), (1, 'close', 'sell')):
            fills.append({'id': f'{i}-{offset}', 'order': order, 'side': side,
                          'timestamp': end_ms - 100000 + 2 * i + offset,
                          'price': 100 + offset, 'amount': 1, 'cost': 100 + offset})
    ledger.ingest(fills)
    with database.get_db_conn() as conn:
        for i in range(25):
            value = {'flat_observed_at': end_ms - 90000 + i, 'samples': i + 1}
            conn.execute('INSERT INTO execution_episodes VALUES(?,?,?,?,?)',
                         (f'episode-{i}', ledger.scope, 'cfg', ledger.symbol, json.dumps(value)))
        # Older observations and other task/account/symbol data must stay out.
        for key, scope, cfg, symbol, observed in (
            ('old', ledger.scope, 'cfg', ledger.symbol, end_ms - 8 * 86400000),
            ('future', ledger.scope, 'cfg', ledger.symbol, end_ms + 1000),
            ('other-task', ledger.scope, 'other', ledger.symbol, end_ms - 1000),
            ('other-scope', 'other', 'cfg', ledger.symbol, end_ms - 1000),
            ('other-symbol', ledger.scope, 'cfg', 'BTC/USDT:USDT', end_ms - 1000),
        ):
            conn.execute('INSERT INTO execution_episodes VALUES(?,?,?,?,?)',
                         (key, scope, cfg, symbol, json.dumps({'flat_observed_at': observed, 'samples': key})))
        conn.commit()
    review = metrics_context('cfg', end_ms - 7 * 86400000, end_ms, ledger.scope, ledger.symbol)
    assert [item['samples'] for item in review['details']] == list(range(1, 26))
    assert review['observation_count'] == 25 and review['omitted_details'] == 0
    data = recent_activity_data('cfg', 'ETH/USDT', now)
    assert data['fill_count'] == len(data['recent_fills']) == 50
    assert data['omitted_details'] == 0
    assert data['position_cycles']['completed_count'] == len(data['position_cycles']['completed']) == 25
    assert data['position_cycles']['omitted_completed'] == 0
    assert {item['id'] for item in data['recent_fills']} == {item['id'] for item in fills}
    text = recent_activity_summary('cfg', 'ETH/USDT', now)
    payload = json.loads(text.split('完整窗口成交、持仓周期与执行观察（与以上统计属于同一批证据，不重复计算盈亏）：\n')[1])
    assert payload == {key: value for key, value in data.items() if key not in {'from_ms', 'through_ms'}}
    assert recent_activity_summary('cfg', 'ETH/USDT', now + timedelta(seconds=1)) == text


def test_daily_local_evidence_keeps_more_than_200_records_and_protection_events(memory_db):
    day = '2026-10-05'
    with database.get_db_conn() as conn:
        for i in range(205):
            timestamp = f'{day} 12:{i // 60:02d}:{i % 60:02d}'
            conn.execute('INSERT INTO trade_history(trade_id, config_id, timestamp, symbol, realized_pnl) VALUES(?,?,?,?,?)',
                         (f'trade-{i}', 'cfg', timestamp, 'ETH/USDT', i))
            conn.execute('INSERT INTO real_protection_events(timestamp,config_id,symbol,payload) VALUES(?,?,?,?)',
                         (timestamp, 'cfg', 'ETH/USDT', json.dumps({'revision': i})))
        conn.execute('INSERT INTO trade_history(trade_id,config_id,timestamp) VALUES(?,?,?)',
                     ('outside-day', 'cfg', '2026-10-04 12:00:00'))
        conn.commit()
    text = daily_execution_evidence('cfg', day)
    payload = json.loads(text[text.index('{'):])
    assert len(payload['trade_history']) == 205
    assert [item['trade_id'] for item in payload['trade_history']] == [f'trade-{i}' for i in range(205)]
    assert [item['plan']['revision'] for item in payload['protection_changes']] == list(range(205))
    assert payload['real_fill_totals']['fill_count'] == 205


def test_daily_spot_evidence_keeps_all_fetched_fills_after_deduplication(monkeypatch):
    day = '2026-10-05'
    start = database.TZ_CN.localize(datetime.fromisoformat(day))
    start_ms = int(start.timestamp() * 1000)
    end_ms = int((start + timedelta(days=1)).timestamp() * 1000)
    fills = [{'id': str(i), 'timestamp': start_ms + i, 'order': f'order-{i}', 'side': 'buy'} for i in range(205)]
    exchange = SimpleNamespace(fetch_my_trades=lambda *_, **__: [*fills, fills[0], {'id': 'outside-day', 'timestamp': end_ms}])
    monkeypatch.setattr('backend.utils.market_data.MarketTool', lambda **_: SimpleNamespace(exchange=exchange))
    text = daily_exchange_evidence({'mode': 'SPOT_DCA', 'config_id': 'cfg', 'symbol': 'ETH/USDT'}, day)
    payload = json.loads(text[text.index('{'):])
    assert payload['returned_fill_count'] == len(payload['fills']) == 205
    assert payload['omitted_detail_count'] == 0
    assert [item['id'] for item in payload['fills']] == [str(i) for i in range(205)]
