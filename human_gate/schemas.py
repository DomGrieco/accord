from __future__ import annotations

from typing import Any

from .identity import (
    DEMO_EFFECT_TOOL,
    GET_TOOL,
    LEGACY_DEMO_EFFECT_TOOL,
    LEGACY_GET_TOOL,
    LEGACY_LIST_TOOL,
    LEGACY_MOCK_PUBLISH_TOOL,
    LIST_TOOL,
    MOCK_PUBLISH_TOOL,
    PLUGIN_DISPLAY_NAME,
)


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


def _named(schema: dict[str, Any], name: str) -> dict[str, Any]:
    return {**schema, "name": name}


LIST = _schema(
    LIST_TOOL,
    f"List durable {PLUGIN_DISPLAY_NAME} requests. This is read-only and never approves or executes an effect.",
    {
        "state": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    },
)
LEGACY_LIST = _named(LIST, LEGACY_LIST_TOOL)

GET = _schema(
    GET_TOOL,
    f"Read one durable {PLUGIN_DISPLAY_NAME} request by id. This is read-only.",
    {"request_id": {"type": "string", "minLength": 1, "maxLength": 128}},
    required=["request_id"],
)
LEGACY_GET = _named(GET, LEGACY_GET_TOOL)

DEMO_EFFECT = _schema(
    DEMO_EFFECT_TOOL,
    f"Run a deterministic local demo effect. {PLUGIN_DISPLAY_NAME} always blocks the first call and requires one exact durable owner approval before replay.",
    {"message": {"type": "string", "minLength": 1, "maxLength": 1000}},
    required=["message"],
)
LEGACY_DEMO_EFFECT = _named(DEMO_EFFECT, LEGACY_DEMO_EFFECT_TOOL)

MOCK_PUBLISH = _schema(
    MOCK_PUBLISH_TOOL,
    f"Run a local idempotent publication fixture. It never contacts a provider. {PLUGIN_DISPLAY_NAME} blocks the first call until the owner approves the exact payload.",
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
LEGACY_MOCK_PUBLISH = _named(MOCK_PUBLISH, LEGACY_MOCK_PUBLISH_TOOL)

X_CREATE_POST = _schema(
    "x_create_post",
    f"Create one post or quote post on X. {PLUGIN_DISPLAY_NAME} requires exact owner approval before this owned effect can contact X.",
    {
        "account": {"type": "string", "minLength": 1, "maxLength": 64},
        "text": {"type": "string", "minLength": 1, "maxLength": 280},
        "quote_post_id": {"type": "string", "pattern": "^[0-9]{1,32}$"},
    },
    required=["account", "text"],
)
