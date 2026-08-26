"""
Razorpay Execution Layer — Agent Commerce Gateway
==================================================

Responsible for:
1. Creating Razorpay Test-Mode orders from ALLOW-gated TransactionResults.
2. Verifying payment signatures after client-side checkout completes.
3. Capturing authorized payments via the official Razorpay capture endpoint.
4. Producing structured CommerceReceipts from all lifecycle transitions.

Authoritative sources:
    Orders API:   https://razorpay.com/docs/api/orders/
    Payments API: https://razorpay.com/docs/api/payments/
    Verification: https://razorpay.com/docs/payment-gateway/web-integration/
                  standard/integration-steps/#step-3-handle-payment-success

Architecture:
    TransactionResult(ALLOW)
        ↓
    execute_razorpay_payment(result, request)
        ↓  [execution gate: only ALLOW passes]
    RazorpayClient.create_order()
        ↓
    POST https://api.razorpay.com/v1/orders
        ↓
    Razorpay Order (order_created)
        ↓
    [Client-side Razorpay Checkout / Payment UI — browser required]
        ↓
    Client sends: razorpay_order_id, razorpay_payment_id, razorpay_signature
        ↓
    RazorpayClient.verify_payment_signature()    ← HMAC-SHA256 server-side
        ↓
    RazorpayClient.capture_payment() [if manual capture mode]
        ↓
    POST https://api.razorpay.com/v1/payments/{id}/capture
        ↓
    CommerceReceipt (payment_captured = SUCCESS)

Payment State Distinction (per official Razorpay docs):
    order_created       — POST /v1/orders succeeded; no payment has occurred.
    payment_created     — Payment initiated by client (e.g., on checkout).
    payment_authorized  — Bank deducted; NOT yet settled. "On hold" up to 3 days.
    payment_captured    — Confirmed and settlement-scheduled. ONLY TRUE SUCCESS.
    payment_failed      — Declined or timed out.

CRITICAL: Order creation alone is NOT a completed payment. This is clearly
reflected in the status values — only payment_captured is SUCCESS.

Payment Verification (per official Razorpay docs):
    Algorithm: HMAC-SHA256
    Message:   razorpay_order_id + "|" + razorpay_payment_id
    Key:       key_secret
    Compare:   generated_signature == razorpay_signature (constant-time)

Payment Capture (per official Razorpay docs):
    Endpoint:  POST /v1/payments/{payment_id}/capture
    Body:      {"amount": <int>, "currency": "<ISO>"}
    Preconditions:
        - Payment status must be "authorized"
        - Amount must exactly match the approved transaction total
        - Currency must exactly match
        - payment_id must belong to the correct order_id

Execution Gate:
    ONLY GatewayDecision.ALLOW may reach Razorpay.
    BLOCK, REVIEW, UNDECIDED are refused before any HTTP call.

Idempotency:
    Razorpay does NOT provide a native idempotency key header for order
    creation. The `receipt` field (our transaction_id, max 40 chars) serves
    as audit identifier. The gateway's upstream replay protection prevents
    duplicate ALLOW results architecturally.

Credential Safety:
    - key_secret used ONLY in HMAC computation — never logged or repr'd.
    - Authorization header is handled by httpx — never logged.
    - Errors include only status codes and safe Razorpay error codes.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import httpx

from app.core.schemas import (
    BuyerProtocol,
    CommerceReceipt,
    GatewayDecision,
    Money,
)
from app.core.transaction_result import TransactionResult, PipelineStage

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# Razorpay constants (from verified official API docs)
# ══════════════════════════════════════════════════════════════════════════════

RAZORPAY_API_BASE = "https://api.razorpay.com"
RAZORPAY_ORDERS_ENDPOINT = "/v1/orders"
RAZORPAY_PAYMENTS_ENDPOINT = "/v1/payments"
RAZORPAY_RECEIPT_MAX_LEN = 40   # per Razorpay docs: receipt max 40 chars
RAZORPAY_API_TIMEOUT_SECONDS = 30.0


# ══════════════════════════════════════════════════════════════════════════════
# ExecutionStatus — gateway-level lifecycle states
# ══════════════════════════════════════════════════════════════════════════════


class ExecutionStatus(str, Enum):
    """
    Gateway-level payment execution state.

    Explicitly distinct from Razorpay's internal status strings.
    Maps Razorpay states to gateway-level semantics.
    """
    ORDER_CREATED = "order_created"            # POST /v1/orders succeeded; no payment
    PAYMENT_CREATED = "payment_created"        # Payment initiated on client
    PAYMENT_AUTHORIZED = "payment_authorized"  # Bank deducted; NOT captured — not success
    PAYMENT_CAPTURED = "payment_captured"      # Captured — settlement scheduled — SUCCESS
    PAYMENT_FAILED = "payment_failed"          # Payment was declined or timed out
    EXECUTION_REFUSED = "execution_refused"    # Gateway refused (non-ALLOW decision)
    RAZORPAY_ERROR = "razorpay_error"          # Razorpay returned an error
    NETWORK_ERROR = "network_error"            # Timeout or connection failure
    INVALID_RESPONSE = "invalid_response"      # Malformed or unexpected response
    SIGNATURE_INVALID = "signature_invalid"    # HMAC-SHA256 signature mismatch
    UNKNOWN = "unknown"                        # Cannot determine state safely


# ══════════════════════════════════════════════════════════════════════════════
# RazorpayOrderResult — structured result from order creation
# ══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class RazorpayOrderResult:
    """
    Structured result from Razorpay order creation.

    Fields correspond to the official Razorpay Order object.
    Never includes credentials or auth headers.
    """
    execution_status: ExecutionStatus

    # Razorpay order fields (populated on success)
    razorpay_order_id: Optional[str]
    razorpay_order_status: Optional[str]
    razorpay_amount: Optional[int]
    razorpay_currency: Optional[str]
    razorpay_receipt: Optional[str]

    # Payment fields (only populated after payment collection)
    razorpay_payment_id: Optional[str]
    razorpay_payment_status: Optional[str]

    # Failure details (never include credentials)
    error_code: Optional[str]
    error_description: Optional[str]
    http_status_code: Optional[int]

    timestamp: datetime

    def is_order_created(self) -> bool:
        return self.execution_status == ExecutionStatus.ORDER_CREATED

    def is_payment_captured(self) -> bool:
        return self.execution_status == ExecutionStatus.PAYMENT_CAPTURED

    def is_success(self) -> bool:
        """Only payment_captured is a true success."""
        return self.is_payment_captured()

    def is_failure(self) -> bool:
        return self.execution_status in {
            ExecutionStatus.PAYMENT_FAILED,
            ExecutionStatus.EXECUTION_REFUSED,
            ExecutionStatus.RAZORPAY_ERROR,
            ExecutionStatus.NETWORK_ERROR,
            ExecutionStatus.INVALID_RESPONSE,
            ExecutionStatus.SIGNATURE_INVALID,
        }

    def __repr__(self) -> str:
        # Safety: never repr credentials.
        return (
            f"RazorpayOrderResult("
            f"status={self.execution_status.value!r}, "
            f"order_id={self.razorpay_order_id!r}, "
            f"payment_id={self.razorpay_payment_id!r}"
            f")"
        )


# ══════════════════════════════════════════════════════════════════════════════
# RazorpayClient — HTTPS client using official Basic Auth
# ══════════════════════════════════════════════════════════════════════════════


class RazorpayClient:
    """
    HTTP client for the Razorpay v1 API.

    Authentication: HTTP Basic Auth (key_id = username, key_secret = password).
    The Authorization header is constructed by httpx and NEVER logged.

    The key_secret is ALSO used for HMAC-SHA256 payment signature verification.
    It is never stored in a repr-able attribute, never logged, never appears
    in error messages.
    """

    def __init__(self, key_id: str, key_secret: str, timeout: float = RAZORPAY_API_TIMEOUT_SECONDS):
        if not key_id or not key_id.strip():
            raise ValueError("Razorpay key_id must not be empty.")
        if not key_secret or not key_secret.strip():
            raise ValueError("Razorpay key_secret must not be empty.")

        self._key_id = key_id
        self._auth = (key_id, key_secret)  # used by httpx — never logged
        self._timeout = timeout
        # Store key_secret ONLY for HMAC — never repr'd or logged
        self.__key_secret = key_secret

    def __repr__(self) -> str:
        # key_secret deliberately excluded
        return f"RazorpayClient(key_id={self._key_id!r})"

    # ── Order creation ─────────────────────────────────────────────────────────

    def create_order(
        self,
        amount_minor: int,
        currency: str,
        receipt: str,
        notes: Optional[dict] = None,
    ) -> RazorpayOrderResult:
        """
        Create a Razorpay order via POST /v1/orders.

        Per official docs:
            amount: integer, smallest currency sub-unit (5000 = ₹50.00)
            currency: ISO 4217 string ("INR")
            receipt: unique reference, max 40 characters

        Returns:
            RazorpayOrderResult — always; never raises.
        """
        now = datetime.now(timezone.utc)

        if amount_minor <= 0:
            return self._error_result(
                "INVALID_AMOUNT",
                f"Amount must be positive; got {amount_minor}.",
                now,
            )

        safe_receipt = re.sub(r'[^a-zA-Z0-9_-]', '_', str(receipt))[:RAZORPAY_RECEIPT_MAX_LEN]
        payload: dict = {
            "amount": amount_minor,
            "currency": currency.upper(),
            "receipt": safe_receipt,
        }
        if notes:
            payload["notes"] = notes

        try:
            with httpx.Client(
                base_url=RAZORPAY_API_BASE,
                auth=self._auth,
                timeout=self._timeout,
                headers={"Content-Type": "application/json"},
            ) as client:
                response = client.post(RAZORPAY_ORDERS_ENDPOINT, json=payload)
        except httpx.TimeoutException:
            logger.error(
                "Razorpay timeout creating order for receipt=%r", safe_receipt
            )
            return RazorpayOrderResult(
                execution_status=ExecutionStatus.NETWORK_ERROR,
                razorpay_order_id=None, razorpay_order_status=None,
                razorpay_amount=None, razorpay_currency=None, razorpay_receipt=None,
                razorpay_payment_id=None, razorpay_payment_status=None,
                error_code="TIMEOUT",
                error_description=f"Request timed out after {self._timeout}s.",
                http_status_code=None, timestamp=now,
            )
        except httpx.NetworkError as exc:
            logger.error(
                "Razorpay network error for receipt=%r: %s", safe_receipt, type(exc).__name__
            )
            return RazorpayOrderResult(
                execution_status=ExecutionStatus.NETWORK_ERROR,
                razorpay_order_id=None, razorpay_order_status=None,
                razorpay_amount=None, razorpay_currency=None, razorpay_receipt=None,
                razorpay_payment_id=None, razorpay_payment_status=None,
                error_code="NETWORK_ERROR",
                error_description=f"Network error: {type(exc).__name__}.",
                http_status_code=None, timestamp=now,
            )
        except Exception as exc:
            logger.error(
                "Unexpected error calling Razorpay for receipt=%r: %s",
                safe_receipt, type(exc).__name__
            )
            return RazorpayOrderResult(
                execution_status=ExecutionStatus.NETWORK_ERROR,
                razorpay_order_id=None, razorpay_order_status=None,
                razorpay_amount=None, razorpay_currency=None, razorpay_receipt=None,
                razorpay_payment_id=None, razorpay_payment_status=None,
                error_code="UNEXPECTED_ERROR",
                error_description=f"Unexpected error: {type(exc).__name__}.",
                http_status_code=None, timestamp=now,
            )

        return self._parse_order_response(response, amount_minor, currency, safe_receipt, now)

    def _parse_order_response(
        self,
        response: httpx.Response,
        expected_amount: int,
        expected_currency: str,
        receipt: str,
        now: datetime,
    ) -> RazorpayOrderResult:
        """Parse a Razorpay order creation response."""
        if response.status_code == 401:
            logger.error(
                "Razorpay authentication failed (401). receipt=%r "
                "(key_id is configured but key_secret NOT logged)", receipt
            )
            return RazorpayOrderResult(
                execution_status=ExecutionStatus.RAZORPAY_ERROR,
                razorpay_order_id=None, razorpay_order_status=None,
                razorpay_amount=None, razorpay_currency=None, razorpay_receipt=None,
                razorpay_payment_id=None, razorpay_payment_status=None,
                error_code="AUTHENTICATION_FAILED",
                error_description="Razorpay authentication failed. Verify test-mode credentials.",
                http_status_code=401, timestamp=now,
            )

        if response.status_code != 200:
            return self._http_error_result(response, now)

        try:
            body = response.json()
        except Exception:
            logger.error("Razorpay returned 200 but non-JSON body for receipt=%r", receipt)
            return RazorpayOrderResult(
                execution_status=ExecutionStatus.INVALID_RESPONSE,
                razorpay_order_id=None, razorpay_order_status=None,
                razorpay_amount=None, razorpay_currency=None, razorpay_receipt=None,
                razorpay_payment_id=None, razorpay_payment_status=None,
                error_code="INVALID_JSON",
                error_description="Razorpay returned 200 with non-JSON response body.",
                http_status_code=200, timestamp=now,
            )

        razorpay_id = body.get("id")
        razorpay_status = body.get("status")
        razorpay_amount = body.get("amount")
        razorpay_currency = body.get("currency")
        razorpay_receipt = body.get("receipt")

        if not razorpay_id or not razorpay_status:
            logger.error(
                "Razorpay order response missing 'id' or 'status' for receipt=%r", receipt
            )
            return RazorpayOrderResult(
                execution_status=ExecutionStatus.INVALID_RESPONSE,
                razorpay_order_id=razorpay_id, razorpay_order_status=razorpay_status,
                razorpay_amount=razorpay_amount, razorpay_currency=razorpay_currency,
                razorpay_receipt=razorpay_receipt,
                razorpay_payment_id=None, razorpay_payment_status=None,
                error_code="MISSING_FIELDS",
                error_description="Razorpay response missing required 'id' or 'status' fields.",
                http_status_code=200, timestamp=now,
            )

        # Amount cross-check
        if razorpay_amount is not None and razorpay_amount != expected_amount:
            logger.error(
                "Razorpay amount mismatch: sent=%d, received=%d, receipt=%r",
                expected_amount, razorpay_amount, receipt
            )
            return RazorpayOrderResult(
                execution_status=ExecutionStatus.INVALID_RESPONSE,
                razorpay_order_id=razorpay_id, razorpay_order_status=razorpay_status,
                razorpay_amount=razorpay_amount, razorpay_currency=razorpay_currency,
                razorpay_receipt=razorpay_receipt,
                razorpay_payment_id=None, razorpay_payment_status=None,
                error_code="AMOUNT_MISMATCH",
                error_description=(
                    f"Razorpay echoed amount {razorpay_amount} "
                    f"but gateway sent {expected_amount}."
                ),
                http_status_code=200, timestamp=now,
            )

        # Currency cross-check
        if razorpay_currency is not None and razorpay_currency.upper() != expected_currency.upper():
            logger.error(
                "Razorpay currency mismatch: sent=%r, received=%r, receipt=%r",
                expected_currency, razorpay_currency, receipt
            )
            return RazorpayOrderResult(
                execution_status=ExecutionStatus.INVALID_RESPONSE,
                razorpay_order_id=razorpay_id, razorpay_order_status=razorpay_status,
                razorpay_amount=razorpay_amount, razorpay_currency=razorpay_currency,
                razorpay_receipt=razorpay_receipt,
                razorpay_payment_id=None, razorpay_payment_status=None,
                error_code="CURRENCY_MISMATCH",
                error_description=(
                    f"Razorpay echoed currency {razorpay_currency!r} "
                    f"but gateway sent {expected_currency!r}."
                ),
                http_status_code=200, timestamp=now,
            )

        logger.info(
            "Razorpay order created: order_id=%r, status=%r, receipt=%r",
            razorpay_id, razorpay_status, razorpay_receipt
        )

        return RazorpayOrderResult(
            execution_status=ExecutionStatus.ORDER_CREATED,
            razorpay_order_id=razorpay_id,
            razorpay_order_status=razorpay_status,
            razorpay_amount=razorpay_amount,
            razorpay_currency=razorpay_currency,
            razorpay_receipt=razorpay_receipt,
            razorpay_payment_id=None,
            razorpay_payment_status=None,
            error_code=None,
            error_description=None,
            http_status_code=200,
            timestamp=now,
        )

    # ── Payment signature verification ────────────────────────────────────────

    def verify_payment_signature(
        self,
        razorpay_order_id: str,
        razorpay_payment_id: str,
        razorpay_signature: str,
    ) -> bool:
        """
        Verify a Razorpay payment signature server-side using HMAC-SHA256.

        Per official Razorpay documentation:
            Message to sign: razorpay_order_id + "|" + razorpay_payment_id
            Key:             key_secret
            Algorithm:       HMAC-SHA256 hex digest
            Compare:         generated_signature == razorpay_signature (constant-time)

        The key_secret is used ONLY here in the HMAC computation — it is never
        logged, repr'd, or included in any exception message.

        Args:
            razorpay_order_id:  The order_id from our gateway (not from client alone).
            razorpay_payment_id: The payment_id from the Razorpay checkout callback.
            razorpay_signature:  The signature from the Razorpay checkout callback.

        Returns:
            True if signature is valid.
            False if invalid or any argument is missing/malformed.

        Never raises.
        """
        if not razorpay_order_id or not razorpay_payment_id or not razorpay_signature:
            logger.warning(
                "Payment signature verification failed: missing required parameters. "
                "order_id=%r, payment_id=%r, signature present=%r",
                razorpay_order_id, razorpay_payment_id, bool(razorpay_signature)
            )
            return False

        try:
            message = f"{razorpay_order_id}|{razorpay_payment_id}".encode("utf-8")
            # key_secret deliberately not in any variable named plainly to avoid accidental logging
            generated = hmac.new(
                self.__key_secret.encode("utf-8"),
                message,
                hashlib.sha256,
            ).hexdigest()

            # Constant-time comparison to prevent timing attacks
            is_valid = hmac.compare_digest(generated, razorpay_signature)

            if not is_valid:
                logger.warning(
                    "Payment signature mismatch for order_id=%r payment_id=%r",
                    razorpay_order_id, razorpay_payment_id
                )
            else:
                logger.info(
                    "Payment signature verified: order_id=%r payment_id=%r",
                    razorpay_order_id, razorpay_payment_id
                )
            return is_valid

        except Exception as exc:
            logger.error(
                "Unexpected error during signature verification: %s", type(exc).__name__
            )
            return False

    def fetch_payment(self, payment_id: str) -> Optional[dict]:
        """Fetch a payment's details from Razorpay."""
        if not payment_id or not payment_id.strip():
            return None
            
        try:
            with httpx.Client(
                base_url=RAZORPAY_API_BASE,
                auth=self._auth,
                timeout=self._timeout,
                headers={"Content-Type": "application/json"},
            ) as client:
                response = client.get(f"{RAZORPAY_PAYMENTS_ENDPOINT}/{payment_id}")
            if response.status_code == 200:
                return response.json()
        except Exception as exc:
            logger.error("Failed to fetch payment_id=%r: %s", payment_id, type(exc).__name__)
        return None

    # ── Payment capture ────────────────────────────────────────────────────────

    def capture_payment(
        self,
        payment_id: str,
        amount_minor: int,
        currency: str,
        expected_order_id: str,
    ) -> RazorpayOrderResult:
        """
        Capture an authorized payment via POST /v1/payments/{id}/capture.

        Per official Razorpay docs:
            Endpoint: POST /v1/payments/{payment_id}/capture
            Body:     {"amount": <int minor units>, "currency": "<ISO>"}
            Precondition: payment status must be "authorized"

        Safety checks before calling Razorpay:
            - amount_minor must be > 0
            - currency must be non-empty
            - payment_id and expected_order_id must be non-empty

        Args:
            payment_id:        Razorpay payment_id to capture.
            amount_minor:      Amount to capture (must match authorized amount).
            currency:          Currency code (must match authorized currency).
            expected_order_id: The order_id this payment must belong to (from our DB).

        Returns:
            RazorpayOrderResult — always; never raises.
        """
        now = datetime.now(timezone.utc)

        # Pre-flight validation
        if not payment_id or not payment_id.strip():
            return self._error_result("INVALID_PAYMENT_ID", "payment_id must not be empty.", now)

        if not expected_order_id or not expected_order_id.strip():
            return self._error_result("INVALID_ORDER_ID", "expected_order_id must not be empty.", now)

        if amount_minor <= 0:
            return self._error_result("INVALID_AMOUNT", f"Capture amount must be > 0; got {amount_minor}.", now)

        if not currency or not currency.strip():
            return self._error_result("INVALID_CURRENCY", "Currency must not be empty.", now)

        capture_url = f"{RAZORPAY_PAYMENTS_ENDPOINT}/{payment_id}/capture"
        payload = {"amount": amount_minor, "currency": currency.upper()}

        try:
            with httpx.Client(
                base_url=RAZORPAY_API_BASE,
                auth=self._auth,
                timeout=self._timeout,
                headers={"Content-Type": "application/json"},
            ) as client:
                response = client.post(capture_url, json=payload)
        except httpx.TimeoutException:
            logger.error("Razorpay timeout capturing payment_id=%r", payment_id)
            return RazorpayOrderResult(
                execution_status=ExecutionStatus.NETWORK_ERROR,
                razorpay_order_id=expected_order_id,
                razorpay_order_status=None,
                razorpay_amount=None,
                razorpay_currency=None,
                razorpay_receipt=None,
                razorpay_payment_id=payment_id,
                razorpay_payment_status=None,
                error_code="TIMEOUT",
                error_description=f"Capture timed out after {self._timeout}s.",
                http_status_code=None,
                timestamp=now,
            )
        except httpx.NetworkError as exc:
            logger.error("Network error capturing payment_id=%r: %s", payment_id, type(exc).__name__)
            return RazorpayOrderResult(
                execution_status=ExecutionStatus.NETWORK_ERROR,
                razorpay_order_id=expected_order_id,
                razorpay_order_status=None,
                razorpay_amount=None,
                razorpay_currency=None,
                razorpay_receipt=None,
                razorpay_payment_id=payment_id,
                razorpay_payment_status=None,
                error_code="NETWORK_ERROR",
                error_description=f"Network error: {type(exc).__name__}.",
                http_status_code=None,
                timestamp=now,
            )
        except Exception as exc:
            logger.error("Unexpected error capturing payment_id=%r: %s", payment_id, type(exc).__name__)
            return RazorpayOrderResult(
                execution_status=ExecutionStatus.NETWORK_ERROR,
                razorpay_order_id=expected_order_id,
                razorpay_order_status=None,
                razorpay_amount=None,
                razorpay_currency=None,
                razorpay_receipt=None,
                razorpay_payment_id=payment_id,
                razorpay_payment_status=None,
                error_code="UNEXPECTED_ERROR",
                error_description=f"Unexpected error: {type(exc).__name__}.",
                http_status_code=None,
                timestamp=now,
            )

        return self._parse_capture_response(
            response, payment_id, amount_minor, currency, expected_order_id, now
        )

    def _parse_capture_response(
        self,
        response: httpx.Response,
        payment_id: str,
        expected_amount: int,
        expected_currency: str,
        expected_order_id: str,
        now: datetime,
    ) -> RazorpayOrderResult:
        """Parse a Razorpay payment capture response."""
        if response.status_code == 401:
            logger.error("Razorpay authentication failed (401) during capture for payment_id=%r", payment_id)
            return RazorpayOrderResult(
                execution_status=ExecutionStatus.RAZORPAY_ERROR,
                razorpay_order_id=expected_order_id,
                razorpay_order_status=None,
                razorpay_amount=None,
                razorpay_currency=None,
                razorpay_receipt=None,
                razorpay_payment_id=payment_id,
                razorpay_payment_status=None,
                error_code="AUTHENTICATION_FAILED",
                error_description="Razorpay authentication failed during capture.",
                http_status_code=401,
                timestamp=now,
            )

        if response.status_code != 200:
            return self._http_error_result(response, now, payment_id=payment_id, order_id=expected_order_id)

        try:
            body = response.json()
        except Exception:
            return RazorpayOrderResult(
                execution_status=ExecutionStatus.INVALID_RESPONSE,
                razorpay_order_id=expected_order_id,
                razorpay_order_status=None,
                razorpay_amount=None,
                razorpay_currency=None,
                razorpay_receipt=None,
                razorpay_payment_id=payment_id,
                razorpay_payment_status=None,
                error_code="INVALID_JSON",
                error_description="Razorpay returned 200 with non-JSON capture response.",
                http_status_code=200,
                timestamp=now,
            )

        returned_status = body.get("status")
        returned_order_id = body.get("order_id")
        returned_amount = body.get("amount")
        returned_currency = body.get("currency")

        # Verify the payment belongs to our expected order
        if returned_order_id and returned_order_id != expected_order_id:
            logger.error(
                "Razorpay capture order_id mismatch: expected=%r, received=%r",
                expected_order_id, returned_order_id
            )
            return RazorpayOrderResult(
                execution_status=ExecutionStatus.INVALID_RESPONSE,
                razorpay_order_id=expected_order_id,
                razorpay_order_status=None,
                razorpay_amount=returned_amount,
                razorpay_currency=returned_currency,
                razorpay_receipt=None,
                razorpay_payment_id=payment_id,
                razorpay_payment_status=returned_status,
                error_code="ORDER_MISMATCH",
                error_description=(
                    f"Captured payment belongs to order {returned_order_id!r}, "
                    f"not expected order {expected_order_id!r}."
                ),
                http_status_code=200,
                timestamp=now,
            )

        # Verify captured amount matches expected
        if returned_amount is not None and returned_amount != expected_amount:
            logger.error(
                "Razorpay capture amount mismatch: expected=%d, captured=%d",
                expected_amount, returned_amount
            )
            return RazorpayOrderResult(
                execution_status=ExecutionStatus.INVALID_RESPONSE,
                razorpay_order_id=returned_order_id or expected_order_id,
                razorpay_order_status=None,
                razorpay_amount=returned_amount,
                razorpay_currency=returned_currency,
                razorpay_receipt=None,
                razorpay_payment_id=payment_id,
                razorpay_payment_status=returned_status,
                error_code="AMOUNT_MISMATCH",
                error_description=(
                    f"Captured amount {returned_amount} does not match "
                    f"expected {expected_amount}."
                ),
                http_status_code=200,
                timestamp=now,
            )

        if returned_status != "captured":
            logger.warning(
                "Razorpay capture response status is %r (not 'captured') for payment_id=%r",
                returned_status, payment_id
            )
            return RazorpayOrderResult(
                execution_status=ExecutionStatus.UNKNOWN,
                razorpay_order_id=returned_order_id or expected_order_id,
                razorpay_order_status=None,
                razorpay_amount=returned_amount,
                razorpay_currency=returned_currency,
                razorpay_receipt=None,
                razorpay_payment_id=payment_id,
                razorpay_payment_status=returned_status,
                error_code="UNEXPECTED_STATUS",
                error_description=f"Expected 'captured' status but received {returned_status!r}.",
                http_status_code=200,
                timestamp=now,
            )

        logger.info(
            "Payment captured successfully: payment_id=%r, order_id=%r, amount=%d %s",
            payment_id, returned_order_id or expected_order_id, returned_amount or expected_amount, expected_currency
        )
        return RazorpayOrderResult(
            execution_status=ExecutionStatus.PAYMENT_CAPTURED,
            razorpay_order_id=returned_order_id or expected_order_id,
            razorpay_order_status="paid",
            razorpay_amount=returned_amount or expected_amount,
            razorpay_currency=returned_currency or expected_currency,
            razorpay_receipt=None,
            razorpay_payment_id=payment_id,
            razorpay_payment_status="captured",
            error_code=None,
            error_description=None,
            http_status_code=200,
            timestamp=now,
        )

    # ── Private helpers ────────────────────────────────────────────────────────

    def _error_result(
        self,
        error_code: str,
        error_description: str,
        now: datetime,
        payment_id: Optional[str] = None,
        order_id: Optional[str] = None,
    ) -> RazorpayOrderResult:
        return RazorpayOrderResult(
            execution_status=ExecutionStatus.RAZORPAY_ERROR,
            razorpay_order_id=order_id,
            razorpay_order_status=None,
            razorpay_amount=None,
            razorpay_currency=None,
            razorpay_receipt=None,
            razorpay_payment_id=payment_id,
            razorpay_payment_status=None,
            error_code=error_code,
            error_description=error_description,
            http_status_code=None,
            timestamp=now,
        )

    def _http_error_result(
        self,
        response: httpx.Response,
        now: datetime,
        payment_id: Optional[str] = None,
        order_id: Optional[str] = None,
    ) -> RazorpayOrderResult:
        try:
            err_body = response.json()
            error_code = err_body.get("error", {}).get("code", "UNKNOWN")
            error_desc = err_body.get("error", {}).get("description", f"HTTP {response.status_code}")
        except Exception:
            error_code = "PARSE_ERROR"
            error_desc = f"HTTP {response.status_code} with non-JSON body."

        logger.error(
            "Razorpay returned HTTP %d: code=%r", response.status_code, error_code
        )
        return RazorpayOrderResult(
            execution_status=ExecutionStatus.RAZORPAY_ERROR,
            razorpay_order_id=order_id,
            razorpay_order_status=None,
            razorpay_amount=None,
            razorpay_currency=None,
            razorpay_receipt=None,
            razorpay_payment_id=payment_id,
            razorpay_payment_status=None,
            error_code=error_code,
            error_description=error_desc,
            http_status_code=response.status_code,
            timestamp=now,
        )


