import pytest
from unittest.mock import Mock, MagicMock
from backend.mcp import canonical_tools, paper_market, service
from backend.utils.market_data import MarketTool


class MockExchange:
    def __init__(self, market_type='swap'):
        self.id = 'binance'
        self.market_type = market_type
        self.markets = {
            'BTC/USDT:USDT': {'symbol': 'BTC/USDT:USDT', 'active': True, 'swap': True, 'linear': True},
            'BTC/USDT': {'symbol': 'BTC/USDT', 'active': True, 'spot': True},
        }

    def market(self, symbol):
        return self.markets.get(symbol, {'symbol': symbol, 'active': True, 'swap': self.market_type == 'swap', 'spot': self.market_type == 'spot'})

    def fetch_ticker(self, symbol):
        return {
            'symbol': symbol,
            'last': 65000.0,
            'bid': 64990.0,
            'ask': 65010.0,
            'percentage': 2.5,
            'quoteVolume': 1000000.0,
            'info': {'secret': 'hidden'},
        }

    def fetch_ohlcv(self, symbol, timeframe='1h', limit=120):
        # 120 mock candles: [timestamp, open, high, low, close, volume]
        now = 1700000000000
        step = 3600000
        return [[now + i * step, 65000.0, 65500.0, 64500.0, 65100.0, 10.0] for i in range(min(limit, 120))]


def test_live_market_query_default_enriched(monkeypatch):
    caller = {'id': 'key:test', 'scopes': ['read'], 'profile_ids': ['swap']}
    profile = {'profile_id': 'swap', 'market_type': 'swap', 'symbols': ['BTC/USDT:USDT']}
    monkeypatch.setattr(service, 'require', lambda *args, **kwargs: profile)
    monkeypatch.setattr(service, 'remaining', lambda *args, **kwargs: 30)

    exchange = MockExchange(market_type='swap')
    mock_mt = object.__new__(MarketTool)
    mock_mt.exchange = exchange
    mock_mt.market_type = 'swap'
    mock_mt.get_market_analysis = Mock(return_value={
        'analysis': {
            '1h': {'price': '65000', 'rsi_analysis': {'rsi': 55.0}},
            '4h': {'price': '65000', 'rsi_analysis': {'rsi': 60.0}},
            '1d': {'price': '65000', 'rsi_analysis': {'rsi': 62.0}},
        }
    })
    mock_mt._fetch_market_derivatives = Mock(return_value={
        'funding_rate': 0.0001,
        'open_interest': 50000.0,
        'ls_ratio': 1.5,
    })
    monkeypatch.setattr(service, 'market_tool', lambda _: mock_mt)

    result = service.query(caller, 'market', profile_id='swap', symbol='BTC/USDT:USDT')

    assert result['symbol'] == 'BTC/USDT:USDT'
    assert result['status'] == 'complete'
    assert 'ticker' in result and result['ticker']['last'] == 65000.0
    assert 'candles' in result and len(result['candles']) > 0
    assert 'analysis' in result and '1h' in result['analysis'] and '4h' in result['analysis'] and '1d' in result['analysis']
    assert 'sentiment' in result and result['sentiment']['funding_rate'] == 0.0001
    assert result['sentiment']['ls_ratio'] == 1.5
    assert mock_mt.get_market_analysis.called
    assert mock_mt._fetch_market_derivatives.called


def test_live_market_query_include_pruning(monkeypatch):
    caller = {'id': 'key:test', 'scopes': ['read'], 'profile_ids': ['swap']}
    profile = {'profile_id': 'swap', 'market_type': 'swap', 'symbols': ['BTC/USDT:USDT']}
    monkeypatch.setattr(service, 'require', lambda *args, **kwargs: profile)
    monkeypatch.setattr(service, 'remaining', lambda *args, **kwargs: 30)

    exchange = MockExchange(market_type='swap')
    mock_mt = object.__new__(MarketTool)
    mock_mt.exchange = exchange
    mock_mt.market_type = 'swap'
    mock_mt.get_market_analysis = Mock()
    mock_mt._fetch_market_derivatives = Mock(return_value={'funding_rate': 0.0001})
    monkeypatch.setattr(service, 'market_tool', lambda _: mock_mt)

    # Only request ticker and sentiment
    result = service.query(caller, 'market', profile_id='swap', symbol='BTC/USDT:USDT', include=['ticker', 'sentiment'])

    assert 'ticker' in result
    assert 'sentiment' in result
    assert 'candles' not in result
    assert 'analysis' not in result
    # get_market_analysis should NOT have been called since analysis was not included
    mock_mt.get_market_analysis.assert_not_called()


