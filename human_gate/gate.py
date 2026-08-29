from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .canonical import call_digest
from .claims import ActiveClaim, activate_claim, reset_claim
from .effects import EffectUncertainError
from .models import RequestState
from .policy import PolicyRegistry, ToolPolicy
from .store import GateStore


class GateDecision(str, Enum):
    ALLOW = "allow"
    PENDING = "pending"
    APPROVED = "approved"


@dataclass(frozen=True)
class GateResult:
    decision: GateDecision
    request_id: str | None = None
    call_digest: str | None = None


class HumanGate:
    def __init__(self, store: GateStore, policies: PolicyRegistry, *, profile: str) -> None:
        self.store = store
        self.policies = policies
        self.profile = profile

    def _envelope(
        self,
        policy: ToolPolicy,
        args: dict[str, Any],
        *,
        session_lineage: str,
    ) -> dict[str, Any]:
        return {
            "schema": "hermes.human-gate.call.v1",
            "profile": self.profile,
            "session_lineage": session_lineage,
            "tool_name": policy.tool_name,
            "effect_kind": policy.effect_kind,
            "arguments": args,
        }

    def _digest(
        self,
        policy: ToolPolicy,
        args: dict[str, Any],
        *,
        session_lineage: str,
    ) -> str:
        return call_digest(self._envelope(policy, args, session_lineage=session_lineage))

    def intercept(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        session_id: str,
        session_lineage: str,
    ) -> GateResult:
        policy = self.policies.get(tool_name)
        if policy is None:
            return GateResult(GateDecision.ALLOW)
        digest = self._digest(policy, args, session_lineage=session_lineage)
        approved = self.store.find_matching_approved(
            profile=self.profile,
            session_lineage=session_lineage,
            tool_name=tool_name,
            call_digest=digest,
        )
        if approved is not None:
            return GateResult(GateDecision.APPROVED, approved.id, digest)
        pending = self.store.create_or_get_pending(
            profile=self.profile,
            session_id=session_id,
            session_lineage=session_lineage,
            tool_name=tool_name,
            effect_kind=policy.effect_kind,
            call_digest=digest,
            display=policy.display_projection(args),
            replay=policy.replay_projection(args),
        )
        return GateResult(GateDecision.PENDING, pending.id, digest)

    def execute(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        session_id: str,
        session_lineage: str,
        next_call: Callable[[dict[str, Any]], Any],
    ) -> Any:
        policy = self.policies.get(tool_name)
        if policy is None:
            return next_call(args)
        digest = self._digest(policy, args, session_lineage=session_lineage)
        approved = self.store.find_matching_approved(
            profile=self.profile,
            session_lineage=session_lineage,
            tool_name=tool_name,
            call_digest=digest,
        )
        if approved is None:
            pending = self.intercept(
                tool_name,
                args,
                session_id=session_id,
                session_lineage=session_lineage,
            )
            return {
                "ok": False,
                "status": "pending_approval",
                "request_id": pending.request_id,
            }
        if not self.store.claim(approved.id, expected_digest=digest):
            return {
                "ok": False,
                "status": "approval_already_claimed",
                "request_id": approved.id,
            }
        claim_token = activate_claim(
            ActiveClaim(
                request_id=approved.id,
                tool_name=tool_name,
                call_digest=digest,
                args_digest=call_digest(args),
            )
        )
        try:
            result = next_call(args)
        except EffectUncertainError as exc:
            safe_error = f"{type(exc).__name__}: effect outcome could not be verified"
            self.store.complete(
                approved.id,
                RequestState.UNCERTAIN,
                result={"error_type": type(exc).__name__},
                display={"error_type": type(exc).__name__},
            )
            return {
                "ok": False,
                "status": "uncertain",
                "request_id": approved.id,
                "error": safe_error,
            }
        except Exception as exc:
            safe_error = f"{type(exc).__name__}: effect failed"
            self.store.complete(
                approved.id,
                RequestState.FAILED,
                result={"error_type": type(exc).__name__},
                display={"error_type": type(exc).__name__},
            )
            return {
                "ok": False,
                "status": "failed",
                "request_id": approved.id,
                "error": safe_error[:1000],
            }
        finally:
            reset_claim(claim_token)
        self.store.complete(
            approved.id,
            RequestState.EXECUTED,
            result=result,
            display={"status": "executed"},
        )
        return result
