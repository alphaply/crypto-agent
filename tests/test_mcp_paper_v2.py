import asyncio
import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_mcp_paper import create, ledger
from backend.mcp import auth, paper, paper_competition as competition, paper_market, paper_orders as orders, store
from backend.mcp.server import install_mcp, mcp_lifespan


def limit(caller, price=90, action='buy', kind='spot', amount=1, **kw):
    return orders.place(caller, '123456', kind, 'BTC/USDT', action, 'limit', 'limit-entry-001', amount=amount, price=price, **kw)['order']


def test_limit_reservation_matching_once_and_fill_event(ledger):
    caller, ticker, _ = ledger
    create(caller)
    order = limit(caller)
    account = paper.get_account(caller, '123456')
    assert account['reserved_cash'] == 90.09
    assert account['cash'] == 1000
    assert account['available_cash'] == 909.91
    with pytest.raises(ValueError, match='Insufficient'):
        paper.trade(caller, '123456', 'spot', 'ETH/USDT', 'buy', 9.5, 'over-reservation')
    paper.reconcile()
    assert orders.get_orders(caller, '123456')['orders'][0]['state'] == 'open'
    ticker.update(price=85, bid=85, ask=85)
    paper.reconcile()
    paper.reconcile()
    filled = orders.get_orders(caller, '123456', 'filled')['orders']
    assert len(filled) == 1 and filled[0]['fill']['price'] == 85
    account = paper.get_account(caller, '123456')
    assert account['cash'] == 914.915 and account['reserved_cash'] == 0
    notifications = competition.scan(caller)['events']
    assert len(notifications) == 1 and notifications[0]['kind'] == 'order_filled'
    assert notifications[0]['recipient_user_id'] == '123456'
    assert notifications[0]['payload']['order_id'] == order['order_id']


def test_cancel_and_edit_release_old_reservation(ledger):
    caller, _, _ = ledger
    create(caller)
    order = limit(caller)
    edited = orders.edit(caller, '123456', order['order_id'], 'edit-limit-001', price=80, amount=2)
    assert edited['order']['reserved_cash'] == 160.16
    assert paper.get_account(caller, '123456')['reserved_cash'] == 160.16
    with pytest.raises(ValueError, match='Insufficient'):
        orders.edit(caller, '123456', order['order_id'], 'edit-over-limit', amount=20)
    assert paper.get_account(caller, '123456')['reserved_cash'] == 160.16
    cancelled = orders.cancel(caller, '123456', order['order_id'], 'cancel-limit-001')
    assert orders.cancel(caller, '123456', order['order_id'], 'cancel-limit-001') == cancelled
    assert paper.get_account(caller, '123456')['reserved_cash'] == 0


@pytest.mark.parametrize('side,trigger,kind', [('LONG', 110, 'take_profit'), ('LONG', 95, 'stop_loss'), ('SHORT', 90, 'take_profit'), ('SHORT', 105, 'stop_loss')])
def test_oco_protection_closes_correct_side_once(ledger, side, trigger, kind):
    caller, ticker, _ = ledger
    create(caller)
    orders.execute_market(caller, '123456', 'swap', 'BTC', 'open_' + side.lower(), 'open-protected', amount=1, leverage=5,
                          stop_loss=95 if side == 'LONG' else 105, take_profit=110 if side == 'LONG' else 90)
    assert len(orders.get_orders(caller, '123456')['orders']) == 2
    ticker.update(price=trigger, bid=trigger, ask=trigger)
    paper.reconcile()
    assert paper.get_account(caller, '123456')['positions'] == []
    filled = orders.get_orders(caller, '123456', 'filled')['orders']
    assert len(filled) == 1 and filled[0]['order_type'] == kind
    assert len(orders.get_orders(caller, '123456', 'cancelled')['orders']) == 1
    assert len([e for e in competition.scan(caller)['events'] if e['kind'] == 'order_filled']) == 1


def test_old_protection_cannot_close_reopened_position(ledger):
    caller, ticker, _ = ledger
    create(caller)
    orders.execute_market(caller, '123456', 'swap', 'BTC', 'open_long', 'open-first-cycle', amount=1, leverage=5, take_profit=110)
    orders.execute_market(caller, '123456', 'swap', 'BTC', 'close_long', 'close-first-cycle', close_all=True)
    paper.trade(caller, '123456', 'swap', 'BTC', 'open_long', 1, 'open-second-cycle', 5)
    ticker.update(price=120, bid=120, ask=120)
    paper.reconcile()
    assert paper.get_account(caller, '123456')['positions'][0]['amount'] == 1
    assert not orders.get_orders(caller, '123456')['orders']


