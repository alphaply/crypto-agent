import asyncio
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult

from backend.mcp import canonical_tools, formatters, paper_market, resilience, server, service, tools
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


def test_normalize_tool_input_coerces_include_strings_and_integer_strings():
    single = tools.normalize_tool_input('get_market', {'include': ' Ticker '})
    assert single['include'] == ['ticker']

    multi = tools.normalize_tool_input('get_account_state', {'include': ' balances , Positions ， Orders '})
    assert multi['include'] == ['balances', 'positions', 'orders']

    nums = tools.normalize_tool_input('execute_trade_actions', {
        'leverage': '10',
        'limit': '25',
        'offset': '5',
        'contract_version': '3',
    })
    assert nums['leverage'] == 10 and type(nums['leverage']) is int
    assert nums['limit'] == 25 and type(nums['limit']) is int
    assert nums['offset'] == 5 and type(nums['offset']) is int
    assert nums['contract_version'] == 3 and type(nums['contract_version']) is int

    with pytest.raises(ValueError, match='contract_version must be an integer'):
        tools.normalize_tool_input('execute_trade_actions', {'contract_version': 3.0})


def test_formatters_render_concise_chinese_for_all_eight_tools():
    market_text = formatters.render_read_tool_result('get_market', {
        'symbol': 'BTC/USDT:USDT',
        'exchange': 'binance',
        'market_type': 'swap',
        'status': 'complete',
        'observed_at': 1790000000.0,
        'price': 65000.0,
        'bid': 64995.0,
        'ask': 65005.0,
        'change_percentage_24h': 1.5,
        'quote_volume_24h': 5000000.0,
        'timeframe': '1h',
        'candles': [[1700000000000 + i * 3600000, 64000, 66000, 63500, 65000 + i, 10] for i in range(120)],
        'analysis': {'1h': {'price': '65000', 'rsi_analysis': {'rsi': 55.2}, 'macd': {'diff': 10, 'dea': 5, 'hist': 5, 'momentum': 'hist扩大'}}},
        'sentiment': {'funding_rate': 0.0001, 'open_interest': 12345, 'ls_ratio': '1.25'},
    })
    assert '【行情】BTC/USDT:USDT' in market_text
    assert 'K线(1h): 共 120 根' in market_text
    assert '最新收盘 65,119' in market_text
    assert '1700000000000' not in market_text

    acct_text = formatters.render_read_tool_result('get_account_state', {
        'execution_mode': 'paper',
        'user_id': '123456',
        'account_status': 'active',
        'account_epoch': 0,
        'reset_count': 0,
        'completeness': {'valuation': 'complete', 'reason_codes': []},
        'balances': {
            'cash': 1200, 'available_cash': 1000, 'reserved_cash': 100,
            'debt': 200, 'used_margin': 100, 'net_equity': 1100,
            'interest_reserve': {'message': '请预留 0.2 U 付息'},
        },
        'positions': [{
            'symbol': 'BTC/USDT:USDT', 'side': 'LONG', 'leverage': 10,
            'amount': 0.1, 'entry_price': 64000, 'mark_price': 65000,
            'unrealized_pnl': 100, 'margin': 640, 'resource_id': 'pos-abc-123',
        }],
        'orders': [],
        'performance': {'period': 'current_cycle', 'summary': '本轮已结束2笔，盈利1笔，胜率50%。', 'performance': {'realized_net': 50, 'fees': 2, 'interest_paid': 1}, 'current': {'net_equity': 1100}},
    })
    assert '【账户状态】123456' in acct_text
    assert 'position_id=pos-abc-123' in acct_text
    assert '请预留 0.2 U 付息' in acct_text
    assert '当前挂单: 无' in acct_text

    ctx_text = formatters.render_read_tool_result('get_trading_context', {
        'execution_mode': 'paper', 'namespace': 'qqbot', 'bot_user_id': 'bot:qqbot',
        'settings': {'initial_balance': 1000, 'max_leverage': 125, 'fee_rate': 0.001, 'maintenance_margin_rate': 0.005, 'bank_enabled': True, 'loan_limit': 5000, 'daily_interest_rate': 0.01},
        'allowed_actions': ['open', 'close'],
    })
    assert 'bot_user_id: bot:qqbot' in ctx_text

    sym_text = formatters.render_read_tool_result('search_symbols', {
        'symbols': [{'symbol': 'BTC/USDT:USDT', 'market_type': 'swap', 'exchange': 'binance', 'base': 'BTC', 'name': 'BTC'}],
        'total': 1, 'has_more': False, 'status': 'complete',
    })
    assert 'BTC/USDT:USDT (binance · 永续)' in sym_text

    news_text = formatters.render_read_tool_result('get_news', {
        'as_of': '2026-11-24T09:00:00Z', 'status': 'complete', 'stale': False, 'digest': 'BTC 突破关键阻力位。',
    })
    assert '【市场情报】' in news_text and 'BTC 突破关键阻力位。' in news_text

    rec_text = formatters.render_read_tool_result('get_trade_records', {
        'record_type': 'orders', 'has_more': False,
        'items': [{'order_id': 'ord-1', 'symbol': 'BTC/USDT', 'action': 'buy', 'order_type': 'limit', 'state': 'open', 'amount': 0.5, 'price': 60000}],
    })
    assert 'order_id=ord-1' in rec_text

    lb_text = formatters.render_read_tool_result('get_paper_leaderboard', {
        'metric': 'return_pct', 'total': 2, 'status': 'complete',
        'items': [{'rank': 1, 'name': '小金', 'account_type': 'bot', 'return_pct': 12.5, 'pnl': 125}],
        'self': {'rank': 2, 'name': '测试', 'account_type': 'user', 'return_pct': -3.2, 'pnl': -32},
    })
    assert '#1 小金[Bot]' in lb_text and '你的排名:' in lb_text

    cfg_text = formatters.render_read_tool_result('configure_paper_account', {
        'created': True, 'account': {'user_id': '123456', 'cash': 1000},
    })
    assert '【开户成功】账户 123456' in cfg_text


