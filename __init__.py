"""Hermes Human Gate plugin registration."""

from __future__ import annotations

import json
from typing import Any

try:
    from .human_gate.effects import demo_effect_handler
    from .human_gate.gate import GateDecision, HumanGate
    from .human_gate.models import Decision as Decision
    from .human_gate.models import RequestRecord, RequestState
    from .human_gate.paths import resolve_db_path
    from .human_gate.policy import PolicyRegistry, ToolPolicy
    from .human_gate.schemas import DEMO_EFFECT, GET, LIST
    from .human_gate.store import GateStore
except ImportError:  # pragma: no cover - repository-level plugin doctor import
    from human_gate.effects import demo_effect_handler
    from human_gate.gate import GateDecision, HumanGate
    from human_gate.models import Decision as Decision
    from human_gate.models import RequestRecord, RequestState
    from human_gate.paths import resolve_db_path
    from human_gate.policy import PolicyRegistry, ToolPolicy
    from human_gate.schemas import DEMO_EFFECT, GET, LIST
    from human_gate.store import GateStore

_gate: HumanGate | None = None


def _request_payload(record: RequestRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "profile": record.profile,
        "session_id": record.session_id,
        "session_lineage": record.session_lineage,
        "tool_name": record.tool_name,
        "effect_kind": record.effect_kind,
        "call_digest": record.call_digest,
        "display": record.display,
        "replay": record.replay,
        "state": record.state.value,
        "resume_state": record.resume_state.value,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "claimed_at": record.claimed_at,
        "completed_at": record.completed_at,
    }


def _require_gate() -> HumanGate:
    if _gate is None:
        raise RuntimeError("Human Gate is not initialized")
    return _gate


def _list_handler(args: dict[str, Any], **_: Any) -> str:
    gate = _require_gate()
    raw_state = str(args.get("state") or "").strip()
    states: tuple[RequestState, ...] | None = None
    if raw_state:
        try:
            states = (RequestState(raw_state),)
        except ValueError:
            return json.dumps({"ok": False, "error": f"unknown state: {raw_state}"})
    limit = args.get("limit", 50)
    if isinstance(limit, bool) or not isinstance(limit, int):
        return json.dumps({"ok": False, "error": "limit must be an integer"})
    records = gate.store.list_requests(states=states, limit=limit)
    return json.dumps(
        {"ok": True, "requests": [_request_payload(record) for record in records]},
        sort_keys=True,
    )


def _get_handler(args: dict[str, Any], **_: Any) -> str:
    request_id = str(args.get("request_id") or "").strip()
    record = _require_gate().store.get_request(request_id)
    if record is None:
        return json.dumps({"ok": False, "error": "request not found"})
    return json.dumps({"ok": True, "request": _request_payload(record)}, sort_keys=True)


def _session_lineage(session_id: str) -> str:
    normalized = str(session_id or "").strip()
    return normalized or "unroutable-session"


def _pre_tool_call(
    tool_name: str,
    args: dict[str, Any] | None = None,
    *,
    session_id: str = "",
    **_: Any,
) -> dict[str, str] | None:
    gate = _require_gate()
    values = dict(args) if isinstance(args, dict) else {}
    if gate.policies.get(tool_name) is None:
        return None
    try:
        result = gate.intercept(
            tool_name,
            values,
            session_id=session_id,
            session_lineage=_session_lineage(session_id),
        )
    except Exception as exc:
        return {
            "action": "block",
            "message": json.dumps(
                {
                    "ok": False,
                    "status": "human_gate_unavailable",
                    "error_type": type(exc).__name__,
                },
                sort_keys=True,
            ),
        }
    if result.decision in {GateDecision.ALLOW, GateDecision.APPROVED}:
        return None
    return {
        "action": "block",
        "message": json.dumps(
            {
                "ok": False,
                "status": "pending_approval",
                "request_id": result.request_id,
                "tool_name": tool_name,
            },
            sort_keys=True,
        ),
    }


def _tool_execution(
    tool_name: str,
    args: dict[str, Any] | None = None,
    *,
    next_call: Any,
    session_id: str = "",
    **_: Any,
) -> Any:
    gate = _require_gate()
    values = dict(args) if isinstance(args, dict) else {}
    if gate.policies.get(tool_name) is None:
        return next_call(values)
    try:
        result = gate.execute(
            tool_name,
            values,
            session_id=session_id,
            session_lineage=_session_lineage(session_id),
            next_call=next_call,
        )
    except Exception as exc:
        return json.dumps(
            {
                "ok": False,
                "status": "human_gate_unavailable",
                "error_type": type(exc).__name__,
            },
            sort_keys=True,
        )
    if isinstance(result, dict):
        return json.dumps(result, sort_keys=True)
    return result


def register(ctx: Any) -> None:
    """Register durable tools, policy hook, and execution claim middleware."""
    global _gate
    policies = PolicyRegistry()
    policies.register(
        ToolPolicy(
            tool_name="human_gate_demo_effect",
            effect_kind="demo",
            display_fields=("message",),
            replay_fields=("message",),
        )
    )
    _gate = HumanGate(
        GateStore(resolve_db_path(fallback_data_dir=ctx.state.data_dir)),
        policies,
        profile=str(getattr(ctx, "profile_name", "default") or "default"),
    )
    ctx.register_tool(
        name="human_gate_list",
        toolset="human_gate",
        schema=LIST,
        handler=_list_handler,
        emoji="✋",
    )
    ctx.register_tool(
        name="human_gate_get",
        toolset="human_gate",
        schema=GET,
        handler=_get_handler,
        emoji="✋",
    )
    ctx.register_tool(
        name="human_gate_demo_effect",
        toolset="human_gate",
        schema=DEMO_EFFECT,
        handler=demo_effect_handler,
        emoji="✋",
    )
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_middleware("tool_execution", _tool_execution)
