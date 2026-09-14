from __future__ import annotations

import json
from collections.abc import Iterator
from itertools import permutations
from pathlib import Path
from typing import Any

import pytest
from test_plugin_contract import FakeContext, load_plugin

from human_gate.runtime_registry import reset_process_runtime_for_tests


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    reset_process_runtime_for_tests()
    for key in ("HERMES_HOME", "ACCORD_DB_PATH", "HUMAN_GATE_DB_PATH"):
        monkeypatch.delenv(key, raising=False)
    yield
    reset_process_runtime_for_tests()


@pytest.mark.parametrize(
    "tool_name",
    ["accord_demo_effect", "human_gate_demo_effect", "accord_mock_publish", "human_gate_mock_publish"],
)
@pytest.mark.parametrize("nested_hook", [False, True])
def test_effect_alias_consumes_exact_approval_once(
    tmp_path: Path, tool_name: str, nested_hook: bool,
) -> None:
    plugin = load_plugin()
    context = FakeContext(tmp_path)
    plugin.register(context)
    pre_hook = context.hooks["pre_tool_call"]
    middleware = context.middleware["tool_execution"]
    handler = context.tools[tool_name][1]
    args: dict[str, Any] = {"message": "approved fixture"}
    if tool_name.endswith("mock_publish"):
        args = {
            "destination": "mock", "text": "approved fixture", "media_sha256": [],
            "idempotency_key": "alias-once", "simulate_outcome": "success",
        }
    assert json.loads(handler(args))["status"] == "human_gate_required"
    blocked = pre_hook(tool_name=tool_name, args=args, session_id="fixture-session")
    request_id = json.loads(blocked["message"])["request_id"]
    plugin._gate.store.decide(request_id, plugin.Decision.APPROVE, actor_id="fixture-owner")
    results: list[dict[str, Any]] = []

    def dispatch(payload: dict[str, Any]) -> str:
        if nested_hook:
            assert pre_hook(
                tool_name=tool_name, args=payload, session_id="fixture-session",
            ) is None
        changed = dict(payload)
        changed["text" if tool_name.endswith("mock_publish") else "message"] = "changed"
        assert json.loads(handler(changed))["status"] == "human_gate_required"
        first = handler(payload)
        results.append(json.loads(first))
        assert json.loads(handler(payload))["status"] == "human_gate_required"
        return first

    middleware(
        tool_name=tool_name, args=args, session_id="fixture-session", next_call=dispatch,
    )
    assert results and results[0]["ok"] is True
    assert plugin._gate.store.get_request(request_id).state.value == "executed"
    assert json.loads(handler(args))["status"] == "human_gate_required"
    assert plugin._gate.store.count_mock_publications() == int(tool_name.endswith("mock_publish"))


@pytest.mark.parametrize("tool_name", ["accord_mock_publish", "human_gate_mock_publish"])
def test_mock_alias_uncertain_replay_requires_new_approval(tmp_path: Path, tool_name: str) -> None:
    plugin = load_plugin()
    context = FakeContext(tmp_path)
    plugin.register(context)
    handler = context.tools[tool_name][1]
    args = {
        "destination": "mock", "text": "fixture", "media_sha256": [],
        "idempotency_key": "uncertain-alias", "simulate_outcome": "uncertain",
    }
    ids = []
    results = []
    for _ in range(2):
        blocked = context.hooks["pre_tool_call"](
            tool_name=tool_name, args=args, session_id="fixture-session",
        )
        request_id = json.loads(blocked["message"])["request_id"]
        ids.append(request_id)
        plugin._gate.store.decide(request_id, plugin.Decision.APPROVE, actor_id="fixture-owner")
        results.append(json.loads(context.middleware["tool_execution"](
            tool_name=tool_name, args=args, session_id="fixture-session", next_call=handler,
        )))
    assert ids[0] != ids[1]
    assert results[0]["status"] == "uncertain"
    assert results[1]["ok"] is True and results[1]["replayed"] is True
    assert plugin._gate.store.count_mock_publications() == 1


@pytest.mark.parametrize(
    ("approved_tool", "attempted_tool"),
    list(permutations(
        ["accord_demo_effect", "human_gate_demo_effect", "accord_mock_publish", "human_gate_mock_publish"],
        2,
    )),
)
def test_approval_does_not_authorize_another_tool_name(
    tmp_path: Path, approved_tool: str, attempted_tool: str,
) -> None:
    plugin = load_plugin()
    context = FakeContext(tmp_path)
    plugin.register(context)
    pre_hook = context.hooks["pre_tool_call"]
    middleware = context.middleware["tool_execution"]

    def arguments(tool_name: str) -> dict[str, Any]:
        if tool_name.endswith("mock_publish"):
            return {
                "destination": "mock", "text": "fixture", "media_sha256": [],
                "idempotency_key": "name-isolation", "simulate_outcome": "success",
            }
        return {"message": "fixture"}

    args = arguments(approved_tool)
    blocked = pre_hook(tool_name=approved_tool, args=args, session_id="fixture-session")
    approved_id = json.loads(blocked["message"])["request_id"]
    plugin._gate.store.decide(approved_id, plugin.Decision.APPROVE, actor_id="fixture-owner")

    def forbidden_dispatch(payload: dict[str, Any]) -> str:
        raise AssertionError("Another tool name reached dispatch using the original approval")

    denied = json.loads(middleware(
        tool_name=attempted_tool, args=arguments(attempted_tool),
        session_id="fixture-session", next_call=forbidden_dispatch,
    ))
    assert denied["status"] == "pending_approval"
    assert denied["request_id"] != approved_id
    pending = plugin._gate.store.get_request(denied["request_id"])
    assert pending.tool_name == attempted_tool
    assert pending.state.value == "pending"
    assert plugin._gate.store.get_request(approved_id).state.value == "approved"
    assert plugin._gate.store.count_mock_publications() == 0

    result = json.loads(middleware(
        tool_name=approved_tool, args=args, session_id="fixture-session",
        next_call=context.tools[approved_tool][1],
    ))
    assert result["ok"] is True
    assert plugin._gate.store.get_request(approved_id).state.value == "executed"
    assert plugin._gate.store.get_request(denied["request_id"]).state.value == "pending"
    assert plugin._gate.store.count_mock_publications() == int(approved_tool.endswith("mock_publish"))
