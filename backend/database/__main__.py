"""Preserve the explicit ``python -m backend.database`` entry point."""

from backend.database import init_db, logger


if __name__ == "__main__":
    init_db()
    logger.info("Database initialized.")
