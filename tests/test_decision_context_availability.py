"""Published context stays useful through background and evidence failures."""
import json
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import database
from backend.agent import context_projections as projections
from backend.agent.decision_context import load_decision_memory
from backend.agent import memory_service
from backend.agent.memory_workflow import memory_review_lease
from backend.app.api.history import router
from backend.app.core.deps import get_current_user
from backend.app.services.decision_memory_service import projection_status
from test_context_projections import CFG, LEGACY, THESIS, model, response_for, sources, simulation


def add_summary(timestamp, text=LEGACY):
    with database.get_db_conn() as conn:
        identity = conn.execute('INSERT INTO summaries(config_id,symbol,timestamp,strategy_logic) VALUES(?,?,?,?)',
                                ('cfg', 'ETH/USDT', timestamp, text)).lastrowid
        conn.commit()
        return identity


def publish():
    context = projections.prepare_review('cfg')
    metadata = projections.validate_review(response_for(context), context)
    projections.persist_projections('cfg', metadata)
    return metadata


@pytest.mark.parametrize('status', ['pending', 'failed'])
def test_new_unpublished_window_preserves_memory_and_three_summaries(sources, monkeypatch, status):
    add_summary('2026-10-02 01:00:00')
    add_summary('2026-10-03 01:00:00')
    publish()
    before = load_decision_memory('cfg', CFG)
    database.save_short_memory('2026-10-13', '2026-10-13 04:00:00', 'ETH/USDT', 'cfg', '新原文待整理', '', 4)
    for day in (11, 12, 13):
        add_summary(f'2026-10-{day} 04:00:00', '新摘要待整理')
    projections.ensure_sources('cfg')
    with database.get_db_conn() as conn:
        conn.execute('UPDATE decision_context_projections SET status=? WHERE status=\'pending\'', (status,))
        conn.commit()
    monkeypatch.setattr(projections, 'ensure_sources', Mock(side_effect=AssertionError('Read must not write/queue')))
    after = load_decision_memory('cfg', CFG)
    assert after.short_memory_text == before.short_memory_text
    assert after.recent_summaries_text == before.recent_summaries_text
    assert after.recent_summaries_text.count('[截至') == 3
    assert '2026-10-03' in after.recent_summaries_text and '2026-10-13' not in after.recent_summaries_text
    assert '整理中' not in after.short_memory_text + after.recent_summaries_text


def test_late_publication_outside_current_window_and_source_order(sources):
    old = projections.prepare_review('cfg')
    metadata = projections.validate_review(response_for(old, thesis='旧有效策略'), old)
    for day in (11, 12, 13, 14):
        add_summary(f'2026-10-{day} 00:00:00')
    newer = publish()
    projections.persist_projections('cfg', metadata)
    view = load_decision_memory('cfg', CFG)
    assert '旧有效策略' not in view.recent_summaries_text
    assert THESIS in view.recent_summaries_text
    with database.get_db_conn() as conn:
        assert conn.execute("SELECT status FROM decision_context_projections WHERE source_kind='strategy_summary' AND source_id=?",
                            (str(sources),)).fetchone()['status'] == 'ready'
    assert newer['projections']


def test_edited_or_deleted_source_cannot_publish_stale_result(sources):
    ctx = projections.prepare_review('cfg')
    metadata = projections.validate_review(response_for(ctx), ctx)
    original = database.get_short_memories('cfg', 1)[0]
    database.update_short_memory('cfg', original['bucket_start'], '人工修订', '')
    with database.get_db_conn() as conn:
        conn.execute('DELETE FROM summaries WHERE id=?', (sources,))
        conn.commit()
    projections.persist_projections('cfg', metadata)
    view = load_decision_memory('cfg', CFG)
    assert THESIS not in view.short_memory_text + view.recent_summaries_text


