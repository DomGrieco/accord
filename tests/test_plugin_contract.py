from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
from collections.abc import Iterator
from contextvars import Context, copy_context
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from human_gate.effects import demo_effect_handler, mock_publish_handler
from human_gate.runtime_registry import reset_process_runtime_for_tests


@pytest.fixture(autouse=True)
def isolate_plugin_data(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    reset_process_runtime_for_tests()
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("HUMAN_GATE_DB_PATH", raising=False)
    yield
    reset_process_runtime_for_tests()


class FakeContext:
    def __init__(
        self,
        data_dir: Path,
        *,
        policies: list[dict[str, Any]] | None = None,
        x_config: dict[str, Any] | None = None,
    ) -> None:
        self.profile_name = "life"
        self.state = SimpleNamespace(data_dir=data_dir)
        self.tools: dict[str, tuple[dict[str, Any], Any]] = {}
        self.toolsets: dict[str, str] = {}
        self.hooks: dict[str, Any] = {}
        self.middleware: dict[str, Any] = {}
        self._policies = policies
        self._x_config = x_config

    def get_config(self, key: str, default: Any = None) -> Any:
        if key == "policies" and self._policies is not None:
            return self._policies
        if key == "x" and self._x_config is not None:
            return self._x_config
        return default

    def register_tool(
        self,
        *,
        name: str,
        toolset: str,
        schema: dict[str, Any],
        handler: Any,
        **_: Any,
    ) -> None:
        self.tools[name] = (schema, handler)
        self.toolsets[name] = toolset

    def register_hook(self, name: str, handler: Any) -> None:
        self.hooks[name] = handler

    def register_middleware(self, name: str, handler: Any) -> None:
        self.middleware[name] = handler


def load_plugin(module_name: str = "hermes_human_gate_plugin"):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        module_name,
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
    assert "x_create_post" in context.tools
    assert context.toolsets["x_create_post"] == "terminal"
    assert "pre_tool_call" in context.hooks
    assert "tool_execution" in context.middleware


def test_plugin_can_register_the_same_runtime_into_a_second_context(tmp_path: Path) -> None:
    plugin = load_plugin()
    first = FakeContext(
        tmp_path,
        x_config={"enabled": True, "app": "life", "account": "DomAtSiteSage"},
    )
    second = FakeContext(
        tmp_path,
        x_config={"enabled": True, "app": "life", "account": "DomAtSiteSage"},
    )

    plugin.register(first)
    original_gate = plugin._gate
    plugin.register(second)

    assert plugin._gate is original_gate
    assert "x_create_post" in second.tools
    assert "pre_tool_call" in second.hooks
    assert "tool_execution" in second.middleware


def test_re_registering_changed_terminal_exact_policy_replaces_stale_runtime(
    tmp_path: Path,
) -> None:
    plugin = load_plugin()
    first = FakeContext(
        tmp_path,
        policies=[
            {
                "tool_name": "terminal",
                "effect_kind": "local_fixture",
                "display_fields": ["command"],
                "replay_fields": ["command"],
                "command_exact": ["printf A"],
            }
        ],
    )
    second = FakeContext(
        tmp_path,
        policies=[
            {
                "tool_name": "terminal",
                "effect_kind": "local_fixture",
                "display_fields": ["command"],
                "replay_fields": ["command"],
                "command_exact": ["printf B"],
            }
        ],
    )
    plugin.register(first)
    plugin.register(second)
    executed: list[dict[str, Any]] = []

    result = json.loads(
        second.middleware["tool_execution"](
            tool_name="terminal",
            args={"command": "printf B"},
            session_id="stored-session",
            next_call=lambda payload: executed.append(payload) or {"ok": True},
        )
    )

    assert result["status"] == "pending_approval"
    assert executed == []
    assert plugin._gate.store.get_request(result["request_id"]).state is plugin.RequestState.PENDING


def test_ambiguous_tool_globs_fail_closed_without_calling_the_tool(tmp_path: Path) -> None:
    plugin = load_plugin()
    context = FakeContext(
        tmp_path,
        policies=[
            {"tool_glob": "records_*", "effect_kind": "records"},
            {"tool_glob": "*_delete", "effect_kind": "delete"},
        ],
    )
    plugin.register(context)
    executed: list[dict[str, Any]] = []

    result = json.loads(
        context.middleware["tool_execution"](
            tool_name="records_delete",
            args={"record_id": "fixture"},
            session_id="stored-session",
            next_call=lambda payload: executed.append(payload) or {"ok": True},
        )
    )

    assert result == {
        "error_type": "ValueError",
        "ok": False,
        "status": "human_gate_unavailable",
    }
    assert executed == []


def test_enabled_x_adapter_gates_every_other_terminal_command(tmp_path: Path) -> None:
    plugin = load_plugin()
    context = FakeContext(
        tmp_path,
        x_config={"enabled": True, "app": "fixture_app", "account": "FixtureAccount"},
    )
    plugin.register(context)
    executed: list[dict[str, Any]] = []

    result = json.loads(
        context.middleware["tool_execution"](
            tool_name="terminal",
            args={"command": "python -c 'print(1)'"},
            session_id="stored-session",
            next_call=lambda payload: executed.append(payload) or {"ok": True},
        )
    )

    assert result["status"] == "pending_approval"
    assert executed == []


def test_re_registering_changed_terminal_glob_policy_replaces_stale_runtime(
    tmp_path: Path,
) -> None:
    plugin = load_plugin()
    first = FakeContext(
        tmp_path,
        policies=[
            {
                "tool_name": "terminal",
                "effect_kind": "local_fixture",
                "display_fields": ["command"],
                "replay_fields": ["command"],
                "command_glob": ["touch /tmp/policy-a-*"],
            }
        ],
    )
    second = FakeContext(
        tmp_path,
        policies=[
            {
                "tool_name": "terminal",
                "effect_kind": "local_fixture",
                "display_fields": ["command"],
                "replay_fields": ["command"],
                "command_glob": ["touch /tmp/policy-b-*"],
            }
        ],
    )
    plugin.register(first)
    plugin.register(second)
    executed: list[dict[str, Any]] = []

    result = json.loads(
        second.middleware["tool_execution"](
            tool_name="terminal",
            args={"command": "touch /tmp/policy-b-fixture"},
            session_id="stored-session",
            next_call=lambda payload: executed.append(payload) or {"ok": True},
        )
    )

    assert result["status"] == "pending_approval"
    assert executed == []
    assert plugin._gate.store.get_request(result["request_id"]).state is plugin.RequestState.PENDING


@pytest.mark.parametrize(
    ("first_policy", "second_policy", "tool_name"),
    [
        (
            {"tool_name": "records_delete", "effect_kind": "archive"},
            {"tool_glob": "records_*", "effect_kind": "archive"},
            "records_archive",
        ),
        (
            {"tool_glob": "records_a*", "effect_kind": "archive"},
            {"tool_glob": "records_b*", "effect_kind": "archive"},
            "records_backup",
        ),
    ],
)
def test_re_registering_changed_tool_selector_replaces_stale_runtime(
    tmp_path: Path,
    first_policy: dict[str, Any],
    second_policy: dict[str, Any],
    tool_name: str,
) -> None:
    plugin = load_plugin()
    first = FakeContext(tmp_path, policies=[first_policy])
    second = FakeContext(tmp_path, policies=[second_policy])
    plugin.register(first)
    plugin.register(second)
    executed: list[dict[str, Any]] = []

    result = json.loads(
        second.middleware["tool_execution"](
            tool_name=tool_name,
            args={"record_id": "fixture-1"},
            session_id="stored-session",
            next_call=lambda payload: executed.append(payload) or {"ok": True},
        )
    )

    assert result["status"] == "pending_approval"
    assert executed == []
    assert plugin._gate.store.get_request(result["request_id"]).state is plugin.RequestState.PENDING


def test_re_registering_projection_change_invalidates_old_approval(
    tmp_path: Path,
) -> None:
    plugin = load_plugin()
    first = FakeContext(
        tmp_path,
        policies=[
            {
                "tool_name": "archive_record",
                "effect_kind": "archive",
                "display_fields": ["record_id"],
                "replay_fields": ["record_id"],
            }
        ],
    )
    plugin.register(first)
    args = {"record_id": "fixture-1", "note": "owner-visible after reconfiguration"}
    blocked = json.loads(
        first.middleware["tool_execution"](
            tool_name="archive_record",
            args=args,
            session_id="stored-session",
            next_call=lambda payload: {"unexpected": payload},
        )
    )
    pending = plugin._gate.store.get_request(blocked["request_id"])
    assert pending is not None
    plugin._gate.store.decide(
        pending.id,
        plugin.Decision.APPROVE,
        actor_id="fixture-owner",
        expected_digest=pending.call_digest,
        expected_record_version=pending.record_version,
    )
    second = FakeContext(
        tmp_path,
        policies=[
            {
                "tool_name": "archive_record",
                "effect_kind": "archive",
                "display_fields": ["record_id", "note"],
                "replay_fields": ["record_id"],
            }
        ],
    )
    plugin.register(second)
    executed: list[dict[str, Any]] = []

    result = json.loads(
        second.middleware["tool_execution"](
            tool_name="archive_record",
            args=args,
            session_id="stored-session",
            next_call=lambda payload: executed.append(payload) or {"ok": True},
        )
    )

    assert result["status"] == "pending_approval"
    assert result["request_id"] != pending.id
    assert executed == []
    replacement = plugin._gate.store.get_request(result["request_id"])
    assert replacement is not None
    assert replacement.display == args


@pytest.mark.parametrize(
    ("first_x_config", "second_x_config"),
    [
        (
            {"enabled": True, "app": "app_a", "account": "FixtureAccount"},
            {"enabled": True, "app": "app_b", "account": "FixtureAccount"},
        ),
        (
            {"enabled": True, "app": "fixture_app", "account": "AccountA"},
            {"enabled": True, "app": "fixture_app", "account": "AccountB"},
        ),
        (
            {"enabled": False, "app": "fixture_app", "account": "FixtureAccount"},
            {"enabled": True, "app": "fixture_app", "account": "FixtureAccount"},
        ),
    ],
    ids=["app", "account", "enabled"],
)
def test_changed_x_execution_settings_invalidate_old_terminal_xurl_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_x_config: dict[str, Any],
    second_x_config: dict[str, Any],
) -> None:
    plugin = load_plugin()
    first = FakeContext(tmp_path, x_config=first_x_config)
    plugin.register(first)
    args = {"command": "xurl post 'Exact approved terminal post text.'"}
    blocked = first.hooks["pre_tool_call"](
        tool_name="terminal",
        args=args,
        session_id="stored-session",
    )
    old_request_id = json.loads(blocked["message"])["request_id"]
    plugin._gate.store.decide(old_request_id, plugin.Decision.APPROVE, actor_id="owner")
    old_gate = plugin._gate

    provider_calls: list[list[str]] = []
    monkeypatch.setitem(
        plugin.x_create_post_handler.__globals__,
        "_run_xurl",
        lambda command: provider_calls.append(command),
    )
    second = FakeContext(tmp_path, x_config=second_x_config)
    plugin.register(second)
    next_calls: list[dict[str, Any]] = []

    result = json.loads(
        second.middleware["tool_execution"](
            tool_name="terminal",
            args=args,
            session_id="stored-session",
            next_call=lambda payload: next_calls.append(payload),
        )
    )

    assert plugin._gate is not old_gate
    assert result["status"] == "pending_approval"
    assert result["request_id"] != old_request_id
    assert next_calls == []
    assert provider_calls == []
    assert plugin._gate.store.get_request(old_request_id).state is plugin.RequestState.APPROVED
    assert plugin._gate.store.get_request(result["request_id"]).state is plugin.RequestState.PENDING


