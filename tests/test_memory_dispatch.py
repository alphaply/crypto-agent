from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest

from backend import database
from backend.agent import agent_graph
from backend.agent.memory_workflow import memory_review_lease
from backend.app.core import scheduler
from backend.app.services import dashboard_service
from backend.database_schema import initialize_schema


@pytest.fixture
def local_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'memory-dispatch.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)


def test_manual_review_uses_rolling_four_hours_and_reports_existing_result(local_db, monkeypatch):
    now = dashboard_service.TZ_CN.localize(datetime(2026, 10, 6, 10, 15))
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    monkeypatch.setattr(dashboard_service, 'datetime', Clock)
    generate = Mock(return_value=False)
    monkeypatch.setattr(agent_graph, 'generate_rolling_short_memory_for_config', generate)
    monkeypatch.setattr(dashboard_service.global_config, 'get_config_by_id', lambda _: {'config_id': 'cfg'})
    monkeypatch.setattr(dashboard_service, 'get_review_result', lambda *_: {'status': 'completed'})
    monkeypatch.setattr(dashboard_service, 'get_short_memory', lambda *_: {'market_summary': 'retained'})
    result = dashboard_service.generate_short_memory_payload('cfg')
    assert result['review_status'] == 'unchanged'
    assert result['bucket_start'] == '2026-10-06 06:15:00'
    assert result['bucket_end'] == '2026-10-06 10:15:00'
    assert generate.call_args.kwargs['now_cn'] == now
    with pytest.raises(ValueError, match='已结束'):
        dashboard_service.generate_short_memory_payload('cfg', '2026-10-06 11:00:00')
    with pytest.raises(ValueError, match='无效'):
        dashboard_service.generate_short_memory_payload('cfg', 'not-a-date')
    assert generate.call_count == 1


def test_scheduler_marks_partial_terminal_without_repeating_rule_writes(monkeypatch):
    monkeypatch.setattr(scheduler, '_short_memory_done_buckets', set())
    monkeypatch.setattr(scheduler, '_short_memory_last_attempts', {})
    monkeypatch.setattr(scheduler.global_config, 'get_all_symbol_configs', lambda: [{'config_id': 'cfg'}])
    generate = Mock(return_value=False)
    monkeypatch.setattr(scheduler, 'generate_short_memory_for_config', generate)
    monkeypatch.setattr(scheduler, 'get_short_memory', lambda *_: None)
    monkeypatch.setattr(scheduler, 'get_review_result', lambda *_: {'status': 'partial'})
    now = scheduler.TZ_CN.localize(datetime(2026, 10, 6, 12, 1))
    result = scheduler.run_short_memory_job(now)
    assert result['status'] == 'partial'
    assert result['partial'] == ['cfg'] and result['generated'] == 0
    scheduler.run_short_memory_job(now + timedelta(minutes=30))
    generate.assert_called_once()


def test_latest_memory_uses_coverage_end_not_bucket_start(local_db):
    database.save_short_memory('2026-10-06 10:15:00', '2026-10-06 10:15:00', 'ETH/USDT', 'cfg', 'rolling', '', 1)
    database.save_short_memory('2026-10-06 08:00:00', '2026-10-06 12:00:00', 'ETH/USDT', 'cfg', 'completed batch', '', 3)
    assert agent_graph.format_short_memory_for_llm('cfg') == 'completed batch'


def test_lost_lease_rejects_memory_writes(local_db, monkeypatch):
    with memory_review_lease('cfg') as acquired:
        assert acquired
        with database.get_db_conn() as conn:
            conn.execute("UPDATE memory_review_leases SET owner='replacement' WHERE config_id='cfg'")
            conn.commit()
        with pytest.raises(RuntimeError, match='租约'):
            agent_graph.save_short_memory('2026-10-06 00:00:00', '2026-10-06 04:00:00', 'ETH/USDT', 'cfg', 'stale', '', 1)
    assert database.get_short_memories('cfg') == []