@pytest.mark.parametrize('same_bucket', [False, True])
def test_memory_and_projection_commit_or_rollback_together(sources, monkeypatch, same_bucket):
    metadata = publish()
    before = database.get_short_memories('cfg', 10)
    view = load_decision_memory('cfg', CFG)
    metadata = {**metadata, 'summary': '新完整原文', 'memory': {**metadata['memory'], 'strategy_text': '新发布策略'}}
    real_publish = projections.publish_new_memory
    def interrupt(*args):
        real_publish(*args)
        # Another connection can read the intact published version before commit.
        assert load_decision_memory('cfg', CFG).short_memory_text == view.short_memory_text
        raise RuntimeError('interrupted before commit')
    monkeypatch.setattr(projections, 'publish_new_memory', interrupt)
    start = before[0]['bucket_start'] if same_bucket else '2026-10-13 00:00:00'
    def save():
        database.save_short_memory(start, '2026-10-13 04:00:00', 'ETH/USDT', 'cfg', metadata['summary'], '', 1,
                                   decision_projection=metadata)
    with pytest.raises(RuntimeError):
        save()
    assert database.get_short_memories('cfg', 10) == before
    assert load_decision_memory('cfg', CFG).short_memory_text == view.short_memory_text
    monkeypatch.setattr(projections, 'publish_new_memory', real_publish)
    save()
    with database.get_db_conn() as conn:
        projections.initialize_schema(conn)
        conn.commit()
    assert '新发布策略' in load_decision_memory('cfg', CFG).short_memory_text


def test_evidence_failure_preserves_text_and_original_identity(sources, monkeypatch):
    publish()
    before = load_decision_memory('cfg', CFG)
    monkeypatch.setattr(projections, 'operation_evidence', Mock(side_effect=RuntimeError('offline')))
    after = load_decision_memory('cfg', CFG)
    assert (after.short_memory_text, after.recent_summaries_text) == (before.short_memory_text, before.recent_summaries_text)
    assert after.execution_evidence['old-close']['resolution'] == 'unavailable'


def test_source_failure_isolated_and_corrupt_reference_does_not_hide_text(sources, monkeypatch):
    publish()
    read = projections.stored_sources
    def broken_memory(cid, scope, kind, **kwargs):
        if kind == 'short_memory':
            raise RuntimeError('memory table unavailable')
        return read(cid, scope, kind, **kwargs)
    monkeypatch.setattr(projections, 'stored_sources', broken_memory)
    assert THESIS in load_decision_memory('cfg', CFG).recent_summaries_text
    monkeypatch.setattr(projections, 'stored_sources', read)
    with database.get_db_conn() as conn:
        conn.execute("UPDATE decision_context_projections SET execution_refs='invalid-json' WHERE source_kind='short_memory'")
        conn.commit()
    view = load_decision_memory('cfg', CFG)
    assert THESIS in view.short_memory_text and THESIS in view.recent_summaries_text
    assert 'context' not in view.execution_evidence
    assert 'old-close' in view.execution_evidence


def test_corrupt_row_keeps_other_published_sources_and_management_diagnostic(sources):
    add_summary('2026-10-02')
    publish()
    with database.get_db_conn() as conn:
        conn.execute("UPDATE decision_context_projections SET strategy_text=? WHERE source_kind='strategy_summary' AND source_id=?",
                     (b'corrupt text blob', str(sources)))
        conn.commit()
    view = load_decision_memory('cfg', CFG)
    assert THESIS in view.short_memory_text and THESIS in view.recent_summaries_text
    assert projection_status('cfg')['read_errors'] == [f'strategy_summary:{sources}']


def test_different_account_never_reuses_published_content(sources, monkeypatch):
    publish()
    monkeypatch.setattr(projections.trade_evidence, 'configured_account_scope', lambda _: 'another-account')
    view = load_decision_memory('cfg', CFG)
    assert THESIS not in view.short_memory_text + view.recent_summaries_text


def test_projection_metadata_is_scoped_to_one_review_lease(sources):
    with memory_review_lease('cfg') as acquired:
        assert acquired
        memory_service._projection_metadata.set({'config_id': 'cfg', 'summary': 'old result'})
    with memory_review_lease('cfg') as acquired:
        assert acquired
        assert memory_service._projection_metadata.get() is None
        memory_service.save_short_memory('2026-10-14', '2026-10-14', 'ETH/USDT', 'cfg', 'unprojected import', '', 1)
    assert database.get_short_memories('cfg', 1)[0]['market_summary'] == 'unprojected import'


def test_symbol_change_rejects_inflight_memory_publication(sources, monkeypatch):
    from backend.config import config
    metadata = publish()
    before = database.get_short_memories('cfg', 10)
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: {**CFG, 'symbol': 'BTC/USDT'})
    with pytest.raises(ValueError, match='Symbol changed'):
        database.save_short_memory('2026-10-14', '2026-10-14', 'BTC/USDT', 'cfg', metadata['summary'], '', 1,
                                   decision_projection=metadata)
    assert database.get_short_memories('cfg', 10) == before


