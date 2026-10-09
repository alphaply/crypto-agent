from datetime import datetime, timedelta, timezone
import json

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from backend import database, database_news
from backend.database_schema import initialize_schema
from backend.app.schemas.news import NewsSettings, NewsTriggerRule
from backend.app.services import news_service, news_models, news_triggers
from backend.utils import llm_utils


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'news.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    monkeypatch.setattr(llm_utils, 'sync_langsmith_environment', lambda: {})
    monkeypatch.setenv('LANGSMITH_TRACING', 'false')
    monkeypatch.setenv('LANGCHAIN_TRACING_V2', 'false')
    runtime = {'news': {'scoring_mode': 'off', 'summarizer_provider_id': 'cheap'},
               'llm_providers': [{'provider_id': 'cheap', 'model': 'custom-flash', 'api_key': 'offline-key', 'api_base': 'https://example.test/v1'}]}
    monkeypatch.setattr(news_service, '_runtime', lambda: runtime)
    return runtime


def article(title='CPI actual 2.4%, forecast 2.5%', **kw):
    return {'id': title, 'title': title, 'content': title, 'published_at': (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),
            'source': 'Test', 'source_id': 'test', 'category': 'macro_market', 'url': 'https://example.test/' + title, **kw}


def collect(monkeypatch, articles):
    monkeypatch.setattr(news_service, 'collect_candidates', lambda *a: (articles, {'test': {'status': 'ok'}}, {}))


def test_off_scoring_no_provider_no_fake_score_and_no_duplicate_summary(env, monkeypatch):
    collect(monkeypatch, [article()])
    monkeypatch.setattr(news_service, 'score_news', lambda *a, **k: pytest.fail('Scoring is off'))
    calls = []
    monkeypatch.setattr(news_service, '_summarize', lambda *a: calls.append(a) or 'Digest [1]')
    first = news_service.refresh_news(background=False)
    assert first['status'] == 'success' and first['counts']['skipped'] == 1 and first['counts']['scored'] == 0
    assert news_service.get_latest_news()['items'][0]['scoring'] == {'score': None, 'method': 'off'}
    second = news_service.refresh_news(background=False)
    assert second['summary_reused'] and len(calls) == 1
    assert 'trigger_items' not in news_service.get_latest_news()


def test_deferred_summary_keeps_citations_but_publishes_new_events(env, monkeypatch):
    articles = [article('old CPI')]
    collect(monkeypatch, articles)
    calls = []
    monkeypatch.setattr(news_service, '_summarize', lambda *a: calls.append(a) or 'Digest [1]')
    news_service.refresh_news(background=False)
    articles[:] = [article('new CPI')]
    result = news_service.refresh_news(background=False)
    assert result['summary_deferred'] and len(calls) == 1
    assert news_service.get_latest_news()['items'][0]['title'] == 'old CPI'
    assert database_news.read_state('news-events')['trigger_items'][0]['title'] == 'new CPI'
    env['news']['summary_min_seconds'] = 0
    news_service.refresh_news(background=False)
    assert len(calls) == 2 and calls[-1][1]['_summary_context']['digest'] == 'Digest [1]'
    assert news_service.get_latest_news()['items'][0]['title'] == 'new CPI'


def test_changed_summary_settings_bypass_cooldown_and_empty_window_clears(env, monkeypatch):
    articles = [article()]
    collect(monkeypatch, articles)
    calls = []
    monkeypatch.setattr(news_service, '_summarize', lambda *a: calls.append(a) or 'Digest')
    news_service.refresh_news(background=False)
    env['news']['summary_instructions'] = 'Include prior values'
    news_service.refresh_news(background=False)
    assert len(calls) == 2
    articles.clear()
    result = news_service.refresh_news(background=False)
    assert not result['summary_reused'] and not news_service.get_latest_news()['items']


def test_summary_failure_does_not_lose_trigger_events(env, monkeypatch):
    collect(monkeypatch, [article()])
    monkeypatch.setattr(news_service, '_summarize', lambda *a: (_ for _ in ()).throw(ValueError('private-error')))
    assert news_service.refresh_news(background=False)['status'] == 'error'
    assert database_news.read_state('news-events')['trigger_items']
    assert not news_service.get_latest_news()['available']


