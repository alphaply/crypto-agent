import asyncio
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.mcp import canonical_tools, paper_market, resilience, server, tools
from backend.utils.market_data import MarketTool


def test_normalize_tool_input_coerces_integer_ids_and_normalizes_enums():
    normalized = tools.normalize_tool_input('get_account_state', {
        'args': {
            'user_id': 123456789,
            'recipient_user_id': 987654321,
            'symbol': '  btc/usdt:usdt  ',
            'market_type': ' SWAP ',
            'action': ' Open_Long ',
            'side': ' long ',
            'pos_side': ' short ',
            'order_type': ' LIMIT ',
            'state': ' OPEN ',
            'period': ' 7D ',
        }
    })
    assert normalized['user_id'] == '123456789'
    assert normalized['recipient_user_id'] == '987654321'
    assert normalized['symbol'] == 'btc/usdt:usdt'
    assert normalized['market_type'] == 'swap'
    assert normalized['action'] == 'open_long'
    assert normalized['side'] == 'LONG'
    assert normalized['pos_side'] == 'SHORT'
    assert normalized['order_type'] == 'limit'
    assert normalized['state'] == 'open'
    assert normalized['period'] == '7d'


def test_normalize_tool_input_rejects_boolean_as_numeric_and_preserves_bool_ids():
    with pytest.raises(ValueError, match='amount must be a number'):
        tools.normalize_tool_input('paper_trade', {'amount': True})

    unchanged = tools.normalize_tool_input('get_account_state', {'user_id': True})
    assert unchanged['user_id'] is True


def test_paper_market_error_hints_point_to_search_symbols():
    with pytest.raises(ValueError, match='search_symbols'):
        paper_market.canonical('invalid symbol!!', 'swap')


def test_paper_market_quote_reuses_ohlcv_and_compacts_ticker_and_caches(monkeypatch):
    paper_market.clear_read_quote_cache()
    calls = {'ticker': 0, 'ohlcv': []}
    now_ms = time.time() * 1000

    class MockPublicExchange:
        id = 'binance'
        markets = {'BTC/USDT:USDT': {'symbol': 'BTC/USDT:USDT', 'active': True, 'swap': True, 'linear': True, 'base': 'BTC', 'quote': 'USDT'}}

        def market(self, symbol):
            if symbol not in self.markets:
                raise KeyError(symbol)
            return self.markets[symbol]

        def fetch_ticker(self, symbol):
            calls['ticker'] += 1
            return {
                'symbol': symbol,
                'last': 65000.0,
                'bid': 64995.0,
                'ask': 65005.0,
                'high': 66000.0,
                'low': 64000.0,
                'open': None,
                'close': 65000.0,
                'average': None,
                'vwap': None,
                'percentage': 1.5,
                'quoteVolume': 5000000.0,
                'timestamp': now_ms,
                'info': {'raw': 'ignored'},
            }

        def fetch_ohlcv(self, symbol, timeframe='1h', limit=120):
            calls['ohlcv'].append((timeframe, limit))
            step = 3600000
            return [[1700000000000 + i * step, 64900.0, 65100.0, 64800.0, 65000.0, 12.0] for i in range(min(limit, 300))]

    @contextmanager
    def fake_client(name, market_type):
        yield MockPublicExchange()

    def fake_analysis(self, symbol, mode='STRATEGY', timeframes=None):
        analysis = {}
        for tf in (timeframes or []):
            rows = self.exchange.fetch_ohlcv(symbol, tf, limit=240)
            analysis[tf] = {'price': str(rows[-1][4])}
        return {'analysis': analysis}

    monkeypatch.setattr(paper_market, 'client', fake_client)
    monkeypatch.setattr(MarketTool, 'get_market_analysis', fake_analysis)
    monkeypatch.setattr(MarketTool, '_fetch_market_derivatives', lambda self, symbol: {'funding_rate': 0.0001})

    first = paper_market.resolve_quote('BTC/USDT', market_type='swap', timeframe='15m', limit=50)
    assert first['status'] == 'complete'
    assert len(first['candles']) == 50
    # 15m was fetched once (limit=240 for analysis) and reused for 50 candles instead of fetching 15m twice
    assert calls['ohlcv'].count(('15m', 50)) == 0
    assert calls['ohlcv'].count(('15m', 240)) == 1
    # Compact ticker removes None and unlisted noise fields
    assert 'info' not in first['ticker']
    assert 'open' not in first['ticker']
    assert 'average' not in first['ticker']
    assert first['ticker']['last'] == 65000.0

    # Second read call within cache TTL hits short-lived read cache without hitting exchange
    second = paper_market.resolve_quote('BTC/USDT', market_type='swap', timeframe='15m', limit=50)
    assert second['ticker']['last'] == 65000.0
    assert calls['ticker'] == 1

    # Order execution path calls quote(..., candles=False) directly and never uses read cache
    live_exec = paper_market.quote('BTC/USDT:USDT', market_type='swap', candles=False, include=['ticker'])
    assert live_exec['price'] == 65000.0
    assert calls['ticker'] == 2


def test_tool_catalog_contains_grouped_bilingual_metadata():
    catalog = server.tool_catalog()
    assert len(catalog) == len(canonical_tools.CORE_NAMES) + len(canonical_tools.PAPER_NAMES)
    for item in catalog:
        assert item['category'] in {'shared', 'trading', 'paper'}
        assert item['title_zh'] and item['title_en']
        assert item['description_zh'] and item['description_en']
        assert item['scope'] in {'read', 'trade', 'trade / cancel'}


def test_canonical_scope_error_messages_are_actionable(monkeypatch):
    from backend.mcp import paper, service
    monkeypatch.setattr(paper, 'require', lambda *args, **kwargs: 'qqbot')
    monkeypatch.setattr(service, 'require', lambda *args, **kwargs: {})
    monkeypatch.setattr(service, 'list_profiles', lambda *args, **kwargs: [])
    paper_caller = {'id': 'key:p', 'key_type': 'paper', 'paper_namespace': 'qqbot', 'scopes': ['read']}
    live_caller = {'id': 'key:l', 'key_type': 'live', 'profile_ids': ['swap'], 'scopes': ['read']}
    with pytest.raises(ValueError, match='get_trading_context'):
        canonical_tools.context(live_caller, user_id='123')
    with pytest.raises(ValueError, match='authenticated QQ sender ID'):
        canonical_tools._scope(paper_caller, profile_id='swap', user_id=None)
    with pytest.raises(ValueError, match='get_trading_context'):
        canonical_tools._scope(live_caller, profile_id=None, user_id='123')


def test_resilience_timeout_messages_include_retry_guidance(monkeypatch):
    monkeypatch.setenv('MCP_READ_TIMEOUT_SECONDS', '0.02')
    pool = resilience.WorkerPool('read', 1, 0)
    try:
        with pytest.raises(resilience.BudgetExceeded, match='narrow get_market include facets'):
            asyncio.run(pool.run(lambda: time.sleep(0.08)))
    finally:
        pool.executor.shutdown()
