import asyncio
import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import database
from backend.mcp import auth, paper, service, store
from backend.mcp.server import install_mcp, mcp_lifespan
PUBLIC_MARKET = paper.market


@pytest.fixture
def ledger(tmp_path, monkeypatch, financial_pg):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'paper.db'))
    monkeypatch.setenv('CONFIG_MASTER_KEY', 'paper-tests-only-master-key')
    with database.get_db_conn() as conn:
        store.initialize_schema(conn)
        store.initialize_schema(conn)
        conn.commit()
    store.put('settings', 'global', {'enabled': True, 'public_url': 'http://localhost:7860'})
    store.put('paper_settings', 'global', {'initial_balance': 1000, 'loan_limit': 5000, 'fee_rate': .001})
    ticker = {'price': 100, 'bid': 100, 'ask': 100, 'exchange': 'binance'}
    calls = []

    def quote(symbol, market_type='swap', *args, **kwargs):
        calls.append((symbol, market_type))
        return {**ticker, 'symbol': symbol, 'market_type': market_type, 'observed_at': time.time()}

    monkeypatch.setattr(paper, 'market', quote)
    caller = {'id': 'key:paper', 'key_type': 'paper', 'paper_namespace': 'qqbot', 'profile_ids': [], 'scopes': ['read', 'trade']}
    return caller, ticker, calls


def create(caller, user='123456'):
    return paper.create_account(caller, user, 'create-user-' + str(user))['account']


def test_namespace_users_creation_and_rotation_do_not_reset(ledger):
    caller, _, _ = ledger
    assert create(caller)['cash'] == 1000
    paper.bank(caller, '123456', 'borrow', 200, 'borrow-first')
    assert paper.create_account(caller, '123456', 'another-create')['created'] is False
    assert paper.get_account({**caller, 'id': 'key:rotated'}, '123456')['cash'] == 1200
    assert create(caller, '000123')['cash'] == 1000
    other = {**caller, 'paper_namespace': 'another-bot'}
    with pytest.raises(ValueError, match='does not exist'):
        paper.get_account(other, '123456')
    assert create(other)['debt'] == 0
    with pytest.raises(ValueError, match='strings'):
        create(caller, 123456)


def test_bank_net_equity_limits_and_receipts(ledger):
    caller, _, _ = ledger
    create(caller)
    request = (caller, '123456', 'borrow', 500, 'borrow-stable')
    result = paper.bank(*request)
    assert result['account']['cash'] == 1500
    assert result['account']['debt'] == 500
    assert result['account']['net_equity'] == 1000
    assert paper.bank(*request) == result
    with pytest.raises(ValueError, match='different'):
        paper.bank(caller, '123456', 'borrow', 100, 'borrow-stable')
    with pytest.raises(ValueError, match='limit exceeded'):
        paper.bank(caller, '123456', 'borrow', 5000, 'borrow-over-limit')
    result = paper.bank(caller, '123456', 'repay', 200, 'repay-stable')
    assert result['account']['debt'] == 300
    assert result['account']['cash'] == 1300
    with pytest.raises(ValueError, match='exceeds'):
        paper.bank(caller, '123456', 'repay', 301, 'repay-too-much')
    assert paper.operation(caller, '123456', 'borrow-stable')['account']['debt'] == 500
    assert len(paper.history(caller, '123456')) == 3


@pytest.mark.parametrize('value', [0, -1, True, float('inf'), float('nan'), 1e16, 1e-9])
def test_invalid_bank_amounts_never_change_balances(ledger, value):
    caller, _, _ = ledger
    create(caller)
    with pytest.raises(ValueError):
        paper.bank(caller, '123456', 'borrow', value, 'invalid-amount')
    assert paper.get_account(caller, '123456')['cash'] == 1000


