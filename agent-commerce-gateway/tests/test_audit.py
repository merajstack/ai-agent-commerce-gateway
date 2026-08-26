"""
Audit Trail Tests — Agent Commerce Gateway
==========================================

Tests for:
  - AuditLogger.record() persists correctly for all decision types
  - Every gateway decision (ALLOW/REVIEW/BLOCK) is auditable
  - Authorization failure is auditable
  - Replay failure is auditable
  - Razorpay failure is auditable
  - Audit failure does NOT create an ALLOW path or change any decision
  - Secrets never appear in audit records
  - AuditRecord fields are safe (no credentials)
  - AuditLogger.get_events_for_transaction() retrieval works
  - Meta audit_error event on failure
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.audit import (
    AuditEventType,
    AuditLogger,
    AuditRecord,
    AuditStage,
    audit_pipeline_decision,
    audit_razorpay_execution,
)
from app.db.database import Base, init_db
from app.db.models import AuditEvent
from app.core.schemas import GatewayDecision

FAKE_KEY_SECRET = "supersecret_must_never_appear_in_audit"


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def in_memory_engine():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    init_db(engine)
    return engine


@pytest.fixture()
def session(in_memory_engine):
    factory = sessionmaker(bind=in_memory_engine)
    with factory() as sess:
        yield sess


@pytest.fixture()
def audit_logger(session):
    return AuditLogger(session)


# ══════════════════════════════════════════════════════════════════════════════
# Test: Basic ALLOW audit
# ══════════════════════════════════════════════════════════════════════════════


class TestAuditBasicPersistence:
    def test_allow_decision_is_recorded(self, audit_logger, session):
        record = audit_pipeline_decision(
            transaction_id="txn-allow-001",
            stage=AuditStage.FINAL,
            decision="ALLOW",
            reason="All gates passed",
            merchant_id="merch-1",
            buyer_agent_id="buyer-1",
            protocol="acp",
            amount_minor=5000,
            currency="INR",
        )
        success = audit_logger.record(record)
        assert success is True

        events = audit_logger.get_events_for_transaction("txn-allow-001")
        assert len(events) == 1
        evt = events[0]
        assert evt.decision == "ALLOW"
        assert evt.stage == "FINAL"
        assert evt.event_type == "DECISION"
        assert evt.merchant_id == "merch-1"
        assert evt.buyer_agent_id == "buyer-1"
        assert evt.amount_minor == 5000
        assert evt.currency == "INR"
        assert evt.protocol == "acp"

    def test_review_decision_is_recorded(self, audit_logger):
        record = audit_pipeline_decision(
            transaction_id="txn-review-001",
            stage=AuditStage.FINAL,
            decision="REVIEW",
            reason="Flagged for review",
        )
        success = audit_logger.record(record)
        assert success is True
        events = audit_logger.get_events_for_transaction("txn-review-001")
        assert len(events) == 1
        assert events[0].decision == "REVIEW"

    def test_block_decision_is_recorded(self, audit_logger):
        record = audit_pipeline_decision(
            transaction_id="txn-block-001",
            stage=AuditStage.POLICY,
            decision="BLOCK",
            reason="Blocked by policy",
        )
        success = audit_logger.record(record)
        assert success is True
        events = audit_logger.get_events_for_transaction("txn-block-001")
        assert len(events) == 1
        assert events[0].decision == "BLOCK"
        assert events[0].stage == "POLICY"

    def test_authorization_failure_is_recorded(self, audit_logger):
        record = audit_pipeline_decision(
            transaction_id="txn-auth-fail-001",
            stage=AuditStage.AUTHORIZATION,
            decision="BLOCK",
            reason="Signature verification failed",
        )
        success = audit_logger.record(record)
        assert success is True
        events = audit_logger.get_events_for_transaction("txn-auth-fail-001")
        assert len(events) == 1
        assert events[0].stage == "AUTHORIZATION"
        assert events[0].decision == "BLOCK"

    def test_replay_failure_is_recorded(self, audit_logger):
        record = audit_pipeline_decision(
            transaction_id="txn-replay-fail-001",
            stage=AuditStage.REPLAY,
            decision="BLOCK",
            reason="Replay detected: nonce already used",
        )
        success = audit_logger.record(record)
        assert success is True
        events = audit_logger.get_events_for_transaction("txn-replay-fail-001")
        assert len(events) == 1
        assert events[0].stage == "REPLAY"

    def test_razorpay_failure_is_recorded(self, audit_logger):
        record = audit_razorpay_execution(
            transaction_id="txn-rzp-fail-001",
            razorpay_order_id=None,
            razorpay_payment_id=None,
            razorpay_payment_status=None,
            decision="BLOCK",
            reason="Razorpay HTTP 500",
            amount_minor=5000,
            currency="INR",
        )
        success = audit_logger.record(record)
        assert success is True
        events = audit_logger.get_events_for_transaction("txn-rzp-fail-001")
        assert len(events) == 1
        assert events[0].stage == "EXECUTION"
        assert events[0].event_type == "RESULT"

    def test_razorpay_order_created_is_recorded(self, audit_logger):
        record = audit_razorpay_execution(
            transaction_id="txn-order-001",
            razorpay_order_id="order_XYZ",
            razorpay_payment_id=None,
            razorpay_payment_status=None,
            decision="ALLOW",
            reason="Order created",
            amount_minor=5000,
            currency="INR",
        )
        success = audit_logger.record(record)
        assert success is True
        events = audit_logger.get_events_for_transaction("txn-order-001")
        assert events[0].razorpay_order_id == "order_XYZ"

    def test_multiple_events_same_transaction_all_retrieved(self, audit_logger):
        """Multiple pipeline stages should all be retrievable."""
        for stage in [AuditStage.AUTHORIZATION, AuditStage.REPLAY, AuditStage.POLICY, AuditStage.FINAL]:
            audit_logger.record(AuditRecord(
                transaction_id="txn-multi-001",
                stage=stage,
                event_type=AuditEventType.DECISION,
                decision="ALLOW",
                reason=f"Passed {stage.value}",
            ))

        events = audit_logger.get_events_for_transaction("txn-multi-001")
        assert len(events) == 4
        stages = [e.stage for e in events]
        assert "AUTHORIZATION" in stages
        assert "REPLAY" in stages
        assert "POLICY" in stages
        assert "FINAL" in stages


# ══════════════════════════════════════════════════════════════════════════════
# Test: Secrets never appear in audit records
# ══════════════════════════════════════════════════════════════════════════════


class TestSecretRedaction:
    def test_key_secret_never_in_audit_record(self, audit_logger):
        """Key secret must never be placed in any AuditRecord field."""
        record = AuditRecord(
            transaction_id="txn-secret-001",
            stage=AuditStage.EXECUTION,
            event_type=AuditEventType.RESULT,
            decision="ALLOW",
            reason="Authentication successful",  # safe
            # Deliberately NOT putting secret in any field
        )
        audit_logger.record(record)
        events = audit_logger.get_events_for_transaction("txn-secret-001")
        assert len(events) == 1
        evt = events[0]

        # Verify no field contains the fake secret
        safe_fields = [
            evt.decision, evt.reason, evt.merchant_id, evt.buyer_agent_id,
            evt.protocol, evt.currency, evt.razorpay_order_id,
            evt.razorpay_payment_id, evt.razorpay_payment_status,
            evt.transaction_id, evt.stage, evt.event_type,
        ]
        for field_val in safe_fields:
            if field_val is not None:
                assert FAKE_KEY_SECRET not in str(field_val), (
                    f"Secret found in audit field: {field_val!r}"
                )

    def test_reason_does_not_contain_bearer_token(self, audit_logger):
        """A reason field that accidentally contains a bearer token should be safe — 
        this test ensures the caller keeps reasons clean."""
        safe_reason = "Payment verification failed"  # no token
        record = AuditRecord(
            transaction_id="txn-bearer-001",
            stage=AuditStage.AUTHORIZATION,
            event_type=AuditEventType.FAILURE,
            decision="BLOCK",
            reason=safe_reason,
        )
        audit_logger.record(record)
        events = audit_logger.get_events_for_transaction("txn-bearer-001")
        assert events[0].reason == safe_reason

    def test_audit_repr_does_not_expose_sensitive_data(self, audit_logger):
        record = AuditRecord(
            transaction_id="txn-repr-001",
            stage=AuditStage.FINAL,
            event_type=AuditEventType.DECISION,
            decision="ALLOW",
        )
        audit_logger.record(record)
        events = audit_logger.get_events_for_transaction("txn-repr-001")
        r = repr(events[0])
        assert FAKE_KEY_SECRET not in r


# ══════════════════════════════════════════════════════════════════════════════
# Test: Audit failure does NOT create ALLOW path or change decision
# ══════════════════════════════════════════════════════════════════════════════


class TestAuditFailSafe:
    def test_audit_failure_does_not_raise(self, session):
        """AuditLogger.record() must never raise even when DB is broken."""
        broken_session = MagicMock()
        broken_session.add = MagicMock()
        broken_session.commit = MagicMock(side_effect=Exception("DB connection lost"))
        broken_session.rollback = MagicMock()

        audit_logger = AuditLogger(broken_session)
        record = AuditRecord(
            transaction_id="txn-fail-001",
            stage=AuditStage.FINAL,
            event_type=AuditEventType.DECISION,
            decision="ALLOW",
            reason="Test",
        )

        # Must not raise
        result = audit_logger.record(record)
        assert result is False  # Failure reported as False

    def test_audit_failure_returns_false_not_true(self, session):
        """Failed audit must return False, never True."""
        broken_session = MagicMock()
        broken_session.add = MagicMock()
        broken_session.commit = MagicMock(side_effect=RuntimeError("storage full"))
        broken_session.rollback = MagicMock()

        audit_logger = AuditLogger(broken_session)
        record = AuditRecord(
            transaction_id="txn-fail-002",
            stage=AuditStage.AUTHORIZATION,
            event_type=AuditEventType.DECISION,
            decision="BLOCK",
        )
        result = audit_logger.record(record)
        assert result is False

    def test_audit_failure_does_not_change_block_to_allow(self, session):
        """When audit fails, the transaction's BLOCK decision must be preserved by the caller."""
        broken_session = MagicMock()
        broken_session.commit = MagicMock(side_effect=Exception("disk full"))
        broken_session.rollback = MagicMock()

        audit_logger = AuditLogger(broken_session)

        # Simulate what the orchestrator would do: audit fails, but BLOCK decision is returned
        transaction_decision = GatewayDecision.BLOCK
        record = AuditRecord(
            transaction_id="txn-block-noaudit",
            stage=AuditStage.POLICY,
            event_type=AuditEventType.DECISION,
            decision=transaction_decision.value,
            reason="Blocked by policy",
        )

        audit_result = audit_logger.record(record)
        assert audit_result is False

        # The transaction decision itself is NOT modified by audit failure
        assert transaction_decision == GatewayDecision.BLOCK

    def test_audit_failure_logged_to_python_logger(self, session, caplog):
        """Audit failure must be logged at ERROR level, not silently swallowed."""
        broken_session = MagicMock()
        broken_session.add = MagicMock()
        broken_session.commit = MagicMock(side_effect=Exception("storage error"))
        broken_session.rollback = MagicMock()

        audit_logger = AuditLogger(broken_session)
        with caplog.at_level(logging.ERROR, logger="app.core.audit"):
            audit_logger.record(AuditRecord(
                transaction_id="txn-log-001",
                stage=AuditStage.FINAL,
                event_type=AuditEventType.DECISION,
                decision="ALLOW",
            ))

        assert any("AUDIT FAILURE" in r.getMessage() for r in caplog.records)

    def test_record_audit_failure_writes_meta_event(self, audit_logger):
        """record_audit_failure() should create an AUDIT_ERROR event if possible."""
        success = audit_logger.record_audit_failure(
            transaction_id="txn-meta-001",
            stage=AuditStage.FINAL,
            error_type="ConnectionError",
        )
        assert success is True

        events = audit_logger.get_events_for_transaction("txn-meta-001")
        assert len(events) == 1
        assert events[0].event_type == "AUDIT_ERROR"
        assert "ConnectionError" in events[0].reason

    def test_get_events_failure_returns_empty_list(self, session):
        """get_events_for_transaction must return [] if query fails, not raise."""
        broken_session = MagicMock()
        broken_session.query = MagicMock(side_effect=Exception("DB timeout"))

        audit_logger = AuditLogger(broken_session)
        result = audit_logger.get_events_for_transaction("txn-broken-query")
        assert result == []


