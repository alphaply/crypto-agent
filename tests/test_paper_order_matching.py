"""Pending Paper orders use execution quotes and survive transient quote expiry."""
from contextlib import contextmanager
from types import SimpleNamespace
import time

import pytest

from test_mcp_paper import PUBLIC_MARKET, create, ledger
from backend.mcp import paper, paper_market, paper_orders as orders, store


@pytest.mark.parametrize('order_type', ['limit', 'stop_loss', 'take_profit'])
def test_matching_does_not_fetch_slow_analysis_or_derivatives(ledger, monkeypatch, order_type):
    from backend.utils.market_data import MarketTool

    caller, ticker, _ = ledger
    create(caller)
    now = [time.time()]
    slow = [False]
    enrichment_calls = []
    monkeypatch.setattr(paper.time, 'time', lambda: now[0])

    @contextmanager
    def public_client(exchange_name, market_type):
        assert exchange_name == 'binance'
        yield SimpleNamespace(
            market=lambda symbol: {
                'symbol': symbol, 'active': True, 'quote': 'USDT',
                'spot': market_type == 'spot', 'swap': market_type == 'swap', 'linear': True,
            },
            fetch_ticker=lambda symbol: {
                'last': ticker['price'], 'bid': ticker['bid'], 'ask': ticker['ask'],
                'timestamp': now[0] * 1000,
            },
        )

    def slow_analysis(*args, **kwargs):
        enrichment_calls.append('analysis')
        if slow[0]:
            now[0] += 20
        return {'analysis': {}}

    def slow_derivatives(*args, **kwargs):
        enrichment_calls.append('sentiment')
        if slow[0]:
            now[0] += 20
        return {}

    monkeypatch.setattr(paper, 'market', PUBLIC_MARKET)
    monkeypatch.setattr(paper_market, 'client', public_client)
    monkeypatch.setattr(MarketTool, 'get_market_analysis', slow_analysis)
    monkeypatch.setattr(MarketTool, '_fetch_market_derivatives', slow_derivatives)
    if order_type == 'limit':
        orders.place(caller, '123456', 'spot', 'BTC/USDT', 'buy', 'limit', 'slow-read-entry', amount=1, price=90)
        target = 90
    else:
        target = 95 if order_type == 'stop_loss' else 110
        orders.execute_market(
            caller, '123456', 'swap', 'BTC/USDT:USDT', 'open_long',
            'slow-read-open', amount=1, leverage=5, **{order_type: target},
        )
    enrichment_calls.clear()
    slow[0] = True
    ticker.update(price=target, bid=target, ask=target)
    paper.reconcile()

    filled = orders.get_orders(caller, '123456', 'filled')['orders']
    assert len(filled) == 1
    assert filled[0]['order_type'] == order_type
    assert filled[0]['fill']['price'] == target
    assert enrichment_calls == []
    if order_type != 'limit':
        assert paper.get_account(caller, '123456')['positions'] == []


@pytest.mark.parametrize('order_type', ['limit', 'stop_loss', 'take_profit'])
def test_quote_expiry_keeps_pending_order_and_reservations_until_fresh_quote(ledger, order_type):
    caller, ticker, _ = ledger
    create(caller)
    if order_type == 'limit':
        pending = orders.place(
            caller, '123456', 'spot', 'BTC/USDT', 'buy', 'limit',
            'expiry-entry', amount=1, price=90,
        )['order']
        kind, symbol, target = 'spot', 'BTC/USDT', 90
    else:
        orders.execute_market(
            caller, '123456', 'swap', 'BTC/USDT:USDT', 'open_long',
            'expiry-open', amount=1, leverage=5, stop_loss=95, take_profit=110,
        )
        pending = next(row for row in orders.get_orders(caller, '123456')['orders'] if row['order_type'] == order_type)
        kind, symbol, target = 'swap', 'BTC/USDT:USDT', 95 if order_type == 'stop_loss' else 110
    before_orders = orders.get_orders(caller, '123456')['orders']
    before = paper.get_account(caller, '123456')
    stale_quote = {
        'exchange': 'binance', 'symbol': symbol, 'market_type': kind,
        'price': target, 'bid': target, 'ask': target, 'observed_at': time.time() - 16,
    }
    with store.connection() as conn:
        account = paper._get(conn, 'qqbot', '123456')
        orders.match(conn, account, kind, symbol, stale_quote, paper.settings(conn))
        paper._save(conn, account)
        conn.commit()
    after = paper.get_account(caller, '123456')
    assert orders.get_orders(caller, '123456')['orders'] == before_orders
    for key in ('cash', 'reserved_cash', 'reserved_spot', 'positions', 'spot', 'fees'):
        assert after[key] == before[key]
    assert not any(event['kind'] in {'order_rejected', 'order_filled'} for event in paper.history(caller, '123456'))

    ticker.update(price=target, bid=target, ask=target)
    paper.reconcile()
    paper.reconcile()
    filled = orders.get_orders(caller, '123456', 'filled')['orders']
    assert len(filled) == 1
    assert filled[0]['order_id'] == pending['order_id']
    assert filled[0]['filled_amount'] == 1
    assert len([event for event in paper.history(caller, '123456') if event['kind'] == 'order_filled']) == 1


