from __future__ import annotations

import os
from pathlib import Path


def resolve_db_path(*, fallback_data_dir: str | Path | None = None) -> Path:
    override = os.environ.get("HUMAN_GATE_DB_PATH", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    configured_home = os.environ.get("HERMES_HOME", "").strip()
    if configured_home:
        home = Path(configured_home).expanduser().resolve()
        return home / "plugin-data" / "human-gate" / "approvals.db"
    if fallback_data_dir is not None:
        return Path(fallback_data_dir).expanduser().resolve() / "approvals.db"
    return Path("~/.hermes/plugin-data/human-gate/approvals.db").expanduser().resolve()
