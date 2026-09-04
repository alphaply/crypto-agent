from __future__ import annotations

import hashlib
import html
import json
import os
import re
import threading
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from typing import Any, Callable
from zoneinfo import ZoneInfo

from backend.utils.logger import setup_logger


logger = setup_logger("NewsContext")
UTC = timezone.utc
EASTERN = ZoneInfo("America/New_York")

DEFAULT_RSS_SOURCES = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
]
DEFAULT_GEOPOLITICAL_SOURCES = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",
]
DEFAULT_POLICY_SOURCES = {
    "fed_monetary": "https://www.federalreserve.gov/feeds/press_monetary.xml",
    "sec": "https://www.sec.gov/news/pressreleases.rss",
    "cftc": "https://www.cftc.gov/PressRoom/PressReleases",
}
DEFAULT_TREASURY_SOURCE = "https://home.treasury.gov/news/press-releases"
DEFAULT_CALENDAR_SOURCES = {
    "bls": "https://www.bls.gov/schedule/news_release/bls.ics",
    "bea": "https://apps.bea.gov/API/signup/release_dates.json",
    "fomc": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
}

CRYPTO_ALIASES = {
    "BTC": {"bitcoin", "btc"},
    "ETH": {"ethereum", "ether", "eth"},
    "SOL": {"solana", "sol"},
    "BNB": {"binance", "bnb"},
    "XRP": {"ripple", "xrp"},
}
CRYPTO_POLICY_TERMS = {
    "crypto", "cryptocurrency", "digital asset", "bitcoin", "ethereum", "stablecoin",
    "token", "blockchain", "exchange-traded fund", "spot etf", "coinbase", "binance",
}
MACRO_MARKET_TERMS = {
    "treasuries", "treasury yield", "bond yield", "yield curve", "auction size",
    "bill issuance", "debt issuance", "quarterly refunding", "buyback", "liquidity support",
    "cash management bill", "tga", "treasury general account", "soma", "repo", "reserves",
    "balance sheet", "quantitative tightening", "quantitative easing", "dollar", "dxy",
    "federal funds", "interest rate", "rate cut", "rate hike", "monetary policy", "fomc",
    "inflation", "consumer price", "producer price", "payroll", "employment", "gdp",
    "personal consumption expenditures", "pce", "financial conditions", "credit spread",
}
CRITICAL_TERMS = {
    "hack", "exploit", "stolen", "breach", "depeg", "de-peg", "insolvent", "bankruptcy",
    "withdrawal halt", "suspend withdrawals", "outage", "chain halt", "stopped producing blocks",
    "emergency", "liquidation cascade", "delist", "sanction", "war", "attack",
}
GEOPOLITICAL_TERMS = {
    "war", "military", "missile", "drone strike", "sanction", "tariff", "middle east",
    "israel", "iran", "china", "russia", "ukraine", "oil", "opec", "shipping", "strait",
    "election", "trade war",
}
CALENDAR_FILTERS = {
    "bls": (
        "consumer price index", "producer price index", "employment situation",
        "job openings and labor turnover", "jolts",
    ),
    "bea": ("gross domestic product", "personal income and outlays"),
}

_cache_lock = threading.RLock()
_memory_cache: dict[str, dict[str, Any]] = {}
_failure_cache: dict[str, dict[str, Any]] = {}
_source_locks: dict[str, threading.Lock] = {}
_digest_cache: dict[str, dict[str, Any]] = {}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)
    except Exception:
        return None


def _read_url(url: str, timeout: float = 4.0) -> bytes:
    user_agent = os.getenv("NEWS_USER_AGENT", "crypto-agent/1.0 contact@example.com")
    request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except HTTPError as exc:
        if exc.code not in {301, 302, 307, 308} or not exc.headers.get("Location"):
            raise
        redirected = urllib.parse.urljoin(url, exc.headers["Location"])
        retry = urllib.request.Request(redirected, headers={"User-Agent": user_agent, "Accept": "*/*"})
        with urllib.request.urlopen(retry, timeout=timeout) as response:
            return response.read()


def _source_lock(source_key: str) -> threading.Lock:
    with _cache_lock:
        return _source_locks.setdefault(source_key, threading.Lock())