def test_spot_profit_partial_sale_and_offline_retry(ledger, monkeypatch):
    caller, ticker, calls = ledger
    create(caller)
    first = paper.trade(caller, '123456', 'spot', 'BTC/USDT', 'buy', 2, 'spot-buy-001')
    assert first['account']['cash'] == 799.8
    assert first['account']['spot'][0]['amount'] == 2
    ticker.update(price=120, bid=120, ask=120)
    sale = paper.trade(caller, '123456', 'spot', 'BTC/USDT', 'sell', 1, 'spot-sell-001')
    assert sale['fill']['realized_pnl'] == 19.88
    assert sale['account']['cash'] == 919.68
    assert sale['account']['spot'][0]['amount'] == 1
    monkeypatch.setattr(paper, 'market', lambda *a, **k: pytest.fail('Receipt retry must not fetch market'))
    assert paper.trade(caller, '123456', 'spot', 'BTC/USDT', 'buy', 2, 'spot-buy-001') == first
    assert len(calls) == 2


@pytest.mark.parametrize('side,close_action,mark,expected', [('open_long', 'close_long', 110, 998.45), ('open_short', 'close_short', 90, 998.55)])
def test_perpetual_partial_close_margin_and_pnl(ledger, side, close_action, mark, expected):
    caller, ticker, _ = ledger
    create(caller)
    first = paper.trade(caller, '123456', 'swap', 'ETH/USDT:USDT', side, 10, 'swap-open-001', 10)
    assert first['account']['cash'] == 899
    assert first['account']['used_margin'] == 100
    ticker.update(price=mark, bid=mark, ask=mark)
    result = paper.trade(caller, '123456', 'swap', 'ETH/USDT:USDT', close_action, 5, 'swap-close-001')
    assert result['account']['cash'] == pytest.approx(expected)
    assert result['account']['positions'][0]['amount'] == 5
    assert result['account']['used_margin'] == 50
    result = paper.trade(caller, '123456', 'swap', 'ETH/USDT:USDT', close_action, 5, 'swap-close-002')
    assert result['account']['positions'] == []
    assert result['account']['cash'] == pytest.approx(1097.9 if side == 'open_long' else 1098.1)


def test_liquidation_preserves_debt_and_borrow_allows_recovery(ledger):
    caller, ticker, _ = ledger
    create(caller)
    paper.bank(caller, '123456', 'borrow', 500, 'bank-before-loss')
    paper.trade(caller, '123456', 'swap', 'BTC/USDT:USDT', 'open_long', 100, 'leveraged-entry', 10)
    ticker.update(price=1, bid=1, ask=1)
    paper.reconcile()
    account = paper.get_account(caller, '123456')
    assert account['positions'] == []
    assert account['cash'] == 490
    assert account['debt'] == 500
    assert account['net_equity'] == -10
    assert any(e['kind'] == 'liquidation' for e in paper.history(caller, '123456'))
    with pytest.raises(ValueError, match='bankrupt'):
        paper.bank(caller, '123456', 'borrow', 200, 'bank-after-loss')
    paper.reconcile()
    assert sum(e['kind'] == 'liquidation' for e in paper.history(caller, '123456')) == 1


def test_user_leverage_and_admin_controls_are_persistent(ledger):
    caller, _, _ = ledger
    create(caller)
    paper.set_leverage(caller, '123456', 20, 'set-leverage-20')
    paper.admin_update('qqbot', '123456', paper.AccountUpdate(cash=3000, debt=50, markets=['spot'], name='QQ user'))
    account = paper.get_account(caller, '123456')
    assert (account['cash'], account['debt'], account['leverage'], account['loan_limit']) == (3000, 50, 20, 5000)
    with pytest.raises(ValueError, match='disabled'):
        paper.trade(caller, '123456', 'swap', 'BTC/USDT:USDT', 'open_long', 1, 'disabled-market')
    paper.admin_update('qqbot', '123456', paper.AccountUpdate(enabled=False))
    with pytest.raises(ValueError, match='disabled'):
        paper.bank(caller, '123456', 'borrow', 1, 'disabled-borrow')
    assert paper.bank(caller, '123456', 'repay', 50, 'disabled-repay')['account']['debt'] == 0
    assert any(e['kind'] == 'admin_adjustment' for e in paper.history(caller, '123456'))


