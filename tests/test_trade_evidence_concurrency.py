import json
import multiprocessing
from backend import database, storage

from backend.utils.trade_evidence import initialize_schema, save_plan
from test_trade_tool_integration import simulation, real_short, call
import pytest


@pytest.mark.parametrize('other_side', ['SHORT', 'LONG'])
def test_scope_reservation_rejects_stale_same_or_opposite_snapshot(real_short, other_side):
    from backend.utils.position_protection import ConcurrentProtectionUpdate
    service, exchange = real_short
    first = service._load('ETH/USDT:USDT', 'SHORT')
    stale = (service._load('ETH/USDT:USDT', 'SHORT') if other_side == 'SHORT' else
             service._new('ETH/USDT:USDT', 'LONG'))
    first['exits'].append(dict(client_id='reserved', operation_id='inflight', status='submitting', id=None))
    service._save(first)
    with pytest.raises(ConcurrentProtectionUpdate):
        service._save(stale)
    fresh = service._load('ETH/USDT:USDT', 'SHORT')
    from backend.utils.independent_admission import assess_admission
    assert not assess_admission(fresh, 'close', hedged=True)['allowed']
    other = service._new('ETH/USDT:USDT', 'LONG')
    other['position_observation'] = {'status': 'verified', 'amount': 0}
    assert assess_admission(other, 'open', hedged=True, opposite=fresh)['allowed']
    assert not assess_admission(other, 'open', hedged=False, opposite=fresh)['allowed']
    service._save(other)  # Fresh Hedge snapshot can progress, not permanently locked.
    assert len(exchange.calls) == 1


def test_network_is_outside_reservation_transaction_and_post_send_conflict_never_retries(real_short, monkeypatch):
    from backend import database, storage
    from backend.utils.position_protection import ConcurrentProtectionUpdate
    service, exchange = real_short
    original = exchange.create_order
    calls = []
    def submit(*args, **kwargs):
        with database.get_db_conn() as conn:
            conn.execute("SET LOCAL lock_timeout = '100ms'")
            storage.lock_scope(conn, 'protection-account', service.account_scope, 'ETH/USDT:USDT')
            # The exact protected account remains available while the network runs.
            conn.rollback()
        calls.append(1)
        result = original(*args, **kwargs)
        raise ConcurrentProtectionUpdate('Synthetic post-send conflict')
    monkeypatch.setattr(exchange, 'create_order', submit)
    args = dict(side='SHORT', order_type='stop_market', amount=.2, trigger_price=110, reason='protect')
    result = call('close', args, 'post-submit')
    assert result['status'] == 'unknown' and len(calls) == 1
    call('close', args, 'post-submit')
    assert len(calls) == 1


def test_native_restart_and_transaction_rollback_retain_evidence(tmp_path):
    path = tmp_path / 'migration.sqlite'
    conn = storage.connect()
    initialize_schema(conn.cursor())
    conn.execute('INSERT INTO trade_scope_generations (account_scope,symbol,generation) VALUES(%s,%s,%s)', ('account', 'ETH/USDT:USDT', 8))
    save_plan(conn, 'cfg', dict(account_scope='account', symbol='ETH/USDT:USDT', episode_id='episode', record_version=1,
                               entries=[dict(client_id='reserved', operation_id='op', status='submitting')]))
    conn.commit()
    for _ in range(3):
        initialize_schema(conn.cursor())
    conn.close()
    # A fresh PostgreSQL connection sees the original immutable evidence.
    with database.get_db_conn() as reopened:
        assert reopened.execute('SELECT generation FROM trade_scope_generations').fetchone()[0] == 8
        assert reopened.execute('SELECT count(*) FROM trade_operation_evidence').fetchone()[0] == 1
        reopened.execute('DELETE FROM trade_operation_evidence')
        reopened.rollback()
        assert reopened.execute('SELECT count(*) FROM trade_operation_evidence').fetchone()[0] == 1



def update_worker(path, barrier, queue, filled):
    conn = storage.connect()
    row = conn.execute('SELECT payload FROM trade_cycle_evidence').fetchone()
    plan = json.loads(row['payload'])
    expected = plan['record_version']
    plan['record_version'] += 1
    plan['entries'][0]['filled'] = filled
    barrier.wait(timeout=15)
    try:
        conn.execute('BEGIN')
        from backend.utils.trade_evidence import reserve_scope
        reserve_scope(conn, plan)
        save_plan(conn, 'cfg', plan, expected_version=expected)
        conn.commit()
        assert conn.info.transaction_status == 0
        queue.put('saved')
    except RuntimeError:
        conn.rollback()
        queue.put('conflict')
    finally:
        conn.close()


def test_two_process_updates_use_compare_and_set_and_restart(tmp_path):
    path = str(tmp_path / 'concurrent.sqlite')
    conn = storage.connect()
    initialize_schema(conn.cursor())
    save_plan(conn, 'cfg', dict(account_scope='test', symbol='ETH/USDT', episode_id='cycle', record_version=1,
                               entries=[dict(client_id='client', operation_id='op', status='open', filled=0)]))
    conn.commit()
    conn.close()
    context = multiprocessing.get_context('spawn')
    barrier, queue = context.Barrier(2), context.Queue()
    workers = [context.Process(target=update_worker, args=(path, barrier, queue, n)) for n in [1, 2]]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(25)
        if worker.is_alive():
            worker.terminate()
            worker.join()
        assert worker.exitcode == 0
    assert sorted(queue.get(timeout=2) for _ in workers) == ['conflict', 'saved']
    reopened = storage.connect()
    initialize_schema(reopened.cursor())
    row = reopened.execute('SELECT version,payload FROM trade_cycle_evidence').fetchone()
    assert row['version'] == 2
    assert json.loads(row['payload'])['entries'][0]['filled'] in [1, 2]
    assert reopened.execute('SELECT count(*) FROM trade_operation_evidence').fetchone()[0] == 1
    assert reopened.execute('SELECT generation FROM trade_scope_generations').fetchone()[0] == 1
    reopened.close()


@pytest.fixture(autouse=True)
def _postgres_storage(financial_pg):
    """Run original financial contracts against an isolated PostgreSQL schema."""
    yield
