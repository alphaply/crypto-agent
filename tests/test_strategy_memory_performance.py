"""Regression for the supplied ETH history: strategy is independent of bookkeeping."""
import json
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import database
from backend.agent import context_projections as projections
from backend.agent.strategy_material import strategy_material, validate_strategy_text
from backend.app.api.history import router as history_router
from backend.app.api.stats import router as stats_router
from backend.app.core.deps import get_current_user
from backend.app.services.memory_view_service import strategy_memory_rows, memory_audit_detail
from backend.utils.decision_record import execution_identity
from backend.utils.performance_context import performance_data, profit_metric
from test_context_projections import CFG, THESIS, sources, simulation


ETH_MEMORY = '''市场判断：等待4h收盘确认或CPI后反应再决策。
关键价位：支撑2385，突破2565后重新评估；失效价2458。
两次Edit被拒（订单不属于本独立配置，operation_id=toolu_01Xd，resolution=unavailable）。
execute_trade_actions失败：无活跃独立持仓周期，completed=0、submitted=0、failed=1、not_executed=2。
operation_id=null，resolution=unresolved；解除前不新开仓、不换ID重试。
0.02多仓@2415及三笔外部挂单（TP2565/SL2458/限价2385）归属未核验。本任务无自有持仓。
统计起点权益591.12 USDT，截至快照权益589.00 USDT，回撤0.36%。
有效教训：亏损不是加杠杆、放宽止损或强行回本的理由。
{"window_complete":false,"unknown_owner_fill_count":5,"fill_count":0,"known_realized_pnl_before_fees":0,
"active_protection":[{"symbol":"ETH/USDT:USDT","state":"WAITING"}],"position_cycles":{"completed_count":0}}
'''


def test_supplied_memory_keeps_market_conditions_and_levels_only():
    text = strategy_material(ETH_MEMORY)
    for content in ('4h收盘', 'CPI', '2385', '2565', '2458', '亏损不是加杠杆'):
        assert content in text
    for content in ('operation_id', 'resolution', '周期', '多仓', '外部挂单', '解除', '权益', '{'):
        assert content not in text
    assert strategy_material(text) == text
    assert '[2385,2458]' in strategy_material('支撑区间[2385,2458]；突破2565确认。')


@pytest.mark.parametrize('noise', [
    'the existing order is unverified.',
    'reconcile the existing order before further actions.',
    'verify existing order',
    'the original submission remains unconfirmed.',
    'verify the original submission',
])
def test_english_order_diagnostics_preserve_market_conditions(noise):
    strategy = ('Wait for the four-hour close or CPI reaction; support [2385,2458]; '
                'Verify CPI reaction before choosing limit order prices above 2565.')
    text = strategy_material(strategy + '\n' + noise)
    assert noise not in text
    assert 'four-hour close or CPI reaction;' in text
    assert 'support [2385,2458];' in text
    assert 'Verify CPI reaction before choosing limit order prices above 2565.' in text
    assert validate_strategy_text(strategy)
    assert not validate_strategy_text(noise)


def test_refs_are_preserved_by_program_without_model_output(sources):
    metadata = projections.capture_sources('cfg')
    assert metadata['execution_refs'] == ['old-close']
    assert metadata['output_format'] == 'plain_text'


def test_null_and_tool_call_identity_never_invents_a_recovery_target():
    for result in ({'operation_id': None, 'status': 'unknown'},
                   {'status': 'failed', 'error': '订单不属于本独立配置'},
                   {'status': 'not_executed'}):
        assert execution_identity({'tool': 'edit', 'tool_call_id': 'toolu_01Xd',
                                   'result': json.dumps(result)}) is None


def test_projection_version_upgrade_preserves_raw_history_without_fallback(sources):
    raw = database.get_short_memories('cfg', 1)[0]
    from test_context_projections import publish
    publish()
    assert strategy_memory_rows([raw])[0]['market_summary'] == THESIS
    with database.get_db_conn() as conn:
        conn.execute('UPDATE decision_context_projections SET projection_version=1')
        conn.commit()
    assert strategy_memory_rows([raw])[0]['market_summary'] == ''
    assert database.get_short_memories('cfg', 1)[0] == raw
    assert memory_audit_detail(raw['id'])['short_memory']['market_summary'] == raw['market_summary']


def complete_ledger(**values):
    return {'window_complete': True, 'unknown_owner_fill_count': 0,
            'missing_pnl_count': 0, 'missing_fee_count': 0,
            'known_realized_pnl_before_fees': 0, 'fees_by_currency': {},
            'position_cycles': {'completed_count': 0, 'open_cycles': [{'remaining_base': .02}]}, **values}


def test_verified_zero_and_partial_cycle_realized_profit():
    assert profit_metric(complete_ledger())['amount'] == 0
    value = profit_metric(complete_ledger(known_realized_pnl_before_fees=12.0, fees_by_currency={'USDT': .5}))
    assert value['available'] and value['amount'] == 11.5
    for missing in ({'window_complete': False}, {'unknown_owner_fill_count': 5}, {'missing_pnl_count': 1},
                    {'missing_fee_count': 1}, {'fees_by_currency': {'BTC': .01}}):
        value = profit_metric(complete_ledger(**missing))
        assert not value['available'] and value['amount'] is None


