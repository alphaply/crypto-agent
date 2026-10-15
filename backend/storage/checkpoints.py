"""PostgreSQL chat checkpoint lifecycle, independent of application imports."""
import os
import threading

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from backend.storage import StorageConfigurationError, _positive, database_url

_guard = threading.Lock()
_pool = None
_saver = None
_pool_pid = None


def get_checkpointer():
    global _pool, _saver, _pool_pid
    with _guard:
        if _saver is None or _pool_pid != os.getpid():
            size = _positive('DB_CHECKPOINT_POOL_MAX_SIZE', 2)
            if not size.is_integer():
                raise StorageConfigurationError('DB_CHECKPOINT_POOL_MAX_SIZE must be an integer')
            _pool = ConnectionPool(database_url(), min_size=0, max_size=int(size), open=True,
                max_waiting=int(size), timeout=10,
                kwargs={'autocommit': True, 'row_factory': dict_row,
                        'prepare_threshold': 0, 'connect_timeout': 5,
                        'options': '-c search_path=checkpoints -c statement_timeout=30000 -c lock_timeout=5000'})
            _saver = PostgresSaver(_pool)
            _pool_pid = os.getpid()
        return _saver


def close_checkpoints():
    global _pool, _saver, _pool_pid
    with _guard:
        pool, _pool, _saver = _pool, None, None
        _pool_pid = None
    if pool:
        pool.close()