def test_old_middleware_uses_current_x_execution_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = load_plugin()
    first = FakeContext(
        tmp_path,
        x_config={"enabled": True, "app": "app_a", "account": "FixtureAccount"},
    )
    second = FakeContext(
        tmp_path,
        x_config={"enabled": True, "app": "app_b", "account": "FixtureAccount"},
    )
    plugin.register(first)
    plugin.register(second)
    args = {"command": "xurl post 'Exact current configuration text.'"}
    blocked = first.hooks["pre_tool_call"](
        tool_name="terminal",
        args=args,
        session_id="stored-session",
    )
    request_id = json.loads(blocked["message"])["request_id"]
    plugin._gate.store.decide(request_id, plugin.Decision.APPROVE, actor_id="owner")
    provider_calls: list[list[str]] = []
    monkeypatch.setitem(
        plugin.x_create_post_handler.__globals__,
        "_run_xurl",
        lambda command: (
            provider_calls.append(command)
            or SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"data": {"id": "2093515564786540695"}}),
                stderr="",
            )
        ),
    )

    result = json.loads(
        first.middleware["tool_execution"](
            tool_name="terminal",
            args=args,
            session_id="stored-session",
            next_call=lambda _payload: pytest.fail("raw terminal must not execute"),
        )
    )

    assert result["ok"] is True, result
    assert provider_calls == [
        [
            "xurl",
            "--app",
            "app_b",
            "post",
            "Exact current configuration text.",
            "--auth",
            "oauth2",
            "--username",
            "FixtureAccount",
        ]
    ]