def _load_persistent_cache(source_key: str) -> dict[str, Any] | None:
    try:
        from backend.database import get_intelligence_source_cache

        return get_intelligence_source_cache(source_key)
    except Exception as exc:
        logger.debug("Persistent intelligence cache read failed source=%s: %s", source_key, exc)
        return None


def _save_persistent_cache(source_key: str, fetched_at: datetime, payload: Any) -> None:
    try:
        from backend.database import save_intelligence_source_cache

        save_intelligence_source_cache(source_key, _iso(fetched_at), payload)
    except Exception as exc:
        logger.debug("Persistent intelligence cache write failed source=%s: %s", source_key, exc)


def _fetch_cached(
    source_key: str,
    fetcher: Callable[[], list[dict[str, Any]]],
    *,
    ttl: timedelta,
    stale_ttl: timedelta,
    now: datetime,
) -> dict[str, Any]:
    with _cache_lock:
        cached = _memory_cache.get(source_key)
        recent_failure = _failure_cache.get(source_key)
    if cached and now - cached["fetched_at"] <= ttl:
        return {"items": cached["payload"], "status": "ok", "stale": False, "fetched_at": _iso(cached["fetched_at"])}
    if recent_failure and now - recent_failure["failed_at"] <= ttl:
        return dict(recent_failure["result"])

    with _source_lock(source_key):
        with _cache_lock:
            cached = _memory_cache.get(source_key)
        if cached and now - cached["fetched_at"] <= ttl:
            return {"items": cached["payload"], "status": "ok", "stale": False, "fetched_at": _iso(cached["fetched_at"])}
        persisted = _load_persistent_cache(source_key)
        persisted_at = _parse_iso(persisted.get("fetched_at")) if persisted else None
        if persisted_at and now - persisted_at <= ttl:
            payload = persisted.get("payload") or []
            with _cache_lock:
                _memory_cache[source_key] = {"fetched_at": persisted_at, "payload": payload}
            return {"items": payload, "status": "ok", "stale": False, "fetched_at": _iso(persisted_at)}
        try:
            payload = fetcher()
            fetched_at = now
            with _cache_lock:
                _memory_cache[source_key] = {"fetched_at": fetched_at, "payload": payload}
                _failure_cache.pop(source_key, None)
            _save_persistent_cache(source_key, fetched_at, payload)
            return {"items": payload, "status": "ok", "stale": False, "fetched_at": _iso(fetched_at)}
        except Exception as exc:
            logger.warning("Intelligence source failed source=%s: %s", source_key, exc)
            candidates = []
            if cached:
                candidates.append({"fetched_at": cached["fetched_at"], "payload": cached["payload"]})
            if persisted:
                candidates.append({"fetched_at": persisted_at, "payload": persisted.get("payload") or []})
            candidates = [item for item in candidates if item.get("fetched_at")]
            candidates.sort(key=lambda item: item["fetched_at"], reverse=True)
            if candidates and now - candidates[0]["fetched_at"] <= stale_ttl:
                result = {
                    "items": candidates[0]["payload"],
                    "status": "stale",
                    "stale": True,
                    "fetched_at": _iso(candidates[0]["fetched_at"]),
                    "error": str(exc),
                }
            else:
                result = {"items": [], "status": "unavailable", "stale": False, "error": str(exc)}
            with _cache_lock:
                _failure_cache[source_key] = {"failed_at": now, "result": result}
            return result


def _symbol_terms(symbol: str) -> set[str]:
    base = str(symbol or "").split("/")[0].split(":")[0].upper()
    return {term.lower() for term in ({base.lower()} | CRYPTO_ALIASES.get(base, set())) if term}


def _contains_term(text: str, term: str) -> bool:
    normalized_text = str(text or "").lower()
    normalized_term = str(term or "").strip().lower()
    if not normalized_term:
        return False
    return bool(
        re.search(
            rf"(?<![a-z0-9]){re.escape(normalized_term)}(?![a-z0-9])",
            normalized_text,
        )
    )


def _contains_any(text: str, terms: set[str]) -> bool:
    return any(_contains_term(text, term) for term in terms)


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))).strip()


def _item_id(source: str, title: str, when: str | None = None) -> str:
    return hashlib.sha1(f"{source}|{title.lower()}|{when or ''}".encode("utf-8")).hexdigest()[:20]


