from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from human_gate.effects import demo_effect_handler, mock_publish_handler


@pytest.fixture(autouse=True)
def isolate_plugin_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("HUMAN_GATE_DB_PATH", raising=False)


class FakeContext:
    def __init__(self, data_dir: Path, *, policies: list[dict[str, Any]] | None = None) -> None:
        self.profile_name = "life"
        self.state = SimpleNamespace(data_dir=data_dir)
        self.tools: dict[str, tuple[dict[str, Any], Any]] = {}
        self.hooks: dict[str, Any] = {}
        self.middleware: dict[str, Any] = {}
        self._policies = policies

    def get_config(self, key: str, default: Any = None) -> Any:
        if key == "policies" and self._policies is not None:
            return self._policies
        return default

    def register_tool(self, *, name: str, schema: dict[str, Any], handler: Any, **_: Any) -> None:
        self.tools[name] = (schema, handler)

    def register_hook(self, name: str, handler: Any) -> None:
        self.hooks[name] = handler

    def register_middleware(self, name: str, handler: Any) -> None:
        self.middleware[name] = handler


def load_plugin():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "hermes_human_gate_plugin",
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_owned_effect_fails_closed_without_active_claim() -> None:
    result = json.loads(demo_effect_handler({"message": "hello"}))

    assert result["ok"] is False
    assert result["status"] == "human_gate_required"


def test_owned_mock_publisher_fails_closed_without_active_claim(tmp_path: Path) -> None:
    store = load_plugin().GateStore(tmp_path / "gate.db")

    result = json.loads(
        mock_publish_handler(
            store,
            {
                "destination": "mock",
                "text": "hello",
                "media_sha256": [],
                "idempotency_key": "publish-once",
                "simulate_outcome": "success",
            },
        )
    )

    assert result["ok"] is False
    assert result["status"] == "human_gate_required"
    assert store.count_mock_publications() == 0


def test_plugin_registers_owned_tool_hook_and_execution_middleware(tmp_path: Path) -> None:
    plugin = load_plugin()
    context = FakeContext(tmp_path)

    plugin.register(context)

    assert "human_gate_demo_effect" in context.tools
    assert "human_gate_mock_publish" in context.tools
    assert "pre_tool_call" in context.hooks
    assert "tool_execution" in context.middleware


def test_mock_publisher_uncertain_result_replays_idempotently_after_new_approval(
    tmp_path: Path,
) -> None:
    plugin = load_plugin()
    context = FakeContext(tmp_path)
    plugin.register(context)
    pre_hook = context.hooks["pre_tool_call"]
    middleware = context.middleware["tool_execution"]
    _schema, handler = context.tools["human_gate_mock_publish"]
    args = {
        "destination": "mock",
        "text": "Approved local fixture only.",
        "media_sha256": ["a" * 64],
        "idempotency_key": "publish-once",
        "simulate_outcome": "uncertain",
    }

    blocked = pre_hook(
        tool_name="human_gate_mock_publish",
        args=args,
        session_id="stored-session",
    )
    first_id = json.loads(blocked["message"])["request_id"]
    plugin._gate.store.decide(first_id, plugin.Decision.APPROVE, actor_id="owner")
    uncertain = json.loads(
        middleware(
            tool_name="human_gate_mock_publish",
            args=args,
            session_id="stored-session",
            next_call=lambda payload: handler(payload),
        )
    )

    assert uncertain["status"] == "uncertain"
    assert plugin._gate.store.count_mock_publications() == 1
    blocked_again = pre_hook(
        tool_name="human_gate_mock_publish",
        args=args,
        session_id="stored-session",
    )
    second_id = json.loads(blocked_again["message"])["request_id"]
    assert second_id != first_id
    plugin._gate.store.decide(second_id, plugin.Decision.APPROVE, actor_id="owner")

    replay = json.loads(
        middleware(
            tool_name="human_gate_mock_publish",
            args=args,
            session_id="stored-session",
            next_call=lambda payload: handler(payload),
        )
    )

    assert replay["ok"] is True
    assert replay["provider"] == "mock"
    assert replay["replayed"] is True
    assert plugin._gate.store.count_mock_publications() == 1


