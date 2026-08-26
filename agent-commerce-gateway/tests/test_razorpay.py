"""
Razorpay Execution Layer Tests — Agent Commerce Gateway
========================================================

Tests for:
  - execute_razorpay_payment() execution gate
  - RazorpayClient.create_order() with mocked HTTP responses
  - CommerceReceipt production
  - Credential safety (secrets must not appear in logs/repr/errors)

All Razorpay HTTP calls are mocked via httpx.MockTransport.
No real credentials are required for this test suite.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import patch

import httpx
import pytest

from app.razorpay.client import (
    RAZORPAY_ORDERS_ENDPOINT,
    ExecutionStatus,
    RazorpayClient,
    RazorpayOrderResult,
    execute_razorpay_payment,
)
from app.core.schemas import BuyerProtocol, GatewayDecision, Money
from app.core.transaction_result import (
    PipelineStage,
    ProcessingState,
    TransactionResult,
)
from app.core.mandate import AuthorizationVerificationResult
from app.core.replay import ReplayResult
from app.core.policy import PolicyDecision


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

FAKE_KEY_ID = "rzp_test_testkey123"
FAKE_KEY_SECRET = "supersecret_do_not_log"


def make_mock_transport(status_code: int, body: Optional[dict] = None, text: str = "") -> httpx.MockTransport:
    """Return a synchronous MockTransport that returns the given status/body."""
    if body is not None:
        content = json.dumps(body).encode()
        headers = {"content-type": "application/json"}
    else:
        content = text.encode() if text else b""
        headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=status_code, content=content, headers=headers)

    return httpx.MockTransport(handler)


def razorpay_order_response(
    order_id: str = "order_testABC123",
    status: str = "created",
    amount: int = 5000,
    currency: str = "INR",
    receipt: str = "txn-test-123",
) -> dict:
    """Valid Razorpay order creation response body."""
    return {
        "id": order_id,
        "entity": "order",
        "amount": amount,
        "amount_paid": 0,
        "amount_due": amount,
        "currency": currency,
        "receipt": receipt,
        "offer_id": None,
        "status": status,
        "attempts": 0,
        "notes": [],
        "created_at": 1234567890,
    }


def make_allow_result(transaction_id: str = "txn-test-123") -> TransactionResult:
    """Construct a fake ALLOW TransactionResult."""
    auth_result = AuthorizationVerificationResult(
        valid=True, reason="test", requires_replay_check=False
    )
    replay_result = ReplayResult(allowed=True, was_replay=False, reason="test")
    policy_result = PolicyDecision(
        decision=GatewayDecision.ALLOW,
        primary_reason="allowed by policy",
        triggered_rules=[],
    )
    return TransactionResult.allowed(
        transaction_id=transaction_id,
        reason="All gates passed",
        authorization_result=auth_result,
        replay_result=replay_result,
        policy_result=policy_result,
    )


def make_block_result(transaction_id: str = "txn-test-123") -> TransactionResult:
    return TransactionResult.blocked(
        transaction_id=transaction_id,
        stage=PipelineStage.POLICY,
        reason="Blocked by policy",
    )


def make_review_result(transaction_id: str = "txn-test-123") -> TransactionResult:
    auth_result = AuthorizationVerificationResult(
        valid=True, reason="test", requires_replay_check=False
    )
    replay_result = ReplayResult(allowed=True, was_replay=False, reason="test")
    policy_result = PolicyDecision(
        decision=GatewayDecision.REVIEW,
        primary_reason="flagged for review",
        triggered_rules=[],
    )
    return TransactionResult.review(
        transaction_id=transaction_id,
        reason="flagged for review",
        authorization_result=auth_result,
        replay_result=replay_result,
        policy_result=policy_result,
    )


def make_money(amount: int = 5000, currency: str = "INR") -> Money:
    return Money(amount_minor=amount, currency=currency)


def make_razorpay_client(transport: httpx.MockTransport) -> RazorpayClient:
    """Build a RazorpayClient that uses the given mock transport."""
    client = RazorpayClient(key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET)
    # Patch the httpx.Client to use mock transport
    original_init = httpx.Client.__init__

    def patched_init(self_inner, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self_inner, *args, **kwargs)

    client._patched_transport = transport  # store for test use
    return client


# ══════════════════════════════════════════════════════════════════════════════
# Test 1: ALLOW → Razorpay order created
# ══════════════════════════════════════════════════════════════════════════════


class TestAllowFlowOrderCreated:
    def test_allow_creates_razorpay_order(self):
        """ALLOW result must trigger Razorpay order creation and return an order_created receipt."""
        allow_result = make_allow_result("txn-allow-001")
        amount = make_money(5000, "INR")
        response_body = razorpay_order_response(
            order_id="order_ABC", amount=5000, currency="INR", receipt="txn-allow-001"
        )
        transport = make_mock_transport(200, response_body)

        with patch("httpx.Client") as MockClient:
            mock_instance = MockClient.return_value.__enter__.return_value
            mock_response = httpx.Response(
                200,
                content=json.dumps(response_body).encode(),
                headers={"content-type": "application/json"},
            )
            mock_instance.post.return_value = mock_response

            client = RazorpayClient(key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET)
            receipt = execute_razorpay_payment(
                pipeline_result=allow_result,
                buyer_agent_id="buyer-001",
                merchant_id="merchant-001",
                amount=amount,
                razorpay_client=client,
                originating_protocol=BuyerProtocol.acp,
            )

        assert receipt.status == ExecutionStatus.ORDER_CREATED.value
        assert receipt.payment_reference == "order_ABC"
        assert receipt.decision == GatewayDecision.ALLOW
        assert receipt.transaction_id == "txn-allow-001"
        # Verify Razorpay was called with correct args
        call_kwargs = mock_instance.post.call_args
        sent_payload = call_kwargs[1]["json"]
        assert sent_payload["amount"] == 5000
        assert sent_payload["currency"] == "INR"


# ══════════════════════════════════════════════════════════════════════════════
# Tests 2-4: REVIEW / BLOCK / UNDECIDED → Razorpay NOT called
# ══════════════════════════════════════════════════════════════════════════════


class TestNonAllowGating:
    def _assert_not_called(self, result: TransactionResult):
        with patch("httpx.Client") as MockClient:
            client = RazorpayClient(key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET)
            receipt = execute_razorpay_payment(
                pipeline_result=result,
                buyer_agent_id="b1",
                merchant_id="m1",
                amount=make_money(),
                razorpay_client=client,
                originating_protocol=BuyerProtocol.acp,
            )
            # Razorpay HTTP client must NOT have been entered
            MockClient.return_value.__enter__.assert_not_called()
        return receipt

    def test_review_does_not_call_razorpay(self):
        result = make_review_result()
        receipt = self._assert_not_called(result)
        assert receipt.status == ExecutionStatus.EXECUTION_REFUSED.value

    def test_block_does_not_call_razorpay(self):
        result = make_block_result()
        receipt = self._assert_not_called(result)
        assert receipt.status == ExecutionStatus.EXECUTION_REFUSED.value

    def test_block_from_auth_failure_does_not_call_razorpay(self):
        result = TransactionResult.blocked(
            transaction_id="txn-auth-fail",
            stage=PipelineStage.AUTHORIZATION,
            reason="Authorization failed",
        )
        receipt = self._assert_not_called(result)
        assert receipt.status == ExecutionStatus.EXECUTION_REFUSED.value

    def test_block_from_replay_failure_does_not_call_razorpay(self):
        result = TransactionResult.blocked(
            transaction_id="txn-replay-fail",
            stage=PipelineStage.REPLAY,
            reason="Replay detected",
        )
        receipt = self._assert_not_called(result)
        assert receipt.status == ExecutionStatus.EXECUTION_REFUSED.value

    def test_block_from_policy_does_not_call_razorpay(self):
        result = make_block_result()
        receipt = self._assert_not_called(result)
        assert receipt.status == ExecutionStatus.EXECUTION_REFUSED.value


# ══════════════════════════════════════════════════════════════════════════════
# Tests 8-9: Amount / currency mismatch → execution refused
# ══════════════════════════════════════════════════════════════════════════════


class TestAmountCurrencySafety:
    def test_zero_amount_refused_before_razorpay(self):
        """Amount of 0 must be refused before Razorpay is called."""
        allow_result = make_allow_result()
        with patch("httpx.Client") as MockClient:
            client = RazorpayClient(key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET)
            receipt = execute_razorpay_payment(
                pipeline_result=allow_result,
                buyer_agent_id="b1",
                merchant_id="m1",
                amount=Money(amount_minor=0, currency="INR"),
                razorpay_client=client,
                originating_protocol=BuyerProtocol.acp,
            )
            MockClient.return_value.__enter__.assert_not_called()
        assert receipt.status == ExecutionStatus.EXECUTION_REFUSED.value
        assert receipt.decision == GatewayDecision.BLOCK

    def test_negative_amount_refused(self):
        allow_result = make_allow_result()
        # Money model uses ge=0 so we test that execute_razorpay_payment handles 0
        # For negative amounts the Money model itself would reject at construction
        # Instead test internal client amount check
        client = RazorpayClient(key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET)
        with patch("httpx.Client"):
            result = client.create_order(amount_minor=-1, currency="INR", receipt="r")
        assert result.execution_status == ExecutionStatus.RAZORPAY_ERROR
        assert result.error_code == "INVALID_AMOUNT"

    def test_razorpay_amount_echo_mismatch_refused(self):
        """If Razorpay echoes back a different amount, refuse and return INVALID_RESPONSE."""
        allow_result = make_allow_result("txn-amt-mismatch")
        response_body = razorpay_order_response(amount=9999, currency="INR")  # sent 5000, got 9999

        with patch("httpx.Client") as MockClient:
            mock_instance = MockClient.return_value.__enter__.return_value
            mock_instance.post.return_value = httpx.Response(
                200,
                content=json.dumps(response_body).encode(),
                headers={"content-type": "application/json"},
            )
            client = RazorpayClient(key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET)
            order_result = client.create_order(
                amount_minor=5000, currency="INR", receipt="txn-amt-mismatch"
            )

        assert order_result.execution_status == ExecutionStatus.INVALID_RESPONSE
        assert order_result.error_code == "AMOUNT_MISMATCH"

    def test_razorpay_currency_echo_mismatch_refused(self):
        response_body = razorpay_order_response(amount=5000, currency="USD")  # sent INR, got USD

        with patch("httpx.Client") as MockClient:
            mock_instance = MockClient.return_value.__enter__.return_value
            mock_instance.post.return_value = httpx.Response(
                200,
                content=json.dumps(response_body).encode(),
                headers={"content-type": "application/json"},
            )
            client = RazorpayClient(key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET)
            order_result = client.create_order(
                amount_minor=5000, currency="INR", receipt="txn-curr-mismatch"
            )

        assert order_result.execution_status == ExecutionStatus.INVALID_RESPONSE
        assert order_result.error_code == "CURRENCY_MISMATCH"


# ══════════════════════════════════════════════════════════════════════════════
# Test 10-12: Razorpay failure handling
# ══════════════════════════════════════════════════════════════════════════════


class TestRazorpayFailureHandling:
    def test_razorpay_http_500_returns_safe_failure(self):
        error_body = {"error": {"code": "SERVER_ERROR", "description": "Internal server error"}}

        with patch("httpx.Client") as MockClient:
            mock_instance = MockClient.return_value.__enter__.return_value
            mock_instance.post.return_value = httpx.Response(
                500,
                content=json.dumps(error_body).encode(),
                headers={"content-type": "application/json"},
            )
            client = RazorpayClient(key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET)
            result = client.create_order(amount_minor=5000, currency="INR", receipt="txn-500")

        assert result.execution_status == ExecutionStatus.RAZORPAY_ERROR
        assert result.error_code == "SERVER_ERROR"
        assert result.http_status_code == 500
        assert result.razorpay_order_id is None

    def test_razorpay_401_auth_failure_safe(self):
        """Authentication failure must not log or expose secrets."""
        with patch("httpx.Client") as MockClient:
            mock_instance = MockClient.return_value.__enter__.return_value
            mock_instance.post.return_value = httpx.Response(401, content=b"Unauthorized")
            client = RazorpayClient(key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET)
            result = client.create_order(amount_minor=5000, currency="INR", receipt="txn-401")

        assert result.execution_status == ExecutionStatus.RAZORPAY_ERROR
        assert result.error_code == "AUTHENTICATION_FAILED"
        assert result.http_status_code == 401
        # Key secret must not appear in error description
        assert FAKE_KEY_SECRET not in (result.error_description or "")

    def test_razorpay_timeout_returns_network_error(self):
        with patch("httpx.Client") as MockClient:
            mock_instance = MockClient.return_value.__enter__.return_value
            mock_instance.post.side_effect = httpx.TimeoutException("timed out")
            client = RazorpayClient(key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET)
            result = client.create_order(amount_minor=5000, currency="INR", receipt="txn-timeout")

        assert result.execution_status == ExecutionStatus.NETWORK_ERROR
        assert result.error_code == "TIMEOUT"
        assert result.razorpay_order_id is None

    def test_razorpay_network_error_returns_safe_result(self):
        with patch("httpx.Client") as MockClient:
            mock_instance = MockClient.return_value.__enter__.return_value
            mock_instance.post.side_effect = httpx.NetworkError("Connection refused")
            client = RazorpayClient(key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET)
            result = client.create_order(amount_minor=5000, currency="INR", receipt="txn-net")

        assert result.execution_status == ExecutionStatus.NETWORK_ERROR
        assert result.error_code == "NETWORK_ERROR"

    def test_malformed_json_response_returns_invalid_response(self):
        with patch("httpx.Client") as MockClient:
            mock_instance = MockClient.return_value.__enter__.return_value
            mock_instance.post.return_value = httpx.Response(
                200, content=b"NOT_JSON_AT_ALL"
            )
            client = RazorpayClient(key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET)
            result = client.create_order(amount_minor=5000, currency="INR", receipt="txn-json")

        assert result.execution_status == ExecutionStatus.INVALID_RESPONSE
        assert result.error_code == "INVALID_JSON"

    def test_missing_id_in_response_returns_invalid(self):
        incomplete_body = {"status": "created", "amount": 5000}  # no 'id' field
        with patch("httpx.Client") as MockClient:
            mock_instance = MockClient.return_value.__enter__.return_value
            mock_instance.post.return_value = httpx.Response(
                200,
                content=json.dumps(incomplete_body).encode(),
                headers={"content-type": "application/json"},
            )
            client = RazorpayClient(key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET)
            result = client.create_order(amount_minor=5000, currency="INR", receipt="txn-noid")

        assert result.execution_status == ExecutionStatus.INVALID_RESPONSE
        assert result.error_code == "MISSING_FIELDS"


# ══════════════════════════════════════════════════════════════════════════════
# Test 13-14: Payment state distinction
# ══════════════════════════════════════════════════════════════════════════════


class TestPaymentStateDistinction:
    def test_order_created_not_reported_as_captured(self):
        """An order_created receipt must NOT have status=payment_captured."""
        allow_result = make_allow_result("txn-state-001")
        response_body = razorpay_order_response(order_id="order_XYZ", status="created")

        with patch("httpx.Client") as MockClient:
            mock_instance = MockClient.return_value.__enter__.return_value
            mock_instance.post.return_value = httpx.Response(
                200,
                content=json.dumps(response_body).encode(),
                headers={"content-type": "application/json"},
            )
            client = RazorpayClient(key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET)
            receipt = execute_razorpay_payment(
                pipeline_result=allow_result,
                buyer_agent_id="b1",
                merchant_id="m1",
                amount=make_money(),
                razorpay_client=client,
                originating_protocol=BuyerProtocol.acp,
            )

        # Must be order_created, NOT payment_captured
        assert receipt.status == ExecutionStatus.ORDER_CREATED.value
        assert receipt.status != ExecutionStatus.PAYMENT_CAPTURED.value

    def test_authorized_not_reported_as_captured(self):
        """PAYMENT_AUTHORIZED must never be falsely reported as PAYMENT_CAPTURED."""
        # Build a RazorpayOrderResult with AUTHORIZED state directly
        result = RazorpayOrderResult(
            execution_status=ExecutionStatus.PAYMENT_AUTHORIZED,
            razorpay_order_id="order_ABC",
            razorpay_order_status="attempted",
            razorpay_amount=5000,
            razorpay_currency="INR",
            razorpay_receipt="txn-auth",
            razorpay_payment_id="pay_ABC",
            razorpay_payment_status="authorized",
            error_code=None,
            error_description=None,
            http_status_code=200,
            timestamp=datetime.now(timezone.utc),
        )
        assert not result.is_success()           # authorized is NOT success
        assert not result.is_payment_captured()  # explicitly not captured
        assert result.execution_status == ExecutionStatus.PAYMENT_AUTHORIZED

    def test_captured_is_the_only_success_state(self):
        result = RazorpayOrderResult(
            execution_status=ExecutionStatus.PAYMENT_CAPTURED,
            razorpay_order_id="order_ABC",
            razorpay_order_status="paid",
            razorpay_amount=5000,
            razorpay_currency="INR",
            razorpay_receipt="txn-cap",
            razorpay_payment_id="pay_CAP",
            razorpay_payment_status="captured",
            error_code=None,
            error_description=None,
            http_status_code=200,
            timestamp=datetime.now(timezone.utc),
        )
        assert result.is_success()
        assert result.is_payment_captured()


# ══════════════════════════════════════════════════════════════════════════════
# Test 15: Credentials never appear in logs/errors/repr
# ══════════════════════════════════════════════════════════════════════════════


class TestCredentialSafety:
    def test_repr_does_not_contain_secret(self):
        client = RazorpayClient(key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET)
        r = repr(client)
        assert FAKE_KEY_SECRET not in r
        assert "supersecret" not in r

    def test_order_result_repr_does_not_contain_secret(self):
        result = RazorpayOrderResult(
            execution_status=ExecutionStatus.ORDER_CREATED,
            razorpay_order_id="order_test",
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
        r = repr(result)
        assert FAKE_KEY_SECRET not in r
        assert FAKE_KEY_ID not in r  # key_id not expected in order result repr

    def test_auth_failure_error_message_does_not_expose_secret(self):
        with patch("httpx.Client") as MockClient:
            mock_instance = MockClient.return_value.__enter__.return_value
            mock_instance.post.return_value = httpx.Response(401, content=b"Unauthorized")
            client = RazorpayClient(key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET)
            result = client.create_order(5000, "INR", "txn-auth-fail")

        assert FAKE_KEY_SECRET not in (result.error_description or "")
        assert FAKE_KEY_SECRET not in (result.error_code or "")

    def test_network_error_does_not_expose_secret(self, caplog):
        with caplog.at_level(logging.ERROR, logger="app.razorpay.client"):
            with patch("httpx.Client") as MockClient:
                mock_instance = MockClient.return_value.__enter__.return_value
                mock_instance.post.side_effect = httpx.TimeoutException("timed out")
                client = RazorpayClient(key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET)
                client.create_order(5000, "INR", "txn-log-test")

        for record in caplog.records:
            assert FAKE_KEY_SECRET not in record.getMessage()

    def test_client_construction_rejects_empty_key_id(self):
        with pytest.raises(ValueError, match="key_id"):
            RazorpayClient(key_id="", key_secret=FAKE_KEY_SECRET)

    def test_client_construction_rejects_empty_key_secret(self):
        with pytest.raises(ValueError, match="key_secret"):
            RazorpayClient(key_id=FAKE_KEY_ID, key_secret="")
