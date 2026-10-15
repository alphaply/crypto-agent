"""Real public market discovery and quotes for the simulated execution ledger."""
from __future__ import annotations

import math
import re
import time
from contextlib import contextmanager

EXCHANGES = ('binance',)
# Discovery hints verified against issuer/exchange announcements. They never
# create tradable markets: a live exchange catalogue must confirm every symbol.
ASSETS = {
    'MUB': {'name': 'Micron Technology bStocks', 'underlying_symbol': 'MU', 'asset_type': 'tokenized_stock',
            'aliases': ['MU', 'MICRON', '美光', '美光科技'],
            'reference': 'https://www.binance.com/en/square/post/340313977073537'},
    'NVDAB': {'name': 'NVIDIA bStocks', 'underlying_symbol': 'NVDA', 'asset_type': 'tokenized_stock', 'aliases': ['NVDA', 'NVIDIA', '英伟达']},
    'TSLAB': {'name': 'Tesla bStocks', 'underlying_symbol': 'TSLA', 'asset_type': 'tokenized_stock', 'aliases': ['TSLA', 'TESLA', '特斯拉']},
    'CRCLB': {'name': 'Circle bStocks', 'underlying_symbol': 'CRCL', 'asset_type': 'tokenized_stock', 'aliases': ['CRCL', 'CIRCLE']},
    'SNDKB': {'name': 'Sandisk bStocks', 'underlying_symbol': 'SNDK', 'asset_type': 'tokenized_stock', 'aliases': ['SNDK', 'SANDISK', '闪迪']},
}
VENUE_ASSETS = {}


@contextmanager
def client(name, market_type):
    from .paper_policy import venue
    name = venue(name)
    if name not in EXCHANGES or market_type not in {'spot', 'swap'}:
        raise ValueError('Unsupported public exchange or market type')
    import ccxt
    from .exchange_runtime import account_key, install_transport, load_catalogue, close_runtime_client
    cls = ccxt.binanceusdm if name == 'binance' and market_type == 'swap' else getattr(ccxt, name)
    options = {'defaultType': market_type, 'fetchCurrencies': False}
    if name == 'binance':
        options['fetchMarkets'] = {'types': ['spot'] if market_type == 'spot' else ['linear']}
    exchange = cls({'enableRateLimit': True, 'timeout': 8000, 'options': options})
    if name == 'binance' and market_type == 'spot' and getattr(exchange, 'urls', None):
        # Binance's documented production endpoint for anonymous market data.
        # This domain exposes no signed account/order APIs.
        exchange.urls['api']['public'] = 'https://data-api.binance.vision/api/v3'
    key = account_key(name, market_type, ['public-paper-v3'])
    install_transport(exchange, key)
    try:
        load_catalogue(exchange, key)
        yield exchange
    finally:
        close_runtime_client(exchange)


def supported(item, market_type):
    return (item.get('active') is not False and item.get('quote') in {None, 'USDT'}
            and (item.get('spot') is True if market_type == 'spot' else item.get('swap') is True and item.get('linear') is True))


def canonical(symbol, market_type):
    raw = str(symbol or '').strip().upper()
    if re.fullmatch(r'[A-Z0-9]{1,25}', raw):
        raw = raw[:-4] if raw.endswith('USDT') and len(raw) > 4 else raw
        raw += '/USDT'
    if not re.fullmatch(r'[A-Z0-9]{1,25}/USDT(?::USDT)?', raw):
        raise ValueError('Use a base ticker, BASEUSDT or BASE/USDT[:USDT]; discover unknown names with search_paper_symbols')
    return raw.split(':')[0] + (':USDT' if market_type == 'swap' else '')


def asset(item, exchange_name=None, market_type=None):
    base = item.get('base') or item['symbol'].split('/')[0]
    hint = VENUE_ASSETS.get((exchange_name, market_type, base), ASSETS.get(base, {}))
    return {'name': hint.get('name', base), 'asset_type': hint.get('asset_type', 'exchange_market'),
            'underlying_symbol': hint.get('underlying_symbol'), 'reference': hint.get('reference'),
            'base': base, 'quote': item.get('quote') or 'USDT', 'contract_size': item.get('contractSize') or 1}


