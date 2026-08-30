from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from threading import Lock, get_ident
from typing import Any

from .canonical import call_digest


@dataclass
class ActiveClaim:
    request_id: str
    tool_name: str
    call_digest: str
    args_digest: str
    owner_thread_id: int = field(default_factory=get_ident, init=False)
    _consumed: bool = field(default=False, init=False, repr=False)
    _active: bool = field(default=True, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def consume(self) -> bool:
        """Consume this authority once, including across copied contexts."""
        with self._lock:
            if not self._active or self._consumed or get_ident() != self.owner_thread_id:
                return False
            self._consumed = True
            return True

    def revoke(self) -> None:
        """Invalidate every context copy when execution scope ends."""
        with self._lock:
            self._active = False

    def was_consumed(self) -> bool:
        with self._lock:
            return self._consumed

    def can_reach_owned_effect(self) -> bool:
        """Return whether this thread still holds the unconsumed claim."""
        with self._lock:
            return self._active and not self._consumed and get_ident() == self.owner_thread_id


_active_claim: ContextVar[ActiveClaim | None] = ContextVar("human_gate_active_claim", default=None)


def activate_claim(claim: ActiveClaim) -> Token[ActiveClaim | None]:
    return _active_claim.set(claim)


def reset_claim(token: Token[ActiveClaim | None]) -> None:
    _active_claim.reset(token)


def active_claim_matches(tool_name: str, args: dict[str, Any]) -> bool:
    """Check, without consuming, whether dispatch is inside the matching claim."""
    claim = _active_claim.get()
    if claim is None or claim.tool_name != tool_name:
        return False
    if claim.args_digest != call_digest(args):
        return False
    return claim.can_reach_owned_effect()


def consume_active_claim(tool_name: str, args: dict[str, Any]) -> ActiveClaim | None:
    """Return and consume the one matching authority in this execution context."""
    claim = _active_claim.get()
    if claim is None:
        return None
    if claim.tool_name != tool_name:
        return None
    if claim.args_digest != call_digest(args):
        return None
    if not claim.consume():
        return None
    _active_claim.set(None)
    return claim
