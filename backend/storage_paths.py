import os
import shutil
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("DATA_DIR", PROJECT_ROOT / "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)


def data_file(env_key: str, filename: str, legacy_filename: str | None = None) -> Path:
    explicit = os.getenv(env_key)
    if explicit:
        path = Path(explicit).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    path = DATA_DIR / filename
    legacy = PROJECT_ROOT / (legacy_filename or filename)
    if legacy.exists() and not path.exists():
        shutil.copy2(legacy, path)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{legacy}{suffix}")
            if sidecar.exists() and not Path(f"{path}{suffix}").exists():
                shutil.copy2(sidecar, Path(f"{path}{suffix}"))
    return path
