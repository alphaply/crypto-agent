import concurrent.futures
import sqlite3
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

from backend.app.core import scheduler
from backend.database_schema import initialize_schema


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


class SchedulerResilienceTests(unittest.TestCase):
    def setUp(self):
        scheduler._last_run_times.clear()
        scheduler._running_agent_futures.clear()
        scheduler._running_maintenance_futures.clear()
        scheduler._agent_executor = None
        scheduler._maintenance_executor = None
        scheduler._daily_summary_future = None
        scheduler._daily_summary_done_date = None
        scheduler._daily_summary_last_attempt_at = None
        scheduler._short_memory_done_buckets.clear()
        scheduler._short_memory_last_attempts.clear()
        scheduler._short_memory_future = None
        scheduler._last_heartbeat_key = None

    def tearDown(self):
        scheduler._last_run_times.clear()
        scheduler._running_agent_futures.clear()
        scheduler._running_maintenance_futures.clear()
        scheduler._agent_executor = None
        scheduler._maintenance_executor = None
        scheduler._daily_summary_future = None
        scheduler._daily_summary_done_date = None
        scheduler._daily_summary_last_attempt_at = None
        scheduler._short_memory_done_buckets.clear()
        scheduler._short_memory_last_attempts.clear()
        scheduler._short_memory_future = None

    def test_interval_20_is_due_only_on_00_20_40(self):
        cfg = {"config_id": "cfg-a", "mode": "STRATEGY", "run_interval": 20}
        due_minutes = []
        for minute in range(60):
            now = scheduler.TZ_CN.localize(datetime(2026, 5, 29, 8, minute, 0))
            if scheduler.is_time_to_run(cfg, now):
                due_minutes.append(minute)

        self.assertEqual(due_minutes, [0, 20, 40])

    def test_scheduler_run_insert_dedupes_same_config_job_and_minute(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        initialize_schema(conn)

        @contextmanager
        def temp_conn():
            yield conn

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
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        initialize_schema(conn)

        @contextmanager
        def temp_conn():
            yield conn

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
                "SELECT reasoning_content, reasoning_tokens FROM scheduler_runs WHERE config_id = ?",
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
            patch("backend.app.core.scheduler._insert_scheduler_run", return_value=True):
            result = scheduler.job(now=now)

        self.assertEqual(result["agent_due"], 1)
        self.assertEqual(result["agent_queued"], 1)
        self.assertEqual(len(agent_executor.submissions), 1)
        self.assertEqual(len(maintenance_executor.submissions), 1)
        self.assertIn("cfg-a", scheduler._running_agent_futures)

    def test_running_config_skips_next_same_config_submit(self):
        pending = concurrent.futures.Future()
        scheduler._running_agent_futures["cfg-a"] = pending
        recorded = []

        def record_run(config_id, job_type, scheduled_at, status="QUEUED", error=None):
            recorded.append((config_id, job_type, scheduled_at, status, error))
            return True

        with patch("backend.app.core.scheduler._insert_scheduler_run", side_effect=record_run):
            submitted = scheduler._submit_agent(
                {"config_id": "cfg-a", "mode": "STRATEGY"},
                "2026-05-29 08:20:00",
            )

        self.assertFalse(submitted)
        self.assertEqual(recorded[0][3], "SKIPPED_RUNNING")

    def test_duplicate_scheduler_run_does_not_submit_agent(self):
        agent_executor = RecordingExecutor(complete=False)
        scheduler._agent_executor = agent_executor

        with patch("backend.app.core.scheduler._insert_scheduler_run", return_value=False):
            submitted = scheduler._submit_agent(
                {"config_id": "cfg-a", "mode": "STRATEGY"},
                "2026-05-29 08:20:00",
            )

        self.assertFalse(submitted)
        self.assertEqual(agent_executor.submissions, [])

    def test_daily_summary_waits_until_configured_time_then_catches_up(self):
        cfg = {"config_id": "cfg-a", "symbol": "BTC/USDT", "enabled": True}
        before = scheduler.TZ_CN.localize(datetime(2026, 8, 15, 0, 4, 0))
        later = scheduler.TZ_CN.localize(datetime(2026, 8, 15, 13, 0, 0))

        with patch.dict("os.environ", {"DAILY_SUMMARY_TIME": "00:05"}), \
            patch("backend.app.core.scheduler.global_config.get_all_symbol_configs", return_value=[cfg]), \
            patch("backend.app.core.scheduler._ensure_balance_snapshot_for_date"), \
            patch("backend.database.get_daily_summaries", return_value=[]), \
            patch("backend.database.get_pending_daily_summary_data", return_value=[{"strategy_logic": "logic"}]), \
            patch("backend.app.core.scheduler.generate_manual_daily_summary", return_value=True) as generate:
            not_due = scheduler.run_daily_summary_job(now=before)
            completed = scheduler.run_daily_summary_job(now=later)

        self.assertEqual(not_due["status"], "not_due")
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["generated"], 1)
        generate.assert_called_once_with("cfg-a", "2026-08-14")

    def test_daily_summary_rebuilds_prompt_echo_and_retries_failures(self):
        cfg = {"config_id": "cfg-a", "symbol": "BTC/USDT", "enabled": True}
        first = scheduler.TZ_CN.localize(datetime(2026, 8, 15, 0, 5, 0))
        prompt_echo = (
            "以下是 2026-08-14 一整天的多轮交易分析逻辑，请汇总为一段200字以内的当日策略行情回顾..."
        )

        with patch.dict(
            "os.environ",
            {"DAILY_SUMMARY_TIME": "00:05", "DAILY_SUMMARY_RETRY_MINUTES": "15"},
        ), patch(
            "backend.app.core.scheduler.global_config.get_all_symbol_configs", return_value=[cfg]
        ), patch(
            "backend.app.core.scheduler._ensure_balance_snapshot_for_date"
        ), patch(
            "backend.database.get_daily_summaries",
            return_value=[{"date": "2026-08-14", "summary": prompt_echo}],
        ), patch(
            "backend.database.get_pending_daily_summary_data",
            return_value=[{"strategy_logic": "logic"}],
        ), patch(
            "backend.app.core.scheduler.generate_manual_daily_summary",
            side_effect=[False, True],
        ) as generate:
            failed = scheduler.run_daily_summary_job(now=first)
            waiting = scheduler.run_daily_summary_job(now=first + timedelta(minutes=10))
            completed = scheduler.run_daily_summary_job(now=first + timedelta(minutes=15))

        self.assertEqual(failed["status"], "retry_pending")
        self.assertEqual(waiting["status"], "retry_wait")
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(generate.call_count, 2)

    def test_short_memory_catches_up_latest_completed_bucket(self):
        cfg = {"config_id": "cfg-a", "symbol": "BTC/USDT", "enabled": True}
        after_boundary = scheduler.TZ_CN.localize(datetime(2026, 9, 5, 12, 17, 0))

        with patch(
            "backend.app.core.scheduler.global_config.get_all_symbol_configs",
            return_value=[cfg],
        ), patch(
            "backend.app.core.scheduler.generate_short_memory_for_config",
            return_value=True,
        ) as generate, patch(
            "backend.app.core.scheduler.get_short_memory",
            return_value={"source_count": 3},
        ):
            result = scheduler.run_short_memory_job(now=after_boundary)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["bucket_start"], "2026-09-05 08:00:00")
        self.assertEqual(result["bucket_end"], "2026-09-05 12:00:00")
        self.assertEqual(result["generated"], 1)
        generate.assert_called_once()
        self.assertEqual(
            generate.call_args.kwargs["now_cn"],
            scheduler.TZ_CN.localize(datetime(2026, 9, 5, 11, 59, 59)),
        )

    def test_short_memory_retries_failed_config_after_interval(self):
        cfg = {"config_id": "cfg-a", "symbol": "BTC/USDT", "enabled": True}
        first = scheduler.TZ_CN.localize(datetime(2026, 9, 5, 12, 1, 0))

        with patch.dict("os.environ", {"SHORT_MEMORY_RETRY_MINUTES": "15"}), patch(
            "backend.app.core.scheduler.global_config.get_all_symbol_configs",
            return_value=[cfg],
        ), patch(
            "backend.app.core.scheduler.generate_short_memory_for_config",
            side_effect=[False, True],
        ) as generate, patch(
            "backend.app.core.scheduler.get_short_memory",
            return_value=None,
        ):
            failed = scheduler.run_short_memory_job(now=first)
            waiting = scheduler.run_short_memory_job(now=first + timedelta(minutes=10))
            completed = scheduler.run_short_memory_job(now=first + timedelta(minutes=15))

        self.assertEqual(failed["status"], "retry_pending")
        self.assertEqual(waiting["status"], "retry_wait")
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(generate.call_count, 2)


if __name__ == "__main__":
    unittest.main()