def _make_item(
    *,
    source: str,
    title: str,
    category: str,
    impact: str,
    relevance: float,
    scheduled_at: datetime | None = None,
    published_at: datetime | None = None,
    url: str = "",
    status: str = "published",
) -> dict[str, Any]:
    scheduled_iso = _iso(scheduled_at)
    published_iso = _iso(published_at)
    return {
        "id": _item_id(source, title, scheduled_iso or published_iso),
        "category": category,
        "title": _clean_text(title),
        "scheduled_at": scheduled_iso,
        "published_at": published_iso,
        "impact": impact,
        "relevance": round(max(0.0, min(float(relevance), 1.0)), 2),
        "source": source,
        "url": url,
        "status": status,
    }


def _unfold_ics(raw: bytes) -> list[str]:
    lines = raw.decode("utf-8", errors="ignore").replace("\r\n", "\n").split("\n")
    unfolded: list[str] = []
    for line in lines:
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def _parse_ics_datetime(key: str, value: str) -> datetime | None:
    tz_match = re.search(r"TZID=([^;:]+)", key, flags=re.IGNORECASE)
    tz = EASTERN
    if tz_match:
        tz_name = tz_match.group(1).strip()
        try:
            tz = EASTERN if tz_name.lower() in {"us-eastern", "eastern standard time"} else ZoneInfo(tz_name)
        except Exception:
            tz = EASTERN
    value = value.strip()
    try:
        if value.endswith("Z"):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        if "T" in value:
            parsed = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
            return parsed.replace(tzinfo=tz).astimezone(UTC)
        return datetime.strptime(value[:8], "%Y%m%d").replace(hour=8, minute=30, tzinfo=tz).astimezone(UTC)
    except Exception:
        return None


def _parse_ics_events(raw: bytes, source: str, filters: tuple[str, ...]) -> list[dict[str, Any]]:
    records: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in _unfold_ics(raw):
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            if current is not None:
                records.append(current)
            current = None
        elif current is not None and ":" in line:
            key, value = line.split(":", 1)
            current[key] = value
    items = []
    for record in records:
        summary = next((value for key, value in record.items() if key.upper().startswith("SUMMARY")), "")
        if not any(term in summary.lower() for term in filters):
            continue
        dt_key = next((key for key in record if key.upper().startswith("DTSTART")), "")
        scheduled = _parse_ics_datetime(dt_key, record.get(dt_key, ""))
        if scheduled:
            items.append(_make_item(source=source, title=summary, category="macro_calendar", impact="high", relevance=1.0, scheduled_at=scheduled, status="upcoming"))
    return items


def _fetch_bls_calendar(timeout: float) -> list[dict[str, Any]]:
    url = os.getenv("NEWS_BLS_CALENDAR_URL", DEFAULT_CALENDAR_SOURCES["bls"])
    return _parse_ics_events(_read_url(url, timeout), "BLS", CALENDAR_FILTERS["bls"])


def _fetch_bea_calendar(timeout: float) -> list[dict[str, Any]]:
    url = os.getenv("NEWS_BEA_CALENDAR_URL", DEFAULT_CALENDAR_SOURCES["bea"])
    payload = json.loads(_read_url(url, timeout).decode("utf-8", errors="ignore"))
    items = []
    for title, raw in (payload.items() if isinstance(payload, dict) else []):
        if not any(term in title.lower() for term in CALENDAR_FILTERS["bea"]):
            continue
        dates = raw.get("release_dates") or [] if isinstance(raw, dict) else []
        for value in dates:
            scheduled = _parse_iso(value)
            if scheduled:
                items.append(_make_item(source="BEA", title=title, category="macro_calendar", impact="high", relevance=1.0, scheduled_at=scheduled, status="upcoming"))
    return items


