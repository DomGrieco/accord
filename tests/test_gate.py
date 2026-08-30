from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from human_gate.errors import EffectDefinitiveFailureError, EffectUncertainError
from human_gate.gate import GateDecision, HumanGate
from human_gate.models import Decision, RequestState
from human_gate.policy import (
    PolicyRegistry,
    ToolPolicy,
    is_safe_projection_field,
    policies_from_config,
)
from human_gate.store import GateStore


def test_safe_projection_field_uses_policy_validation_rules() -> None:
    assert is_safe_projection_field("record_id")
    assert is_safe_projection_field("media.digest")
    assert not is_safe_projection_field("api_key")
    assert not is_safe_projection_field("sessionToken")
    assert not is_safe_projection_field("invalid field")


def build_gate(tmp_path: Path) -> HumanGate:
    policies = PolicyRegistry()
    policies.register(
        ToolPolicy(
            tool_name="x_create_post",
            effect_kind="publish",
            display_fields=("text", "quote_post_id"),
            replay_fields=("text", "quote_post_id"),
        )
    )
    return HumanGate(GateStore(tmp_path / "gate.db"), policies, profile="life")


def test_unmatched_tool_is_allowed(tmp_path: Path) -> None:
    gate = build_gate(tmp_path)

    result = gate.intercept(
        "read_file",
        {"path": "README.md"},
        session_id="runtime",
        session_lineage="stored",
    )

    assert result.decision is GateDecision.ALLOW
    assert result.request_id is None


def test_explicit_policy_config_gates_a_post_tool_with_narrow_display_fields() -> None:
    policies = policies_from_config(
        [
            {
                "tool_name": "x_create_post",
                "effect_kind": "publish",
                "display_fields": ["account", "text"],
                "replay_fields": ["account", "text"],
            }
        ]
    )

    policy = policies.get("x_create_post")

    assert policy is not None
    assert policy.display_projection(
        {"account": "fixture", "text": "hello", "credential": "never persist"}
    ) == {"account": "fixture", "text": "hello"}


def test_terminal_policy_config_matches_exact_and_glob_commands_only() -> None:
    policies = policies_from_config(
        [
            {
                "tool_name": "terminal",
                "effect_kind": "local_fixture",
                "display_fields": ["command"],
                "replay_fields": ["command"],
                "command_exact": ["printf human-gate-fixture"],
                "command_glob": ["touch /tmp/human-gate-fixture-*"],
            }
        ]
    )

    assert policies.match("terminal", {"command": "printf human-gate-fixture"}) is not None
    assert policies.match("terminal", {"command": "touch /tmp/human-gate-fixture-42"}) is not None
    assert policies.match("terminal", {"command": "printf unrelated"}) is None
    assert policies.match("other_tool", {"command": "printf human-gate-fixture"}) is None


