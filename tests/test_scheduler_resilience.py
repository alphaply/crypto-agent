import concurrent.futures
import unittest
from unittest.mock import patch

from backend.app.core import scheduler


class FakeExecutor:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def submit(self, fn, config):
        future = concurrent.futures.Future()
        future.set_exception(RuntimeError(f"boom:{config.get('config_id')}"))
        return future


class SchedulerResilienceTests(unittest.TestCase):
    @patch("backend.app.core.scheduler.logger.error")
    @patch("backend.app.core.scheduler.concurrent.futures.ThreadPoolExecutor", FakeExecutor)
    @patch("backend.app.core.scheduler.global_config.get_all_symbol_configs", return_value=[{"config_id": "cfg-a", "enabled": True}])
    def test_job_logs_worker_failures(self, _mock_configs, mock_logger_error):
        scheduler.job()

        self.assertTrue(mock_logger_error.called)
        self.assertIn("cfg-a", mock_logger_error.call_args[0][0])


if __name__ == "__main__":
    unittest.main()