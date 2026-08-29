from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, cast

from .canonical import call_digest, canonical_json
from .models import Decision, RequestRecord, RequestState, ResumeState

_SCHEMA_VERSION = 1
_MAX_PROJECTION_BYTES = 256 * 1024


class ConflictError(RuntimeError):
    """Raised when a requested state transition is no longer valid."""


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
            raise RuntimeError("Human Gate runtime is already active") from exc
        return

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise RuntimeError("Human Gate runtime is already active") from exc


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
        self._runtime_lock: BinaryIO | None = None
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

    def acquire_runtime_lock(self) -> None:
        if self._runtime_lock is not None:
            return
        lock_path = self.path.with_name(f"{self.path.name}.runtime.lock")
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
                supersedes_request_id TEXT REFERENCES requests(id),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                claimed_at TEXT,
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

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
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

    @staticmethod
    def _owner_token_digest(token: str) -> str:
        if len(token) < 32:
            raise ValueError("owner token must contain at least 32 characters")
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

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
            supersedes_request_id=(
                str(row["supersedes_request_id"])
                if row["supersedes_request_id"] is not None
                else None
            ),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            claimed_at=str(row["claimed_at"]) if row["claimed_at"] is not None else None,
            completed_at=(
                str(row["completed_at"]) if row["completed_at"] is not None else None
            ),
        )

    def get_request(self, request_id: str) -> RequestRecord | None:
        row = self._connection.execute(
            "SELECT * FROM requests WHERE id = ?", (request_id,)
        ).fetchone()
        return self._record(row)

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
        resume_state = (
            ResumeState.PENDING
            if decision in {Decision.APPROVE, Decision.COMMENT}
            else ResumeState.NOT_REQUESTED
        )
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

    def mark_resume_delivered(self, request_id: str) -> RequestRecord:
        now = _now()
        updated = self._connection.execute(
            """
            UPDATE requests SET resume_state = ?, updated_at = ?
            WHERE id = ? AND resume_state = ?
            """,
            (
                ResumeState.DELIVERED.value,
                now,
                request_id,
                ResumeState.DISPATCHING.value,
            ),
        )
        if updated.rowcount != 1:
            raise ConflictError("request does not have a dispatching resume")
        record = self.get_request(request_id)
        if record is None:
            raise RuntimeError("resumed request disappeared")
        return record

    def mark_resume_failed(self, request_id: str) -> RequestRecord:
        now = _now()
        updated = self._connection.execute(
            """
            UPDATE requests SET resume_state = ?, updated_at = ?
            WHERE id = ? AND resume_state = ?
            """,
            (
                ResumeState.FAILED.value,
                now,
                request_id,
                ResumeState.DISPATCHING.value,
            ),
        )
        if updated.rowcount != 1:
            raise ConflictError("request does not have a dispatching resume")
        record = self.get_request(request_id)
        if record is None:
            raise RuntimeError("failed-resume request disappeared")
        return record

    def begin_resume_delivery(self, request_id: str) -> RequestRecord:
        now = _now()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            updated = self._connection.execute(
                """
                UPDATE requests
                SET resume_state = ?, updated_at = ?, record_version = record_version + 1
                WHERE id = ?
                  AND state IN ('approved', 'changes_requested')
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

    def claim(self, request_id: str, *, expected_digest: str) -> bool:
        now = _now()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            updated = self._connection.execute(
                """
                UPDATE requests
                SET state = 'claimed', claimed_at = ?, updated_at = ?
                WHERE id = ? AND state = 'approved' AND call_digest = ?
                """,
                (now, now, request_id, expected_digest),
            )
            self._connection.execute("COMMIT")
            return updated.rowcount == 1
        except Exception:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

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
                SET state = ?, completed_at = ?, updated_at = ?
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

    def recover_claimed_as_uncertain(self) -> int:
        now = _now()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            rows = self._connection.execute(
                "SELECT id FROM requests WHERE state = 'claimed'"
            ).fetchall()
            for row in rows:
                request_id = str(row["id"])
                self._connection.execute(
                    """
                    UPDATE requests SET state = 'uncertain', completed_at = ?, updated_at = ?
                    WHERE id = ? AND state = 'claimed'
                    """,
                    (now, now, request_id),
                )
                self._connection.execute(
                    """
                    INSERT INTO receipts(id, request_id, outcome, result_digest, display_json, created_at)
                    VALUES (?, ?, 'uncertain', ?, '{}', ?)
                    """,
                    (uuid.uuid4().hex, request_id, call_digest({"recovered": True}), now),
                )
            self._connection.execute("COMMIT")
            return len(rows)
        except Exception:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
