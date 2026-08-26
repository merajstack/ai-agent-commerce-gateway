"""
Replay Protection Tests — Agent Commerce Gateway Prompt 5
==========================================================

Tests cover:
    1. Basic reservation    — first use succeeds; second use → replay
    2. Persistence          — survives object/process recreation (same DB file)
    3. Fail closed          — DB read/write failures → BLOCK, never ALLOW
    4. Transaction ID       — recurring mandate idempotency key
    5. Concurrency          — two simultaneous reservations: exactly one wins
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.replay import ReplayResult, ReplayStore, SQLiteReplayStore
from app.db.database import Base, get_engine, get_session_factory, init_db


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def engine():
    """Fresh in-memory SQLite engine for each test."""
    e = get_engine("sqlite:///:memory:")
    init_db(e)
    yield e
    e.dispose()


@pytest.fixture()
def session(engine):
    """Single session connected to the in-memory engine."""
    factory = get_session_factory(engine)
    s = factory()
    yield s
    s.close()


@pytest.fixture()
def store(session):
    """SQLiteReplayStore connected to the in-memory session."""
    return SQLiteReplayStore(session)


# ══════════════════════════════════════════════════════════════════════════════
# 1. Basic reservation — authorization nonce (one_time mandates)
# ══════════════════════════════════════════════════════════════════════════════


class TestAuthorizationNonceBasic:

    def test_first_reservation_succeeds(self, store: SQLiteReplayStore):
        """First reservation of a nonce returns allowed=True."""
        result = store.check_and_reserve_authorization_nonce("nonce-abc-001")
        assert result.allowed is True
        assert result.was_replay is False

    def test_second_reservation_of_same_nonce_is_replay(self, store: SQLiteReplayStore):
        """Same nonce submitted twice → second attempt is a replay."""
        store.check_and_reserve_authorization_nonce("nonce-replay-001")
        result = store.check_and_reserve_authorization_nonce("nonce-replay-001")
        assert result.allowed is False
        assert result.was_replay is True
        assert "already been used" in result.reason.lower()

    def test_different_nonces_both_succeed(self, store: SQLiteReplayStore):
        """Two different nonces can both be reserved."""
        r1 = store.check_and_reserve_authorization_nonce("nonce-A")
        r2 = store.check_and_reserve_authorization_nonce("nonce-B")
        assert r1.allowed is True
        assert r2.allowed is True

    def test_third_attempt_still_replay(self, store: SQLiteReplayStore):
        """Third+ attempt on same nonce consistently detected as replay."""
        store.check_and_reserve_authorization_nonce("nonce-multi")
        store.check_and_reserve_authorization_nonce("nonce-multi")
        result = store.check_and_reserve_authorization_nonce("nonce-multi")
        assert result.allowed is False
        assert result.was_replay is True


# ══════════════════════════════════════════════════════════════════════════════
# 2. Basic reservation — transaction ID (recurring mandates)
# ══════════════════════════════════════════════════════════════════════════════


class TestTransactionIdBasic:

    def test_first_transaction_id_succeeds(self, store: SQLiteReplayStore):
        result = store.check_and_reserve_transaction_id("txn-001")
        assert result.allowed is True
        assert result.was_replay is False

    def test_duplicate_transaction_id_is_replay(self, store: SQLiteReplayStore):
        store.check_and_reserve_transaction_id("txn-dup-001")
        result = store.check_and_reserve_transaction_id("txn-dup-001")
        assert result.allowed is False
        assert result.was_replay is True
        assert "already been submitted" in result.reason.lower()

    def test_different_transaction_ids_both_succeed(self, store: SQLiteReplayStore):
        r1 = store.check_and_reserve_transaction_id("txn-A")
        r2 = store.check_and_reserve_transaction_id("txn-B")
        assert r1.allowed is True
        assert r2.allowed is True


# ══════════════════════════════════════════════════════════════════════════════
# 3. Persistence — survives object recreation (same underlying DB)
# ══════════════════════════════════════════════════════════════════════════════


class TestPersistence:

    def test_nonce_persists_across_store_recreation(self, engine):
        """
        A nonce reserved by one SQLiteReplayStore instance is still
        detected as a replay by a new instance using the same engine.
        This simulates process restart with a persistent SQLite file.
        """
        factory = get_session_factory(engine)

        # First instance reserves the nonce.
        s1 = factory()
        store1 = SQLiteReplayStore(s1)
        r1 = store1.check_and_reserve_authorization_nonce("nonce-persist-001")
        assert r1.allowed is True
        s1.close()

        # New instance — simulates a restarted process or new request worker.
        s2 = factory()
        store2 = SQLiteReplayStore(s2)
        r2 = store2.check_and_reserve_authorization_nonce("nonce-persist-001")
        assert r2.allowed is False
        assert r2.was_replay is True
        s2.close()

    def test_transaction_id_persists_across_store_recreation(self, engine):
        """Same persistence test for transaction_id (recurring mandate idempotency)."""
        factory = get_session_factory(engine)

        s1 = factory()
        SQLiteReplayStore(s1).check_and_reserve_transaction_id("txn-persist-001")
        s1.close()

        s2 = factory()
        r2 = SQLiteReplayStore(s2).check_and_reserve_transaction_id("txn-persist-001")
        assert r2.allowed is False
        assert r2.was_replay is True
        s2.close()


# ══════════════════════════════════════════════════════════════════════════════
# 4. Fail closed — DB errors always produce BLOCK
# ══════════════════════════════════════════════════════════════════════════════


class TestFailClosed:

    def test_sqlalchemy_error_on_authorization_nonce_blocks(self, engine):
        """
        If the session.commit() raises an unexpected SQLAlchemyError,
        the result must be BLOCK (allowed=False, was_replay=False).
        """
        factory = get_session_factory(engine)
        session = factory()
        store = SQLiteReplayStore(session)

        with patch.object(session, "commit", side_effect=OperationalError(
            "disk full", None, Exception("disk full")
        )):
            result = store.check_and_reserve_authorization_nonce("nonce-fail-001")

        assert result.allowed is False
        assert result.was_replay is False
        assert "unavailable" in result.reason.lower()
        session.close()

    def test_sqlalchemy_error_on_transaction_id_blocks(self, engine):
        """Same fail-closed test for transaction_id reservation."""
        factory = get_session_factory(engine)
        session = factory()
        store = SQLiteReplayStore(session)

        with patch.object(session, "commit", side_effect=OperationalError(
            "disk full", None, Exception("disk full")
        )):
            result = store.check_and_reserve_transaction_id("txn-fail-001")

        assert result.allowed is False
        assert result.was_replay is False
        assert "unavailable" in result.reason.lower()
        session.close()

    def test_unexpected_exception_on_authorization_nonce_blocks(self, engine):
        """Any non-SQLAlchemy exception also fails closed."""
        factory = get_session_factory(engine)
        session = factory()
        store = SQLiteReplayStore(session)

        with patch.object(session, "add", side_effect=RuntimeError("unexpected!")):
            result = store.check_and_reserve_authorization_nonce("nonce-exc-001")

        assert result.allowed is False
        assert result.was_replay is False
        assert "unavailable" in result.reason.lower()
        session.close()

    def test_unexpected_exception_on_transaction_id_blocks(self, engine):
        """Any non-SQLAlchemy exception on transaction_id also fails closed."""
        factory = get_session_factory(engine)
        session = factory()
        store = SQLiteReplayStore(session)

        with patch.object(session, "add", side_effect=RuntimeError("unexpected!")):
            result = store.check_and_reserve_transaction_id("txn-exc-001")

        assert result.allowed is False
        assert result.was_replay is False
        assert "unavailable" in result.reason.lower()
        session.close()

    def test_db_error_reason_does_not_expose_internals(self, engine):
        """
        The reason string returned to the caller must not contain
        raw SQLAlchemy or database error details.
        """
        factory = get_session_factory(engine)
        session = factory()
        store = SQLiteReplayStore(session)

        with patch.object(session, "commit", side_effect=OperationalError(
            "SUPER_SECRET_INTERNAL_TABLE_NAME", None, Exception("internal")
        )):
            result = store.check_and_reserve_authorization_nonce("nonce-safe-001")

        assert "SUPER_SECRET_INTERNAL_TABLE_NAME" not in result.reason
        session.close()


# ══════════════════════════════════════════════════════════════════════════════
# 5. ReplayResult model
# ══════════════════════════════════════════════════════════════════════════════


class TestReplayResult:

    def test_replay_result_is_immutable(self):
        r = ReplayResult(allowed=True, was_replay=False, reason="ok")
        with pytest.raises((AttributeError, TypeError)):
            r.allowed = False  # type: ignore[misc]

    def test_replay_result_repr_allowed(self):
        r = ReplayResult(allowed=True, was_replay=False, reason="ok")
        assert "ALLOWED" in repr(r)

    def test_replay_result_repr_replay(self):
        r = ReplayResult(allowed=False, was_replay=True, reason="replay")
        assert "REPLAY" in repr(r)

    def test_replay_result_repr_unavailable(self):
        r = ReplayResult(allowed=False, was_replay=False, reason="unavailable")
        assert "UNAVAILABLE" in repr(r)


# ══════════════════════════════════════════════════════════════════════════════
# 6. Concurrency test — race condition protection
# ══════════════════════════════════════════════════════════════════════════════


class TestConcurrency:

    def test_concurrent_authorization_nonce_reservation(self, tmp_path):
        """
        Two threads attempt to reserve the same nonce at approximately the
        same time.  Exactly ONE must succeed; the other must be BLOCKED.

        This proves the atomicity guarantee survives concurrent access.
        SQLite serializes writes at the file/connection level; the UNIQUE
        constraint ensures only one INSERT can succeed.

        Uses a file-based SQLite DB (tmp_path) so both threads share the
        same physical database.  An in-memory DB is per-connection in SQLite,
        so threads would each see their own empty DB.
        """
        db_path = tmp_path / "concurrent_auth.db"
        shared_engine = get_engine(f"sqlite:///{db_path}")
        init_db(shared_engine)
        factory = get_session_factory(shared_engine)
        nonce = "nonce-concurrent-001"
        results: list[ReplayResult] = []
        lock = threading.Lock()

        def attempt():
            session = factory()
            store = SQLiteReplayStore(session)
            result = store.check_and_reserve_authorization_nonce(nonce)
            with lock:
                results.append(result)
            session.close()

        t1 = threading.Thread(target=attempt)
        t2 = threading.Thread(target=attempt)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        shared_engine.dispose()

        assert len(results) == 2

        allowed_count = sum(1 for r in results if r.allowed)
        blocked_count = sum(1 for r in results if not r.allowed)

        assert allowed_count == 1, (
            f"Expected exactly 1 allowed, got {allowed_count}. Results: {results}"
        )
        assert blocked_count == 1, (
            f"Expected exactly 1 blocked, got {blocked_count}. Results: {results}"
        )

    def test_concurrent_transaction_id_reservation(self, tmp_path):
        """
        Two threads attempt to reserve the same transaction_id at the same time.
        Exactly ONE must succeed.
        """
        db_path = tmp_path / "concurrent_txn.db"
        shared_engine = get_engine(f"sqlite:///{db_path}")
        init_db(shared_engine)
        factory = get_session_factory(shared_engine)
        txn_id = "txn-concurrent-001"
        results: list[ReplayResult] = []
        lock = threading.Lock()

        def attempt():
            session = factory()
            store = SQLiteReplayStore(session)
            result = store.check_and_reserve_transaction_id(txn_id)
            with lock:
                results.append(result)
            session.close()

        t1 = threading.Thread(target=attempt)
        t2 = threading.Thread(target=attempt)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        shared_engine.dispose()

        assert len(results) == 2
        assert sum(1 for r in results if r.allowed) == 1
        assert sum(1 for r in results if not r.allowed) == 1
