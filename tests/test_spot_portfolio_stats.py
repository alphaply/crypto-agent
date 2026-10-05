import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from backend.app.services import dashboard_service, stats_service
from backend import database
from backend.database_dca import DcaSnapshotStore
from backend.database_schema import initialize_schema


@pytest.fixture
def portfolio(tmp_path, monkeypatch):
    path = tmp_path / 'portfolio.db'
    monkeypatch.setattr(database, 'DB_NAME', str(path))
    @contextmanager
    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
    with connect() as conn:
        initialize_schema(conn)
        for order, symbol, qty, cost in [('btc', 'BTC/USDT', 1, 100), ('eth', 'ETH/USDT', 2, 50)]:
            conn.execute("INSERT INTO orders (order_id,config_id,symbol,trade_mode,status,side) VALUES (?,?,?,'SPOT_DCA','FILLED','buy')", (order, 'spot', symbol))
            conn.execute("INSERT INTO trade_history (trade_id,order_id,config_id,symbol,side,amount,cost,timestamp) VALUES (?,?,?,?, 'buy',?,?,?)", (order, order, 'spot', symbol, qty, cost, '2026-10-01 00:00:00'))
        conn.execute("INSERT INTO orders (order_id,config_id,symbol,trade_mode,status,side) VALUES ('pending-eth','spot','ETH/USDT','SPOT_DCA','PARTIAL','buy')")
        conn.commit()
    cfg = {'config_id': 'spot', 'symbol': 'BTC/USDT', 'symbols': ['BTC/USDT', 'ETH/USDT'], 'mode': 'SPOT_DCA', 'dca_amount': 100}
    sync_calls = []
    def fetch_order(order_id, symbol):
        sync_calls.append((order_id, symbol))
        return {'status': 'open', 'filled': 1, 'cost': 25, 'average': 25}
    exchange = SimpleNamespace(
        fetch_order=fetch_order, fetch_my_trades=lambda *a, **k: [],
        fetch_balance=lambda: {'BTC': {'total': 7}, 'ETH': {'total': 9}},
        fetch_ticker=lambda symbol: {'last': {'BTC/USDT': 120, 'ETH/USDT': 30}[symbol]},
    )
    snapshots = DcaSnapshotStore(connect, lambda: '2026-10-02', lambda: '2026-10-02 12:00:00')
    for module in (dashboard_service, stats_service):
        monkeypatch.setattr(module, 'global_config', SimpleNamespace(get_config_by_id=lambda _: cfg, get_all_symbol_configs=lambda: [cfg]))
    monkeypatch.setattr(dashboard_service, 'get_db_conn', connect)
    monkeypatch.setattr(dashboard_service, 'MarketTool', lambda **_: SimpleNamespace(exchange=exchange))
    monkeypatch.setattr(dashboard_service, 'DCA_STATS_CACHE', {})
    monkeypatch.setattr(dashboard_service, 'update_order_fill_status', lambda *a, **k: None)
    monkeypatch.setattr(dashboard_service, 'upsert_spot_order_fill', lambda *a, **k: None)
    monkeypatch.setattr(dashboard_service, 'save_dca_daily_snapshot', snapshots.save_snapshot)
    return SimpleNamespace(cfg=cfg, snapshots=snapshots, connect=connect, sync_calls=sync_calls, path=path, exchange=exchange)


def test_portfolio_aggregates_quote_values_without_mixing_base_quantities(portfolio):
    result = dashboard_service.calculate_dca_stats('spot')
    assert result['total_invested'] == 175
    assert result['market_value'] == 210
    assert result['unrealized_pnl'] == 35
    assert result['return_pct'] == 20
    assert result['dca_amount_per'] == 100
    assert result['total_qty'] is None and result['avg_cost'] is None
    by_symbol = {item['symbol']: item for item in result['by_symbol']}
    assert by_symbol['BTC/USDT']['total_qty'] == 1
    assert by_symbol['ETH/USDT']['total_qty'] == 3
    assert by_symbol['BTC/USDT']['pending_orders'] == 0
    assert by_symbol['ETH/USDT']['pending_orders'] == 1
    assert portfolio.sync_calls == [('pending-eth', 'ETH/USDT')]
    history = portfolio.snapshots.get_snapshot_history('spot')
    assert len(history) == 1 and history[0]['total_invested'] == 175
    assert history[0]['total_qty'] is None
    assert portfolio.snapshots.get_snapshot_history('spot', symbol='ETH/USDT')[0]['total_qty'] == 3


def test_legacy_manual_cost_only_applies_to_primary_symbol(portfolio):
    portfolio.cfg.update(initial_qty=3, initial_cost=300)
    stats = dashboard_service.calculate_dca_stats('spot')
    by_symbol = {item['symbol']: item for item in stats['by_symbol']}
    assert by_symbol['BTC/USDT']['total_qty'] == 4
    assert by_symbol['BTC/USDT']['total_invested'] == 400
    assert by_symbol['BTC/USDT']['stats_source'] == 'manual_and_trades'
    assert by_symbol['ETH/USDT']['total_qty'] == 3
    assert by_symbol['ETH/USDT']['stats_source'] == 'trades'


