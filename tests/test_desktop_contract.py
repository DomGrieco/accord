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
    assert "Human Gate will not retry it automatically" in source
    assert "setTimeout" in source
    assert "expires" not in source
