from __future__ import annotations

from typing import Any


def _schema(
    name: str,
    description: str,
    properties: dict[str, Any],
    *,
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
    }


LIST = _schema(
    "human_gate_list",
    "List durable Human Gate requests. This is read-only and never approves or executes an effect.",
    {
        "state": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    },
)

GET = _schema(
    "human_gate_get",
    "Read one durable Human Gate request by id. This is read-only.",
    {"request_id": {"type": "string", "minLength": 1, "maxLength": 128}},
    required=["request_id"],
)

DEMO_EFFECT = _schema(
    "human_gate_demo_effect",
    "Run a deterministic local demo effect. Human Gate always blocks the first call and requires one exact durable owner approval before replay.",
    {"message": {"type": "string", "minLength": 1, "maxLength": 1000}},
    required=["message"],
)

MOCK_PUBLISH = _schema(
    "human_gate_mock_publish",
    "Run a local idempotent publication fixture. It never contacts a provider. Human Gate blocks the first call until the owner approves the exact payload.",
    {
        "destination": {"type": "string", "enum": ["mock"]},
        "text": {"type": "string", "minLength": 1, "maxLength": 20000},
        "media_sha256": {
            "type": "array",
            "items": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "maxItems": 4,
        },
        "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 256},
        "simulate_outcome": {
            "type": "string",
            "enum": ["success", "failed", "uncertain"],
            "default": "success",
        },
    },
    required=["destination", "text", "media_sha256", "idempotency_key"],
)
