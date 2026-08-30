from __future__ import annotations

from pathlib import Path
from threading import Barrier, Event, Lock, Thread

import pytest

from human_gate.models import Decision, RequestState, ResumeState
from human_gate.store import ConflictError, GateStore


def create_request(store: GateStore, *, digest: str = "digest-a"):
    return store.create_or_get_pending(
        profile="life",
        session_id="session-runtime",
        session_lineage="session-stored",
        tool_name="x_create_post",
        effect_kind="publish",
        call_digest=digest,
        display={"text": "hello"},
        replay={"text": "hello"},
    )


def test_runtime_leases_allow_concurrent_hermes_processes(tmp_path: Path) -> None:
    path = tmp_path / "gate.db"
    first = GateStore(path)
    second = GateStore(path)

    first.acquire_runtime_lock()
    second.acquire_runtime_lock()

    first.close()
    second.close()


def test_mock_publication_idempotency_persists_only_digests(tmp_path: Path) -> None:
    path = tmp_path / "gate.db"
    store = GateStore(path)
    key = "private-idempotency-key"
    payload_digest = "a" * 64

    first = store.record_mock_publication(key, payload_digest)
    replay = store.record_mock_publication(key, payload_digest)

    assert first.created is True
    assert replay.created is False
    assert replay.provider_id == first.provider_id
    raw = path.read_bytes()
    assert key.encode() not in raw


def test_mock_publication_rejects_idempotency_key_reuse_for_changed_payload(
    tmp_path: Path,
) -> None:
    store = GateStore(tmp_path / "gate.db")
    store.record_mock_publication("same-key", "a" * 64)

    with pytest.raises(ConflictError, match="different payload"):
        store.record_mock_publication("same-key", "b" * 64)


