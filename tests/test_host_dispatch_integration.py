"""Opt-in host+Accord dispatch integration (no providers, no real profiles).

Requires subprocess-local env, never committed user paths:

- ``ACCORD_HOST_SOURCE``: reviewed Hermes checkout to import
- ``ACCORD_HOST_PYTHON``: interpreter that can import that checkout
- ``ACCORD_HOST_PIN``: optional exact ``git rev-parse HEAD`` of that checkout


Default pytest skips this module. Synthetic ``GateStore.decide`` approvals are
fixture-only and are labeled as such. Nested ``execute_code`` is out of scope.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

ACCORD_ROOT = Path(__file__).resolve().parents[1]
HOST_SOURCE_ENV = "ACCORD_HOST_SOURCE"
HOST_PYTHON_ENV = "ACCORD_HOST_PYTHON"
HOST_PIN_ENV = "ACCORD_HOST_PIN"

CHILD_ENV = "ACCORD_HOST_INTEGRATION_CHILD"
RESULT_PREFIX = "ACCORD_HOST_INTEGRATION_RESULT:"

STORED_KEY = "stored-host-1"
OTHER_KEY = "stored-host-2"
RUNTIME_1 = "runtime-host-1"
RUNTIME_2 = "runtime-host-2"
POISON = "poison-ambient-session"
TOOL_NAME = "accord_mock_publish"
SYNTHETIC_ACTOR = "synthetic-fixture-owner"
SYNTHETIC_COMMENT = "SYNTHETIC test fixture approval; not a human owner decision"
PUBLISH_ARGS: dict[str, Any] = {
    "destination": "mock",
    "text": "host-dispatch-fixture",
    "media_sha256": [],
    "idempotency_key": "host-dispatch-once",
    "simulate_outcome": "success",
}
CHANGED_ARGS: dict[str, Any] = {
    **PUBLISH_ARGS,
    "text": "changed-host-dispatch-fixture",
    "idempotency_key": "host-dispatch-changed",
}

pytestmark = pytest.mark.skipif(
    not os.environ.get(HOST_SOURCE_ENV, "").strip()
    or not os.environ.get(HOST_PYTHON_ENV, "").strip()
    or os.environ.get(CHILD_ENV) == "1",
    reason="opt-in host integration: set ACCORD_HOST_SOURCE and ACCORD_HOST_PYTHON",
)


def _required_path(name: str) -> Path:
    raw = os.environ.get(name, "").strip()
    if not raw:
        raise pytest.skip(f"{name} is required")
    # Resolving the interpreter symlink discards its virtualenv/site-packages.
    path = Path(raw).expanduser().absolute()
    if not path.exists():
        pytest.fail(f"{name} does not exist: {path}")
    return path


def _child_env(temp_home: Path, *, host_source: Path) -> dict[str, str]:
    # Do not inherit credentials, profile overrides, or the operator's home.
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
    env["HOME"] = str(temp_home)
    env["HERMES_HOME"] = str(temp_home)
    env[HOST_SOURCE_ENV] = str(host_source)
    env[HOST_PIN_ENV] = os.environ.get(HOST_PIN_ENV, "")
    env["ACCORD_PLUGIN_ROOT"] = str(ACCORD_ROOT)
    env[CHILD_ENV] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPYCACHEPREFIX"] = str(temp_home / "pycache")
    env["PYTHONPATH"] = os.pathsep.join((str(host_source), str(ACCORD_ROOT)))
    return env


def _parse_child_result(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        if line.startswith(RESULT_PREFIX):
            payload = json.loads(line[len(RESULT_PREFIX) :])
            if isinstance(payload, dict):
                return payload
    raise AssertionError(f"child did not emit {RESULT_PREFIX}: {stdout[-2000:]}")


def _run_child(
    scenario: str,
    route: str,
    *,
    host_source: Path,
) -> dict[str, Any]:
    host_python = _required_path(HOST_PYTHON_ENV)
    with tempfile.TemporaryDirectory(prefix="accord-host-int-") as raw_home:
        home = Path(raw_home)
        completed = subprocess.run(  # noqa: S603
            [str(host_python), str(Path(__file__).resolve()), scenario, route],
            cwd=str(home),
            env=_child_env(home, host_source=host_source),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(
                "host integration child failed\n"
                f"stdout:\n{completed.stdout[-4000:]}\n"
                f"stderr:\n{completed.stderr[-4000:]}"
            )
        return _parse_child_result(completed.stdout)


@pytest.mark.parametrize("route", ["direct", "deferred"])
def test_host_dispatch_stores_stable_key_and_executes_owned_mock_once(route: str) -> None:
    result = _run_child("happy", route, host_source=_required_path(HOST_SOURCE_ENV))
    assert result["scenario"] == "happy"
    assert result["route"] == route
    assert result["imported_plugins"].startswith(str(_required_path(HOST_SOURCE_ENV)))
    assert result["imported_model_tools"].startswith(str(_required_path(HOST_SOURCE_ENV)))
    expected_pin = os.environ.get(HOST_PIN_ENV, "").strip()
    if expected_pin:
        assert result["host_head"] == expected_pin
    assert result["pending_session_id"] == STORED_KEY
    assert result["pending_session_lineage"] == STORED_KEY
    assert result["pending_session_id"] != RUNTIME_1
    assert result["first_status"] == "pending_approval"
    assert result["executed_ok"] is True
    assert result["publication_count_after_execute"] == 1
    assert result["publication_count_after_retry"] == 1
    assert result["retry_published"] is False
    assert result["retry_status"] == "pending_approval"
    assert result["original_state_after_retry"] == "executed"


@pytest.mark.parametrize("route", ["direct", "deferred"])
def test_host_dispatch_fails_closed_without_authority(route: str) -> None:
    result = _run_child("closed", route, host_source=_required_path(HOST_SOURCE_ENV))
    assert result["missing_status"] == "human_gate_unroutable"
    assert result["missing_request_count"] == 0
    assert result["missing_publication_count"] == 0
    assert result["poison_used"] is False
    assert result["changed_key_consumed_original"] is False
    assert result["changed_args_consumed_original"] is False
    assert result["changed_key_status"] == "pending_approval"
    assert result["changed_args_status"] == "pending_approval"
    assert result["original_state_after_mismatches"] == "approved"
    assert result["publication_count"] == 0



# --- child process -----------------------------------------------------------


def _decode_tool_result(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        payload = raw
    else:
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {"raw": raw}
    if not isinstance(payload, dict):
        return {"raw": raw}
    error = payload.get("error")
    if isinstance(error, str):
        try:
            nested = json.loads(error)
        except json.JSONDecodeError:
            return payload
        if isinstance(nested, dict):
            return nested
    return payload


def _load_accord_plugin():
    root = Path(os.environ["ACCORD_PLUGIN_ROOT"]).resolve()
    spec = importlib.util.spec_from_file_location(
        "accord_host_integration_plugin",
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _install_host_manager(monkey_home: Path):
    import hermes_cli.plugins as plugins_mod
    from hermes_cli.plugins import PluginContext, PluginManager
    from hermes_cli.plugins_manifest import PluginManifest

    manager = PluginManager()
    manager._discovered = True
    plugins_mod._plugin_manager = manager
    plugins_mod.get_plugin_manager = lambda: manager
    ctx = PluginContext(PluginManifest(name="accord"), manager)
    plugin = _load_accord_plugin()
    plugin.register(ctx)
    from tools.registry import registry

    assert registry.get_entry(TOOL_NAME) is not None
    return plugins_mod, plugin, ctx, monkey_home


def _assert_imported_from_host() -> dict[str, str]:
    host = Path(os.environ[HOST_SOURCE_ENV]).resolve()
    import hermes_cli.plugins as plugins_mod
    import model_tools
    from tools import registry as tools_registry

    plugins_path = Path(plugins_mod.__file__).resolve()
    model_tools_path = Path(model_tools.__file__).resolve()
    registry_path = Path(tools_registry.__file__).resolve()
    for label, path in (
        ("hermes_cli.plugins", plugins_path),
        ("model_tools", model_tools_path),
        ("tools.registry", registry_path),
    ):
        if not path.is_relative_to(host):
            raise AssertionError(f"{label} imported from {path}, not under {host}")
    head = ""
    git_binary = shutil.which("git")
    assert git_binary is not None, "git is required to verify the host artifact"
    git = subprocess.run(  # noqa: S603
        [git_binary, "-C", str(host), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if git.returncode == 0:
        head = git.stdout.strip()
    expected = os.environ.get(HOST_PIN_ENV, "").strip()
    if expected and head != expected:
        raise AssertionError(f"host HEAD {head} != ACCORD_HOST_PIN {expected}")
    return {
        "imported_plugins": str(plugins_path),
        "imported_model_tools": str(model_tools_path),
        "imported_registry": str(registry_path),
        "host_head": head,
        "host_source": str(host),
    }


def _handle_function_call(tool_name: str, args: dict[str, Any], route: str, **ids: Any) -> str:
    from model_tools import handle_function_call

    kwargs = {key: value for key, value in ids.items() if value is not None}
    if route == "deferred":
        return handle_function_call("tool_call", {"name": tool_name, "arguments": args}, **kwargs)
    return handle_function_call(tool_name, args, **kwargs)


def _dispatch_ids(**extra: Any) -> dict[str, Any]:
    ids = {
        "task_id": "accord-host-task",
        "tool_call_id": extra.pop("tool_call_id", "tc-1"),
        "session_id": extra.pop("session_id", ""),
        "session_key": extra.pop("session_key", ""),
    }
    ids.update(extra)
    return ids


def _run_happy(route: str) -> dict[str, Any]:
    pins = _assert_imported_from_host()
    from tools.approval_context import reset_current_session_key, set_current_session_key

    home = Path(os.environ["HERMES_HOME"])
    plugins_mod, plugin, _ctx, _home = _install_host_manager(home)
    token = set_current_session_key(POISON)
    os.environ["HERMES_SESSION_KEY"] = POISON
    try:
        first = _decode_tool_result(
            _handle_function_call(
                TOOL_NAME,
                PUBLISH_ARGS,
                route,
                **_dispatch_ids(session_id=RUNTIME_1, session_key=STORED_KEY, tool_call_id="tc-1"),
            )
        )
        assert first.get("status") == "pending_approval", first
        request_id = first["request_id"]
        pending = plugin._gate.store.get_request(request_id)
        assert pending is not None
        assert pending.session_id == STORED_KEY
        assert pending.session_lineage == STORED_KEY
        plugin._gate.store.decide(
            request_id,
            plugin.Decision.APPROVE,
            actor_id=SYNTHETIC_ACTOR,
            comment=SYNTHETIC_COMMENT,
            actor_kind="synthetic-fixture",
        )
        executed = _decode_tool_result(
            _handle_function_call(
                TOOL_NAME,
                PUBLISH_ARGS,
                route,
                **_dispatch_ids(session_id=RUNTIME_2, session_key=STORED_KEY, tool_call_id="tc-2"),
            )
        )
        assert executed.get("ok") is True, executed
        count_after = plugin._gate.store.count_mock_publications()
        retry = _decode_tool_result(
            _handle_function_call(
                TOOL_NAME,
                PUBLISH_ARGS,
                route,
                **_dispatch_ids(session_id=RUNTIME_2, session_key=STORED_KEY, tool_call_id="tc-3"),
            )
        )
        count_retry = plugin._gate.store.count_mock_publications()
        original = plugin._gate.store.get_request(request_id)
        return {
            "scenario": "happy",
            "route": route,
            **pins,
            "first_status": first.get("status"),
            "pending_session_id": pending.session_id,
            "pending_session_lineage": pending.session_lineage,
            "executed_ok": bool(executed.get("ok")),
            "publication_count_after_execute": count_after,
            "publication_count_after_retry": count_retry,
            "retry_published": retry.get("ok") is True and retry.get("replayed") is not True,
            "retry_status": retry.get("status") or retry.get("ok"),
            "original_state_after_retry": original.state.value if original else None,
            "hermes_home": str(home),
        }
    finally:
        reset_current_session_key(token)
        os.environ.pop("HERMES_SESSION_KEY", None)
        from human_gate.runtime_registry import reset_process_runtime_for_tests

        reset_process_runtime_for_tests()
        _ = plugins_mod


def _run_closed(route: str) -> dict[str, Any]:
    pins = _assert_imported_from_host()
    from tools.approval_context import reset_current_session_key, set_current_session_key

    home = Path(os.environ["HERMES_HOME"])
    _plugins_mod, plugin, _ctx, _home = _install_host_manager(home)
    token = set_current_session_key(POISON)
    os.environ["HERMES_SESSION_KEY"] = POISON
    try:
        missing = _decode_tool_result(
            _handle_function_call(TOOL_NAME, PUBLISH_ARGS, route, **_dispatch_ids())
        )
        missing_requests = plugin._gate.store.list_requests()
        missing_count = plugin._gate.store.count_mock_publications()
        first = _decode_tool_result(
            _handle_function_call(
                TOOL_NAME,
                PUBLISH_ARGS,
                route,
                **_dispatch_ids(session_id=RUNTIME_1, session_key=STORED_KEY, tool_call_id="tc-ok"),
            )
        )
        request_id = first["request_id"]
        plugin._gate.store.decide(
            request_id,
            plugin.Decision.APPROVE,
            actor_id=SYNTHETIC_ACTOR,
            comment=SYNTHETIC_COMMENT,
            actor_kind="synthetic-fixture",
        )
        changed_key = _decode_tool_result(
            _handle_function_call(
                TOOL_NAME,
                PUBLISH_ARGS,
                route,
                **_dispatch_ids(session_id=RUNTIME_2, session_key=OTHER_KEY, tool_call_id="tc-key"),
            )
        )
        changed_args = _decode_tool_result(
            _handle_function_call(
                TOOL_NAME,
                CHANGED_ARGS,
                route,
                **_dispatch_ids(session_id=RUNTIME_2, session_key=STORED_KEY, tool_call_id="tc-args"),
            )
        )
        original = plugin._gate.store.get_request(request_id)
        return {
            "scenario": "closed",
            "route": route,
            **pins,
            "missing_status": missing.get("status"),
            "missing_request_count": len(missing_requests),
            "missing_publication_count": missing_count,
            "poison_used": any(
                record.session_id == POISON or record.session_lineage == POISON
                for record in plugin._gate.store.list_requests()
            ),
            "changed_key_status": changed_key.get("status"),
            "changed_key_consumed_original": changed_key.get("ok") is True,
            "changed_args_status": changed_args.get("status"),
            "changed_args_consumed_original": changed_args.get("ok") is True,
            "original_state_after_mismatches": original.state.value if original else None,
            "publication_count": plugin._gate.store.count_mock_publications(),
            "hermes_home": str(home),
        }
    finally:
        reset_current_session_key(token)
        os.environ.pop("HERMES_SESSION_KEY", None)
        from human_gate.runtime_registry import reset_process_runtime_for_tests

        reset_process_runtime_for_tests()


def _child_main(argv: list[str]) -> int:
    scenario = argv[1] if len(argv) > 1 else "happy"
    route = argv[2] if len(argv) > 2 else "direct"
    if scenario == "happy":
        result = _run_happy(route)
    elif scenario == "closed":
        result = _run_closed(route)
    else:
        raise SystemExit(f"unknown scenario {scenario}")
    print(RESULT_PREFIX + json.dumps(result, sort_keys=True), flush=True)
    return 0


if os.environ.get(CHILD_ENV) == "1" and __name__ == "__main__":
    raise SystemExit(_child_main(sys.argv))
