import json
import sqlite3
from datetime import datetime, timedelta, timezone

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


def test_risk_windows_are_watch_at_24h_and_high_at_6h():
    now = datetime(2026, 7, 11, 0, 0, tzinfo=UTC)
    event = news_context._make_item(
        source="BLS",
        title="Consumer Price Index",
        category="macro_calendar",
        impact="high",
        relevance=1,
        scheduled_at=now + timedelta(hours=20),
        status="upcoming",
    )
    assert news_context._risk_assessment([event], now)[0] == "watch"
    event["scheduled_at"] = news_context._iso(now + timedelta(hours=4))
    assert news_context._risk_assessment([event], now)[0] == "high"


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
    monkeypatch.setattr(news_context, "_fetch_cryptocurrency_cv", lambda _symbol, _timeout: [*crypto, *critical])
    monkeypatch.setattr(news_context, "_fetch_feed_url", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(news_context, "_fetch_policy_source", lambda key, _url, _timeout: policy if key == "sec" else [])
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


def test_all_sources_unavailable_is_not_normal_risk(monkeypatch):
    news_context._memory_cache.clear()
    monkeypatch.setattr(
        news_context,
        "_fetch_cached",
        lambda *_args, **_kwargs: {"items": [], "status": "unavailable", "stale": False, "error": "offline"},
    )
    result = news_context.fetch_news_risk_context("BTC/USDT")
    assert result["risk_level"] == "unknown"
    assert result["headlines"] == []
    assert "unavailable" in result["error"].lower()