def _fetch_fomc_calendar(timeout: float, now: datetime) -> list[dict[str, Any]]:
    url = os.getenv("NEWS_FOMC_CALENDAR_URL", DEFAULT_CALENDAR_SOURCES["fomc"])
    page = _read_url(url, timeout).decode("utf-8", errors="ignore")
    items = []
    current_year = None
    token_pattern = re.compile(
        r'(?P<year>20\d{2})\s+FOMC Meetings|'
        r'fomc-meeting__month[^>]*>\s*<strong>(?P<month>[^<]+)</strong>.*?'
        r'fomc-meeting__date[^>]*>\s*(?P<first>[0-9]{1,2})(?:-(?P<last>[0-9]{1,2}))?\*?\s*</div>',
        flags=re.IGNORECASE | re.DOTALL,
    )
    for match in token_pattern.finditer(page):
        if match.group("year"):
            current_year = int(match.group("year"))
            continue
        if current_year not in {now.year, now.year + 1}:
            continue
        try:
            month = datetime.strptime(match.group("month").strip(), "%B").month
            day = int(match.group("last") or match.group("first"))
            scheduled = datetime(current_year, month, day, 14, 0, tzinfo=EASTERN).astimezone(UTC)
        except Exception:
            continue
        items.append(_make_item(source="Federal Reserve", title="FOMC interest-rate decision", category="macro_calendar", impact="high", relevance=1.0, scheduled_at=scheduled, url=url, status="upcoming"))
    return items


def _parse_feed(raw: bytes, source: str, category: str, relevance: float = 0.8) -> list[dict[str, Any]]:
    root = ET.fromstring(raw)
    items = []
    nodes = root.findall(".//item") or root.findall(".//{*}entry")
    for node in nodes:
        title = node.findtext("title") or node.findtext("{*}title") or ""
        link = node.findtext("link") or ""
        if not link:
            link_node = node.find("{*}link")
            link = link_node.get("href", "") if link_node is not None else ""
        published_raw = node.findtext("pubDate") or node.findtext("{*}published") or node.findtext("{*}updated")
        published = None
        if published_raw:
            try:
                published = parsedate_to_datetime(published_raw).astimezone(UTC)
            except Exception:
                published = _parse_iso(published_raw)
        if _clean_text(title):
            items.append(_make_item(source=source, title=title, category=category, impact="medium", relevance=relevance, published_at=published, url=link))
    return items


def _fetch_cryptocurrency_cv(symbol: str, timeout: float, category: str = "general") -> list[dict[str, Any]]:
    base = str(symbol or "").split("/")[0].split(":")[0].lower()
    params = {
        "limit": max(3, min(25, int(os.getenv("NEWS_CV_LIMIT", "10")))),
    }
    if category and category != "general":
        params["category"] = category
    elif base in {"bitcoin", "btc"}:
        params["category"] = "bitcoin"
    elif base in {"ethereum", "ether", "eth"}:
        params["category"] = "ethereum"
    url = f"https://cryptocurrency.cv/api/news?{urllib.parse.urlencode(params)}"
    payload = json.loads(_read_url(url, timeout).decode("utf-8", errors="ignore"))
    rows = (
        payload.get("articles")
        or payload.get("data")
        or payload.get("news")
        or payload.get("items")
        or []
    ) if isinstance(payload, dict) else payload
    items = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        title = row.get("title") or row.get("headline")
        if title:
            title_l = str(title).lower()
            raw_category = str(row.get("category") or category or "general").lower()
            source = str(row.get("source") or "cryptocurrency.cv")
            if raw_category in {"macro", "tradfi", "mainstream"} or _contains_any(title_l, MACRO_MARKET_TERMS):
                normalized_category = "macro_market"
            elif raw_category == "geopolitical":
                normalized_category = "geopolitical"
            elif source.lower() in {"federal reserve", "federal reserve feds notes", "us treasury press"}:
                normalized_category = "macro_policy"
            else:
                normalized_category = "crypto"
            relevance = 0.95 if _contains_any(title_l, _symbol_terms(symbol)) else 0.82
            items.append(
                _make_item(
                    source=source,
                    title=title,
                    category=normalized_category,
                    impact="high" if normalized_category in {"macro_market", "macro_policy"} else "medium",
                    relevance=relevance,
                    published_at=_parse_iso(
                        row.get("published_at")
                        or row.get("pubDate")
                        or row.get("date")
                        or row.get("time")
                    ),
                    url=str(row.get("url") or row.get("link") or ""),
                )
            )
    return items


