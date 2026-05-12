import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.app.api import auth, chat, config, dashboard, history, public, setup, stats
from backend.app.core.runtime import lifespan


app = FastAPI(title="Crypto Agent API", version="2.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(setup.router)
app.include_router(chat.router)
app.include_router(config.router)
app.include_router(dashboard.router)
app.include_router(history.router)
app.include_router(public.router)
app.include_router(stats.router)


DIST_DIR = Path(__file__).resolve().parents[2] / "frontend" / "dist"
ASSETS_DIR = DIST_DIR / "assets"

if ASSETS_DIR.exists():
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="frontend-assets")


@app.get("/health")
def health():
    db_ok = False
    config_ok = False
    config_error = ""
    db_path = ""
    scheduler_enabled = False
    try:
        from backend.database import DB_NAME, get_db_conn

        db_path = str(DB_NAME)
        with get_db_conn() as conn:
            conn.execute("SELECT 1").fetchone()
        db_ok = True
    except Exception:
        db_ok = False
    try:
        from backend.config import config as runtime_config
        from backend.config_store import LAST_RUNTIME_CONFIG_ERROR

        config_error = LAST_RUNTIME_CONFIG_ERROR or ""
        config_ok = not config_error
        scheduler_enabled = bool(getattr(runtime_config, "enable_scheduler", False))
    except Exception as exc:
        config_error = str(exc)
        config_ok = False
    success = bool(db_ok and config_ok)
    return {
        "success": success,
        "status": "ok" if success else "degraded",
        "database": {"ok": db_ok, "path": db_path},
        "config": {"ok": config_ok, "error": config_error},
        "scheduler": {"enabled": scheduler_enabled, "run_in_web": os.getenv("RUN_SCHEDULER_IN_WEB", "true").lower() == "true"},
    }


if DIST_DIR.exists():

    @app.get("/")
    def serve_root():
        return FileResponse(DIST_DIR / "index.html")


    @app.get("/{full_path:path}")
    def serve_spa(full_path: str):
        if full_path.startswith("api/"):
            return {"success": False, "message": "Not found"}
        target = DIST_DIR / full_path
        if target.exists() and target.is_file():
            return FileResponse(target)
        return FileResponse(DIST_DIR / "index.html")


def main() -> None:
    port = int(os.getenv("PORT", "7860"))
    uvicorn.run("backend.app.main:app", host="0.0.0.0", port=port, reload=False)


if __name__ == "__main__":
    main()
