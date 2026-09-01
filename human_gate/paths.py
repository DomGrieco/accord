from __future__ import annotations

import os
from pathlib import Path

from .identity import DB_FILENAME, PLUGIN_DATA_DIR_CANDIDATES, PLUGIN_ID


def _env_path(*names: str) -> Path | None:
    for name in names:
        override = os.environ.get(name, "").strip()
        if override:
            return Path(override).expanduser().resolve()
    return None


def _db_exists(path: Path) -> bool:
    return path.exists() or Path(str(path) + "-wal").exists()


def resolve_db_path(*, fallback_data_dir: str | Path | None = None) -> Path:
    override = _env_path("ACCORD_DB_PATH", "HUMAN_GATE_DB_PATH")
    if override is not None:
        return override
    configured_home = os.environ.get("HERMES_HOME", "").strip()
    if configured_home:
        home = Path(configured_home).expanduser().resolve()
        return _resolve_under_home(home)
    if fallback_data_dir is not None:
        return Path(fallback_data_dir).expanduser().resolve() / DB_FILENAME
    return _resolve_under_home(Path("~/.hermes").expanduser().resolve())


def _resolve_under_home(home: Path) -> Path:
    candidates = [home / "plugin-data" / name / DB_FILENAME for name in PLUGIN_DATA_DIR_CANDIDATES]
    for path in candidates:
        if _db_exists(path):
            return path
    return home / "plugin-data" / PLUGIN_ID / DB_FILENAME
