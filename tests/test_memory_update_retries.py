from unittest.mock import Mock

import pytest

from backend import database
from backend.database_schema import initialize_schema
from backend.agent import agent_graph as graph, memory_updates as updates


@pytest.fixture
def job(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'memory-updates.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
        cursor = conn.execute('INSERT INTO summaries(timestamp,symbol,config_id,strategy_logic) VALUES(?,?,?,?)',
                              ('2026-10-09 10:00:00', 'ETH/USDT', 'cfg', 'new verified evidence'))
        summary_id = cursor.lastrowid
        conn.commit()
    database.save_short_memory('2026-10-09 08:00:00', '2026-10-09 09:00:00', 'ETH/USDT', 'cfg', 'prior memory', '', 1)
    updates.enqueue_memory_update('cfg', 'run-1', summary_id)
    monkeypatch.setattr(graph, 'format_recent_position_history_for_memory', lambda *args: 'Confirmed execution evidence')
    return {'config_id': 'cfg', 'symbol': 'ETH/USDT'}


def state():
    with database.get_db_conn() as conn:
        return dict(conn.execute("SELECT * FROM memory_update_jobs WHERE run_id='run-1'").fetchone())


def test_failed_memory_retries_back_off_then_stop_without_touching_old_memory(job, monkeypatch):
    model = Mock(return_value='')
    monkeypatch.setattr(graph, '_run_memory_organizer', model)
    old = database.get_short_memories('cfg')
    for attempt in range(1, 4):
        assert not updates.process_memory_update('cfg', 'run-1', job)
        row = state()
        assert row['attempts'] == attempt
        assert row['status'] == ('failed' if attempt == 3 else 'pending')
        assert row['next_attempt'] > updates.time.time() + (50 if attempt == 1 else 290)
        assert not updates.process_memory_update('cfg', 'run-1', job)
        assert model.call_count == attempt
        with database.get_db_conn() as conn:
            conn.execute("UPDATE memory_update_jobs SET next_attempt=0 WHERE run_id='run-1'")
            conn.commit()
    assert not updates.process_memory_update('cfg', 'run-1', job)
    assert model.call_count == 3 and database.get_short_memories('cfg') == old


def test_successful_memory_update_runs_once_and_keeps_rolling_evidence(job, monkeypatch):
    model = Mock(return_value='new dynamic memory')
    monkeypatch.setattr(graph, '_run_memory_organizer', model)
    assert updates.process_memory_update('cfg', 'run-1', job)
    assert updates.process_memory_update('cfg', 'run-1', job)
    assert state()['status'] == 'completed' and model.call_count == 1
    source = model.call_args.args[0]
    assert 'prior memory' in source and source.count('new verified evidence') == 1
    assert 'Confirmed execution evidence' in source
    assert database.get_short_memories('cfg', 1)[0]['market_summary'] == 'new dynamic memory'
