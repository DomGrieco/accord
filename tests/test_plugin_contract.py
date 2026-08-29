from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from human_gate.effects import demo_effect_handler


class FakeContext:
    def __init__(self, data_dir: Path) -> None:
        self.profile_name = "life"
        self.state = SimpleNamespace(data_dir=data_dir)
        self.tools: dict[str, tuple[dict[str, Any], Any]] = {}
        self.hooks: dict[str, Any] = {}
        self.middleware: dict[str, Any] = {}

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