def test_partial_close_and_dynamic_close_all_protection(ledger):
    caller, ticker, _ = ledger
    create(caller)
    orders.execute_market(caller, '123456', 'swap', 'BTC', 'open_long', 'open-two-units', amount=2, leverage=5, take_profit=110)
    orders.execute_market(caller, '123456', 'swap', 'BTC', 'close_long', 'close-one-unit', amount=1)
    ticker.update(price=110, bid=110, ask=110)
    paper.reconcile()
    assert paper.get_account(caller, '123456')['positions'] == []
    assert orders.get_orders(caller, '123456', 'filled')['orders'][0]['filled_amount'] == 1


def test_limit_entry_attaches_protection_only_after_fill(ledger):
    caller, ticker, _ = ledger
    create(caller)
    limit(caller, price=90, action='open_long', kind='swap', leverage=5, stop_loss=85, take_profit=100)
    assert len(orders.get_orders(caller, '123456')['orders']) == 1
    ticker.update(price=89, bid=89, ask=89)
    paper.reconcile()
    assert len(orders.get_orders(caller, '123456')['orders']) == 2
    ticker.update(price=100, bid=100, ask=100)
    paper.reconcile()
    assert paper.get_account(caller, '123456')['positions'] == []


def test_budget_units_margin_vs_notional_and_atomic_close_all(ledger):
    caller, _, _ = ledger
    create(caller)
    first = orders.execute_market(caller, '123456', 'swap', 'BTC', 'open_long', 'margin-budget-001', margin_amount=100, leverage=10)
    assert first['fill']['amount'] == 10
    assert first['account']['used_margin'] == 100
    orders.execute_market(caller, '123456', 'swap', 'BTC', 'close_long', 'close-full-001', close_all=True)
    notional = orders.execute_market(caller, '123456', 'swap', 'BTC', 'open_long', 'notional-budget-001', quote_amount=100, leverage=10)
    assert notional['fill']['amount'] == 1 and notional['account']['used_margin'] == 10
    with pytest.raises(ValueError, match='exactly one'):
        orders.execute_market(caller, '123456', 'swap', 'BTC', 'open_long', 'ambiguous-units', amount=1, margin_amount=100)


def test_bot_provision_ranking_and_admin_funding_does_not_inflate_pnl(ledger):
    caller, _, _ = ledger
    auth.create_api_key('QQBot', ['read', 'trade'], [], 'paper', 'qqbot')
    bot = competition.bot_account(caller)
    assert bot['account_type'] == 'bot' and bot['cash'] == 1000
    create(caller)
    paper.bank(caller, '123456', 'borrow', 500, 'ranking-borrow')
    paper.admin_update('qqbot', '123456', paper.AccountUpdate(cash=5000, debt=200))
    leaderboard = competition.leaderboard(caller, user_id='123456')
    assert leaderboard['self']['pnl'] == 0 and leaderboard['self']['return_pct'] == 0
    assert leaderboard['bot']['pnl'] == 0
    paper.trade(caller, bot['user_id'], 'spot', 'BTC', 'buy', 1, 'bot-own-buy-001')
    assert competition.bot_account(caller)['spot'][0]['amount'] == 1
    assert paper.get_account(caller, '123456')['spot'] == []
    competition.bootstrap()
    assert competition.bot_account(caller)['spot'][0]['amount'] == 1
    competition.preferences(caller, '123456', 'hide-ranking-user', leaderboard_visible=False)
    assert competition.leaderboard(caller)['total'] == 1


def test_scan_cursor_empty_pages_and_namespace_isolation(ledger):
    caller, ticker, _ = ledger
    create(caller)
    paper.bank(caller, '123456', 'borrow', 100, 'non-notify-loan')
    empty = competition.scan(caller, limit=1)
    assert empty['events'] == [] and empty['next_cursor'] > 0 and empty['has_more']
    order = limit(caller)
    ticker.update(price=80, bid=80, ask=80)
    paper.reconcile()
    feed = competition.scan(caller)
    assert len(feed['events']) == 1
    assert competition.scan(caller, feed['next_cursor'])['events'] == []
    other = {**caller, 'paper_namespace': 'other'}
    assert competition.scan(other)['events'] == []
    competition.preferences(caller, '123456', 'mute-notifications', notifications_enabled=False)
    assert competition.scan(caller)['events'] == []