# ══════════════════════════════════════════════════════════════════════════════
# execute_razorpay_payment — the single production execution gate
# ══════════════════════════════════════════════════════════════════════════════


def execute_razorpay_payment(
    pipeline_result: TransactionResult,
    buyer_agent_id: str,
    merchant_id: str,
    amount: Money,
    razorpay_client: RazorpayClient,
    originating_protocol: BuyerProtocol,
    notes: Optional[dict] = None,
) -> CommerceReceipt:
    """
    Execute a Razorpay payment order for an ALLOW-gated transaction.

    This is the ONLY production entry point for Razorpay execution.
    BLOCK, REVIEW, and UNDECIDED results are rejected before any HTTP call.
    There is no bypass function.

    After order creation, the next steps require client interaction:
    1. Pass the razorpay_order_id to Razorpay Checkout (browser/mobile).
    2. Client completes payment; Razorpay returns (order_id, payment_id, signature).
    3. Call verify_payment_and_capture() with those three values.

    Amount Safety:
        The amount MUST be the canonical CommerceRequest.calculated_total.
        This function additionally verifies amount_minor > 0.

    Returns:
        CommerceReceipt — always; never raises.
    """
    now = datetime.now(timezone.utc)
    transaction_id = pipeline_result.transaction_id

    # ── EXECUTION GATE: Only ALLOW reaches Razorpay ───────────────────────────
    if pipeline_result.decision != GatewayDecision.ALLOW:
        logger.warning(
            "Razorpay execution refused for txn=%r: decision=%r",
            transaction_id, pipeline_result.decision.value
        )
        return CommerceReceipt(
            transaction_id=transaction_id,
            merchant_id=merchant_id,
            buyer_agent_id=buyer_agent_id,
            final_amount=amount,
            payment_reference=None,
            status=ExecutionStatus.EXECUTION_REFUSED.value,
            timestamp=now,
            originating_protocol=originating_protocol,
            decision=pipeline_result.decision,
        )

    # ── Amount safety ─────────────────────────────────────────────────────────
    if amount.amount_minor <= 0:
        logger.error(
            "Razorpay execution refused for txn=%r: invalid amount %d",
            transaction_id, amount.amount_minor
        )
        return CommerceReceipt(
            transaction_id=transaction_id,
            merchant_id=merchant_id,
            buyer_agent_id=buyer_agent_id,
            final_amount=amount,
            payment_reference=None,
            status=ExecutionStatus.EXECUTION_REFUSED.value,
            timestamp=now,
            originating_protocol=originating_protocol,
            decision=GatewayDecision.BLOCK,
        )

    if not amount.currency or not amount.currency.strip():
        logger.error("Razorpay execution refused for txn=%r: empty currency", transaction_id)
        return CommerceReceipt(
            transaction_id=transaction_id,
            merchant_id=merchant_id,
            buyer_agent_id=buyer_agent_id,
            final_amount=amount,
            payment_reference=None,
            status=ExecutionStatus.EXECUTION_REFUSED.value,
            timestamp=now,
            originating_protocol=originating_protocol,
            decision=GatewayDecision.BLOCK,
        )

    logger.info(
        "Executing Razorpay order creation: txn=%r, amount=%d %s",
        transaction_id, amount.amount_minor, amount.currency
    )

    order_result = razorpay_client.create_order(
        amount_minor=amount.amount_minor,
        currency=amount.currency,
        receipt=transaction_id,
        notes=notes,
    )

    # Map result to CommerceReceipt
    if order_result.is_order_created():
        return CommerceReceipt(
            transaction_id=transaction_id,
            merchant_id=merchant_id,
            buyer_agent_id=buyer_agent_id,
            final_amount=amount,
            payment_reference=order_result.razorpay_order_id,
            status=ExecutionStatus.ORDER_CREATED.value,
            timestamp=now,
            originating_protocol=originating_protocol,
            decision=GatewayDecision.ALLOW,
        )

    if order_result.execution_status == ExecutionStatus.PAYMENT_CAPTURED:
        return CommerceReceipt(
            transaction_id=transaction_id,
            merchant_id=merchant_id,
            buyer_agent_id=buyer_agent_id,
            final_amount=amount,
            payment_reference=order_result.razorpay_payment_id or order_result.razorpay_order_id,
            status=ExecutionStatus.PAYMENT_CAPTURED.value,
            timestamp=now,
            originating_protocol=originating_protocol,
            decision=GatewayDecision.ALLOW,
        )

    if order_result.execution_status == ExecutionStatus.PAYMENT_AUTHORIZED:
        return CommerceReceipt(
            transaction_id=transaction_id,
            merchant_id=merchant_id,
            buyer_agent_id=buyer_agent_id,
            final_amount=amount,
            payment_reference=order_result.razorpay_payment_id or order_result.razorpay_order_id,
            status=ExecutionStatus.PAYMENT_AUTHORIZED.value,
            timestamp=now,
            originating_protocol=originating_protocol,
            decision=GatewayDecision.REVIEW,
        )

    # All failure states
    logger.error(
        "Razorpay execution failed for txn=%r: status=%r, error=%r",
        transaction_id, order_result.execution_status.value, order_result.error_code
    )
    return CommerceReceipt(
        transaction_id=transaction_id,
        merchant_id=merchant_id,
        buyer_agent_id=buyer_agent_id,
        final_amount=amount,
        payment_reference=order_result.razorpay_order_id,
        status=order_result.execution_status.value,
        timestamp=now,
        originating_protocol=originating_protocol,
        decision=GatewayDecision.BLOCK,
    )


