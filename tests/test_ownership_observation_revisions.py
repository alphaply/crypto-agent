"""Read-only receipt observations must not repeatedly restart ownership scans."""
import json

import pytest

from backend import database
from backend.utils.execution_ledger import ExecutionLedger
from test_execution_plan_index import Exchange


def save(conn, plan):
    conn.execute('INSERT INTO real_protection_plans(config_id,symbol,side,payload) VALUES(%s,%s,%s,%s) '
                 'ON CONFLICT(config_id,symbol,side) DO UPDATE SET payload=excluded.payload',
                 ('cfg', 'ETH/USDT:USDT', 'LONG', json.dumps(plan)))
    conn.commit()


def version(conn):
    return conn.execute("SELECT revision FROM plan_source_revisions WHERE symbol='ETH/USDT:USDT'").fetchone()[0]


def test_index_finishes_despite_observations_between_every_page(financial_pg, monkeypatch):
    monkeypatch.setenv('RUNTIME_SCAN_BATCH_SIZE', '1')
    plan = {'symbol': 'ETH/USDT:USDT', 'side': 'LONG', 'entries': [{'id': 'entry', 'price': 100}]}
    with database.get_db_conn() as conn:
        save(conn, plan)
        original_revision = version(conn)
    exchange = Exchange()
    for attempt in range(30):
        plan.update(record_version=attempt, verified_at=attempt, read_errors=['timeout'])
        plan['entries'][0].update(filled=0, status='open', attempt_count=attempt, last_attempt_at=attempt)
        with database.get_db_conn() as conn:
            save(conn, plan)
            assert version(conn) == original_revision
        ledger = ExecutionLedger(exchange, 'cfg', 'ETH/USDT')
        if ledger.index_state['complete']:
            break
    assert ledger.index_state['complete']
    with database.get_db_conn() as conn:
        assert conn.execute("SELECT order_id FROM execution_order_links WHERE config_id='cfg'").fetchone()[0] == 'entry'
        # Observation data is still persisted in full.
        assert json.loads(conn.execute('SELECT payload FROM real_protection_plans').fetchone()[0]) == plan


@pytest.mark.parametrize('field,value', [
    ('id', 'new'), ('execution_order_id', 'executed-child'), ('price', 101),
    ('order_type', 'stop_market'), ('exchange_channel', 'algo'), ('trigger_price', 90),
])
def test_identity_and_intent_changes_invalidate(financial_pg, field, value):
    plan = {'symbol': 'ETH/USDT:USDT', 'side': 'LONG', 'execution_mode': 'independent_exits',
            'exits': [{'id': 'original', 'order_type': 'limit', 'price': 100}]}
    with database.get_db_conn() as conn:
        save(conn, plan)
        before = version(conn)
        plan['exits'][0][field] = value
        save(conn, plan)
        assert version(conn) == before + 1


@pytest.mark.parametrize('field,value', [('account_scope', 'new-account'), ('side', 'SHORT'),
                                        ('stop_loss', 90), ('take_profit', 110), ('episode_id', 'new-cycle')])
def test_scope_and_plan_changes_invalidate(financial_pg, field, value):
    plan = {'symbol': 'ETH/USDT:USDT', 'side': 'LONG', 'entries': [{'id': 'entry'}]}
    with database.get_db_conn() as conn:
        save(conn, plan)
        before = version(conn)
        save(conn, {**plan, field: value})
        assert version(conn) == before + 1


def test_unprojectable_payload_and_events_still_invalidate(financial_pg):
    plan = {'entries': [{'id': 'entry'}], 'memo': 'raw\0text'}
    with database.get_db_conn() as conn:
        save(conn, plan)
        before = version(conn)
        save(conn, {**plan, 'verified_at': 123})
        assert version(conn) == before + 1
        conn.execute('INSERT INTO real_protection_events(config_id,symbol,timestamp,payload) VALUES(%s,%s,%s,%s)',
                     ('cfg', 'ETH/USDT:USDT', '2026-10-15 00:00:00', json.dumps(plan)))
        assert version(conn) == before + 2