def _fetch_treasury_market_news(timeout: float) -> list[dict[str, Any]]:
    url = os.getenv("NEWS_TREASURY_URL", DEFAULT_TREASURY_SOURCE)
    page = _read_url(url, timeout).decode("utf-8", errors="ignore")
    patterns = (
        re.compile(
            r'<span[^>]+class="date-format"[^>]*>\s*<time[^>]+datetime="(?P<date>[^"]+)"[^>]*>.*?</time>\s*</span>'
            r'.*?<h3[^>]+class="featured-stories__headline"[^>]*>\s*'
            r'<a[^>]+href="(?P<link>/news/press-releases/[^"]+)"[^>]*>(?P<title>.*?)</a>',
            flags=re.IGNORECASE | re.DOTALL,
        ),
        re.compile(
            r'<div[^>]+class="mm-news-row"[^>]*>\s*<time[^>]+datetime="(?P<date>[^"]+)"[^>]*>.*?</time>'
            r'.*?<div[^>]+class="news-title"[^>]*>\s*'
            r'<a[^>]+href="(?P<link>/news/press-releases/[^"]+)"[^>]*>(?P<title>.*?)</a>',
            flags=re.IGNORECASE | re.DOTALL,
        ),
    )
    items = []
    seen = set()
    for pattern in patterns:
        for match in pattern.finditer(page):
            title = _clean_text(match.group("title"))
            link = urllib.parse.urljoin(url, match.group("link"))
            key = (title.lower(), link)
            if not title or key in seen:
                continue
            seen.add(key)
            items.append(
                _make_item(
                    source="U.S. Treasury",
                    title=title,
                    category="macro_policy",
                    impact="high",
                    relevance=0.95,
                    published_at=_parse_iso(match.group("date")),
                    url=link,
                )
            )
    return sorted(items, key=lambda item: item.get("published_at") or "", reverse=True)


def _fetch_crypto_rss(timeout: float) -> list[dict[str, Any]]:
    sources = [item.strip() for item in os.getenv("NEWS_RSS_SOURCES", ",".join(DEFAULT_RSS_SOURCES)).split(",") if item.strip()]
    items = []
    for index, url in enumerate(sources):
        try:
            items.extend(_parse_feed(_read_url(url, timeout), f"crypto_rss_{index + 1}", "crypto", 0.75))
        except Exception as exc:
            logger.debug("Crypto RSS failed source=%s: %s", url, exc)
    return items


def _fetch_feed_url(url: str, source: str, category: str, relevance: float, timeout: float) -> list[dict[str, Any]]:
    return _parse_feed(_read_url(url, timeout), source, category, relevance)


