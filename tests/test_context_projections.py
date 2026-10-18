import json
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from backend import database
from backend.agent import context_projections as projections, memory_agent, memory_service, memory_updates
from backend.agent.decision_context import load_decision_memory
from backend.agent.memory_workflow import memory_review_lease, get_review_result
from backend.database.schema import initialize_schema
from backend.utils.trade_context import project_trade_state, render_orders
from test_trade_tool_integration import simulation, real_short, call, recover


CFG = dict(config_id='cfg', symbol='ETH/USDT', mode='STRATEGY', exit_mode='independent_exits', model='offline')
THESIS = '等四小时收盘确认前不加仓；支撑95，突破110再评估。'
LEGACY = THESIS


@pytest.fixture
def sources(simulation, monkeypatch):
    from backend.config import config
    monkeypatch.setattr(config, 'get_config_by_id', lambda cid: CFG if cid == 'cfg' else None)
    with database.get_db_conn() as conn:
        record = {'messages': [], 'execution_results': [{'tool': 'close', 'tool_call_id': 'old-close',
                  'result': json.dumps({'operation_id': 'old-close', 'status': 'unknown'})}]}
        sid = conn.execute('INSERT INTO summaries(config_id,symbol,timestamp,strategy_logic,decision_json) VALUES(%s,%s,%s,%s,%s) RETURNING id',
                           ('cfg', 'ETH/USDT', '2026-10-01 01:00:00', LEGACY, json.dumps(record))).fetchone()[0]
        conn.execute('INSERT INTO trade_action_runs(config_id,operation_id,symbol,request_hash,status,result,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                     ('cfg', 'old-close', 'ETH/USDT', 'hash', 'unknown', json.dumps({'operation_id': 'old-close', 'status': 'unknown', 'side': 'LONG'}), 0, 0))
        conn.commit()
    database.save_short_memory('2026-10-10 00:00:00', '2026-10-10 04:00:00', 'ETH/USDT', 'cfg', LEGACY, '', 1,
                               source_summary_ids=[sid])
    monkeypatch.setattr(memory_agent, 'invoke_with_retry', lambda op, **_: op())
    monkeypatch.setattr(memory_agent, '_start_audit', lambda *a: None)
    monkeypatch.setattr(memory_agent, '_finish_audit', lambda *a, **k: None)
    return sid


def model(monkeypatch, *, failure=False):
    captured = []
    def invoke(messages):
        captured.append(messages)
        if failure:
            raise RuntimeError('model offline')
        return AIMessage(content=THESIS)
    monkeypatch.setattr(memory_agent, 'build_chat_model', lambda **_: Mock(invoke=invoke))
    return captured


def publish():
    metadata = projections.capture_sources('cfg')
    for source in projections.current_sources('cfg'):
        refs = projections.source_refs('cfg', source)
        with database.get_db_conn() as conn:
            projections.publish_text(conn, 'cfg', source['source_kind'], source['row'], refs)
            conn.commit()
    return {**metadata, 'summary': THESIS}


def test_plain_text_publishes_once_and_replays_without_model(sources, monkeypatch):
    calls = model(monkeypatch)
    publish()
    with memory_review_lease('cfg'):
        for _ in range(2):
            text = memory_service.organize_memory('完整素材', CFG, operation_id='plain-review')
            memory_service.save_short_memory('2026-10-11', '2026-10-11', 'ETH/USDT', 'cfg', text, '', 1, run_id='plain-review')
    assert len(calls) == 1
    assert THESIS in load_decision_memory('cfg', CFG).short_memory_text
    assert get_review_result('plain-review')['projection_metadata']['output_format'] == 'plain_text'


@pytest.mark.parametrize('change', ['account', 'symbol', 'short_memory', 'strategy_summary'])
def test_source_changes_reject_stale_publication(sources, monkeypatch, change):
    from backend.config import config
    model(monkeypatch)
    with memory_review_lease('cfg'):
        text = memory_service.organize_memory('完整素材', CFG, operation_id='source-guard')
        if change == 'account':
            monkeypatch.setattr(projections.trade_evidence, 'configured_account_scope', lambda _: 'other')
        elif change == 'symbol':
            monkeypatch.setattr(config, 'get_config_by_id', lambda _: {**CFG, 'symbol': 'BTC/USDT'})
        else:
            with database.get_db_conn() as conn:
                conn.execute("UPDATE short_memories SET market_summary='edited'" if change == 'short_memory' else 'DELETE FROM summaries')
                conn.commit()
        before = database.get_short_memories('cfg', 10)
        with pytest.raises(ValueError, match='changed'):
            memory_service.save_short_memory('2026-10-14', '2026-10-14', 'ETH/USDT', 'cfg', text, '', 1)
        assert database.get_short_memories('cfg', 10) == before


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



pytestmark = pytest.mark.usefixtures("pg")
