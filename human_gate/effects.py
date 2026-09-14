from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import call_digest
from .claims import consume_active_claim
from .errors import EffectDefinitiveFailureError, EffectUncertainError
from .identity import (
    DEMO_EFFECT_TOOL,
    LEGACY_DEMO_EFFECT_TOOL,
    LEGACY_MOCK_PUBLISH_TOOL,
    MOCK_PUBLISH_TOOL,
)
from .store import GateStore

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_X_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_XURL_APP_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_POST_ID_RE = re.compile(r"^[0-9]{1,32}$")
_XURL_WRITE_ACTIONS = {
    "auth",
    "block",
    "bookmark",
    "delete",
    "dm",
    "follow",
    "like",
    "media",
    "mute",
    "post",
    "quote",
    "reply",
    "repost",
    "token",
    "unblock",
    "unbookmark",
    "unfollow",
    "unlike",
    "unmute",
    "unrepost",
    "webhook",
}
_SHELL_CONTROL_CHARS = frozenset(";&|><\n\r\x00`$")


@dataclass(frozen=True)
class XPublisherConfig:
    enabled: bool
    app: str
    account: str


@dataclass(frozen=True)
class TerminalXurlWrite:
    action: str
    text: str
    quote_post_id: str | None
    account: str | None
    app: str | None


def _terminal_command(args: dict[str, Any]) -> str | None:
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    return command


def parse_terminal_xurl_write(args: dict[str, Any]) -> TerminalXurlWrite | None:
    """Parse only simple post and quote commands that can be safely projected."""
    command = _terminal_command(args)
    if command is None or any(char in command for char in _SHELL_CONTROL_CHARS):
        return None
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not argv or Path(argv[0]).name != "xurl":
        return None
    app: str | None = None
    account: str | None = None
    auth: str | None = None
    index = 1
    while index < len(argv) and argv[index].startswith("-"):
        flag = argv[index]
        if flag not in {"--app", "--auth", "--username", "-u"} or index + 1 >= len(argv):
            return None
        value = argv[index + 1]
        if flag == "--app":
            app = value
        elif flag == "--auth":
            auth = value
        else:
            account = value.lstrip("@")
        index += 2
    if auth not in {None, "oauth2"} or index >= len(argv):
        return None
    action = argv[index]
    values = argv[index + 1 :]
    if action == "post" and len(values) == 1:
        text = values[0]
        quote_post_id = None
    elif action == "quote" and len(values) == 2:
        quote_post_id, text = values
    else:
        return None
    if not 1 <= len(text) <= 280 or "\x00" in text:
        return None
    if quote_post_id is not None and not _POST_ID_RE.fullmatch(quote_post_id):
        return None
    if app is not None and not _XURL_APP_RE.fullmatch(app):
        return None
    if account is not None and not _X_NAME_RE.fullmatch(account):
        return None
    return TerminalXurlWrite(
        action=action,
        text=text,
        quote_post_id=quote_post_id,
        account=account,
        app=app,
    )


def terminal_xurl_write_matches(args: dict[str, Any]) -> bool:
    return parse_terminal_xurl_write(args) is not None


def terminal_xurl_write_projection(args: dict[str, Any]) -> dict[str, Any]:
    parsed = parse_terminal_xurl_write(args)
    if parsed is None:
        raise ValueError("terminal xurl command is not safely projectable")
    display: dict[str, Any] = {
        "account": parsed.account or "configured X account",
        "action": parsed.action,
        "text": parsed.text,
    }
    if parsed.quote_post_id is not None:
        display["quote_post_id"] = parsed.quote_post_id
    return display


