from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from .canonical import call_digest


@dataclass(frozen=True)
class ActiveClaim:
    request_id: str
    tool_name: str
    call_digest: str
    args_digest: str


_active_claim: ContextVar[ActiveClaim | None] = ContextVar(
    "human_gate_active_claim", default=None
)


def activate_claim(claim: ActiveClaim) -> Token[ActiveClaim | None]:
    return _active_claim.set(claim)


def reset_claim(token: Token[ActiveClaim | None]) -> None:
    _active_claim.reset(token)


def require_active_claim(tool_name: str, args: dict[str, Any]) -> ActiveClaim | None:
    claim = _active_claim.get()
    if claim is None:
        return None
    if claim.tool_name != tool_name:
        return None
    if claim.args_digest != call_digest(args):
        return None
    return claim