def test_x_config_cannot_change_between_claim_and_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = load_plugin()
    first = FakeContext(
        tmp_path,
        x_config={"enabled": True, "app": "app_a", "account": "FixtureAccount"},
    )
    second = FakeContext(
        tmp_path,
        x_config={"enabled": True, "app": "app_b", "account": "FixtureAccount"},
    )
    plugin.register(first)
    args = {"command": "xurl post 'Exact configuration snapshot text.'"}
    blocked = first.hooks["pre_tool_call"](
        tool_name="terminal",
        args=args,
        session_id="stored-session",
    )
    request_id = json.loads(blocked["message"])["request_id"]
    plugin._gate.store.decide(request_id, plugin.Decision.APPROVE, actor_id="owner")
    old_gate = plugin._gate
    original_execute = old_gate.execute
    provider_calls: list[list[str]] = []
    monkeypatch.setitem(
        plugin.x_create_post_handler.__globals__,
        "_run_xurl",
        lambda command: (
            provider_calls.append(command)
            or SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"data": {"id": "2093515564786540695"}}),
                stderr="",
            )
        ),
    )

    def reconfigure_then_execute(*call_args: Any, **call_kwargs: Any) -> Any:
        plugin.register(second)
        return original_execute(*call_args, **call_kwargs)

    monkeypatch.setattr(old_gate, "execute", reconfigure_then_execute)

    result = json.loads(
        first.middleware["tool_execution"](
            tool_name="terminal",
            args=args,
            session_id="stored-session",
            next_call=lambda _payload: pytest.fail("raw terminal must not execute"),
        )
    )

    assert result["ok"] is True, result
    assert provider_calls[0][2] == "app_a"


