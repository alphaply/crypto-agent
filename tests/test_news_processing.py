import json
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import config_store, database, database_news
from backend.app.api.news import router
from backend.app.core.deps import get_current_user
from backend.app.services import news_service, pricing_service
from backend.database_agent_runs import get_agent_run
from backend.database_schema import initialize_schema
from backend.utils import jev, llm_utils


@pytest.fixture
def processing(tmp_path, monkeypatch):
    path = tmp_path / 'processing.db'
    monkeypatch.setattr(database, 'DB_NAME', str(path))
    monkeypatch.setattr(config_store, 'DB_NAME', path)
    monkeypatch.setattr(llm_utils, 'sync_langsmith_environment', lambda: {})
    monkeypatch.setenv('LANGSMITH_TRACING', 'false')
    monkeypatch.setenv('LANGCHAIN_TRACING_V2', 'false')
    monkeypatch.setenv('CONFIG_MASTER_KEY', 'offline-processing-test')
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    snapshot = {'news': {'scorer_provider_id': 'jev', 'summarizer_provider_id': 'summary'}, 'llm_providers': [
        {'provider_id': 'jev', 'name': 'Official Jev', 'model': 'jev-latest', 'api_protocol': 'decisions',
         'decisions_api': 'typesafe', 'api_key': 'test-secret', 'input_price_per_m': 0.042, 'output_price_per_m': 0},
        {'provider_id': 'summary', 'name': 'Summary', 'model': 'summary', 'api_key': 'test-secret'},
    ]}
    monkeypatch.setattr(news_service, '_runtime', lambda: snapshot)
    monkeypatch.setattr(pricing_service, '_runtime', lambda: snapshot)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: {'sub': 'admin'}
    return snapshot, client


def answer(score=2.12):
    return {'model': 'jev-1.13.0', 'answers': {'market_relevance': {
        'type': 'score', 'score': score, 'confidence': .82,
        'probabilities': {'0': .011, '1': .117, '2': .631, '3': .218, '4': .023},
        'legend': {str(i): f'Level {i}' for i in range(5)},
    }}, 'usage': {'input_tokens': 200, 'output_tokens': 20}}


def article(index=0):
    return {'id': str(index), 'title': f'Public market fixture {index}', 'content': 'Synthetic market news.', 'source': 'Fixture'}


def test_score_accepts_only_precision_consistent_rounding():
    # Weighted mean is 2.125. Rounded two-decimal API score is valid; the old
    # fixed 0.001 epsilon incorrectly rejected it.
    payload = answer()
    result = jev.parse_response(payload)
    assert result['raw_score'] == 2.12
    assert result['weighted_score'] == pytest.approx(2.125)
    assert result['score'] == 53
    with pytest.raises(jev.JevError, match='returned precision') as error:
        jev.parse_response(answer(2.2))
    assert error.value.code == 'score_distribution_mismatch'
    assert error.value.details['weighted_score'] == pytest.approx(2.125)
    payload['answers']['market_relevance']['probabilities']['4'] = .5
    with pytest.raises(jev.JevError, match='invalid probability'):
        jev.parse_response(payload)


def test_order_of_probability_keys_does_not_change_precision_validation():
    payload = answer()
    payload['answers']['market_relevance']['probabilities'] = dict(reversed(list(payload['answers']['market_relevance']['probabilities'].items())))
    assert jev.parse_response(payload)['score'] == 53


def test_trailing_serialized_precision_is_preserved():
    payload = answer()
    payload['answers']['market_relevance']['score'] = Decimal('2.10')
    with pytest.raises(jev.JevError, match='returned precision'):
        jev.parse_response(payload)
    payload['answers']['market_relevance']['score'] = Decimal('2.1')
    assert jev.parse_response(payload)['score'] == 52.5


