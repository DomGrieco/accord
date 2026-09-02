from __future__ import annotations

import subprocess
from pathlib import Path
from shutil import which

PLUGIN = Path(__file__).resolve().parents[1] / "desktop" / "plugin.js"


def test_desktop_plugin_is_valid_esm_syntax() -> None:
    node = which("node")
    assert node is not None
    result = subprocess.run(  # noqa: S603 - fixed executable and argument list
        [node, "--check", str(PLUGIN)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_desktop_termination_selection_fails_closed() -> None:
    node = which("node")
    assert node is not None
    script = Path(__file__).with_name("desktop_termination.mjs")
    result = subprocess.run(  # noqa: S603 - fixed executable and argument list
        [node, "--experimental-vm-modules", str(script)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_desktop_plugin_uses_durable_decision_and_session_resume_contract() -> None:
    source = PLUGIN.read_text(encoding="utf-8")

    assert "/owner/register" in source
    assert "/decision" in source
    assert "approve" in source
    assert "deny" in source
    assert "comment" in source
    assert "cancel" in source
    assert "Cancel request" in source
    assert "Revoke approval" in source
    assert "host.profileRoutes" in source
    assert "host.retainProfile" in source
    assert "host.requestProfile" in source
    assert "host.onEvent" in source
    assert "session.resume" in source
    assert "profile: route.targetProfile" in source
    assert "omit_messages: true" in source
    assert "prompt.submit" in source
    assert "display_kind: 'hidden'" in source
    assert "digest: request.call_digest" in source
    assert "record_version: request.record_version" in source
    assert "result.resume" in source

    assert "session.active_list" in source
    assert "session.close" in source
    assert "row.session_key" in source
    assert "Ensure session stopped" not in source
    assert "/resume-ack" in source
    assert "/resume-failed" in source
    assert "wakeAttempted" in source
    assert "/resume-instruction" in source
    assert "Retry session wake" in source
    assert "Audit history" in source
    assert "request.audit" in source
    assert "Accord will not retry it automatically" in source
    assert "Search approvals" in source
    assert "All states" in source
    assert "All effects" in source
    assert "Newest first" in source
    assert "Policy settings" in source
    assert "Add policy" in source
    assert "Save policies" in source
    assert "/settings/read" in source
    assert "/settings/options" in source
    assert "/settings/policies" in source
    assert "Search registered tools" in source
    assert "Matching registered tools" in source
    assert "Search or type a field name" in source
    assert "Only displayed fields can be replayed" in source
    assert "Choose a common effect or enter a custom label" in source
    assert "setOptions({ effect_kinds: [], tools: [] })" in source
    assert "setTimeout" in source
    assert "Queue demo in focused chat" not in source
    assert "Focus a Life chat first" not in source
    assert "Human Gate owner binding is still in the approval database" in source
    assert "Session ${request.stored_session_id" in source
    assert "Lineage ${request.session_lineage}" in source
    assert "${PLUGIN_ID}.demo" not in source
    assert "expires" not in source
