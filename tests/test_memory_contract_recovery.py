"""Upgrade failed work without trading, clearing ready views or retry loops."""
import json
from concurrent.futures import Future
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from backend import database
from backend.agent import context_projections as projections, memory_updates, memory_service
from backend.agent import memory_contract_recovery as recovery
from backend.agent.decision_context import load_decision_memory
from backend.agent.memory_workflow import memory_review_lease
from test_context_projections import CFG, LEGACY, THESIS, response_for, sources, simulation
from test_memory_projection_contract import response, scripted_model


def legacy_error():
    try:
        projections.ProjectionReview.model_validate({'summary': LEGACY})
    except ValidationError as exc:
        return str(exc)
    raise AssertionError('Legacy envelope unexpectedly valid')


def fail_old_sources():
    projections.ensure_sources('cfg')
    with database.get_db_conn() as conn:
        conn.execute("UPDATE decision_context_projections SET status='failed',attempts=3,error=?,review_contract_revision=0",
                     (legacy_error(),))
        conn.commit()


def add_recent_summaries():
    with database.get_db_conn() as conn:
        ids = []
        for hour in (2, 3):
            ids.append(conn.execute('INSERT INTO summaries(config_id,symbol,timestamp,strategy_logic) VALUES(?,?,?,?)',
                                   ('cfg', 'ETH/USDT', f'2026-10-01 0{hour}:00:00', LEGACY)).lastrowid)
        conn.commit()
    return ids


def fail_old_job(summary_id):
    memory_updates.enqueue_memory_update('cfg', 'old-run', summary_id)
    with database.get_db_conn() as conn:
        conn.execute("UPDATE memory_update_jobs SET status='failed',attempts=3,error=?,review_contract_revision=0",
                     (legacy_error(),))
        conn.commit()


def job():
    with database.get_db_conn() as conn:
        return dict(conn.execute("SELECT * FROM memory_update_jobs WHERE run_id='old-run'").fetchone())


def background(monkeypatch):
    from backend.config import config
    from backend.agent import tool_registry
    class InlineExecutor:
        def submit(self, fn, *args):
            future = Future()
            try:
                future.set_result(fn(*args))
            except Exception as exc:
                future.set_exception(exc)
            return future
    monkeypatch.setattr(memory_updates, '_executor', InlineExecutor())
    monkeypatch.setattr(memory_updates, '_active', {})
    monkeypatch.setattr(config, 'get_all_symbol_configs', lambda: [CFG])
    monkeypatch.setattr(memory_service, 'format_recent_position_history_for_memory', lambda *a: '离线历史证据')
    monkeypatch.setattr(tool_registry, 'run_trade_tool', Mock(side_effect=AssertionError('Recovery must not trade')))


@pytest.mark.parametrize('no_original', [False, True])
def test_background_first_publication_after_real_legacy_error(sources, monkeypatch, no_original):
    ids = add_recent_summaries()
    if no_original:
        with database.get_db_conn() as conn:
            conn.execute('DELETE FROM short_memories')
            conn.commit()
        fail_old_job(ids[-1])
    fail_old_sources()
    before = load_decision_memory('cfg', CFG)
    assert '尚无已发布' in before.short_memory_text and '尚无已发布' in before.recent_summaries_text
    # This is precisely the response shape from the production failure.
    context = projections.prepare_review('cfg')
    # Failed sources are not normally included until recovery requeues them.
    context['sources'] = projections.ensure_sources('cfg')
    calls, _, _ = scripted_model(monkeypatch, response({'summary': LEGACY}), response(response_for(context)))
    background(monkeypatch)
    memory_updates.tick_memory_updates()
    after = load_decision_memory('cfg', CFG)
    assert len(calls) == 2 and THESIS in after.short_memory_text and THESIS in after.recent_summaries_text
    assert after.recent_summaries_text.count('[截至') == 3
    assert '2026-10-01 03:00:00' in after.recent_summaries_text
    assert '旧 unknown' not in after.short_memory_text + after.recent_summaries_text
    if no_original:
        assert job()['status'] == 'completed'
        assert database.get_short_memories('cfg', 1)[0]['market_summary'] == THESIS
    else:
        assert database.get_short_memories('cfg', 1)[0]['market_summary'] == LEGACY
    with database.get_db_conn() as conn:
        saved = conn.execute('SELECT contract_recovery FROM decision_context_projections LIMIT 1').fetchone()
    assert json.loads(saved[0])['previous_error'] == legacy_error()
    # No new decision or manual retry is required; no repeat model call next tick.
    memory_updates.tick_memory_updates()
    assert len(calls) == 2


