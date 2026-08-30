from __future__ import annotations

import contextlib
import io
import json
import sys
import threading
import time
from collections.abc import Mapping
from hashlib import sha256
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
from human_gate.policy import is_safe_projection_field, policies_from_config  # noqa: E402
from human_gate.store import ConflictError, GateStore  # noqa: E402

_POLICY_CONFIG_PATH = "plugins.entries.human-gate.settings.policies"
_SETTINGS_LOCK = threading.RLock()
_MAX_OPTION_TOOLS = 512
_MAX_OPTION_FIELDS = 64
_EFFECT_KIND_PRESETS = ("consequential_write", "external_publish", "local_command")


@contextlib.contextmanager
def _cross_process_settings_lock():
    """Serialize policy compare-and-write operations across dashboard processes."""
    from hermes_constants import get_config_path

    lock_path = get_config_path().with_name("config.yaml.human-gate.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        deadline = time.monotonic() + 10.0
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    lock_file.seek(0, 2)
                    if lock_file.tell() == 0:
                        lock_file.write(b"0")
                        lock_file.flush()
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError, PermissionError) as exc:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "timed out waiting for the Human Gate settings lock"
                    ) from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            if sys.platform == "win32":
                import msvcrt

                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


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


class PolicySettingsBody(OwnerTokenBody):
    expected_digest: str = Field(min_length=64, max_length=64)
    policies: list[dict[str, Any]] = Field(max_length=64)


def default_db_path() -> Path:
    return resolve_db_path()


def _normalize_policy_config(raw: Any) -> list[dict[str, Any]]:
    policies_from_config(raw)
    encoded = json.dumps(raw, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    decoded = json.loads(encoded)
    if not isinstance(decoded, list) or not all(isinstance(item, dict) for item in decoded):
        raise ValueError("policies must be a list of objects")
    return decoded


def _policy_digest(policies: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        policies,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _settings_response(policies: list[dict[str, Any]]) -> dict[str, object]:
    return {
        "policies": policies,
        "digest": _policy_digest(policies),
        "restart_required": True,
    }


def _bounded_option_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _bounded_option_identifier(value: Any, limit: int = 128) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        return ""
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return ""
    return value


def _build_settings_options(
    available_tools: list[dict[str, Any]],
    policies: list[dict[str, Any]],
) -> dict[str, object]:
    effect_kinds: set[str] = set(_EFFECT_KIND_PRESETS)
    for policy in policies:
        effect_kind = _bounded_option_identifier(policy.get("effect_kind"))
        if effect_kind:
            effect_kinds.add(effect_kind)

    tools: list[dict[str, object]] = []
    seen_names: set[str] = set()
    ordered_tools = sorted(
        available_tools,
        key=lambda tool: (
            _bounded_option_identifier(tool.get("name")),
            _bounded_option_identifier(tool.get("toolset")),
        ),
    )
    for tool in ordered_tools:
        name = _bounded_option_identifier(tool.get("name"))
        if not name or name in seen_names:
            continue
        parameters = tool.get("parameters")
        properties = parameters.get("properties") if isinstance(parameters, Mapping) else None
        fields = (
            sorted(field for field in properties if is_safe_projection_field(field))[
                :_MAX_OPTION_FIELDS
            ]
            if isinstance(properties, Mapping)
            else []
        )
        tools.append(
            {
                "name": name,
                "toolset": _bounded_option_identifier(tool.get("toolset")),
                "description": _bounded_option_text(tool.get("description"), 240),
                "fields": fields,
            }
        )
        seen_names.add(name)
        if len(tools) >= _MAX_OPTION_TOOLS:
            break

    payload: dict[str, object] = {
        "effect_kinds": sorted(effect_kinds),
        "tools": tools,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return {"digest": sha256(encoded).hexdigest(), **payload}


def _available_tool_definitions() -> list[dict[str, Any]]:
    from hermes_cli.plugins import discover_plugins
    from tools.registry import discover_builtin_tools, registry

    # The Desktop dashboard can run without importing model_tools, whose module
    # startup normally populates the process-local registry. Discover explicitly
    # so this endpoint reflects built-in and enabled plugin tools in that process.
    discover_builtin_tools()
    discover_plugins()

    names = set(registry.get_all_tool_names())
    definitions = registry.get_definitions(names, quiet=True)
    available: list[dict[str, Any]] = []
    for definition in definitions:
        function = definition.get("function") if isinstance(definition, Mapping) else None
        if not isinstance(function, Mapping):
            continue
        name = function.get("name")
        entry = registry.get_entry(name) if isinstance(name, str) else None
        available.append(
            {
                "name": name,
                "toolset": entry.toolset if entry is not None else "",
                "description": function.get("description"),
                "parameters": function.get("parameters"),
            }
        )
    return available


def _settings_options() -> dict[str, object]:
    return _build_settings_options(_available_tool_definitions(), _read_policy_config())


def _read_policy_config() -> list[dict[str, Any]]:
    from hermes_cli.config import load_config_readonly

    config = load_config_readonly() or {}
    plugins = config.get("plugins") if isinstance(config, Mapping) else None
    entries = plugins.get("entries") if isinstance(plugins, Mapping) else None
    entry = entries.get("human-gate") if isinstance(entries, Mapping) else None
    if not isinstance(entry, Mapping):
        return []
    settings = entry.get("settings")
    legacy = entry.get("config")
    if isinstance(settings, Mapping) and "policies" in settings:
        raw = settings.get("policies")
    elif isinstance(legacy, Mapping):
        raw = legacy.get("policies", [])
    else:
        raw = []
    return _normalize_policy_config(raw)


def _write_policy_config(
    policies: list[dict[str, Any]],
    expected_digest: str,
) -> list[dict[str, Any]]:
    normalized = _normalize_policy_config(policies)
    with _SETTINGS_LOCK, _cross_process_settings_lock():
        current = _read_policy_config()
        if _policy_digest(current) != expected_digest:
            raise ConflictError("policy settings changed; reload before saving")
        from hermes_cli.config import set_config_value

        sink = io.StringIO()
        try:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                set_config_value(
                    _POLICY_CONFIG_PATH,
                    json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
                    force=True,
                )
        except SystemExit as exc:
            raise RuntimeError("Hermes rejected the policy settings update") from exc
        saved = _read_policy_config()
        if saved != normalized:
            raise RuntimeError("policy settings could not be verified after saving")
        return saved


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

    @api.post("/settings/read")
    async def read_settings(body: OwnerTokenBody) -> dict[str, object]:
        store = GateStore(db_path)
        try:
            _authorize(store, body.token)
        finally:
            store.close()
        try:
            return _settings_response(_read_policy_config())
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.post("/settings/options")
    async def read_settings_options(body: OwnerTokenBody) -> dict[str, object]:
        store = GateStore(db_path)
        try:
            _authorize(store, body.token)
        finally:
            store.close()
        try:
            return _settings_options()
        except (ImportError, RuntimeError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @api.put("/settings/policies")
    async def write_settings(body: PolicySettingsBody) -> dict[str, object]:
        store = GateStore(db_path)
        try:
            _authorize(store, body.token)
        finally:
            store.close()
        try:
            normalized = _normalize_policy_config(body.policies)
            saved = _write_policy_config(normalized, body.expected_digest)
            return _settings_response(saved)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ImportError, PermissionError, RuntimeError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

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