def test_llm_batch_cache_rubric_change_and_validation(env, monkeypatch):
    provider = env['llm_providers'][0]
    settings = NewsSettings(scoring_mode='llm')
    articles = [article(str(i)) for i in range(25)]
    calls = []
    def generate(_p, _i, data, _purpose, validator):
        calls.append(data)
        text = json.dumps({'scores': [{'id': i['id'], 'score': 83} for i in data['articles']]})
        validator(text)
        return text
    monkeypatch.setattr(news_models, 'generate', generate)
    result = news_models.score_batch(articles, provider, settings)
    assert len(calls) == 2 and all(r['score'] == 83 for r in result)
    assert all(r['cached'] for r in news_models.score_batch(articles, provider, settings))
    assert len(calls) == 2
    settings.scorer_instructions += ' Updated rubric'
    news_models.score_batch(articles, provider, settings)
    assert len(calls) == 4
    def invalid(_p, _i, _data, _purpose, validator):
        validator('{"scores":[{"id":0,"score":NaN}]}')
    monkeypatch.setattr(news_models, 'generate', invalid)
    bad = news_models.score_batch([article('new')], provider, settings)
    assert isinstance(bad[0], ValueError)


def test_generative_scoring_integration_and_reasoning_model_config(env, monkeypatch):
    env['news'].update(scoring_mode='llm', scorer_provider_id='cheap')
    env['llm_providers'][0].update(thinking_enabled=True, reasoning_effort='low', system_prompt_role='user')
    collect(monkeypatch, [article()])
    builds, requests = [], []
    class Model:
        def invoke(self, messages):
            requests.append(messages)
            if 'Rate market relevance' in messages[0].content:
                return AIMessage(content='{"scores":[{"id":0,"score":85}]}')
            return AIMessage(content='Macro digest [1]')
    monkeypatch.setattr(llm_utils, 'build_chat_model', lambda **kw: builds.append(kw) or Model())
    result = news_service.refresh_news(background=False)
    assert result['status'] == 'success'
    assert len(builds) == 2 and all(b['thinking_enabled'] and b['reasoning_effort'] == 'low' for b in builds)
    assert all(r[0].type == 'human' for r in requests)
    assert news_service.get_latest_news()['items'][0]['scoring']['method'] == 'llm'


def rule(**kw):
    return NewsTriggerRule(config_id='task', enabled=True, provider_id='cheap', keywords=['CPI'], **kw)


def publish(items):
    owner = database_news.acquire_lease('news')
    assert database_news.publish_events(items, owner)
    database_news.update_state('news', {}, owner=owner, release=True)