@pytest.mark.parametrize(
    ("first_x_config", "second_x_config"),
    [
        (
            {"enabled": True, "app": "app_a", "account": "FixtureAccount"},
            {"enabled": True, "app": "app_b", "account": "FixtureAccount"},
        ),
        (
            {"enabled": True, "app": "fixture_app", "account": "AccountA"},
            {"enabled": True, "app": "fixture_app", "account": "AccountB"},
        ),
        (
            {"enabled": False, "app": "fixture_app", "account": "FixtureAccount"},
            {"enabled": True, "app": "fixture_app", "account": "FixtureAccount"},
        ),
    ],
    ids=["app", "account", "enabled"],
)
def test_changed_x_execution_settings_invalidate_old_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_x_config: dict[str, Any],
    second_x_config: dict[str, Any],
) -> None:
    plugin = load_plugin()
    first = FakeContext(tmp_path, x_config=first_x_config)
    plugin.register(first)
    args = {
        "account": first_x_config["account"],
        "text": "Exact approved post text.",
    }
    blocked = first.hooks["pre_tool_call"](
        tool_name="x_create_post",
        args=args,
        session_id="stored-session",
    )
    old_request_id = json.loads(blocked["message"])["request_id"]
    plugin._gate.store.decide(old_request_id, plugin.Decision.APPROVE, actor_id="owner")
    old_gate = plugin._gate

    provider_calls: list[list[str]] = []
    monkeypatch.setitem(
        plugin.x_create_post_handler.__globals__,
        "_run_xurl",
        lambda command: provider_calls.append(command),
    )
    second = FakeContext(tmp_path, x_config=second_x_config)
    plugin.register(second)
    _schema, second_handler = second.tools["x_create_post"]
    next_calls: list[dict[str, Any]] = []

    result = json.loads(
        second.middleware["tool_execution"](
            tool_name="x_create_post",
            args=args,
            session_id="stored-session",
            next_call=lambda payload: next_calls.append(payload) or second_handler(payload),
        )
    )

    assert plugin._gate is not old_gate
    assert result["status"] == "pending_approval"
    assert result["request_id"] != old_request_id
    assert next_calls == []
    assert provider_calls == []
    assert plugin._gate.store.get_request(old_request_id).state is plugin.RequestState.APPROVED
    assert plugin._gate.store.get_request(result["request_id"]).state is plugin.RequestState.PENDING