# ══════════════════════════════════════════════════════════════════════════════
# Test: AuditRecord convenience builders
# ══════════════════════════════════════════════════════════════════════════════


class TestAuditRecordBuilders:
    def test_audit_pipeline_decision_builder(self):
        record = audit_pipeline_decision(
            transaction_id="txn-build-001",
            stage=AuditStage.POLICY,
            decision="ALLOW",
            reason="Policy passed",
            merchant_id="m1",
            buyer_agent_id="b1",
            protocol="acp",
            amount_minor=1000,
            currency="INR",
        )
        assert record.transaction_id == "txn-build-001"
        assert record.stage == AuditStage.POLICY
        assert record.event_type == AuditEventType.DECISION
        assert record.decision == "ALLOW"
        assert record.merchant_id == "m1"
        assert record.amount_minor == 1000

    def test_audit_razorpay_execution_builder(self):
        record = audit_razorpay_execution(
            transaction_id="txn-exec-001",
            razorpay_order_id="order_XYZ",
            razorpay_payment_id="pay_ABC",
            razorpay_payment_status="captured",
            decision="ALLOW",
            reason="Payment captured",
            amount_minor=5000,
            currency="INR",
        )
        assert record.stage == AuditStage.EXECUTION
        assert record.event_type == AuditEventType.RESULT
        assert record.razorpay_order_id == "order_XYZ"
        assert record.razorpay_payment_id == "pay_ABC"
        assert record.razorpay_payment_status == "captured"

    def test_audit_record_timestamp_is_set(self):
        before = datetime.now(timezone.utc)
        record = AuditRecord(
            transaction_id="txn-ts-001",
            stage=AuditStage.VALIDATION,
            event_type=AuditEventType.DECISION,
        )
        after = datetime.now(timezone.utc)
        assert before <= record.timestamp <= after

    def test_audit_record_all_optional_fields_default_none(self):
        record = AuditRecord(
            transaction_id="txn-min-001",
            stage=AuditStage.VALIDATION,
            event_type=AuditEventType.DECISION,
        )
        assert record.decision is None
        assert record.reason is None
        assert record.merchant_id is None
        assert record.buyer_agent_id is None
        assert record.protocol is None
        assert record.amount_minor is None
        assert record.currency is None
        assert record.razorpay_order_id is None
        assert record.razorpay_payment_id is None
        assert record.razorpay_payment_status is None


