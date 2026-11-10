import logging
import os
import sys
import threading
from contextlib import contextmanager
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytz


TZ_CN = pytz.timezone(os.getenv("TIMEZONE", "Asia/Shanghai"))
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUPS = 5
_file_handlers = {}
_handler_lock = threading.RLock()


def log_path() -> Path:
    return Path(os.getenv('DATA_DIR') or '.') / 'app.log'


@contextmanager
def log_file_lock(path: Path):
    """Serialize writes and rotation across Web and scheduler processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + '.lock', 'a+b') as lock:
        if os.name == 'nt':
            import msvcrt
            if lock.seek(0, os.SEEK_END) == 0:
                lock.write(b'0')
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class SharedRotatingFileHandler(RotatingFileHandler):
    def emit(self, record):
        try:
            with log_file_lock(Path(self.baseFilename)):
                # Another process may have rotated the path since our last write.
                if self.stream:
                    self.stream.close()
                    self.stream = None
                try:
                    super().emit(record)
                finally:
                    if self.stream:
                        self.stream.close()
                        self.stream = None
        except Exception:
            self.handleError(record)


class LocalTimeFormatter(logging.Formatter):
    """Format log timestamps using the configured local timezone."""

    def converter(self, timestamp):
        dt = datetime.fromtimestamp(timestamp, tz=TZ_CN)
        return dt.timetuple()

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=TZ_CN)
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S")

    def format(self, record):
        from backend.utils.log_safety import redact_log_text
        return redact_log_text(super().format(record))


def setup_logger(name, log_file="app.log", level=logging.INFO):
    logger = logging.getLogger(name)

    if not logger.handlers:
        logger.setLevel(level)
        logger.propagate = False

        log_format = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
        formatter = LocalTimeFormatter(log_format)

        if hasattr(sys.stdout, "reconfigure"):
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
            except Exception:
                pass

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        if not os.path.isabs(log_file):
            log_dir = os.getenv("DATA_DIR")
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
                log_file = os.path.join(log_dir, log_file)

        path = str(Path(log_file).resolve())
        with _handler_lock:
            file_handler = _file_handlers.get(path)
            if file_handler is None:
                file_handler = SharedRotatingFileHandler(
                    path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS,
                    encoding='utf-8', delay=True,
                )
                file_handler.setFormatter(formatter)
                _file_handlers[path] = file_handler
        logger.addHandler(file_handler)

    return logger