def test_plugin_startup_does_not_recover_claim_owned_by_live_runtime(tmp_path: Path) -> None:
    first_plugin = load_plugin()
    first_context = FakeContext(tmp_path)
    first_plugin.register(first_context)
    args = {"message": "hello"}

    blocked = first_context.hooks["pre_tool_call"](
        tool_name="human_gate_demo_effect",
        args=args,
        session_id="stored-session",
    )
    request_id = json.loads(blocked["message"])["request_id"]
    request = first_plugin._gate.store.get_request(request_id)
    assert request is not None
    first_plugin._gate.store.decide(
        request_id,
        first_plugin.Decision.APPROVE,
        actor_id="owner",
    )
    assert first_plugin._gate.store.claim(
        request_id,
        expected_digest=request.call_digest,
    )

    same_runtime_context = FakeContext(tmp_path)
    with pytest.raises(RuntimeError, match="already active"):
        first_plugin.register(same_runtime_context)
    assert same_runtime_context.tools == {}
    assert same_runtime_context.hooks == {}
    assert same_runtime_context.middleware == {}
    assert (
        first_plugin._gate.store.get_request(request_id).state is first_plugin.RequestState.CLAIMED
    )

    second_plugin = load_plugin()
    second_context = FakeContext(tmp_path)
    with pytest.raises(RuntimeError, match="already active"):
        second_plugin.register(second_context)

    assert second_plugin._gate is None
    assert second_context.tools == {}
    assert second_context.hooks == {}
    assert second_context.middleware == {}
    assert (
        first_plugin._gate.store.get_request(request_id).state is first_plugin.RequestState.CLAIMED
    )
    first_plugin._gate.store.close()


def test_plugin_startup_recovers_abandoned_claim_as_uncertain(tmp_path: Path) -> None:
    first_plugin = load_plugin()
    first_context = FakeContext(tmp_path)
    first_plugin.register(first_context)
    args = {"message": "hello"}

    blocked = first_context.hooks["pre_tool_call"](
        tool_name="human_gate_demo_effect",
        args=args,
        session_id="stored-session",
    )
    request_id = json.loads(blocked["message"])["request_id"]
    request = first_plugin._gate.store.get_request(request_id)
    assert request is not None
    first_plugin._gate.store.decide(
        request_id,
        first_plugin.Decision.APPROVE,
        actor_id="owner",
    )
    assert first_plugin._gate.store.claim(
        request_id,
        expected_digest=request.call_digest,
    )
    first_plugin._gate.store.close()

    restarted_plugin = load_plugin()
    restarted_context = FakeContext(tmp_path)
    restarted_plugin.register(restarted_context)

    recovered = restarted_plugin._gate.store.get_request(request_id)
    assert recovered is not None
    assert recovered.state is restarted_plugin.RequestState.UNCERTAIN
    with sqlite3.connect(tmp_path / "approvals.db") as connection:
        receipts = connection.execute(
            "SELECT outcome FROM receipts WHERE request_id = ?",
            (request_id,),
        ).fetchall()
    assert receipts == [("uncertain",)]

    restarted_plugin._gate.store.close()
    second_restart_plugin = load_plugin()
    second_restart_context = FakeContext(tmp_path)
    second_restart_plugin.register(second_restart_context)
    with sqlite3.connect(tmp_path / "approvals.db") as connection:
        receipts_after_second_restart = connection.execute(
            "SELECT outcome FROM receipts WHERE request_id = ?",
            (request_id,),
        ).fetchall()
    assert receipts_after_second_restart == [("uncertain",)]

    replay = second_restart_context.hooks["pre_tool_call"](
        tool_name="human_gate_demo_effect",
        args=args,
        session_id="stored-session",
    )
    replay_id = json.loads(replay["message"])["request_id"]
    assert replay_id != request_id
    assert second_restart_plugin._gate.store.get_request(replay_id).state.value == "pending"


def test_plugin_startup_fails_closed_when_claim_recovery_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = load_plugin()
    context = FakeContext(tmp_path)

    def fail_recovery(_store: Any) -> int:
        raise RuntimeError("recovery failed")

    monkeypatch.setattr(plugin.GateStore, "recover_claimed_as_uncertain", fail_recovery)

    with pytest.raises(RuntimeError, match="recovery failed"):
        plugin.register(context)

    assert plugin._gate is None
    assert context.tools == {}
    assert context.hooks == {}
    assert context.middleware == {}


def test_configured_tool_fails_closed_without_session_identity(tmp_path: Path) -> None:
    plugin = load_plugin()
    context = FakeContext(tmp_path)
    plugin.register(context)
    pre_hook = context.hooks["pre_tool_call"]
    middleware = context.middleware["tool_execution"]
    calls: list[dict[str, Any]] = []

    blocked = pre_hook(
        tool_name="human_gate_demo_effect",
        args={"message": "hello"},
    )
    execution = middleware(
        tool_name="human_gate_demo_effect",
        args={"message": "hello"},
        next_call=lambda payload: calls.append(payload),
    )

    assert blocked["action"] == "block"
    assert json.loads(blocked["message"])["status"] == "human_gate_unroutable"
    assert json.loads(execution)["status"] == "human_gate_unroutable"
    assert calls == []
    assert plugin._gate.store.list_requests() == []


