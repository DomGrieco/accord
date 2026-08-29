from __future__ import annotations

from pathlib import Path

from human_gate.effects import EffectUncertainError
from human_gate.gate import GateDecision, HumanGate
from human_gate.models import Decision, RequestState
from human_gate.policy import PolicyRegistry, ToolPolicy
from human_gate.store import GateStore


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


def test_executor_exception_records_failed_without_retry(tmp_path: Path) -> None:
    gate = build_gate(tmp_path)
    pending = gate.intercept(
        "x_create_post",
        {"text": "hello", "quote_post_id": None},
        session_id="runtime",
        session_lineage="stored",
    )
    gate.store.decide(pending.request_id, Decision.APPROVE, actor_id="owner")

    def fail(_args: dict):
        raise RuntimeError("provider rejected request")

    result = gate.execute(
        "x_create_post",
        {"text": "hello", "quote_post_id": None},
        session_id="runtime-2",
        session_lineage="stored",
        next_call=fail,
    )

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert gate.store.get_request(pending.request_id).state is RequestState.FAILED


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
    assert gate.store.find_matching_approved(
        profile="life",
        session_lineage="stored",
        tool_name="x_create_post",
        call_digest=request.call_digest,
    ) is None
