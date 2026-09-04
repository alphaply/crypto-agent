import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from backend.utils import news_context
from backend import database
from backend.database_schema import initialize_schema


UTC = timezone.utc


def test_bls_ics_parser_handles_eastern_daylight_time():
    raw = b"""BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART;TZID=America/New_York:20260714T083000
SUMMARY:Consumer Price Index
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=America/New_York:20260715T100000
SUMMARY:Import Prices
END:VEVENT
END:VCALENDAR
"""
    events = news_context._parse_ics_events(raw, "BLS", news_context.CALENDAR_FILTERS["bls"])
    assert len(events) == 1
    assert events[0]["scheduled_at"] == "2026-07-14T12:30:00+00:00"


def test_bea_json_parser_keeps_only_gdp_and_pce(monkeypatch):
    payload = {
        "Gross Domestic Product": {"release_dates": ["2026-07-30T12:30:00+00:00"]},
        "Personal Income and Outlays": {"release_dates": ["2026-07-31T12:30:00+00:00"]},
        "International Trade": {"release_dates": ["2026-08-01T12:30:00+00:00"]},
    }
    monkeypatch.setattr(news_context, "_read_url", lambda *_args, **_kwargs: json.dumps(payload).encode())
    events = news_context._fetch_bea_calendar(1)
    assert [event["title"] for event in events] == ["Gross Domestic Product", "Personal Income and Outlays"]


def test_cryptocurrency_cv_parser_reads_articles_payload(monkeypatch):
    payload = {
        "articles": [
            {
                "title": "Treasury expands long-end liquidity support buybacks",
                "link": "https://example.com/treasury-buybacks",
                "pubDate": "2026-08-19T12:30:00Z",
                "source": "US Treasury Press",
                "category": "macro",
            }
        ]
    }
    requested = {}

    def fake_read(url, _timeout):
        requested["url"] = url
        return json.dumps(payload).encode()

    monkeypatch.setattr(news_context, "_read_url", fake_read)
    items = news_context._fetch_cryptocurrency_cv("ETH/USDT", 1, "macro")

    assert "category=macro" in requested["url"]
    assert len(items) == 1
    assert items[0]["category"] == "macro_market"
    assert items[0]["source"] == "US Treasury Press"
    assert items[0]["published_at"] == "2026-08-19T12:30:00+00:00"


def test_treasury_market_parser_keeps_release_time_and_link(monkeypatch):
    page = """
    <span class="date-format"><time datetime="2026-08-19T12:30:00Z" class="datetime">August 19, 2026</time></span><span></span>
    <h3 class="featured-stories__headline">
      <a href="/news/press-releases/sb0607">Treasury Announces Increased Sizes of Nominal Long-End Liquidity Support Buybacks</a>
    </h3>
    """
    monkeypatch.setattr(news_context, "_read_url", lambda *_args, **_kwargs: page.encode())

    items = news_context._fetch_treasury_market_news(1)

    assert len(items) == 1
    assert items[0]["category"] == "macro_policy"
    assert items[0]["published_at"] == "2026-08-19T12:30:00+00:00"
    assert items[0]["url"] == "https://home.treasury.gov/news/press-releases/sb0607"


def test_unrelated_local_attack_is_not_selected_as_geopolitical_news():
    now = datetime(2026, 8, 21, tzinfo=UTC)
    item = news_context._make_item(
        source="BBC",
        title="Several injured after sword attack at Swedish school",
        category="geopolitical",
        impact="medium",
        relevance=0.65,
        published_at=now,
    )

    assert news_context._classify_news([item], "ETH/USDT", now) == []


def test_keyword_matching_does_not_treat_warning_or_delaware_as_war():
    assert not news_context._contains_term("warning issued over infections", "war")
    assert not news_context._contains_term("tornadoes touch down in Delaware", "war")
    assert news_context._contains_term("impact of war on oil shipping", "war")


def test_news_digest_uses_global_summarizer_configuration(monkeypatch):
    from backend.config import config as global_config
    from backend.utils import llm_utils

    captured = {}
    news_context._digest_cache.clear()
    monkeypatch.setenv("NEWS_LLM_SUMMARY_ENABLED", "true")
    monkeypatch.setattr(global_config, "global_summarizer_model", "summary-model")
    monkeypatch.setattr(global_config, "global_summarizer_api_key", "summary-key")
    monkeypatch.setattr(global_config, "global_summarizer_api_base", "https://summary.example/v1")

    class FakeLlm:
        def invoke(self, messages):
            captured["prompt"] = messages[0].content
            return SimpleNamespace(content="- 美债回购规模调整，关注流动性影响", response_metadata={})

    def fake_build_chat_model(**kwargs):
        captured["config"] = kwargs
        return FakeLlm()

    monkeypatch.setattr(llm_utils, "build_chat_model", fake_build_chat_model)
    monkeypatch.setattr(llm_utils, "invoke_with_retry", lambda call, **_kwargs: call())
    item = news_context._make_item(
        source="U.S. Treasury",
        title="Treasury increases long-end liquidity support buybacks",
        category="macro_policy",
        impact="high",
        relevance=0.95,
        published_at=datetime(2026, 8, 19, 12, 30, tzinfo=UTC),
    )

    digest = news_context._news_digest([item], "ETH/USDT")

    assert "美债回购" in digest
    assert captured["config"]["model"] == "summary-model"
    assert captured["config"]["api_key"] == "summary-key"
    assert captured["config"]["base_url"] == "https://summary.example/v1"
    assert "Treasury increases" in captured["prompt"]