def _fetch_policy_source(source_key: str, url: str, timeout: float) -> list[dict[str, Any]]:
    raw = _read_url(url, timeout)
    category = "macro_policy" if source_key == "fed_monetary" else "policy"
    try:
        return _parse_feed(raw, source_key.upper(), category, 0.9 if category == "macro_policy" else 0.85)
    except Exception:
        page = raw.decode("utf-8", errors="ignore")
        pattern = re.compile(r'href="([^"]*PressRoom/PressReleases/[^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
        return [_make_item(source=source_key.upper(), title=_clean_text(title), category=category, impact="medium", relevance=0.8, url=urllib.parse.urljoin(url, link)) for link, title in pattern.findall(page) if _clean_text(title)]


def _fetch_geopolitical(timeout: float) -> list[dict[str, Any]]:
    sources = [item.strip() for item in os.getenv("NEWS_MACRO_RSS_SOURCES", ",".join(DEFAULT_GEOPOLITICAL_SOURCES)).split(",") if item.strip()]
    items = []
    for index, url in enumerate(sources):
        try:
            items.extend(_parse_feed(_read_url(url, timeout), f"macro_rss_{index + 1}", "geopolitical", 0.65))
        except Exception as exc:
            logger.debug("Macro RSS failed source=%s: %s", url, exc)
    return items


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", title.lower()).strip()


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: list[set[str]] = []
    result = []
    for item in sorted(items, key=lambda row: (row.get("relevance", 0), row.get("published_at") or row.get("scheduled_at") or ""), reverse=True):
        tokens = set(_normalize_title(item.get("title", "")).split())
        if not tokens:
            continue
        if any(len(tokens & previous) / max(1, min(len(tokens), len(previous))) >= 0.75 for previous in seen):
            continue
        seen.append(tokens)
        result.append(item)
    return result


def _dedupe_preserve_order(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    result = []
    for item in items:
        key = item.get("id") or _normalize_title(item.get("title", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _classify_news(items: list[dict[str, Any]], symbol: str, now: datetime) -> list[dict[str, Any]]:
    symbol_terms = _symbol_terms(symbol)
    crypto_cutoff = now - timedelta(hours=max(12, int(os.getenv("NEWS_CRYPTO_LOOKBACK_HOURS", "48"))))
    macro_cutoff = now - timedelta(hours=max(48, int(os.getenv("NEWS_MACRO_LOOKBACK_HOURS", "336"))))
    geopolitical_cutoff = now - timedelta(hours=max(24, int(os.getenv("NEWS_GEOPOLITICAL_LOOKBACK_HOURS", "72"))))
    result = []
    for item in items:
        title_l = item.get("title", "").lower()
        published = _parse_iso(item.get("published_at"))
        category = item.get("category")
        cutoff = macro_cutoff if category in {"macro_policy", "macro_market", "policy"} else geopolitical_cutoff if category == "geopolitical" else crypto_cutoff
        if published and published < cutoff:
            continue
        if category == "crypto":
            symbol_match = _contains_any(title_l, symbol_terms)
            broad_match = _contains_any(title_l, CRYPTO_POLICY_TERMS | CRITICAL_TERMS)
            if not (symbol_match or broad_match):
                continue
            item["relevance"] = max(item.get("relevance", 0), 0.95 if symbol_match else 0.72)
        elif category == "policy":
            if not _contains_any(title_l, CRYPTO_POLICY_TERMS):
                continue
        elif category == "macro_policy":
            if not _contains_any(title_l, MACRO_MARKET_TERMS):
                continue
            item["relevance"] = max(item.get("relevance", 0), 0.92)
        elif category == "macro_market":
            if not _contains_any(title_l, MACRO_MARKET_TERMS):
                continue
            item["relevance"] = max(item.get("relevance", 0), 0.88)
        elif category == "geopolitical":
            if not _contains_any(title_l, GEOPOLITICAL_TERMS):
                continue
        if _contains_any(title_l, CRITICAL_TERMS):
            item["category"] = "critical"
            item["impact"] = "high"
            item["relevance"] = max(item.get("relevance", 0), 0.95)
        result.append(item)
    return _dedupe(result)


def _news_digest(items: list[dict[str, Any]], symbol: str) -> str:
    if not items or str(os.getenv("NEWS_LLM_SUMMARY_ENABLED", "true")).lower() in {"0", "false", "no"}:
        return ""
    try:
        from langchain_core.messages import HumanMessage

        from backend.config import config as global_config
        from backend.database import save_token_usage
        from backend.utils.llm_utils import build_chat_model, extract_message_text, invoke_with_retry

        model = str(getattr(global_config, "global_summarizer_model", "") or os.getenv("GLOBAL_SUMMARIZER_MODEL", "")).strip()
        api_key = str(getattr(global_config, "global_summarizer_api_key", "") or os.getenv("GLOBAL_SUMMARIZER_API_KEY", "")).strip()
        api_base = str(getattr(global_config, "global_summarizer_api_base", "") or os.getenv("GLOBAL_SUMMARIZER_API_BASE", "")).strip()
        if not model or not api_key:
            return ""

        candidates = items[:max(6, min(30, int(os.getenv("NEWS_LLM_CANDIDATE_ITEMS", "20"))))]
        input_lines = []
        for item in candidates:
            when = item.get("scheduled_at") or item.get("published_at") or "time unknown"
            input_lines.append(
                f"- [{item.get('category')}] [{item.get('source')}] [{when}] {item.get('title')}"
            )
        fingerprint = hashlib.sha1(
            f"{model}|{symbol}|{'|'.join(item.get('id', '') for item in candidates)}".encode("utf-8")
        ).hexdigest()
        now = _utc_now()
        with _cache_lock:
            cached = _digest_cache.get(fingerprint)
        if cached and now - cached["created_at"] <= timedelta(minutes=10):
            return str(cached["digest"])

        prompt = (
            f"你是加密交易消息风控编辑。请把下面关于 {symbol} 的候选消息压缩为 3-6 条中文要点，"
            "按对未来24小时至14天价格影响排序。必须优先保留美债发行/回购、收益率、TGA、美元流动性、"
            "央行政策、ETF资金流、监管与重大安全事件。只根据标题和时间陈述，区分事实与可能影响，"
            "不要编造数字或因果。总长度不超过500字。\n\n" + "\n".join(input_lines)
        )
        llm = build_chat_model(
            model=model,
            api_key=api_key,
            base_url=api_base or None,
            temperature=0.1,
            thinking_enabled=False,
        )
        response = invoke_with_retry(
            lambda: llm.invoke([HumanMessage(content=prompt)]),
            logger=logger,
            context=f"news digest model={model} symbol={symbol}",
        )
        digest = extract_message_text(response).strip()[:2000]
        if not digest:
            return ""
        with _cache_lock:
            expired = [
                key
                for key, value in _digest_cache.items()
                if now - value["created_at"] > timedelta(minutes=10)
            ]
            for key in expired:
                _digest_cache.pop(key, None)
            _digest_cache[fingerprint] = {"created_at": now, "digest": digest}
        try:
            usage = response.response_metadata.get("token_usage", {})
            if usage:
                save_token_usage(
                    symbol=symbol,
                    config_id="news-intelligence",
                    model=model,
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                )
        except Exception as exc:
            logger.debug("News digest token accounting failed: %s", exc)
        return digest
    except Exception as exc:
        logger.warning("News digest compression failed; using structured headlines: %s", exc)
        return ""


def _select_calendar(items: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    horizon = now + timedelta(days=7)
    unique = {}
    for item in items:
        scheduled = _parse_iso(item.get("scheduled_at"))
        if not scheduled or scheduled < now - timedelta(hours=2) or scheduled > horizon:
            continue
        key = (_normalize_title(item.get("title", "")), scheduled.strftime("%Y-%m-%d"))
        unique[key] = item
    return sorted(unique.values(), key=lambda item: item.get("scheduled_at") or "")[:2]


def _display_title(item: dict[str, Any]) -> str:
    if item.get("category") != "macro_calendar":
        return item.get("title", "")
    scheduled = _parse_iso(item.get("scheduled_at"))
    when = scheduled.astimezone(EASTERN).strftime("%b %d %H:%M ET") if scheduled else "TBA"
    return f"{item.get('title', '')} — {when}"


def fetch_news_risk_context(symbol: str, limit: int = 10, timeout: float = 8.0) -> dict:
    if str(os.getenv("NEWS_RISK_ENABLED", "true")).lower() in {"0", "false", "no"}:
        return {}

    now = _utc_now()
    max_items = min(12, max(1, int(os.getenv("NEWS_MAX_ITEMS", str(limit or 10)))))
    source_specs: dict[str, tuple[Callable[[], list[dict[str, Any]]], timedelta, timedelta]] = {
        "calendar_bls": (lambda: _fetch_bls_calendar(timeout), timedelta(hours=6), timedelta(hours=72)),
        "calendar_bea": (lambda: _fetch_bea_calendar(timeout), timedelta(hours=6), timedelta(hours=72)),
        "calendar_fomc": (lambda: _fetch_fomc_calendar(timeout, now), timedelta(hours=6), timedelta(hours=72)),
    }
    if str(os.getenv("CRYPTOCURRENCY_CV_ENABLED", "true")).lower() in {"1", "true", "yes"}:
        cv_categories = [
            item.strip().lower()
            for item in os.getenv("NEWS_CV_CATEGORIES", "general,macro,institutional,etf").split(",")
            if item.strip()
        ]
        for category in cv_categories:
            source_specs[f"crypto_api:{symbol}:{category}"] = (
                lambda selected_category=category: _fetch_cryptocurrency_cv(symbol, timeout, selected_category),
                timedelta(minutes=10),
                timedelta(hours=12),
            )
    crypto_sources = [item.strip() for item in os.getenv("NEWS_RSS_SOURCES", ",".join(DEFAULT_RSS_SOURCES)).split(",") if item.strip()]
    for index, url in enumerate(crypto_sources):
        source_specs[f"crypto_rss_{index + 1}"] = (
            lambda source_url=url, source_index=index: _fetch_feed_url(source_url, f"crypto_rss_{source_index + 1}", "crypto", 0.75, timeout),
            timedelta(minutes=10),
            timedelta(hours=6),
        )
    geopolitical_sources = [item.strip() for item in os.getenv("NEWS_MACRO_RSS_SOURCES", ",".join(DEFAULT_GEOPOLITICAL_SOURCES)).split(",") if item.strip()]
    for index, url in enumerate(geopolitical_sources):
        source_specs[f"geopolitical_{index + 1}"] = (
            lambda source_url=url, source_index=index: _fetch_feed_url(source_url, f"macro_rss_{source_index + 1}", "geopolitical", 0.65, timeout),
            timedelta(minutes=10),
            timedelta(hours=6),
        )
    for source_key, default_url in DEFAULT_POLICY_SOURCES.items():
        url = os.getenv(f"NEWS_{source_key.upper()}_URL", default_url)
        source_specs[f"policy_{source_key}"] = (lambda key=source_key, source_url=url: _fetch_policy_source(key, source_url, timeout), timedelta(minutes=10), timedelta(hours=6))
    source_specs["policy_us_treasury"] = (
        lambda: _fetch_treasury_market_news(timeout),
        timedelta(minutes=15),
        timedelta(hours=48),
    )

    worker_count = max(1, min(16, int(os.getenv("NEWS_SOURCE_WORKERS", "16")), len(source_specs)))
    executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="news-intel")
    futures = {
        executor.submit(_fetch_cached, key, spec[0], ttl=spec[1], stale_ttl=spec[2], now=now): key
        for key, spec in source_specs.items()
    }
    done, pending = wait(futures, timeout=float(os.getenv("NEWS_TOTAL_TIMEOUT_SECONDS", "10")))
    results: dict[str, dict[str, Any]] = {}
    for future in done:
        key = futures[future]
        try:
            results[key] = future.result()
        except Exception as exc:
            results[key] = {"items": [], "status": "unavailable", "stale": False, "error": str(exc)}
    for future in pending:
        results[futures[future]] = {"items": [], "status": "timeout", "stale": False, "error": "source deadline exceeded"}
        future.cancel()
    executor.shutdown(wait=False, cancel_futures=True)

    calendar_items = []
    news_items = []
    for key, result in results.items():
        if key.startswith("calendar_"):
            calendar_items.extend(result.get("items") or [])
        else:
            news_items.extend(result.get("items") or [])

    events = _select_calendar(calendar_items, now)
    classified = _classify_news(news_items, symbol, now)
    critical = [item for item in classified if item.get("category") == "critical"][:2]
    crypto = [item for item in classified if item.get("category") == "crypto"][:3]
    policy = [item for item in classified if item.get("category") in {"policy", "macro_policy", "macro_market"}][:3]
    if not critical:
        critical = [item for item in classified if item.get("category") == "geopolitical"][:1]
    geopolitical = [item for item in classified if item.get("category") == "geopolitical"][:1]
    selected = _dedupe_preserve_order([*events, *critical, *policy, *crypto, *geopolitical])
    selected = selected[:max_items]
    digest_candidates = _dedupe([*events, *classified])
    digest = _news_digest(digest_candidates, symbol)
    stale = any(result.get("stale") for result in results.values())
    available_count = sum(1 for result in results.values() if result.get("status") in {"ok", "stale"})
    source_health = {
        key: {
            "status": result.get("status"),
            "stale": bool(result.get("stale")),
            "fetched_at": result.get("fetched_at"),
            "error": result.get("error") if result.get("status") not in {"ok"} else None,
        }
        for key, result in results.items()
    }
    headlines = [_display_title(item) for item in selected]
    crypto_headlines = [_display_title(item) for item in selected if item.get("category") in {"crypto", "critical"}]
    macro_headlines = [_display_title(item) for item in selected if item.get("category") in {"macro_calendar", "macro_policy", "policy", "geopolitical"}]
    payload = {
        "available": bool(available_count),
        "headlines": headlines,
        "digest": digest,
        "crypto_headlines": crypto_headlines,
        "macro_headlines": macro_headlines,
        "events": events,
        "items": selected,
        "as_of": _iso(now),
        "stale": stale,
        "source_health": source_health,
        "source": "official_macro+official_policy+us_treasury+cryptocurrency.cv+crypto_rss",
    }
    if not available_count:
        payload["error"] = "All configured news and macro sources are unavailable"
    return payload