def test_position_payload_contains_separate_symbol_positions(portfolio):
    result = stats_service.get_position_stats_payload('spot')
    assert [item['symbol'] for item in result['positions']] == ['BTC/USDT', 'ETH/USDT']
    assert [item['amount'] for item in result['positions']] == [1, 3]
    assert result['balance'] == 210


def test_cancel_events_do_not_duplicate_invested_cost_and_foreign_trades_are_excluded(portfolio):
    with portfolio.connect() as conn:
        conn.execute("INSERT INTO orders(order_id,config_id,symbol,trade_mode,status,side,event_type) VALUES ('btc','spot','BTC/USDT','SPOT_DCA','CANCELLED','CANCEL','ORDER_CANCELLED')")
        conn.execute("INSERT INTO trade_history(trade_id,order_id,config_id,symbol,side,amount,cost) VALUES ('foreign','btc','other-task','BTC/USDT','buy',99,999)")
        conn.commit()
    result = dashboard_service.calculate_dca_stats('spot')
    assert result['total_invested'] == 175
    assert result['buy_count'] == 3


def test_kline_secondary_symbol_never_displays_other_symbol_pending_orders(portfolio, monkeypatch):
    monkeypatch.setattr(stats_service, 'DB_NAME', str(portfolio.path))
    portfolio.exchange.fetch_ohlcv = lambda symbol, *a, **k: [[1000, 30, 31, 29, 30, 100]]
    monkeypatch.setattr(stats_service, 'MarketTool', lambda **_: SimpleNamespace(exchange=portfolio.exchange))
    btc = stats_service.get_kline_payload('spot', symbol='BTC/USDT')
    eth = stats_service.get_kline_payload('spot', symbol='ETH/USDT')
    assert btc['pending_orders'] == []
    assert eth['pending_orders'][0]['order_id'] == 'pending-eth'
    assert eth['position']['amount'] == 3
    assert eth['symbol'] == 'ETH/USDT'
    with pytest.raises(ValueError, match='not configured'):
        stats_service.get_kline_payload('spot', symbol='SOL/USDT')


def test_ticker_failure_preserves_holdings_but_never_reports_zero_valuation(portfolio):
    def ticker(symbol):
        if symbol == 'ETH/USDT':
            raise TimeoutError('offline')
        return {'last': 120}
    portfolio.exchange.fetch_ticker = ticker
    result = stats_service.get_position_stats_payload('spot')
    assert result['balance'] is None
    assert result['unrealized_pnl'] is None
    assert result['dca_stats']['total_invested'] == 175
    assert result['dca_stats']['sync_status'] == 'partial'
    assert result['errors']
    assert result['positions'][1]['mark_price'] is None
    assert result['positions'][1]['amount'] == 3


def test_fill_evidence_survives_empty_exchange_trade_history(portfolio):
    with portfolio.connect() as conn:
        conn.execute("DELETE FROM trade_history WHERE order_id='btc'")
        conn.execute("INSERT INTO spot_order_fills (order_id,config_id,symbol,status,filled_qty,filled_cost,avg_fill_price) VALUES ('btc','spot','BTC/USDT','FILLED',1,100,100)")
        conn.commit()
    result = dashboard_service.calculate_dca_stats('spot')
    assert result['total_invested'] == 175
    assert result['by_symbol'][0]['total_qty'] == 1


def test_duplicate_creation_logs_never_double_count_trade_history(portfolio):
    with portfolio.connect() as conn:
        conn.execute("INSERT INTO orders(order_id,config_id,symbol,trade_mode,status,side) VALUES ('btc','spot','BTC/USDT','SPOT_DCA','FILLED','buy')")
        conn.commit()
    assert dashboard_service.calculate_dca_stats('spot')['total_invested'] == 175


def test_missing_entire_symbol_marks_totals_unknown(portfolio, monkeypatch):
    original = dashboard_service._calculate_dca_symbol_stats
    monkeypatch.setattr(dashboard_service, '_calculate_dca_symbol_stats',
                        lambda config_id, cfg, symbol, force_sync: None if symbol == 'ETH/USDT' else original(config_id, cfg, symbol, force_sync))
    result = stats_service.get_position_stats_payload('spot')
    assert result['balance'] is None
    assert result['dca_stats']['total_invested'] is None
    assert result['dca_stats']['missing_symbols'] == ['ETH/USDT']
    assert result['errors']


def test_initial_single_symbol_balance_adds_subsequent_owned_buys(portfolio):
    portfolio.cfg.update(symbols=['BTC/USDT'], initial_qty=1, manual_avg_cost=50000)
    with portfolio.connect() as conn:
        conn.execute("UPDATE trade_history SET amount=0.1,cost=6000 WHERE order_id='btc'")
        conn.commit()
    result = dashboard_service.calculate_dca_stats('spot')
    assert result['total_qty'] == 1.1
    assert result['total_invested'] == 56000
    assert result['manual_qty'] == 1
    assert result['manual_avg_cost'] == 50000
    assert result['avg_cost'] == round(56000 / 1.1, 4)
