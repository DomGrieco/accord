from __future__ import annotations

import json
from typing import Any

from .claims import require_active_claim


class EffectUncertainError(RuntimeError):
    """Raised when an external effect may have occurred but cannot be verified."""


def demo_effect_handler(args: dict[str, Any], **_: Any) -> str:
    """A deterministic owned effect used to prove the gate contract."""
    claim = require_active_claim("human_gate_demo_effect", args)
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
