import json
import multiprocessing
import sqlite3

from backend.utils.trade_evidence import initialize_schema, save_plan


def update_worker(path, barrier, queue, filled):
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT payload FROM trade_cycle_evidence').fetchone()
    plan = json.loads(row['payload'])
    expected = plan['record_version']
    plan['record_version'] += 1
    plan['entries'][0]['filled'] = filled
    barrier.wait(timeout=15)
    try:
        conn.execute('BEGIN IMMEDIATE')
        save_plan(conn, 'cfg', plan, expected_version=expected)
        conn.commit()
        queue.put('saved')
    except RuntimeError:
        conn.rollback()
        queue.put('conflict')
    finally:
        conn.close()


def test_two_process_updates_use_compare_and_set_and_restart(tmp_path):
    path = str(tmp_path / 'concurrent.sqlite')
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
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
    reopened = sqlite3.connect(path)
    reopened.row_factory = sqlite3.Row
    initialize_schema(reopened.cursor())
    row = reopened.execute('SELECT version,payload FROM trade_cycle_evidence').fetchone()
    assert row['version'] == 2
    assert json.loads(row['payload'])['entries'][0]['filled'] in [1, 2]
    assert reopened.execute('SELECT count(*) FROM trade_operation_evidence').fetchone()[0] == 1
    reopened.close()