def test_mock_publication_idempotency_is_atomic_under_concurrency(tmp_path: Path) -> None:
    path = tmp_path / "gate.db"
    GateStore(path).close()
    barrier = Barrier(3)
    lock = Lock()
    outcomes: list[tuple[bool, str]] = []

    def publish() -> None:
        store = GateStore(path)
        barrier.wait()
        result = store.record_mock_publication("same-key", "a" * 64)
        with lock:
            outcomes.append((result.created, result.provider_id))
        store.close()

    workers = [Thread(target=publish), Thread(target=publish)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join()

    assert sorted(created for created, _provider_id in outcomes) == [False, True]
    assert len({provider_id for _created, provider_id in outcomes}) == 1
    store = GateStore(path)
    assert store.count_mock_publications() == 1


def test_same_store_serializes_connection_access(tmp_path: Path) -> None:
    store = GateStore(tmp_path / "gate.db")
    entered = Event()
    finished = Event()

    def publish() -> None:
        entered.set()
        store.record_mock_publication("same-key", "a" * 64)
        finished.set()

    with store._connection_lock:
        worker = Thread(target=publish)
        worker.start()
        assert entered.wait(timeout=1)
        assert finished.wait(timeout=0.05) is False
    worker.join(timeout=1)

    assert finished.is_set()
    assert store.count_mock_publications() == 1


def test_audit_history_orders_safe_decisions_and_receipts(tmp_path: Path) -> None:
    store = GateStore(tmp_path / "gate.db")
    request = create_request(store)
    store.decide(
        request.id,
        Decision.APPROVE,
        actor_id="desktop-owner",
        comment="Approved after review.",
    )
    assert store.claim(request.id, expected_digest="digest-a") is True
    store.complete(
        request.id,
        RequestState.EXECUTED,
        result={"provider_id": "private-result"},
        display={"status": "published"},
    )

    events = store.audit_history(request.id)
    batched = store.audit_history_for_requests([request.id, "missing"])

    assert batched == {request.id: events, "missing": []}
    assert events == [
        {
            "id": events[0]["id"],
            "event_type": "decision",
            "decision": "approve",
            "actor_kind": "owner",
            "actor_id": "desktop-owner",
            "comment": "Approved after review.",
            "created_at": events[0]["created_at"],
        },
        {
            "id": events[1]["id"],
            "event_type": "receipt",
            "outcome": "executed",
            "result_digest": events[1]["result_digest"],
            "created_at": events[1]["created_at"],
        },
    ]
    assert "private-result" not in str(events)


def test_pending_request_persists_across_store_reopen(tmp_path: Path) -> None:
    path = tmp_path / "gate.db"
    first = GateStore(path)
    request = create_request(first)
    first.close()

    second = GateStore(path)
    restored = second.get_request(request.id)

    assert restored is not None
    assert restored.state is RequestState.PENDING
    assert restored.display == {"text": "hello"}
    assert restored.call_digest == "digest-a"


def test_equivalent_pending_request_is_reused(tmp_path: Path) -> None:
    store = GateStore(tmp_path / "gate.db")

    first = create_request(store)
    second = create_request(store)

    assert second.id == first.id


def test_approve_can_be_claimed_exactly_once_under_concurrency(tmp_path: Path) -> None:
    path = tmp_path / "gate.db"
    setup = GateStore(path)
    request = create_request(setup)
    setup.decide(request.id, Decision.APPROVE, actor_id="owner")
    setup.close()

    barrier = Barrier(3)
    lock = Lock()
    outcomes: list[bool] = []

    def claim() -> None:
        store = GateStore(path)
        barrier.wait()
        outcome = store.claim(request.id, expected_digest="digest-a")
        with lock:
            outcomes.append(outcome)
        store.close()

    workers = [Thread(target=claim), Thread(target=claim)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join()

    assert sorted(outcomes) == [False, True]
    final = GateStore(path).get_request(request.id)
    assert final is not None
    assert final.state is RequestState.CLAIMED


def test_claim_rejects_digest_mismatch(tmp_path: Path) -> None:
    store = GateStore(tmp_path / "gate.db")
    request = create_request(store)
    store.decide(request.id, Decision.APPROVE, actor_id="owner")

    assert store.claim(request.id, expected_digest="different") is False
    assert store.get_request(request.id).state is RequestState.APPROVED


def test_approved_request_can_be_cancelled_before_claim(tmp_path: Path) -> None:
    store = GateStore(tmp_path / "gate.db")
    request = create_request(store)
    approved = store.decide(request.id, Decision.APPROVE, actor_id="owner")

    cancelled = store.decide(
        request.id,
        Decision.CANCEL,
        actor_id="owner",
        comment="Approval withdrawn.",
        expected_digest=approved.call_digest,
        expected_record_version=approved.record_version,
    )

    assert cancelled.state is RequestState.CANCELLED
    assert (
        store.find_matching_approved(
            profile="life",
            session_lineage="session-stored",
            tool_name="x_create_post",
            call_digest="digest-a",
        )
        is None
    )
    assert store.claim(request.id, expected_digest="digest-a") is False


def test_changes_requested_request_can_be_cancelled(tmp_path: Path) -> None:
    store = GateStore(tmp_path / "gate.db")
    request = create_request(store)
    changes = store.decide(
        request.id,
        Decision.COMMENT,
        actor_id="owner",
        comment="Revise this.",
    )

    cancelled = store.decide(
        request.id,
        Decision.CANCEL,
        actor_id="owner",
        expected_digest=changes.call_digest,
        expected_record_version=changes.record_version,
    )

    assert cancelled.state is RequestState.CANCELLED


def test_cancel_and_claim_race_has_exactly_one_winner(tmp_path: Path) -> None:
    path = tmp_path / "gate.db"
    setup = GateStore(path)
    request = create_request(setup)
    approved = setup.decide(request.id, Decision.APPROVE, actor_id="owner")
    setup.close()

    barrier = Barrier(3)
    lock = Lock()
    outcomes: list[str] = []

    def claim() -> None:
        store = GateStore(path)
        barrier.wait()
        claimed = store.claim(request.id, expected_digest="digest-a")
        with lock:
            outcomes.append("claimed" if claimed else "claim-rejected")
        store.close()

    def cancel() -> None:
        store = GateStore(path)
        barrier.wait()
        try:
            store.decide(
                request.id,
                Decision.CANCEL,
                actor_id="owner",
                expected_digest=approved.call_digest,
                expected_record_version=approved.record_version,
            )
        except ConflictError:
            outcome = "cancel-rejected"
        else:
            outcome = "cancelled"
        with lock:
            outcomes.append(outcome)
        store.close()

    workers = [Thread(target=claim), Thread(target=cancel)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join()

    final = GateStore(path).get_request(request.id)
    assert final is not None
    if final.state is RequestState.CLAIMED:
        assert sorted(outcomes) == ["cancel-rejected", "claimed"]
    else:
        assert final.state is RequestState.CANCELLED
        assert sorted(outcomes) == ["cancelled", "claim-rejected"]


@pytest.mark.parametrize(
    "terminal_state",
    [RequestState.CLAIMED, RequestState.EXECUTED, RequestState.FAILED, RequestState.UNCERTAIN],
)
def test_cancel_rejects_claimed_and_terminal_requests(
    tmp_path: Path, terminal_state: RequestState
) -> None:
    store = GateStore(tmp_path / "gate.db")
    request = create_request(store)
    approved = store.decide(request.id, Decision.APPROVE, actor_id="owner")
    assert store.claim(request.id, expected_digest="digest-a") is True
    if terminal_state is not RequestState.CLAIMED:
        store.complete(
            request.id,
            terminal_state,
            result={"ok": terminal_state is RequestState.EXECUTED},
        )

    with pytest.raises(ConflictError):
        store.decide(
            request.id,
            Decision.CANCEL,
            actor_id="owner",
            expected_digest=approved.call_digest,
            expected_record_version=approved.record_version,
        )


def test_comment_and_deny_never_create_execution_authority(tmp_path: Path) -> None:
    store = GateStore(tmp_path / "gate.db")
    commented = create_request(store, digest="commented")
    denied = create_request(store, digest="denied")

    store.decide(
        commented.id,
        Decision.COMMENT,
        actor_id="owner",
        comment="Make the opening more direct.",
    )
    store.decide(denied.id, Decision.DENY, actor_id="owner", comment="Do not post this.")

    assert store.claim(commented.id, expected_digest="commented") is False
    assert store.claim(denied.id, expected_digest="denied") is False
    assert store.get_request(commented.id).state is RequestState.CHANGES_REQUESTED
    assert store.get_request(denied.id).state is RequestState.DENIED


def test_comment_requires_text(tmp_path: Path) -> None:
    store = GateStore(tmp_path / "gate.db")
    request = create_request(store)

    with pytest.raises(ValueError, match="comment"):
        store.decide(request.id, Decision.COMMENT, actor_id="owner", comment="  ")


def test_stale_resume_attempt_cannot_ack_or_fail_a_new_attempt(tmp_path: Path) -> None:
    store = GateStore(tmp_path / "gate.db")
    request = create_request(store)
    store.decide(request.id, Decision.APPROVE, actor_id="owner")
    first = store.begin_resume_delivery(request.id)
    store.mark_resume_failed(
        request.id,
        error="session limit",
        expected_record_version=first.record_version,
    )
    second = store.begin_resume_delivery(request.id)

    with pytest.raises(ConflictError, match="dispatching resume attempt"):
        store.mark_resume_delivered(
            request.id,
            expected_record_version=first.record_version,
        )
    with pytest.raises(ConflictError, match="dispatching resume attempt"):
        store.mark_resume_failed(
            request.id,
            error="stale failure",
            expected_record_version=first.record_version,
        )
    with pytest.raises(ConflictError, match="dispatching resume attempt"):
        store.register_session_continuation(
            request.id,
            "stale-continuation",
            expected_record_version=first.record_version,
        )

    assert (
        store.register_session_continuation(
            request.id,
            "current-continuation",
            expected_record_version=second.record_version,
        )
        == "session-stored"
    )
    delivered = store.mark_resume_delivered(
        request.id,
        expected_record_version=second.record_version,
    )
    assert delivered.resume_state is ResumeState.DELIVERED


def test_second_decision_is_rejected(tmp_path: Path) -> None:
    store = GateStore(tmp_path / "gate.db")
    request = create_request(store)
    store.decide(request.id, Decision.DENY, actor_id="owner")

    with pytest.raises(ConflictError):
        store.decide(request.id, Decision.APPROVE, actor_id="owner")


def test_stale_claim_recovery_is_uncertain(tmp_path: Path) -> None:
    path = tmp_path / "gate.db"
    store = GateStore(path)
    request = create_request(store)
    store.decide(request.id, Decision.APPROVE, actor_id="owner")
    assert store.claim(request.id, expected_digest="digest-a") is True
    store.close()

    restarted = GateStore(path)
    restarted.acquire_runtime_lock()
    recovered = restarted.recover_claimed_as_uncertain()

    assert recovered == 1
    recovered_request = restarted.get_request(request.id)
    assert recovered_request is not None
    assert recovered_request.state is RequestState.UNCERTAIN


def test_live_claim_owner_is_not_recovered_by_another_runtime(tmp_path: Path) -> None:
    path = tmp_path / "gate.db"
    owner = GateStore(path)
    request = create_request(owner)
    owner.decide(request.id, Decision.APPROVE, actor_id="owner")
    assert owner.claim(request.id, expected_digest="digest-a") is True

    observer = GateStore(path)
    observer.acquire_runtime_lock()

    assert observer.recover_claimed_as_uncertain() == 0
    observed_request = observer.get_request(request.id)
    assert observed_request is not None
    assert observed_request.state is RequestState.CLAIMED


def test_verified_false_delivered_resume_can_be_requeued_with_owner_visible_error(
    tmp_path: Path,
) -> None:
    store = GateStore(tmp_path / "gate.db")
    request = create_request(store)
    decided = store.decide(
        request.id,
        Decision.COMMENT,
        actor_id="owner",
        comment="Change only the ending.",
    )
    attempt = store.begin_resume_delivery(decided.id)
    store.mark_resume_delivered(
        decided.id,
        expected_record_version=attempt.record_version,
    )

    repaired = store.requeue_verified_undelivered_resume(
        decided.id,
        error="Verified legacy delivery wrote an empty prompt; retry is queued.",
    )

    assert repaired.state is RequestState.CHANGES_REQUESTED
    assert repaired.resume_state is ResumeState.FAILED
    assert (
        repaired.resume_error == "Verified legacy delivery wrote an empty prompt; retry is queued."
    )


def test_owner_token_is_registered_once_and_verified_without_storing_plaintext(
    tmp_path: Path,
) -> None:
    store = GateStore(tmp_path / "gate.db")
    token = "a" * 64

    assert store.register_owner_token(token) is True
    assert store.register_owner_token(token) is True
    assert store.register_owner_token("b" * 64) is False
    assert store.verify_owner_token(token) is True
    assert store.verify_owner_token("b" * 64) is False

    raw = (tmp_path / "gate.db").read_bytes()
    assert token.encode() not in raw


def test_owner_token_rejects_short_values(tmp_path: Path) -> None:
    store = GateStore(tmp_path / "gate.db")

    with pytest.raises(ValueError, match="32"):
        store.register_owner_token("short")