def test_fomc_parser_uses_second_meeting_day_at_2pm_eastern(monkeypatch):
    page = """
    >2026 FOMC Meetings</a>
    <div class="row fomc-meeting">
      <div class="fomc-meeting__month"><strong>July</strong></div>
      <div class="fomc-meeting__date">28-29</div>
    </div>
    >2027 FOMC Meetings</a>
    """
    monkeypatch.setattr(news_context, "_read_url", lambda *_args, **_kwargs: page.encode())
    events = news_context._fetch_fomc_calendar(1, datetime(2026, 7, 1, tzinfo=UTC))
    assert events[0]["scheduled_at"] == "2026-07-29T18:00:00+00:00"


def test_cached_source_falls_back_to_recent_persistent_payload(monkeypatch):
    news_context._memory_cache.clear()
    now = datetime(2026, 7, 11, 0, 0, tzinfo=UTC)
    cached_item = {"title": "cached"}
    monkeypatch.setattr(
        news_context,
        "_load_persistent_cache",
        lambda _key: {"fetched_at": news_context._iso(now - timedelta(hours=2)), "payload": [cached_item]},
    )

    def fail():
        raise TimeoutError("offline")

    result = news_context._fetch_cached(
        "test-source",
        fail,
        ttl=timedelta(minutes=10),
        stale_ttl=timedelta(hours=6),
        now=now,
    )
    assert result["status"] == "stale"
    assert result["items"] == [cached_item]


def test_fresh_persistent_cache_skips_network_after_restart(monkeypatch):
    news_context._memory_cache.clear()
    now = datetime(2026, 7, 11, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(
        news_context,
        "_load_persistent_cache",
        lambda _key: {"fetched_at": news_context._iso(now - timedelta(minutes=2)), "payload": [{"title": "fresh"}]},
    )

    def unexpected_fetch():
        raise AssertionError("network should not be called")

    result = news_context._fetch_cached(
        "fresh-source",
        unexpected_fetch,
        ttl=timedelta(minutes=10),
        stale_ttl=timedelta(hours=6),
        now=now,
    )
    assert result["status"] == "ok"
    assert result["items"] == [{"title": "fresh"}]


def test_intelligence_cache_persists_across_process_memory(tmp_path, monkeypatch):
    db_path = tmp_path / "intelligence.sqlite"
    with sqlite3.connect(db_path) as conn:
        initialize_schema(conn)
    monkeypatch.setattr(database, "DB_NAME", str(db_path))
    database.save_intelligence_source_cache("calendar_bls", "2026-07-11T00:00:00+00:00", [{"title": "CPI"}])
    cached = database.get_intelligence_source_cache("calendar_bls")
    assert cached["payload"] == [{"title": "CPI"}]


def test_aggregator_enforces_six_item_category_budget(monkeypatch):
    now = datetime(2026, 7, 11, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(news_context, "_utc_now", lambda: now)
    monkeypatch.setenv("NEWS_MAX_ITEMS", "6")
    monkeypatch.setenv("CRYPTOCURRENCY_CV_ENABLED", "true")

    calendar = [
        news_context._make_item(source="BLS", title=f"Macro {index}", category="macro_calendar", impact="high", relevance=1, scheduled_at=now + timedelta(days=index + 1), status="upcoming")
        for index in range(3)
    ]
    crypto = [
        news_context._make_item(source="crypto", title=title, category="crypto", impact="medium", relevance=0.9 - index / 100, published_at=now)
        for index, title in enumerate([
            "Bitcoin miners reduce exchange deposits",
            "BTC institutional demand rises in Asia",
            "Bitcoin volatility reaches monthly low",
            "BTC options traders adjust downside hedges",
        ])
    ]
    policy = [news_context._make_item(source="SEC", title="SEC digital asset policy update", category="policy", impact="medium", relevance=0.9, published_at=now)]
    critical = [news_context._make_item(source="crypto", title="Major exchange withdrawal halt", category="crypto", impact="high", relevance=1, published_at=now)]

    monkeypatch.setattr(news_context, "_fetch_bls_calendar", lambda _timeout: calendar)
    monkeypatch.setattr(news_context, "_fetch_bea_calendar", lambda _timeout: [])
    monkeypatch.setattr(news_context, "_fetch_fomc_calendar", lambda _timeout, _now: [])
    monkeypatch.setattr(news_context, "_fetch_cryptocurrency_cv", lambda _symbol, _timeout, _category="general": [*crypto, *critical])
    monkeypatch.setattr(news_context, "_fetch_feed_url", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(news_context, "_fetch_policy_source", lambda key, _url, _timeout: policy if key == "sec" else [])
    monkeypatch.setattr(news_context, "_fetch_treasury_market_news", lambda _timeout: [])
    monkeypatch.setattr(news_context, "_news_digest", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(
        news_context,
        "_fetch_cached",
        lambda _key, fetcher, **_kwargs: {"items": fetcher(), "status": "ok", "stale": False, "fetched_at": news_context._iso(now)},
    )

    result = news_context.fetch_news_risk_context("BTC/USDT")
    categories = [item["category"] for item in result["items"]]
    assert len(result["items"]) == 6
    assert categories.count("macro_calendar") == 2
    assert categories.count("critical") == 1
    assert categories.count("crypto") == 2
    assert categories.count("policy") == 1


def test_all_sources_unavailable_has_no_risk_level_fields(monkeypatch):
    news_context._memory_cache.clear()
    monkeypatch.setattr(
        news_context,
        "_fetch_cached",
        lambda *_args, **_kwargs: {"items": [], "status": "unavailable", "stale": False, "error": "offline"},
    )
    result = news_context.fetch_news_risk_context("BTC/USDT")
    assert "risk_level" not in result
    assert "risk_reasons" not in result
    assert result["headlines"] == []
    assert "unavailable" in result["error"].lower()
