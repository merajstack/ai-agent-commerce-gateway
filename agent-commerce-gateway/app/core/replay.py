"""
Replay Protection — Agent Commerce Gateway
==========================================

Answers the question:
    **Has this authorization/transaction nonce already been consumed?**

Security contract:
    - This module is a SECURITY GATE.
    - If the store cannot be read or written, the result is BLOCK.
    - The system never assumes a nonce is unused on database failure.
    - There is no separate check() then consume() — the operation is atomic.

Atomic reservation:
    Both `check_and_reserve_*` methods attempt an INSERT with a UNIQUE-
    constrained primary key.  The database serializes concurrent writes.
    If two requests arrive simultaneously with the same nonce:

        Request A → INSERT succeeds → allowed
        Request B → INSERT raises IntegrityError → replay detected → BLOCK

    There is no window between check and consume because they are the same
    database operation.

One-time mandate semantics:
    The authorization nonce (Mandate.nonce from the SignedAuthorization) is
    consumed on the FIRST successful authorization + replay check.

    If the same signed authorization is submitted again:
        → BLOCK, reason = "Authorization nonce has already been used."

    The nonce is reserved even if merchant policy subsequently BLOCKs the
    transaction.  A one_time credential is single-use from the moment the
    gateway verifies and reserves it.  To retry after a policy block, the
    buyer must obtain a new signed authorization with a fresh nonce.

    Rationale: Allowing retry of the same one_time credential after a policy
    block would create a window for policy-condition-based retry attacks —
    an adversary could submit the same authorization repeatedly until some
    external policy condition changes.

Recurring mandate semantics:
    The mandate nonce is NOT consumed per transaction.  Instead, each unique
    transaction_id is reserved as an idempotency key.

    This prevents:
        - The same transaction request being processed twice (double-charge).
        - Network retries causing duplicate payments.

    This allows:
        - The same mandate to authorize multiple distinct transactions.

    If policy blocks a recurring transaction, the transaction_id reservation
    remains, but the mandate is unaffected and future transactions with new
    transaction_ids proceed normally.

Fail-closed contract:
    Any exception during store operations (DB connection error, disk full,
    unexpected exception) → BLOCK.

    The reason returned to the pipeline is:
        "Replay protection unavailable; transaction blocked."

    Internal details are logged at ERROR level for audit/debugging.
    Raw database error messages are never surfaced to callers.

Interface:
    ReplayStore (abstract base class)
        check_and_reserve_authorization_nonce(nonce: str) -> ReplayResult
        check_and_reserve_transaction_id(transaction_id: str) -> ReplayResult

    SQLiteReplayStore (concrete implementation)
        Backed by SQLAlchemy + SQLite.
        Replace with SupabaseReplayStore later without changing the pipeline.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.models import UsedAuthorizationNonce, UsedTransactionNonce

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# ReplayResult — structured outcome of a replay check
# ──────────────────────────────────────────────────────────────────────────────

_UNAVAILABLE_REASON = "Replay protection unavailable; transaction blocked."


@dataclass(frozen=True)
class ReplayResult:
    """
    Result of a replay check-and-reserve operation.

    Attributes:
        allowed:    True if the nonce was successfully reserved (first use).
                    False if a replay was detected OR the store was unavailable.
        was_replay: True if the nonce was already present in the store.
                    False if the store was unavailable or the nonce is new.
                    Distinguishing replay vs. unavailability enables precise
                    audit logging without leaking DB internals.
        reason:     Human-readable explanation.  Safe for audit logs.
                    Must NOT contain raw database error messages.
    """

    allowed: bool
    was_replay: bool
    reason: str

    def __repr__(self) -> str:
        status = "ALLOWED" if self.allowed else ("REPLAY" if self.was_replay else "UNAVAILABLE")
        return f"ReplayResult({status}: {self.reason})"


# ──────────────────────────────────────────────────────────────────────────────
# ReplayStore — abstract interface
# ──────────────────────────────────────────────────────────────────────────────


class ReplayStore(ABC):
    """
    Abstract interface for the replay/idempotency store.

    Implementations may use SQLite, Supabase, Redis, etc.
    The pipeline depends only on this interface — swapping implementations
    does not require changing any orchestration code.

    Every method must be atomic: check and reserve in a single operation.
    Every method must fail closed: exceptions → ReplayResult(allowed=False).
    """

    @abstractmethod
    def check_and_reserve_authorization_nonce(self, nonce: str) -> ReplayResult:
        """
        Atomically check whether an authorization nonce has been used and,
        if not, reserve it.

        Used for one_time mandates.

        Args:
            nonce: The mandate nonce from the SignedAuthorization payload.

        Returns:
            ReplayResult:
                allowed=True, was_replay=False → first use; nonce now reserved.
                allowed=False, was_replay=True → nonce already consumed; replay.
                allowed=False, was_replay=False → store unavailable; fail closed.
        """

    @abstractmethod
    def check_and_reserve_transaction_id(self, transaction_id: str) -> ReplayResult:
        """
        Atomically check whether a transaction_id has been processed and,
        if not, reserve it.

        Used for recurring mandates (idempotency, not authorization consumption).

        Args:
            transaction_id: The CommerceRequest.transaction_id.

        Returns:
            ReplayResult:
                allowed=True, was_replay=False → first use; ID now reserved.
                allowed=False, was_replay=True → transaction already submitted.
                allowed=False, was_replay=False → store unavailable; fail closed.
        """


# ──────────────────────────────────────────────────────────────────────────────
# SQLiteReplayStore — concrete SQLite-backed implementation
# ──────────────────────────────────────────────────────────────────────────────


class SQLiteReplayStore(ReplayStore):
    """
    SQLite-backed replay store using SQLAlchemy ORM sessions.

    Atomicity mechanism:
        Both UNIQUE-constrained primary key columns (nonce, transaction_id)
        guarantee that a concurrent INSERT of the same value raises
        IntegrityError.  SQLAlchemy catches this; the caller receives
        ReplayResult(allowed=False, was_replay=True).

    Fail-closed:
        Any SQLAlchemy error other than IntegrityError, or any unexpected
        Python exception, produces ReplayResult(allowed=False, was_replay=False)
        with a safe reason string.  Details are logged at ERROR level.

    Args:
        session: An active SQLAlchemy Session connected to the replay store DB.
                 The caller owns the session lifecycle (open/close/rollback).
                 This makes the store testable with injected sessions.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def check_and_reserve_authorization_nonce(self, nonce: str) -> ReplayResult:
        """
        Attempt to INSERT the nonce into `used_authorization_nonces`.

        Returns:
            allowed=True  → INSERT succeeded; nonce reserved.
            allowed=False, was_replay=True  → nonce already exists.
            allowed=False, was_replay=False → DB error; fail closed.
        """
        try:
            record = UsedAuthorizationNonce(
                nonce=nonce,
                reserved_at=datetime.now(timezone.utc),
            )
            self._session.add(record)
            self._session.commit()
            logger.info("Authorization nonce reserved: %r", nonce)
            return ReplayResult(
                allowed=True,
                was_replay=False,
                reason="Authorization nonce reserved successfully.",
            )

        except IntegrityError:
            # The nonce already exists → replay detected.
            self._session.rollback()
            logger.warning("Replay detected: authorization nonce already used: %r", nonce)
            return ReplayResult(
                allowed=False,
                was_replay=True,
                reason="Authorization nonce has already been used.",
            )

        except SQLAlchemyError as exc:
            self._session.rollback()
            logger.error(
                "SQLAlchemy error during authorization nonce reservation "
                "(nonce=%r): %s", nonce, exc, exc_info=True
            )
            return ReplayResult(
                allowed=False,
                was_replay=False,
                reason=_UNAVAILABLE_REASON,
            )

        except Exception as exc:  # noqa: BLE001
            self._session.rollback()
            logger.error(
                "Unexpected error during authorization nonce reservation "
                "(nonce=%r): %s", nonce, exc, exc_info=True
            )
            return ReplayResult(
                allowed=False,
                was_replay=False,
                reason=_UNAVAILABLE_REASON,
            )

    def check_and_reserve_transaction_id(self, transaction_id: str) -> ReplayResult:
        """
        Attempt to INSERT the transaction_id into `used_transaction_nonces`.

        Returns:
            allowed=True  → INSERT succeeded; transaction_id reserved.
            allowed=False, was_replay=True  → transaction_id already submitted.
            allowed=False, was_replay=False → DB error; fail closed.
        """
        try:
            record = UsedTransactionNonce(
                transaction_id=transaction_id,
                reserved_at=datetime.now(timezone.utc),
            )
            self._session.add(record)
            self._session.commit()
            logger.info("Transaction ID reserved: %r", transaction_id)
            return ReplayResult(
                allowed=True,
                was_replay=False,
                reason="Transaction ID reserved successfully.",
            )

        except IntegrityError:
            self._session.rollback()
            logger.warning(
                "Replay detected: transaction_id already submitted: %r", transaction_id
            )
            return ReplayResult(
                allowed=False,
                was_replay=True,
                reason="Transaction ID has already been submitted.",
            )

        except SQLAlchemyError as exc:
            self._session.rollback()
            logger.error(
                "SQLAlchemy error during transaction ID reservation "
                "(transaction_id=%r): %s", transaction_id, exc, exc_info=True
            )
            return ReplayResult(
                allowed=False,
                was_replay=False,
                reason=_UNAVAILABLE_REASON,
            )

        except Exception as exc:  # noqa: BLE001
            self._session.rollback()
            logger.error(
                "Unexpected error during transaction ID reservation "
                "(transaction_id=%r): %s", transaction_id, exc, exc_info=True
            )
            return ReplayResult(
                allowed=False,
                was_replay=False,
                reason=_UNAVAILABLE_REASON,
            )