def test_upgrade_retries_failed_once_and_preserves_ready(sources):
    publish()
    with database.get_db_conn() as conn:
        conn.execute('ALTER TABLE decision_context_projections DROP COLUMN publication_revision')
        conn.execute("UPDATE decision_context_projections SET status='failed',attempts=3 WHERE source_kind='strategy_summary'")
        projections.initialize_schema(conn)
        rows = conn.execute('SELECT source_kind,status,attempts FROM decision_context_projections').fetchall()
        assert {(r['source_kind'], r['status'], r['attempts']) for r in rows} == {
            ('short_memory', 'ready', 0), ('strategy_summary', 'pending', 0)}
        conn.execute("UPDATE decision_context_projections SET status='failed',attempts=3 WHERE source_kind='strategy_summary'")
        projections.initialize_schema(conn)
        conn.commit()
    assert projection_status('cfg')['failed'] == 1


def test_upgrade_marker_rolls_back_if_requeue_fails(sources):
    publish()
    with database.get_db_conn() as conn:
        conn.execute('ALTER TABLE decision_context_projections DROP COLUMN publication_revision')
        conn.execute("UPDATE decision_context_projections SET status='failed',attempts=3")
        conn.commit()
        class InterruptedConnection:
            def execute(self, sql, *args):
                if sql.startswith('UPDATE decision_context_projections'):
                    raise RuntimeError('interrupted migration')
                return conn.execute(sql, *args)
        with pytest.raises(RuntimeError, match='interrupted migration'):
            projections.initialize_schema(InterruptedConnection())
        assert 'publication_revision' not in {row[1] for row in conn.execute('PRAGMA table_info(decision_context_projections)')}
        assert conn.execute('SELECT status FROM decision_context_projections LIMIT 1').fetchone()[0] == 'failed'
        projections.initialize_schema(conn)
        conn.commit()
    assert projection_status('cfg')['pending'] == 2


def test_failed_atomic_save_reuses_durable_model_result(sources, monkeypatch):
    calls = model(monkeypatch)
    publisher = projections.publish_new_memory
    with memory_review_lease('cfg'):
        summary = memory_service.organize_memory('策略来源', CFG, operation_id='atomic-retry')
        monkeypatch.setattr(projections, 'publish_new_memory', Mock(side_effect=RuntimeError('write interrupted')))
        with pytest.raises(RuntimeError):
            memory_service.save_short_memory('2026-10-14', '2026-10-14', 'ETH/USDT', 'cfg', summary, '', 1)
    monkeypatch.setattr(projections, 'publish_new_memory', publisher)
    with memory_review_lease('cfg'):
        summary = memory_service.organize_memory('策略来源', CFG, operation_id='atomic-retry')
        memory_service.save_short_memory('2026-10-14', '2026-10-14', 'ETH/USDT', 'cfg', summary, '', 1)
    assert len(calls) == 1
    assert '2026-10-14' in load_decision_memory('cfg', CFG).short_memory_text


def test_status_and_retry_are_authenticated_and_do_not_call_model(sources, monkeypatch):
    app = FastAPI()
    app.include_router(router)
    calls = model(monkeypatch, failure=True)
    client = TestClient(app)
    url = '/api/history/short-memories/projections/'
    assert client.get(url + 'status?config_id=cfg').status_code == 401
    assert client.post(url + 'retry', json={'config_id': 'cfg'}).status_code == 401
    app.dependency_overrides[get_current_user] = lambda: {'sub': 'admin'}
    assert client.get(url + 'status?config_id=cfg').json()['not_generated'] == 2
    publish()
    with database.get_db_conn() as conn:
        conn.execute("UPDATE decision_context_projections SET status='failed',attempts=3,error='model offline' WHERE source_kind='strategy_summary'")
        conn.commit()
    status = client.get(url + 'status?config_id=cfg').json()
    assert status['failed'] == 1 and status['published']['short_memory'][0]['as_of'] == '2026-10-10 04:00:00'
    assert status['errors'][0]['error'] == 'model offline'
    retried = client.post(url + 'retry', json={'config_id': 'cfg'}).json()
    assert retried['requeued'] == 1 and retried['pending'] == 1 and retried['failed'] == 0
    assert client.post(url + 'retry', json={'config_id': 'cfg'}).json()['requeued'] == 0
    assert client.post(url + 'retry', json={'config_id': 'cfg', 'anything': True}).status_code == 422
    assert client.get(url + 'status?config_id=missing').status_code == 404
    assert calls == []