@pytest.mark.parametrize('error', [
    'model offline', 'authentication failed', 'Memory review response is incomplete: finish_reason=length',
    'Strategy projection contains an execution permission/restriction',
    'Some source mentioned ProjectionReview memory projections Field required',
    '1 validation error for OtherModel\nmemory\n  Field required [type=missing, input_value={}]',
    '2 validation errors for ProjectionReview\nmemory\n  Field required [type=missing, input_value={}]',
])
def test_only_exact_legacy_signature_matches(error):
    assert recovery.is_legacy_projection_failure(legacy_error())
    assert not recovery.is_legacy_projection_failure(error)


@pytest.mark.parametrize('exclude', ['new_revision', 'unrelated_error', 'partial', 'completed', 'edited', 'deleted', 'foreign_scope'])
def test_recovery_excludes_unrelated_and_invalid_projection_rows(sources, exclude):
    fail_old_sources()
    with database.get_db_conn() as conn:
        if exclude == 'new_revision':
            conn.execute('UPDATE decision_context_projections SET review_contract_revision=?', (recovery.CONTRACT_REVISION,))
        elif exclude == 'unrelated_error':
            conn.execute("UPDATE decision_context_projections SET error='authentication failed'")
        elif exclude in {'partial', 'completed'}:
            conn.execute('UPDATE decision_context_projections SET status=?', (exclude,))
        elif exclude == 'edited':
            conn.execute("UPDATE short_memories SET market_summary='manual edit'")
            conn.execute("UPDATE summaries SET strategy_logic='manual edit'")
        elif exclude == 'deleted':
            conn.execute('DELETE FROM short_memories')
            conn.execute('DELETE FROM summaries')
        else:
            conn.execute("UPDATE decision_context_projections SET account_scope='other'")
        conn.commit()
        before = [tuple(r) for r in conn.execute('SELECT * FROM decision_context_projections')]
    assert recovery.recover_legacy_failures('cfg', CFG) == {'jobs': 0, 'projections': 0}
    with database.get_db_conn() as conn:
        assert [tuple(r) for r in conn.execute('SELECT * FROM decision_context_projections')] == before


def test_retry_exhaustion_restart_and_lease_never_repeat_recovery(sources, monkeypatch):
    fail_old_sources()
    with memory_review_lease('cfg') as acquired:
        assert acquired and recovery.recover_legacy_failures('cfg', CFG)['projections'] == 0
    assert recovery.recover_legacy_failures('cfg', CFG)['projections'] == 2
    monkeypatch.setattr(memory_service, 'organize_memory', Mock(side_effect=ValueError(legacy_error())))
    for attempt in range(1, 4):
        assert not projections.process_projection_updates('cfg', CFG)
        assert recovery.recover_legacy_failures('cfg', CFG)['projections'] == 0
        with database.get_db_conn() as conn:
            projections.initialize_schema(conn)
            rows = conn.execute('SELECT * FROM decision_context_projections').fetchall()
            assert all(r['attempts'] == attempt for r in rows)
            assert all(r['status'] == ('failed' if attempt == 3 else 'pending') for r in rows)
            conn.execute('UPDATE decision_context_projections SET next_attempt=0')
            conn.commit()
    assert not projections.process_projection_updates('cfg', CFG)
    assert recovery.recover_legacy_failures('cfg', CFG)['projections'] == 0
    assert memory_service.organize_memory.call_count == 3