def test_policy_registration_identity_normalizes_patterns_and_tracks_enforcement() -> None:
    first = policies_from_config(
        [
            {
                "tool_name": "terminal",
                "effect_kind": "local_fixture",
                "display_fields": ["command", "workdir"],
                "replay_fields": ["command"],
                "command_exact": ["printf B", "printf A"],
                "command_glob": ["touch /tmp/b-*", "touch /tmp/a-*"],
            }
        ]
    )
    reordered = policies_from_config(
        [
            {
                "tool_name": "terminal",
                "effect_kind": "local_fixture",
                "display_fields": ["workdir", "command"],
                "replay_fields": ["command"],
                "command_exact": ["printf A", "printf B"],
                "command_glob": ["touch /tmp/a-*", "touch /tmp/b-*"],
            }
        ]
    )
    changed_command = policies_from_config(
        [
            {
                "tool_name": "terminal",
                "effect_kind": "local_fixture",
                "display_fields": ["command", "workdir"],
                "replay_fields": ["command"],
                "command_exact": ["printf C"],
                "command_glob": ["touch /tmp/a-*", "touch /tmp/b-*"],
            }
        ]
    )
    changed_projection = policies_from_config(
        [
            {
                "tool_name": "terminal",
                "effect_kind": "local_fixture",
                "display_fields": ["command"],
                "replay_fields": ["command"],
                "command_exact": ["printf A", "printf B"],
                "command_glob": ["touch /tmp/a-*", "touch /tmp/b-*"],
            }
        ]
    )

    assert first.registration_identity() == reordered.registration_identity()
    assert first.registration_identity() != changed_command.registration_identity()
    assert first.registration_identity() != changed_projection.registration_identity()
    exact_command_mode = policies_from_config(
        [
            {
                "tool_name": "terminal",
                "effect_kind": "local_fixture",
                "command_exact": ["touch /tmp/fixture"],
            }
        ]
    )
    glob_command_mode = policies_from_config(
        [
            {
                "tool_name": "terminal",
                "effect_kind": "local_fixture",
                "command_glob": ["touch /tmp/fixture"],
            }
        ]
    )
    assert exact_command_mode.registration_identity() != glob_command_mode.registration_identity()
    exact_tool = policies_from_config([{"tool_name": "records_delete", "effect_kind": "archive"}])
    glob_tool = policies_from_config([{"tool_glob": "records_delete", "effect_kind": "archive"}])
    assert exact_tool.registration_identity() != glob_tool.registration_identity()


def test_owned_handler_identity_changes_policy_registration_identity() -> None:
    first = PolicyRegistry()
    first.register(
        ToolPolicy(
            tool_name="owned_effect",
            effect_kind="publish",
            owned_effect=True,
            owned_handler_identity="handler-v1",
        )
    )
    second = PolicyRegistry()
    second.register(
        ToolPolicy(
            tool_name="owned_effect",
            effect_kind="publish",
            owned_effect=True,
            owned_handler_identity="handler-v2",
        )
    )

    assert first.registration_identity() != second.registration_identity()


def test_owned_execution_identity_changes_policy_fingerprint_and_rejects_secrets() -> None:
    first = ToolPolicy(
        tool_name="x_create_post",
        effect_kind="publish",
        owned_effect=True,
        owned_handler_identity="human_gate.x_create_post.v1",
        owned_execution_identity=(("enabled", True), ("app", "app_a"), ("account", "fixture")),
    )
    second = ToolPolicy(
        tool_name="x_create_post",
        effect_kind="publish",
        owned_effect=True,
        owned_handler_identity="human_gate.x_create_post.v1",
        owned_execution_identity=(("enabled", True), ("app", "app_b"), ("account", "fixture")),
    )

    assert first.fingerprint() != second.fingerprint()
    with pytest.raises(ValueError, match="secret-bearing"):
        ToolPolicy(
            tool_name="x_create_post",
            effect_kind="publish",
            owned_effect=True,
            owned_handler_identity="human_gate.x_create_post.v1",
            owned_execution_identity=(("access_token", "must-not-bind"),),
        )


def test_tool_policy_config_matches_tool_name_glob() -> None:
    policies = policies_from_config(
        [
            {
                "tool_glob": "records_*",
                "effect_kind": "records_write",
                "display_fields": ["record_id"],
                "replay_fields": ["record_id"],
            }
        ]
    )

    assert policies.match("records_archive", {"record_id": "fixture"}) is not None
    assert policies.match("records_delete", {"record_id": "fixture"}) is not None
    assert policies.match("record_read", {"record_id": "fixture"}) is None


def test_exact_tool_and_terminal_command_policies_outrank_globs() -> None:
    policies = policies_from_config(
        [
            {"tool_glob": "records_*", "effect_kind": "glob"},
            {"tool_name": "records_delete", "effect_kind": "exact"},
            {
                "tool_name": "terminal",
                "effect_kind": "command_glob",
                "command_glob": ["touch /tmp/*"],
            },
            {
                "tool_name": "terminal",
                "effect_kind": "command_exact",
                "command_exact": ["touch /tmp/exact"],
            },
        ]
    )

    exact_tool = policies.match("records_delete", {})
    exact_command = policies.match("terminal", {"command": "touch /tmp/exact"})
    assert exact_tool is not None
    assert exact_command is not None
    assert exact_tool.effect_kind == "exact"
    assert exact_command.effect_kind == "command_exact"