def test_liquidation_cancels_exits_and_notifies_with_debt(ledger):
    caller, ticker, _ = ledger
    create(caller)
    paper.bank(caller, '123456', 'borrow', 100, 'loan-for-margin')
    orders.execute_market(caller, '123456', 'swap', 'BTC', 'open_long', 'open-to-liquidate', amount=1, leverage=10, take_profit=110)
    ticker.update(price=1, bid=1, ask=1)
    paper.reconcile()
    assert not orders.get_orders(caller, '123456')['orders']
    event = competition.scan(caller)['events'][0]
    assert event['kind'] == 'liquidation' and event['payload']['debt_after'] == 100


def test_spot_reserved_quantity_and_spot_protection(ledger):
    caller, ticker, _ = ledger
    create(caller)
    paper.trade(caller, '123456', 'spot', 'BTC', 'buy', 2, 'spot-two-units')
    orders.place(caller, '123456', 'spot', 'BTC', 'sell', 'limit', 'spot-sell-limit', amount=1, price=120)
    with pytest.raises(ValueError, match='Insufficient'):
        paper.trade(caller, '123456', 'spot', 'BTC', 'sell', 2, 'spot-over-reserved')
    orders.execute_market(caller, '123456', 'spot', 'BTC', 'sell', 'spot-close-all', close_all=True)
    assert not orders.get_orders(caller, '123456')['orders']
    paper.trade(caller, '123456', 'spot', 'BTC', 'buy', 1, 'spot-new-cycle')
    orders.protection(caller, '123456', 'spot', 'BTC', 'LONG', 'spot-protection', stop_loss=95, take_profit=110)
    ticker.update(price=110, bid=110, ask=110)
    paper.reconcile()
    assert paper.get_account(caller, '123456')['spot'] == []


def test_symbol_discovery_queries_catalogue_and_maps_mub(ledger, monkeypatch):
    @contextmanager
    def catalogue(name, kind):
        if kind == 'swap':
            raise ValueError('Venue temporarily unavailable')
        yield SimpleNamespace(markets={'MUB/USDT': {'symbol': 'MUB/USDT', 'base': 'MUB', 'quote': 'USDT', 'spot': True, 'active': True},
                                      'BTC/USDT': {'symbol': 'BTC/USDT', 'base': 'BTC', 'quote': 'USDT', 'spot': True, 'active': True}})
    monkeypatch.setattr(paper_market, 'client', catalogue)
    found = paper_market.search('MU', exchange_name='binance')
    assert found['status'] == 'partial' and found['symbols'][0]['symbol'] == 'MUB/USDT'
    assert found['symbols'][0]['underlying_symbol'] == 'MU'
    assert found['symbols'][0]['catalogue_verified'] is True
    assert paper_market.search('美光', 'spot', 'binance')['total'] == 1
    assert paper_market.search('not-listed', 'spot', 'binance')['total'] == 0


def test_symbol_search_does_not_expand_other_venues_after_binance_failure(ledger, monkeypatch):
    calls = []
    @contextmanager
    def catalogue(name, kind):
        calls.append((name, kind))
        raise ValueError('Binance unreachable')
        yield
    monkeypatch.setattr(paper_market, 'client', catalogue)
    found = paper_market.search('TENCENT', 'swap')
    assert found['status'] == 'partial' and found['total'] == 0
    assert found['queried_exchanges'] == ['binance'] and calls == [('binance', 'swap')]


def test_read_quote_has_no_other_venue_resolution(ledger, monkeypatch):
    calls = []
    def quote(symbol, kind, timeframe, limit, exchange_name=None):
        calls.append(exchange_name)
        raise ValueError('Binance market source unavailable')
    monkeypatch.setattr(paper_market, 'quote', quote)
    monkeypatch.setattr(paper_market, 'search', lambda *a: pytest.fail('No fallback search is permitted'))
    with pytest.raises(ValueError, match='unavailable'):
        paper_market.resolve_quote('tencent/usdt:usdt')
    with pytest.raises(ValueError, match='only supports Binance'):
        paper_market.resolve_quote('TENCENT', exchange_name='bitget')
    assert calls == ['binance']