@pytest.mark.parametrize('fault', ['no_witness', 'foreign_witness', 'edited', 'deleted', 'invalid_metadata', 'partial_result'])
def test_legacy_job_needs_unchanged_trusted_scope(sources, fault):
    fail_old_sources()
    fail_old_job(sources)
    with database.get_db_conn() as conn:
        if fault == 'no_witness':
            conn.execute('DELETE FROM decision_context_projections')
        elif fault == 'foreign_witness':
            conn.execute("UPDATE decision_context_projections SET account_scope='other'")
        elif fault == 'edited':
            conn.execute("UPDATE summaries SET strategy_logic='manual edit'")
        elif fault == 'deleted':
            conn.execute('DELETE FROM summaries')
        elif fault == 'invalid_metadata':
            conn.execute("UPDATE memory_update_jobs SET review_source='bad json'")
        else:
            from backend.agent.memory_workflow import _initialize_results
            _initialize_results(conn)
            conn.execute("INSERT INTO memory_review_results VALUES('run-memory:old-run','cfg','partial','','[]','',0,'{}')")
        conn.commit()
    assert recovery.recover_legacy_failures('cfg', CFG)['jobs'] == 0
    assert job()['status'] == 'failed' and job()['attempts'] == 3 and job()['error'] == legacy_error()