def test_distinct_wrapper_module_identities_share_one_process_runtime(
    tmp_path: Path,
) -> None:
    first_plugin = load_plugin("hermes_human_gate_plugin_first")
    second_plugin = load_plugin("hermes_human_gate_plugin_second")
    first = FakeContext(tmp_path)
    second = FakeContext(tmp_path)

    first_plugin.register(first)
    second_plugin.register(second)

    assert second_plugin._gate is first_plugin._gate
    assert "x_create_post" in second.tools
    assert "pre_tool_call" in second.hooks
    assert "tool_execution" in second.middleware


def test_approved_x_quote_post_uses_configured_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = load_plugin()
    context = FakeContext(
        tmp_path,
        x_config={"enabled": True, "app": "life", "account": "DomAtSiteSage"},
    )
    calls: list[list[str]] = []

    def run_xurl(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"data": {"id": "2094000000000000000"}}),
            stderr="",
        )

    monkeypatch.setitem(plugin.x_create_post_handler.__globals__, "_run_xurl", run_xurl)
    plugin.register(context)
    pre_hook = context.hooks["pre_tool_call"]
    middleware = context.middleware["tool_execution"]
    _schema, handler = context.tools["x_create_post"]
    args = {
        "account": "DomAtSiteSage",
        "text": "Exact approved post text.",
        "quote_post_id": "2093515564786540695",
    }
    blocked = pre_hook(
        tool_name="x_create_post",
        args=args,
        session_id="stored-session",
    )
    request_id = json.loads(blocked["message"])["request_id"]
    plugin._gate.store.decide(request_id, plugin.Decision.APPROVE, actor_id="owner")

    result = json.loads(
        middleware(
            tool_name="x_create_post",
            args=args,
            session_id="stored-session",
            next_call=lambda payload: handler(payload),
        )
    )

    assert result == {
        "account": "DomAtSiteSage",
        "ok": True,
        "provider": "x",
        "provider_id": "2094000000000000000",
        "url": "https://x.com/DomAtSiteSage/status/2094000000000000000",
    }
    assert calls == [
        [
            "xurl",
            "--app",
            "life",
            "quote",
            "2093515564786540695",
            "Exact approved post text.",
            "--auth",
            "oauth2",
            "--username",
            "DomAtSiteSage",
        ]
    ]


def test_x_post_cannot_dispatch_without_an_active_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = load_plugin()
    context = FakeContext(
        tmp_path,
        x_config={"enabled": True, "app": "life", "account": "DomAtSiteSage"},
    )
    calls: list[list[str]] = []
    monkeypatch.setitem(
        plugin.x_create_post_handler.__globals__,
        "_run_xurl",
        lambda command: calls.append(command),
    )
    plugin.register(context)
    _schema, handler = context.tools["x_create_post"]

    result = json.loads(
        handler(
            {
                "account": "DomAtSiteSage",
                "text": "Unapproved text.",
            }
        )
    )

    assert result["status"] == "human_gate_required"
    assert calls == []


def test_x_provider_failure_becomes_uncertain_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = load_plugin()
    context = FakeContext(
        tmp_path,
        x_config={"enabled": True, "app": "life", "account": "DomAtSiteSage"},
    )
    calls: list[list[str]] = []

    def fail_xurl(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="provider error")

    monkeypatch.setitem(plugin.x_create_post_handler.__globals__, "_run_xurl", fail_xurl)
    plugin.register(context)
    pre_hook = context.hooks["pre_tool_call"]
    middleware = context.middleware["tool_execution"]
    _schema, handler = context.tools["x_create_post"]
    args = {"account": "DomAtSiteSage", "text": "Exact approved post text."}
    blocked = pre_hook(
        tool_name="x_create_post",
        args=args,
        session_id="stored-session",
    )
    request_id = json.loads(blocked["message"])["request_id"]
    plugin._gate.store.decide(request_id, plugin.Decision.APPROVE, actor_id="owner")

    result = json.loads(
        middleware(
            tool_name="x_create_post",
            args=args,
            session_id="stored-session",
            next_call=lambda payload: handler(payload),
        )
    )

    assert result["status"] == "uncertain"
    assert len(calls) == 1
    assert plugin._gate.store.get_request(request_id).state.value == "uncertain"


