import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler

import pytz


TZ_CN = pytz.timezone(os.getenv("TIMEZONE", "Asia/Shanghai"))


class LocalTimeFormatter(logging.Formatter):
    """Format log timestamps using the configured local timezone."""

    def converter(self, timestamp):
        dt = datetime.fromtimestamp(timestamp, tz=TZ_CN)
        return dt.timetuple()

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=TZ_CN)
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S")


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

        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
            delay=True,
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger
