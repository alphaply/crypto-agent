"""PostgreSQL baseline retains versioned exit contracts without SQLite import."""
import json
import pytest
from backend import database, storage
from backend.database_independent import MockIndependentTrading, initialize_independent_schema


@pytest.fixture
def native_database(financial_pg):
    receipt = dict(id='legacy-limit', status='open', amount=1, remaining=1)
    with database.get_db_conn() as conn:
        conn.execute("INSERT INTO mock_positions(config_id,symbol,side,episode_id,quantity,entry_price,status) VALUES('cfg','ETH/USDT','LONG','old-episode',.8,100,'ACTIVE')")
        for oid, kind, amount, remaining, state, pnl in [
            ('legacy-limit', 'take_profit_limit', 1, .8, 'OPEN', 2),
            ('legacy-stop', 'stop_market', 1, 1, 'OPEN', 0),
            ('legacy-done', 'take_profit_limit', .2, 0, 'CLOSED', 4),
        ]:
            order_type = 'limit' if kind == 'take_profit_limit' else kind
            conn.execute('INSERT INTO mock_exit_orders(order_id,config_id,symbol,pos_side,episode_id,exit_type,'
                         'price,trigger_price,amount,remaining,status,reason,timestamp,created_at,realized_pnl,'
                         'contract_version,order_type,quantity_mode,filled_amount,exchange_channel,exchange_order_id) '
                         "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,'fixed',%s,%s,%s)",
                         (oid, 'cfg', 'ETH/USDT', 'LONG', 'old-episode', kind,
                          120 if order_type == 'limit' else None, 90 if kind == 'stop_market' else None,
                          amount, remaining, state, 'old reason', '2026-01-01 00:00:00', 1, pnl,
                          order_type, max(amount - remaining, 0), 'regular' if order_type == 'limit' else 'algo', oid))
        conn.execute('INSERT INTO mock_trade_operations(config_id,symbol,operation_id,result) VALUES(%s,%s,%s,%s)',
                     ('cfg', 'ETH/USDT', 'old-op', json.dumps(receipt)))
        conn.commit()
    return receipt


def test_native_baseline_preserves_ids_episode_fill_receipts_and_v1_meaning(native_database):
    with database.get_db_conn() as conn:
        columns = {row['name']: row for row in storage.table_columns(conn, 'mock_exit_orders')}
        assert columns['amount']['notnull'] == columns['remaining']['notnull'] == 0
        rows = [storage.record(row) for row in conn.execute('SELECT * FROM mock_exit_orders ORDER BY order_id')]
        assert {row['order_id'] for row in rows} == {'legacy-limit', 'legacy-stop', 'legacy-done'}
        assert all(row['contract_version'] == 1 and row['quantity_mode'] == 'fixed' and row['episode_id'] == 'old-episode' for row in rows)
        limit = next(row for row in rows if row['order_id'] == 'legacy-limit')
        assert limit['order_type'] == 'limit' and limit['exchange_channel'] == 'regular'
        assert limit['amount'] == 1 and limit['remaining'] == .8 and limit['filled_amount'] == pytest.approx(.2)
        assert limit['realized_pnl'] == 2
        before = json.dumps(rows, sort_keys=True)
        initialize_independent_schema(conn.cursor())
        assert json.dumps([storage.record(row) for row in conn.execute('SELECT * FROM mock_exit_orders ORDER BY order_id')], sort_keys=True) == before
    store = MockIndependentTrading('cfg', 'ETH/USDT')
    assert store.close('LONG', 0, 'take_profit_limit', 100, price=120, operation_id='old-op') == native_database
    assert store.describe_order('legacy-limit')['order_type'] == 'limit'


def test_transaction_rollback_retains_original_exit_evidence(native_database):
    with database.get_db_conn() as conn:
        conn.execute("DELETE FROM mock_exit_orders WHERE order_id='legacy-limit'")
        conn.rollback()
        assert conn.execute('SELECT COUNT(*) FROM mock_exit_orders').fetchone()[0] == 3
        assert conn.execute("SELECT filled_amount FROM mock_exit_orders WHERE order_id='legacy-limit'").fetchone()[0] == pytest.approx(.2)


def test_v2_all_stays_nullable_across_native_reopen(native_database):
    store = MockIndependentTrading('cfg', 'ETH/USDT')
    all_order = store.close('LONG', None, 'stop_market', 100, trigger_price=80,
                            close_all=True, contract_version=2, operation_id='all')
    storage.close_pool()
    restarted = MockIndependentTrading('cfg', 'ETH/USDT')
    actual = restarted.describe_order(all_order['order_id'])
    assert actual['contract_version'] == 2 and actual['quantity_mode'] == 'all'
    assert actual['amount'] is None and actual['remaining'] is None
    assert restarted.close('LONG', None, 'stop_market', 100, trigger_price=80,
                           close_all=True, contract_version=2, operation_id='all') == all_order