def test_terminal_xurl_quote_is_gated_and_never_calls_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = load_plugin()
    context = FakeContext(
        tmp_path,
        x_config={"enabled": True, "app": "life", "account": "DomAtSiteSage"},
    )
    provider_calls: list[list[str]] = []
    terminal_calls: list[dict[str, Any]] = []

    def run_xurl(command: list[str]) -> subprocess.CompletedProcess[str]:
        provider_calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"data": {"id": "2094000000000000001"}}),
            stderr="",
        )

    monkeypatch.setitem(plugin.x_create_post_handler.__globals__, "_run_xurl", run_xurl)
    plugin.register(context)
    args = {
        "command": (
            "xurl --app life --username DomAtSiteSage quote "
            "2093515564786540695 'Exact terminal post text.'"
        ),
        "timeout": 120,
        "workdir": str(tmp_path),
    }

    blocked = context.hooks["pre_tool_call"](
        tool_name="terminal",
        args=args,
        session_id="stored-session",
    )
    payload = json.loads(blocked["message"])
    request = plugin._gate.store.get_request(payload["request_id"])

    assert payload["status"] == "pending_approval"
    assert request is not None
    assert request.tool_name == "terminal"
    assert request.display == {
        "account": "DomAtSiteSage",
        "action": "quote",
        "quote_post_id": "2093515564786540695",
        "text": "Exact terminal post text.",
    }
    assert request.replay == {}
    assert "command" not in json.dumps(request.display)
    plugin._gate.store.decide(request.id, plugin.Decision.APPROVE, actor_id="owner")

    result = json.loads(
        context.middleware["tool_execution"](
            tool_name="terminal",
            args=args,
            session_id="stored-session",
            next_call=lambda payload: terminal_calls.append(payload),
        )
    )

    assert result["provider_id"] == "2094000000000000001"
    assert terminal_calls == []
    assert provider_calls == [
        [
            "xurl",
            "--app",
            "life",
            "quote",
            "2093515564786540695",
            "Exact terminal post text.",
            "--auth",
            "oauth2",
            "--username",
            "DomAtSiteSage",
        ]
    ]


def test_terminal_xurl_read_passes_through_without_gate(tmp_path: Path) -> None:
    plugin = load_plugin()
    context = FakeContext(tmp_path)
    plugin.register(context)
    args = {"command": "xurl read 2093515564786540695"}

    assert (
        context.hooks["pre_tool_call"](
            tool_name="terminal",
            args=args,
            session_id="stored-session",
        )
        is None
    )
    result = context.middleware["tool_execution"](
        tool_name="terminal",
        args=args,
        session_id="stored-session",
        next_call=lambda payload: {"ran": payload["command"]},
    )

    assert result == {"ran": "xurl read 2093515564786540695"}
    assert plugin._gate.store.list_requests() == []


@pytest.mark.parametrize(
    "command",
    ["printf human-gate-fixture", "touch /tmp/human-gate-fixture-42"],
)
def test_configured_terminal_exact_and_glob_are_blocked_by_middleware_before_execution(
    tmp_path: Path,
    command: str,
) -> None:
    plugin = load_plugin()
    context = FakeContext(
        tmp_path,
        policies=[
            {
                "tool_name": "terminal",
                "effect_kind": "local_fixture",
                "display_fields": ["command"],
                "replay_fields": ["command"],
                "command_exact": ["printf human-gate-fixture"],
                "command_glob": ["touch /tmp/human-gate-fixture-*"],
            }
        ],
    )
    plugin.register(context)
    executed: list[dict[str, Any]] = []

    result = context.middleware["tool_execution"](
        tool_name="terminal",
        args={"command": command},
        session_id="boring-local-session",
        next_call=lambda payload: executed.append(payload) or {"ok": True},
    )

    payload = json.loads(result)
    assert payload["status"] == "pending_approval"
    assert executed == []
    request = plugin._gate.store.get_request(payload["request_id"])
    assert request is not None
    assert request.state is plugin.RequestState.PENDING
    assert request.tool_name == "terminal"
    assert request.display == {"command": command}


