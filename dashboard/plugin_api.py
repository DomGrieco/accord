from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE_DIR = _PLUGIN_ROOT / "human_gate"
if not _PACKAGE_DIR.is_dir():
    raise ImportError("Human Gate package is missing from the plugin root")

_plugin_root_text = str(_PLUGIN_ROOT)
sys.path[:] = [entry for entry in sys.path if entry != _plugin_root_text]
sys.path.insert(0, _plugin_root_text)

import human_gate as _human_gate  # noqa: E402

_package_file = getattr(_human_gate, "__file__", None)
if _package_file is None or not Path(_package_file).resolve().is_relative_to(_PLUGIN_ROOT):
    raise ImportError("Human Gate resolved outside the plugin root")

from human_gate.models import Decision, RequestRecord, RequestState  # noqa: E402
from human_gate.paths import resolve_db_path  # noqa: E402
from human_gate.store import ConflictError, GateStore  # noqa: E402


class OwnerTokenBody(BaseModel):
    token: str = Field(min_length=32, max_length=4096)


class ResumeAttemptBody(OwnerTokenBody):
    record_version: int = Field(ge=1)


class ResumeFailedBody(ResumeAttemptBody):
    error: str = Field(min_length=1, max_length=2000)


class ResumeTargetBody(ResumeAttemptBody):
    session_id: str = Field(min_length=1, max_length=256)


class DecisionBody(OwnerTokenBody):
    decision: Literal["approve", "deny", "comment", "cancel"]
    comment: str = Field(default="", max_length=20_000)
    digest: str = Field(min_length=64, max_length=64)
    record_version: int = Field(ge=1)


def default_db_path() -> Path:
    return resolve_db_path()


def _record(record: RequestRecord, audit: list[dict[str, Any]]) -> dict[str, object]:
    return {
        "id": record.id,
        "record_version": record.record_version,
        "profile": record.profile,
        "stored_session_id": record.session_lineage,
        "tool_name": record.tool_name,
        "effect_kind": record.effect_kind,
        "call_digest": record.call_digest,
        "display": record.display,
        "state": record.state.value,
        "resume_state": record.resume_state.value,
        "resume_error": record.resume_error,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "audit": audit,
    }


def _resume_prompt(record: RequestRecord, decision: Decision, comment: str) -> str:
    owner_words = comment.strip()
    if decision is Decision.APPROVE:
        prompt = (
            f"Human Gate request {record.id} was approved by the owner. "
            "Retry the exact original tool call once without changing its arguments. "
            "Do not improvise another consequential action."
        )
    elif decision is Decision.COMMENT:
        prompt = (
            f"Human Gate request {record.id} needs changes from the owner. "
            f"Owner comment: {owner_words} "
            "Revise the proposal. Do not replay the old call or its digest. "
            "Any revised consequential tool call requires a new approval request."
        )
    elif decision is Decision.DENY:
        prompt = f"Human Gate request {record.id} was denied by the owner. Do not execute it."
    else:
        prompt = f"Human Gate request {record.id} was cancelled. Do not execute it."
    if owner_words and decision is not Decision.COMMENT:
        prompt = f"{prompt} Owner reason: {owner_words}"
    if not prompt.strip():
        raise ValueError("decision prompt must not be empty")
    return prompt


def _decision_envelope(record: RequestRecord, decision: Decision, comment: str) -> dict[str, str]:
    return {
        "stored_session_id": record.session_id,
        "profile": record.profile,
        "request_id": record.id,
        "decision": decision.value,
        "prompt": _resume_prompt(record, decision, comment),
    }


def _resume(record: RequestRecord, decision: Decision, comment: str) -> dict[str, str | int] | None:
    return {
        "stored_session_id": record.session_id,
        "profile": record.profile,
        "display_kind": "hidden",
        "request_id": record.id,
        "record_version": record.record_version,
        "prompt": _resume_prompt(record, decision, comment),
    }


def _terminate(record: RequestRecord, decision: Decision) -> dict[str, str] | None:
    del record, decision
    return None


def _authorize(store: GateStore, token: str) -> None:
    if not store.verify_owner_token(token):
        raise HTTPException(status_code=403, detail="owner token rejected")