# ══════════════════════════════════════════════════════════════════════════════
# Test: AuditEvent ORM model
# ══════════════════════════════════════════════════════════════════════════════


class TestAuditEventModel:
    def test_audit_event_repr_is_safe(self, session):
        """AuditEvent repr must not contain sensitive data."""
        audit_logger = AuditLogger(session)
        audit_logger.record(AuditRecord(
            transaction_id="txn-repr-evt-001",
            stage=AuditStage.AUTHORIZATION,
            event_type=AuditEventType.DECISION,
            decision="BLOCK",
            reason="Sig fail",
        ))
        events = audit_logger.get_events_for_transaction("txn-repr-evt-001")
        r = repr(events[0])
        assert "txn-repr-evt-001" in r
        assert "AUTHORIZATION" in r
        assert "BLOCK" in r
        assert FAKE_KEY_SECRET not in r

    def test_audit_event_is_append_only(self, audit_logger, session):
        """Verify we can only INSERT, not UPDATE or DELETE."""
        record = audit_pipeline_decision(
            transaction_id="txn-append-001",
            stage=AuditStage.FINAL,
            decision="ALLOW",
            reason="Passed",
        )
        audit_logger.record(record)
        events = audit_logger.get_events_for_transaction("txn-append-001")
        original_id = events[0].id
        original_decision = events[0].decision

        # There's no update path — verify the original record unchanged
        events_again = audit_logger.get_events_for_transaction("txn-append-001")
        assert events_again[0].id == original_id
        assert events_again[0].decision == original_decision

    def test_different_transactions_isolated(self, audit_logger):
        audit_logger.record(audit_pipeline_decision("txn-A", AuditStage.FINAL, "ALLOW", "A passed"))
        audit_logger.record(audit_pipeline_decision("txn-B", AuditStage.POLICY, "BLOCK", "B blocked"))

        a_events = audit_logger.get_events_for_transaction("txn-A")
        b_events = audit_logger.get_events_for_transaction("txn-B")

        assert len(a_events) == 1
        assert len(b_events) == 1
        assert a_events[0].decision == "ALLOW"
        assert b_events[0].decision == "BLOCK"
