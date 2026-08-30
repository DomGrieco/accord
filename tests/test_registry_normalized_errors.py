from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from human_gate.claims import consume_active_claim
from human_gate.errors import EffectUncertainError
from human_gate.gate import HumanGate
from human_gate.models import Decision, RequestState
from human_gate.policy import PolicyRegistry, ToolPolicy
from human_gate.store import ConflictError, GateStore


def _owned_gate(tmp_path: Path) -> HumanGate:
    policies = PolicyRegistry()
    policies.register(
        ToolPolicy(
            tool_name="x_create_post",
            effect_kind="publish",
            display_fields=("text",),
            replay_fields=("text",),
            owned_effect=True,
            owned_handler_identity="human_gate.x_create_post.v1",
        )
    )
    return HumanGate(GateStore(tmp_path / "gate.db"), policies, profile="life")


def _generic_gate(tmp_path: Path) -> HumanGate:
    policies = PolicyRegistry()
    policies.register(
        ToolPolicy(
            tool_name="third_party_write",
            effect_kind="write",
            display_fields=("record_id",),
            replay_fields=("record_id",),
        )
    )
    return HumanGate(GateStore(tmp_path / "gate.db"), policies, profile="life")


def _approve(gate: HumanGate, args: dict[str, Any]) -> str:
    pending = gate.intercept("x_create_post", args, session_id="runtime", session_lineage="stored")
    assert pending.request_id is not None
    gate.store.decide(pending.request_id, Decision.APPROVE, actor_id="owner")
    return pending.request_id


def _consume_and_return(args: dict[str, Any], result: Any) -> Any:
    assert consume_active_claim("x_create_post", args) is not None
    return result


def _execute(gate: HumanGate, args: dict[str, Any], result: Any) -> Any:
    return gate.execute(
        "x_create_post",
        args,
        session_id="runtime-2",
        session_lineage="stored",
        next_call=lambda dispatched: _consume_and_return(dispatched, result),
    )


@pytest.mark.parametrize(
    "error_text",
    [
        "Tool execution failed: EffectUncertainError: response lost SECRET_DETAIL",
        "Tool execution failed: EffectDefinitiveFailureError: rejected SECRET_DETAIL",
        "Tool execution failed: RuntimeError: unknown SECRET_DETAIL",
    ],
)
def test_registry_normalized_exception_after_exact_owned_claim_is_uncertain(
    tmp_path: Path, error_text: str
) -> None:
    gate = _owned_gate(tmp_path)
    args = {"text": "hello"}
    request_id = _approve(gate, args)

    result = _execute(gate, args, json.dumps({"error": error_text}))

    assert result == {
        "ok": False,
        "status": "uncertain",
        "request_id": request_id,
        "error": "RegistryNormalizedToolError: effect outcome could not be verified",
    }
    request = gate.store.get_request(request_id)
    assert request is not None and request.state is RequestState.UNCERTAIN
    receipt = gate.store.audit_history(request_id)[-1]
    assert receipt["outcome"] == "uncertain"
    with sqlite3.connect(tmp_path / "gate.db") as connection:
        display = connection.execute(
            "SELECT display_json FROM receipts WHERE request_id = ?", (request_id,)
        ).fetchone()[0]
    assert json.loads(display) == {"error_type": "RegistryNormalizedToolError"}
    assert "SECRET_DETAIL" not in display


@pytest.mark.parametrize(
    "ordinary_result",
    [
        "published",
        {"ok": True, "post_id": "post-1"},
        {"error": "Tool execution failed: domain record"},
        json.dumps({"error": "Tool execution failed: x", "ok": True}),
        json.dumps({"error": "tool execution failed: wrong case"}),
        json.dumps({"error": None}),
        "not json",
        "{",
        "[]",
        json.dumps({"error": "Tool execution failed: RuntimeError: " + ("x" * (64 * 1024))}),
    ],
)
def test_non_exact_envelopes_remain_normal_success(tmp_path: Path, ordinary_result: Any) -> None:
    gate = _owned_gate(tmp_path)
    args = {"text": "hello"}
    request_id = _approve(gate, args)

    assert _execute(gate, args, ordinary_result) == ordinary_result
    request = gate.store.get_request(request_id)
    assert request is not None and request.state is RequestState.EXECUTED