def build_router(path: str | Path) -> APIRouter:
    db_path = Path(path)
    api = APIRouter()

    @api.post("/owner/register")
    async def register_owner(body: OwnerTokenBody) -> dict[str, bool]:
        store = GateStore(db_path)
        try:
            if not store.register_owner_token(body.token):
                raise HTTPException(status_code=409, detail="owner already registered")
            return {"ok": True}
        finally:
            store.close()

    @api.get("/requests")
    async def list_requests(state: str = "pending", limit: int = 100) -> dict[str, object]:
        try:
            states = tuple(RequestState(item.strip()) for item in state.split(",") if item.strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid request state") from exc
        store = GateStore(db_path)
        try:
            items = store.list_requests(states=states or None, limit=limit)
            histories = store.audit_history_for_requests([item.id for item in items])
            return {"requests": [_record(item, histories[item.id]) for item in items]}
        finally:
            store.close()

    @api.get("/requests/{request_id}")
    async def get_request(request_id: str) -> dict[str, object]:
        store = GateStore(db_path)
        try:
            record = store.get_request(request_id)
            if record is None:
                raise HTTPException(status_code=404, detail="request not found")
            return {"request": _record(record, store.audit_history(record.id))}
        finally:
            store.close()

    @api.post("/requests/{request_id}/decision")
    async def decide_request(request_id: str, body: DecisionBody) -> dict[str, object]:
        store = GateStore(db_path)
        try:
            _authorize(store, body.token)
            decision = Decision(body.decision)
            try:
                record = store.decide(
                    request_id,
                    decision,
                    actor_id="desktop-owner",
                    comment=body.comment,
                    expected_digest=body.digest,
                    expected_record_version=body.record_version,
                )
            except ConflictError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {
                "request": _record(record, store.audit_history(record.id)),
                "resume": None,
                "terminate": _terminate(record, decision),
                "decision_envelope": _decision_envelope(record, decision, body.comment),
            }
        finally:
            store.close()

    @api.post("/requests/{request_id}/resume-ack")
    async def resume_ack(request_id: str, body: ResumeAttemptBody) -> dict[str, object]:
        store = GateStore(db_path)
        try:
            _authorize(store, body.token)
            try:
                record = store.mark_resume_delivered(
                    request_id,
                    expected_record_version=body.record_version,
                )
            except ConflictError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {"request": _record(record, store.audit_history(record.id))}
        finally:
            store.close()

    @api.post("/requests/{request_id}/resume-target")
    async def resume_target(request_id: str, body: ResumeTargetBody) -> dict[str, object]:
        store = GateStore(db_path)
        try:
            _authorize(store, body.token)
            try:
                lineage = store.register_session_continuation(
                    request_id,
                    body.session_id,
                    expected_record_version=body.record_version,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="request not found") from exc
            except ConflictError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {"request_id": request_id, "session_id": body.session_id, "lineage": lineage}
        finally:
            store.close()

    @api.post("/requests/{request_id}/resume-failed")
    async def resume_failed(request_id: str, body: ResumeFailedBody) -> dict[str, object]:
        store = GateStore(db_path)
        try:
            _authorize(store, body.token)
            try:
                record = store.mark_resume_failed(
                    request_id,
                    error=body.error,
                    expected_record_version=body.record_version,
                )
            except ConflictError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {"request": _record(record, store.audit_history(record.id))}
        finally:
            store.close()

    @api.post("/requests/{request_id}/resume-instruction")
    async def resume_instruction(request_id: str, body: OwnerTokenBody) -> dict[str, object]:
        store = GateStore(db_path)
        try:
            _authorize(store, body.token)
            try:
                record = store.begin_resume_delivery(request_id)
            except ConflictError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            latest = store.latest_decision(request_id)
            if latest is None:
                raise HTTPException(status_code=409, detail="request has no decision")
            decision, comment = latest
            return {
                "request": _record(record, store.audit_history(record.id)),
                "resume": _resume(record, decision, comment),
            }
        finally:
            store.close()

    @api.post("/requests/{request_id}/termination-instruction")
    async def termination_instruction(request_id: str, body: OwnerTokenBody) -> dict[str, object]:
        store = GateStore(db_path)
        try:
            _authorize(store, body.token)
            record = store.get_request(request_id)
            if record is None:
                raise HTTPException(status_code=404, detail="request not found")
            latest = store.latest_decision(request_id)
            if latest is None or latest[0] is not Decision.DENY:
                raise HTTPException(status_code=409, detail="request was not denied")
            return {
                "request": _record(record, store.audit_history(record.id)),
                "terminate": _terminate(record, Decision.DENY),
            }
        finally:
            store.close()

    return api


router = build_router(default_db_path())
