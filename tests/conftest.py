from pathlib import Path
import os
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Even tests without a per-test DB fixture must never use downloaded/runtime data.
_runtime = tempfile.TemporaryDirectory(prefix='crypto-agent-tests-', ignore_cleanup_errors=True)
os.environ['DATA_DIR'] = _runtime.name
os.environ['TRADING_DB_PATH'] = str(Path(_runtime.name) / 'trading.sqlite')
os.environ['CHAT_CHECKPOINT_DB'] = str(Path(_runtime.name) / 'chat.sqlite')
os.environ['RUN_SCHEDULER_IN_WEB'] = 'false'
os.environ['PYTHON_DOTENV_DISABLED'] = '1'
os.environ['JWT_SECRET'] = 'isolated-postgres-tests-only-signing-key'


from postgres_fixtures import backup_client, checkpoint_url, financial_pg, pg, pg_cli, restore_dump


def pytest_sessionstart(session):
    test_url = os.getenv('TEST_POSTGRES_URL')
    # Never inherit a deployment database even for tests lacking a DB fixture.
    os.environ.pop('DATABASE_URL', None)
    if test_url:
        from urllib.parse import urlsplit
        import pytest
        if 'test' not in urlsplit(test_url).path.lower():
            pytest.exit('TEST_POSTGRES_URL must identify a disposable test database', 2)
        os.environ['DATABASE_URL'] = test_url