def test_async_registry_normalized_exception_is_uncertain(tmp_path: Path) -> None:
    gate = _owned_gate(tmp_path)
    args = {"text": "hello"}
    request_id = _approve(gate, args)

    async def dispatch(dispatched: dict[str, Any]) -> str:
        return _consume_and_return(
            dispatched, json.dumps({"error": "Tool execution failed: RuntimeError: lost"})
        )

    result = asyncio.run(
        gate.execute(
            "x_create_post",
            args,
            session_id="runtime-2",
            session_lineage="stored",
            next_call=dispatch,
        )
    )

    assert result["status"] == "uncertain"
    request = gate.store.get_request(request_id)
    assert request is not None and request.state is RequestState.UNCERTAIN


def test_real_tool_registry_dispatch_normalization_is_uncertain(tmp_path: Path) -> None:
    hermes_root = Path.home() / ".hermes" / "hermes-agent"
    sys.path.insert(0, str(hermes_root))
    try:
        from tools.registry import ToolRegistry

        registry = ToolRegistry()

        def handler(dispatched: dict[str, Any]) -> str:
            assert consume_active_claim("x_create_post", dispatched) is not None
            raise EffectUncertainError("response lost after dispatch")

        registry.register(
            name="x_create_post",
            toolset="test",
            schema={"name": "x_create_post", "description": "test", "parameters": {}},
            handler=handler,
        )
        gate = _owned_gate(tmp_path)
        args = {"text": "hello"}
        request_id = _approve(gate, args)

        result = gate.execute(
            "x_create_post",
            args,
            session_id="runtime-2",
            session_lineage="stored",
            next_call=lambda dispatched: registry.dispatch("x_create_post", dispatched),
        )

        assert result["status"] == "uncertain"
        request = gate.store.get_request(request_id)
        assert request is not None and request.state is RequestState.UNCERTAIN
        receipts = [
            item for item in gate.store.audit_history(request_id) if item["event_type"] == "receipt"
        ]
        assert [item["outcome"] for item in receipts] == ["uncertain"]
    finally:
        sys.path.remove(str(hermes_root))


def _historical_executed(store: GateStore) -> tuple[str, str]:
    request = store.create_or_get_pending(
        profile="life",
        session_id="runtime",
        session_lineage="stored",
        tool_name="x_create_post",
        effect_kind="publish",
        call_digest="d" * 64,
        display={"text": "hello"},
        replay={"text": "hello"},
    )
    store.decide(request.id, Decision.APPROVE, actor_id="owner")
    assert store.claim(request.id, expected_digest=request.call_digest)
    store.complete(request.id, RequestState.EXECUTED, result={"normalized": "error"})
    with sqlite3.connect(store.path) as connection:
        digest = connection.execute(
            "SELECT result_digest FROM receipts WHERE request_id = ?", (request.id,)
        ).fetchone()[0]
    return request.id, digest


def test_reconcile_preserves_original_receipt_appends_correction_and_is_idempotent(
    tmp_path: Path,
) -> None:
    store = GateStore(tmp_path / "gate.db")
    request_id, digest = _historical_executed(store)

    first = store.reconcile_executed_x_create_post_as_uncertain(
        request_id, expected_receipt_digest=digest
    )
    second = store.reconcile_executed_x_create_post_as_uncertain(
        request_id, expected_receipt_digest=digest
    )

    assert first.state is RequestState.UNCERTAIN
    assert second.record_version == first.record_version
    with sqlite3.connect(store.path) as connection:
        receipts = connection.execute(
            "SELECT outcome, result_digest, display_json FROM receipts "
            "WHERE request_id = ? ORDER BY created_at, id",
            (request_id,),
        ).fetchall()
    assert len(receipts) == 2
    assert receipts[0][0:2] == ("executed", digest)
    assert receipts[1][0] == "uncertain"
    assert json.loads(receipts[1][2]) == {"error_type": "RegistryNormalizedToolError"}


