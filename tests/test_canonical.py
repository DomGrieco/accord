from __future__ import annotations

import math

import pytest

from human_gate.canonical import CanonicalizationError, call_digest, canonical_json


def test_canonical_json_is_stable_across_mapping_order() -> None:
    left = {"text": "hello", "meta": {"b": 2, "a": 1}}
    right = {"meta": {"a": 1, "b": 2}, "text": "hello"}

    assert canonical_json(left) == canonical_json(right)
    assert call_digest(left) == call_digest(right)


def test_call_digest_changes_when_any_argument_changes() -> None:
    base = {
        "schema": "hermes.human-gate.call.v1",
        "profile": "life",
        "session_lineage": "session-1",
        "tool_name": "x_create_post",
        "effect_kind": "publish",
        "arguments": {"text": "hello", "reply_to": None},
    }
    changed = {**base, "arguments": {"text": "hello!", "reply_to": None}}

    assert call_digest(base) != call_digest(changed)


def test_canonical_json_rejects_non_finite_numbers() -> None:
    with pytest.raises(CanonicalizationError):
        canonical_json({"value": math.nan})