def test_non_transient_matching_failure_still_rejects_order(ledger):
    caller, ticker, _ = ledger
    create(caller)
    pending = orders.place(
        caller, '123456', 'spot', 'BTC/USDT', 'buy', 'limit',
        'unfunded-entry', amount=1, price=90,
    )['order']
    paper.admin_update('qqbot', '123456', paper.AccountUpdate(cash=50))
    ticker.update(price=90, bid=90, ask=90)
    paper.reconcile()
    rejected = orders.get_orders(caller, '123456', 'rejected')['orders']
    assert len(rejected) == 1
    assert rejected[0]['order_id'] == pending['order_id']
    assert 'Insufficient' in rejected[0]['reason']
    account = paper.get_account(caller, '123456')
    assert account['cash'] == 50
    assert account['reserved_cash'] == 0
    assert account['spot'] == []


@pytest.mark.parametrize('delay_account_load', [False, True])
def test_stale_maintenance_quote_cannot_liquidate_before_matching(ledger, monkeypatch, delay_account_load):
    caller, ticker, _ = ledger
    create(caller)
    orders.execute_market(
        caller, '123456', 'swap', 'BTC/USDT:USDT', 'open_long',
        'stale-maintenance-open', amount=1, leverage=10, stop_loss=95,
    )
    before = paper.get_account(caller, '123456')
    before_orders = orders.get_orders(caller, '123456')['orders']
    fresh_market = paper.market
    now = [time.time()]
    monkeypatch.setattr(paper.time, 'time', lambda: now[0])
    monkeypatch.setattr(paper, 'market', lambda *args, **kwargs: {
        'exchange': 'binance', 'symbol': 'BTC/USDT:USDT', 'market_type': 'swap',
        'price': 1, 'bid': 1, 'ask': 1, 'observed_at': now[0] - (0 if delay_account_load else 16),
    })
    original_get = paper._get
    if delay_account_load:
        def delayed_get(*args, **kwargs):
            account = original_get(*args, **kwargs)
            now[0] += 16
            return account
        monkeypatch.setattr(paper, '_get', delayed_get)
    with pytest.raises(orders.PaperQuoteExpired):
        paper.reconcile_instrument('swap', 'BTC/USDT:USDT', 'binance', namespace='qqbot', user_id='123456')
    monkeypatch.setattr(paper, '_get', original_get)
    after = paper.get_account(caller, '123456')
    assert after['positions'] == before['positions']
    assert after['cash'] == before['cash']
    assert orders.get_orders(caller, '123456')['orders'] == before_orders
    assert not any(event['kind'] in {'liquidation', 'order_rejected'} for event in paper.history(caller, '123456'))

    monkeypatch.setattr(paper, 'market', fresh_market)
    ticker.update(price=95, bid=95, ask=95)
    paper.reconcile_instrument('swap', 'BTC/USDT:USDT', 'binance', namespace='qqbot', user_id='123456')
    assert paper.get_account(caller, '123456')['positions'] == []
    assert orders.get_orders(caller, '123456', 'filled')['orders'][0]['order_type'] == 'stop_loss'
