from pathlib import Path
import os
import sqlite3
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


def pytest_sessionstart(session):
    from backend.database_schema import initialize_schema
    with sqlite3.connect(os.environ['TRADING_DB_PATH']) as conn:
        initialize_schema(conn)