def test_official_endpoint_cache_and_failed_response_audit(processing, monkeypatch):
    snapshot, _ = processing
    requests = []
    def post(url, **kwargs):
        requests.append((url, kwargs))
        return httpx.Response(200, json=answer(4), request=httpx.Request('POST', url))
    monkeypatch.setattr(jev.httpx, 'post', post)
    with pytest.raises(jev.JevError) as failure:
        jev.score_news(article(), snapshot['llm_providers'][0])
    assert len(requests) == 1 and requests[0][0] == 'https://api.typesafe.ai/v1/systemone'
    row = get_agent_run(failure.value.details['run_id'])
    assert row['status'] == 'error' and row['prompt_tokens'] == 200
    assert json.loads(row['output'])['market_relevance']['score'] == 4
    assert row['details']['error_code'] == 'score_distribution_mismatch'
    assert 'test-secret' not in json.dumps(row)
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM news_score_cache').fetchone()[0] == 0


def test_adapter_roundtrip_keeps_encrypted_credentials(processing):
    snapshot, _ = processing
    provider = snapshot['llm_providers'][0]
    config_store.save_runtime_snapshot({}, [], [provider], [])
    loaded = config_store.load_runtime_snapshot()['llm_providers'][0]
    assert loaded['decisions_api'] == 'typesafe' and loaded['api_key'] == 'test-secret'
    exported = config_store.export_full_snapshot(include_secrets=False)
    assert exported['llm_providers'][0]['decisions_api'] == 'typesafe'
    assert 'test-secret' not in json.dumps(exported)
    config_store.import_full_snapshot(exported)
    assert config_store.load_runtime_snapshot()['llm_providers'][0]['decisions_api'] == 'typesafe'
    with pytest.raises(ValueError, match='adapter'):
        config_store.save_runtime_snapshot({}, [], [{**provider, 'decisions_api': 'unknown'}], [])


def test_model_discovery_uses_saved_adapter_and_safe_errors(processing, monkeypatch):
    _, client = processing
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json={'models': [{'name': 'jev-latest', 'description': 'Official alias'}, {'name': 'chat-model'}]})
    monkeypatch.setattr(jev.httpx, 'get', get)
    response = client.get('/api/news/scorer-models', params={'provider_id': 'jev'})
    assert response.status_code == 200 and response.json()['models'] == [{'id': 'jev-latest', 'name': 'jev-latest', 'description': 'Official alias'}]
    assert calls[0][0] == 'https://api.typesafe.ai/v1/models'
    monkeypatch.setattr(jev.httpx, 'get', lambda *a, **k: (_ for _ in ()).throw(ValueError('test-secret')))
    response = client.get('/api/news/scorer-models', params={'provider_id': 'jev'})
    assert response.status_code == 502 and 'test-secret' not in response.text


def test_progress_items_partial_failure_and_history_are_durable(processing, monkeypatch):
    _, client = processing
    monkeypatch.setattr(news_service, 'collect_candidates', lambda *a: ([article(i) for i in range(3)], {'fixture': {'status': 'ok', 'item_count': 3}}, {}))
    def score(item, *args, **kwargs):
        if item['id'] == '2':
            raise jev.JevError('Invalid score for this rubric', code='score_distribution_mismatch', details={'run_id': 'local-failed-run'})
        return {'score': 90 if item['id'] == '0' else 10, 'cached': item['id'] == '0'}
    monkeypatch.setattr(news_service, 'score_news', score)
    monkeypatch.setattr(news_service, '_summarize', lambda *a: 'Test digest')
    result = news_service.refresh_news(background=False)
    assert result['status'] == 'degraded' and result['stage'] == 'complete'
    assert result['counts'] == {'sources_total': 0, 'sources_completed': 0, 'fetched': 0, 'candidates': 3,
                                'scored': 2, 'cached': 1, 'failed': 1, 'selected': 1, 'filtered': 1}
    assert [item['status'] for item in result['items']] == ['selected', 'filtered', 'failed']
    assert result['items'][2]['error_code'] == 'score_distribution_mismatch'
    assert result['items'][2]['run_id'] == 'local-failed-run'
    assert result['progress']['percent'] == 100
    assert result['finished_at'] and result['trace_status'] == 'disabled'
    run_id = result['run_id']
    history = client.get('/api/news/runs').json()
    assert history['total'] == 1 and 'items' not in history['runs'][0]
    assert client.get('/api/news/runs/' + run_id).json()['run']['counts'] == result['counts']
    client.app.dependency_overrides.clear()
    assert client.get('/api/news/runs').status_code == 401
    assert client.get('/api/news/runs/' + run_id).status_code == 401


