from backend import database
from backend.database_schema import initialize_schema
from backend.app.services.portfolio_history_service import portfolio_decision_page


def test_secondary_symbol_shows_task_decisions_without_duplicating_or_crossing_other_assets(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'history.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    for symbol, config in [('BTC/USDT', 'portfolio'), ('BTC/USDT', 'btc-only'), ('ETH/USDT', 'eth-only')]:
        database.save_summary(symbol, 'test', 'analysis', 'logic', config_id=config)
    rows, count, active = portfolio_decision_page('ETH/USDT', ['portfolio'], 'ALL', 1, 10)
    assert count == 2
    assert {row['config_id'] for row in rows} == {'portfolio', 'eth-only'}
    assert set(active) == {'portfolio', 'eth-only'}
    rows, count, _ = portfolio_decision_page('ETH/USDT', ['portfolio'], 'portfolio', 1, 10)
    assert count == 1 and rows[0]['config_id'] == 'portfolio'
    rows, count, _ = portfolio_decision_page('BTC/USDT', ['portfolio'], 'ALL', 1, 10)
    assert count == 2  # OR must not duplicate a portfolio's primary-symbol row.


def test_cancellation_rows_with_same_exchange_id_keep_asset_values():
    from backend.app.services.dashboard_service import _collapse_recent_order_activity
    rows = []
    for symbol, amount in [('BTC/USDT', .001), ('ETH/USDT', .1)]:
        base = {'activity_type': 'order', 'config_id': 'spot', 'symbol': symbol,
                'order_id': '7', 'status': 'CANCELLED'}
        rows.extend([{**base, 'side': 'CANCEL_BUY', 'amount': 0},
                     {**base, 'side': 'buy', 'amount': amount, 'entry_price': 100}])
    result = _collapse_recent_order_activity(rows)
    assert [(item['symbol'], item['amount']) for item in result] == [('BTC/USDT', .001), ('ETH/USDT', .1)]
