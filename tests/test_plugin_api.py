from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from human_gate.models import RequestState, ResumeState
from human_gate.store import GateStore

OWNER_TOKEN = "o" * 64

_API_PATH = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
_SPEC = importlib.util.spec_from_file_location("human_gate_plugin_api_test", _API_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_API = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _API
_SPEC.loader.exec_module(_API)
build_router: Callable[[Path], Any] = _API.build_router


def _app(path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(build_router(path))
    return TestClient(app)


def _pending(path: Path):
    store = GateStore(path)
    request = store.create_or_get_pending(
        profile="life",
        session_id="runtime-session",
        session_lineage="stored-session",
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
        "stored_session_id": "stored-session",
        "profile": "life",
        "display_kind": "hidden",
        "request_id": request.id,
        "prompt": (
            f"Human Gate request {request.id} was approved by the owner. "
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


def test_cancel_closes_request_without_resuming_or_stopping_session(tmp_path: Path) -> None:
    path = tmp_path / "gate.db"
    request = _pending(path)
    client = _app(path)
    client.post("/owner/register", json={"token": OWNER_TOKEN})

    response = client.post(
        f"/requests/{request.id}/decision",
        json=_decision_body(request, "cancel", "Withdraw this request."),
    )

    assert response.status_code == 200
    assert response.json()["resume"] is None
    assert response.json()["terminate"] is None
    store = GateStore(path)
    assert store.claim(request.id, expected_digest="d" * 64) is False
    stored = store.get_request(request.id)
    assert stored is not None
    assert stored.state is RequestState.CANCELLED
    assert stored.resume_state is ResumeState.NOT_REQUESTED


def test_approved_request_can_be_revoked_without_session_action(tmp_path: Path) -> None:
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
    assert revoked.json()["request"]["resume_state"] == "not_requested"
    assert revoked.json()["resume"] is None
    assert revoked.json()["terminate"] is None
    store = GateStore(path)
    assert store.claim(request.id, expected_digest="d" * 64) is False


def test_deny_kills_request_without_waking_session_or_replay_authority(
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
    assert response.json()["resume"] is None
    assert response.json()["terminate"] == {
        "stored_session_id": "stored-session",
        "profile": "life",
        "request_id": request.id,
    }
    store = GateStore(path)
    assert store.claim(request.id, expected_digest="d" * 64) is False
    stored = store.get_request(request.id)
    assert stored is not None
    assert stored.state is RequestState.DENIED
    assert stored.resume_state is ResumeState.NOT_REQUESTED


def test_denied_request_exposes_authenticated_retryable_termination_instruction(
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
    assert retry.json()["terminate"] == {
        "stored_session_id": "stored-session",
        "profile": "life",
        "request_id": request.id,
    }


def test_resume_ack_is_separate_and_authenticated(tmp_path: Path) -> None:
    path = tmp_path / "gate.db"
    request = _pending(path)
    client = _app(path)
    client.post("/owner/register", json={"token": OWNER_TOKEN})
    client.post(
        f"/requests/{request.id}/decision",
        json=_decision_body(request, "approve"),
    )
    client.post(
        f"/requests/{request.id}/resume-instruction",
        json={"token": OWNER_TOKEN},
    )

    wrong = client.post(
        f"/requests/{request.id}/resume-ack",
        json={"token": "x" * 64},
    )
    ok = client.post(
        f"/requests/{request.id}/resume-ack",
        json={"token": OWNER_TOKEN},
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
    client.post(
        f"/requests/{request.id}/resume-instruction",
        json={"token": OWNER_TOKEN},
    )
    assert client.post(
        f"/requests/{request.id}/resume-failed",
        json={"token": OWNER_TOKEN},
    ).status_code == 200

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
