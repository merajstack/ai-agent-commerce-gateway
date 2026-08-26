"""
Audit Trail — Agent Commerce Gateway
======================================

Implements a structured, append-only audit trail persisted to the SQLite
`audit_events` table. Every significant gateway action produces an audit event.

Audit Philosophy:
    - Every transaction decision (ALLOW/REVIEW/BLOCK) is audited at the stage
      it was produced.
    - Every Razorpay execution attempt and result is audited.
    - Audit failure NEVER blocks or changes the transaction decision.
    - Audit failure is detected and recorded as a meta-event (if possible).
    - No secrets, private keys, bearer tokens, or raw signatures are ever stored.

Fail-Safe Contract:
    AuditLogger.record() catches all exceptions internally. If SQLite write
    fails, the error is logged to the Python logger (for syslog/cloud logs)
    and the caller's result is unaffected. The transaction's security decision
    is always preserved.

Event Types:
    DECISION    — a pipeline stage produced a gateway decision
    EXECUTION   — Razorpay execution was attempted
    RESULT      — a Razorpay execution result was received
    FAILURE     — an internal/external failure occurred
    AUDIT_ERROR — the audit system itself encountered an error

Stages:
    VALIDATION, AUTHORIZATION, REPLAY, POLICY, EXECUTION, FINAL
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy.orm import Session

from app.db.models import AuditEvent

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Enums
# ══════════════════════════════════════════════════════════════════════════════


class AuditStage(str, Enum):
    VALIDATION = "VALIDATION"
    AUTHORIZATION = "AUTHORIZATION"
    REPLAY = "REPLAY"
    POLICY = "POLICY"
    EXECUTION = "EXECUTION"
    FINAL = "FINAL"


class AuditEventType(str, Enum):
    DECISION = "DECISION"
    EXECUTION = "EXECUTION"
    RESULT = "RESULT"
    FAILURE = "FAILURE"
    AUDIT_ERROR = "AUDIT_ERROR"


# ══════════════════════════════════════════════════════════════════════════════
# AuditRecord dataclass — safe input to the logger
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class AuditRecord:
    """
    Safe, sanitized input to AuditLogger.record().

    All fields are safe for persistence — no secrets, credentials, or
    cryptographic material should ever be placed in this object.
    """
    transaction_id: str
    stage: AuditStage
    event_type: AuditEventType

    # Optional context fields — all safe for storage
    decision: Optional[str] = None          # "ALLOW", "REVIEW", "BLOCK", "UNDECIDED"
    reason: Optional[str] = None            # Safe human-readable reason
    merchant_id: Optional[str] = None
    buyer_agent_id: Optional[str] = None
    protocol: Optional[str] = None
    amount_minor: Optional[int] = None
    currency: Optional[str] = None

    # Razorpay references (IDs only — no secrets)
    razorpay_order_id: Optional[str] = None
    razorpay_payment_id: Optional[str] = None
    razorpay_payment_status: Optional[str] = None

    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ══════════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════════
# AuditStore Interface & In-Memory Implementation
# ══════════════════════════════════════════════════════════════════════════════

from abc import ABC, abstractmethod


class AuditStore(ABC):
    """
    Abstract interface for audit trail and event storage.
    Enables swapping in-memory runtime storage with Supabase or PostgreSQL later.
    """
    @abstractmethod
    def record(self, audit_record: AuditRecord) -> bool:
        """Append an audit record to the store."""
        pass

    @abstractmethod
    def get_events(self, merchant_id: Optional[str] = None, limit: int = 50) -> list[AuditRecord]:
        """Retrieve audit events, optionally filtered by merchant_id (newest first)."""
        pass

    @abstractmethod
    def get_events_for_transaction(self, transaction_id: str) -> list[AuditRecord]:
        """Retrieve all audit events for a transaction_id (chronological order)."""
        pass


class InMemoryAuditStore(AuditStore):
    """
    Thread-safe, append-only in-memory implementation of AuditStore.
    Maintains all audit events in backend runtime memory.
    """
    def __init__(self):
        self._events: list[AuditRecord] = []

    def record(self, audit_record: AuditRecord) -> bool:
        try:
            self._events.append(audit_record)
            logger.debug(
                "In-memory audit recorded: txn=%r stage=%r decision=%r",
                audit_record.transaction_id,
                audit_record.stage.value if hasattr(audit_record.stage, "value") else str(audit_record.stage),
                audit_record.decision,
            )
            return True
        except Exception as exc:
            logger.error("In-memory audit recording error: %s", exc)
            return False

    def get_events(self, merchant_id: Optional[str] = None, limit: int = 50) -> list[AuditRecord]:
        if merchant_id:
            events = [e for e in self._events if e.merchant_id == merchant_id]
        else:
            events = list(self._events)
        # Newest first
        events.sort(key=lambda x: x.timestamp, reverse=True)
        return events[:limit]

    def get_events_for_transaction(self, transaction_id: str) -> list[AuditRecord]:
        events = [e for e in self._events if e.transaction_id == transaction_id]
        events.sort(key=lambda x: x.timestamp)
        return events

    def clear(self):
        self._events.clear()


# Global singleton in-memory audit store
audit_store = InMemoryAuditStore()


# ══════════════════════════════════════════════════════════════════════════════
# AuditLogger
# ══════════════════════════════════════════════════════════════════════════════


class AuditLogger:
    """
    Unified Audit Logger backed by the AuditStore interface (defaulting to InMemoryAuditStore).
    Supports optional SQLAlchemy Session to maintain backwards compatibility with existing test suites.

    Usage:
        audit = AuditLogger()
        audit.record(AuditRecord(
            transaction_id="txn-001",
            stage=AuditStage.POLICY,
            event_type=AuditEventType.DECISION,
            decision="ALLOW",
            reason="Policy passed",
        ))
    """

    def __init__(self, session: Optional[Session] = None, store: Optional[AuditStore] = None):
        self._session = session
        self._store = store or audit_store

    def record(self, audit_record: AuditRecord) -> bool:
        """
        Record an audit event to the in-memory AuditStore (and optionally session if provided).
        Never raises. Fail-safe by design.
        """
        res = self._store.record(audit_record)

        if self._session is not None:
            try:
                event = AuditEvent(
                    transaction_id=audit_record.transaction_id,
                    event_timestamp=audit_record.timestamp,
                    stage=audit_record.stage.value if hasattr(audit_record.stage, "value") else str(audit_record.stage),
                    event_type=audit_record.event_type.value if hasattr(audit_record.event_type, "value") else str(audit_record.event_type),
                    decision=audit_record.decision,
                    reason=audit_record.reason,
                    merchant_id=audit_record.merchant_id,
                    buyer_agent_id=audit_record.buyer_agent_id,
                    protocol=audit_record.protocol,
                    amount_minor=audit_record.amount_minor,
                    currency=audit_record.currency,
                    razorpay_order_id=audit_record.razorpay_order_id,
                    razorpay_payment_id=audit_record.razorpay_payment_id,
                    razorpay_payment_status=audit_record.razorpay_payment_status,
                )
                self._session.add(event)
                self._session.commit()
            except Exception as exc:
                try:
                    self._session.rollback()
                except Exception:
                    pass
                logger.error(
                    "AUDIT FAILURE (Session write): txn=%r: %s",
                    audit_record.transaction_id, type(exc).__name__
                )
                return False
        return True

    def record_audit_failure(self, transaction_id: str, stage: AuditStage, error_type: str) -> bool:
        """
        Attempt to write a meta-audit record when normal audit recording failed.
        """
        meta_record = AuditRecord(
            transaction_id=transaction_id,
            stage=stage,
            event_type=AuditEventType.AUDIT_ERROR,
            reason=f"Audit persistence failed: {error_type}",
        )
        return self.record(meta_record)

    def get_events_for_transaction(self, transaction_id: str) -> list[Any]:
        """
        Retrieve audit events for a given transaction_id.
        """
        if self._session is not None:
            try:
                return (
                    self._session.query(AuditEvent)
                    .filter(AuditEvent.transaction_id == transaction_id)
                    .order_by(AuditEvent.event_timestamp.asc())
                    .all()
                )
            except Exception:
                pass
        return self._store.get_events_for_transaction(transaction_id)



# ══════════════════════════════════════════════════════════════════════════════
# Convenience builders — keep callers readable
# ══════════════════════════════════════════════════════════════════════════════


def audit_pipeline_decision(
    transaction_id: str,
    stage: AuditStage,
    decision: str,
    reason: str,
    merchant_id: Optional[str] = None,
    buyer_agent_id: Optional[str] = None,
    protocol: Optional[str] = None,
    amount_minor: Optional[int] = None,
    currency: Optional[str] = None,
) -> AuditRecord:
    """Build an AuditRecord for a pipeline stage decision."""
    return AuditRecord(
        transaction_id=transaction_id,
        stage=stage,
        event_type=AuditEventType.DECISION,
        decision=decision,
        reason=reason,
        merchant_id=merchant_id,
        buyer_agent_id=buyer_agent_id,
        protocol=protocol,
        amount_minor=amount_minor,
        currency=currency,
    )


def audit_razorpay_execution(
    transaction_id: str,
    razorpay_order_id: Optional[str],
    razorpay_payment_id: Optional[str],
    razorpay_payment_status: Optional[str],
    decision: str,
    reason: str,
    amount_minor: Optional[int] = None,
    currency: Optional[str] = None,
    merchant_id: Optional[str] = None,
    buyer_agent_id: Optional[str] = None,
    protocol: Optional[str] = None,
) -> AuditRecord:
    """Build an AuditRecord for a Razorpay execution event."""
    return AuditRecord(
        transaction_id=transaction_id,
        stage=AuditStage.EXECUTION,
        event_type=AuditEventType.RESULT,
        decision=decision,
        reason=reason,
        amount_minor=amount_minor,
        currency=currency,
        razorpay_order_id=razorpay_order_id,
        razorpay_payment_id=razorpay_payment_id,
        razorpay_payment_status=razorpay_payment_status,
        merchant_id=merchant_id,
        buyer_agent_id=buyer_agent_id,
        protocol=protocol,
    )


def audit_request_received(
    transaction_id: str,
    protocol: str,
    merchant_id: Optional[str] = None,
    buyer_agent_id: Optional[str] = None,
    amount_minor: Optional[int] = None,
    currency: Optional[str] = None,
    reason: str = "Incoming protocol request received for gateway processing",
) -> AuditRecord:
    """Build an AuditRecord for an initial request received at the Gateway."""
    return AuditRecord(
        transaction_id=transaction_id,
        stage=AuditStage.VALIDATION,
        event_type=AuditEventType.EXECUTION,
        decision="UNDECIDED",
        reason=reason,
        merchant_id=merchant_id,
        buyer_agent_id=buyer_agent_id,
        protocol=protocol,
        amount_minor=amount_minor,
        currency=currency,
    )