def test_live_paper_and_readonly_permissions(ledger):
    caller, _, _ = ledger
    create(caller)
    with pytest.raises(PermissionError, match='dedicated paper'):
        paper.get_account({**caller, 'key_type': 'live'}, '123456')
    with pytest.raises(PermissionError, match='trade'):
        paper.bank({**caller, 'scopes': ['read']}, '123456', 'borrow', 1, 'readonly-borrow')
    with pytest.raises(PermissionError, match='cannot access live'):
        service.require({**caller, 'profile_ids': ['live']}, 'trade', 'live')
    store.put('paper_settings', 'global', {'enabled': False})
    with pytest.raises(PermissionError, match='disabled'):
        paper.get_account(caller, '123456')


def test_duplicate_concurrent_loans_are_atomic(ledger):
    caller, _, _ = ledger
    create(caller)
    with ThreadPoolExecutor(max_workers=6) as pool:
        def invoke(_):
            from backend.utils.runtime_capacity import CapacityBusy
            try:
                return paper.bank(caller, '123456', 'borrow', 100, 'same-loan-message')
            except CapacityBusy:
                return None
        results = list(pool.map(invoke, range(12)))
    results = [row for row in results if row is not None]
    results.append(paper.bank(caller, '123456', 'borrow', 100, 'same-loan-message'))
    assert all(r == results[0] for r in results)
    assert paper.get_account(caller, '123456')['debt'] == 100


def test_failed_trade_rolls_back_everything_and_operation_can_be_retried(ledger):
    caller, _, _ = ledger
    create(caller)
    with pytest.raises(ValueError, match='Insufficient'):
        paper.trade(caller, '123456', 'spot', 'ETH/USDT', 'buy', 100, 'failed-spot-buy')
    assert paper.get_account(caller, '123456')['spot'] == []
    assert len(paper.history(caller, '123456')) == 1
    paper.bank(caller, '123456', 'borrow', 1000, 'fund-second-try')
    assert paper.trade(caller, '123456', 'spot', 'ETH/USDT', 'buy', 10, 'new-spot-buy')['status'] == 'completed'


def test_paper_key_transfer_keeps_type_and_namespace(ledger):
    from backend.mcp.transfer import export_settings, import_settings
    key = auth.create_api_key('QQBot', ['read', 'trade'], [], 'paper', 'qqbot')
    token = asyncio.run(auth.AuthorizationProvider('http://localhost:7860').load_access_token(key['secret']))
    assert token.claims['key_type'] == 'paper'
    exported = export_settings()
    import_settings(exported)
    assert store.get('key', key['id'])['paper_namespace'] == 'qqbot'
    tampered = copy.deepcopy(exported)
    tampered['keys'][0]['scopes'].append('cancel')
    with pytest.raises(ValueError, match='cannot grant'):
        import_settings(tampered)
    assert 'ca_mcp_' not in json.dumps(export_settings(False))


