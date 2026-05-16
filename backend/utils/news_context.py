import json
import os
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from backend.utils.logger import setup_logger


logger = setup_logger("NewsContext")

DEFAULT_RSS_SOURCES = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
]

HIGH_RISK_KEYWORDS = {
    "hack", "exploit", "stolen", "lawsuit", "sec", "cftc", "ban", "halt",
    "outage", "bankruptcy", "liquidation", "etf", "fed", "cpi", "inflation",
    "rate decision", "emergency", "delist", "regulation",
}


def _read_url(url: str, timeout: float = 4.0) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "crypto-agent/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _symbol_terms(symbol: str) -> set[str]:
    base = str(symbol or "").split("/")[0].split(":")[0].upper()
    terms = {base}
    aliases = {
        "BTC": {"bitcoin", "btc"},
        "ETH": {"ethereum", "ether", "eth"},
        "SOL": {"solana", "sol"},
        "BNB": {"binance", "bnb"},
        "XRP": {"ripple", "xrp"},
    }
    terms |= aliases.get(base, {base.lower()})
    return {term.lower() for term in terms if term}


def _parse_rss_items(raw: bytes, symbol: str, limit: int) -> list[str]:
    terms = _symbol_terms(symbol)
    root = ET.fromstring(raw)
    titles = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        title_l = title.lower()
        if any(term in title_l for term in terms) or any(keyword in title_l for keyword in HIGH_RISK_KEYWORDS):
            titles.append(title)
        if len(titles) >= limit:
            break
    return titles


def _fetch_cryptocurrency_cv(symbol: str, limit: int, timeout: float) -> list[str]:
    base = str(symbol or "").split("/")[0].split(":")[0].lower()
    url = f"https://cryptocurrency.cv/api/news?{urllib.parse.urlencode({'symbol': base})}"
    raw = _read_url(url, timeout=timeout)
    payload = json.loads(raw.decode("utf-8", errors="ignore"))
    if isinstance(payload, dict):
        items = payload.get("data") or payload.get("news") or payload.get("items") or []
    else:
        items = payload
    titles = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("headline") or "").strip()
        if title:
            titles.append(title)
        if len(titles) >= limit:
            break
    return titles


def fetch_news_risk_context(symbol: str, limit: int = 5, timeout: float = 4.0) -> dict:
    if str(os.getenv("NEWS_RISK_ENABLED", "true")).lower() in {"0", "false", "no"}:
        return {}

    headlines = []
    try:
        headlines.extend(_fetch_cryptocurrency_cv(symbol, limit=limit, timeout=timeout))
    except Exception as exc:
        logger.debug(f"cryptocurrency.cv news fetch failed: {exc}")

    if len(headlines) < limit:
        sources = [
            item.strip()
            for item in os.getenv("NEWS_RSS_SOURCES", ",".join(DEFAULT_RSS_SOURCES)).split(",")
            if item.strip()
        ]
        for source in sources:
            try:
                headlines.extend(_parse_rss_items(_read_url(source, timeout=timeout), symbol, limit - len(headlines)))
            except Exception as exc:
                logger.debug(f"RSS news fetch failed source={source}: {exc}")
            if len(headlines) >= limit:
                break

    deduped = list(dict.fromkeys(headlines))[:limit]
    risk_score = 0
    for title in deduped:
        title_l = title.lower()
        risk_score += sum(1 for keyword in HIGH_RISK_KEYWORDS if keyword in title_l)

    risk_level = "high" if risk_score >= 2 else ("watch" if risk_score == 1 else "normal")
    return {
        "risk_level": risk_level,
        "headlines": deduped,
        "source": "cryptocurrency.cv/rss",
    }
