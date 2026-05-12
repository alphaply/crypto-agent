import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.app.core.scheduler import run_scheduler_forever, scheduler_should_run
from backend.database import init_db
from backend.utils.logger import setup_logger


logger = setup_logger("FastAPI")
_scheduler_lock = threading.Lock()
_scheduler_thread: threading.Thread | None = None


def _run_scheduler_once() -> None:
    global _scheduler_thread
    with _scheduler_lock:
        if _scheduler_thread and _scheduler_thread.is_alive():
            return
        if not scheduler_should_run():
            logger.info("Scheduler disabled by configuration; API will run without the scheduler thread.")
            return
        _scheduler_thread = threading.Thread(
            target=run_scheduler_forever,
            daemon=True,
            name="smart-scheduler",
        )
        _scheduler_thread.start()
        logger.info("Background smart scheduler started from FastAPI runtime.")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    if os.getenv("RUN_SCHEDULER_IN_WEB", "true").lower() == "true":
        _run_scheduler_once()
    yield