def test_canonical_read_tools_emit_dual_channel_call_tool_result(monkeypatch):
    mcp_server = FastMCP('Dual channel read test')
    tools.register_tools(mcp_server)
    caller = {'id': 'key:paper', 'key_type': 'paper', 'paper_namespace': 'qqbot', 'scopes': ['read', 'trade'], 'profile_ids': []}
    monkeypatch.setattr(service, 'principal', lambda: caller)
    monkeypatch.setattr(
        service,
        'query',
        lambda _caller, resource, **kwargs: {
            'digest': '市场冷静观察中。',
            'status': 'complete',
            'stale': False,
            'as_of': '2026-11-24T09:00:00Z',
        },
    )
    # Direct .fn() remains raw dict for backwards compatibility
    raw = asyncio.run(mcp_server._tool_manager.get_tool('get_news').fn())
    assert isinstance(raw, dict) and raw['digest'] == '市场冷静观察中。'

    # Protocol call_tool returns CallToolResult with Chinese summary + full structuredContent
    result = asyncio.run(mcp_server.call_tool('get_news', {}))
    assert isinstance(result, CallToolResult)
    assert '【市场情报】' in result.content[0].text
    assert '市场冷静观察中。' in result.content[0].text
    assert result.structuredContent['digest'] == '市场冷静观察中。'


def test_market_analysis_and_sentiment_run_concurrently_and_search_caches(monkeypatch):
    paper_market.clear_read_quote_cache()
    paper_market.clear_search_cache()
    now_ms = time.time() * 1000
    client_calls = {'search': 0}

    class MockConcurrentExchange:
        id = 'binanceusdm'
        markets = {
            'BTC/USDT:USDT': {'symbol': 'BTC/USDT:USDT', 'active': True, 'swap': True, 'linear': True, 'base': 'BTC', 'quote': 'USDT'},
        }

        def market(self, symbol):
            return self.markets[symbol]

        def fetch_ticker(self, symbol):
            return {'symbol': symbol, 'last': 65000.0, 'bid': 64995.0, 'ask': 65005.0, 'timestamp': now_ms}

        def fetch_ohlcv(self, symbol, timeframe='1h', limit=120):
            return [[1700000000000 + i * 3600000, 64900.0, 65100.0, 64800.0, 65000.0, 12.0] for i in range(limit)]

    @contextmanager
    def fake_client(name, market_type):
        client_calls['search'] += 1
        yield MockConcurrentExchange()

    barrier = threading.Barrier(2, timeout=2.0)

    def concurrent_analysis(self, symbol, mode='STRATEGY', timeframes=None):
        barrier.wait()
        return {'analysis': {'1h': {'price': '65000'}}}

    def concurrent_sentiment(self, symbol):
        barrier.wait()
        return {'funding_rate': 0.0002, 'open_interest': 1000, '24h_quote_vol': 500000}

    monkeypatch.setattr(paper_market, 'client', fake_client)
    monkeypatch.setattr('backend.mcp.paper.settings', lambda conn=None: {'exchange': 'binance'})
    monkeypatch.setattr(MarketTool, 'get_market_analysis', concurrent_analysis)
    monkeypatch.setattr(MarketTool, '_fetch_market_derivatives', concurrent_sentiment)

    res = paper_market.quote('BTC/USDT:USDT', market_type='swap', candles=False, include=['ticker', 'analysis', 'sentiment'])
    assert res['status'] == 'complete'
    assert res['analysis'] == {'1h': {'price': '65000'}}
    assert res['sentiment']['funding_rate'] == 0.0002

    # search() caches complete results for repeated queries within TTL
    first_search = paper_market.search(keyword='BTC', market_type='swap')
    second_search = paper_market.search(keyword='BTC', market_type='swap')
    assert first_search['total'] == 1 and second_search['total'] == 1
    assert client_calls['search'] == 2  # 1 from quote + 1 from first search (second search hit cache)

    # service.query(..., 'market') also executes multiple facets concurrently
    live_barrier = threading.Barrier(2, timeout=2.0)
    caller = {'id': 'key:live', 'scopes': ['read'], 'profile_ids': ['swap']}
    profile = {'profile_id': 'swap', 'market_type': 'swap', 'symbols': ['BTC/USDT:USDT']}
    monkeypatch.setattr(service, 'require', lambda *args, **kwargs: profile)
    monkeypatch.setattr(service, 'remaining', lambda *args, **kwargs: 30)
    mock_mt = object.__new__(MarketTool)
    mock_mt.exchange = MockConcurrentExchange()
    mock_mt.market_type = 'swap'
    mock_mt.mcp_account_key = 'test-concurrent-live-' + str(time.time())
    mock_mt.get_market_analysis = lambda *args, **kwargs: (live_barrier.wait(), {'analysis': {'1h': {'price': '65000'}}})[1]
    mock_mt._fetch_market_derivatives = lambda *args, **kwargs: (live_barrier.wait(), {'funding_rate': 0.0003})[1]
    monkeypatch.setattr(service, 'market_tool', lambda _: mock_mt)

    live_res = service.query(caller, 'market', profile_id='swap', symbol='BTC/USDT:USDT', include=['analysis', 'sentiment'])
    assert live_res['status'] == 'complete'
    assert live_res['sentiment']['funding_rate'] == 0.0003