def test_live_market_query_timeframe_merging(monkeypatch):
    caller = {'id': 'key:test', 'scopes': ['read'], 'profile_ids': ['swap']}
    profile = {'profile_id': 'swap', 'market_type': 'swap', 'symbols': ['BTC/USDT:USDT']}
    monkeypatch.setattr(service, 'require', lambda *args, **kwargs: profile)
    monkeypatch.setattr(service, 'remaining', lambda *args, **kwargs: 30)

    exchange = MockExchange(market_type='swap')
    mock_mt = object.__new__(MarketTool)
    mock_mt.exchange = exchange
    mock_mt.market_type = 'swap'
    mock_mt.get_market_analysis = Mock(return_value={'analysis': {}})
    mock_mt._fetch_market_derivatives = Mock(return_value={})
    monkeypatch.setattr(service, 'market_tool', lambda _: mock_mt)

    service.query(caller, 'market', profile_id='swap', symbol='BTC/USDT:USDT', timeframe='15m')

    # Verify that '15m' was merged into the timeframes list along with ['1h', '4h', '1d']
    called_timeframes = mock_mt.get_market_analysis.call_args.kwargs['timeframes']
    assert '15m' in called_timeframes
    assert '1h' in called_timeframes
    assert '4h' in called_timeframes
    assert '1d' in called_timeframes


def test_live_market_spot_clears_sentiment(monkeypatch):
    caller = {'id': 'key:test', 'scopes': ['read'], 'profile_ids': ['spot']}
    profile = {'profile_id': 'spot', 'market_type': 'spot', 'symbols': ['BTC/USDT']}
    monkeypatch.setattr(service, 'require', lambda *args, **kwargs: profile)
    monkeypatch.setattr(service, 'remaining', lambda *args, **kwargs: 30)

    exchange = MockExchange(market_type='spot')
    mock_mt = object.__new__(MarketTool)
    mock_mt.exchange = exchange
    mock_mt.market_type = 'spot'
    mock_mt.get_market_analysis = Mock(return_value={'analysis': {'1h': {'price': '65000'}}})
    mock_mt._fetch_market_derivatives = Mock()
    monkeypatch.setattr(service, 'market_tool', lambda _: mock_mt)

    result = service.query(caller, 'market', profile_id='spot', symbol='BTC/USDT')

    assert result['sentiment'] == {}
    # Derivative fetcher should not be called for spot
    mock_mt._fetch_market_derivatives.assert_not_called()


def test_paper_market_default_enrichment(monkeypatch):
    from contextlib import contextmanager

    exchange = MockExchange(market_type='swap')
    exchange.fetch_order_book = Mock(return_value={'bids': [[64990.0, 1.0]], 'asks': [[65010.0, 1.0]]})

    @contextmanager
    def fake_client(name, market_type):
        yield exchange

    monkeypatch.setattr(paper_market, 'client', fake_client)
    monkeypatch.setattr(paper_market, 'supported', lambda item, market_type: True)

    # Mock MarketTool methods
    mock_analysis = {'1h': {'price': '65000', 'macd': {}}, '4h': {}, '1d': {}}
    mock_sentiment = {'funding_rate': 0.0001, 'ls_ratio': 1.6}
    monkeypatch.setattr(MarketTool, 'get_market_analysis', lambda self, symbol, mode, timeframes: {'analysis': mock_analysis})
    monkeypatch.setattr(MarketTool, '_fetch_market_derivatives', lambda self, symbol: mock_sentiment)

    result = paper_market.resolve_quote('BTC/USDT', market_type='swap', timeframe='1h')

    assert result['status'] == 'complete'
    assert 'ticker' in result and result['ticker']['last'] == 65000.0
    assert 'candles' in result
    assert result['analysis'] == mock_analysis
    assert result['sentiment'] == mock_sentiment


def test_paper_market_include_and_spot_isolation(monkeypatch):
    from contextlib import contextmanager

    exchange = MockExchange(market_type='spot')
    exchange.fetch_order_book = Mock(return_value={'bids': [[64990.0, 1.0]], 'asks': [[65010.0, 1.0]]})

    @contextmanager
    def fake_client(name, market_type):
        yield exchange

    monkeypatch.setattr(paper_market, 'client', fake_client)
    monkeypatch.setattr(paper_market, 'supported', lambda item, market_type: True)
    monkeypatch.setattr(MarketTool, 'get_market_analysis', lambda self, symbol, mode, timeframes: {'analysis': {'1h': {}}})

    # Test include filter
    pruned = paper_market.resolve_quote('BTC/USDT', market_type='spot', include=['analysis', 'sentiment'])
    assert 'analysis' in pruned
    assert pruned['sentiment'] == {}  # Spot market has empty sentiment
    assert 'candles' not in pruned
    assert 'ticker' not in pruned


