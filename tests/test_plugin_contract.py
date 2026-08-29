from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from human_gate.effects import demo_effect_handler


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


def test_plugin_registers_owned_tool_hook_and_execution_middleware(tmp_path: Path) -> None:
    plugin = load_plugin()
    context = FakeContext(tmp_path)

    plugin.register(context)

    assert "human_gate_demo_effect" in context.tools
    assert "pre_tool_call" in context.hooks
    assert "tool_execution" in context.middleware


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
