"""
demo_prompt9.py — Audit Trail + Razorpay Payment Lifecycle Demo
================================================================

Demonstrates all 5 required cases:

  Case 1: BLOCK → audit recorded → Razorpay NOT called
  Case 2: REVIEW → audit recorded → Razorpay NOT called
  Case 3: ALLOW → Razorpay order created → audit records execution
  Case 4: Razorpay failure → structured failure → audit records failure → no false SUCCESS
  Case 5: Payment lifecycle boundary — documents what backend vs. client does

What requires browser/checkout interaction:
  After POST /v1/orders, the Razorpay backend cannot proceed further on its own.
  Payment collection requires:
    1. Client-side Razorpay Checkout (JS widget or mobile SDK)
    2. Client provides: razorpay_order_id, razorpay_payment_id, razorpay_signature
    3. Backend calls verify_payment_and_capture() with those values
    4. Only if HMAC-SHA256 signature is valid AND Razorpay reports status='captured'
       does the gateway report payment_captured (SUCCESS).

  This demo shows verify_payment_and_capture() working with both:
    - Invalid signature (rejected, no Razorpay capture call)
    - Valid signature + auto_captured=True (simulates Razorpay auto-capture mode)
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from unittest.mock import patch

import httpx

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

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
from app.core.mandate import AuthorizationVerificationResult
from app.core.policy import PolicyDecision
from app.core.replay import ReplayResult
from app.core.schemas import BuyerProtocol, GatewayDecision, Money
from app.core.transaction_result import PipelineStage, TransactionResult
from app.db.database import Base, init_db
from app.razorpay.client import (
    ExecutionStatus,
    RazorpayClient,
    execute_razorpay_payment,
    verify_payment_and_capture,
)
from tests.test_razorpay_lifecycle import _make_valid_signature

SEP = "─" * 65


# ══════════════════════════════════════════════════════════════════════════════
# Setup: in-memory DB for audit trail
# ══════════════════════════════════════════════════════════════════════════════


def make_demo_audit() -> AuditLogger:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    init_db(engine)
    session = sessionmaker(bind=engine)()
    return AuditLogger(session)


def make_allow_result(txn_id: str) -> TransactionResult:
    return TransactionResult.allowed(
        transaction_id=txn_id,
        reason="All gates passed",
        authorization_result=AuthorizationVerificationResult(
            valid=True, reason="Demo auth", requires_replay_check=False
        ),
        replay_result=ReplayResult(allowed=True, was_replay=False, reason="demo"),
        policy_result=PolicyDecision(
            decision=GatewayDecision.ALLOW,
            primary_reason="Demo ALLOW",
            triggered_rules=[],
        ),
    )


def make_block_result(txn_id: str) -> TransactionResult:
    return TransactionResult.blocked(
        transaction_id=txn_id,
        stage=PipelineStage.POLICY,
        reason="Demo policy: transaction blocked",
    )


def make_review_result(txn_id: str) -> TransactionResult:
    return TransactionResult.review(
        transaction_id=txn_id,
        reason="Demo policy: flagged for review",
        authorization_result=AuthorizationVerificationResult(
            valid=True, reason="demo", requires_replay_check=False
        ),
        replay_result=ReplayResult(allowed=True, was_replay=False, reason="demo"),
        policy_result=PolicyDecision(
            decision=GatewayDecision.REVIEW,
            primary_reason="Demo REVIEW",
            triggered_rules=[],
        ),
    )


def print_audit_trail(audit: AuditLogger, txn_id: str):
    events = audit.get_events_for_transaction(txn_id)
    print(f"  Audit trail ({len(events)} event(s)):")
    for evt in events:
        print(f"    [{evt.stage}] {evt.event_type} decision={evt.decision} reason={evt.reason!r}")
        if evt.razorpay_order_id:
            print(f"             razorpay_order_id={evt.razorpay_order_id}")
        if evt.razorpay_payment_id:
            print(f"             razorpay_payment_id={evt.razorpay_payment_id}")


# ══════════════════════════════════════════════════════════════════════════════
# Case 1: BLOCK → audit → Razorpay NOT called
# ══════════════════════════════════════════════════════════════════════════════


def case1_block_with_audit():
    print(f"\n{SEP}")
    print("Case 1: BLOCK → audit recorded → Razorpay NOT called")
    print(SEP)

    audit = make_demo_audit()
    txn_id = "demo9-block-001"
    block_result = make_block_result(txn_id)
    amount = Money(amount_minor=5000, currency="INR")

    # Record audit event
    audit.record(audit_pipeline_decision(
        transaction_id=txn_id,
        stage=AuditStage.POLICY,
        decision="BLOCK",
        reason="Demo policy: transaction blocked",
        merchant_id="demo-merchant",
        buyer_agent_id="demo-buyer",
        protocol="acp",
        amount_minor=5000,
        currency="INR",
    ))

    # Gate: Razorpay must NOT be called
    razorpay_called = False
    with patch("httpx.Client") as MockClient:
        MockClient.return_value.__enter__.return_value.post.side_effect = (
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("Razorpay must not be called"))
        )
        client = RazorpayClient(key_id="rzp_test_demo", key_secret="demo_secret")
        receipt = execute_razorpay_payment(
            pipeline_result=block_result,
            buyer_agent_id="demo-buyer",
            merchant_id="demo-merchant",
            amount=amount,
            razorpay_client=client,
            originating_protocol=BuyerProtocol.acp,
        )

    print(f"  ✅ Razorpay NOT called for BLOCK decision.")
    print(f"  Receipt status: {receipt.status}")
    print_audit_trail(audit, txn_id)
    assert receipt.status == ExecutionStatus.EXECUTION_REFUSED.value


# ══════════════════════════════════════════════════════════════════════════════
# Case 2: REVIEW → audit → Razorpay NOT called
# ══════════════════════════════════════════════════════════════════════════════


def case2_review_with_audit():
    print(f"\n{SEP}")
    print("Case 2: REVIEW → audit recorded → Razorpay NOT called")
    print(SEP)

    audit = make_demo_audit()
    txn_id = "demo9-review-001"
    review_result = make_review_result(txn_id)
    amount = Money(amount_minor=10000, currency="INR")

    audit.record(audit_pipeline_decision(
        transaction_id=txn_id,
        stage=AuditStage.FINAL,
        decision="REVIEW",
        reason="Demo: high-value transaction flagged for review",
        merchant_id="demo-merchant",
        buyer_agent_id="demo-buyer",
        protocol="x402",
        amount_minor=10000,
        currency="INR",
    ))

    with patch("httpx.Client") as MockClient:
        client = RazorpayClient(key_id="rzp_test_demo", key_secret="demo_secret")
        receipt = execute_razorpay_payment(
            pipeline_result=review_result,
            buyer_agent_id="demo-buyer",
            merchant_id="demo-merchant",
            amount=amount,
            razorpay_client=client,
            originating_protocol=BuyerProtocol.x402,
        )
        MockClient.return_value.__enter__.assert_not_called()

    print(f"  ✅ Razorpay NOT called for REVIEW decision.")
    print(f"  Receipt status: {receipt.status}")
    print_audit_trail(audit, txn_id)


# ══════════════════════════════════════════════════════════════════════════════
# Case 3: ALLOW → Razorpay order created → audit records execution
# ══════════════════════════════════════════════════════════════════════════════


def case3_allow_order_created():
    print(f"\n{SEP}")
    print("Case 3: ALLOW → Razorpay order created → audit records execution")
    print(SEP)

    key_id = os.environ.get("RAZORPAY_KEY_ID", "").strip()
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "").strip()

    audit = make_demo_audit()
    txn_id = "demo9-allow-001"
    allow_result = make_allow_result(txn_id)
    amount = Money(amount_minor=5000, currency="INR")

    # Audit the pipeline ALLOW decision
    audit.record(audit_pipeline_decision(
        transaction_id=txn_id,
        stage=AuditStage.FINAL,
        decision="ALLOW",
        reason="All security gates passed",
        merchant_id="demo-merchant",
        buyer_agent_id="demo-buyer",
        protocol="acp",
        amount_minor=5000,
        currency="INR",
    ))

    if not key_id or not key_secret or not key_id.startswith("rzp_test_"):
        print("  ⚠️  TEST MODE credentials not set/invalid — mocking Razorpay response.")
        mock_order_body = {
            "id": "order_DEMO9MOCK",
            "status": "created",
            "amount": 5000,
            "currency": "INR",
            "receipt": txn_id[:40],
        }
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.return_value = httpx.Response(
                200,
                content=json.dumps(mock_order_body).encode(),
                headers={"content-type": "application/json"},
            )
            client = RazorpayClient(key_id="rzp_test_mock", key_secret="mock_secret")
            receipt = execute_razorpay_payment(
                pipeline_result=allow_result,
                buyer_agent_id="demo-buyer",
                merchant_id="demo-merchant",
                amount=amount,
                razorpay_client=client,
                originating_protocol=BuyerProtocol.acp,
            )
    else:
        print(f"  ✅ Live Test Mode credentials found (key_id: {key_id[:12]}...)")
        client = RazorpayClient(key_id=key_id, key_secret=key_secret)
        receipt = execute_razorpay_payment(
            pipeline_result=allow_result,
            buyer_agent_id="demo-buyer",
            merchant_id="demo-merchant",
            amount=amount,
            razorpay_client=client,
            originating_protocol=BuyerProtocol.acp,
            notes={"demo": "prompt9"},
        )

    # Audit the execution result
    audit.record(audit_razorpay_execution(
        transaction_id=txn_id,
        razorpay_order_id=receipt.payment_reference,
        razorpay_payment_id=None,
        razorpay_payment_status=None,
        decision=receipt.decision.value,
        reason=f"Razorpay order creation: {receipt.status}",
        amount_minor=5000,
        currency="INR",
    ))

    print(f"  Receipt status: {receipt.status}")
    print(f"  Razorpay order ID: {receipt.payment_reference}")
    print(f"  ℹ️  NOTE: order_created ≠ payment. Payment requires client-side checkout.")
    print_audit_trail(audit, txn_id)
    assert receipt.status == ExecutionStatus.ORDER_CREATED.value


# ══════════════════════════════════════════════════════════════════════════════
# Case 4: Razorpay failure → structured failure → no false SUCCESS
# ══════════════════════════════════════════════════════════════════════════════


def case4_razorpay_failure_with_audit():
    print(f"\n{SEP}")
    print("Case 4: Razorpay failure → structured failure → no false SUCCESS")
    print(SEP)

    audit = make_demo_audit()
    txn_id = "demo9-failure-001"
    allow_result = make_allow_result(txn_id)
    amount = Money(amount_minor=5000, currency="INR")

    # Audit ALLOW decision
    audit.record(audit_pipeline_decision(
        transaction_id=txn_id, stage=AuditStage.FINAL,
        decision="ALLOW", reason="All gates passed",
    ))

    error_body = {"error": {"code": "SERVER_ERROR", "description": "Internal server error"}}
    with patch("httpx.Client") as MockClient:
        MockClient.return_value.__enter__.return_value.post.return_value = httpx.Response(
            500, content=json.dumps(error_body).encode(),
            headers={"content-type": "application/json"},
        )
        client = RazorpayClient(key_id="rzp_test_mock", key_secret="mock_secret")
        receipt = execute_razorpay_payment(
            pipeline_result=allow_result,
            buyer_agent_id="demo-buyer",
            merchant_id="demo-merchant",
            amount=amount,
            razorpay_client=client,
            originating_protocol=BuyerProtocol.acp,
        )

    # Audit the failure
    audit.record(audit_razorpay_execution(
        transaction_id=txn_id,
        razorpay_order_id=None,
        razorpay_payment_id=None,
        razorpay_payment_status=None,
        decision="BLOCK",
        reason=f"Razorpay call failed: {receipt.status}",
    ))

    print(f"  Receipt status: {receipt.status}  (NOT payment_captured)")
    assert receipt.status == ExecutionStatus.RAZORPAY_ERROR.value
    assert receipt.status != ExecutionStatus.PAYMENT_CAPTURED.value
    print(f"  ✅ No false SUCCESS — failure is correctly labeled {receipt.status!r}")
    print_audit_trail(audit, txn_id)


# ══════════════════════════════════════════════════════════════════════════════
# Case 5: Payment lifecycle boundary — backend vs. client
# ══════════════════════════════════════════════════════════════════════════════


def case5_payment_lifecycle_boundary():
    print(f"\n{SEP}")
    print("Case 5: Payment Lifecycle Boundary")
    print(SEP)
    print()
    print("  BACKEND (this gateway) can perform:")
    print("    1. POST /v1/orders  → creates order with amount/currency/receipt")
    print("       Response: { id, status='created', amount, currency, receipt }")
    print()
    print("    2. verify_payment_signature(order_id, payment_id, signature)")
    print("       HMAC-SHA256 server-side verification using key_secret")
    print("       Fail-closed on mismatch")
    print()
    print("    3. POST /v1/payments/{id}/capture  → captures authorized payment")
    print("       Only after valid signature + status='authorized'")
    print("       Body: {amount, currency} — must exactly match approved total")
    print()
    print("  REQUIRES BROWSER/CLIENT (cannot be performed backend-only):")
    print("    - Razorpay Checkout JS widget / mobile SDK")
    print("    - Customer enters payment details (card, UPI, netbanking)")
    print("    - Razorpay processes payment authorization")
    print("    - Checkout callback returns: razorpay_order_id, razorpay_payment_id,")
    print("      razorpay_signature")
    print()
    print("  EVIDENCE REQUIRED to mark PAYMENT_CAPTURED:")
    print("    1. razorpay_signature must match HMAC-SHA256(order_id|payment_id, key_secret)")
    print("    2. POST /v1/payments/{id}/capture must return status='captured'")
    print("    3. Returned amount and currency must exactly match approved transaction")
    print("    4. Returned order_id must match the gateway's own order_id (not client-supplied)")
    print()
    print("  DEMONSTRATING: verify_payment_and_capture() with valid signature + auto_captured")
    print()

    audit = make_demo_audit()
    client = RazorpayClient(key_id="rzp_test_demo", key_secret="demo_secret")

    order_id = "order_DEMO9"
    payment_id = "pay_DEMO9"
    valid_sig = _make_valid_signature(order_id, payment_id, secret="demo_secret")

    result = verify_payment_and_capture(
        razorpay_client=client,
        expected_order_id=order_id,
        razorpay_payment_id=payment_id,
        razorpay_signature=valid_sig,
        amount_minor=5000,
        currency="INR",
        auto_captured=True,
    )

    audit.record(audit_razorpay_execution(
        transaction_id="demo9-lifecycle-001",
        razorpay_order_id=result.razorpay_order_id,
        razorpay_payment_id=result.razorpay_payment_id,
        razorpay_payment_status=result.razorpay_payment_status,
        decision="ALLOW" if result.is_success() else "BLOCK",
        reason=f"Payment verification: {result.execution_status.value}",
        amount_minor=5000,
        currency="INR",
    ))

    print(f"  verify_payment_and_capture() result: {result.execution_status.value}")
    print(f"  is_success(): {result.is_success()}")
    print_audit_trail(audit, "demo9-lifecycle-001")

    print()
    print("  NOW: with INVALID signature (rejected):")
    bad_result = verify_payment_and_capture(
        razorpay_client=client,
        expected_order_id=order_id,
        razorpay_payment_id=payment_id,
        razorpay_signature="completely_invalid_signature",
        amount_minor=5000,
        currency="INR",
        auto_captured=False,
    )
    print(f"  Result with bad signature: {bad_result.execution_status.value}")
    print(f"  is_success(): {bad_result.is_success()}")
    assert bad_result.execution_status == ExecutionStatus.SIGNATURE_INVALID
    print(f"  ✅ Invalid signature correctly rejected — no capture attempted, no false SUCCESS.")


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════


if __name__ == "__main__":
    print("=" * 65)
    print("Agent Commerce Gateway — Prompt 9: Audit + Payment Lifecycle")
    print("=" * 65)

    case1_block_with_audit()
    case2_review_with_audit()
    case3_allow_order_created()
    case4_razorpay_failure_with_audit()
    case5_payment_lifecycle_boundary()

    print(f"\n{SEP}")
    print("All 5 demo cases completed.")
    print(SEP)