def test_equal_specificity_overlapping_tool_globs_fail_closed() -> None:
    policies = policies_from_config(
        [
            {"tool_glob": "records_*", "effect_kind": "records"},
            {"tool_glob": "*_delete", "effect_kind": "delete"},
        ]
    )

    with pytest.raises(ValueError, match="ambiguous policies"):
        policies.match("records_delete", {})


def test_duplicate_terminal_exact_command_is_rejected() -> None:
    with pytest.raises(ValueError, match="command selector already registered"):
        policies_from_config(
            [
                {
                    "tool_name": "terminal",
                    "effect_kind": "first",
                    "command_exact": ["printf fixture"],
                },
                {
                    "tool_name": "terminal",
                    "effect_kind": "second",
                    "command_exact": ["printf fixture"],
                },
            ]
        )


def test_policy_config_rejects_secret_bearing_projection_fields() -> None:
    with pytest.raises(ValueError, match="secret-bearing"):
        policies_from_config(
            [
                {
                    "tool_name": "x_create_post",
                    "effect_kind": "publish",
                    "display_fields": ["text", "access_token"],
                    "replay_fields": ["text"],
                }
            ]
        )


def test_first_matching_call_becomes_pending_and_is_blocked(tmp_path: Path) -> None:
    gate = build_gate(tmp_path)

    result = gate.intercept(
        "x_create_post",
        {"text": "hello", "quote_post_id": "123", "secret": "never persist"},
        session_id="runtime",
        session_lineage="stored",
    )

    assert result.decision is GateDecision.PENDING
    request = gate.store.get_request(result.request_id)
    assert request is not None
    assert request.display == {"text": "hello", "quote_post_id": "123"}
    assert request.replay == {"text": "hello", "quote_post_id": "123"}
    assert "never persist" not in request.display_json
    assert "never persist" not in request.replay_json


def test_approved_exact_replay_is_ready_but_changed_call_is_new_pending(tmp_path: Path) -> None:
    gate = build_gate(tmp_path)
    first = gate.intercept(
        "x_create_post",
        {"text": "hello", "quote_post_id": "123"},
        session_id="runtime",
        session_lineage="stored",
    )
    gate.store.decide(first.request_id, Decision.APPROVE, actor_id="owner")

    exact = gate.intercept(
        "x_create_post",
        {"quote_post_id": "123", "text": "hello"},
        session_id="runtime-2",
        session_lineage="stored",
    )
    changed = gate.intercept(
        "x_create_post",
        {"quote_post_id": "123", "text": "hello!"},
        session_id="runtime-2",
        session_lineage="stored",
    )

    assert exact.decision is GateDecision.APPROVED
    assert exact.request_id == first.request_id
    assert changed.decision is GateDecision.PENDING
    assert changed.request_id != first.request_id


def test_requested_revision_creates_new_pending_request_without_old_digest_replay(
    tmp_path: Path,
) -> None:
    gate = build_gate(tmp_path)
    original = gate.intercept(
        "x_create_post",
        {"text": "Original final sentence.", "quote_post_id": "123"},
        session_id="runtime",
        session_lineage="stored",
    )
    assert original.request_id is not None
    gate.store.decide(
        original.request_id,
        Decision.COMMENT,
        actor_id="owner",
        comment="Revise only the final sentence.",
    )

    revised = gate.intercept(
        "x_create_post",
        {"text": "Revised final sentence.", "quote_post_id": "123"},
        session_id="runtime-after-comment",
        session_lineage="stored",
    )

    assert revised.decision is GateDecision.PENDING
    assert revised.request_id is not None
    assert revised.request_id != original.request_id
    original_record = gate.store.get_request(original.request_id)
    revised_record = gate.store.get_request(revised.request_id)
    assert original_record is not None
    assert revised_record is not None
    assert original_record.state is RequestState.CHANGES_REQUESTED
    assert revised_record.state is RequestState.PENDING
    assert revised_record.call_digest != original_record.call_digest
    assert revised_record.replay == {
        "text": "Revised final sentence.",
        "quote_post_id": "123",
    }