def test_trigger_baseline_dedupe_cooldown_caps_restart_and_model_isolation(env):
    env['news']['trigger_rules'] = [rule(cooldown_seconds=60, max_runs_per_day=1).model_dump()]
    config = {'config_id': 'task', 'enabled': True, 'mode': 'REAL', 'symbol': 'ETH/USDT:USDT', 'model': 'expensive', 'api_key': 'old-secret',
              'fallback_models': [{'model': 'expensive-fallback'}], 'fallback_llm_provider_ids': ['expensive'], 'extra_body': {'old': True}}
    calls = []
    submit = lambda c: calls.append(c) or True
    publish([article('CPI baseline')])
    assert news_triggers.dispatch_news([config], submit) == 0
    publish([article('CPI new'), article('CPI another')])
    assert news_triggers.dispatch_news([config], submit) == 1
    changed = calls[0]
    assert changed['model'] == 'custom-flash' and changed['api_key'] == 'offline-key'
    assert not changed['fallback_models'] and not changed['fallback_llm_provider_ids'] and changed['extra_body'] == {}
    assert config['model'] == 'expensive' and len(changed['_news_event']['articles']) == 2
    assert news_triggers.dispatch_news([dict(config)], submit) == 0
    publish([article('CPI next')])
    assert news_triggers.dispatch_news([config], submit) == 0
    assert database_news.read_state('news-trigger:task')['status'] == 'cooldown'
    database_news.update_state('news-trigger:task', {'last_dispatch': (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()})
    publish([article('CPI latest')])
    assert news_triggers.dispatch_news([config], submit) == 0
    assert database_news.read_state('news-trigger:task')['status'] == 'daily_limit'


@pytest.mark.parametrize('patch', [{'source_stale': True}, {'scheduled_at': '2026-10-10T00:00:00Z'}, {'published_at': None}, {'published_at': '2000-01-01T00:00:00Z'}, {'published_at': '2099-01-01T00:00:00Z'}, {'title': 'ordinary news', 'content': 'ordinary news'}])
def test_stale_calendar_old_future_or_unmatched_never_trigger(patch):
    assert not news_triggers.matches(article(**patch), rule(), datetime.now(timezone.utc))


def test_trigger_filter_groups_and_score_requirement():
    now = datetime.now(timezone.utc)
    filters = rule(source_ids=['test'], categories=['macro_market'], min_score=80)
    assert not news_triggers.matches(article(), filters, now)
    assert news_triggers.matches(article(scoring={'score': 90}), filters, now)
    with pytest.raises(ValidationError):
        NewsSettings(scoring_mode='off', trigger_rules=[filters])


@pytest.mark.parametrize('reason', ['regular', 'disabled', 'dca', 'global_disabled', 'invalid_model', 'busy'])
def test_trigger_dispatch_gates(env, reason):
    env['news']['trigger_rules'] = [rule().model_dump()]
    config = {'config_id': 'task', 'enabled': True, 'mode': 'REAL'}
    publish([article('CPI baseline')])
    news_triggers.dispatch_news([config], lambda _: pytest.fail('baseline'))
    publish([article('CPI next')])
    regular = set()
    if reason == 'regular': regular.add('task')
    if reason == 'disabled': config['enabled'] = False
    if reason == 'dca': config['mode'] = 'SPOT_DCA'
    if reason == 'global_disabled': env['enable_scheduler'] = False
    if reason == 'invalid_model': env['llm_providers'] = []
    def submit(_):
        assert reason == 'busy'
        return False
    assert news_triggers.dispatch_news([config], submit, regularly_due=regular) == 0


def test_events_expired_owner_cannot_publish(env):
    assert not database_news.publish_events([article()], 'bad-owner')
    assert not database_news.read_state('news-events').get('trigger_items')


def test_trigger_reservation_survives_submit_failure_and_lease_contention(env):
    env['news']['trigger_rules'] = [rule().model_dump()]
    config = {'config_id': 'task', 'enabled': True, 'mode': 'REAL'}
    publish([article('CPI baseline')])
    news_triggers.dispatch_news([config], lambda _: pytest.fail('baseline'))
    publish([article('CPI next')])
    owner = database_news.acquire_lease('news-trigger:task')
    assert news_triggers.dispatch_news([config], lambda _: pytest.fail('lease')) == 0
    database_news.update_state('news-trigger:task', {}, owner=owner, release=True)
    def fail(_):
        raise RuntimeError('submission interrupted')
    with pytest.raises(RuntimeError):
        news_triggers.dispatch_news([config], fail)
    assert database_news.read_state('news-trigger:task')['count'] == 1
    assert news_triggers.dispatch_news([config], lambda _: pytest.fail('must not replay')) == 0


def test_stream_signal_shortens_fetch_cadence_without_refresh_storm(env, monkeypatch):
    env['news']['stream_refresh_enabled'] = True
    collect(monkeypatch, [article()])
    monkeypatch.setattr(news_service, '_summarize', lambda *a: 'Digest')
    news_service.refresh_news(background=False)
    assert not news_service.refresh_news(force=False, background=False)['started']
    database_news.update_state('news', {'last_attempt_at': (datetime.now(timezone.utc)-timedelta(seconds=65)).isoformat()})
    database_news.update_state('binance-stream', {'last_message_at': database_news.now_iso()})
    assert news_service.refresh_news(force=False, background=False)['started']
    assert not news_service.refresh_news(force=False, background=False)['started']


@pytest.mark.parametrize('purpose,background,expected', [('decision', False, 'local'), ('chat', False, 'local'), ('strategy_summary', False, 'local'), ('memory_review', False, 'local'), ('daily_summary', False, False), ('daily_summary', True, 'local')])
def test_auxiliary_trace_switch_keeps_agent_calls_and_local_audit(env, monkeypatch, purpose, background, expected):
    from backend.agent.call_audit import audited_invoke
    from backend.config import config
    from langsmith import tracing_context, utils
    monkeypatch.setattr(config, 'langchain_background_tracing', background)
    def operation():
        assert utils.tracing_is_enabled() == expected
        return AIMessage(content='Response')
    with tracing_context(enabled='local'):
        audited_invoke(operation, config_id='task', purpose=purpose, model='offline', messages=[])
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM agent_runs WHERE purpose=?', (purpose,)).fetchone()[0] == 1


@pytest.mark.parametrize('mode', ['off', 'summary', 'full'])
def test_news_trace_modes_preserve_local_processing(env, monkeypatch, mode):
    from langsmith import get_current_run_tree, tracing_context
    env['news']['trace_mode'] = mode
    monkeypatch.setenv('LANGSMITH_API_KEY', 'synthetic-local-only')
    collect(monkeypatch, [article()])
    spans = []
    monkeypatch.setattr(news_service, '_summarize', lambda *a: spans.append(get_current_run_tree()) or 'Digest')
    with tracing_context(enabled='local'):
        result = news_service.refresh_news(background=False)
    assert result['status'] == 'success'
    assert bool(result['trace_id']) == (mode != 'off')
    if mode == 'full': assert spans[0].name == 'news.summarize'
    if mode == 'off': assert spans[0] is None
