"""Hermes Human Gate plugin registration."""

from __future__ import annotations

import json
from typing import Any

try:
    from .human_gate.claims import active_claim_matches
    from .human_gate.effects import (
        demo_effect_handler,
        mock_publish_handler,
    )
    from .human_gate.gate import GateDecision, HumanGate
    from .human_gate.models import Decision as Decision
    from .human_gate.models import RequestRecord, RequestState
    from .human_gate.paths import resolve_db_path
    from .human_gate.policy import ToolPolicy, policies_from_config
    from .human_gate.runtime_registry import get_process_runtime_state
    from .human_gate.schemas import DEMO_EFFECT, GET, LIST, MOCK_PUBLISH, X_CREATE_POST
    from .human_gate.store import GateStore
    from .human_gate.xurl_adapter import (
        XPublisherConfig,
        terminal_xurl_write_handler,
        terminal_xurl_write_is_unsupported,
        terminal_xurl_write_matches,
        terminal_xurl_write_projection,
        x_create_post_handler,
        x_publisher_config,
    )
except ImportError:  # pragma: no cover - repository-level plugin doctor import
    from human_gate.claims import active_claim_matches
    from human_gate.effects import (
        demo_effect_handler,
        mock_publish_handler,
    )
    from human_gate.gate import GateDecision, HumanGate
    from human_gate.models import Decision as Decision
    from human_gate.models import RequestRecord, RequestState
    from human_gate.paths import resolve_db_path
    from human_gate.policy import ToolPolicy, policies_from_config
    from human_gate.runtime_registry import get_process_runtime_state
    from human_gate.schemas import DEMO_EFFECT, GET, LIST, MOCK_PUBLISH, X_CREATE_POST
    from human_gate.store import GateStore
    from human_gate.xurl_adapter import (
        XPublisherConfig,
        terminal_xurl_write_handler,
        terminal_xurl_write_is_unsupported,
        terminal_xurl_write_matches,
        terminal_xurl_write_projection,
        x_create_post_handler,
        x_publisher_config,
    )

_runtime_state = get_process_runtime_state()
_gate: HumanGate | None = _runtime_state.gate
_registration_key: tuple[Any, ...] | None = _runtime_state.registration_key
_registration_lock = _runtime_state.lock


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
    gate = _runtime_state.gate
    if gate is None:
        raise RuntimeError("Human Gate plugin is not initialized")
    return gate


def _current_x_config() -> XPublisherConfig:
    _gate_snapshot, config = _runtime_snapshot()
    return config


def _runtime_snapshot() -> tuple[HumanGate, XPublisherConfig]:
    with _runtime_state.lock:
        gate = _runtime_state.gate
        config = _runtime_state.x_config
    if not isinstance(gate, HumanGate):
        raise RuntimeError("Human Gate plugin is not initialized")
    if not isinstance(config, XPublisherConfig):
        raise RuntimeError("Human Gate X configuration is unavailable")
    return gate, config


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


def _session_identity(*, session_id: str, session_key: str) -> str | None:
    """Return the stable stored-session identity when Hermes provides one."""
    stable = str(session_key or "").strip()
    if stable:
        return stable
    runtime_or_legacy = str(session_id or "").strip()
    return runtime_or_legacy or None


def _unroutable_payload(tool_name: str) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "human_gate_unroutable",
        "tool_name": tool_name,
    }


def _unsupported_xurl_payload() -> dict[str, Any]:
    return {
        "ok": False,
        "status": "human_gate_xurl_unsupported",
        "tool_name": "terminal",
    }