def test_continuation_tip_uses_stable_lineage_to_consume_exact_approval(tmp_path: Path) -> None:
    gate = build_gate(tmp_path)
    original = gate.intercept(
        "x_create_post",
        {"text": "hello", "quote_post_id": "123"},
        session_id="runtime",
        session_lineage="stored-root",
    )
    assert original.request_id is not None
    gate.store.decide(original.request_id, Decision.APPROVE, actor_id="owner")
    resume_attempt = gate.store.begin_resume_delivery(original.request_id)
    gate.store.register_session_continuation(
        original.request_id,
        "continuation-tip",
        expected_record_version=resume_attempt.record_version,
    )

    continued = gate.intercept(
        "x_create_post",
        {"text": "hello", "quote_post_id": "123"},
        session_id="runtime-tip",
        session_lineage="continuation-tip",
    )

    assert continued.decision is GateDecision.APPROVED
    assert continued.request_id == original.request_id


def test_execute_claims_before_dispatch_and_records_success(tmp_path: Path) -> None:
    gate = build_gate(tmp_path)
    pending = gate.intercept(
        "x_create_post",
        {"text": "hello", "quote_post_id": None},
        session_id="runtime",
        session_lineage="stored",
    )
    gate.store.decide(pending.request_id, Decision.APPROVE, actor_id="owner")
    calls: list[dict] = []

    result = gate.execute(
        "x_create_post",
        {"text": "hello", "quote_post_id": None},
        session_id="runtime-2",
        session_lineage="stored",
        next_call=lambda args: calls.append(args) or {"ok": True, "post_id": "p1"},
    )

    assert result == {"ok": True, "post_id": "p1"}
    assert calls == [{"text": "hello", "quote_post_id": None}]
    assert gate.store.get_request(pending.request_id).state is RequestState.EXECUTED


def test_execute_without_approval_fails_closed(tmp_path: Path) -> None:
    gate = build_gate(tmp_path)
    calls: list[dict] = []

    result = gate.execute(
        "x_create_post",
        {"text": "hello", "quote_post_id": None},
        session_id="runtime",
        session_lineage="stored",
        next_call=lambda args: calls.append(args) or {"ok": True},
    )

    assert result["ok"] is False
    assert result["status"] == "pending_approval"
    assert calls == []


def test_async_executor_is_awaited_before_marking_executed(tmp_path: Path) -> None:
    gate = build_gate(tmp_path)
    pending = gate.intercept(
        "x_create_post",
        {"text": "hello", "quote_post_id": None},
        session_id="runtime",
        session_lineage="stored",
    )
    assert pending.request_id is not None
    request_id = pending.request_id
    gate.store.decide(request_id, Decision.APPROVE, actor_id="owner")
    calls: list[dict] = []

    async def execute_later(args: dict) -> dict[str, bool]:
        claimed = gate.store.get_request(request_id)
        assert claimed is not None
        assert claimed.state is RequestState.CLAIMED
        calls.append(args)
        return {"ok": True}

    awaitable = gate.execute(
        "x_create_post",
        {"text": "hello", "quote_post_id": None},
        session_id="runtime",
        session_lineage="stored",
        next_call=execute_later,
    )
    claimed_before_await = gate.store.get_request(request_id)
    assert claimed_before_await is not None
    assert claimed_before_await.state is RequestState.CLAIMED

    result = asyncio.run(awaitable)

    assert result == {"ok": True}
    assert calls == [{"text": "hello", "quote_post_id": None}]
    executed = gate.store.get_request(request_id)
    assert executed is not None
    assert executed.state is RequestState.EXECUTED