def test_external_write_tools_are_not_gated_without_explicit_policy(tmp_path: Path) -> None:
    plugin = load_plugin()
    context = FakeContext(tmp_path)
    plugin.register(context)

    result = context.hooks["pre_tool_call"](
        tool_name="x_create_post",
        args={"account": "fixture", "text": "hello"},
        session_id="stored-session",
    )

    assert result is None


def test_owned_effect_executes_only_inside_approved_execution_context(tmp_path: Path) -> None:
    plugin = load_plugin()
    context = FakeContext(tmp_path)
    plugin.register(context)
    pre_hook = context.hooks["pre_tool_call"]
    middleware = context.middleware["tool_execution"]
    _schema, handler = context.tools["human_gate_demo_effect"]
    args = {"message": "hello"}

    blocked = pre_hook(
        tool_name="human_gate_demo_effect",
        args=args,
        session_id="stored-session",
    )
    assert blocked["action"] == "block"
    request_id = json.loads(blocked["message"])["request_id"]
    plugin._gate.store.decide(request_id, plugin.Decision.APPROVE, actor_id="owner")

    assert (
        pre_hook(
            tool_name="human_gate_demo_effect",
            args=args,
            session_id="stored-session",
        )
        is None
    )
    result = middleware(
        tool_name="human_gate_demo_effect",
        args=args,
        original_args=args,
        session_id="stored-session",
        next_call=lambda payload: handler(payload),
    )
    decoded = json.loads(result)

    assert decoded == {"effect": "demo", "message": "hello", "ok": True}


def test_approved_effect_uses_stable_lineage_after_runtime_replacement(tmp_path: Path) -> None:
    plugin = load_plugin()
    context = FakeContext(tmp_path)
    plugin.register(context)
    pre_hook = context.hooks["pre_tool_call"]
    middleware = context.middleware["tool_execution"]
    _schema, handler = context.tools["human_gate_demo_effect"]
    args = {"message": "hello"}

    blocked = pre_hook(
        tool_name="human_gate_demo_effect",
        args=args,
        session_id="runtime-1",
        session_key="stored-1",
    )
    request_id = json.loads(blocked["message"])["request_id"]
    pending = plugin._gate.store.get_request(request_id)
    assert pending is not None
    assert pending.session_id == "stored-1"
    assert pending.session_lineage == "stored-1"
    plugin._gate.store.decide(request_id, plugin.Decision.APPROVE, actor_id="owner")

    assert (
        pre_hook(
            tool_name="human_gate_demo_effect",
            args=args,
            session_id="runtime-2",
            session_key="stored-1",
        )
        is None
    )
    result = middleware(
        tool_name="human_gate_demo_effect",
        args=args,
        session_id="runtime-2",
        session_key="stored-1",
        next_call=lambda payload: handler(payload),
    )

    assert json.loads(result) == {"effect": "demo", "message": "hello", "ok": True}


def test_approved_effect_cannot_cross_stable_session_lineage(tmp_path: Path) -> None:
    plugin = load_plugin()
    context = FakeContext(tmp_path)
    plugin.register(context)
    pre_hook = context.hooks["pre_tool_call"]
    args = {"message": "hello"}

    first = pre_hook(
        tool_name="human_gate_demo_effect",
        args=args,
        session_id="runtime-1",
        session_key="stored-1",
    )
    first_id = json.loads(first["message"])["request_id"]
    plugin._gate.store.decide(first_id, plugin.Decision.APPROVE, actor_id="owner")

    second = pre_hook(
        tool_name="human_gate_demo_effect",
        args=args,
        session_id="runtime-2",
        session_key="stored-2",
    )
    second_id = json.loads(second["message"])["request_id"]

    assert second["action"] == "block"
    assert second_id != first_id
    assert plugin._gate.store.get_request(second_id).state.value == "pending"


def test_plugin_intercepts_only_explicitly_configured_external_write_tool(
    tmp_path: Path,
) -> None:
    plugin = load_plugin()
    context = FakeContext(
        tmp_path,
        policies=[
            {
                "tool_name": "x_create_post",
                "effect_kind": "publish",
                "display_fields": ["account", "text"],
                "replay_fields": ["account", "text"],
            }
        ],
    )
    plugin.register(context)

    blocked = context.hooks["pre_tool_call"](
        tool_name="x_create_post",
        args={"account": "fixture", "text": "hello"},
        session_id="stored-session",
    )

    assert blocked["action"] == "block"
    request_id = json.loads(blocked["message"])["request_id"]
    assert plugin._gate.store.get_request(request_id).display == {
        "account": "fixture",
        "text": "hello",
    }
