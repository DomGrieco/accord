from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any


class RequestState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    DENIED = "denied"
    CANCELLED = "cancelled"
    CLAIMED = "claimed"
    EXECUTED = "executed"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class ResumeState(str, Enum):
    NOT_REQUESTED = "not_requested"
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


class Decision(str, Enum):
    APPROVE = "approve"
    DENY = "deny"
    COMMENT = "comment"
    CANCEL = "cancel"


@dataclass(frozen=True)
class RequestRecord:
    id: str
    schema_version: int
    record_version: int
    profile: str
    session_id: str
    session_lineage: str
    tool_name: str
    effect_kind: str
    call_digest: str
    display_json: str
    replay_json: str
    state: RequestState
    resume_state: ResumeState
    supersedes_request_id: str | None
    created_at: str
    updated_at: str
    claimed_at: str | None
    completed_at: str | None

    @property
    def display(self) -> dict[str, Any]:
        value = json.loads(self.display_json)
        return value if isinstance(value, dict) else {}

    @property
    def replay(self) -> dict[str, Any]:
        value = json.loads(self.replay_json)
        return value if isinstance(value, dict) else {}