def test_full_spot_stop_releases_conflicting_limit_exit(ledger):
    caller, ticker, _ = ledger
    create(caller)
    paper.trade(caller, '123456', 'spot', 'BTC', 'buy', 2, 'spot-before-stop')
    orders.place(caller, '123456', 'spot', 'BTC', 'sell', 'limit', 'other-limit-exit', amount=1, price=120)
    orders.protection(caller, '123456', 'spot', 'BTC', 'LONG', 'full-protection', stop_loss=95, take_profit=110)
    ticker.update(price=94, bid=94, ask=94)
    paper.reconcile()
    assert paper.get_account(caller, '123456')['spot'] == []
    assert not orders.get_orders(caller, '123456')['orders']


def test_concurrent_matchers_do_not_double_fill(ledger):
    caller, ticker, _ = ledger
    create(caller)
    limit(caller)
    ticker.update(price=80, bid=80, ask=80)
    with ThreadPoolExecutor(max_workers=4) as pool:
        def reconcile(_):
            from backend.utils.runtime_capacity import CapacityBusy
            try:
                paper.reconcile()
            except CapacityBusy:
                pass
        list(pool.map(reconcile, range(8)))
    paper.reconcile()
    assert paper.get_account(caller, '123456')['spot'][0]['amount'] == 1
    assert len(competition.scan(caller)['events']) == 1


def test_http_polling_sdk_adapters_and_admin_dashboard(ledger, monkeypatch):
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
    key = auth.create_api_key('QQBot', ['read', 'trade'], [], 'paper', 'qqbot')
    with TestClient(app, base_url='http://localhost:7860') as client:
        hdr = {'Authorization': 'Bearer ' + key['secret']}
        admin = {'Authorization': 'Bearer ' + create_access_token()}
        assert client.get('/api/paper/events', headers=admin).status_code == 401
        assert client.get('/api/paper/events', headers=hdr).status_code == 200
        assert client.get('/api/paper/events', headers=hdr).headers['cache-control'] == 'no-store'
        assert client.get('/api/paper/leaderboard', headers=hdr).json()['items'][0]['account_type'] == 'bot'
        create(ledger[0])
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'paper_open_position', 'arguments': {'user_id': '123456', 'symbol': 'BTC', 'side': 'LONG', 'margin_amount': 100, 'leverage': 5, 'operation_id': 'sdk-margin-open', 'stop_loss': 95, 'take_profit': 110}}}
        response = client.post('/mcp', headers={**hdr, 'Accept': 'application/json,text/event-stream'}, json=request).json()['result']
        assert not response.get('isError'), response
        def rpc(name, args):
            result = client.post('/mcp', headers={**hdr, 'Accept': 'application/json,text/event-stream'}, json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}}).json()['result']
            assert not result.get('isError'), result
            return json.loads(result['content'][0]['text'])
        placed = rpc('paper_place_order', {'user_id': '123456', 'market_type': 'swap', 'symbol': 'ETH', 'action': 'open_short', 'order_type': 'limit', 'price': 110, 'margin_amount': 20, 'leverage': 5, 'stop_loss': 115, 'take_profit': 100, 'operation_id': 'sdk-limit-short'})
        assert placed['status'] == 'submitted' and placed['order_status'] == 'open'
        assert placed['order_id'] and placed['quantity_unit'] == 'base'
        edited = rpc('paper_edit_order', {'user_id': '123456', 'order_id': placed['order_id'], 'price': 109, 'operation_id': 'sdk-edit-short'})
        assert edited['price'] == 109 and edited['order_id'] == placed['order_id']
        assert rpc('paper_cancel_order', {'user_id': '123456', 'order_id': placed['order_id'], 'operation_id': 'sdk-cancel-short'})['order_status'] == 'cancelled'
        protected = rpc('paper_set_protection', {'user_id': '123456', 'market_type': 'swap', 'symbol': 'BTC', 'side': 'LONG', 'stop_loss': 96, 'operation_id': 'sdk-change-stop'})
        assert protected['status'] == 'submitted' and protected['order_status'] == 'waiting_trigger'
        assert len(protected['orders']) == 2 and all(order['order_id'] for order in protected['orders'])
        data = client.get('/api/mcp/paper/dashboard', headers=admin).json()
        assert data['account_total'] == 2 and len([o for o in data['orders'] if o['state'] == 'open']) == 2 and data['events']
        assert client.get('/api/mcp/paper/dashboard', headers=hdr).status_code == 401
        store.put('paper_settings', 'global', {'enabled': False})
        assert client.get('/api/mcp/paper/dashboard', headers=admin).json()['leaderboard']
