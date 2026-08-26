"""
ORM Models — Agent Commerce Gateway
=====================================

Defines the SQLite table schemas used for replay protection and audit trail.

Tables:
    used_authorization_nonces
        Stores nonces from one_time mandate SignedAuthorizations that have
        been successfully verified and reserved by the replay store.

        The UNIQUE constraint on `nonce` is the atomic reservation mechanism.
        The replay store attempts an INSERT; if a duplicate nonce exists,
        SQLAlchemy raises IntegrityError, which the replay store interprets
        as a replay attempt.

    used_transaction_nonces
        Stores transaction_ids for recurring mandate transactions.

        Recurring mandates are not consumed per-transaction (the mandate stays
        alive across multiple payments). Instead, individual transaction_ids
        are reserved here to prevent a single transaction request from being
        processed twice (idempotency protection).

        The UNIQUE constraint on `transaction_id` provides the same atomic
        reservation guarantee as used_authorization_nonces.

    audit_events
        Append-only audit trail for every transaction decision and Razorpay
        execution event. Never stores secrets, private keys, signatures, or
        bearer tokens. Only safe, audit-relevant fields are persisted.

Security note:
    Replay protection tables use database-level UNIQUE constraints — not
    application-level checks — as the replay-detection mechanism. This ensures
    atomicity across concurrent requests even across separate OS processes
    sharing the same SQLite file (SQLite serializes writes at the file level).

    Audit events are append-only; there is no UPDATE or DELETE path.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class UsedAuthorizationNonce(Base):
    """
    Records a one_time mandate authorization nonce that has been consumed.

    Once a row exists for a given nonce, any subsequent attempt to insert
    the same nonce raises IntegrityError — the replay detection signal.

    Columns:
        nonce        (PK, UNIQUE): The mandate nonce string.
        reserved_at:               UTC timestamp when the nonce was consumed.
    """

    __tablename__ = "used_authorization_nonces"

    nonce: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        nullable=False,
        comment="Mandate nonce consumed by a one_time authorization.",
    )
    reserved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        comment="UTC timestamp when this nonce was first reserved.",
    )

    def __repr__(self) -> str:
        return f"UsedAuthorizationNonce(nonce={self.nonce!r}, reserved_at={self.reserved_at!r})"


class UsedTransactionNonce(Base):
    """
    Records a transaction_id that has been reserved for idempotency.

    Used by recurring mandate flows to prevent the same transaction request
    from being processed more than once.  The mandate nonce itself is NOT
    stored here — the mandate remains valid for future distinct transactions.

    Columns:
        transaction_id (PK, UNIQUE): The CommerceRequest.transaction_id.
        reserved_at:                 UTC timestamp when the ID was reserved.
    """

    __tablename__ = "used_transaction_nonces"

    transaction_id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        nullable=False,
        comment="Transaction ID reserved for idempotency (recurring mandates).",
    )
    reserved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        comment="UTC timestamp when this transaction_id was first reserved.",
    )

    def __repr__(self) -> str:
        return (
            f"UsedTransactionNonce(transaction_id={self.transaction_id!r}, "
            f"reserved_at={self.reserved_at!r})"
        )


class AuditEvent(Base):
    """
    Append-only audit event record for every significant gateway action.

    Recorded for:
      - Every pipeline stage result (VALIDATION, AUTHORIZATION, REPLAY, POLICY)
      - Razorpay execution attempts and results
      - Payment lifecycle transitions (order_created, authorized, captured, failed)
      - Audit failure meta-events (when logging itself fails)

    Security invariants:
      - Never stores: Razorpay key_secret, bearer tokens, private keys, raw
        signatures, or any sensitive authentication credential.
      - Append-only: no UPDATE or DELETE paths exist.
      - Audit failure never blocks or changes a transaction decision.
      - buyer_agent_id is stored as-is (it is a stable non-secret identifier).
    """

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
        comment="Surrogate primary key.",
    )
    transaction_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        index=True,
        comment="The CommerceRequest/gateway transaction ID.",
    )
    event_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        comment="UTC timestamp when this event occurred.",
    )
    stage: Mapped[str] = mapped_column(
        String,
        nullable=False,
        comment="Pipeline stage: VALIDATION, AUTHORIZATION, REPLAY, POLICY, EXECUTION, FINAL.",
    )
    event_type: Mapped[str] = mapped_column(
        String,
        nullable=False,
        comment="Event category: DECISION, EXECUTION, FAILURE, AUDIT_ERROR.",
    )
    decision: Mapped[str] = mapped_column(
        String,
        nullable=True,
        comment="GatewayDecision: ALLOW, REVIEW, BLOCK, UNDECIDED. Null for non-decision events.",
    )
    reason: Mapped[str] = mapped_column(
        String,
        nullable=True,
        comment="Human-readable reason. Must not contain secrets.",
    )
    merchant_id: Mapped[str] = mapped_column(
        String,
        nullable=True,
        comment="Merchant identifier.",
    )
    buyer_agent_id: Mapped[str] = mapped_column(
        String,
        nullable=True,
        comment="Buyer agent identifier (safe non-secret stable ID).",
    )
    protocol: Mapped[str] = mapped_column(
        String,
        nullable=True,
        comment="BuyerProtocol: acp, x402, etc.",
    )
    amount_minor: Mapped[int] = mapped_column(
        nullable=True,
        comment="Transaction amount in minor currency units.",
    )
    currency: Mapped[str] = mapped_column(
        String,
        nullable=True,
        comment="ISO 4217 currency code.",
    )
    razorpay_order_id: Mapped[str] = mapped_column(
        String,
        nullable=True,
        comment="Razorpay order ID when available.",
    )
    razorpay_payment_id: Mapped[str] = mapped_column(
        String,
        nullable=True,
        comment="Razorpay payment ID when available.",
    )
    razorpay_payment_status: Mapped[str] = mapped_column(
        String,
        nullable=True,
        comment="Razorpay payment status when available.",
    )

    def __repr__(self) -> str:
        return (
            f"AuditEvent(id={self.id!r}, txn={self.transaction_id!r}, "
            f"stage={self.stage!r}, decision={self.decision!r})"
        )