@pytest.mark.parametrize(
    "mismatch",
    [
        "missing_request",
        "wrong_tool",
        "wrong_effect_kind",
        "wrong_request_state",
        "missing_receipt",
        "multiple_receipts",
        "wrong_receipt_outcome",
        "wrong_receipt_digest",
    ],
)
def test_reconcile_refuses_shape_mismatches_atomically(tmp_path: Path, mismatch: str) -> None:
    path = tmp_path / "gate.db"
    store = GateStore(path)
    request_id, digest = _historical_executed(store)
    target_id = request_id
    with sqlite3.connect(path) as connection:
        if mismatch == "missing_request":
            target_id = "0" * 32
        elif mismatch == "wrong_tool":
            connection.execute("UPDATE requests SET tool_name='other' WHERE id=?", (request_id,))
        elif mismatch == "wrong_effect_kind":
            connection.execute("UPDATE requests SET effect_kind='other' WHERE id=?", (request_id,))
        elif mismatch == "wrong_request_state":
            connection.execute("UPDATE requests SET state='failed' WHERE id=?", (request_id,))
        elif mismatch == "missing_receipt":
            connection.execute("DELETE FROM receipts WHERE request_id=?", (request_id,))
        elif mismatch == "multiple_receipts":
            connection.execute(
                "INSERT INTO receipts SELECT ?,request_id,outcome,result_digest,display_json,created_at "
                "FROM receipts WHERE request_id=?",
                ("f" * 32, request_id),
            )
        elif mismatch == "wrong_receipt_outcome":
            connection.execute(
                "UPDATE receipts SET outcome='failed' WHERE request_id=?", (request_id,)
            )
        elif mismatch == "wrong_receipt_digest":
            digest = "0" * 64

    before = path.read_bytes()
    with pytest.raises(ConflictError, match="historical executed x_create_post"):
        store.reconcile_executed_x_create_post_as_uncertain(
            target_id, expected_receipt_digest=digest
        )
    assert path.read_bytes() == before


def _approve_and_execute_generic(gate: HumanGate, result: Any) -> tuple[str, Any]:
    args = {"record_id": "record-1"}
    pending = gate.intercept(
        "third_party_write", args, session_id="runtime", session_lineage="stored"
    )
    assert pending.request_id is not None
    gate.store.decide(pending.request_id, Decision.APPROVE, actor_id="owner")
    executed = gate.execute(
        "third_party_write",
        args,
        session_id="runtime-2",
        session_lineage="stored",
        next_call=lambda _dispatched: result,
    )
    return pending.request_id, executed


@pytest.mark.parametrize(
    "error_text",
    [
        "[TOOL_ERROR] Tool execution failed: RuntimeError: sanitized",
        "Tool execution failed: RuntimeError: fallback",
    ],
)
def test_registry_normalized_error_for_generic_policy_is_uncertain(
    tmp_path: Path, error_text: str
) -> None:
    gate = _generic_gate(tmp_path)

    request_id, result = _approve_and_execute_generic(gate, json.dumps({"error": error_text}))

    assert result == {
        "ok": False,
        "status": "uncertain",
        "request_id": request_id,
        "error": "RegistryNormalizedToolError: effect outcome could not be verified",
    }
    request = gate.store.get_request(request_id)
    assert request is not None and request.state is RequestState.UNCERTAIN
    receipts = [
        item for item in gate.store.audit_history(request_id) if item["event_type"] == "receipt"
    ]
    assert [item["outcome"] for item in receipts] == ["uncertain"]
    with sqlite3.connect(tmp_path / "gate.db") as connection:
        persisted = connection.execute(
            "SELECT outcome, display_json FROM receipts WHERE request_id = ?", (request_id,)
        ).fetchall()
    assert [row[0] for row in persisted] == ["uncertain"]
    assert [json.loads(row[1]) for row in persisted] == [
        {"error_type": "RegistryNormalizedToolError"}
    ]


@pytest.mark.parametrize(
    "ordinary_result",
    [
        "written",
        {"ok": True, "record_id": "record-1"},
        {"error": "Tool execution failed: domain record"},
        json.dumps({"error": "domain failure"}),
        json.dumps({"error": "Tool execution failed: domain record", "code": "E_DOMAIN"}),
        json.dumps({"error": "tool execution failed: wrong case"}),
    ],
)
def test_non_registry_results_for_generic_policy_remain_executed(
    tmp_path: Path, ordinary_result: Any
) -> None:
    gate = _generic_gate(tmp_path)

    request_id, result = _approve_and_execute_generic(gate, ordinary_result)

    assert result == ordinary_result
    request = gate.store.get_request(request_id)
    assert request is not None and request.state is RequestState.EXECUTED
    receipts = [
        item for item in gate.store.audit_history(request_id) if item["event_type"] == "receipt"
    ]
    assert [item["outcome"] for item in receipts] == ["executed"]