def terminal_xurl_write_is_unsupported(args: dict[str, Any]) -> bool:
    """Return whether a terminal command requests an X mutation we cannot gate safely."""
    command = _terminal_command(args)
    if command is None or "xurl" not in command.lower():
        return False
    if parse_terminal_xurl_write(args) is not None:
        return False
    lowered = command.lower()
    if any(char in command for char in _SHELL_CONTROL_CHARS):
        return any(action in lowered for action in _XURL_WRITE_ACTIONS)
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return True
    if not argv or Path(argv[0]).name != "xurl":
        return False
    tokens = {token.lower() for token in argv[1:]}
    if tokens & _XURL_WRITE_ACTIONS:
        return True
    methods = {"post", "put", "patch", "delete"}
    for index, token in enumerate(argv[:-1]):
        if token.lower() in {"-x", "--method"} and argv[index + 1].lower() in methods:
            return True
    return any(token.lower() in {"-d", "--data"} for token in argv)


def x_publisher_config(raw: Any) -> XPublisherConfig:
    """Parse the narrow non-secret X adapter settings or fail closed."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("x settings must be an object")
    unknown = set(raw) - {"enabled", "app", "account"}
    if unknown:
        raise ValueError(f"unknown x setting: {sorted(unknown)[0]}")
    enabled = raw.get("enabled", False)
    app = raw.get("app", "")
    account = raw.get("account", "")
    if not isinstance(enabled, bool):
        raise ValueError("x.enabled must be a boolean")
    if not isinstance(app, str) or (app and not _XURL_APP_RE.fullmatch(app)):
        raise ValueError("x.app is invalid")
    if not isinstance(account, str) or (account and not _X_NAME_RE.fullmatch(account)):
        raise ValueError("x.account is invalid")
    if enabled and (not app or not account):
        raise ValueError("enabled X publishing requires x.app and x.account")
    return XPublisherConfig(enabled=enabled, app=app, account=account)


def _run_xurl(command: list[str]) -> subprocess.CompletedProcess[str]:
    binary = shutil.which(command[0])
    if binary is None:
        raise EffectDefinitiveFailureError("xurl is not installed")
    try:
        result = subprocess.run(  # noqa: S603
            [binary, *command[1:]],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise EffectUncertainError("X response timed out after dispatch") from exc
    except OSError as exc:
        raise EffectDefinitiveFailureError("xurl could not start") from exc
    if result.returncode != 0:
        raise EffectUncertainError("X rejected or did not confirm the post")
    return result


def demo_effect_handler(args: dict[str, Any], **_: Any) -> str:
    """A deterministic owned effect used to prove the gate contract."""
    claim = consume_active_claim(DEMO_EFFECT_TOOL, args) or consume_active_claim(
        LEGACY_DEMO_EFFECT_TOOL, args
    )
    if claim is None:
        return json.dumps(
            {
                "ok": False,
                "status": "human_gate_required",
                "error": "owned effect requires an active claimed approval",
            },
            sort_keys=True,
        )
    message = str(args.get("message") or "")
    return json.dumps(
        {"effect": "demo", "message": message, "ok": True},
        sort_keys=True,
    )


def mock_publish_handler(store: GateStore, args: dict[str, Any], **_: Any) -> str:
    """Run a persistent local publisher fixture behind an active one-use claim."""
    claim = consume_active_claim(MOCK_PUBLISH_TOOL, args) or consume_active_claim(
        LEGACY_MOCK_PUBLISH_TOOL, args
    )
    if claim is None:
        return json.dumps(
            {
                "ok": False,
                "status": "human_gate_required",
                "error": "owned effect requires an active claimed approval",
            },
            sort_keys=True,
        )
    destination = args.get("destination")
    text = args.get("text")
    media_sha256 = args.get("media_sha256")
    idempotency_key = args.get("idempotency_key")
    simulate_outcome = args.get("simulate_outcome", "success")
    if destination != "mock":
        raise ValueError("mock publisher destination must be mock")
    if not isinstance(text, str) or not 1 <= len(text) <= 20_000:
        raise ValueError("text must contain 1 to 20000 characters")
    if not isinstance(media_sha256, list) or len(media_sha256) > 4:
        raise ValueError("media_sha256 must contain at most four digests")
    if any(not isinstance(value, str) or not _SHA256_RE.fullmatch(value) for value in media_sha256):
        raise ValueError("media_sha256 contains an invalid SHA-256 digest")
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 256:
        raise ValueError("idempotency_key must contain 1 to 256 characters")
    if simulate_outcome not in {"success", "failed", "uncertain"}:
        raise ValueError("simulate_outcome is invalid")
    if simulate_outcome == "failed":
        raise EffectDefinitiveFailureError("mock publisher rejected the fixture before dispatch")
    payload_digest = call_digest(
        {
            "schema": "hermes.human-gate.publish.v1",
            "destination": destination,
            "text": text,
            "media_sha256": media_sha256,
        }
    )
    publication = store.record_mock_publication(idempotency_key, payload_digest)
    if simulate_outcome == "uncertain" and publication.created:
        raise EffectUncertainError("mock publisher response was lost after dispatch")
    return json.dumps(
        {
            "ok": True,
            "provider": "mock",
            "provider_id": publication.provider_id,
            "replayed": not publication.created,
        },
        sort_keys=True,
    )


def _human_gate_required() -> str:
    return json.dumps(
        {
            "ok": False,
            "status": "human_gate_required",
            "error": "owned effect requires an active claimed approval",
        },
        sort_keys=True,
    )


def _publish_x_post(config: XPublisherConfig, args: dict[str, Any]) -> str:
    if not config.enabled:
        raise EffectDefinitiveFailureError("X publisher is not enabled")
    account = args.get("account")
    text = args.get("text")
    quote_post_id = args.get("quote_post_id")
    if account != config.account:
        raise EffectDefinitiveFailureError("X account does not match configured account")
    if not isinstance(text, str) or not 1 <= len(text) <= 280 or "\x00" in text:
        raise EffectDefinitiveFailureError("X text must contain 1 to 280 characters")
    if quote_post_id is not None and (
        not isinstance(quote_post_id, str) or not _POST_ID_RE.fullmatch(quote_post_id)
    ):
        raise EffectDefinitiveFailureError("X quote post id is invalid")
    if quote_post_id is None:
        command = ["xurl", "--app", config.app, "post", text]
    else:
        command = ["xurl", "--app", config.app, "quote", quote_post_id, text]
    command.extend(["--auth", "oauth2", "--username", config.account])
    completed = _run_xurl(command)
    try:
        provider_id = json.loads(completed.stdout)["data"]["id"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise EffectUncertainError("X returned no verifiable post id") from exc
    if not isinstance(provider_id, str) or not _POST_ID_RE.fullmatch(provider_id):
        raise EffectUncertainError("X returned an invalid post id")
    return json.dumps(
        {
            "account": config.account,
            "ok": True,
            "provider": "x",
            "provider_id": provider_id,
            "url": f"https://x.com/{config.account}/status/{provider_id}",
        },
        sort_keys=True,
    )


def x_create_post_handler(
    config: XPublisherConfig,
    args: dict[str, Any],
    **_: Any,
) -> str:
    """Create one X post after consuming an exact one-use tool claim."""
    if consume_active_claim("x_create_post", args) is None:
        return _human_gate_required()
    return _publish_x_post(config, args)


def terminal_xurl_write_handler(
    config: XPublisherConfig,
    args: dict[str, Any],
    **_: Any,
) -> str:
    """Convert one approved terminal xurl write into the owned X effect."""
    if consume_active_claim("terminal", args) is None:
        return _human_gate_required()
    parsed = parse_terminal_xurl_write(args)
    if parsed is None:
        raise EffectDefinitiveFailureError("terminal xurl command is not safely supported")
    if parsed.app is not None and parsed.app != config.app:
        raise EffectDefinitiveFailureError("X app does not match configured app")
    normalized: dict[str, Any] = {
        "account": parsed.account or config.account,
        "text": parsed.text,
    }
    if parsed.quote_post_id is not None:
        normalized["quote_post_id"] = parsed.quote_post_id
    return _publish_x_post(config, normalized)
