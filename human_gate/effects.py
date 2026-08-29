from __future__ import annotations

import json
import re
from typing import Any

from .canonical import call_digest
from .claims import consume_active_claim
from .store import GateStore

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class EffectUncertainError(RuntimeError):
    """Raised when an external effect may have occurred but cannot be verified."""


def demo_effect_handler(args: dict[str, Any], **_: Any) -> str:
    """A deterministic owned effect used to prove the gate contract."""
    claim = consume_active_claim("human_gate_demo_effect", args)
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
    claim = consume_active_claim("human_gate_mock_publish", args)
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
        raise RuntimeError("mock publisher rejected the fixture before dispatch")
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