def test_executor_exception_records_failed_without_retry(tmp_path: Path) -> None:
    gate = build_gate(tmp_path)
    pending = gate.intercept(
        "x_create_post",
        {"text": "hello", "quote_post_id": None},
        session_id="runtime",
        session_lineage="stored",
    )
    gate.store.decide(pending.request_id, Decision.APPROVE, actor_id="owner")
    assert pending.request_id is not None
    replay_only_secret = "REPLAY_ONLY_SECRET"

    def fail(_args: dict):
        raise EffectDefinitiveFailureError(f"provider rejected payload={replay_only_secret}")

    result = gate.execute(
        "x_create_post",
        {"text": "hello", "quote_post_id": None},
        session_id="runtime-2",
        session_lineage="stored",
        next_call=fail,
    )

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert replay_only_secret not in str(result)
    assert gate.store.get_request(pending.request_id).state is RequestState.FAILED
    with sqlite3.connect(tmp_path / "gate.db") as connection:
        persisted_display = connection.execute(
            "SELECT display_json FROM receipts WHERE request_id = ?",
            (pending.request_id,),
        ).fetchone()[0]
    assert replay_only_secret not in persisted_display
    history = gate.store.audit_history(pending.request_id)
    assert history[-1] == {
        "id": history[-1]["id"],
        "event_type": "receipt",
        "outcome": "failed",
        "result_digest": history[-1]["result_digest"],
        "created_at": history[-1]["created_at"],
    }


def test_unclassified_exception_after_dispatch_is_uncertain(tmp_path: Path) -> None:
    gate = build_gate(tmp_path)
    args = {"text": "hello", "quote_post_id": None}
    pending = gate.intercept(
        "x_create_post",
        args,
        session_id="runtime",
        session_lineage="stored",
    )
    assert pending.request_id is not None
    gate.store.decide(pending.request_id, Decision.APPROVE, actor_id="owner")
    effects: list[dict] = []
    secret = "RESPONSE_LOSS_SECRET"

    def lose_response(dispatched_args: dict) -> None:
        effects.append(dispatched_args)
        raise TimeoutError(f"response lost after dispatch: {secret}")

    result = gate.execute(
        "x_create_post",
        args,
        session_id="runtime-2",
        session_lineage="stored",
        next_call=lose_response,
    )

    assert effects == [args]
    assert result["ok"] is False
    assert result["status"] == "uncertain"
    assert secret not in str(result)
    request = gate.store.get_request(pending.request_id)
    assert request is not None
    assert request.state is RequestState.UNCERTAIN
    assert (
        gate.store.find_matching_approved(
            profile="life",
            session_lineage="stored",
            tool_name="x_create_post",
            call_digest=request.call_digest,
        )
        is None
    )
    with sqlite3.connect(tmp_path / "gate.db") as connection:
        persisted_display = connection.execute(
            "SELECT display_json FROM receipts WHERE request_id = ?",
            (pending.request_id,),
        ).fetchone()[0]
    assert secret not in persisted_display


def test_uncertain_effect_never_reuses_the_approval(tmp_path: Path) -> None:
    gate = build_gate(tmp_path)
    args = {"text": "hello", "quote_post_id": None}
    pending = gate.intercept(
        "x_create_post",
        args,
        session_id="runtime",
        session_lineage="stored",
    )
    gate.store.decide(pending.request_id, Decision.APPROVE, actor_id="owner")

    def uncertain(_args: dict):
        raise EffectUncertainError("post may exist")

    result = gate.execute(
        "x_create_post",
        args,
        session_id="runtime-2",
        session_lineage="stored",
        next_call=uncertain,
    )

    assert result["ok"] is False
    assert result["status"] == "uncertain"
    request = gate.store.get_request(pending.request_id)
    assert request is not None
    assert request.state is RequestState.UNCERTAIN
    assert (
        gate.store.find_matching_approved(
            profile="life",
            session_lineage="stored",
            tool_name="x_create_post",
            call_digest=request.call_digest,
        )
        is None
    )
