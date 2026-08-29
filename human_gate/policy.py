from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_SECRET_FIELD_PARTS = (
    "authorization",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
    "cookie",
    "api_key",
)
_MAX_POLICIES = 64
_MAX_FIELDS = 64


def _field_names(value: Any, *, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > _MAX_FIELDS:
        raise ValueError(f"{label} must be a list with at most {_MAX_FIELDS} fields")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _NAME_RE.fullmatch(item):
            raise ValueError(f"{label} contains an invalid field name")
        normalized = item.lower()
        if any(part in normalized for part in _SECRET_FIELD_PARTS):
            raise ValueError(f"{label} contains a secret-bearing field")
        if item in result:
            raise ValueError(f"{label} contains a duplicate field")
        result.append(item)
    return tuple(result)


@dataclass(frozen=True)
class ToolPolicy:
    """An explicit durable-gate policy for one Hermes tool name."""

    tool_name: str
    effect_kind: str
    display_fields: tuple[str, ...] = ()
    replay_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.tool_name.strip():
            raise ValueError("tool_name is required")
        if not self.effect_kind.strip():
            raise ValueError("effect_kind is required")

    def display_projection(self, args: dict[str, Any]) -> dict[str, Any]:
        return {field: args[field] for field in self.display_fields if field in args}

    def replay_projection(self, args: dict[str, Any]) -> dict[str, Any]:
        return {field: args[field] for field in self.replay_fields if field in args}


class PolicyRegistry:
    def __init__(self) -> None:
        self._policies: dict[str, ToolPolicy] = {}

    def register(self, policy: ToolPolicy) -> None:
        if policy.tool_name in self._policies:
            raise ValueError(f"policy already registered for {policy.tool_name}")
        self._policies[policy.tool_name] = policy

    def get(self, tool_name: str) -> ToolPolicy | None:
        return self._policies.get(tool_name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._policies))


def policies_from_config(raw: Any) -> PolicyRegistry:
    """Parse explicit profile-scoped policy settings and reject ambiguous input."""
    registry = PolicyRegistry()
    if raw is None:
        return registry
    if not isinstance(raw, list) or len(raw) > _MAX_POLICIES:
        raise ValueError(f"policies must be a list with at most {_MAX_POLICIES} entries")
    allowed_keys = {"tool_name", "effect_kind", "display_fields", "replay_fields"}
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise ValueError("each policy must be an object")
        unknown = set(entry) - allowed_keys
        if unknown:
            raise ValueError(f"policy contains unknown fields: {', '.join(sorted(unknown))}")
        tool_name = entry.get("tool_name")
        effect_kind = entry.get("effect_kind")
        if not isinstance(tool_name, str) or not _NAME_RE.fullmatch(tool_name):
            raise ValueError("policy tool_name is invalid")
        if not isinstance(effect_kind, str) or not _NAME_RE.fullmatch(effect_kind):
            raise ValueError("policy effect_kind is invalid")
        display_fields = _field_names(entry.get("display_fields"), label="display_fields")
        replay_fields = _field_names(entry.get("replay_fields"), label="replay_fields")
        if not set(replay_fields).issubset(display_fields):
            raise ValueError("replay_fields must be a subset of display_fields")
        registry.register(
            ToolPolicy(
                tool_name=tool_name,
                effect_kind=effect_kind,
                display_fields=display_fields,
                replay_fields=replay_fields,
            )
        )
    return registry
