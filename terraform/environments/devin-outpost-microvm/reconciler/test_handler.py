"""Tests for the pure planning step.

`plan` is the whole session-to-MicroVM lifecycle mapping, so it is worth
covering without a queue server or an AWS account behind it.

Run with: python -m pytest reconciler/test_handler.py
"""

import handler

ACCEPTOR = "reconciler-1"
NOW = 1_000_000


def session(session_id, phase="pending", acceptor=None, status="pending", deadline=None):
    return {
        "metadata": {"session_id": session_id, "outpost_id": "outpost_env-test"},
        "spec": {"kind": "new", "platform": "linux"},
        "status": {
            "phase": phase,
            "acceptor_id": acceptor,
            "claim_deadline": deadline,
            "session_status": status,
        },
    }


def mine(session_id, status="running", deadline=NOW + 300):
    return session(session_id, phase="claimed", acceptor=ACCEPTOR, status=status, deadline=deadline)


def run(sessions, records=None, max_concurrent=2, renew_margin=60):
    return plan_set(
        handler.plan(sessions, records or {}, ACCEPTOR, max_concurrent, renew_margin, NOW)
    )


def plan_set(actions):
    return {(action, session_id) for action, session_id, _ in actions}


def test_pending_sessions_are_claimed_up_to_capacity():
    sessions = [session("s1"), session("s2"), session("s3")]
    assert run(sessions, max_concurrent=2) == {("claim", "s1"), ("claim", "s2")}


def test_existing_claims_consume_capacity():
    sessions = [mine("s1"), session("s2")]
    records = {"s1": {"session_id": "s1", "microvm_id": "mvm-1"}}
    assert run(sessions, records, max_concurrent=1) == set()


def test_sessions_claimed_by_others_are_ignored():
    sessions = [session("s1", phase="claimed", acceptor="someone-else", status="running")]
    assert run(sessions) == set()


def test_claim_without_a_microvm_starts_a_worker():
    assert run([mine("s1")]) == {("start_worker", "s1")}


def test_claim_near_its_deadline_is_renewed():
    records = {"s1": {"session_id": "s1", "microvm_id": "mvm-1"}}
    assert run([mine("s1", deadline=NOW + 30)], records) == {("renew", "s1")}


def test_healthy_claim_is_left_alone():
    records = {"s1": {"session_id": "s1", "microvm_id": "mvm-1"}}
    assert run([mine("s1", deadline=NOW + 300)], records) == set()


def test_terminated_session_is_torn_down():
    records = {"s1": {"session_id": "s1", "microvm_id": "mvm-1"}}
    assert run([mine("s1", status="terminated")], records) == {("terminate", "s1")}


def test_suspended_session_gives_its_microvm_back():
    records = {"s1": {"session_id": "s1", "microvm_id": "mvm-1"}}
    assert run([mine("s1", status="suspended")], records) == {("suspend", "s1")}


def test_record_with_no_live_claim_is_released():
    records = {"gone": {"session_id": "gone", "microvm_id": "mvm-9"}}
    assert run([], records) == {("release_orphan", "gone")}


def test_terminated_session_does_not_consume_capacity():
    sessions = [mine("s1", status="terminated"), session("s2")]
    assert run(sessions, max_concurrent=1) == {("terminate", "s1"), ("claim", "s2")}