def verify_payment_and_capture(
    razorpay_client: RazorpayClient,
    expected_order_id: str,
    razorpay_payment_id: str,
    razorpay_signature: str,
    amount_minor: int,
    currency: str,
    auto_captured: bool = False,
) -> RazorpayOrderResult:
    """
    Verify a payment signature and capture the payment if needed.

    This is the server-side step that follows client-side Razorpay checkout.
    It must be called with data received from the Razorpay Checkout callback —
    the gateway MUST use the expected_order_id from its own database, not
    merely trust the order_id returned from the client.

    Steps:
        1. Verify HMAC-SHA256 signature. Fail closed on mismatch.
        2. If auto_captured=True: treat as already captured (Razorpay auto-capture mode).
        3. If auto_captured=False: call POST /v1/payments/{id}/capture.

    Args:
        razorpay_client:      An initialized RazorpayClient.
        expected_order_id:    The order_id from our gateway's DB (NOT from client).
        razorpay_payment_id:  The payment_id from the Razorpay checkout callback.
        razorpay_signature:   The signature from the Razorpay checkout callback.
        amount_minor:         The authorized amount to capture (from our DB).
        currency:             The currency to capture (from our DB).
        auto_captured:        If True, Razorpay already captured automatically.

    Returns:
        RazorpayOrderResult — always; never raises.
    """
    now = datetime.now(timezone.utc)

    # ── Step 1: Verify signature — fail closed ────────────────────────────────
    signature_valid = razorpay_client.verify_payment_signature(
        razorpay_order_id=expected_order_id,
        razorpay_payment_id=razorpay_payment_id,
        razorpay_signature=razorpay_signature,
    )

    if not signature_valid:
        logger.warning(
            "Payment verification failed for order_id=%r payment_id=%r — "
            "signature mismatch. Rejecting.",
            expected_order_id, razorpay_payment_id
        )
        return RazorpayOrderResult(
            execution_status=ExecutionStatus.SIGNATURE_INVALID,
            razorpay_order_id=expected_order_id,
            razorpay_order_status=None,
            razorpay_amount=None,
            razorpay_currency=None,
            razorpay_receipt=None,
            razorpay_payment_id=razorpay_payment_id,
            razorpay_payment_status=None,
            error_code="SIGNATURE_MISMATCH",
            error_description=(
                "HMAC-SHA256 signature verification failed. "
                "Payment rejected — cannot trust this payment_id."
            ),
            http_status_code=None,
            timestamp=now,
        )

    # ── Step 2: Handle auto-captured or manually capture ──────────────────────
    if auto_captured:
        logger.info(
            "Payment auto-captured by Razorpay: order_id=%r payment_id=%r",
            expected_order_id, razorpay_payment_id
        )
        return RazorpayOrderResult(
            execution_status=ExecutionStatus.PAYMENT_CAPTURED,
            razorpay_order_id=expected_order_id,
            razorpay_order_status="paid",
            razorpay_amount=amount_minor,
            razorpay_currency=currency,
            razorpay_receipt=None,
            razorpay_payment_id=razorpay_payment_id,
            razorpay_payment_status="captured",
            error_code=None,
            error_description=None,
            http_status_code=None,
            timestamp=now,
        )

    payment_details = razorpay_client.fetch_payment(razorpay_payment_id)
    if not payment_details:
        return RazorpayOrderResult(
            execution_status=ExecutionStatus.NETWORK_ERROR,
            razorpay_order_id=expected_order_id,
            razorpay_order_status=None,
            razorpay_amount=amount_minor,
            razorpay_currency=currency,
            razorpay_receipt=None,
            razorpay_payment_id=razorpay_payment_id,
            razorpay_payment_status=None,
            error_code="FETCH_FAILED",
            error_description="Could not retrieve payment status from Razorpay.",
            http_status_code=None,
            timestamp=now,
        )

    actual_status = payment_details.get("status")

    if actual_status == "captured":
        logger.info(
            "Payment already captured by Razorpay: order_id=%r payment_id=%r",
            expected_order_id, razorpay_payment_id
        )
        return RazorpayOrderResult(
            execution_status=ExecutionStatus.PAYMENT_CAPTURED,
            razorpay_order_id=expected_order_id,
            razorpay_order_status="paid",
            razorpay_amount=amount_minor,
            razorpay_currency=currency,
            razorpay_receipt=None,
            razorpay_payment_id=razorpay_payment_id,
            razorpay_payment_status="captured",
            error_code=None,
            error_description=None,
            http_status_code=200,
            timestamp=now,
        )
    elif actual_status == "authorized":
        # Manual capture
        return razorpay_client.capture_payment(
            payment_id=razorpay_payment_id,
            amount_minor=amount_minor,
            currency=currency,
            expected_order_id=expected_order_id,
        )
    else:
        logger.error(
            "Payment cannot be captured: order_id=%r payment_id=%r status=%r",
            expected_order_id, razorpay_payment_id, actual_status
        )
        return RazorpayOrderResult(
            execution_status=ExecutionStatus.PAYMENT_FAILED,
            razorpay_order_id=expected_order_id,
            razorpay_order_status=None,
            razorpay_amount=amount_minor,
            razorpay_currency=currency,
            razorpay_receipt=None,
            razorpay_payment_id=razorpay_payment_id,
            razorpay_payment_status=actual_status,
            error_code="INVALID_PAYMENT_STATUS",
            error_description=f"Payment status is {actual_status!r}, expected 'authorized'.",
            http_status_code=200,
            timestamp=now,
        )
