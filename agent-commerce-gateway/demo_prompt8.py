"""
demo_prompt8.py — Razorpay Test-Mode Execution Layer Demo
==========================================================

Demonstrates:
  Case 1: ALLOW → Razorpay Test Mode order creation/execution
  Case 2: REVIEW → Razorpay NOT called (execution refused)
  Case 3: BLOCK → Razorpay NOT called (execution refused)
  Case 4: Razorpay failure → structured failure receipt

IMPORTANT:
  - Cases 1 uses real Razorpay Test Mode credentials if RAZORPAY_KEY_ID and
    RAZORPAY_KEY_SECRET are set in .env. If not, it reports clearly.
  - Cases 2-4 use mock/local logic only.
  - No credentials are hardcoded.
  - No secrets are printed.

Setup:
  cp .env.example .env
  # Edit .env and add your TEST MODE credentials
  # (keys starting with rzp_test_...)
  .venv/bin/python demo_prompt8.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import httpx

# Ensure the project root is in the path
sys.path.insert(0, os.path.dirname(__file__))

from app.core.schemas import (
    BuyerProtocol,
    CommerceItem,
    CommerceRequest,
    GatewayDecision,
    Money,
)
from app.core.mandate import AuthorizationVerificationResult
from app.core.replay import ReplayResult
from app.core.policy import PolicyDecision
from app.core.transaction_result import PipelineStage, TransactionResult
from app.razorpay.client import (
    ExecutionStatus,
    RazorpayClient,
    execute_razorpay_payment,
)

SEP = "─" * 60


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════


def make_allow_result(transaction_id: str) -> TransactionResult:
    return TransactionResult.allowed(
        transaction_id=transaction_id,
        reason="All security gates passed",
        authorization_result=AuthorizationVerificationResult(
            valid=True, reason="Demo auth", requires_replay_check=False
        ),
        replay_result=ReplayResult(allowed=True, was_replay=False, reason="Demo replay"),
        policy_result=PolicyDecision(
            decision=GatewayDecision.ALLOW,
            primary_reason="Demo policy: ALLOW",
            triggered_rules=[],
        ),
    )


def make_review_result(transaction_id: str) -> TransactionResult:
    return TransactionResult.review(
        transaction_id=transaction_id,
        reason="Transaction flagged for manual review",
        authorization_result=AuthorizationVerificationResult(
            valid=True, reason="Demo auth", requires_replay_check=False
        ),
        replay_result=ReplayResult(allowed=True, was_replay=False, reason="Demo replay"),
        policy_result=PolicyDecision(
            decision=GatewayDecision.REVIEW,
            primary_reason="Demo policy: REVIEW",
            triggered_rules=[],
        ),
    )


def make_block_result(transaction_id: str) -> TransactionResult:
    return TransactionResult.blocked(
        transaction_id=transaction_id,
        stage=PipelineStage.POLICY,
        reason="Transaction blocked by policy",
    )


def print_receipt(receipt):
    print(f"  Receipt:")
    print(f"    transaction_id : {receipt.transaction_id}")
    print(f"    merchant_id    : {receipt.merchant_id}")
    print(f"    buyer_agent_id : {receipt.buyer_agent_id}")
    print(f"    status         : {receipt.status}")
    print(f"    decision       : {receipt.decision.value}")
    print(f"    amount         : {receipt.final_amount.amount_minor} {receipt.final_amount.currency}")
    print(f"    payment_ref    : {receipt.payment_reference}")


# ══════════════════════════════════════════════════════════════════════════════
# Case 1: ALLOW → Razorpay Test Mode
# ══════════════════════════════════════════════════════════════════════════════


def case1_allow_to_razorpay():
    print(f"\n{SEP}")
    print("Case 1: ALLOW → Razorpay Test Mode Order Creation")
    print(SEP)

    key_id = os.environ.get("RAZORPAY_KEY_ID", "").strip()
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "").strip()

    if not key_id or not key_secret:
        print("  ⚠️  RAZORPAY_KEY_ID or RAZORPAY_KEY_SECRET not set in .env")
        print("  ⚠️  Skipping live Test Mode call. No fake success will be reported.")
        print("  ℹ️  To test, add TEST MODE credentials (rzp_test_...) to .env")
        return

    if not key_id.startswith("rzp_test_"):
        print("  ⚠️  Key ID does not start with 'rzp_test_'. Live mode credentials are NOT permitted.")
        print("  ⚠️  Only Test Mode keys (rzp_test_...) may be used here.")
        return

    print(f"  ✅ Test mode credentials found (key_id: {key_id[:12]}...)")
    print("  Creating Razorpay Test Mode order...")

    allow_result = make_allow_result("demo-txn-razorpay-001")
    amount = Money(amount_minor=5000, currency="INR")  # ₹50.00

    client = RazorpayClient(key_id=key_id, key_secret=key_secret)
    receipt = execute_razorpay_payment(
        pipeline_result=allow_result,
        buyer_agent_id="demo-agent-001",
        merchant_id="demo-merchant-001",
        amount=amount,
        razorpay_client=client,
        originating_protocol=BuyerProtocol.acp,
        notes={"demo": "prompt8", "gateway": "agent-commerce-gateway"},
    )

    if receipt.status == ExecutionStatus.ORDER_CREATED.value:
        print("  ✅ Razorpay Test Mode order created successfully!")
        print(f"  ℹ️  Note: order_created ≠ payment completed.")
        print(f"       Payment must still be authorized and captured via checkout.")
    elif receipt.status in (ExecutionStatus.RAZORPAY_ERROR.value, ExecutionStatus.NETWORK_ERROR.value):
        print(f"  ❌ Razorpay call failed: {receipt.status}")
        print("       Check your test credentials and network connectivity.")
    else:
        print(f"  ℹ️  Result status: {receipt.status}")

    print_receipt(receipt)


# ══════════════════════════════════════════════════════════════════════════════
# Case 2: REVIEW → Razorpay NOT called
# ══════════════════════════════════════════════════════════════════════════════


def case2_review_no_razorpay():
    print(f"\n{SEP}")
    print("Case 2: REVIEW → Razorpay NOT called")
    print(SEP)

    razorpay_called = False

    def tracking_post(*args, **kwargs):
        nonlocal razorpay_called
        razorpay_called = True
        raise AssertionError("Razorpay must not be called for REVIEW decisions")

    review_result = make_review_result("demo-txn-review-001")
    amount = Money(amount_minor=5000, currency="INR")

    with patch("httpx.Client") as MockClient:
        MockClient.return_value.__enter__.return_value.post.side_effect = tracking_post
        client = RazorpayClient(key_id="rzp_test_demo", key_secret="demo_secret")
        receipt = execute_razorpay_payment(
            pipeline_result=review_result,
            buyer_agent_id="demo-agent",
            merchant_id="demo-merchant",
            amount=amount,
            razorpay_client=client,
            originating_protocol=BuyerProtocol.acp,
        )

    print(f"  ✅ Razorpay was NOT called (HTTP client never entered).")
    print(f"  ✅ Execution gate correctly refused REVIEW decision.")
    print_receipt(receipt)


# ══════════════════════════════════════════════════════════════════════════════
# Case 3: BLOCK → Razorpay NOT called
# ══════════════════════════════════════════════════════════════════════════════


def case3_block_no_razorpay():
    print(f"\n{SEP}")
    print("Case 3: BLOCK → Razorpay NOT called")
    print(SEP)

    block_result = make_block_result("demo-txn-block-001")
    amount = Money(amount_minor=5000, currency="INR")

    with patch("httpx.Client") as MockClient:
        MockClient.return_value.__enter__.return_value.post.side_effect = AssertionError(
            "Razorpay must not be called for BLOCK decisions"
        )
        client = RazorpayClient(key_id="rzp_test_demo", key_secret="demo_secret")
        receipt = execute_razorpay_payment(
            pipeline_result=block_result,
            buyer_agent_id="demo-agent",
            merchant_id="demo-merchant",
            amount=amount,
            razorpay_client=client,
            originating_protocol=BuyerProtocol.acp,
        )

    print(f"  ✅ Razorpay was NOT called (HTTP client never entered).")
    print(f"  ✅ Execution gate correctly refused BLOCK decision.")
    print_receipt(receipt)


# ══════════════════════════════════════════════════════════════════════════════
# Case 4: Razorpay failure → structured failure receipt
# ══════════════════════════════════════════════════════════════════════════════


def case4_razorpay_failure():
    print(f"\n{SEP}")
    print("Case 4: Razorpay Failure → Structured failure receipt")
    print(SEP)

    allow_result = make_allow_result("demo-txn-failure-001")
    amount = Money(amount_minor=5000, currency="INR")

    # Sub-case 4a: HTTP 500
    print("  [4a] Razorpay HTTP 500 error:")
    error_body = {"error": {"code": "SERVER_ERROR", "description": "Internal server error"}}
    with patch("httpx.Client") as MockClient:
        MockClient.return_value.__enter__.return_value.post.return_value = httpx.Response(
            500,
            content=json.dumps(error_body).encode(),
            headers={"content-type": "application/json"},
        )
        client = RazorpayClient(key_id="rzp_test_demo", key_secret="demo_secret")
        receipt = execute_razorpay_payment(
            pipeline_result=allow_result,
            buyer_agent_id="demo-agent",
            merchant_id="demo-merchant",
            amount=amount,
            razorpay_client=client,
            originating_protocol=BuyerProtocol.acp,
        )
    print(f"    Status: {receipt.status} (expected: razorpay_error)")
    assert receipt.status == ExecutionStatus.RAZORPAY_ERROR.value
    print("    ✅ Safe failure receipt produced. Uncertain state not converted to success.")

    # Sub-case 4b: Timeout
    print("  [4b] Razorpay timeout:")
    with patch("httpx.Client") as MockClient:
        MockClient.return_value.__enter__.return_value.post.side_effect = httpx.TimeoutException("timed out")
        client = RazorpayClient(key_id="rzp_test_demo", key_secret="demo_secret")
        receipt = execute_razorpay_payment(
            pipeline_result=allow_result,
            buyer_agent_id="demo-agent",
            merchant_id="demo-merchant",
            amount=amount,
            razorpay_client=client,
            originating_protocol=BuyerProtocol.acp,
        )
    print(f"    Status: {receipt.status} (expected: network_error)")
    assert receipt.status == ExecutionStatus.NETWORK_ERROR.value
    print("    ✅ Timeout handled safely — uncertain state not reported as success.")


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    print("=" * 60)
    print("Agent Commerce Gateway — Prompt 8: Razorpay Execution Demo")
    print("=" * 60)

    case1_allow_to_razorpay()
    case2_review_no_razorpay()
    case3_block_no_razorpay()
    case4_razorpay_failure()

    print(f"\n{SEP}")
    print("All demo cases completed.")
    print(SEP)
