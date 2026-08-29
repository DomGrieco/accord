from __future__ import annotations

import hashlib
import json
from typing import Any


class CanonicalizationError(ValueError):
    """Raised when a value cannot be represented by canonical JSON."""


def canonical_json(value: Any) -> str:
    """Return deterministic strict JSON suitable for authority digests."""
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CanonicalizationError(f"value is not canonical JSON: {exc}") from exc


def call_digest(value: Any) -> str:
    """Return a full SHA-256 digest of canonical JSON bytes."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
