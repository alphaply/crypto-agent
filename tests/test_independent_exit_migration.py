"""Upgrade fixtures never connect to an exchange or a production database."""
import json
import sqlite3

import pytest

import backend.database as database
from backend.database_independent import MockIndependentTrading, initialize_independent_schema
from backend.database_schema import initialize_schema


LEGACY_TABLE = '''CREATE TABLE mock_exit_orders (
    order_id TEXT PRIMARY KEY,config_id TEXT NOT NULL,symbol TEXT NOT NULL,
    pos_side TEXT NOT NULL,episode_id TEXT NOT NULL,exit_type TEXT NOT NULL,
    price REAL,trigger_price REAL,amount REAL NOT NULL,remaining REAL NOT NULL,
    status TEXT NOT NULL,reason TEXT,timestamp TEXT NOT NULL,created_at REAL NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0)'''


@pytest.fixture
def old_database(tmp_path,monkeypatch):
    path=str(tmp_path/'legacy.db')
    monkeypatch.setattr(database,'DB_NAME',path)
    with database.get_db_conn() as conn:
        initialize_schema(conn)
        conn.execute('DROP TABLE mock_exit_orders')
        conn.execute('DROP TABLE mock_trade_operations')
        conn.execute(LEGACY_TABLE)
        conn.execute('CREATE INDEX idx_legacy_episode ON mock_exit_orders(config_id,episode_id,status)')
        conn.execute('CREATE TABLE mock_trade_operations(config_id TEXT,symbol TEXT,operation_id TEXT,result TEXT NOT NULL,PRIMARY KEY(config_id,symbol,operation_id))')
        conn.execute("INSERT INTO mock_positions(config_id,symbol,side,episode_id,quantity,entry_price,status) VALUES('cfg','ETH/USDT','LONG','old-episode',.8,100,'ACTIVE')")
        for oid,kind,amount,remaining,state,pnl in [('legacy-limit','take_profit_limit',1,.8,'OPEN',2),
                                                 ('legacy-stop','stop_market',1,1,'OPEN',0),
                                                 ('legacy-done','take_profit_limit',.2,0,'CLOSED',4)]:
            conn.execute('INSERT INTO mock_exit_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         (oid,'cfg','ETH/USDT','LONG','old-episode',kind,120 if 'profit' in kind else None,
                          90 if kind=='stop_market' else None,amount,remaining,state,'old reason','2026-01-01 00:00:00',1,pnl))
        receipt=dict(id='legacy-limit',status='open',amount=1,remaining=1)
        conn.execute('INSERT INTO mock_trade_operations VALUES(?,?,?,?)',('cfg','ETH/USDT','old-op',json.dumps(receipt)))
        conn.commit()
    return path,receipt


def test_upgrade_preserves_ids_episode_fill_indexes_receipts_and_legacy_meaning(old_database):
    path,receipt=old_database
    with database.get_db_conn() as conn:
        initialize_independent_schema(conn.cursor())
        conn.commit()
        columns={row['name']:row for row in conn.execute('PRAGMA table_info(mock_exit_orders)')}
        assert columns['amount']['notnull']==columns['remaining']['notnull']==0
        rows=[dict(row) for row in conn.execute('SELECT * FROM mock_exit_orders ORDER BY order_id')]
        assert {row['order_id'] for row in rows}=={'legacy-limit','legacy-stop','legacy-done'}
        assert all(row['contract_version']==1 and row['quantity_mode']=='fixed' and row['episode_id']=='old-episode' for row in rows)
        limit=next(row for row in rows if row['order_id']=='legacy-limit')
        assert limit['order_type']=='limit' and limit['exchange_channel']=='regular'
        assert limit['amount']==1 and limit['remaining']==.8 and limit['filled_amount']==pytest.approx(.2)
        assert limit['realized_pnl']==2
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_legacy_episode'").fetchone()
        before=json.dumps(rows,sort_keys=True)
        initialize_independent_schema(conn.cursor())
        conn.commit()
        assert json.dumps([dict(row) for row in conn.execute('SELECT * FROM mock_exit_orders ORDER BY order_id')],sort_keys=True)==before
    store=MockIndependentTrading('cfg','ETH/USDT')
    assert store.close('LONG',0,'take_profit_limit',100,price=120,operation_id='old-op')==receipt
    assert store.describe_order('legacy-limit')['order_type']=='limit'


def test_migration_rollback_leaves_original_constraints_rows_and_indexes(old_database):
    path,_=old_database
    with database.get_db_conn() as conn:
        # The deliberately conflicting temp name simulates a failed migration.
        conn.execute('CREATE TABLE mock_exit_orders_v2(dummy TEXT)')
        conn.commit()
        with pytest.raises(sqlite3.OperationalError):
            initialize_independent_schema(conn.cursor())
        assert conn.execute('SELECT COUNT(*) FROM mock_exit_orders').fetchone()[0]==3
        columns={row['name']:row for row in conn.execute('PRAGMA table_info(mock_exit_orders)')}
        assert columns['amount']['notnull']==1 and columns['remaining']['notnull']==1
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='idx_legacy_episode'").fetchone()


def test_v2_all_stays_nullable_across_schema_initialization(old_database):
    with database.get_db_conn() as conn:
        initialize_independent_schema(conn.cursor())
        conn.commit()
    store=MockIndependentTrading('cfg','ETH/USDT')
    all_order=store.close('LONG',None,'stop_market',100,trigger_price=80,close_all=True,contract_version=2,operation_id='all')
    with database.get_db_conn() as conn:
        initialize_independent_schema(conn.cursor())
        conn.commit()
    restarted=MockIndependentTrading('cfg','ETH/USDT')
    actual=restarted.describe_order(all_order['order_id'])
    assert actual['contract_version']==2 and actual['quantity_mode']=='all'
    assert actual['amount'] is None and actual['remaining'] is None
    assert restarted.close('LONG',None,'stop_market',100,trigger_price=80,close_all=True,contract_version=2,operation_id='all')==all_order
