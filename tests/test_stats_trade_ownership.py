from types import SimpleNamespace

import backend.database as database
from backend.app.services import stats_service
from backend.database_schema import initialize_schema
from backend.utils.execution_ledger import account_scope


def test_same_account_fills_are_persisted_only_for_their_verified_real_task(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'owned-stats.db'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)

    def fill(order):
        return {'id': f'fill-{order}', 'order': order, 'timestamp': 1720000000000,
                'datetime': '2024-07-03T09:46:40Z', 'symbol': 'BTC/USDT:USDT',
                'side': 'sell', 'price': 100, 'amount': 1, 'cost': 100,
                'info': {'realizedPnl': '5'}}
    raw = [fill(order) for order in ('a-owned', 'b-owned', 'stale-log', 'unknown')]
    raw.append({**fill('a-owned'), 'id': 'wrong-symbol', 'symbol': 'ETH/USDT:USDT'})
    exchange = SimpleNamespace(
        id='binanceusdm', apiKey='shared-offline-test-account', options={'defaultType': 'swap'},
        market=lambda symbol: {'symbol': 'BTC/USDT:USDT', 'contract': True, 'linear': True},
        fetch_positions=lambda symbols: [], fetch_balance=lambda: {'USDT': {'total': 1000}},
        fetch_my_trades=lambda *a, **k: raw,
    )
    mt = SimpleNamespace(exchange=exchange, market_type='swap')
    with database.get_db_conn() as conn:
        for order in ('a-owned', 'stale-log'):
            conn.execute('''INSERT INTO orders (order_id,config_id,symbol,trade_mode,side,event_type)
                VALUES (?, 'task-a', 'BTC/USDT', 'REAL', 'SELL', 'CLOSE_ORDER_CREATED')''', (order,))
        for order in ('b-owned', 'stale-log'):
            conn.execute('INSERT INTO execution_order_links VALUES (?,?,?,?,?,?)',
                         (account_scope(exchange, 'task-b'), 'BTC/USDT:USDT', order, 'task-b', 'agent_exit', '{}'))
        conn.commit()
    monkeypatch.setattr(stats_service, 'global_config', SimpleNamespace(get_leverage=lambda _: 1))
    monkeypatch.setattr(stats_service, 'get_history_pnl_stats', lambda *a: {})
    for config_id, expected_count in [('task-a', 1), ('task-b', 2)]:
        recent = stats_service._fetch_real_position_data(mt, 'BTC/USDT', {'config_id': config_id})[2]
        assert len(recent) == expected_count
    with database.get_db_conn() as conn:
        rows = conn.execute('SELECT trade_id, config_id FROM trade_history').fetchall()
    assert {(row['trade_id'], row['config_id']) for row in rows} == {
        ('fill-a-owned', 'task-a'), ('fill-b-owned', 'task-b'), ('fill-stale-log', 'task-b'),
    }
