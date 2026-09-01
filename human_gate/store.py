from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from threading import RLock
from typing import Any, BinaryIO, TypeVar, cast

from .canonical import call_digest, canonical_json
from .models import Decision, RequestRecord, RequestState, ResumeState

_SCHEMA_VERSION = 1
_MAX_PROJECTION_BYTES = 256 * 1024


class ConflictError(RuntimeError):
    """Raised when a requested state transition is no longer valid."""


@dataclass(frozen=True)
class MockPublication:
    provider_id: str
    created: bool


_F = TypeVar("_F", bound=Callable[..., Any])


def _serialized(method: _F) -> _F:
    @wraps(method)
    def wrapper(self: GateStore, *args: Any, **kwargs: Any) -> Any:
        with self._connection_lock:
            return method(self, *args, **kwargs)

    return cast(_F, wrapper)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _bounded_json(value: dict[str, Any], *, field: str) -> str:
    encoded = canonical_json(value)
    if len(encoded.encode("utf-8")) > _MAX_PROJECTION_BYTES:
        raise ValueError(f"{field} projection exceeds {_MAX_PROJECTION_BYTES} bytes")
    return encoded


def _lock_runtime_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        import msvcrt

        windows_lock = cast(Any, msvcrt)
        try:
            windows_lock.locking(handle.fileno(), windows_lock.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError("Accord runtime is already active") from exc
        return

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise RuntimeError("Accord runtime is already active") from exc


def _unlock_runtime_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        import msvcrt

        windows_lock = cast(Any, msvcrt)
        windows_lock.locking(handle.fileno(), windows_lock.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class GateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self._runtime_id = uuid.uuid4().hex
        self._runtime_lock: BinaryIO | None = None
        self._connection_lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.path,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._initialize()

    @_serialized
    def acquire_runtime_lock(self) -> None:
        if self._runtime_lock is not None:
            return
        lock_path = self._runtime_lock_path(self._runtime_id)
        handle = lock_path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            _lock_runtime_file(handle)
        except Exception:
            handle.close()
            raise
        self._runtime_lock = handle

    def _runtime_lock_path(self, runtime_id: str) -> Path:
        return self.path.with_name(f"{self.path.name}.runtime.{runtime_id}.lock")

    def _runtime_owner_is_active(self, runtime_id: str) -> bool:
        if len(runtime_id) != 32 or any(
            character not in "0123456789abcdef" for character in runtime_id
        ):
            return False
        if runtime_id == self._runtime_id and self._runtime_lock is not None:
            return True
        lock_path = self._runtime_lock_path(runtime_id)
        if not lock_path.is_file():
            return False
        handle = lock_path.open("r+b")
        try:
            try:
                _lock_runtime_file(handle)
            except RuntimeError:
                return True
            _unlock_runtime_file(handle)
            return False
        finally:
            handle.close()

    @_serialized
    def close(self) -> None:
        try:
            self._connection.close()
        finally:
            if self._runtime_lock is not None:
                handle = self._runtime_lock
                self._runtime_lock = None
                try:
                    _unlock_runtime_file(handle)
                finally:
                    handle.close()

    @_serialized
    def _initialize(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                record_version INTEGER NOT NULL DEFAULT 1,
                profile TEXT NOT NULL,
                session_id TEXT NOT NULL,
                session_lineage TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                effect_kind TEXT NOT NULL,
                call_digest TEXT NOT NULL,
                display_json TEXT NOT NULL,
                replay_json TEXT NOT NULL,
                state TEXT NOT NULL,
                resume_state TEXT NOT NULL,
                resume_error TEXT,
                supersedes_request_id TEXT REFERENCES requests(id),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                claimed_at TEXT,
                claim_owner_id TEXT,
                completed_at TEXT
            );

            CREATE UNIQUE INDEX IF NOT EXISTS requests_one_pending_call
            ON requests(profile, session_lineage, tool_name, call_digest)
            WHERE state = 'pending';

            CREATE INDEX IF NOT EXISTS requests_matching_approval
            ON requests(profile, session_lineage, tool_name, call_digest, state, created_at);

            CREATE TABLE IF NOT EXISTS decisions (
                id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL REFERENCES requests(id),
                decision TEXT NOT NULL,
                actor_kind TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                comment TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS receipts (
                id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL REFERENCES requests(id),
                outcome TEXT NOT NULL,
                result_digest TEXT NOT NULL,
                display_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS decisions_by_request_and_created_at
            ON decisions(request_id, created_at);

            CREATE INDEX IF NOT EXISTS receipts_by_request_and_created_at
            ON receipts(request_id, created_at);

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS session_lineages (
                profile TEXT NOT NULL,
                session_id TEXT NOT NULL,
                lineage_root TEXT NOT NULL,
                request_id TEXT NOT NULL REFERENCES requests(id),
                created_at TEXT NOT NULL,
                PRIMARY KEY(profile, session_id)
            );

            CREATE TABLE IF NOT EXISTS mock_publications (
                idempotency_digest TEXT PRIMARY KEY,
                payload_digest TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        columns = {
            str(row["name"])
            for row in self._connection.execute("PRAGMA table_info(requests)").fetchall()
        }
        if "record_version" not in columns:
            self._connection.execute(
                "ALTER TABLE requests ADD COLUMN record_version INTEGER NOT NULL DEFAULT 1"
            )
        if "claim_owner_id" not in columns:
            self._connection.execute("ALTER TABLE requests ADD COLUMN claim_owner_id TEXT")
        if "resume_error" not in columns:
            self._connection.execute("ALTER TABLE requests ADD COLUMN resume_error TEXT")

    @_serialized
    def record_mock_publication(self, idempotency_key: str, payload_digest: str) -> MockPublication:
        """Record one local mock publish without persisting content or the opaque key."""
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 256:
            raise ValueError("idempotency_key must contain 1 to 256 characters")
        if (
            not isinstance(payload_digest, str)
            or len(payload_digest) != 64
            or any(character not in "0123456789abcdef" for character in payload_digest)
        ):
            raise ValueError("payload_digest must be a lowercase SHA-256 digest")
        idempotency_digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        provider_id = (
            "mock_"
            + hashlib.sha256(f"{idempotency_digest}:{payload_digest}".encode("ascii")).hexdigest()[
                :24
            ]
        )
        now = _now()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            existing = self._connection.execute(
                "SELECT payload_digest, provider_id FROM mock_publications "
                "WHERE idempotency_digest = ?",
                (idempotency_digest,),
            ).fetchone()
            if existing is not None:
                if not hmac.compare_digest(str(existing["payload_digest"]), payload_digest):
                    raise ConflictError(
                        "mock publication idempotency key was used for a different payload"
                    )
                self._connection.execute("COMMIT")
                return MockPublication(provider_id=str(existing["provider_id"]), created=False)
            self._connection.execute(
                """
                INSERT INTO mock_publications(
                    idempotency_digest, payload_digest, provider_id, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (idempotency_digest, payload_digest, provider_id, now),
            )
            self._connection.execute("COMMIT")
            return MockPublication(provider_id=provider_id, created=True)
        except Exception:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    @_serialized
    def count_mock_publications(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) AS count FROM mock_publications").fetchone()
        if row is None:
            raise RuntimeError("mock publication count was unavailable")
        return int(row["count"])

    @staticmethod
    def _owner_token_digest(token: str) -> str:
        if len(token) < 32:
            raise ValueError("owner token must contain at least 32 characters")
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @_serialized
    def register_owner_token(self, token: str) -> bool:
        digest = self._owner_token_digest(token)
        now = _now()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT value FROM settings WHERE key = 'owner_token_sha256'"
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)",
                    ("owner_token_sha256", digest, now),
                )
                self._connection.execute("COMMIT")
                return True
            self._connection.execute("COMMIT")
            return hmac.compare_digest(str(row["value"]), digest)
        except Exception:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    @_serialized
    def verify_owner_token(self, token: str) -> bool:
        try:
            digest = self._owner_token_digest(token)
        except ValueError:
            return False
        row = self._connection.execute(
            "SELECT value FROM settings WHERE key = 'owner_token_sha256'"
        ).fetchone()
        if row is None:
            return False
        return hmac.compare_digest(str(row["value"]), digest)

    @staticmethod
    def _record(row: sqlite3.Row | None) -> RequestRecord | None:
        if row is None:
            return None
        return RequestRecord(
            id=str(row["id"]),
            schema_version=int(row["schema_version"]),
            record_version=int(row["record_version"]),
            profile=str(row["profile"]),
            session_id=str(row["session_id"]),
            session_lineage=str(row["session_lineage"]),
            tool_name=str(row["tool_name"]),
            effect_kind=str(row["effect_kind"]),
            call_digest=str(row["call_digest"]),
            display_json=str(row["display_json"]),
            replay_json=str(row["replay_json"]),
            state=RequestState(str(row["state"])),
            resume_state=ResumeState(str(row["resume_state"])),
            resume_error=(str(row["resume_error"]) if row["resume_error"] is not None else None),
            supersedes_request_id=(
                str(row["supersedes_request_id"])
                if row["supersedes_request_id"] is not None
                else None
            ),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            claimed_at=str(row["claimed_at"]) if row["claimed_at"] is not None else None,
            completed_at=(str(row["completed_at"]) if row["completed_at"] is not None else None),
        )

    @_serialized
    def get_request(self, request_id: str) -> RequestRecord | None:
        row = self._connection.execute(
            "SELECT * FROM requests WHERE id = ?", (request_id,)
        ).fetchone()
        return self._record(row)

    @_serialized
    def list_requests(
        self,
        *,
        states: tuple[RequestState, ...] | None = None,
        limit: int = 100,
    ) -> list[RequestRecord]:
        bounded_limit = max(1, min(limit, 500))
        if states:
            state_csv = "," + ",".join(state.value for state in states) + ","
            rows = self._connection.execute(
                "SELECT * FROM requests WHERE instr(?, ',' || state || ',') > 0 "
                "ORDER BY created_at DESC LIMIT ?",
                (state_csv, bounded_limit),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM requests ORDER BY created_at DESC LIMIT ?", (bounded_limit,)
            ).fetchall()
        return [record for row in rows if (record := self._record(row)) is not None]

    @_serialized
    def create_or_get_pending(
        self,
        *,
        profile: str,
        session_id: str,
        session_lineage: str,
        tool_name: str,
        effect_kind: str,
        call_digest: str,
        display: dict[str, Any],
        replay: dict[str, Any],
        supersedes_request_id: str | None = None,
    ) -> RequestRecord:
        display_json = _bounded_json(display, field="display")
        replay_json = _bounded_json(replay, field="replay")
        request_id = uuid.uuid4().hex
        now = _now()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            existing = self._connection.execute(
                """
                SELECT * FROM requests
                WHERE profile = ? AND session_lineage = ? AND tool_name = ?
                  AND call_digest = ? AND state = 'pending'
                ORDER BY created_at DESC LIMIT 1
                """,
                (profile, session_lineage, tool_name, call_digest),
            ).fetchone()
            if existing is not None:
                self._connection.execute("COMMIT")
                record = self._record(existing)
                if record is None:
                    raise RuntimeError("failed to restore pending request")
                return record
            self._connection.execute(
                """
                INSERT INTO requests(
                    id, schema_version, record_version, profile, session_id, session_lineage,
                    tool_name, effect_kind, call_digest, display_json, replay_json,
                    state, resume_state, supersedes_request_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    _SCHEMA_VERSION,
                    1,
                    profile,
                    session_id,
                    session_lineage,
                    tool_name,
                    effect_kind,
                    call_digest,
                    display_json,
                    replay_json,
                    RequestState.PENDING.value,
                    ResumeState.NOT_REQUESTED.value,
                    supersedes_request_id,
                    now,
                    now,
                ),
            )
            self._connection.execute("COMMIT")
        except Exception:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        record = self.get_request(request_id)
        if record is None:
            raise RuntimeError("pending request was not persisted")
        return record

    @_serialized
    def find_matching_approved(
        self,
        *,
        profile: str,
        session_lineage: str,
        tool_name: str,
        call_digest: str,
    ) -> RequestRecord | None:
        row = self._connection.execute(
            """
            SELECT * FROM requests
            WHERE profile = ? AND session_lineage = ? AND tool_name = ?
              AND call_digest = ? AND state = 'approved'
            ORDER BY updated_at ASC LIMIT 1
            """,
            (profile, session_lineage, tool_name, call_digest),
        ).fetchone()
        return self._record(row)

    @_serialized
    def find_matching_claimed(
        self,
        *,
        profile: str,
        session_lineage: str,
        tool_name: str,
        call_digest: str,
    ) -> RequestRecord | None:
        row = self._connection.execute(
            """
            SELECT * FROM requests
            WHERE profile = ? AND session_lineage = ? AND tool_name = ?
              AND call_digest = ? AND state = 'claimed'
            ORDER BY updated_at ASC LIMIT 1
            """,
            (profile, session_lineage, tool_name, call_digest),
        ).fetchone()
        return self._record(row)

    @_serialized
    def decide(
        self,
        request_id: str,
        decision: Decision,
        *,
        actor_id: str,
        comment: str = "",
        actor_kind: str = "owner",
        expected_digest: str | None = None,
        expected_record_version: int | None = None,
    ) -> RequestRecord:
        normalized_comment = comment.strip()
        if decision is Decision.COMMENT and not normalized_comment:
            raise ValueError("comment text is required")
        target = {
            Decision.APPROVE: RequestState.APPROVED,
            Decision.DENY: RequestState.DENIED,
            Decision.COMMENT: RequestState.CHANGES_REQUESTED,
            Decision.CANCEL: RequestState.CANCELLED,
        }[decision]
        resume_state = ResumeState.PENDING
        now = _now()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            parameters: list[Any] = [
                target.value,
                resume_state.value,
                now,
                request_id,
                decision.value,
                decision.value,
                decision.value,
                expected_digest,
                expected_digest,
                expected_record_version,
                expected_record_version,
            ]
            updated = self._connection.execute(
                "UPDATE requests SET state = ?, resume_state = ?, updated_at = ?, "
                "record_version = record_version + 1 "
                "WHERE id = ? "
                "AND ((? = 'cancel' AND state IN ('pending', 'approved', 'changes_requested')) "
                "OR (? != 'cancel' AND state = 'pending')) "
                "AND NOT (? = 'cancel' AND resume_state = 'dispatching') "
                "AND (? IS NULL OR call_digest = ?) "
                "AND (? IS NULL OR record_version = ?)",
                parameters,
            )
            if updated.rowcount != 1:
                raise ConflictError("request changed or is not eligible for this decision")
            self._connection.execute(
                """
                INSERT INTO decisions(id, request_id, decision, actor_kind, actor_id, comment, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    request_id,
                    decision.value,
                    actor_kind,
                    actor_id,
                    normalized_comment,
                    now,
                ),
            )
            self._connection.execute("COMMIT")
        except Exception:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        record = self.get_request(request_id)
        if record is None:
            raise RuntimeError("decided request disappeared")
        return record

    @_serialized
    def mark_resume_delivered(
        self, request_id: str, *, expected_record_version: int
    ) -> RequestRecord:
        now = _now()
        updated = self._connection.execute(
            """
            UPDATE requests SET resume_state = ?, resume_error = NULL, updated_at = ?
            WHERE id = ? AND resume_state = ? AND record_version = ?
            """,
            (
                ResumeState.DELIVERED.value,
                now,
                request_id,
                ResumeState.DISPATCHING.value,
                expected_record_version,
            ),
        )
        if updated.rowcount != 1:
            raise ConflictError("request does not have the expected dispatching resume attempt")
        record = self.get_request(request_id)
        if record is None:
            raise RuntimeError("resumed request disappeared")
        return record

    @_serialized
    def mark_resume_failed(
        self, request_id: str, *, error: str, expected_record_version: int
    ) -> RequestRecord:
        normalized_error = error.strip()
        if not normalized_error:
            raise ValueError("resume error is required")
        if len(normalized_error) > 2000:
            normalized_error = normalized_error[:2000]
        now = _now()
        updated = self._connection.execute(
            """
            UPDATE requests SET resume_state = ?, resume_error = ?, updated_at = ?
            WHERE id = ? AND resume_state = ? AND record_version = ?
            """,
            (
                ResumeState.FAILED.value,
                normalized_error,
                now,
                request_id,
                ResumeState.DISPATCHING.value,
                expected_record_version,
            ),
        )
        if updated.rowcount != 1:
            raise ConflictError("request does not have the expected dispatching resume attempt")
        record = self.get_request(request_id)
        if record is None:
            raise RuntimeError("failed-resume request disappeared")
        return record

    @_serialized
    def requeue_verified_undelivered_resume(self, request_id: str, *, error: str) -> RequestRecord:
        """Repair a legacy false-positive delivery after external evidence proves no prompt landed."""
        normalized_error = error.strip()
        if not normalized_error:
            raise ValueError("resume error is required")
        now = _now()
        updated = self._connection.execute(
            """
            UPDATE requests
            SET resume_state = ?, resume_error = ?, updated_at = ?,
                record_version = record_version + 1
            WHERE id = ?
              AND state IN ('approved', 'changes_requested')
              AND resume_state = 'delivered'
            """,
            (ResumeState.FAILED.value, normalized_error[:2000], now, request_id),
        )
        if updated.rowcount != 1:
            raise ConflictError("request does not have a verified delivered resume to repair")
        record = self.get_request(request_id)
        if record is None:
            raise RuntimeError("requeued resume request disappeared")
        return record

    @_serialized
    def begin_resume_delivery(self, request_id: str) -> RequestRecord:
        now = _now()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            updated = self._connection.execute(
                """
                UPDATE requests
                SET resume_state = ?, resume_error = NULL, updated_at = ?,
                    record_version = record_version + 1
                WHERE id = ?
                  AND state IN ('approved', 'changes_requested', 'denied', 'cancelled')
                  AND resume_state IN ('pending', 'failed')
                """,
                (ResumeState.DISPATCHING.value, now, request_id),
            )
            if updated.rowcount != 1:
                raise ConflictError("request does not have a resumable decision")
            self._connection.execute("COMMIT")
        except Exception:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        record = self.get_request(request_id)
        if record is None:
            raise RuntimeError("resume delivery request disappeared")
        return record

    @_serialized
    def latest_decision(self, request_id: str) -> tuple[Decision, str] | None:
        row = self._connection.execute(
            """
            SELECT decision, comment FROM decisions
            WHERE request_id = ? ORDER BY created_at DESC LIMIT 1
            """,
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        return Decision(str(row["decision"])), str(row["comment"])

    @_serialized
    def register_session_continuation(
        self,
        request_id: str,
        session_id: str,
        *,
        expected_record_version: int,
    ) -> str:
        normalized = session_id.strip()
        if not normalized:
            raise ValueError("continuation session_id is required")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                """
                SELECT profile, session_lineage FROM requests
                WHERE id = ? AND resume_state = ? AND record_version = ?
                """,
                (
                    request_id,
                    ResumeState.DISPATCHING.value,
                    expected_record_version,
                ),
            ).fetchone()
            if row is None:
                exists = self._connection.execute(
                    "SELECT 1 FROM requests WHERE id = ?", (request_id,)
                ).fetchone()
                if exists is None:
                    raise KeyError(request_id)
                raise ConflictError("request does not have the expected dispatching resume attempt")
            profile = str(row["profile"])
            session_lineage = str(row["session_lineage"])
            existing = self._connection.execute(
                "SELECT lineage_root FROM session_lineages WHERE profile = ? AND session_id = ?",
                (profile, normalized),
            ).fetchone()
            if existing is not None and str(existing["lineage_root"]) != session_lineage:
                raise ConflictError("continuation session already belongs to another lineage")
            self._connection.execute(
                """
                INSERT OR IGNORE INTO session_lineages(
                    profile, session_id, lineage_root, request_id, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (profile, normalized, session_lineage, request_id, _now()),
            )
            self._connection.execute("COMMIT")
        except Exception:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        return session_lineage

    @_serialized
    def resolve_session_lineage(self, profile: str, session_id: str) -> str:
        row = self._connection.execute(
            "SELECT lineage_root FROM session_lineages WHERE profile = ? AND session_id = ?",
            (profile, session_id),
        ).fetchone()
        return str(row["lineage_root"]) if row is not None else session_id

    @_serialized
    def audit_history(self, request_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.audit_history_for_requests([request_id], limit=limit)[request_id]

    @_serialized
    def audit_history_for_requests(
        self, request_ids: list[str], *, limit: int = 100
    ) -> dict[str, list[dict[str, Any]]]:
        unique_ids = list(dict.fromkeys(request_ids))
        if len(unique_ids) > 500:
            raise ValueError("audit history supports at most 500 requests")
        histories: dict[str, list[dict[str, Any]]] = {request_id: [] for request_id in unique_ids}
        if not unique_ids:
            return histories
        bounded_limit = max(1, min(limit, 500))
        placeholders = ",".join("?" for _ in unique_ids)
        decision_rows = self._connection.execute(
            f"""
            SELECT id, request_id, decision, actor_kind, actor_id, comment, created_at
            FROM decisions WHERE request_id IN ({placeholders}) ORDER BY created_at ASC
            """,  # noqa: S608 - placeholders are generated, values remain parameterized
            unique_ids,
        ).fetchall()
        receipt_rows = self._connection.execute(
            f"""
            SELECT id, request_id, outcome, result_digest, created_at
            FROM receipts WHERE request_id IN ({placeholders}) ORDER BY created_at ASC
            """,  # noqa: S608 - placeholders are generated, values remain parameterized
            unique_ids,
        ).fetchall()
        for row in decision_rows:
            histories[str(row["request_id"])].append(
                {
                    "id": str(row["id"]),
                    "event_type": "decision",
                    "decision": str(row["decision"]),
                    "actor_kind": str(row["actor_kind"]),
                    "actor_id": str(row["actor_id"]),
                    "comment": str(row["comment"]),
                    "created_at": str(row["created_at"]),
                }
            )
        for row in receipt_rows:
            histories[str(row["request_id"])].append(
                {
                    "id": str(row["id"]),
                    "event_type": "receipt",
                    "outcome": str(row["outcome"]),
                    "result_digest": str(row["result_digest"]),
                    "created_at": str(row["created_at"]),
                }
            )
        for request_id, events in histories.items():
            events.sort(key=lambda event: (str(event["created_at"]), str(event["event_type"])))
            histories[request_id] = events[:bounded_limit]
        return histories

    @_serialized
    def claim(self, request_id: str, *, expected_digest: str) -> bool:
        self.acquire_runtime_lock()
        now = _now()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            updated = self._connection.execute(
                """
                UPDATE requests
                SET state = 'claimed', claimed_at = ?, claim_owner_id = ?, updated_at = ?
                WHERE id = ? AND state = 'approved' AND call_digest = ?
                """,
                (now, self._runtime_id, now, request_id, expected_digest),
            )
            self._connection.execute("COMMIT")
            return updated.rowcount == 1
        except Exception:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    @_serialized
    def complete(
        self,
        request_id: str,
        state: RequestState,
        *,
        result: Any = None,
        display: dict[str, Any] | None = None,
    ) -> RequestRecord:
        if state not in {RequestState.EXECUTED, RequestState.FAILED, RequestState.UNCERTAIN}:
            raise ValueError("completion state must be executed, failed, or uncertain")
        now = _now()
        result_digest = call_digest(result)
        display_json = _bounded_json(display or {}, field="receipt display")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            updated = self._connection.execute(
                """
                UPDATE requests
                SET state = ?, claim_owner_id = NULL, completed_at = ?, updated_at = ?
                WHERE id = ? AND state = 'claimed'
                """,
                (state.value, now, now, request_id),
            )
            if updated.rowcount != 1:
                raise ConflictError("request is not claimed")
            self._connection.execute(
                """
                INSERT INTO receipts(id, request_id, outcome, result_digest, display_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (uuid.uuid4().hex, request_id, state.value, result_digest, display_json, now),
            )
            self._connection.execute("COMMIT")
        except Exception:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        record = self.get_request(request_id)
        if record is None:
            raise RuntimeError("completed request disappeared")
        return record

    @_serialized
    def reconcile_executed_x_create_post_as_uncertain(
        self, request_id: str, *, expected_receipt_digest: str
    ) -> RequestRecord:
        """Repair one known false EXECUTED result without granting new execution authority."""
        if len(expected_receipt_digest) != 64 or any(
            character not in "0123456789abcdef" for character in expected_receipt_digest
        ):
            raise ValueError("expected_receipt_digest must be a lowercase SHA-256 digest")
        now = _now()
        reconciliation_result = call_digest({"reconciled": "registry_normalized_tool_error"})
        reconciliation_display = _bounded_json(
            {"error_type": "RegistryNormalizedToolError"}, field="receipt display"
        )
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            request = self._connection.execute(
                "SELECT tool_name, effect_kind, state FROM requests WHERE id = ?",
                (request_id,),
            ).fetchone()
            receipts = self._connection.execute(
                "SELECT id, outcome, result_digest, display_json FROM receipts WHERE request_id = ? "
                "ORDER BY created_at ASC, id ASC",
                (request_id,),
            ).fetchall()
            request_shape_valid = (
                request is not None
                and str(request["tool_name"]) == "x_create_post"
                and str(request["effect_kind"]) == "publish"
            )
            request_state = str(request["state"]) if request is not None else ""
            already_reconciled = (
                request_shape_valid
                and request_state == RequestState.UNCERTAIN.value
                and len(receipts) == 2
                and str(receipts[0]["outcome"]) == RequestState.EXECUTED.value
                and str(receipts[0]["result_digest"]) == expected_receipt_digest
                and str(receipts[1]["outcome"]) == RequestState.UNCERTAIN.value
                and str(receipts[1]["result_digest"]) == reconciliation_result
                and str(receipts[1]["display_json"]) == reconciliation_display
            )
            if already_reconciled:
                self._connection.execute("COMMIT")
                record = self.get_request(request_id)
                if record is None:
                    raise RuntimeError("reconciled request disappeared")
                return record
            valid = (
                request_shape_valid
                and request_state == RequestState.EXECUTED.value
                and len(receipts) == 1
                and str(receipts[0]["outcome"]) == RequestState.EXECUTED.value
                and str(receipts[0]["result_digest"]) == expected_receipt_digest
            )
            if not valid:
                raise ConflictError(
                    "request is not exactly one historical executed x_create_post request/receipt"
                )
            updated_request = self._connection.execute(
                """
                UPDATE requests
                SET state = 'uncertain', claim_owner_id = NULL, completed_at = ?, updated_at = ?,
                    record_version = record_version + 1
                WHERE id = ? AND state = 'executed' AND tool_name = 'x_create_post'
                  AND effect_kind = 'publish'
                """,
                (now, now, request_id),
            )
            updated_receipt = self._connection.execute(
                """
                INSERT INTO receipts(id, request_id, outcome, result_digest, display_json, created_at)
                VALUES (?, ?, 'uncertain', ?, ?, ?)
                """,
                (uuid.uuid4().hex, request_id, reconciliation_result, reconciliation_display, now),
            )
            if updated_request.rowcount != 1 or updated_receipt.rowcount != 1:
                raise ConflictError(
                    "request is not exactly one historical executed x_create_post request/receipt"
                )
            self._connection.execute("COMMIT")
        except Exception:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        record = self.get_request(request_id)
        if record is None:
            raise RuntimeError("reconciled request disappeared")
        return record

    @_serialized
    def set_resume_state(self, request_id: str, state: ResumeState) -> RequestRecord:
        now = _now()
        updated = self._connection.execute(
            "UPDATE requests SET resume_state = ?, updated_at = ? WHERE id = ?",
            (state.value, now, request_id),
        )
        if updated.rowcount != 1:
            raise KeyError(request_id)
        record = self.get_request(request_id)
        if record is None:
            raise RuntimeError("request disappeared after resume update")
        return record

    @_serialized
    def recover_claimed_as_uncertain(self) -> int:
        self.acquire_runtime_lock()
        now = _now()
        candidates = self._connection.execute(
            "SELECT id, claim_owner_id FROM requests WHERE state = 'claimed'"
        ).fetchall()
        stale = [
            (str(row["id"]), str(row["claim_owner_id"] or ""))
            for row in candidates
            if not self._runtime_owner_is_active(str(row["claim_owner_id"] or ""))
        ]
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            recovered = 0
            for request_id, owner_id in stale:
                updated = self._connection.execute(
                    """
                    UPDATE requests
                    SET state = 'uncertain', claim_owner_id = NULL,
                        completed_at = ?, updated_at = ?
                    WHERE id = ? AND state = 'claimed'
                      AND COALESCE(claim_owner_id, '') = ?
                    """,
                    (now, now, request_id, owner_id),
                )
                if updated.rowcount != 1:
                    continue
                self._connection.execute(
                    """
                    INSERT INTO receipts(id, request_id, outcome, result_digest, display_json, created_at)
                    VALUES (?, ?, 'uncertain', ?, '{}', ?)
                    """,
                    (uuid.uuid4().hex, request_id, call_digest({"recovered": True}), now),
                )
                recovered += 1
            self._connection.execute("COMMIT")
            return recovered
        except Exception:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
