from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from human_gate.identity import (
    DEMO_EFFECT_TOOL,
    GET_TOOL,
    LEGACY_DEMO_EFFECT_TOOL,
    LEGACY_GET_TOOL,
    LEGACY_LIST_TOOL,
    LEGACY_MOCK_PUBLISH_TOOL,
    LEGACY_PLUGIN_ID,
    LIST_TOOL,
    MOCK_PUBLISH_TOOL,
    PLUGIN_DISPLAY_NAME,
    PLUGIN_ID,
)
from human_gate.paths import resolve_db_path

ROOT = Path(__file__).resolve().parents[1]


def test_plugin_manifest_uses_accord_id() -> None:
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    dashboard = (ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8")

    assert manifest["name"] == PLUGIN_ID
    assert LIST_TOOL in manifest["provides_tools"]
    assert GET_TOOL in manifest["provides_tools"]
    assert DEMO_EFFECT_TOOL in manifest["provides_tools"]
    assert MOCK_PUBLISH_TOOL in manifest["provides_tools"]
    assert LEGACY_LIST_TOOL in manifest["provides_tools"]
    assert LEGACY_GET_TOOL in manifest["provides_tools"]
    assert LEGACY_DEMO_EFFECT_TOOL in manifest["provides_tools"]
    assert LEGACY_MOCK_PUBLISH_TOOL in manifest["provides_tools"]
    assert f'"name": "{PLUGIN_ID}"' in dashboard
    assert f'"label": "{PLUGIN_DISPLAY_NAME}"' in dashboard


def test_resolve_db_path_prefers_accord_then_legacy_human_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ACCORD_DB_PATH", raising=False)
    monkeypatch.delenv("HUMAN_GATE_DB_PATH", raising=False)
    home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(home))

    assert resolve_db_path() == home / "plugin-data" / PLUGIN_ID / "approvals.db"

    legacy = home / "plugin-data" / LEGACY_PLUGIN_ID / "approvals.db"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy")
    assert resolve_db_path() == legacy.resolve()

    current = home / "plugin-data" / PLUGIN_ID / "approvals.db"
    current.parent.mkdir(parents=True)
    current.write_bytes(b"accord")
    assert resolve_db_path() == current.resolve()
