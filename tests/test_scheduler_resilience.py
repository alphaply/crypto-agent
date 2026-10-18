import concurrent.futures
import pytest
from backend import storage
import unittest
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

from backend.app.core import scheduler
from backend.database.schema import initialize_schema


class RecordingExecutor:
    def __init__(self, complete=False):
        self.complete = complete
        self.submissions = []

    def submit(self, fn, *args):
        self.submissions.append((fn, args))
        future = concurrent.futures.Future()
        if self.complete:
            try:
                future.set_result(fn(*args))
            except Exception as exc:
                future.set_exception(exc)
        return future


@pytest.mark.usefixtures('pg')
class SchedulerResilienceTests(unittest.TestCase):
    def setUp(self):
        scheduler._last_run_times.clear()
        scheduler._running_agent_futures.clear()
        scheduler._running_maintenance_futures.clear()
        scheduler._agent_executor = None
        scheduler._maintenance_executor = None
        scheduler._last_heartbeat_key = None

    def tearDown(self):
        scheduler._last_run_times.clear()
        scheduler._running_agent_futures.clear()
        scheduler._running_maintenance_futures.clear()
        scheduler._agent_executor = None
        scheduler._maintenance_executor = None

    def test_interval_20_is_due_only_on_00_20_40(self):
        cfg = {"config_id": "cfg-a", "mode": "STRATEGY", "run_interval": 20}
        due_minutes = []
        for minute in range(60):
            now = scheduler.TZ_CN.localize(datetime(2026, 5, 29, 8, minute, 0))
            if scheduler.is_time_to_run(cfg, now):
                due_minutes.append(minute)

        self.assertEqual(due_minutes, [0, 20, 40])

    def test_scheduler_run_insert_dedupes_same_config_job_and_minute(self):
        conn = storage.connect()
        conn.row_factory = storage.row_factory
        pass  # Native schema installed by the isolated PostgreSQL fixture.

        @contextmanager
        def temp_conn():
            try:
                yield conn
            finally:
                conn.rollback()

        try:
            with patch("backend.database.get_db_conn", temp_conn):
                first = scheduler._insert_scheduler_run("cfg-a", scheduler.AGENT_JOB_TYPE, "2026-05-29 08:20:00")
                second = scheduler._insert_scheduler_run("cfg-a", scheduler.AGENT_JOB_TYPE, "2026-05-29 08:20:00")
                other_minute = scheduler._insert_scheduler_run("cfg-a", scheduler.AGENT_JOB_TYPE, "2026-05-29 08:40:00")
        finally:
            conn.close()

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertTrue(other_minute)

    def test_scheduler_progress_persists_reasoning_tokens(self):
        conn = storage.connect()
        conn.row_factory = storage.row_factory
        pass  # Native schema installed by the isolated PostgreSQL fixture.

        @contextmanager
        def temp_conn():
            try:
                yield conn
            finally:
                conn.rollback()

        scheduled_at = "2026-05-29 08:20:00"
        try:
            with patch("backend.database.get_db_conn", temp_conn):
                scheduler._insert_scheduler_run("cfg-a", scheduler.AGENT_JOB_TYPE, scheduled_at)
                scheduler._mark_scheduler_progress(
                    "cfg-a",
                    scheduled_at,
                    {
                        "phase": "thinking",
                        "message": "working",
                        "reasoning_content": "risk check",
                        "reasoning_tokens": 321,
                    },
                )
            row = conn.execute(
                'SELECT reasoning_content, reasoning_tokens FROM scheduler_runs WHERE config_id = %s',
                ("cfg-a",),
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(row["reasoning_content"], "risk check")
        self.assertEqual(row["reasoning_tokens"], 321)

    def test_job_dispatches_without_waiting_for_agent_future(self):
        agent_executor = RecordingExecutor(complete=False)
        maintenance_executor = RecordingExecutor(complete=False)
        scheduler._agent_executor = agent_executor
        scheduler._maintenance_executor = maintenance_executor
        now = scheduler.TZ_CN.localize(datetime(2026, 5, 29, 8, 20, 0))
        cfg = {"config_id": "cfg-a", "symbol": "BTC/USDT", "mode": "STRATEGY", "run_interval": 20, "enabled": True}

        with patch("backend.app.core.scheduler.global_config.reload_config"), \
            patch("backend.app.core.scheduler.global_config.get_all_symbol_configs", return_value=[cfg]), \
            patch("backend.app.core.scheduler.sync_langsmith_environment"), \
            patch("backend.app.core.scheduler.agent_queue.enqueue", return_value=True):
            result = scheduler.job(now=now)

        self.assertEqual(result["agent_due"], 1)
        self.assertEqual(result["agent_queued"], 1)
        self.assertEqual(len(agent_executor.submissions), 0)
        self.assertEqual(len(maintenance_executor.submissions), 1)
        self.assertNotIn("cfg-a", scheduler._running_agent_futures)

    def test_running_config_persists_next_trigger_without_starting_another_worker(self):
        pending = concurrent.futures.Future()
        scheduler._running_agent_futures["cfg-a"] = pending
        with patch("backend.app.core.scheduler.agent_queue.enqueue", return_value=True) as enqueue:
            submitted = scheduler._submit_agent(
                {"config_id": "cfg-a", "mode": "STRATEGY"},
                "2026-05-29 08:20:00",
            )

        self.assertTrue(submitted)
        enqueue.assert_called_once()
        self.assertIs(scheduler._running_agent_futures['cfg-a'], pending)

    def test_duplicate_scheduler_run_does_not_submit_agent(self):
        agent_executor = RecordingExecutor(complete=False)
        scheduler._agent_executor = agent_executor

        with patch("backend.app.core.scheduler.agent_queue.enqueue", return_value=False):
            submitted = scheduler._submit_agent(
                {"config_id": "cfg-a", "mode": "STRATEGY"},
                "2026-05-29 08:20:00",
            )

        self.assertFalse(submitted)
        self.assertEqual(agent_executor.submissions, [])


if __name__ == "__main__":
    unittest.main()