@pytest.mark.parametrize('table', ['memory_update_jobs', 'decision_context_projections'])
def test_additive_migration_rolls_back_all_columns_and_keeps_work(sources, table):
    fail_old_sources()
    fail_old_job(sources)
    columns = ['review_contract_revision', 'contract_recovery'] + (['review_source'] if table == 'memory_update_jobs' else [])
    with database.get_db_conn() as conn:
        for name in columns:
            conn.execute(f'ALTER TABLE {table} DROP COLUMN {name}')
        conn.commit()
        class Interrupted:
            def execute(self, sql, *args):
                if 'ADD COLUMN contract_recovery' in sql:
                    raise RuntimeError('interrupted migration')
                return conn.execute(sql, *args)
        with pytest.raises(RuntimeError, match='interrupted'):
            recovery.initialize_contract_state(Interrupted(), table)
        assert not set(columns) & {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
        assert conn.execute(f'SELECT status FROM {table} LIMIT 1').fetchone()[0] == 'failed'
        recovery.initialize_contract_state(conn, table)
        recovery.initialize_contract_state(conn, table)
        assert set(columns) <= {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}


def test_ready_view_kept_and_late_recovered_old_summary_does_not_replace_new(sources, monkeypatch):
    from test_decision_context_availability import add_summary, publish
    fail_old_sources()
    for day in (11, 12, 13):
        add_summary(f'2026-10-{day} 01:00:00', '较新策略')
    publish()
    before = load_decision_memory('cfg', CFG)
    assert recovery.recover_legacy_failures('cfg', CFG)['projections'] == 2
    context = projections.prepare_review('cfg')
    scripted_model(monkeypatch, response(response_for(context, thesis='较旧策略')))
    assert projections.process_projection_updates('cfg', CFG)
    after = load_decision_memory('cfg', CFG)
    assert after.recent_summaries_text == before.recent_summaries_text
    assert '较旧策略' in after.short_memory_text


@pytest.mark.parametrize('change', ['edit', 'delete', 'scope'])
def test_recovered_job_revalidates_source_before_publication(sources, monkeypatch, change):
    from backend.agent import memory_agent
    fail_old_sources()
    fail_old_job(sources)
    assert recovery.recover_legacy_failures('cfg', CFG)['jobs'] == 1
    before = database.get_short_memories('cfg', 10)
    context = projections.prepare_review('cfg')
    def invoke(messages):
        if change == 'scope':
            monkeypatch.setattr(projections.trade_evidence, 'configured_account_scope', lambda _: 'other')
        else:
            with database.get_db_conn() as conn:
                conn.execute("UPDATE summaries SET strategy_logic='edited'" if change == 'edit' else 'DELETE FROM summaries')
                conn.commit()
        return response(response_for(context))
    monkeypatch.setattr(memory_agent, 'build_chat_model', lambda **kw: Mock(invoke=invoke))
    monkeypatch.setattr(memory_service, 'format_recent_position_history_for_memory', lambda *a: '离线')
    assert not memory_updates.process_memory_update('cfg', 'old-run', CFG)
    assert database.get_short_memories('cfg', 10) == before
    assert job()['status'] == 'pending'


def test_no_source_does_not_fabricate_memory(sources, monkeypatch):
    with database.get_db_conn() as conn:
        conn.execute('DELETE FROM short_memories')
        conn.commit()
    fail_old_sources()
    assert recovery.recover_legacy_failures('cfg', CFG) == {'jobs': 0, 'projections': 1}
    context = projections.prepare_review('cfg')
    scripted_model(monkeypatch, response(response_for(context)))
    assert projections.process_projection_updates('cfg', CFG)
    view = load_decision_memory('cfg', CFG)
    assert '尚无已发布' in view.short_memory_text and THESIS in view.recent_summaries_text
    assert not database.get_short_memories('cfg', 10)


def test_no_sources_or_jobs_keeps_both_missing_without_model_calls(sources, monkeypatch):
    with database.get_db_conn() as conn:
        conn.execute('DELETE FROM short_memories')
        conn.execute('DELETE FROM summaries')
        conn.commit()
    calls, _, _ = scripted_model(monkeypatch)
    background(monkeypatch)
    memory_updates.tick_memory_updates()
    view = load_decision_memory('cfg', CFG)
    assert '尚无已发布' in view.short_memory_text and '尚无已发布' in view.recent_summaries_text
    assert not calls and not database.get_short_memories('cfg', 10)


def test_source_deleted_after_requeue_cannot_block_queue_forever(sources, monkeypatch):
    fail_old_sources()
    fail_old_job(sources)
    assert recovery.recover_legacy_failures('cfg', CFG)['jobs'] == 1
    with database.get_db_conn() as conn:
        conn.execute('DELETE FROM summaries')
        conn.commit()
    calls, _, _ = scripted_model(monkeypatch)
    for attempt in range(1, 4):
        assert not memory_updates.process_memory_update('cfg', 'old-run', CFG)
        assert job()['attempts'] == attempt
        assert job()['status'] == ('failed' if attempt == 3 else 'pending')
        with database.get_db_conn() as conn:
            conn.execute('UPDATE memory_update_jobs SET next_attempt=0')
            conn.commit()
    assert recovery.recover_legacy_failures('cfg', CFG)['jobs'] == 0
    assert not memory_updates.process_memory_update('cfg', 'old-run', CFG)
    assert not calls and job()['status'] == 'failed'


@pytest.mark.parametrize('change', ['short_memory', 'strategy_summary', 'account', 'symbol'])
def test_atomic_save_rechecks_sources_after_successful_model(sources, monkeypatch, change):
    from backend.config import config
    context = projections.prepare_review('cfg')
    calls, _, _ = scripted_model(monkeypatch, response(response_for(context)))
    with memory_review_lease('cfg'):
        result = memory_service.organize_memory('完整来源', CFG, operation_id='atomic-source-check')
        if change == 'account':
            monkeypatch.setattr(projections.trade_evidence, 'configured_account_scope', lambda _: 'other')
        elif change == 'symbol':
            monkeypatch.setattr(config, 'get_config_by_id', lambda _: {**CFG, 'symbol': 'BTC/USDT'})
        else:
            with database.get_db_conn() as conn:
                conn.execute("UPDATE short_memories SET market_summary='edited'" if change == 'short_memory'
                             else 'DELETE FROM summaries')
                conn.commit()
        before = database.get_short_memories('cfg', 10)
        with pytest.raises(ValueError, match='changed'):
            memory_service.save_short_memory('2026-10-14', '2026-10-14', 'ETH/USDT', 'cfg', result, '', 1)
    assert len(calls) == 1 and database.get_short_memories('cfg', 10) == before
