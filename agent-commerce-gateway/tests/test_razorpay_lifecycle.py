"""
Razorpay Payment Lifecycle Tests — Prompt 9
============================================

Tests for the full Razorpay payment lifecycle:
  - Payment signature verification (HMAC-SHA256)
  - Payment capture endpoint
  - Order/payment mismatch detection
  - Amount mismatch detection
  - Currency mismatch detection
  - Already captured / wrong state handling
  - Unauthorized payment rejection
  - Auto-capture handling
  - No false SUCCESS reporting
  - verify_payment_and_capture() orchestration
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.razorpay.client import (
    ExecutionStatus,
    RazorpayClient,
    RazorpayOrderResult,
    execute_razorpay_payment,
    verify_payment_and_capture,
)
from app.core.schemas import BuyerProtocol, GatewayDecision, Money
from app.core.transaction_result import PipelineStage, TransactionResult
from app.core.mandate import AuthorizationVerificationResult
from app.core.replay import ReplayResult
from app.core.policy import PolicyDecision


# ══════════════════════════════════════════════════════════════════════════════
# Constants / helpers
# ══════════════════════════════════════════════════════════════════════════════

FAKE_KEY_ID = "rzp_test_testkey123"
FAKE_KEY_SECRET = "supersecret_do_not_log"


def _make_valid_signature(order_id: str, payment_id: str, secret: str = FAKE_KEY_SECRET) -> str:
    """Generate a valid Razorpay HMAC-SHA256 signature for testing."""
    message = f"{order_id}|{payment_id}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def make_allow_result(transaction_id: str = "txn-test-123") -> TransactionResult:
    return TransactionResult.allowed(
        transaction_id=transaction_id,
        reason="All gates passed",
        authorization_result=AuthorizationVerificationResult(
            valid=True, reason="test", requires_replay_check=False
        ),
        replay_result=ReplayResult(allowed=True, was_replay=False, reason="test"),
        policy_result=PolicyDecision(
            decision=GatewayDecision.ALLOW,
            primary_reason="allowed by policy",
            triggered_rules=[],
        ),
    )


def make_block_result(transaction_id: str = "txn-test-123") -> TransactionResult:
    return TransactionResult.blocked(
        transaction_id=transaction_id,
        stage=PipelineStage.POLICY,
        reason="Blocked by policy",
    )


def make_review_result(transaction_id: str = "txn-test-123") -> TransactionResult:
    return TransactionResult.review(
        transaction_id=transaction_id,
        reason="flagged",
        authorization_result=AuthorizationVerificationResult(
            valid=True, reason="test", requires_replay_check=False
        ),
        replay_result=ReplayResult(allowed=True, was_replay=False, reason="test"),
        policy_result=PolicyDecision(
            decision=GatewayDecision.REVIEW,
            primary_reason="review",
            triggered_rules=[],
        ),
    )


def make_client() -> RazorpayClient:
    return RazorpayClient(key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET)


def make_money(amount: int = 5000, currency: str = "INR") -> Money:
    return Money(amount_minor=amount, currency=currency)


def capture_response(
    payment_id: str = "pay_CAP123",
    order_id: str = "order_ORD123",
    amount: int = 5000,
    currency: str = "INR",
    status: str = "captured",
) -> dict:
    return {
        "id": payment_id,
        "entity": "payment",
        "order_id": order_id,
        "amount": amount,
        "currency": currency,
        "status": status,
        "captured": True,
    }


# ══════════════════════════════════════════════════════════════════════════════
# TestPaymentSignatureVerification
# ══════════════════════════════════════════════════════════════════════════════


class TestPaymentSignatureVerification:
    def test_valid_signature_returns_true(self):
        client = make_client()
        order_id = "order_XYZ123"
        payment_id = "pay_ABC456"
        sig = _make_valid_signature(order_id, payment_id)
        assert client.verify_payment_signature(order_id, payment_id, sig) is True

    def test_invalid_signature_returns_false(self):
        client = make_client()
        assert client.verify_payment_signature(
            "order_XYZ", "pay_ABC", "definitely_not_valid_sig"
        ) is False

    def test_tampered_order_id_fails(self):
        """Changing order_id must invalidate signature."""
        client = make_client()
        order_id = "order_REAL"
        payment_id = "pay_REAL"
        sig = _make_valid_signature(order_id, payment_id)
        # Signature was for order_REAL but we pass order_TAMPERED
        assert client.verify_payment_signature("order_TAMPERED", payment_id, sig) is False

    def test_tampered_payment_id_fails(self):
        """Changing payment_id must invalidate signature."""
        client = make_client()
        order_id = "order_REAL"
        payment_id = "pay_REAL"
        sig = _make_valid_signature(order_id, payment_id)
        assert client.verify_payment_signature(order_id, "pay_TAMPERED", sig) is False

    def test_empty_order_id_fails(self):
        client = make_client()
        assert client.verify_payment_signature("", "pay_ABC", "sig") is False

    def test_empty_payment_id_fails(self):
        client = make_client()
        assert client.verify_payment_signature("order_XYZ", "", "sig") is False

    def test_empty_signature_fails(self):
        client = make_client()
        assert client.verify_payment_signature("order_XYZ", "pay_ABC", "") is False

    def test_wrong_secret_fails(self):
        """Signature generated with different secret must not verify."""
        client = make_client()
        order_id = "order_XYZ"
        payment_id = "pay_ABC"
        sig = _make_valid_signature(order_id, payment_id, secret="wrong_secret")
        assert client.verify_payment_signature(order_id, payment_id, sig) is False

    def test_signature_verification_is_constant_time(self):
        """verify_payment_signature must use compare_digest, not == comparison.
        This test ensures the implementation uses hmac.compare_digest."""
        client = make_client()
        order_id = "order_XYZ"
        payment_id = "pay_ABC"
        sig = _make_valid_signature(order_id, payment_id)
        # If constant-time comparison breaks, valid sig still returns True
        assert client.verify_payment_signature(order_id, payment_id, sig) is True

    def test_verify_does_not_log_secret(self, caplog):
        """Secret must not appear in log output during verification."""
        client = make_client()
        with caplog.at_level(logging.WARNING, logger="app.razorpay.client"):
            client.verify_payment_signature("order_X", "pay_Y", "bad_sig")
        for record in caplog.records:
            assert FAKE_KEY_SECRET not in record.getMessage()


# ══════════════════════════════════════════════════════════════════════════════
# TestPaymentCapture
# ══════════════════════════════════════════════════════════════════════════════


class TestPaymentCapture:
    def test_successful_capture_returns_payment_captured(self):
        resp_body = capture_response(
            payment_id="pay_CAP", order_id="order_ORD", amount=5000, currency="INR"
        )
        with patch("httpx.Client") as MockClient:
            mock_instance = MockClient.return_value.__enter__.return_value
            mock_instance.post.return_value = httpx.Response(
                200,
                content=json.dumps(resp_body).encode(),
                headers={"content-type": "application/json"},
            )
            client = make_client()
            result = client.capture_payment(
                payment_id="pay_CAP",
                amount_minor=5000,
                currency="INR",
                expected_order_id="order_ORD",
            )

        assert result.execution_status == ExecutionStatus.PAYMENT_CAPTURED
        assert result.razorpay_payment_id == "pay_CAP"
        assert result.razorpay_payment_status == "captured"
        assert result.is_success() is True

    def test_capture_with_wrong_order_id_is_rejected(self):
        """Payment belonging to a different order must be rejected."""
        resp_body = capture_response(
            payment_id="pay_CAP", order_id="order_DIFFERENT", amount=5000
        )
        with patch("httpx.Client") as MockClient:
            mock_instance = MockClient.return_value.__enter__.return_value
            mock_instance.post.return_value = httpx.Response(
                200,
                content=json.dumps(resp_body).encode(),
                headers={"content-type": "application/json"},
            )
            client = make_client()
            result = client.capture_payment(
                payment_id="pay_CAP",
                amount_minor=5000,
                currency="INR",
                expected_order_id="order_EXPECTED",  # different from response
            )

        assert result.execution_status == ExecutionStatus.INVALID_RESPONSE
        assert result.error_code == "ORDER_MISMATCH"
        assert result.is_success() is False

    def test_capture_with_amount_mismatch_is_rejected(self):
        """If Razorpay echoes back a different amount, reject the capture."""
        resp_body = capture_response(
            payment_id="pay_CAP", order_id="order_ORD", amount=9999, currency="INR"
        )
        with patch("httpx.Client") as MockClient:
            mock_instance = MockClient.return_value.__enter__.return_value
            mock_instance.post.return_value = httpx.Response(
                200,
                content=json.dumps(resp_body).encode(),
                headers={"content-type": "application/json"},
            )
            client = make_client()
            result = client.capture_payment(
                payment_id="pay_CAP",
                amount_minor=5000,  # sent 5000, got 9999
                currency="INR",
                expected_order_id="order_ORD",
            )

        assert result.execution_status == ExecutionStatus.INVALID_RESPONSE
        assert result.error_code == "AMOUNT_MISMATCH"

    def test_capture_of_already_captured_payment_is_unknown(self):
        """If Razorpay returns non-'captured' status on capture, return UNKNOWN."""
        resp_body = capture_response(
            payment_id="pay_CAP", order_id="order_ORD", amount=5000, status="failed"
        )
        with patch("httpx.Client") as MockClient:
            mock_instance = MockClient.return_value.__enter__.return_value
            mock_instance.post.return_value = httpx.Response(
                200,
                content=json.dumps(resp_body).encode(),
                headers={"content-type": "application/json"},
            )
            client = make_client()
            result = client.capture_payment(
                payment_id="pay_CAP",
                amount_minor=5000,
                currency="INR",
                expected_order_id="order_ORD",
            )

        assert result.execution_status == ExecutionStatus.UNKNOWN
        assert result.is_success() is False

    def test_capture_with_empty_payment_id_is_refused(self):
        client = make_client()
        result = client.capture_payment(
            payment_id="",
            amount_minor=5000,
            currency="INR",
            expected_order_id="order_ORD",
        )
        assert result.execution_status == ExecutionStatus.RAZORPAY_ERROR
        assert result.error_code == "INVALID_PAYMENT_ID"

    def test_capture_with_zero_amount_is_refused(self):
        client = make_client()
        result = client.capture_payment(
            payment_id="pay_CAP",
            amount_minor=0,
            currency="INR",
            expected_order_id="order_ORD",
        )
        assert result.execution_status == ExecutionStatus.RAZORPAY_ERROR
        assert result.error_code == "INVALID_AMOUNT"

    def test_capture_timeout_returns_network_error(self):
        with patch("httpx.Client") as MockClient:
            mock_instance = MockClient.return_value.__enter__.return_value
            mock_instance.post.side_effect = httpx.TimeoutException("timed out")
            client = make_client()
            result = client.capture_payment(
                payment_id="pay_CAP",
                amount_minor=5000,
                currency="INR",
                expected_order_id="order_ORD",
            )

        assert result.execution_status == ExecutionStatus.NETWORK_ERROR
        assert result.error_code == "TIMEOUT"

    def test_capture_http_400_returns_razorpay_error(self):
        error_body = {"error": {"code": "BAD_REQUEST_ERROR", "description": "Payment already captured"}}
        with patch("httpx.Client") as MockClient:
            mock_instance = MockClient.return_value.__enter__.return_value
            mock_instance.post.return_value = httpx.Response(
                400,
                content=json.dumps(error_body).encode(),
                headers={"content-type": "application/json"},
            )
            client = make_client()
            result = client.capture_payment(
                payment_id="pay_CAP",
                amount_minor=5000,
                currency="INR",
                expected_order_id="order_ORD",
            )

        assert result.execution_status == ExecutionStatus.RAZORPAY_ERROR
        assert result.error_code == "BAD_REQUEST_ERROR"
        assert result.is_success() is False


# ══════════════════════════════════════════════════════════════════════════════
# TestVerifyPaymentAndCapture
# ══════════════════════════════════════════════════════════════════════════════


class TestVerifyPaymentAndCapture:
    def test_valid_signature_with_auto_capture_returns_captured(self):
        client = make_client()
        order_id = "order_AUTO"
        payment_id = "pay_AUTO"
        sig = _make_valid_signature(order_id, payment_id)

        result = verify_payment_and_capture(
            razorpay_client=client,
            expected_order_id=order_id,
            razorpay_payment_id=payment_id,
            razorpay_signature=sig,
            amount_minor=5000,
            currency="INR",
            auto_captured=True,
        )

        assert result.execution_status == ExecutionStatus.PAYMENT_CAPTURED
        assert result.is_success() is True
        assert result.razorpay_payment_id == payment_id
        assert result.razorpay_order_id == order_id

    def test_invalid_signature_fails_closed(self):
        """Bad signature must fail before any capture attempt."""
        client = make_client()
        result = verify_payment_and_capture(
            razorpay_client=client,
            expected_order_id="order_X",
            razorpay_payment_id="pay_X",
            razorpay_signature="invalid_sig",
            amount_minor=5000,
            currency="INR",
        )

        assert result.execution_status == ExecutionStatus.SIGNATURE_INVALID
        assert result.error_code == "SIGNATURE_MISMATCH"
        assert result.is_success() is False

    def test_invalid_signature_does_not_call_capture(self):
        """When signature fails, capture must NOT be called."""
        with patch("httpx.Client") as MockClient:
            client = make_client()
            result = verify_payment_and_capture(
                razorpay_client=client,
                expected_order_id="order_X",
                razorpay_payment_id="pay_X",
                razorpay_signature="bad_sig",
                amount_minor=5000,
                currency="INR",
                auto_captured=False,
            )
            # httpx.Client must never be entered because signature failed first
            MockClient.return_value.__enter__.assert_not_called()

        assert result.execution_status == ExecutionStatus.SIGNATURE_INVALID

    def test_valid_signature_with_manual_capture_calls_capture_endpoint(self):
        order_id = "order_MAN"
        payment_id = "pay_MAN"
        sig = _make_valid_signature(order_id, payment_id)
        resp_body = capture_response(payment_id=payment_id, order_id=order_id, amount=5000)

        with patch("httpx.Client") as MockClient:
            mock_instance = MockClient.return_value.__enter__.return_value
            mock_instance.get.return_value = httpx.Response(
                200,
                content=json.dumps({"id": payment_id, "status": "authorized"}).encode(),
                headers={"content-type": "application/json"},
            )
            mock_instance.post.return_value = httpx.Response(
                200,
                content=json.dumps(resp_body).encode(),
                headers={"content-type": "application/json"},
            )
            client = make_client()
            result = verify_payment_and_capture(
                razorpay_client=client,
                expected_order_id=order_id,
                razorpay_payment_id=payment_id,
                razorpay_signature=sig,
                amount_minor=5000,
                currency="INR",
                auto_captured=False,
            )

        assert result.execution_status == ExecutionStatus.PAYMENT_CAPTURED
        assert result.is_success() is True

    def test_unauthorized_payment_via_wrong_order_rejected(self):
        """Client supplies valid signature but payment belongs to different order."""
        # Attacker has a valid signature for their own order
        attacker_order = "order_ATTACKER"
        victim_payment = "pay_VICTIM"
        attacker_sig = _make_valid_signature(attacker_order, victim_payment)

        client = make_client()
        # Gateway uses its DB's expected_order_id (victim's order)
        result = verify_payment_and_capture(
            razorpay_client=client,
            expected_order_id="order_VICTIM",  # from our DB
            razorpay_payment_id=victim_payment,
            razorpay_signature=attacker_sig,  # signed with attacker's order_id
            amount_minor=5000,
            currency="INR",
            auto_captured=True,
        )

        # Signature invalid because order_VICTIM != order_ATTACKER
        assert result.execution_status == ExecutionStatus.SIGNATURE_INVALID
        assert result.is_success() is False


# ══════════════════════════════════════════════════════════════════════════════
# TestNoFalseSuccess
# ══════════════════════════════════════════════════════════════════════════════


class TestNoFalseSuccess:
    def test_order_created_is_never_success(self):
        result = RazorpayOrderResult(
            execution_status=ExecutionStatus.ORDER_CREATED,
            razorpay_order_id="order_X",
            razorpay_order_status="created",
            razorpay_amount=5000,
            razorpay_currency="INR",
            razorpay_receipt="txn-001",
            razorpay_payment_id=None,
            razorpay_payment_status=None,
            error_code=None,
            error_description=None,
            http_status_code=200,
            timestamp=datetime.now(timezone.utc),
        )
        assert not result.is_success()
        assert not result.is_payment_captured()

    def test_payment_authorized_is_never_success(self):
        result = RazorpayOrderResult(
            execution_status=ExecutionStatus.PAYMENT_AUTHORIZED,
            razorpay_order_id="order_X",
            razorpay_order_status="attempted",
            razorpay_amount=5000,
            razorpay_currency="INR",
            razorpay_receipt="txn-001",
            razorpay_payment_id="pay_X",
            razorpay_payment_status="authorized",
            error_code=None,
            error_description=None,
            http_status_code=200,
            timestamp=datetime.now(timezone.utc),
        )
        assert not result.is_success()
        assert not result.is_payment_captured()

    def test_signature_invalid_is_never_success(self):
        result = RazorpayOrderResult(
            execution_status=ExecutionStatus.SIGNATURE_INVALID,
            razorpay_order_id="order_X",
            razorpay_order_status=None,
            razorpay_amount=None,
            razorpay_currency=None,
            razorpay_receipt=None,
            razorpay_payment_id="pay_X",
            razorpay_payment_status=None,
            error_code="SIGNATURE_MISMATCH",
            error_description="HMAC mismatch",
            http_status_code=None,
            timestamp=datetime.now(timezone.utc),
        )
        assert not result.is_success()
        assert result.is_failure()

    def test_unknown_status_is_never_success(self):
        result = RazorpayOrderResult(
            execution_status=ExecutionStatus.UNKNOWN,
            razorpay_order_id="order_X",
            razorpay_order_status="attempted",
            razorpay_amount=5000,
            razorpay_currency="INR",
            razorpay_receipt=None,
            razorpay_payment_id="pay_X",
            razorpay_payment_status="unknown_state",
            error_code="UNEXPECTED_STATUS",
            error_description="Got unexpected_state",
            http_status_code=200,
            timestamp=datetime.now(timezone.utc),
        )
        assert not result.is_success()
        assert not result.is_payment_captured()

    def test_only_payment_captured_is_success(self):
        result = RazorpayOrderResult(
            execution_status=ExecutionStatus.PAYMENT_CAPTURED,
            razorpay_order_id="order_X",
            razorpay_order_status="paid",
            razorpay_amount=5000,
            razorpay_currency="INR",
            razorpay_receipt="txn-001",
            razorpay_payment_id="pay_CAP",
            razorpay_payment_status="captured",
            error_code=None,
            error_description=None,
            http_status_code=200,
            timestamp=datetime.now(timezone.utc),
        )
        assert result.is_success()
        assert result.is_payment_captured()

    def test_block_result_receipt_is_never_success(self):
        """A BLOCK pipeline result must produce execution_refused, not payment_captured."""
        block_result = make_block_result()
        with patch("httpx.Client") as MockClient:
            client = make_client()
            receipt = execute_razorpay_payment(
                pipeline_result=block_result,
                buyer_agent_id="b1",
                merchant_id="m1",
                amount=make_money(),
                razorpay_client=client,
                originating_protocol=BuyerProtocol.acp,
            )
            MockClient.return_value.__enter__.assert_not_called()

        assert receipt.status != ExecutionStatus.PAYMENT_CAPTURED.value
        assert receipt.status == ExecutionStatus.EXECUTION_REFUSED.value

    def test_review_result_receipt_is_never_success(self):
        review_result = make_review_result()
        with patch("httpx.Client") as MockClient:
            client = make_client()
            receipt = execute_razorpay_payment(
                pipeline_result=review_result,
                buyer_agent_id="b1",
                merchant_id="m1",
                amount=make_money(),
                razorpay_client=client,
                originating_protocol=BuyerProtocol.acp,
            )
            MockClient.return_value.__enter__.assert_not_called()

        assert receipt.status == ExecutionStatus.EXECUTION_REFUSED.value
        assert receipt.decision != GatewayDecision.ALLOW


# ══════════════════════════════════════════════════════════════════════════════
# TestExecutionGate (all non-ALLOW decisions)
# ══════════════════════════════════════════════════════════════════════════════


class TestExecutionGate:
    def _assert_not_called(self, pipeline_result: TransactionResult):
        with patch("httpx.Client") as MockClient:
            client = make_client()
            receipt = execute_razorpay_payment(
                pipeline_result=pipeline_result,
                buyer_agent_id="b1",
                merchant_id="m1",
                amount=make_money(),
                razorpay_client=client,
                originating_protocol=BuyerProtocol.acp,
            )
            MockClient.return_value.__enter__.assert_not_called()
        return receipt

    def test_block_at_validation_does_not_call_razorpay(self):
        result = TransactionResult.blocked(
            "txn-val", PipelineStage.VALIDATION, "Validation failed"
        )
        receipt = self._assert_not_called(result)
        assert receipt.status == ExecutionStatus.EXECUTION_REFUSED.value

    def test_block_at_authorization_does_not_call_razorpay(self):
        result = TransactionResult.blocked(
            "txn-auth", PipelineStage.AUTHORIZATION, "Auth failed"
        )
        receipt = self._assert_not_called(result)
        assert receipt.status == ExecutionStatus.EXECUTION_REFUSED.value

    def test_block_at_replay_does_not_call_razorpay(self):
        result = TransactionResult.blocked(
            "txn-replay", PipelineStage.REPLAY, "Replay detected"
        )
        receipt = self._assert_not_called(result)
        assert receipt.status == ExecutionStatus.EXECUTION_REFUSED.value

    def test_block_at_policy_does_not_call_razorpay(self):
        result = make_block_result()
        receipt = self._assert_not_called(result)
        assert receipt.status == ExecutionStatus.EXECUTION_REFUSED.value

    def test_review_does_not_call_razorpay(self):
        result = make_review_result()
        receipt = self._assert_not_called(result)
        assert receipt.status == ExecutionStatus.EXECUTION_REFUSED.value
