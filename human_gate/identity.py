"""Owner-visible Accord identity and Human Gate migration aliases."""

from __future__ import annotations

PLUGIN_ID = "accord"
LEGACY_PLUGIN_ID = "human-gate"
PLUGIN_DISPLAY_NAME = "Accord"
DB_FILENAME = "approvals.db"
PLUGIN_DATA_DIR_CANDIDATES = (PLUGIN_ID, LEGACY_PLUGIN_ID)
POLICY_ENTRY_CANDIDATES = (PLUGIN_ID, LEGACY_PLUGIN_ID)

LIST_TOOL = "accord_list"
GET_TOOL = "accord_get"
DEMO_EFFECT_TOOL = "accord_demo_effect"
MOCK_PUBLISH_TOOL = "accord_mock_publish"

LEGACY_LIST_TOOL = "human_gate_list"
LEGACY_GET_TOOL = "human_gate_get"
LEGACY_DEMO_EFFECT_TOOL = "human_gate_demo_effect"
LEGACY_MOCK_PUBLISH_TOOL = "human_gate_mock_publish"

TOOLSET = "accord"
LEGACY_TOOLSET = "human_gate"
