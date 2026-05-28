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

DEFAULT_MACRO_RSS_SOURCES = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://www.reutersagency.com/feed/?best-topics=political-general&post_type=best",
]

HIGH_RISK_KEYWORDS = {
    "hack", "exploit", "stolen", "lawsuit", "sec", "cftc", "ban", "halt",
    "outage", "bankruptcy", "liquidation", "etf", "fed", "cpi", "inflation",
    "rate decision", "emergency", "delist", "regulation", "war", "sanction",
    "tariff", "trump", "election", "middle east", "china", "russia",
    "ukraine", "oil", "geopolitical",
}

MACRO_KEYWORDS = {
    "fed", "federal reserve", "cpi", "inflation", "rate", "rates", "dollar",
    "treasury", "tariff", "sanction", "trump", "election", "white house",
    "war", "middle east", "israel", "iran", "china", "russia", "ukraine",
    "oil", "opec", "geopolitical",
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


def _parse_rss_items(raw: bytes, symbol: str, limit: int, *, include_symbol_terms: bool = True, keywords: set[str] | None = None) -> list[str]:
    terms = _symbol_terms(symbol)
    keywords = keywords or HIGH_RISK_KEYWORDS
    root = ET.fromstring(raw)
    titles = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        title_l = title.lower()
        symbol_match = include_symbol_terms and any(term in title_l for term in terms)
        keyword_match = any(keyword in title_l for keyword in keywords)
        if symbol_match or keyword_match:
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

    crypto_limit = min(3, max(1, limit))
    macro_limit = max(0, limit - crypto_limit)
    crypto_headlines = []
    macro_headlines = []
    try:
        crypto_headlines.extend(_fetch_cryptocurrency_cv(symbol, limit=crypto_limit, timeout=timeout))
    except Exception as exc:
        logger.debug(f"cryptocurrency.cv news fetch failed: {exc}")

    if len(crypto_headlines) < crypto_limit:
        sources = [
            item.strip()
            for item in os.getenv("NEWS_RSS_SOURCES", ",".join(DEFAULT_RSS_SOURCES)).split(",")
            if item.strip()
        ]
        for source in sources:
            try:
                crypto_headlines.extend(_parse_rss_items(_read_url(source, timeout=timeout), symbol, crypto_limit - len(crypto_headlines)))
            except Exception as exc:
                logger.debug(f"RSS news fetch failed source={source}: {exc}")
            if len(crypto_headlines) >= crypto_limit:
                break

    if macro_limit > 0:
        sources = [
            item.strip()
            for item in os.getenv("NEWS_MACRO_RSS_SOURCES", ",".join(DEFAULT_MACRO_RSS_SOURCES)).split(",")
            if item.strip()
        ]
        for source in sources:
            try:
                macro_headlines.extend(
                    _parse_rss_items(
                        _read_url(source, timeout=timeout),
                        symbol,
                        macro_limit - len(macro_headlines),
                        include_symbol_terms=False,
                        keywords=MACRO_KEYWORDS,
                    )
                )
            except Exception as exc:
                logger.debug(f"Macro RSS news fetch failed source={source}: {exc}")
            if len(macro_headlines) >= macro_limit:
                break

    deduped = list(dict.fromkeys([*crypto_headlines[:crypto_limit], *macro_headlines[:macro_limit]]))[:limit]
    risk_score = 0
    for title in deduped:
        title_l = title.lower()
        risk_score += sum(1 for keyword in HIGH_RISK_KEYWORDS if keyword in title_l)

    risk_level = "high" if risk_score >= 2 else ("watch" if risk_score == 1 else "normal")
    return {
        "risk_level": risk_level,
        "headlines": deduped,
        "crypto_headlines": crypto_headlines[:crypto_limit],
        "macro_headlines": macro_headlines[:macro_limit],
        "source": "cryptocurrency.cv/rss+macro_rss",
    }