def _pre_tool_call(
    tool_name: str,
    args: dict[str, Any] | None = None,
    *,
    session_id: str = "",
    session_key: str = "",
    **_: Any,
) -> dict[str, str] | None:
    gate = _require_gate()
    values = dict(args) if isinstance(args, dict) else {}
    if tool_name == "terminal" and terminal_xurl_write_is_unsupported(values):
        return {
            "action": "block",
            "message": json.dumps(_unsupported_xurl_payload(), sort_keys=True),
        }
    if gate.policies.match(tool_name, values) is None:
        return None
    stable_session_id = _session_identity(
        session_id=session_id,
        session_key=session_key,
    )
    if stable_session_id is None:
        return {
            "action": "block",
            "message": json.dumps(_unroutable_payload(tool_name), sort_keys=True),
        }
    # Hermes runs the execution middleware before its nested pre-tool hook,
    # sometimes in a separate authorization context. Once this gate has
    # atomically claimed the exact call, that hook must let the same call reach
    # the owned handler that consumes the claim. Other invocations cannot reach
    # this nested hook because their middleware finds no approved request.
    if active_claim_matches(tool_name, values) or gate.has_matching_claimed(
        tool_name,
        values,
        session_lineage=stable_session_id,
    ):
        return None
    try:
        result = gate.intercept(
            tool_name,
            values,
            session_id=stable_session_id,
            session_lineage=stable_session_id,
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
    session_key: str = "",
    **_: Any,
) -> Any:
    gate, x_config = _runtime_snapshot()
    values = dict(args) if isinstance(args, dict) else {}
    if tool_name == "terminal" and terminal_xurl_write_is_unsupported(values):
        return json.dumps(_unsupported_xurl_payload(), sort_keys=True)
    try:
        policy = gate.policies.match(tool_name, values)
    except Exception as exc:
        return json.dumps(
            {
                "ok": False,
                "status": "human_gate_unavailable",
                "error_type": type(exc).__name__,
            },
            sort_keys=True,
        )
    if policy is None:
        return next_call(values)
    stable_session_id = _session_identity(
        session_id=session_id,
        session_key=session_key,
    )
    if stable_session_id is None:
        return json.dumps(_unroutable_payload(tool_name), sort_keys=True)
    try:
        if tool_name == "terminal" and policy.owned_effect:

            def effect_call(payload: dict[str, Any]) -> Any:
                return terminal_xurl_write_handler(x_config, payload)
        elif tool_name == "x_create_post" and policy.owned_effect:

            def effect_call(payload: dict[str, Any]) -> Any:
                return x_create_post_handler(x_config, payload)
        else:
            effect_call = next_call
        result = gate.execute(
            tool_name,
            values,
            session_id=stable_session_id,
            session_lineage=stable_session_id,
            next_call=effect_call,
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
    global _gate, _registration_key
    policies = policies_from_config(ctx.get_config("policies", []))
    x_config = x_publisher_config(ctx.get_config("x", {}))

    policies.register(
        ToolPolicy(
            tool_name="human_gate_demo_effect",
            effect_kind="demo",
            display_fields=("message",),
            replay_fields=("message",),
            owned_effect=True,
            owned_handler_identity="human_gate.demo_effect.v1",
        )
    )
    policies.register(
        ToolPolicy(
            tool_name="human_gate_mock_publish",
            effect_kind="publish",
            display_fields=("destination", "text", "media_sha256", "simulate_outcome"),
            replay_fields=("destination", "text", "media_sha256", "simulate_outcome"),
            owned_effect=True,
            owned_handler_identity="human_gate.mock_publish.v1",
        )
    )
    policies.register(
        ToolPolicy(
            tool_name="x_create_post",
            effect_kind="publish",
            display_fields=("account", "text", "quote_post_id"),
            replay_fields=("account", "text", "quote_post_id"),
            owned_effect=True,
            owned_handler_identity="human_gate.x_create_post.v1",
            owned_execution_identity=x_config.execution_identity(),
        )
    )
    policies.register(
        ToolPolicy(
            tool_name="terminal",
            effect_kind="publish",
            owned_effect=True,
            matcher_identity="human_gate.terminal_xurl_write_match.v1",
            display_projector_identity="human_gate.terminal_xurl_write_display.v1",
            replay_projector_identity="human_gate.empty_replay.v1",
            owned_handler_identity="human_gate.terminal_xurl_write.v1",
            owned_execution_identity=x_config.execution_identity(),
            argument_matcher=terminal_xurl_write_matches,
            display_projector=terminal_xurl_write_projection,
            replay_projector=lambda _args: {},
        )
    )
    terminal_is_already_fully_gated = any(
        policy.tool_name == "terminal"
        and not policy.tool_name_is_glob
        and policy.argument_matcher is None
        for policy in policies.all()
    )
    if x_config.enabled and not terminal_is_already_fully_gated:
        policies.register(
            ToolPolicy(
                tool_name="terminal",
                effect_kind="x_raw_bypass_guard",
                display_fields=("command", "workdir"),
                replay_fields=("command", "workdir"),
                matcher_identity="human_gate.x_raw_bypass_guard.v1",
                argument_matcher=lambda _args: True,
            )
        )
    db_path = resolve_db_path(fallback_data_dir=ctx.state.data_dir)
    profile = str(getattr(ctx, "profile_name", "default") or "default")
    registration_key = (
        profile,
        str(db_path),
        policies.registration_identity(),
    )
    with _registration_lock:
        if _runtime_state.gate is None:
            store = GateStore(db_path)
            try:
                store.acquire_runtime_lock()
                store.recover_claimed_as_uncertain()
            except Exception:
                store.close()
                raise
            _runtime_state.gate = HumanGate(store, policies, profile=profile)
            _runtime_state.registration_key = registration_key
        elif _runtime_state.registration_key != registration_key:
            existing_key = _runtime_state.registration_key
            if existing_key is None or existing_key[:2] != registration_key[:2]:
                raise RuntimeError(
                    "Human Gate runtime cannot change profile or database while active: "
                    f"existing={existing_key!r}, requested={registration_key!r}"
                )
            existing_gate = _runtime_state.gate
            _runtime_state.gate = HumanGate(existing_gate.store, policies, profile=profile)
            _runtime_state.registration_key = registration_key
        gate = _runtime_state.gate
        _runtime_state.x_config = x_config
        if gate is None:  # pragma: no cover - guarded by the branch above
            raise RuntimeError("Human Gate plugin is not initialized")
        _gate = gate
        _registration_key = _runtime_state.registration_key
        store = gate.store
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
    ctx.register_tool(
        name="human_gate_mock_publish",
        toolset="human_gate",
        schema=MOCK_PUBLISH,
        handler=lambda args, **kwargs: mock_publish_handler(store, args, **kwargs),
        emoji="✋",
    )
    ctx.register_tool(
        name="x_create_post",
        toolset="terminal",
        schema=X_CREATE_POST,
        handler=lambda args, **kwargs: x_create_post_handler(_current_x_config(), args, **kwargs),
        emoji="✋",
    )
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_middleware("tool_execution", _tool_execution)