def test_all_scores_failed_keep_previous_digest_and_per_item_diagnostics(processing, monkeypatch):
    _, _ = processing
    owner = database_news.acquire_lease('news')
    database_news.publish_snapshot({'available': True, 'digest': 'Previous good digest', 'as_of': database_news.now_iso()}, owner)
    database_news.update_state('news', {}, owner=owner, release=True)
    monkeypatch.setattr(news_service, 'collect_candidates', lambda *a: ([article()], {'fixture': {'status': 'ok'}}, {}))
    monkeypatch.setattr(news_service, 'score_news', lambda *a, **k: (_ for _ in ()).throw(jev.JevError('Score unavailable', code='http_error')))
    result = news_service.refresh_news(background=False)
    assert result['status'] == 'error' and result['stage'] == 'score'
    assert result['counts']['failed'] == 1 and result['items'][0]['error_code'] == 'http_error'
    assert news_service.get_latest_news()['digest'] == 'Previous good digest'


def test_langsmith_parent_child_context_crosses_scoring_worker(processing, monkeypatch):
    from langsmith import tracing_context, get_current_run_tree
    from langsmith.run_trees import RunTree
    snapshot, _ = processing
    snapshot['news']['trace_mode'] = 'full'
    monkeypatch.setenv('LANGSMITH_API_KEY', 'fake-offline-tracing-key')
    monkeypatch.setattr(RunTree, 'get_url', lambda self: f'https://smith.example.test/runs/{self.id}')
    monkeypatch.setattr(news_service, 'collect_candidates', lambda *a: ([article()], {'fixture': {'status': 'ok'}}, {}))
    monkeypatch.setattr(news_service, '_summarize', lambda *a: 'Digest')
    spans = []
    def post(url, **kwargs):
        span = get_current_run_tree()
        spans.append(span)
        return httpx.Response(200, json=answer(), request=httpx.Request('POST', url))
    monkeypatch.setattr(jev.httpx, 'post', post)
    # 'local' creates genuine SDK RunTrees without submitting to LangSmith.
    with tracing_context(enabled='local'):
        result = news_service.refresh_news(background=False)
    assert spans[0].name == 'jev.score' and str(spans[0].parent_run_id) == result['trace_id']
    assert result['trace_url'].startswith('https://smith.example.test/runs/')
    row = get_agent_run(result['items'][0]['run_id'])
    assert row['parent_run_id'] == result['run_id']
    assert row['details']['trace_id'] == str(spans[0].id)
    assert 'test-secret' not in json.dumps(result)


@pytest.mark.parametrize('failure', ['construct', 'end', 'identity', 'sync'])
def test_tracing_failures_do_not_fail_news_or_leave_lease(processing, monkeypatch, failure):
    import langsmith
    from langsmith import tracing_context
    monkeypatch.setattr(news_service, 'collect_candidates', lambda *a: ([article()], {'fixture': {'status': 'ok'}}, {}))
    monkeypatch.setattr(news_service, '_summarize', lambda *a: 'Digest')
    monkeypatch.setattr(jev.httpx, 'post', lambda url, **kw: httpx.Response(200, json=answer(), request=httpx.Request('POST', url)))
    class BrokenTrace:
        @property
        def id(self):
            raise RuntimeError('trace identity unavailable')
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def end(self, **kwargs):
            raise RuntimeError('trace upload failed')
        def get_url(self):
            raise RuntimeError('trace URL failed')
    if failure == 'construct':
        monkeypatch.setattr(langsmith, 'trace', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('tracing constructor failed')))
    elif failure == 'sync':
        monkeypatch.setattr(llm_utils, 'sync_langsmith_environment', lambda: (_ for _ in ()).throw(RuntimeError('tracing configuration failed')))
    else:
        monkeypatch.setattr(langsmith, 'trace', lambda *a, **k: BrokenTrace())
    with tracing_context(enabled='local'):
        result = news_service.refresh_news(background=False)
    assert result['status'] == 'success' and result['running'] is False
    assert result['counts']['failed'] == 0


