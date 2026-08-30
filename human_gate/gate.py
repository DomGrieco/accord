from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from inspect import isawaitable
from typing import Any

from .canonical import call_digest
from .claims import ActiveClaim, activate_claim, reset_claim
from .errors import EffectDefinitiveFailureError
from .models import RequestState
from .policy import PolicyRegistry, ToolPolicy
from .store import GateStore

_MAX_REGISTRY_ERROR_ENVELOPE_CHARS = 64 * 1024
_REGISTRY_ERROR_PREFIXES = (
    "Tool execution failed: ",
    "[TOOL_ERROR] Tool execution failed: ",
)


def _is_registry_normalized_error(result: Any) -> bool:
    """Recognize the safe outer shape without interpreting provider error text."""
    if not isinstance(result, str) or len(result) > _MAX_REGISTRY_ERROR_ENVELOPE_CHARS:
        return False
    try:
        envelope = json.loads(result)
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        return False
    return (
        isinstance(envelope, dict)
        and set(envelope) == {"error"}
        and isinstance(envelope["error"], str)
        and envelope["error"].startswith(_REGISTRY_ERROR_PREFIXES)
    )


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
            "policy_fingerprint": policy.fingerprint(),
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
        session_lineage = self.store.resolve_session_lineage(self.profile, session_lineage)
        policy = self.policies.match(tool_name, args)
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
            display=policy.project_display(args),
            replay=policy.project_replay(args),
        )
        return GateResult(GateDecision.PENDING, pending.id, digest)

    def has_matching_claimed(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        session_lineage: str,
    ) -> bool:
        """Return whether middleware already claimed this exact call."""
        session_lineage = self.store.resolve_session_lineage(self.profile, session_lineage)
        policy = self.policies.match(tool_name, args)
        if policy is None:
            return False
        digest = self._digest(policy, args, session_lineage=session_lineage)
        return (
            self.store.find_matching_claimed(
                profile=self.profile,
                session_lineage=session_lineage,
                tool_name=tool_name,
                call_digest=digest,
            )
            is not None
        )

    def execute(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        session_id: str,
        session_lineage: str,
        next_call: Callable[[dict[str, Any]], Any],
    ) -> Any:
        session_lineage = self.store.resolve_session_lineage(self.profile, session_lineage)
        policy = self.policies.match(tool_name, args)
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
        active_claim = ActiveClaim(
            request_id=approved.id,
            tool_name=tool_name,
            call_digest=digest,
            args_digest=call_digest(args),
        )
        claim_token = activate_claim(active_claim)

        def finish_failure(exc: Exception) -> dict[str, Any]:
            if isinstance(exc, EffectDefinitiveFailureError):
                status = RequestState.FAILED
                outcome = "failed"
                message = f"{type(exc).__name__}: effect failed before dispatch"
            else:
                status = RequestState.UNCERTAIN
                outcome = "uncertain"
                message = f"{type(exc).__name__}: effect outcome could not be verified"
            self.store.complete(
                approved.id,
                status,
                result={"error_type": type(exc).__name__},
                display={"error_type": type(exc).__name__},
            )
            return {
                "ok": False,
                "status": outcome,
                "request_id": approved.id,
                "error": message[:1000],
            }

        def finish_success(result: Any) -> Any:
            if policy.owned_effect and not active_claim.was_consumed():
                self.store.complete(
                    approved.id,
                    RequestState.FAILED,
                    result={"error_type": "OwnedEffectClaimNotConsumed"},
                    display={"error_type": "OwnedEffectClaimNotConsumed"},
                )
                return {
                    "ok": False,
                    "status": "owned_effect_claim_not_consumed",
                    "request_id": approved.id,
                    "error": "owned effect returned without consuming its approval claim",
                }
            if _is_registry_normalized_error(result):
                self.store.complete(
                    approved.id,
                    RequestState.UNCERTAIN,
                    result={"error_type": "RegistryNormalizedToolError"},
                    display={"error_type": "RegistryNormalizedToolError"},
                )
                return {
                    "ok": False,
                    "status": "uncertain",
                    "request_id": approved.id,
                    "error": "RegistryNormalizedToolError: effect outcome could not be verified",
                }
            self.store.complete(
                approved.id,
                RequestState.EXECUTED,
                result=result,
                display={"status": "executed"},
            )
            return result

        try:
            result = next_call(args)
        except Exception as exc:
            active_claim.revoke()
            reset_claim(claim_token)
            return finish_failure(exc)

        if isawaitable(result):
            reset_claim(claim_token)

            async def await_effect() -> Any:
                async_token = activate_claim(active_claim)
                try:
                    awaited = await result
                except Exception as exc:
                    return finish_failure(exc)
                finally:
                    active_claim.revoke()
                    reset_claim(async_token)
                return finish_success(awaited)

            return await_effect()

        try:
            return finish_success(result)
        finally:
            active_claim.revoke()
            reset_claim(claim_token)