def search(keyword='', market_type=None, exchange_name=None, limit=50, offset=0):
    from .paper import settings
    if not isinstance(keyword, str) or len(keyword) > 100 or type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
        raise ValueError('Invalid symbol search or pagination')
    if market_type not in {None, 'spot', 'swap'}:
        raise ValueError('market_type must be spot or swap')
    from .paper_policy import venue
    preferred = venue(settings()['exchange'])
    names = [venue(exchange_name)]
    if any(name not in EXCHANGES for name in names):
        raise ValueError('Unsupported public exchange')
    needle = keyword.strip().upper()

    def fetch_venue(name):
        matches, missing = [], []
        for kind in ([market_type] if market_type else ['spot', 'swap']):
            try:
                with client(name, kind) as exchange:
                    for item in exchange.markets.values():
                        if not supported(item, kind) or not item.get('symbol') or '/USDT' not in item['symbol']:
                            continue
                        details = asset(item, name, kind)
                        hint = VENUE_ASSETS.get((name, kind, details['base']), ASSETS.get(details['base'], {}))
                        searchable = [item['symbol'], details['name'], details['underlying_symbol'] or '', *hint.get('aliases', [])]
                        if needle and needle != str(item.get('id', '')).upper() and not any(needle in str(v).upper() for v in searchable):
                            continue
                        matches.append({'symbol': item['symbol'], 'market_type': kind, 'exchange': name, **details,
                                        'active': True, 'catalogue_verified': True})
            except Exception as exc:
                missing.append({'exchange': name, 'market_type': kind, 'error': type(exc).__name__})
        return matches, missing

    matches, missing = [], []
    for name in names:
        found, failed = fetch_venue(name)
        matches.extend(found)
        missing.extend(failed)
    matches.sort(key=lambda r: (r['base'] != needle, r['exchange'] != preferred, r['market_type'], r['symbol'], r['exchange']))
    hints = [{**details, 'base': base, 'catalogue_verified': False, 'hint_only': True}
             for base, details in ASSETS.items() if needle and any(needle in str(value).upper() for value in [base, details['name'], details.get('underlying_symbol', ''), *details.get('aliases', [])])]
    return {'symbols': matches[offset:offset + limit], 'total': len(matches), 'limit': limit, 'offset': offset,
            'has_more': offset + limit < len(matches), 'status': 'partial' if missing else 'complete',
            'missing_sources': missing, 'asset_hints': hints, 'queried_exchanges': names,
            'market_data_mode': 'live_public', 'observed_at': time.time(),
            'note': 'Prices/listings come from real public exchange APIs. A failed venue is not evidence of nonexistence. Asset hints are not tradability proof.'}


def quote(symbol, market_type='swap', timeframe='1h', limit=120, *, candles=True, exchange_name=None):
    from .paper import money, number, settings
    if timeframe not in {'1m', '5m', '15m', '1h', '4h', '1d', '1w'} or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('Unsupported timeframe or candle limit')
    from .paper_policy import venue
    name = venue(exchange_name)
    wanted = canonical(symbol, market_type)
    with client(name, market_type) as exchange:
        try:
            item = exchange.market(wanted)
        except Exception:
            raise ValueError('Market not found on this exchange; use search_paper_symbols before concluding the asset does not exist') from None
        if not supported(item, market_type):
            raise ValueError('Market is inactive or not a USDT spot/linear perpetual')
        ticker = exchange.fetch_ticker(item['symbol'])
        stamp = ticker.get('timestamp')
        if stamp is not None and (isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or not -5000 <= time.time() * 1000 - stamp <= 120000):
            raise ValueError('Public ticker is stale; no fill is allowed')
        last = number(ticker.get('last') or ticker.get('close'), positive=True)
        bid, ask = ticker.get('bid'), ticker.get('ask')
        bid_ask_source = 'exchange_ticker'
        if not bid or not ask:
            book = exchange.fetch_order_book(item['symbol'], limit=5)
            bid, ask = book['bids'][0][0], book['asks'][0][0]
            bid_ask_source = 'exchange_order_book'
        bid, ask = money(number(bid, positive=True)), money(number(ask, positive=True))
        if min(bid, ask, money(last)) <= 0 or bid > ask:
            raise ValueError('Public bid/ask is invalid; no fill is allowed')
        result = {'exchange': name, 'symbol': item['symbol'], 'market_type': market_type, **asset(item, name, market_type),
                  'market_data_mode': 'live_public', 'execution_mode': 'paper', 'market_data_is_simulated': False,
                  'source': f'{name} public production API', 'source_environment': 'production',
                  'price': money(last), 'bid': bid, 'ask': ask, 'bid_ask_source': bid_ask_source,
                  'change_percentage_24h': ticker.get('percentage'), 'quote_volume_24h': ticker.get('quoteVolume'),
                  'observed_at': time.time(), 'ticker_timestamp': stamp, 'timestamp_unit': 'milliseconds',
                  'observed_at_unit': 'seconds', 'ticker_timestamp_unit': 'milliseconds',
                  'display_timezone': 'Asia/Shanghai',
                  'note': 'Prices are real exchange public quotes. Only balances, orders and execution are simulated.'}
        if candles:
            result.update(timeframe=timeframe, candles=exchange.fetch_ohlcv(item['symbol'], timeframe, limit=limit))
        return result


def resolve_quote(symbol, market_type='swap', timeframe='1h', limit=120, exchange_name=None):
    """The trusted Paper policy permits no implicit or explicit venue fallback."""
    from .paper_policy import venue
    selected = venue(exchange_name)
    return quote(canonical(symbol, market_type), market_type, timeframe, limit, exchange_name=selected)