def test_supplied_equity_change_and_incomplete_profit_are_distinct(simulation, monkeypatch):
    from backend.utils import execution_ledger
    monkeypatch.setattr('backend.utils.trade_evidence.configured_account_scope', lambda _: 'test-account')
    ledger = complete_ledger(window_complete=False, unknown_owner_fill_count=5, fill_count=0)
    monkeypatch.setattr(execution_ledger, 'execution_context', lambda *a, **k: '\n' + json.dumps(ledger))
    with database.get_db_conn() as conn:
        conn.executemany('INSERT INTO balance_history(config_id,symbol,timestamp,total_equity) VALUES(?,?,?,?)', [
            ('cfg', 'ETH/USDT', '2026-10-14 00:00:18', 591.12),
            ('cfg', 'ETH/USDT', '2026-10-14 12:00:09', 589.00)])
        conn.commit()
    data = performance_data('cfg', 'ETH/USDT', 'REAL', now=datetime(2026, 10, 14, 13))
    assert data['equity']['change'] == pytest.approx(-2.12)
    assert data['drawdown']['max_pct'] == pytest.approx(.3586412)
    assert data['profit']['amount'] is None and not data['profit']['available']
    assert data['profit']['reason'] == 'incomplete_evidence'
    assert 'details' not in data and 'ledger' not in data


def test_strategy_metrics_keep_zero_missing_values_and_window_separate(simulation):
    now = datetime(2026, 10, 14, 13)
    with database.get_db_conn() as conn:
        conn.executemany('INSERT INTO mock_orders(order_id,config_id,symbol,status,close_time,realized_pnl) '
                         'VALUES(?,?,?,?,?,?)', [
                             ('recent', 'cfg', 'ETH/USDT', 'CLOSED', '2026-10-14 12:00:00', 0),
                             ('older', 'cfg', 'ETH/USDT', 'CLOSED', '2026-10-12 12:00:00', 7),
                             ('other', 'other', 'ETH/USDT', 'CLOSED', '2026-10-14 12:00:00', 99)])
        conn.commit()
    daily = performance_data('cfg', 'ETH/USDT', 'STRATEGY', hours=24, now=now, include_details=True)
    weekly = performance_data('cfg', 'ETH/USDT', 'STRATEGY', hours=168, now=now)
    assert daily['profit']['available'] and daily['profit']['amount'] == 0
    assert len(daily['details']['closed_positions']) == 1
    assert weekly['profit']['amount'] == 7
    with database.get_db_conn() as conn:
        conn.execute("UPDATE mock_orders SET realized_pnl=NULL WHERE order_id='recent'")
        conn.commit()
    missing = performance_data('cfg', 'ETH/USDT', 'STRATEGY', hours=24, now=now)
    assert missing['profit']['amount'] is None and not missing['profit']['available']


def test_real_details_preserve_same_account_and_snapshot_boundaries(simulation, monkeypatch):
    from backend.utils import execution_ledger
    from backend.database import TZ_CN
    now = TZ_CN.localize(datetime(2026, 10, 14, 13))
    monkeypatch.setattr('backend.utils.trade_evidence.configured_account_scope', lambda _: 'test-account')
    monkeypatch.setattr(execution_ledger, 'execution_context', lambda *a, **k: '\n' + json.dumps(complete_ledger()))
    with database.get_db_conn() as conn:
        for name, scope, hour in [('in-window', 'test-account', 12), ('future', 'test-account', 14),
                                  ('other-account', 'other-account', 12)]:
            timestamp = int(now.replace(hour=hour).timestamp() * 1000)
            conn.execute('INSERT INTO execution_position_history(position_id,account_scope,config_id,symbol,side,'
                         'opened_at_ms,closed_at_ms,realized_pnl,fees_json,payload,updated_at_ms) '
                         'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                         (name, scope, 'cfg', 'ETH/USDT:USDT', 'LONG', timestamp - 60000, timestamp,
                          3, '{"USDT":0.1}', '{"missing_fees":false}', timestamp))
        conn.commit()
    data = performance_data('cfg', 'ETH/USDT', 'REAL', hours=24, now=now, include_details=True)
    closed = data['details']['closed_positions']
    assert [row['position_id'] for row in closed] == ['in-window']
    assert closed[0]['net_realized_pnl'] == pytest.approx(2.9)
    assert data['details']['as_of'] == data['as_of']


def test_summary_and_audit_endpoints_require_existing_auth(sources):
    app = FastAPI()
    app.include_router(stats_router)
    app.include_router(history_router)
    api = TestClient(app)
    raw = database.get_short_memories('cfg', 1)[0]
    paths = ['/api/stats/performance/cfg', '/api/stats/performance/cfg/details',
             f"/api/history/short-memories/{raw['id']}/details"]
    for path in paths:
        assert api.get(path).status_code == 401
    app.dependency_overrides[get_current_user] = lambda: {'sub': 'admin'}
    summary = api.get(paths[0]).json()
    assert summary['success'] and set(summary['performance']) >= {'equity', 'profit', 'drawdown'}
    assert 'details' not in summary
    detail = api.get(paths[1]).json()
    assert set(detail['details']) >= {'ledger', 'closed_positions', 'equity', 'as_of'}
    assert api.get(paths[2]).json()['short_memory'] == raw
    assert api.get('/api/stats/performance/cfg?hours=0').status_code == 400
    assert api.get('/api/stats/performance/absent').status_code == 404
