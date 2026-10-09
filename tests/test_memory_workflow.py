from backend.agent import memory_service
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

from backend import database
from backend.agent import agent_graph, memory_agent, memory_workflow
from backend.agent.memory_agent import MemoryReviewResult
from backend.database_schema import initialize_schema


CONFIG = {'config_id': 'cfg', 'symbol': 'BTC/USDT', 'mode': 'REAL', 'enabled': True}
NOW = agent_graph.TZ_CN.localize(datetime(2026, 10, 6, 10))
EVIDENCE = '证据读取时间：2026-10-06 10:00\n截至快照: 2026-10-06 09:00\n完整平仓周期: 2 笔；费用未知'


@pytest.fixture
def local_db(tmp_path, monkeypatch):
    class ReviewClock(datetime):
        @classmethod
        def now(cls, tz=None):
            current = datetime(2026, 10, 7, 12)
            return tz.localize(current) if tz else current

    db_path = tmp_path / 'workflow.sqlite'
    monkeypatch.setattr(database, 'DB_NAME', str(db_path))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
        conn.execute("INSERT INTO summaries(timestamp,config_id,strategy_logic) VALUES('2026-10-06 09:00:00','cfg','new evidence')")
        conn.commit()
    monkeypatch.setattr(agent_graph.global_config, 'get_all_symbol_configs', lambda: [CONFIG])
    monkeypatch.setattr(memory_service, 'datetime', ReviewClock)
    monkeypatch.setattr(memory_service, 'format_recent_position_history_for_memory', lambda *_: EVIDENCE)
    # Every paid boundary is mocked, including paths expected never to reach it.
    runner = Mock(side_effect=AssertionError('Unexpected memory model invocation'))
    monkeypatch.setattr(memory_agent, 'run_memory_review', runner)
    return db_path, runner


def prior_memory(evidence='old evidence'):
    database.save_short_memory('2026-10-06 00:00:00', '2026-10-06 04:00:00',
                               'BTC/USDT', 'cfg', 'prior verified memory', evidence, 1)


def rolling_operation():
    return 'rolling:cfg:2026-10-06 09:00:00'


def test_partial_rule_success_is_persisted_without_overwriting_memory_or_retry(local_db):
    _, runner = local_db
    prior_memory()
    before = database.get_short_memories('cfg', 10)
    receipts = [{'success': True, 'status': 'completed', 'operation_id': rolling_operation() + ':rules',
                 'rules': [{'rule_id': 'r1', 'revision': 2, 'config_id': 'cfg', 'content': 'Wait for confirmation'}]}]
    runner.side_effect = None
    runner.return_value = MemoryReviewResult(status='partial', rule_receipts=receipts, error='Final model call failed')

    assert not memory_service.generate_rolling_short_memory_for_config('cfg', now_cn=NOW)
    saved = memory_workflow.get_review_result(rolling_operation())
    assert saved['status'] == 'partial' and saved['summary'] == ''
    assert saved['rule_receipts'] == receipts and saved['error'] == 'Final model call failed'
    assert database.get_short_memories('cfg', 10) == before

    assert not memory_service.generate_rolling_short_memory_for_config('cfg', now_cn=NOW)
    runner.assert_called_once()
    assert database.get_short_memories('cfg', 10) == before


def test_failed_review_preserves_memory_but_can_retry_same_window(local_db):
    _, runner = local_db
    prior_memory()
    before = database.get_short_memories('cfg', 10)
    runner.side_effect = None
    runner.return_value = MemoryReviewResult(status='failed', error='Model unavailable')
    with pytest.raises(memory_service.MemoryReviewError, match='Model unavailable'):
        memory_service.generate_rolling_short_memory_for_config('cfg', now_cn=NOW)
    assert memory_workflow.get_review_result(rolling_operation()) is None
    assert database.get_short_memories('cfg', 10) == before

    runner.return_value = MemoryReviewResult(status='completed', summary='new reviewed evidence')
    assert memory_service.generate_rolling_short_memory_for_config('cfg', now_cn=NOW)
    assert runner.call_count == 2
    assert database.get_short_memories('cfg', 1)[0]['market_summary'] == 'new reviewed evidence'


def test_completed_result_replay_uses_saved_review_without_another_model(local_db):
    _, runner = local_db
    runner.side_effect = None
    runner.return_value = MemoryReviewResult(status='completed', summary='Reviewed conclusion')
    args = {'operation_id': 'explicit:cfg:evidence-hash'}
    assert memory_service.organize_memory('evidence', CONFIG, **args) == 'Reviewed conclusion'
    assert memory_service.organize_memory('evidence', CONFIG, **args) == 'Reviewed conclusion'
    runner.assert_called_once_with('evidence', CONFIG, operation_id=args['operation_id'])


def test_database_lease_blocks_same_task_across_processes_but_allows_other_task(local_db):
    db_path, _ = local_db
    script = '''
import json
import sys
from backend import database
from backend.agent.memory_workflow import memory_review_lease
database.DB_NAME = sys.argv[1]
with memory_review_lease('cfg') as same:
    with memory_review_lease('other') as other:
        print(json.dumps({'same': same, 'other': other}))
'''
    with memory_workflow.memory_review_lease('cfg') as acquired:
        assert acquired
        child = subprocess.run([sys.executable, '-c', script, str(db_path)],
                               cwd=Path(__file__).resolve().parents[1], text=True,
                               capture_output=True, timeout=30, check=True)
        assert json.loads(child.stdout.strip().splitlines()[-1]) == {'same': False, 'other': True}
        with memory_workflow.memory_review_lease('cfg') as still_locked:
            assert not still_locked
    with memory_workflow.memory_review_lease('cfg') as reacquired:
        assert reacquired


def test_lease_releases_after_exception_and_serialized_helpers_do_not_enter(local_db):
    _, runner = local_db
    with memory_workflow.memory_review_lease('cfg') as acquired:
        assert acquired
        assert not memory_service.generate_rolling_short_memory_for_config('cfg', now_cn=NOW)
        runner.assert_not_called()
    with pytest.raises(RuntimeError):
        with memory_workflow.memory_review_lease('cfg') as acquired:
            assert acquired
            raise RuntimeError('Stopped review')
    with memory_workflow.memory_review_lease('cfg') as acquired:
        assert acquired
