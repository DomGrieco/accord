from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
