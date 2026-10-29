"""Time comparisons are deterministic and read-only; model prose retains its source zone."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.utils.event_time import (
    EVENT_TIME_POLICY, article_time_fields, explicit_datetime, news_time_context, source_time_text,
)


GDP = {'title': 'GDP', 'category': 'macro_calendar', 'scheduled_at': '2026-10-29T12:30:00+00:00',
       'published_at': '2026-10-28T08:00:00+00:00'}


@pytest.mark.parametrize('value,expected', [
    ('2026-10-29T12:30:00Z', '2026-10-29 12:30:00 UTC'),
    ('2026-10-29T12:30:00+00:00', '2026-10-29 12:30:00 UTC'),
    ('2026-10-29T20:30:00+08:00', '2026-10-29 20:30:00 UTC+08:00'),
    ('2026-07-01T08:30:00-04:00', '2026-07-01 08:30:00 UTC-04:00'),
    ('2026-12-01T08:30:00-05:00', '2026-12-01 08:30:00 UTC-05:00'),
    ('2026-10-29 12:30', '2026-10-29 12:30'),
    ('12:30 ET', '12:30 ET'),
    ('2026-10-29', '2026-10-29'),
    ('invalid', 'invalid'),
])
def test_source_display_preserves_offset_and_never_guesses(value, expected):
    assert source_time_text(value) == expected


def test_gdp_read_only_replay_before_and_after_scheduled_instant():
    from backend.agent.decision_context import DecisionMemory, render_decision_prompt
    snapshot = {'as_of': '2026-10-29T05:00:00Z', 'events': [GDP]}
    original = deepcopy(snapshot)
    before = datetime.fromisoformat('2026-10-29T13:00:00+08:00')
    context = news_time_context(snapshot, now=before)
    prompt = render_decision_prompt('{formatted_market_data}', DecisionMemory('', ''), formatted_market_data=context)
    assert '2026-10-29 12:30:00 UTC' in prompt
    assert 'upcoming（预定时刻未到）' in prompt
    assert '快照采集时间（非事件公布时间）' in prompt
    assert EVENT_TIME_POLICY in prompt
    assert 'scheduled_time_reached（预定时刻已到' not in context
    after = news_time_context(snapshot, now=datetime.fromisoformat('2026-10-29T20:30:00+08:00'))
    assert 'scheduled_time_reached（预定时刻已到，不证明实际公布）' in after
    assert snapshot == original  # No record changes, model calls or trade tools.


def test_cross_date_and_dst_use_real_instants():
    instant = explicit_datetime('2026-10-29T20:30:00Z')
    assert instant.astimezone(ZoneInfo('Asia/Shanghai')).isoformat() == '2026-10-30T04:30:00+08:00'
    for date, utc_hour, offset in [('2026-07-01', 12, '-04:00'), ('2026-12-01', 13, '-05:00')]:
        value = f'{date}T08:30:00{offset}'
        assert explicit_datetime(value).astimezone(ZoneInfo('UTC')).hour == utc_hour
        text = news_time_context({'events': [{'title': 'release', 'scheduled_at': value}]},
                                 now=datetime.fromisoformat(f'{date}T{utc_hour:02d}:29:59+00:00'))
        assert 'upcoming' in text and f'UTC{offset}' in text


def test_unknown_times_do_not_become_release_facts():
    assert explicit_datetime('2026-10-29 12:30:00') is None
    assert '无法比较' in news_time_context({'events': [{'title': 'release', 'scheduled_at': '12:30'}]})
    with pytest.raises(ValueError, match='explicit timezone'):
        news_time_context({}, now=datetime(2026, 10, 29))
    enriched = article_time_fields(GDP)
    assert enriched['scheduled_at'] == GDP['scheduled_at']
    assert enriched['published_at'] == GDP['published_at']
    assert enriched['scheduled_at_display'] != enriched['published_at_display']


def test_policy_and_source_times_survive_all_prompt_boundaries(monkeypatch):
    from backend.app.services import news_service
    from backend.agent import chat_graph, memory_agent
    from backend.agent.summary_prompts import STRATEGY_BREVITY_POLICY
    from backend.utils.formatters import format_spot_portfolio_news
    from backend.utils.news_context import _display_title
    captured = {}

    def generate(provider, instructions, data, purpose):
        captured.update(instructions=instructions, data=data, purpose=purpose)
        return 'GDP 2026-10-29 12:30 UTC，预定数据公布。'

    monkeypatch.setattr(news_service, 'generate', generate)
    result = news_service._summarize([GDP], {})
    assert EVENT_TIME_POLICY in captured['instructions']
    assert captured['data']['current_sources'][0]['scheduled_at'] == GDP['scheduled_at']
    assert captured['data']['current_sources'][0]['scheduled_at_display'].endswith(' UTC')
    assert '预定时间 2026-10-29 12:30:00 UTC' in captured['data']['time_context']
    assert EVENT_TIME_POLICY in STRATEGY_BREVITY_POLICY
    assert EVENT_TIME_POLICY in memory_agent.MEMORY_REVIEW_POLICY
    news = {'digest': result, 'headlines': ['GDP'], 'events': [GDP], 'available': True}
    assert '2026-10-29 12:30:00 UTC' in chat_graph._temporary_news_context_text(news)
    portfolio = format_spot_portfolio_news({'BTC': news, 'ETH': news})
    assert portfolio.count('预定时间 2026-10-29 12:30:00 UTC') == 1
    assert '2026-10-29 12:30:00 UTC' in _display_title(GDP)


def test_time_policy_version_invalidates_summary_cache(pg, monkeypatch):
    from backend.app.services import news_service
    from backend.database import news as database_news
    from backend.utils import llm_utils
    monkeypatch.setattr(llm_utils, 'sync_langsmith_environment', lambda: {})
    monkeypatch.setenv('LANGSMITH_TRACING', 'false')
    monkeypatch.setenv('LANGCHAIN_TRACING_V2', 'false')
    runtime = {'news': {'scoring_mode': 'off', 'summarizer_provider_id': 'offline'},
               'llm_providers': [{'provider_id': 'offline', 'model': 'offline', 'api_key': 'test'}]}
    monkeypatch.setattr(news_service, '_runtime', lambda: runtime)
    event = {**GDP, 'published_at': None,
             'scheduled_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
    monkeypatch.setattr(news_service, 'collect_candidates', lambda *a: ([event], {'fixture': {'status': 'ok'}}, {}))
    calls = []
    monkeypatch.setattr(news_service, '_summarize', lambda *a: calls.append(a) or 'GDP 12:30 UTC')
    monkeypatch.setattr(news_service, 'EVENT_TIME_POLICY_VERSION', 'old-time-rule')
    assert news_service.refresh_news(background=False)['status'] == 'success'
    previous = deepcopy(database_news.latest_snapshot())
    assert news_service.refresh_news(background=False)['summary_reused']
    monkeypatch.setattr(news_service, 'EVENT_TIME_POLICY_VERSION', 'new-time-rule')
    assert not news_service.refresh_news(background=False)['summary_reused']
    assert len(calls) == 2
    raw = deepcopy(database_news.latest_snapshot())
    enriched = news_service.get_latest_news()
    assert enriched['events'][0]['scheduled_at'] == event['scheduled_at']
    assert enriched['events'][0]['scheduled_at_display'].endswith(' UTC')
    assert EVENT_TIME_POLICY == enriched['time_policy']
    assert database_news.latest_snapshot() == raw
    assert previous['digest'] == 'GDP 12:30 UTC'
