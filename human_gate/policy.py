from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from hashlib import sha256
from json import dumps
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

ArgumentMatcher = Callable[[dict[str, Any]], bool] | None
ArgumentSpecificity = Callable[[dict[str, Any]], int] | None
ArgumentProjector = Callable[[dict[str, Any]], Mapping[str, Any]] | None


def is_safe_projection_field(value: Any) -> bool:
    """Return whether a field name is valid and not secret-bearing."""
    if not isinstance(value, str) or not _NAME_RE.fullmatch(value):
        return False
    normalized = value.lower()
    return not any(part in normalized for part in _SECRET_FIELD_PARTS)


def _field_names(value: Any, *, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > _MAX_FIELDS:
        raise ValueError(f"{label} must be a list with at most {_MAX_FIELDS} fields")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _NAME_RE.fullmatch(item):
            raise ValueError(f"{label} contains an invalid field name")
        if not is_safe_projection_field(item):
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
    owned_effect: bool = False
    tool_name_is_glob: bool = False
    command_exact: tuple[str, ...] = ()
    command_glob: tuple[str, ...] = ()
    matcher_identity: str | None = None
    display_projector_identity: str | None = None
    replay_projector_identity: str | None = None
    owned_handler_identity: str | None = None
    owned_execution_identity: tuple[tuple[str, str | bool], ...] = ()
    argument_matcher: ArgumentMatcher = field(default=None, compare=False, repr=False)
    argument_specificity: ArgumentSpecificity = field(default=None, compare=False, repr=False)
    display_projector: ArgumentProjector = field(default=None, compare=False, repr=False)
    replay_projector: ArgumentProjector = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not self.tool_name.strip():
            raise ValueError("tool_name is required")
        if not self.effect_kind.strip():
            raise ValueError("effect_kind is required")
        identities = (
            self.matcher_identity,
            self.display_projector_identity,
            self.replay_projector_identity,
            self.owned_handler_identity,
        )
        if any(identity is not None and not identity.strip() for identity in identities):
            raise ValueError("policy identities must be non-empty strings")
        if (self.argument_matcher is not None or self.argument_specificity is not None) and not (
            self.matcher_identity
        ):
            raise ValueError("callable argument matching requires matcher_identity")
        if self.display_projector is not None and not self.display_projector_identity:
            raise ValueError("callable display projection requires display_projector_identity")
        if self.replay_projector is not None and not self.replay_projector_identity:
            raise ValueError("callable replay projection requires replay_projector_identity")
        if self.owned_effect and not self.owned_handler_identity:
            raise ValueError("owned effects require owned_handler_identity")
        if self.owned_execution_identity and not self.owned_effect:
            raise ValueError("owned execution identity requires an owned effect")
        execution_keys: set[str] = set()
        for key, value in self.owned_execution_identity:
            if not _NAME_RE.fullmatch(key):
                raise ValueError("owned execution identity contains an invalid field name")
            if not is_safe_projection_field(key):
                raise ValueError("owned execution identity contains a secret-bearing field")
            if key in execution_keys:
                raise ValueError("owned execution identity contains a duplicate field")
            if not isinstance(value, (str, bool)) or (isinstance(value, str) and len(value) > 256):
                raise ValueError("owned execution identity contains an invalid value")
            execution_keys.add(key)

    def registration_identity(self) -> tuple[Any, ...]:
        """Return the normalized enforcement identity without comparing callables."""
        return (
            ("tool_selector", "glob" if self.tool_name_is_glob else "exact", self.tool_name),
            ("commands", tuple(sorted(self.command_exact)), tuple(sorted(self.command_glob))),
            ("effect", self.effect_kind),
            (
                "projections",
                tuple(sorted(self.display_fields)),
                tuple(sorted(self.replay_fields)),
                self.display_projector_identity,
                self.replay_projector_identity,
            ),
            ("matcher", self.matcher_identity),
            (
                "owned_effect",
                self.owned_effect,
                self.owned_handler_identity,
                self.owned_execution_identity,
            ),
        )

    def fingerprint(self) -> str:
        """Return a stable digest used by registration and approval binding."""
        encoded = dumps(
            self.registration_identity(),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    def applies_to(self, args: dict[str, Any]) -> bool:
        if self.argument_matcher is None:
            return True
        return bool(self.argument_matcher(args))

    def matches_tool(self, tool_name: str) -> bool:
        if self.tool_name_is_glob:
            return fnmatchcase(tool_name, self.tool_name)
        return tool_name == self.tool_name

    def specificity(self, args: dict[str, Any]) -> tuple[int, int, int]:
        argument = self.argument_specificity(args) if self.argument_specificity else 0
        return (int(self.owned_effect), int(not self.tool_name_is_glob), argument)

    def project_display(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.display_projector is not None:
            return dict(self.display_projector(args))
        return {field: args[field] for field in self.display_fields if field in args}

    def display_projection(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.project_display(args)

    def project_replay(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.replay_projector is not None:
            return dict(self.replay_projector(args))
        return {field: args[field] for field in self.replay_fields if field in args}

    def replay_projection(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.project_replay(args)


class PolicyRegistry:
    def __init__(self) -> None:
        self._policies: dict[str, list[ToolPolicy]] = {}

    def register(self, policy: ToolPolicy) -> None:
        existing = self._policies.setdefault(policy.tool_name, [])
        if (
            any(item.argument_matcher is None for item in existing)
            and policy.argument_matcher is None
        ):
            raise ValueError(f"unconditional policy already registered for {policy.tool_name}")
        for item in existing:
            if set(item.command_exact) & set(policy.command_exact):
                raise ValueError(f"command selector already registered for {policy.tool_name}")
            if set(item.command_glob) & set(policy.command_glob):
                raise ValueError(f"command selector already registered for {policy.tool_name}")
        existing.append(policy)

    def get(self, tool_name: str) -> ToolPolicy | None:
        policies = self._policies.get(tool_name, [])
        return policies[0] if policies else None

    def match(self, tool_name: str, args: dict[str, Any]) -> ToolPolicy | None:
        policies = [
            policy
            for configured in self._policies.values()
            for policy in configured
            if policy.matches_tool(tool_name)
        ]
        applicable = [policy for policy in policies if policy.applies_to(args)]
        if not applicable:
            return None
        highest = max(policy.specificity(args) for policy in applicable)
        winners = [policy for policy in applicable if policy.specificity(args) == highest]
        if len({policy.fingerprint() for policy in winners}) != 1:
            raise ValueError(f"ambiguous policies match {tool_name}")
        return winners[0]

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._policies))

    def all(self) -> tuple[ToolPolicy, ...]:
        return tuple(policy for name in self.names() for policy in self._policies[name])

    def registration_identity(self) -> tuple[str, ...]:
        return tuple(policy.fingerprint() for policy in self.all())


def _command_patterns(value: Any, *, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > _MAX_FIELDS:
        raise ValueError(f"{label} must be a list with at most {_MAX_FIELDS} commands")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > 4096:
            raise ValueError(f"{label} contains an invalid command")
        if item in result:
            raise ValueError(f"{label} contains a duplicate command")
        result.append(item)
    return tuple(result)


def policies_from_config(raw: Any) -> PolicyRegistry:
    """Parse explicit profile-scoped policy settings and reject ambiguous input."""
    registry = PolicyRegistry()
    if raw is None:
        return registry
    if not isinstance(raw, list) or len(raw) > _MAX_POLICIES:
        raise ValueError(f"policies must be a list with at most {_MAX_POLICIES} entries")
    allowed_keys = {
        "tool_name",
        "tool_glob",
        "effect_kind",
        "display_fields",
        "replay_fields",
        "command_exact",
        "command_glob",
    }
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise ValueError("each policy must be an object")
        unknown = set(entry) - allowed_keys
        if unknown:
            raise ValueError(f"policy contains unknown fields: {', '.join(sorted(unknown))}")
        tool_name = entry.get("tool_name")
        tool_glob = entry.get("tool_glob")
        effect_kind = entry.get("effect_kind")
        if (tool_name is None) == (tool_glob is None):
            raise ValueError("policy requires exactly one of tool_name or tool_glob")
        if tool_glob is not None:
            if not isinstance(tool_glob, str) or not tool_glob.strip() or len(tool_glob) > 128:
                raise ValueError("policy tool_glob is invalid")
            configured_tool_name = tool_glob
            tool_name_is_glob = True
        else:
            if not isinstance(tool_name, str) or not _NAME_RE.fullmatch(tool_name):
                raise ValueError("policy tool_name is invalid")
            configured_tool_name = tool_name
            tool_name_is_glob = False
        if not isinstance(effect_kind, str) or not _NAME_RE.fullmatch(effect_kind):
            raise ValueError("policy effect_kind is invalid")
        display_fields = _field_names(entry.get("display_fields"), label="display_fields")
        replay_fields = _field_names(entry.get("replay_fields"), label="replay_fields")
        if not set(replay_fields).issubset(display_fields):
            raise ValueError("replay_fields must be a subset of display_fields")
        command_exact = _command_patterns(entry.get("command_exact"), label="command_exact")
        command_glob = _command_patterns(entry.get("command_glob"), label="command_glob")
        if (command_exact or command_glob) and configured_tool_name != "terminal":
            raise ValueError("command_exact and command_glob are only valid for terminal policies")

        def command_matches(
            args: dict[str, Any],
            *,
            exact: tuple[str, ...] = command_exact,
            globs: tuple[str, ...] = command_glob,
        ) -> bool:
            if not exact and not globs:
                return True
            command = args.get("command")
            return isinstance(command, str) and (
                command in exact or any(fnmatchcase(command, pattern) for pattern in globs)
            )

        def command_specificity(
            args: dict[str, Any],
            exact: tuple[str, ...] = command_exact,
            globs: tuple[str, ...] = command_glob,
        ) -> int:
            command = args.get("command")
            if not isinstance(command, str):
                return 0
            if command in exact:
                return 2
            if any(fnmatchcase(command, pattern) for pattern in globs):
                return 1
            return 0

        registry.register(
            ToolPolicy(
                tool_name=configured_tool_name,
                effect_kind=effect_kind,
                display_fields=display_fields,
                replay_fields=replay_fields,
                command_exact=command_exact,
                command_glob=command_glob,
                matcher_identity=(
                    "terminal-command-patterns-v1" if configured_tool_name == "terminal" else None
                ),
                argument_matcher=command_matches if configured_tool_name == "terminal" else None,
                argument_specificity=(
                    command_specificity if configured_tool_name == "terminal" else None
                ),
                tool_name_is_glob=tool_name_is_glob,
            )
        )
    return registry
