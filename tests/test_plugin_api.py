from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from human_gate.models import Decision, RequestState, ResumeState
from human_gate.store import GateStore

OWNER_TOKEN = "o" * 64

_API_PATH = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
_SPEC = importlib.util.spec_from_file_location("human_gate_plugin_api_test", _API_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_API = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _API
_SPEC.loader.exec_module(_API)
build_router: Callable[[Path], Any] = _API.build_router


def test_dashboard_api_imports_from_plugin_root_without_installed_package(
    tmp_path: Path,
) -> None:
    isolated_root = tmp_path / "human-gate-plugin"
    isolated_dashboard = isolated_root / "dashboard"
    isolated_dashboard.mkdir(parents=True)
    isolated_api = isolated_dashboard / "plugin_api.py"
    shutil.copy2(_API_PATH, isolated_api)
    shutil.copytree(_API_PATH.parent.parent / "human_gate", isolated_root / "human_gate")
    script = """
import importlib.util
import sys
from pathlib import Path

api_path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_human_gate", api_path)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
assert module.router is not None
assert Path(sys.modules["human_gate"].__file__).resolve().is_relative_to(
    api_path.parent.parent.resolve()
)
"""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    result = subprocess.run(  # noqa: S603 - current interpreter and fixed script
        [sys.executable, "-I", "-c", script, str(isolated_api)],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def _app(path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(build_router(path))
    return TestClient(app)


def _pending(
    path: Path,
    *,
    session_id: str = "runtime-session",
    session_lineage: str = "stored-session",
):
    store = GateStore(path)
    request = store.create_or_get_pending(
        profile="life",
        session_id=session_id,
        session_lineage=session_lineage,
        tool_name="x_create_post",
        effect_kind="publish",
        call_digest="d" * 64,
        display={"account": "@example", "text": "hello"},
        replay={"text": "hello"},
    )
    store.close()
    return request


def _decision_body(request, decision: str, comment: str = "", token: str = OWNER_TOKEN):
    return {
        "token": token,
        "decision": decision,
        "comment": comment,
        "digest": request.call_digest,
        "record_version": request.record_version,
    }


def test_policy_settings_are_owner_authenticated_validated_and_versioned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "gate.db"
    configured = [
        {
            "tool_name": "terminal",
            "effect_kind": "local_command",
            "display_fields": ["command"],
            "replay_fields": ["command"],
            "command_glob": ["git status*"],
        }
    ]
    writes: list[tuple[list[dict[str, Any]], str]] = []
    monkeypatch.setattr(_API, "_read_policy_config", lambda: configured)

    def write(policies: list[dict[str, Any]], expected_digest: str) -> list[dict[str, Any]]:
        writes.append((policies, expected_digest))
        return policies

    monkeypatch.setattr(_API, "_write_policy_config", write)
    client = _app(path)
    client.post("/owner/register", json={"token": OWNER_TOKEN})

    rejected = client.post("/settings/read", json={"token": "x" * 64})
    loaded = client.post("/settings/read", json={"token": OWNER_TOKEN})

    assert rejected.status_code == 403
    assert loaded.status_code == 200
    body = loaded.json()
    assert body["policies"] == configured
    assert len(body["digest"]) == 64
    assert body["restart_required"] is True

    revised = [
        {
            "tool_glob": "records_*",
            "effect_kind": "records_write",
            "display_fields": ["record_id"],
            "replay_fields": ["record_id"],
        }
    ]
    saved = client.put(
        "/settings/policies",
        json={
            "token": OWNER_TOKEN,
            "expected_digest": body["digest"],
            "policies": revised,
        },
    )

    assert saved.status_code == 200
    assert saved.json()["policies"] == revised
    assert writes == [(revised, body["digest"])]


def test_policy_options_are_owner_authenticated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "gate.db"
    monkeypatch.setattr(
        _API,
        "_available_tool_definitions",
        lambda: [
            {
                "name": "terminal",
                "toolset": "terminal",
                "description": "Run a command",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                },
            }
        ],
    )
    monkeypatch.setattr(
        _API,
        "_read_policy_config",
        lambda: [{"tool_name": "terminal", "effect_kind": "local_command"}],
    )
    client = _app(path)
    client.post("/owner/register", json={"token": OWNER_TOKEN})

    rejected = client.post("/settings/options", json={"token": "x" * 64})
    loaded = client.post("/settings/options", json={"token": OWNER_TOKEN})

    assert rejected.status_code == 403
    assert loaded.status_code == 200
    body = loaded.json()
    assert len(body["digest"]) == 64
    assert body["tools"] == [
        {
            "name": "terminal",
            "toolset": "terminal",
            "description": "Run a command",
            "fields": ["command"],
        }
    ]


def test_policy_options_are_bounded_sorted_and_exclude_secret_schema_data() -> None:
    available = [
        {
            "name": "aaa_terminal",
            "toolset": "terminal",
            "description": f"  Run commands\n{'safely ' * 60}",
            "parameters": {
                "type": "object",
                "properties": {
                    "workdir": {"type": "string", "default": "SECRET_VALUE"},
                    "api_key": {"type": "string"},
                    "sessionToken": {"type": "string"},
                    "invalid field": {"type": "string"},
                    "command": {"type": "string"},
                },
            },
        },
        *[
            {
                "name": f"tool_{index:03d}",
                "toolset": "fixture",
                "description": "Fixture tool",
                "parameters": {"type": "object", "properties": {}},
            }
            for index in range(512)
        ],
    ]

    body = _API._build_settings_options(
        available,
        [{"tool_name": "x_create_post", "effect_kind": "publish"}],
    )

    assert len(body["digest"]) == 64
    assert body["effect_kinds"] == [
        "consequential_write",
        "external_publish",
        "local_command",
        "publish",
    ]
    assert len(body["tools"]) == 512
    assert [tool["name"] for tool in body["tools"]] == sorted(
        tool["name"] for tool in body["tools"]
    )
    terminal = body["tools"][0]
    assert terminal["name"] == "aaa_terminal"
    assert terminal["toolset"] == "terminal"
    assert terminal["fields"] == ["command", "workdir"]
    assert "\n" not in terminal["description"]
    assert len(terminal["description"]) <= 240
    encoded = json.dumps(body)
    assert "SECRET_VALUE" not in encoded
    assert "api_key" not in encoded
    assert "sessionToken" not in encoded


def test_policy_options_preserve_registered_identifiers_exactly() -> None:
    body = _API._build_settings_options(
        [
            {
                "name": " exact_tool ",
                "toolset": " custom_set ",
                "description": "  Description may be normalized.  ",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
        [{"tool_name": " exact_tool ", "effect_kind": " custom_effect "}],
    )

    assert body["tools"][0]["name"] == " exact_tool "
    assert body["tools"][0]["toolset"] == " custom_set "
    assert " custom_effect " in body["effect_kinds"]
    assert body["tools"][0]["description"] == "Description may be normalized."


def test_available_tool_definitions_discovers_builtin_and_plugin_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeRegistry:
        def get_all_tool_names(self) -> list[str]:
            calls.append("names")
            return ["terminal"]

        def get_definitions(self, names: set[str], *, quiet: bool) -> list[dict[str, Any]]:
            calls.append(f"definitions:{sorted(names)}:{quiet}")
            return [
                {
                    "type": "function",
                    "function": {
                        "name": "terminal",
                        "description": "Run a command",
                        "parameters": {
                            "type": "object",
                            "properties": {"command": {"type": "string"}},
                        },
                    },
                }
            ]

        def get_entry(self, name: str) -> types.SimpleNamespace:
            calls.append(f"entry:{name}")
            return types.SimpleNamespace(toolset="terminal")

    registry_module = types.ModuleType("tools.registry")
    registry_module.__dict__.update(
        registry=FakeRegistry(),
        discover_builtin_tools=lambda: calls.append("builtins"),
    )
    tools_module = types.ModuleType("tools")
    tools_module.__path__ = []  # type: ignore[attr-defined]
    plugins_module = types.ModuleType("hermes_cli.plugins")
    plugins_module.__dict__["discover_plugins"] = lambda: calls.append("plugins")
    monkeypatch.setitem(sys.modules, "tools", tools_module)
    monkeypatch.setitem(sys.modules, "tools.registry", registry_module)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins_module)

    definitions = _API._available_tool_definitions()

    assert calls == [
        "builtins",
        "plugins",
        "names",
        "definitions:['terminal']:True",
        "entry:terminal",
    ]
    assert definitions == [
        {
            "name": "terminal",
            "toolset": "terminal",
            "description": "Run a command",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
            },
        }
    ]


def test_policy_settings_reject_unsafe_projection_fields_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "gate.db"
    monkeypatch.setattr(_API, "_read_policy_config", lambda: [])
    writes: list[object] = []
    monkeypatch.setattr(
        _API,
        "_write_policy_config",
        lambda policies, expected_digest: writes.append((policies, expected_digest)),
    )
    client = _app(path)
    client.post("/owner/register", json={"token": OWNER_TOKEN})
    loaded = client.post("/settings/read", json={"token": OWNER_TOKEN}).json()

    response = client.put(
        "/settings/policies",
        json={
            "token": OWNER_TOKEN,
            "expected_digest": loaded["digest"],
            "policies": [
                {
                    "tool_name": "dangerous_write",
                    "effect_kind": "write",
                    "display_fields": ["api_key"],
                    "replay_fields": ["api_key"],
                }
            ],
        },
    )

    assert response.status_code == 400
    assert writes == []


def test_request_list_includes_safe_audit_history(tmp_path: Path) -> None:
    path = tmp_path / "gate.db"
    request = _pending(path)
    client = _app(path)
    client.post("/owner/register", json={"token": OWNER_TOKEN})
    client.post(
        f"/requests/{request.id}/decision",
        json=_decision_body(request, "comment", "Use fewer words."),
    )

    response = client.get("/requests?state=changes_requested")

    assert response.status_code == 200
    item = response.json()["requests"][0]
    assert item["audit"] == [
        {
            "id": item["audit"][0]["id"],
            "event_type": "decision",
            "decision": "comment",
            "actor_kind": "owner",
            "actor_id": "desktop-owner",
            "comment": "Use fewer words.",
            "created_at": item["audit"][0]["created_at"],
        }
    ]
    assert "replay" not in item


def test_receipt_audit_never_exposes_persisted_display_payload(tmp_path: Path) -> None:
    path = tmp_path / "gate.db"
    request = _pending(path)
    store = GateStore(path)
    store.decide(request.id, Decision.APPROVE, actor_id="owner")
    assert store.claim(request.id, expected_digest=request.call_digest)
    secret = "PERSISTED_RECEIPT_SECRET"
    store.complete(
        request.id,
        RequestState.EXECUTED,
        result={"ok": True},
        display={"provider_response": secret},
    )
    store.close()

    response = _app(path).get(f"/requests/{request.id}")

    assert response.status_code == 200
    body = response.json()
    assert secret not in str(body)
    assert body["request"]["audit"][-1]["event_type"] == "receipt"
    assert "display" not in body["request"]["audit"][-1]


def test_owner_registration_and_approval_return_durable_resume_instruction(
    tmp_path: Path,
) -> None:
    path = tmp_path / "gate.db"
    request = _pending(path)
    client = _app(path)

    registered = client.post("/owner/register", json={"token": OWNER_TOKEN})
    approved = client.post(
        f"/requests/{request.id}/decision",
        json=_decision_body(request, "approve"),
    )

    assert registered.status_code == 200
    assert approved.status_code == 200
    body = approved.json()
    assert body["request"]["state"] == "approved"
    assert body["resume"] is None

    instruction = client.post(
        f"/requests/{request.id}/resume-instruction",
        json={"token": OWNER_TOKEN},
    )
    assert instruction.status_code == 200
    assert instruction.json()["resume"] == {
        "stored_session_id": "runtime-session",
        "profile": "life",
        "display_kind": "hidden",
        "request_id": request.id,
        "record_version": instruction.json()["request"]["record_version"],
        "prompt": (
            f"Accord request {request.id} was approved by the owner. "
            "Retry the exact original tool call once without changing its arguments. "
            "Do not improvise another consequential action."
        ),
    }

    stored = GateStore(path).get_request(request.id)
    assert stored is not None
    assert stored.state is RequestState.APPROVED
    assert stored.resume_state is ResumeState.DISPATCHING


def test_decision_rejects_wrong_owner_token(tmp_path: Path) -> None:
    path = tmp_path / "gate.db"
    request = _pending(path)
    client = _app(path)
    assert client.post("/owner/register", json={"token": OWNER_TOKEN}).status_code == 200

    response = client.post(
        f"/requests/{request.id}/decision",
        json=_decision_body(request, "approve", token="x" * 64),
    )

    assert response.status_code == 403
    stored = GateStore(path).get_request(request.id)
    assert stored is not None
    assert stored.state is RequestState.PENDING


def test_comment_resumes_with_owner_words_but_never_approves(tmp_path: Path) -> None:
    path = tmp_path / "gate.db"
    request = _pending(path)
    client = _app(path)
    client.post("/owner/register", json={"token": OWNER_TOKEN})

    response = client.post(
        f"/requests/{request.id}/decision",
        json=_decision_body(request, "comment", "Make the first sentence shorter."),
    )

    assert response.status_code == 200
    assert response.json()["resume"] is None
    instruction = client.post(
        f"/requests/{request.id}/resume-instruction",
        json={"token": OWNER_TOKEN},
    )
    assert "Make the first sentence shorter." in instruction.json()["resume"]["prompt"]
    stored = GateStore(path).get_request(request.id)
    assert stored is not None
    assert stored.state is RequestState.CHANGES_REQUESTED


@pytest.mark.parametrize(
    ("decision", "comment", "expected_state", "expected_text"),
    [
        ("approve", "Ship this exact call.", "approved", "Ship this exact call."),
        ("comment", "Change only the ending.", "changes_requested", "Change only the ending."),
        ("deny", "This must not run.", "denied", "This must not run."),
        ("cancel", "No longer needed.", "cancelled", "No longer needed."),
    ],
)
def test_every_decision_returns_a_nonempty_owner_decision_envelope(
    tmp_path: Path,
    decision: str,
    comment: str,
    expected_state: str,
    expected_text: str,
) -> None:
    path = tmp_path / "gate.db"
    request = _pending(path)
    client = _app(path)
    client.post("/owner/register", json={"token": OWNER_TOKEN})

    response = client.post(
        f"/requests/{request.id}/decision",
        json=_decision_body(request, decision, comment),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["request"]["state"] == expected_state
    envelope = body["decision_envelope"]
    assert envelope["request_id"] == request.id
    assert envelope["decision"] == decision
    assert envelope["profile"] == "life"
    assert envelope["stored_session_id"] == "runtime-session"
    assert envelope["prompt"].strip()
    assert expected_text in envelope["prompt"]


@pytest.mark.parametrize("decision", ["approve", "comment", "deny", "cancel"])
def test_every_decision_resume_envelope_targets_exact_record_session(
    tmp_path: Path,
    decision: str,
) -> None:
    path = tmp_path / "gate.db"
    request = _pending(
        path,
        session_id="runtime-tip",
        session_lineage="stored-root",
    )
    client = _app(path)
    client.post("/owner/register", json={"token": OWNER_TOKEN})

    response = client.post(
        f"/requests/{request.id}/decision",
        json=_decision_body(request, decision, "Owner decision."),
    )

    assert response.status_code == 200
    assert response.json()["decision_envelope"]["stored_session_id"] == "runtime-tip"
    instruction = client.post(
        f"/requests/{request.id}/resume-instruction",
        json={"token": OWNER_TOKEN},
    )
    assert instruction.status_code == 200
    assert instruction.json()["resume"]["stored_session_id"] == "runtime-tip"


def test_cancel_queues_nonexecuting_resume_to_originating_session(tmp_path: Path) -> None:
    path = tmp_path / "gate.db"
    request = _pending(path)
    client = _app(path)
    client.post("/owner/register", json={"token": OWNER_TOKEN})
    response = client.post(
        f"/requests/{request.id}/decision",
        json=_decision_body(request, "cancel", "Withdraw this request."),
    )
    assert response.status_code == 200
    instruction = client.post(
        f"/requests/{request.id}/resume-instruction",
        json={"token": OWNER_TOKEN},
    )
    assert instruction.status_code == 200
    assert "Withdraw this request." in instruction.json()["resume"]["prompt"]
    store = GateStore(path)
    assert store.claim(request.id, expected_digest="d" * 64) is False
    stored = store.get_request(request.id)
    assert stored is not None
    assert stored.state is RequestState.CANCELLED
    assert stored.resume_state is ResumeState.DISPATCHING


def test_approved_request_can_be_revoked_with_cancel_resume(tmp_path: Path) -> None:
    path = tmp_path / "gate.db"
    request = _pending(path)
    client = _app(path)
    client.post("/owner/register", json={"token": OWNER_TOKEN})
    approved = client.post(
        f"/requests/{request.id}/decision",
        json=_decision_body(request, "approve"),
    ).json()["request"]

    revoked = client.post(
        f"/requests/{request.id}/decision",
        json={
            "token": OWNER_TOKEN,
            "decision": "cancel",
            "comment": "Withdraw approval.",
            "digest": approved["call_digest"],
            "record_version": approved["record_version"],
        },
    )

    assert revoked.status_code == 200
    assert revoked.json()["request"]["state"] == "cancelled"
    assert revoked.json()["request"]["resume_state"] == "pending"
    assert revoked.json()["resume"] is None
    assert revoked.json()["terminate"] is None
    store = GateStore(path)
    assert store.claim(request.id, expected_digest="d" * 64) is False


def test_deny_queues_nonexecuting_resume_without_replay_authority(
    tmp_path: Path,
) -> None:
    path = tmp_path / "gate.db"
    request = _pending(path)
    client = _app(path)
    client.post("/owner/register", json={"token": OWNER_TOKEN})

    response = client.post(
        f"/requests/{request.id}/decision",
        json=_decision_body(request, "deny", "Do not post this."),
    )

    assert response.status_code == 200
    instruction = client.post(
        f"/requests/{request.id}/resume-instruction",
        json={"token": OWNER_TOKEN},
    )
    assert instruction.status_code == 200
    assert "Do not post this." in instruction.json()["resume"]["prompt"]
    store = GateStore(path)
    assert store.claim(request.id, expected_digest="d" * 64) is False
    stored = store.get_request(request.id)
    assert stored is not None
    assert stored.state is RequestState.DENIED
    assert stored.resume_state is ResumeState.DISPATCHING


def test_resume_target_registers_continuation_tip_under_stable_request_lineage(
    tmp_path: Path,
) -> None:
    path = tmp_path / "gate.db"
    request = _pending(path)
    client = _app(path)
    client.post("/owner/register", json={"token": OWNER_TOKEN})
    client.post(
        f"/requests/{request.id}/decision",
        json=_decision_body(request, "approve"),
    )

    instruction = client.post(
        f"/requests/{request.id}/resume-instruction",
        json={"token": OWNER_TOKEN},
    )
    response = client.post(
        f"/requests/{request.id}/resume-target",
        json={
            "token": OWNER_TOKEN,
            "record_version": instruction.json()["resume"]["record_version"],
            "session_id": "continuation-tip",
        },
    )

    assert response.status_code == 200
    store = GateStore(path)
    assert store.resolve_session_lineage("life", "continuation-tip") == "stored-session"


def test_denied_request_never_exposes_a_termination_instruction(
    tmp_path: Path,
) -> None:
    path = tmp_path / "gate.db"
    request = _pending(path)
    client = _app(path)
    client.post("/owner/register", json={"token": OWNER_TOKEN})
    client.post(
        f"/requests/{request.id}/decision",
        json=_decision_body(request, "deny", "Stop this work."),
    )

    wrong = client.post(
        f"/requests/{request.id}/termination-instruction",
        json={"token": "x" * 64},
    )
    retry = client.post(
        f"/requests/{request.id}/termination-instruction",
        json={"token": OWNER_TOKEN},
    )

    assert wrong.status_code == 403
    assert retry.status_code == 200
    assert retry.json()["terminate"] is None


def test_resume_ack_is_separate_and_authenticated(tmp_path: Path) -> None:
    path = tmp_path / "gate.db"
    request = _pending(path)
    client = _app(path)
    client.post("/owner/register", json={"token": OWNER_TOKEN})
    client.post(
        f"/requests/{request.id}/decision",
        json=_decision_body(request, "approve"),
    )
    instruction = client.post(
        f"/requests/{request.id}/resume-instruction",
        json={"token": OWNER_TOKEN},
    )
    attempt_version = instruction.json()["resume"]["record_version"]

    wrong = client.post(
        f"/requests/{request.id}/resume-ack",
        json={"token": "x" * 64, "record_version": attempt_version},
    )
    ok = client.post(
        f"/requests/{request.id}/resume-ack",
        json={"token": OWNER_TOKEN, "record_version": attempt_version},
    )

    assert wrong.status_code == 403
    assert ok.status_code == 200
    stored = GateStore(path).get_request(request.id)
    assert stored is not None
    assert stored.resume_state is ResumeState.DELIVERED


def test_second_decision_returns_conflict(tmp_path: Path) -> None:
    path = tmp_path / "gate.db"
    request = _pending(path)
    client = _app(path)
    client.post("/owner/register", json={"token": OWNER_TOKEN})
    payload = _decision_body(request, "deny")

    assert client.post(f"/requests/{request.id}/decision", json=payload).status_code == 200
    assert client.post(f"/requests/{request.id}/decision", json=payload).status_code == 409


def test_failed_resume_can_be_retried_without_a_second_decision(tmp_path: Path) -> None:
    path = tmp_path / "gate.db"
    request = _pending(path)
    client = _app(path)
    client.post("/owner/register", json={"token": OWNER_TOKEN})
    client.post(
        f"/requests/{request.id}/decision",
        json=_decision_body(request, "comment", "Use fewer words."),
    )
    instruction = client.post(
        f"/requests/{request.id}/resume-instruction",
        json={"token": OWNER_TOKEN},
    )
    failed = client.post(
        f"/requests/{request.id}/resume-failed",
        json={
            "token": OWNER_TOKEN,
            "error": "Hermes active session limit (3/3); retry after a slot is free.",
            "record_version": instruction.json()["resume"]["record_version"],
        },
    )
    assert failed.status_code == 200
    assert failed.json()["request"]["resume_state"] == "failed"
    assert failed.json()["request"]["resume_error"] == (
        "Hermes active session limit (3/3); retry after a slot is free."
    )

    retry = client.post(
        f"/requests/{request.id}/resume-instruction",
        json={"token": OWNER_TOKEN},
    )

    assert retry.status_code == 200
    assert "Use fewer words." in retry.json()["resume"]["prompt"]
    stored = GateStore(path).get_request(request.id)
    assert stored is not None
    assert stored.resume_state is ResumeState.DISPATCHING


def test_cancel_cannot_race_an_in_flight_resume(tmp_path: Path) -> None:
    path = tmp_path / "gate.db"
    request = _pending(path)
    client = _app(path)
    client.post("/owner/register", json={"token": OWNER_TOKEN})
    approved = client.post(
        f"/requests/{request.id}/decision",
        json=_decision_body(request, "approve"),
    ).json()["request"]
    instruction = client.post(
        f"/requests/{request.id}/resume-instruction",
        json={"token": OWNER_TOKEN},
    )

    cancel = client.post(
        f"/requests/{request.id}/decision",
        json={
            "token": OWNER_TOKEN,
            "decision": "cancel",
            "comment": "Too late to dispatch.",
            "digest": approved["call_digest"],
            "record_version": instruction.json()["request"]["record_version"],
        },
    )

    assert cancel.status_code == 409
    stored = GateStore(path).get_request(request.id)
    assert stored is not None
    assert stored.state is RequestState.APPROVED
    assert stored.resume_state is ResumeState.DISPATCHING


def test_approval_requires_authoritative_digest_and_record_version(tmp_path: Path) -> None:
    path = tmp_path / "gate.db"
    request = _pending(path)
    client = _app(path)
    client.post("/owner/register", json={"token": OWNER_TOKEN})

    response = client.post(
        f"/requests/{request.id}/decision",
        json={
            "token": OWNER_TOKEN,
            "decision": "approve",
            "comment": "",
            "digest": "f" * 64,
            "record_version": 999,
        },
    )

    assert response.status_code == 409
    stored = GateStore(path).get_request(request.id)
    assert stored is not None
    assert stored.state is RequestState.PENDING


def test_policy_settings_compare_and_write_hold_cross_process_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    @contextlib.contextmanager
    def fake_process_lock():
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")

    reads = iter([[], [{"tool_name": "terminal", "effect_kind": "local"}]])

    def fake_read() -> list[dict[str, Any]]:
        events.append("read")
        return next(reads)

    def fake_set_config_value(_encoded_policies: str) -> None:
        events.append("write")

    monkeypatch.setattr(_API, "_cross_process_settings_lock", fake_process_lock)
    monkeypatch.setattr(_API, "_read_policy_config", fake_read)
    monkeypatch.setattr(_API, "_set_policy_config_value", fake_set_config_value)

    saved = _API._write_policy_config(
        [{"tool_name": "terminal", "effect_kind": "local"}],
        _API._policy_digest([]),
    )

    assert saved == [{"tool_name": "terminal", "effect_kind": "local"}]
    assert events == ["lock-enter", "read", "write", "read", "lock-exit"]