def test_configured_arbitrary_tool_is_blocked_by_middleware_before_execution(
    tmp_path: Path,
) -> None:
    plugin = load_plugin()
    context = FakeContext(
        tmp_path,
        policies=[
            {
                "tool_name": "archive_record",
                "effect_kind": "archive",
                "display_fields": ["record_id"],
                "replay_fields": ["record_id"],
            }
        ],
    )
    plugin.register(context)
    executed: list[dict[str, Any]] = []

    result = context.middleware["tool_execution"](
        tool_name="archive_record",
        args={"record_id": "fixture-1"},
        session_id="boring-local-session",
        next_call=lambda payload: executed.append(payload) or {"ok": True},
    )

    payload = json.loads(result)
    assert payload["status"] == "pending_approval"
    assert executed == []
    request = plugin._gate.store.get_request(payload["request_id"])
    assert request is not None
    assert request.state is plugin.RequestState.PENDING
    assert request.tool_name == "archive_record"


def test_configured_third_party_tool_executes_exactly_once_after_claimed_approval(
    tmp_path: Path,
) -> None:
    plugin = load_plugin()
    context = FakeContext(
        tmp_path,
        policies=[
            {
                "tool_name": "archive_record",
                "effect_kind": "archive",
                "display_fields": ["record_id"],
                "replay_fields": ["record_id"],
            }
        ],
    )
    plugin.register(context)
    executed: list[dict[str, Any]] = []
    kwargs = {
        "tool_name": "archive_record",
        "args": {"record_id": "fixture-1"},
        "session_id": "boring-local-session",
        "next_call": lambda payload: executed.append(payload) or {"ok": True},
    }

    blocked = json.loads(context.middleware["tool_execution"](**kwargs))
    assert blocked["status"] == "pending_approval"
    assert executed == []
    request_id = blocked["request_id"]
    request = plugin._gate.store.get_request(request_id)
    assert request is not None
    plugin._gate.store.decide(
        request_id,
        plugin.Decision.APPROVE,
        actor_id="fixture-owner",
        expected_digest=request.call_digest,
        expected_record_version=request.record_version,
    )

    approved = json.loads(context.middleware["tool_execution"](**kwargs))
    assert approved == {"ok": True}
    assert executed == [{"record_id": "fixture-1"}]
    executed_record = plugin._gate.store.get_request(request_id)
    assert executed_record is not None
    assert executed_record.state is plugin.RequestState.EXECUTED

    replay = json.loads(context.middleware["tool_execution"](**kwargs))
    assert replay["status"] == "pending_approval"
    assert replay["request_id"] != request_id
    assert executed == [{"record_id": "fixture-1"}]


@pytest.mark.parametrize(
    "command",
    [
        'xurl -X POST /2/tweets -d \'{"text":"bypass"}\'',
        "xurl reply 2093515564786540695 'bypass'",
        "xurl token",
        "xurl post 'first'; xurl post 'second'",
    ],
)
def test_unsupported_terminal_xurl_writes_fail_closed(
    tmp_path: Path,
    command: str,
) -> None:
    plugin = load_plugin()
    context = FakeContext(tmp_path)
    plugin.register(context)

    blocked = context.hooks["pre_tool_call"](
        tool_name="terminal",
        args={"command": command},
        session_id="stored-session",
    )

    assert json.loads(blocked["message"])["status"] == "human_gate_xurl_unsupported"
    assert plugin._gate.store.list_requests() == []


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
    first_plugin.register(same_runtime_context)
    assert "human_gate_demo_effect" in same_runtime_context.tools
    assert "pre_tool_call" in same_runtime_context.hooks
    assert "tool_execution" in same_runtime_context.middleware
    assert (
        first_plugin._gate.store.get_request(request_id).state is first_plugin.RequestState.CLAIMED
    )

    second_plugin = load_plugin()
    second_context = FakeContext(tmp_path)
    second_plugin.register(second_context)

    assert second_plugin._gate is first_plugin._gate
    assert (
        second_plugin._gate.store.get_request(request_id).state
        is second_plugin.RequestState.CLAIMED
    )


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
    reset_process_runtime_for_tests()

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

    reset_process_runtime_for_tests()
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