@pytest.fixture(autouse=True)
def clean_runtime():
    from backend.mcp.exchange_runtime import retire_runtime_caches
    retire_runtime_caches(all_idle=True)
    yield
    retire_runtime_caches(all_idle=True)


def test_live_market_partial_failure(monkeypatch):
    caller = {'id': 'key:test', 'scopes': ['read'], 'profile_ids': ['swap']}
    profile = {'profile_id': 'swap', 'market_type': 'swap', 'symbols': ['BTC/USDT:USDT']}
    monkeypatch.setattr(service, 'require', lambda *args, **kwargs: profile)
    monkeypatch.setattr(service, 'remaining', lambda *args, **kwargs: 30)

    exchange = MockExchange(market_type='swap')
    mock_mt = object.__new__(MarketTool)
    mock_mt.exchange = exchange
    mock_mt.market_type = 'swap'
    mock_mt.get_market_analysis = Mock(return_value={'analysis': {'1h': {'price': '65000'}}})
    mock_mt._fetch_market_derivatives = Mock(side_effect=RuntimeError('Derivatives API timed out'))
    monkeypatch.setattr(service, 'market_tool', lambda _: mock_mt)

    result = service.query(caller, 'market', profile_id='swap', symbol='BTC/USDT:USDT')

    assert result['status'] == 'partial'
    assert 'sentiment' in result['missing_sources']
    assert 'ticker' in result and result['ticker']['last'] == 65000.0
    assert 'candles' in result
    assert 'analysis' in result


def test_invalid_include_facet_rejected(monkeypatch):
    caller = {'id': 'key:test', 'scopes': ['read'], 'profile_ids': ['swap']}
    profile = {'profile_id': 'swap', 'market_type': 'swap', 'symbols': ['BTC/USDT:USDT']}
    monkeypatch.setattr(service, 'require', lambda *args, **kwargs: profile)
    monkeypatch.setattr(service, 'remaining', lambda *args, **kwargs: 30)

    exchange = MockExchange(market_type='swap')
    mock_mt = object.__new__(MarketTool)
    mock_mt.exchange = exchange
    mock_mt.market_type = 'swap'
    monkeypatch.setattr(service, 'market_tool', lambda _: mock_mt)

    with pytest.raises(ValueError, match='Unsupported market facet'):
        service.query(caller, 'market', profile_id='swap', symbol='BTC/USDT:USDT', include=['invalid_facet'])


def test_fastmcp_get_market_schema(monkeypatch):
    import asyncio
    from mcp.server.fastmcp import FastMCP
    from backend.mcp.tools import register_tools
    from backend.mcp.paper_tools import register_paper_tools

    caller = {'id': 'key:test', 'key_type': 'live', 'scopes': ['read'], 'profile_ids': ['swap']}
    monkeypatch.setattr(service, 'principal', lambda: caller)
    monkeypatch.setattr(service, 'require', lambda *args, **kwargs: {'enabled': True, 'symbols': ['BTC/USDT:USDT']})
    monkeypatch.setattr(service, 'list_profiles', lambda *args, **kwargs: [{'profile_id': 'swap'}])
    monkeypatch.setattr(canonical_tools, 'catalog_names', lambda *args, **kwargs: {'get_market'})

    server = FastMCP('Test Server')
    register_tools(server)
    canonical_tools.register(server)

    tools = asyncio.run(server.list_tools())
    get_market_tool = next(t for t in tools if t.name == 'get_market')
    props = get_market_tool.inputSchema['properties']

    assert 'symbol' in props
    assert 'timeframe' in props
    assert 'include' in props
    include_prop = props['include']
    if 'anyOf' in include_prop:
        array_def = next(branch for branch in include_prop['anyOf'] if branch.get('type') == 'array')
        include_items = array_def.get('items', {})
    else:
        include_items = include_prop.get('items', {})
    expected_facets = {'ticker', 'candles', 'analysis', 'sentiment'}
    assert set(include_items.get('enum', [])) == expected_facets