def test_real_sdk_paper_contracts_and_admin_api(ledger, monkeypatch):
    from backend.app.core.security import create_access_token
    async def idle(stop):
        await stop.wait()
    monkeypatch.setattr(paper, 'maintenance', idle)
    @asynccontextmanager
    async def lifespan(app):
        async with mcp_lifespan():
            yield
    app = FastAPI(lifespan=lifespan)
    install_mcp(app)
    with TestClient(app, base_url='http://localhost:7860') as client:
        admin = {'Authorization': 'Bearer ' + create_access_token()}
        response = client.post('/api/mcp/keys', headers=admin, json={'name': 'QQBot', 'key_type': 'paper', 'scopes': ['read', 'trade']})
        assert response.status_code == 200
        raw = response.json()['key']['secret']
        headers = {'Authorization': 'Bearer ' + raw, 'Accept': 'application/json, text/event-stream'}
        def rpc(name, arguments):
            response = client.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}})
            assert response.status_code == 200
            return response.json()['result']
        assert not rpc('create_paper_account', {'user_id': '888', 'operation_id': 'qq-create-888'}).get('isError')
        assert not rpc('paper_bank', {'user_id': '888', 'action': 'borrow', 'amount': 100, 'operation_id': 'qq-borrow-888'}).get('isError')
        assert rpc('paper_trade', {'user_id': '888', 'market_type': 'swap', 'symbol': 'BTC/USDT:USDT', 'action': 'open_long', 'amount': True, 'operation_id': 'bool-amount-888'}).get('isError')
        assert rpc('set_paper_leverage', {'user_id': '888', 'leverage': True, 'operation_id': 'bool-leverage-888'}).get('isError')
        assert client.patch('/api/mcp/paper/accounts/qqbot/888', headers=headers, json={'cash': 999}).status_code == 401
        edited = client.patch('/api/mcp/paper/accounts/qqbot/888', headers=admin, json={'cash': 500, 'debt': 20})
        assert edited.status_code == 200
        assert client.get('/api/mcp/paper/accounts', headers=admin).json()['items'][0]['cash'] == 500
        assert client.get('/api/mcp/paper/accounts', headers=admin).headers['cache-control'] == 'no-store'
        assert client.post('/api/mcp/keys', headers=admin, json={'name': 'bad', 'key_type': 'paper', 'profile_ids': ['real'], 'scopes': ['read', 'trade']}).status_code == 400
        client.delete('/api/mcp/keys/' + response.json()['key']['id'], headers=admin)
        assert client.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}).status_code == 401


def test_public_market_uses_only_anonymous_methods_and_rejects_stale_quotes(ledger, monkeypatch):
    import ccxt
    from backend.mcp import exchange_runtime
    seen = []
    ticker = {'last': 100, 'bid': 99, 'ask': 101, 'timestamp': time.time() * 1000}

    class PublicExchange:
        def __init__(self, config):
            assert 'apiKey' not in config and 'secret' not in config
            self.session = SimpleNamespace(close=lambda: seen.append('closed'))
        def market(self, symbol):
            return {'symbol': symbol, 'active': True, 'spot': ':' not in symbol, 'swap': ':' in symbol, 'linear': True}
        def fetch_ticker(self, symbol):
            seen.append(('ticker', symbol))
            return ticker
        def fetch_ohlcv(self, symbol, timeframe, limit):
            return [[1, 99, 101, 98, 100, 10]]

    monkeypatch.setattr(ccxt, 'binance', PublicExchange)
    monkeypatch.setattr(ccxt, 'binanceusdm', PublicExchange)
    monkeypatch.setattr(exchange_runtime, 'install_transport', lambda *a: None)
    monkeypatch.setattr(exchange_runtime, 'load_catalogue', lambda *a: None)
    assert PUBLIC_MARKET('BTC/USDT', 'swap')['symbol'] == 'BTC/USDT:USDT'
    assert PUBLIC_MARKET('BTC/USDT', 'spot')['ask'] == 101
    ticker['timestamp'] = (time.time() - 180) * 1000
    with pytest.raises(ValueError, match='stale'):
        PUBLIC_MARKET('BTC/USDT', 'spot')
    with pytest.raises(ValueError, match='USDT'):
        PUBLIC_MARKET('BTC/EUR', 'spot')
    ticker.update(timestamp=time.time() * 1000, bid=102, ask=101)
    with pytest.raises(ValueError, match='invalid'):
        PUBLIC_MARKET('BTC/USDT', 'spot')
    assert seen.count('closed') == 7