def test_other_external_write_tools_are_not_gated_without_explicit_policy(tmp_path: Path) -> None:
    plugin = load_plugin()
    context = FakeContext(tmp_path)
    plugin.register(context)

    result = context.hooks["pre_tool_call"](
        tool_name="provider_create_post",
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


def test_claimed_owned_effect_is_not_reblocked_by_nested_pre_hook(tmp_path: Path) -> None:
    plugin = load_plugin()
    context = FakeContext(tmp_path)
    plugin.register(context)
    pre_hook = context.hooks["pre_tool_call"]
    middleware = context.middleware["tool_execution"]
    _schema, handler = context.tools["human_gate_demo_effect"]
    args = {"message": "nested host order"}

    blocked = pre_hook(
        tool_name="human_gate_demo_effect",
        args=args,
        session_id="stored-session",
    )
    request_id = json.loads(blocked["message"])["request_id"]
    plugin._gate.store.decide(request_id, plugin.Decision.APPROVE, actor_id="owner")

    def host_dispatch(payload: dict[str, Any]) -> str:
        nested_block = Context().run(
            pre_hook,
            tool_name="human_gate_demo_effect",
            args=payload,
            session_id="stored-session",
        )
        if nested_block is not None:
            return nested_block["message"]
        return handler(payload)

    result = middleware(
        tool_name="human_gate_demo_effect",
        args=args,
        session_id="stored-session",
        next_call=host_dispatch,
    )

    assert json.loads(result) == {
        "effect": "demo",
        "message": "nested host order",
        "ok": True,
    }
    assert plugin._gate.store.get_request(request_id).state.value == "executed"


def test_owned_effect_claim_is_consumed_by_first_matching_handler_call(
    tmp_path: Path,
) -> None:
    plugin = load_plugin()
    context = FakeContext(tmp_path)
    plugin.register(context)
    pre_hook = context.hooks["pre_tool_call"]
    middleware = context.middleware["tool_execution"]
    _schema, handler = context.tools["human_gate_demo_effect"]
    args = {"message": "approved once"}

    blocked = pre_hook(
        tool_name="human_gate_demo_effect",
        args=args,
        session_id="stored-session",
    )
    request_id = json.loads(blocked["message"])["request_id"]
    plugin._gate.store.decide(request_id, plugin.Decision.APPROVE, actor_id="owner")

    def call_from_copied_contexts(payload: dict[str, Any]) -> list[dict[str, Any]]:
        first_context = copy_context()
        second_context = copy_context()
        return [
            json.loads(first_context.run(handler, payload)),
            json.loads(second_context.run(handler, payload)),
        ]

    result = middleware(
        tool_name="human_gate_demo_effect",
        args=args,
        session_id="stored-session",
        next_call=call_from_copied_contexts,
    )

    assert result == [
        {"effect": "demo", "message": "approved once", "ok": True},
        {
            "error": "owned effect requires an active claimed approval",
            "ok": False,
            "status": "human_gate_required",
        },
    ]
    assert plugin._gate.store.get_request(request_id).state.value == "executed"
    receipts = plugin._gate.store.audit_history(request_id)
    assert [event["event_type"] for event in receipts].count("receipt") == 1


def test_owned_effect_claim_is_revoked_when_execution_scope_returns(
    tmp_path: Path,
) -> None:
    plugin = load_plugin()
    context = FakeContext(tmp_path)
    plugin.register(context)
    pre_hook = context.hooks["pre_tool_call"]
    middleware = context.middleware["tool_execution"]
    _schema, handler = context.tools["human_gate_demo_effect"]
    args = {"message": "do not defer"}

    blocked = pre_hook(
        tool_name="human_gate_demo_effect",
        args=args,
        session_id="stored-session",
    )
    request_id = json.loads(blocked["message"])["request_id"]
    plugin._gate.store.decide(request_id, plugin.Decision.APPROVE, actor_id="owner")
    delayed_contexts = []

    def defer_handler(_: dict[str, Any]) -> str:
        delayed_contexts.append(copy_context())
        return "returned without calling the owned handler"

    failed = json.loads(
        middleware(
            tool_name="human_gate_demo_effect",
            args=args,
            session_id="stored-session",
            next_call=defer_handler,
        )
    )

    assert failed["status"] == "owned_effect_claim_not_consumed"
    assert plugin._gate.store.get_request(request_id).state.value == "failed"
    delayed = json.loads(delayed_contexts[0].run(handler, args))
    assert delayed["status"] == "human_gate_required"


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
                "tool_name": "provider_create_post",
                "effect_kind": "publish",
                "display_fields": ["account", "text"],
                "replay_fields": ["account", "text"],
            }
        ],
    )
    plugin.register(context)

    blocked = context.hooks["pre_tool_call"](
        tool_name="provider_create_post",
        args={"account": "fixture", "text": "hello"},
        session_id="stored-session",
    )

    assert blocked["action"] == "block"
    request_id = json.loads(blocked["message"])["request_id"]
    assert plugin._gate.store.get_request(request_id).display == {
        "account": "fixture",
        "text": "hello",
    }