def test_progress_initialization_failure_releases_lease(processing, monkeypatch):
    original = database_news.save_processing_run
    calls = []
    def fail_once(*args):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError('temporary persistence failure')
        return original(*args)
    monkeypatch.setattr(database_news, 'save_processing_run', fail_once)
    result = news_service.refresh_news(background=False)
    assert result['status'] == 'error' and result['running'] is False
    assert result['finished_at']


def test_lost_lease_marks_historical_run_interrupted(processing):
    from backend.app.services.news_progress import NewsProgress
    owner = database_news.acquire_lease('news')
    progress = NewsProgress(owner)
    progress.update(stage='score', counts={'candidates': 3, 'scored': 1})
    database_news.update_state('news', {}, owner=owner, release=True)
    row = database_news.processing_run(owner)
    assert row['status'] == 'error' and row['interrupted'] is True
    assert row['stage'] == 'score' and row['counts']['scored'] == 1
    assert database_news.processing_runs()['runs'][0]['status'] == 'error'


def test_public_snapshots_hide_private_audit_and_trace_metadata(processing, monkeypatch):
    _, client = processing
    monkeypatch.setattr(news_service, 'collect_candidates', lambda *a: ([article()], {'fixture': {'status': 'ok'}}, {}))
    monkeypatch.setattr(news_service, '_summarize', lambda *a: 'Digest')
    monkeypatch.setattr(news_service, 'score_news', lambda *a, **k: {'score': 90, 'run_id': 'private-audit-id',
                                                                   'trace_id': 'private-trace-id', 'trace_url': 'https://smith.example.test/private'})
    result = news_service.refresh_news(background=False)
    assert result['items'][0]['run_id'] == 'private-audit-id'
    public = client.get('/api/news').text
    assert 'private-audit' not in public and 'private-trace' not in public and 'smith.example' not in public


def test_news_audit_retention_is_independent_of_ordinary_agents(processing, monkeypatch):
    from backend import database_agent_runs as runs
    monkeypatch.setattr(runs, 'MAX_RUNS_PER_CONFIG', 1)
    monkeypatch.setattr(runs, 'MAX_NEWS_RUNS', 3)
    news_ids = [runs.start_agent_run('news-intelligence', 'news_score', 'jev-latest', [], provider_id='jev') for _ in range(3)]
    agent_ids = [runs.start_agent_run('ordinary-agent', 'decision', 'summary', [], provider_id='summary') for _ in range(2)]
    assert all(runs.get_agent_run(run_id) for run_id in news_ids)
    assert runs.get_agent_run(agent_ids[0]) is None and runs.get_agent_run(agent_ids[1])
    assert runs.list_agent_runs(config_id='news-intelligence')['retention']['per_config'] == 3


def test_recovery_serializes_with_new_worker_start(processing):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from backend.app.services.news_progress import NewsProgress
    reading_runs = threading.Event()
    writer_finished = threading.Event()
    def writer():
        assert reading_runs.wait(2)
        owner = database_news.acquire_lease('news')
        NewsProgress(owner).update(stage='score')
        writer_finished.set()
        return owner
    class InterleavedConnection:
        def __init__(self, connection):
            self.connection = connection
        def execute(self, statement, *args):
            if statement.startswith('SELECT run_id,payload_json FROM news_processing_runs'):
                reading_runs.set()
                # Without the recovery transaction the new worker can publish
                # here, after the stale lease read, and be incorrectly failed.
                writer_finished.wait(.1)
            return self.connection.execute(statement, *args)
        def commit(self):
            self.connection.commit()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(writer)
        with database.get_db_conn() as conn:
            database_news.initialize(conn)
            database_news._recover_interrupted_runs(InterleavedConnection(conn))
        owner = future.result(timeout=3)
    recovered = database_news.processing_run(owner)
    assert recovered['status'] == 'running' and not recovered.get('interrupted')
