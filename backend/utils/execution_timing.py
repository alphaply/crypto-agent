"""Small, credential-free timing boundaries for trading and historical work."""
from contextlib import contextmanager
import time
from backend.utils.logger import setup_logger

logger = setup_logger('ExecutionTiming')


@contextmanager
def stage(name: str):
    start = time.monotonic()
    outcome = 'completed'
    try:
        yield
    except BaseException:
        outcome = 'failed'
        raise
    finally:
        logger.info('execution_stage stage=%s result=%s elapsed_ms=%.2f',
                    name, outcome, (time.monotonic() - start) * 1000)
