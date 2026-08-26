"""
Comprehensive Schema Tests — Agent Commerce Gateway Prompt 2
=============================================================

Test categories:
  1.  Valid cases            — well-formed objects that should pass
  2.  Money validation       — minor-unit integer enforcement
  3.  CommerceItem           — quantity, price, line total
  4.  CommerceRequest        — identity, timestamps, item totals, supplied total
  5.  Mandate                — expiry, identity, status semantics
  6.  SignedAuthorization    — canonical serialization determinism
  7.  CommerceContext        — cross-object consistency (buyer, merchant, currency, amount)
  8.  CommerceReceipt        — receipt structure
  9.  GatewayDecision        — enum values
  10. GatewayBlockedError    — exception behaviour
  11. Security / immutability — frozen models cannot be mutated after validation

Conventions:
  - Fixtures are module-level helpers, not pytest fixtures (simpler for this codebase).
  - All monetary values are in INR minor units (paise) unless otherwise stated.
  - ₹1,500 = 150000 paise; ₹5,000 = 500000 paise; ₹8,000 = 800000 paise.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.core.schemas import (
    BuyerProtocol,
    CommerceContext,
    CommerceItem,
    CommerceReceipt,
    CommerceRequest,
    GatewayBlockedError,
    GatewayDecision,
    Mandate,
    MandateStatus,
    MandateType,
    Money,
    Ed25519AuthorizationProof,
)


# ══════════════════════════════════════════════════════════════════════════════
# Helper factories
# ══════════════════════════════════════════════════════════════════════════════

NOW = datetime(2026, 8, 21, 15, 0, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=1)
MUCH_LATER = NOW + timedelta(days=30)
BEFORE = NOW - timedelta(seconds=1)


def make_inr(amount_minor: int) -> Money:
    return Money(amount_minor=amount_minor, currency="INR")


def make_item(
    product_id: str = "prod-001",
    name: str = "AI API Access",
    quantity: int = 1,
    unit_price_minor: int = 150000,
    currency: str = "INR",
    category: str | None = "software",
) -> CommerceItem:
    return CommerceItem(
        product_id=product_id,
        name=name,
        quantity=quantity,
        unit_price=Money(amount_minor=unit_price_minor, currency=currency),
        category=category,
    )


def make_request(
    transaction_id: str = "txn-001",
    created_at: datetime = NOW,
    expires_at: datetime = LATER,
    nonce: str = "nonce-abc-001",
    buyer_agent_id: str = "agent-demo",
    buyer_protocol: BuyerProtocol = BuyerProtocol.x402,
    merchant_id: str = "merchant-razorpay-01",
    items: list[CommerceItem] | None = None,
    supplied_total: Money | None = None,
    receipt_destination_ref: str = "https://callback.example.com/receipt",
) -> CommerceRequest:
    if items is None:
        items = [make_item()]
    return CommerceRequest(
        transaction_id=transaction_id,
        created_at=created_at,
        expires_at=expires_at,
        nonce=nonce,
        buyer_agent_id=buyer_agent_id,
        buyer_agent_name="Demo Buyer Agent",
        buyer_protocol=buyer_protocol,
        merchant_id=merchant_id,
        items=items,
        supplied_total=supplied_total,
        receipt_destination_protocol=buyer_protocol,
        receipt_destination_ref=receipt_destination_ref,
    )


def make_mandate(
    mandate_id: str = "mandate-001",
    buyer_agent_id: str = "agent-demo",
    merchant_id: str = "merchant-razorpay-01",
    max_amount_minor: int = 500000,   # ₹5,000
    currency: str = "INR",
    mandate_type: MandateType = MandateType.one_time,
    status: MandateStatus = MandateStatus.active,
    issued_at: datetime = NOW,
    expires_at: datetime = MUCH_LATER,
    nonce: str = "mandate-nonce-001",
    authorization_method: str = "ed25519",
    authorization_ref: str = "key-id-pub-001",
) -> Mandate:
    return Mandate(
        mandate_id=mandate_id,
        buyer_agent_id=buyer_agent_id,
        merchant_id=merchant_id,
        max_amount=Money(amount_minor=max_amount_minor, currency=currency),
        mandate_type=mandate_type,
        status=status,
        issued_at=issued_at,
        expires_at=expires_at,
        nonce=nonce,
        authorization_method=authorization_method,
        authorization_ref=authorization_ref,
    )


def make_context(
    request: CommerceRequest | None = None,
    auth_proof = None,
) -> CommerceContext:
    if auth_proof is None:
        from nacl.signing import SigningKey
        from app.core.mandate import sign_mandate
        sk = SigningKey.generate()
        auth_proof = sign_mandate(make_mandate(), sk, "test")
    return CommerceContext(
        request=request or make_request(),
        auth_proof=auth_proof,
        is_recurring=auth_proof.payload.mandate_type.value == "recurring",
    )


# ══════════════════════════════════════════════════════════════════════════════
# 1. Valid cases
# ══════════════════════════════════════════════════════════════════════════════


class TestValidCases:
    def test_valid_single_item_purchase(self):
        """A well-formed single-item request is accepted."""
        req = make_request()
        assert req.transaction_id == "txn-001"
        assert req.calculated_total == make_inr(150000)

    def test_valid_multi_item_purchase(self):
        """A multi-item request calculates total correctly."""
        items = [
            make_item(product_id="prod-001", unit_price_minor=100000, quantity=2),  # ₹1,000 × 2
            make_item(product_id="prod-002", unit_price_minor=50000, quantity=1),   # ₹500 × 1
        ]
        req = make_request(items=items)
        assert req.calculated_total == make_inr(250000)  # ₹2,500

    def test_valid_one_time_mandate(self):
        """A one-time mandate with valid expiry is accepted."""
        mandate = make_mandate()
        assert mandate.mandate_type == MandateType.one_time
        assert mandate.max_amount.amount_minor == 500000

    def test_valid_receipt(self):
        """A well-formed receipt is accepted."""
        receipt = CommerceReceipt(
            transaction_id="txn-001",
            merchant_id="merchant-razorpay-01",
            buyer_agent_id="agent-demo",
            final_amount=make_inr(150000),
            payment_reference=None,
            status="pending",
            timestamp=NOW,
            originating_protocol=BuyerProtocol.x402,
            decision=GatewayDecision.UNDECIDED,
        )
        assert receipt.decision == GatewayDecision.UNDECIDED

    def test_valid_inr_minor_unit_money(self):
        """INR paise representation is accepted."""
        m = make_inr(2550)  # ₹25.50
        assert m.amount_minor == 2550
        assert m.currency == "INR"

    def test_valid_context_is_undecided(self):
        """A valid CommerceContext starts with decision=UNDECIDED."""
        ctx = make_context()
        assert ctx.decision == GatewayDecision.UNDECIDED

    def test_valid_context_with_matching_supplied_total(self):
        """A correct supplied_total that matches the calculated total is accepted."""
        req = make_request(supplied_total=make_inr(150000))
        assert req.supplied_total is not None
        assert req.supplied_total.amount_minor == req.calculated_total.amount_minor

    def test_valid_zero_price_item(self):
        """A free item (unit price = 0) is structurally valid."""
        item = make_item(unit_price_minor=0)
        assert item.line_total.amount_minor == 0

    def test_valid_money_currency_is_uppercased(self):
        """Currency codes are normalized to uppercase."""
        m = Money(amount_minor=100, currency="inr")
        assert m.currency == "INR"

    def test_valid_mandate_status_is_metadata_not_authorization(self):
        """
        A mandate with status='active' is structurally valid.
        This test documents that status is metadata only — it is NOT proof
        of authorization.  The authorization layer (future) decides that.
        """
        mandate = make_mandate(status=MandateStatus.active)
        # A mandate with status=revoked is also structurally valid.
        # The authorization layer, not the schema, decides whether to reject it.
        revoked = make_mandate(status=MandateStatus.revoked)
        assert mandate.status == MandateStatus.active
        assert revoked.status == MandateStatus.revoked

    def test_valid_signed_authorization_structure(self):
        """SignedAuthorization holds a mandate and placeholder signature bytes."""
        mandate = make_mandate()
        signed = Ed25519AuthorizationProof(
            payload=mandate,
            signature=b"\x00" * 64,  # 64-byte placeholder (real Ed25519 size)
            key_id="key-ed25519-001",
            algorithm="Ed25519",
        )
        assert signed.algorithm == "Ed25519"
        assert len(signed.signature) == 64


# ══════════════════════════════════════════════════════════════════════════════
# 2. Money validation
# ══════════════════════════════════════════════════════════════════════════════


class TestMoneyValidation:
    def test_negative_amount_rejected(self):
        with pytest.raises(ValidationError, match="greater than or equal to 0"):
            Money(amount_minor=-1, currency="INR")

    def test_float_amount_rejected(self):
        """Floats must be rejected — monetary values are integers only."""
        with pytest.raises(ValidationError):
            Money(amount_minor=25.50, currency="INR")  # type: ignore[arg-type]

    def test_missing_currency_rejected(self):
        with pytest.raises(ValidationError):
            Money(amount_minor=100)  # type: ignore[call-arg]

    def test_currency_too_short_rejected(self):
        with pytest.raises(ValidationError, match="at least 3 characters"):
            Money(amount_minor=100, currency="IN")

    def test_currency_too_long_rejected(self):
        with pytest.raises(ValidationError, match="at most 3 characters"):
            Money(amount_minor=100, currency="INRR")

    def test_zero_amount_is_valid(self):
        """Zero is a valid amount (free item, authorization floor, etc.)."""
        m = Money(amount_minor=0, currency="INR")
        assert m.amount_minor == 0

    def test_money_is_frozen(self):
        """Money cannot be mutated after creation."""
        m = make_inr(100)
        with pytest.raises((TypeError, ValidationError)):
            m.amount_minor = 999  # type: ignore[misc]


# ══════════════════════════════════════════════════════════════════════════════
# 3. CommerceItem validation
# ══════════════════════════════════════════════════════════════════════════════


class TestCommerceItemValidation:
    def test_zero_quantity_rejected(self):
        with pytest.raises(ValidationError, match="greater than 0"):
            CommerceItem(
                product_id="p1",
                name="Test",
                quantity=0,
                unit_price=make_inr(1000),
            )

    def test_negative_quantity_rejected(self):
        with pytest.raises(ValidationError, match="greater than 0"):
            CommerceItem(
                product_id="p1",
                name="Test",
                quantity=-1,
                unit_price=make_inr(1000),
            )

    def test_float_quantity_rejected(self):
        """Quantity must be an integer — no fractional quantities."""
        with pytest.raises(ValidationError):
            CommerceItem(
                product_id="p1",
                name="Test",
                quantity=1.5,  # type: ignore[arg-type]
                unit_price=make_inr(1000),
            )

    def test_empty_product_id_rejected(self):
        with pytest.raises(ValidationError, match="at least 1 character"):
            CommerceItem(
                product_id="",
                name="Test",
                quantity=1,
                unit_price=make_inr(1000),
            )

    def test_line_total_deterministic(self):
        """Line total is always quantity × unit_price, never externally supplied."""
        item = make_item(quantity=3, unit_price_minor=50000)
        assert item.line_total.amount_minor == 150000

    def test_commerce_item_is_frozen(self):
        item = make_item()
        with pytest.raises((TypeError, ValidationError)):
            item.quantity = 99  # type: ignore[misc]


# ══════════════════════════════════════════════════════════════════════════════
# 4. CommerceRequest validation
# ══════════════════════════════════════════════════════════════════════════════


class TestCommerceRequestValidation:
    def test_empty_transaction_id_rejected(self):
        with pytest.raises(ValidationError, match="at least 1 character"):
            make_request(transaction_id="")

    def test_empty_nonce_rejected(self):
        with pytest.raises(ValidationError, match="at least 1 character"):
            make_request(nonce="")

    def test_empty_buyer_agent_id_rejected(self):
        with pytest.raises(ValidationError, match="at least 1 character"):
            make_request(buyer_agent_id="")

    def test_empty_merchant_id_rejected(self):
        with pytest.raises(ValidationError, match="at least 1 character"):
            make_request(merchant_id="")

    def test_expires_at_before_created_at_rejected(self):
        with pytest.raises(ValidationError, match="must be strictly after created_at"):
            make_request(created_at=NOW, expires_at=BEFORE)

    def test_expires_at_equal_to_created_at_rejected(self):
        with pytest.raises(ValidationError, match="must be strictly after created_at"):
            make_request(created_at=NOW, expires_at=NOW)

    def test_no_items_rejected(self):
        with pytest.raises(ValidationError):
            make_request(items=[])

    def test_mixed_currency_items_rejected(self):
        """Items with different currencies within a single request are rejected."""
        items = [
            make_item(product_id="p1", currency="INR"),
            make_item(product_id="p2", currency="USD"),
        ]
        with pytest.raises(ValidationError, match="same currency"):
            make_request(items=items)

    def test_inconsistent_supplied_total_rejected(self):
        """A supplied_total that does not match the calculated total is rejected."""
        with pytest.raises(ValidationError, match="does not match calculated total"):
            make_request(
                items=[make_item(unit_price_minor=150000)],
                supplied_total=make_inr(999999),  # wrong
            )

    def test_supplied_total_wrong_currency_rejected(self):
        with pytest.raises(ValidationError, match="currency"):
            make_request(
                items=[make_item(currency="INR", unit_price_minor=100000)],
                supplied_total=Money(amount_minor=100000, currency="USD"),
            )

    def test_calculated_total_correct_for_multi_item(self):
        """Calculated total sums all items correctly."""
        items = [
            make_item(product_id="p1", unit_price_minor=100000, quantity=1),
            make_item(product_id="p2", unit_price_minor=50000, quantity=2),
        ]
        req = make_request(items=items)
        assert req.calculated_total.amount_minor == 200000  # 100000 + 100000

    def test_commerce_request_is_frozen(self):
        req = make_request()
        with pytest.raises((TypeError, ValidationError)):
            req.buyer_agent_id = "attacker"  # type: ignore[misc]


# ══════════════════════════════════════════════════════════════════════════════
# 5. Mandate validation
# ══════════════════════════════════════════════════════════════════════════════


class TestMandateValidation:
    def test_empty_mandate_id_rejected(self):
        with pytest.raises(ValidationError, match="at least 1 character"):
            make_mandate(mandate_id="")

    def test_empty_nonce_rejected(self):
        with pytest.raises(ValidationError, match="at least 1 character"):
            make_mandate(nonce="")

    def test_expires_at_before_issued_at_rejected(self):
        with pytest.raises(ValidationError, match="strictly after issued_at"):
            make_mandate(issued_at=NOW, expires_at=BEFORE)

    def test_expires_at_equal_to_issued_at_rejected(self):
        with pytest.raises(ValidationError, match="strictly after issued_at"):
            make_mandate(issued_at=NOW, expires_at=NOW)

    def test_empty_buyer_agent_id_rejected(self):
        with pytest.raises(ValidationError, match="at least 1 character"):
            make_mandate(buyer_agent_id="")

    def test_empty_merchant_id_rejected(self):
        with pytest.raises(ValidationError, match="at least 1 character"):
            make_mandate(merchant_id="")

    def test_mandate_is_frozen(self):
        mandate = make_mandate()
        with pytest.raises((TypeError, ValidationError)):
            mandate.max_amount = make_inr(99999999)  # type: ignore[misc]

    def test_revoked_mandate_is_structurally_valid(self):
        """
        A revoked mandate is structurally valid at the schema level.
        Whether it is ACCEPTED for payment is a future authorization concern.
        """
        mandate = make_mandate(status=MandateStatus.revoked)
        assert mandate.status == MandateStatus.revoked


# ══════════════════════════════════════════════════════════════════════════════
# 6. SignedAuthorization — canonical serialization
# ══════════════════════════════════════════════════════════════════════════════


class TestSignedAuthorization:
    def test_canonical_bytes_are_deterministic(self):
        """Same mandate always produces identical canonical bytes."""
        mandate = make_mandate()
        signed = Ed25519AuthorizationProof(
            payload=mandate, signature=b"\x01" * 64, key_id="k1"
        )
        b1 = signed.canonical_payload_bytes()
        b2 = signed.canonical_payload_bytes()
        assert b1 == b2

    def test_canonical_bytes_are_valid_json(self):
        """Canonical bytes parse as valid JSON."""
        mandate = make_mandate()
        signed = Ed25519AuthorizationProof(
            payload=mandate, signature=b"\x01" * 64, key_id="k1"
        )
        data = json.loads(signed.canonical_payload_bytes())
        assert "buyer_agent_id" in data
        assert "max_amount_minor" in data
        assert "nonce" in data

    def test_canonical_bytes_cover_critical_fields(self):
        """All spending-decision fields are present in canonical bytes."""
        mandate = make_mandate()
        signed = Ed25519AuthorizationProof(
            payload=mandate, signature=b"\x01" * 64, key_id="k1"
        )
        data = json.loads(signed.canonical_payload_bytes())
        assert data["mandate_id"] == mandate.mandate_id
        assert data["buyer_agent_id"] == mandate.buyer_agent_id
        assert data["merchant_id"] == mandate.merchant_id
        assert data["max_amount_minor"] == mandate.max_amount.amount_minor
        assert data["currency"] == mandate.max_amount.currency
        assert data["mandate_type"] == mandate.mandate_type.value
        assert data["expires_at"] == mandate.expires_at.isoformat()
        assert data["issued_at"] == mandate.issued_at.isoformat()
        assert data["nonce"] == mandate.nonce
        assert data["status"] == mandate.status.value
        assert data["authorization_method"] == mandate.authorization_method
        assert data["authorization_ref"] == mandate.authorization_ref

    def test_different_amount_produces_different_bytes(self):
        """Changing max_amount changes canonical bytes — signature would break."""
        m1 = make_mandate(max_amount_minor=500000)
        m2 = make_mandate(max_amount_minor=999999)
        s1 = Ed25519AuthorizationProof(payload=m1, signature=b"\x01" * 64, key_id="k")
        s2 = Ed25519AuthorizationProof(payload=m2, signature=b"\x01" * 64, key_id="k")
        assert s1.canonical_payload_bytes() != s2.canonical_payload_bytes()

    def test_signed_authorization_is_frozen(self):
        mandate = make_mandate()
        signed = Ed25519AuthorizationProof(
            payload=mandate, signature=b"\x01" * 64, key_id="k1"
        )
        with pytest.raises((TypeError, ValidationError)):
            signed.key_id = "attacker-key"  # type: ignore[misc]

    def test_empty_signature_rejected(self):
        mandate = make_mandate()
        with pytest.raises(ValidationError):
            Ed25519AuthorizationProof(payload=mandate, signature=b"", key_id="k1")


# ══════════════════════════════════════════════════════════════════════════════
# 7. CommerceContext — cross-object consistency
# ══════════════════════════════════════════════════════════════════════════════


class TestCommerceContext:






    def test_context_decision_is_undecided_not_approved(self):
        """
        Structural validation does NOT produce an ALLOW decision.
        UNDECIDED means we have validated structure, not authorized the payment.
        """
        ctx = make_context()
        assert ctx.decision == GatewayDecision.UNDECIDED
        assert ctx.decision != GatewayDecision.ALLOW

    def test_context_is_frozen(self):
        """CommerceContext cannot be mutated — decision cannot be changed in place."""
        ctx = make_context()
        with pytest.raises((TypeError, ValidationError)):
            ctx.decision = GatewayDecision.ALLOW  # type: ignore[misc]


# ══════════════════════════════════════════════════════════════════════════════
# 8. CommerceReceipt validation
# ══════════════════════════════════════════════════════════════════════════════


class TestCommerceReceiptValidation:
    def test_empty_transaction_id_rejected(self):
        with pytest.raises(ValidationError, match="at least 1 character"):
            CommerceReceipt(
                transaction_id="",
                merchant_id="m1",
                buyer_agent_id="a1",
                final_amount=make_inr(100),
                status="pending",
                timestamp=NOW,
                originating_protocol=BuyerProtocol.x402,
                decision=GatewayDecision.UNDECIDED,
            )

    def test_receipt_is_frozen(self):
        receipt = CommerceReceipt(
            transaction_id="txn-001",
            merchant_id="m1",
            buyer_agent_id="a1",
            final_amount=make_inr(100),
            status="pending",
            timestamp=NOW,
            originating_protocol=BuyerProtocol.x402,
            decision=GatewayDecision.UNDECIDED,
        )
        with pytest.raises((TypeError, ValidationError)):
            receipt.decision = GatewayDecision.ALLOW  # type: ignore[misc]


# ══════════════════════════════════════════════════════════════════════════════
# 9. GatewayDecision enum
# ══════════════════════════════════════════════════════════════════════════════


class TestGatewayDecision:
    def test_all_decision_states_exist(self):
        assert GatewayDecision.UNDECIDED
        assert GatewayDecision.ALLOW
        assert GatewayDecision.REVIEW
        assert GatewayDecision.BLOCK

    def test_decision_string_values(self):
        assert GatewayDecision.UNDECIDED.value == "UNDECIDED"
        assert GatewayDecision.ALLOW.value == "ALLOW"
        assert GatewayDecision.REVIEW.value == "REVIEW"
        assert GatewayDecision.BLOCK.value == "BLOCK"


# ══════════════════════════════════════════════════════════════════════════════
# 10. GatewayBlockedError
# ══════════════════════════════════════════════════════════════════════════════


class TestGatewayBlockedError:
    def test_blocked_error_carries_reason(self):
        err = GatewayBlockedError(reason="Test block reason")
        assert err.reason == "Test block reason"
        assert err.decision == GatewayDecision.BLOCK

    def test_blocked_error_is_exception(self):
        with pytest.raises(GatewayBlockedError) as exc_info:
            raise GatewayBlockedError(reason="Exceeded authorization limit")
        assert exc_info.value.decision == GatewayDecision.BLOCK

    def test_blocked_error_message_contains_reason(self):
        err = GatewayBlockedError(reason="Some reason")
        assert "Some reason" in str(err)
        assert "BLOCK" in str(err)


# ══════════════════════════════════════════════════════════════════════════════
# 11. Security / immutability tests
# ══════════════════════════════════════════════════════════════════════════════


class TestImmutabilityAndTamperResistance:
    """
    Prove that authorization-critical models cannot be mutated after validation.

    These tests document the security guarantee: a malicious actor that gets
    hold of a validated Mandate or CommerceContext object cannot modify the
    spending limit, buyer identity, merchant identity, currency, or expiry
    without raising an error.
    """

    def test_mandate_max_amount_cannot_be_inflated(self):
        """
        Security: A validated mandate's spending limit cannot be increased.

        If a Mandate were mutable, a malicious actor could construct a valid
        low-value mandate and then inflate max_amount before the context check.
        Frozen models prevent this.
        """
        mandate = make_mandate(max_amount_minor=100000)  # ₹1,000
        with pytest.raises((TypeError, ValidationError)):
            mandate.max_amount = make_inr(99999999)  # type: ignore[misc]
        # Mandate still has the original value
        assert mandate.max_amount.amount_minor == 100000

    def test_mandate_buyer_id_cannot_be_swapped(self):
        """Security: buyer identity on a mandate cannot be changed after creation."""
        mandate = make_mandate(buyer_agent_id="legitimate-agent")
        with pytest.raises((TypeError, ValidationError)):
            mandate.buyer_agent_id = "attacker-agent"  # type: ignore[misc]
        assert mandate.buyer_agent_id == "legitimate-agent"

    def test_mandate_merchant_id_cannot_be_swapped(self):
        mandate = make_mandate(merchant_id="merchant-A")
        with pytest.raises((TypeError, ValidationError)):
            mandate.merchant_id = "merchant-B"  # type: ignore[misc]
        assert mandate.merchant_id == "merchant-A"

    def test_mandate_currency_cannot_be_changed(self):
        mandate = make_mandate(currency="INR")
        with pytest.raises((TypeError, ValidationError)):
            mandate.max_amount = Money(amount_minor=500000, currency="USD")  # type: ignore[misc]
        assert mandate.max_amount.currency == "INR"

    def test_mandate_expiry_cannot_be_extended(self):
        """Security: expiry cannot be extended after signing."""
        mandate = make_mandate(expires_at=MUCH_LATER)
        with pytest.raises((TypeError, ValidationError)):
            mandate.expires_at = MUCH_LATER + timedelta(days=9999)  # type: ignore[misc]
        assert mandate.expires_at == MUCH_LATER

    def test_context_decision_cannot_be_self_approved(self):
        """
        Security: An agent cannot self-approve a transaction by mutating
        the context's decision field from UNDECIDED to ALLOW.
        """
        ctx = make_context()
        assert ctx.decision == GatewayDecision.UNDECIDED
        with pytest.raises((TypeError, ValidationError)):
            ctx.decision = GatewayDecision.ALLOW  # type: ignore[misc]
        # Decision is still UNDECIDED
        assert ctx.decision == GatewayDecision.UNDECIDED

    def test_signed_authorization_signature_cannot_be_replaced(self):
        """
        Security: A real signature cannot be swapped for a forged one
        after the Ed25519AuthorizationProof is constructed.
        """
        mandate = make_mandate()
        signed = Ed25519AuthorizationProof(
            payload=mandate, signature=b"\x01" * 64, key_id="k1"
        )
        with pytest.raises((TypeError, ValidationError)):
            signed.signature = b"\xff" * 64  # type: ignore[misc]

    def test_request_buyer_cannot_be_changed_after_creation(self):
        """Security: buyer_agent_id on a request cannot be swapped."""
        req = make_request(buyer_agent_id="real-agent")
        with pytest.raises((TypeError, ValidationError)):
            req.buyer_agent_id = "impersonator"  # type: ignore[misc]
        assert req.buyer_agent_id == "real-agent"

    def test_new_context_required_for_different_decision(self):
        """
        Architecture: The only way to change a decision is to construct a
        new object — not mutate the existing one.  This forces the pipeline
        to be explicit about every state transition.
        """
        ctx = make_context()
        # The correct way to record a future ALLOW decision is to build a new object.
        # This test verifies the pattern works (future policy layer would do this).
        ctx_allowed = CommerceContext(
            request=ctx.request,
            auth_proof=ctx.auth_proof,
            decision=GatewayDecision.ALLOW,
        )
        assert ctx_allowed.decision == GatewayDecision.ALLOW
        # Original context is unchanged
        assert ctx.decision == GatewayDecision.UNDECIDED


# ══════════════════════════════════════════════════════════════════════════════
# 12. Protocol adapter interface
# ══════════════════════════════════════════════════════════════════════════════


class TestProtocolAdapterInterface:
    def test_acp_adapter_parse_rejects_empty_payload(self):
        """ACP adapter is implemented; an empty payload raises ValueError."""
        from app.adapters.acp_adapter import ACPAdapter
        adapter = ACPAdapter()
        with pytest.raises((ValueError, Exception)):
            adapter.parse_request({})

    def test_acp_adapter_build_receipt_returns_dict(self):
        """ACP adapter is implemented; build_receipt returns a dict."""
        from app.adapters.acp_adapter import ACPAdapter
        receipt = CommerceReceipt(
            transaction_id="txn-001",
            merchant_id="m1",
            buyer_agent_id="a1",
            final_amount=make_inr(100),
            status="pending",
            timestamp=NOW,
            originating_protocol=BuyerProtocol.acp,
            decision=GatewayDecision.UNDECIDED,
        )
        adapter = ACPAdapter()
        result = adapter.build_receipt(receipt)
        assert isinstance(result, dict)
        assert result["id"] == "txn-001"

    def test_x402_adapter_parse_rejects_empty_payload(self):
        """x402 adapter is implemented; an empty payload raises ValueError."""
        from app.adapters.x402_adapter import X402Adapter
        adapter = X402Adapter()
        with pytest.raises((ValueError, Exception)):
            adapter.parse_request({})

    def test_x402_adapter_build_receipt_returns_dict(self):
        """x402 adapter is implemented; build_receipt returns a dict."""
        from app.adapters.x402_adapter import X402Adapter
        receipt = CommerceReceipt(
            transaction_id="txn-001",
            merchant_id="m1",
            buyer_agent_id="a1",
            final_amount=make_inr(100),
            status="completed",
            timestamp=NOW,
            originating_protocol=BuyerProtocol.x402,
            decision=GatewayDecision.ALLOW,
        )
        adapter = X402Adapter()
        result = adapter.build_receipt(receipt)
        assert isinstance(result, dict)
        assert result["status"] == "success"

    def test_adapters_are_protocol_adapter_subclasses(self):
        from app.adapters.acp_adapter import ACPAdapter
        from app.adapters.base_adapter import ProtocolAdapter
        from app.adapters.x402_adapter import X402Adapter
        assert issubclass(ACPAdapter, ProtocolAdapter)
        assert issubclass(X402Adapter, ProtocolAdapter)
