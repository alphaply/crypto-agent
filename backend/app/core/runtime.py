import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.database import init_db
from backend.main_scheduler import run_smart_scheduler
from backend.utils.logger import setup_logger


logger = setup_logger("FastAPI")
_scheduler_lock = threading.Lock()
_scheduler_thread: threading.Thread | None = None


def _scheduler_enabled() -> bool:
    return os.getenv("ENABLE_SCHEDULER", "true").lower() == "true"


def _run_scheduler_once() -> None:
    global _scheduler_thread
    with _scheduler_lock:
        if _scheduler_thread and _scheduler_thread.is_alive():
            return
        if not _scheduler_enabled():
            logger.info("Scheduler disabled by configuration; web app will run without the scheduler thread.")
            return
        _scheduler_thread = threading.Thread(target=run_smart_scheduler, daemon=True, name="smart-scheduler")
        _scheduler_thread.start()
        logger.info("Background smart scheduler started from FastAPI runtime.")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    if os.getenv("RUN_SCHEDULER_IN_WEB", "true").lower() == "true":
        _run_scheduler_once()
    yield
