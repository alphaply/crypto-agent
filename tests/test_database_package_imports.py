"""Database packaging must preserve cold imports and the public connection hook."""

from contextlib import nullcontext
import os
from pathlib import Path
import runpy
import subprocess
import sys
import textwrap
from unittest.mock import Mock

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("first_module", [
    "backend.database.news",
    "backend.database.independent",
    "backend.app.main",
    "backend.agent.unified_dispatch",
])
def test_database_package_supports_cold_consumer_imports(tmp_path, first_module):
    """Importing a child or consumer first must not create a cycle or open storage."""
    env = {
        **os.environ,
        "PYTHONPATH": str(PROJECT_ROOT),
        "PYTHON_DOTENV_DISABLED": "1",
        "DATA_DIR": str(tmp_path),
        "RUN_SCHEDULER_IN_WEB": "false",
        "CONFIG_MASTER_KEY": "database-package-import-test-key",
        "SYMBOL_CONFIGS": "[]",
    }
    env.pop("DATABASE_URL", None)
    env.pop("TEST_POSTGRES_URL", None)
    script = textwrap.dedent("""
        import importlib
        import pkgutil
        import sys
        from unittest.mock import patch

        def forbidden(*args, **kwargs):
            raise AssertionError('Import attempted to open storage')

        with patch('psycopg.connect', forbidden), \
             patch('psycopg_pool.ConnectionPool.__init__', forbidden), \
             patch('sqlite3.connect', forbidden):
            importlib.import_module(sys.argv[1])
            from backend import database
            from backend.database import get_db_conn, init_db, save_summary

            for module in pkgutil.iter_modules(database.__path__, database.__name__ + '.'):
                if not module.name.endswith('.__main__'):
                    importlib.import_module(module.name)
            import backend.app.main
            import backend.agent.call_audit
            import backend.app.services.news_service
            import backend.app.services.pricing_service
            import backend.mcp.server

            assert get_db_conn is database.get_db_conn
            assert callable(init_db) and callable(save_summary)
    """)
    result = subprocess.run(
        [sys.executable, "-c", script, first_module], cwd=tmp_path, env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_connection_hook_remains_shared_by_public_functions_and_children(monkeypatch):
    """Existing callers can still replace backend.database.get_db_conn."""
    from backend import database
    from backend.database import news

    connection = Mock()
    connection.execute.return_value.fetchone.return_value = None
    get_connection = Mock(side_effect=lambda: nullcontext(connection))
    monkeypatch.setattr(database, "get_db_conn", get_connection)

    database.save_token_usage("BTC/USDT", "package-test", "offline", 10, 5)
    assert news.latest_snapshot() is None

    assert get_connection.call_count == 2
    connection.cursor.return_value.execute.assert_called_once()
    connection.commit.assert_called_once()


def test_module_command_preserves_explicit_database_initialization(monkeypatch):
    from backend import database

    initialize = Mock()
    monkeypatch.setattr(database, "init_db", initialize)

    runpy.run_module("backend.database", run_name="__main__")

    initialize.assert_called_once_with()
