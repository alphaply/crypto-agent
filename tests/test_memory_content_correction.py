"""Repair misplaced execution narrative without granting execution authority."""
import json

import pytest

from backend import database
from backend.agent import context_projections as projections, memory_agent as memory, memory_updates
from backend.agent import memory_contract_recovery as recovery
from backend.agent.decision_context import load_decision_memory
from backend.utils import llm_utils
from test_context_projections import CFG, THESIS, response_for, sources, simulation
from test_memory_projection_contract import response, scripted_model
from test_memory_contract_recovery import fail_old_sources, fail_old_job, add_recent_summaries, background, job, legacy_error


OLD_ERROR = 'Strategy projection contains an execution permission/restriction'
NARRATIVE = '旧 unknown 未核验，所以禁止加仓。'


@pytest.mark.parametrize('text', [
    '旧 unknown 仅供审计。等四小时收盘确认前不加仓。',
    '回执不能作为成交证明；等四小时收盘确认前不加仓。',
    '历史回执已核验。等四小时收盘前不加仓。',
])
def test_unrelated_sentences_and_execution_lessons_do_not_block_review(sources, monkeypatch, text):
    context = projections.prepare_review('cfg')
    calls, _, _ = scripted_model(monkeypatch, response(response_for(context, thesis=text)))
    result = memory.run_memory_review('原始资料', CFG, operation_id='no-false-positive')
    assert result.status == 'completed' and len(calls) == 1


@pytest.mark.parametrize('text', [NARRATIVE, '旧 unknown 未核验。禁止加仓。', 'blocked_actions=[]，已解锁', 'allowed=true'])
def test_content_correction_retains_evidence_and_separate_audits(sources, monkeypatch, text):
    context = projections.prepare_review('cfg')
    bad = response_for(context, thesis=THESIS + text)
    good = response_for(context)
    calls, costs, audits = scripted_model(monkeypatch, response(bad), response(good))
    result = memory.run_memory_review('原始资料', CFG, operation_id='content-correction')
    assert result.status == 'completed' and len(calls) == len(costs) == len(audits) == 2
    assert calls[0][1].content == calls[1][1].content
    assert 'unlinked_execution_notes' in calls[1][0].content
    assert [row[1]['status'] for row in audits] == ['error', 'success']
    assert audits[0][1]['details']['validation_error']['category'] == 'content_separation'
    assert audits[1][1]['details']['phase'] == 'content_correction'
    assert all(row[1]['details']['operation_id'] == 'content-correction' for row in audits)
    assert 'memory.strategy_text' in audits[0][1]['error']
    assert result.projection_metadata['memory']['strategy_text'] == THESIS


@pytest.mark.parametrize('first,second', [('content', 'content'), ('content', 'shape'), ('shape', 'content')])
def test_shape_and_content_share_one_correction_and_never_publish_bad_text(sources, monkeypatch, first, second):
    context = projections.prepare_review('cfg')
    outputs = {'content': response(response_for(context, thesis=NARRATIVE)), 'shape': response({'summary': THESIS})}
    calls, _, _ = scripted_model(monkeypatch, outputs[first], outputs[second])
    result = memory.run_memory_review('原始资料', CFG, operation_id='shared-correction')
    assert result.status == 'failed' and not result.projection_metadata and len(calls) == 2


def test_network_and_content_share_three_actual_calls(sources, monkeypatch):
    context = projections.prepare_review('cfg')
    calls, _, _ = scripted_model(monkeypatch, ConnectionError('offline'),
                                response(response_for(context, thesis=NARRATIVE)), response(response_for(context)))
    monkeypatch.setattr(memory, 'invoke_with_retry', llm_utils.invoke_with_retry)
    monkeypatch.setattr(llm_utils, 'get_llm_max_retries', lambda: 8)
    monkeypatch.setattr(llm_utils.time, 'sleep', lambda _: None)
    result = memory.run_memory_review('原始资料', CFG, operation_id='content-budget')
    assert result.status == 'completed' and len(calls) == 3


@pytest.mark.parametrize('no_original', [False, True])
def test_upgrade_recovers_reported_content_failure_through_background(sources, monkeypatch, no_original):
    ids = add_recent_summaries()
    if no_original:
        with database.get_db_conn() as conn:
            conn.execute('DELETE FROM short_memories')
            conn.commit()
        fail_old_job(ids[-1])
    fail_old_sources()
    with database.get_db_conn() as conn:
        for table in ('decision_context_projections', 'memory_update_jobs') if no_original else ('decision_context_projections',):
            conn.execute(f'UPDATE {table} SET error=?,review_contract_revision=1,contract_recovery=?',
                         (OLD_ERROR, json.dumps({'revision': 1, 'previous_error': 'old diagnostic'})))
        conn.commit()
    assert recovery.recover_legacy_failures('cfg', CFG)['projections'] == (3 if no_original else 4)
    context = projections.prepare_review('cfg')
    calls, _, _ = scripted_model(monkeypatch, response(response_for(context, thesis=NARRATIVE)), response(response_for(context)))
    background(monkeypatch)
    memory_updates.tick_memory_updates()
    view = load_decision_memory('cfg', CFG)
    assert THESIS in view.short_memory_text and view.recent_summaries_text.count('[截至') == 3
    assert 'unknown' not in view.short_memory_text + view.recent_summaries_text
    if no_original:
        assert job()['status'] == 'completed'
    with database.get_db_conn() as conn:
        rows = conn.execute("SELECT * FROM decision_context_projections WHERE contract_recovery<>'{}'").fetchall()
        assert all(json.loads(r['contract_recovery'])['previous_error'] == OLD_ERROR for r in rows)
        assert all(json.loads(r['contract_recovery'])['previous_recovery']['previous_error'] == 'old diagnostic' for r in rows)
        conn.execute("UPDATE decision_context_projections SET status='failed',attempts=3,error=?", (OLD_ERROR,))
        conn.commit()
    assert recovery.recover_legacy_failures('cfg', CFG)['projections'] == 0
    assert len(calls) == 2


def test_revision_two_does_not_reset_revision_one_format_failures(sources):
    fail_old_sources()
    with database.get_db_conn() as conn:
        conn.execute('UPDATE decision_context_projections SET review_contract_revision=1')
        conn.commit()
    assert recovery.recover_legacy_failures('cfg', CFG)['projections'] == 0
    assert not recovery.is_recoverable_contract_failure({'review_contract_revision': 1, 'error': legacy_error()})
    assert not recovery.is_recoverable_contract_failure({'review_contract_revision': 1, 'error': OLD_ERROR + ' forged suffix'})


def test_newer_projection_cannot_attest_old_job_and_repeated_skip_is_bounded(sources):
    fail_old_sources()
    fail_old_job(sources)
    with database.get_db_conn() as conn:
        conn.execute('UPDATE decision_context_projections SET review_contract_revision=1')
        conn.execute('UPDATE memory_update_jobs SET error=?,contract_recovery=?',
                     (OLD_ERROR, json.dumps({'revision': 1, 'previous_error': 'original diagnostic'})))
        conn.commit()
    assert recovery.recover_legacy_failures('cfg', CFG)['jobs'] == 0
    first = job()['contract_recovery']
    assert json.loads(first)['reason'] == 'source_scope_unverified'
    for _ in range(3):
        assert recovery.recover_legacy_failures('cfg', CFG)['jobs'] == 0
        assert job()['contract_recovery'] == first
    assert json.loads(first)['previous_recovery']['previous_error'] == 'original diagnostic'
