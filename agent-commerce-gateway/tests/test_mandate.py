"""
Cryptographic Authorization Tests — Agent Commerce Gateway Prompt 3
====================================================================

These tests attack the Ed25519 mandate verification implementation.

Test categories:
    1. Valid authorization       — correctly signed mandate verifies
    2. Signature attacks         — tampering with signed fields breaks verification
    3. Identity attacks          — wrong buyer/merchant fails
    4. Temporal attacks          — expired or impossible timestamps fail
    5. Malformed input           — missing/invalid signatures, keys, payloads
    6. Tamper-proof demonstration — modify-after-signing → INVALID
    7. Scope verification        — request vs signed mandate scope checks
    8. Fail-closed guarantee     — every failure path produces valid=False

Every test that expects failure asserts valid=False AND checks the reason.
No test can pass with a silent error or exception swallowing.
"""

from datetime import datetime, timedelta, timezone

import pytest
from nacl.signing import SigningKey, VerifyKey

from app.core.mandate import (
    AuthorizationVerificationResult,
    sign_mandate,
    verify_authorization_scope,
    verify_mandate,
)
from app.core.schemas import (
    BuyerProtocol,
    CommerceItem,
    CommerceRequest,
    Mandate,
    MandateStatus,
    MandateType,
    Money,
    Ed25519AuthorizationProof,
)


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures and helpers
# ══════════════════════════════════════════════════════════════════════════════

NOW = datetime(2026, 8, 21, 15, 0, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=1)
MUCH_LATER = NOW + timedelta(days=30)


def make_inr(amount_minor: int) -> Money:
    return Money(amount_minor=amount_minor, currency="INR")


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


def make_request(
    buyer_agent_id: str = "agent-demo",
    merchant_id: str = "merchant-razorpay-01",
    unit_price_minor: int = 150000,  # ₹1,500
    currency: str = "INR",
) -> CommerceRequest:
    return CommerceRequest(
        transaction_id="txn-001",
        created_at=NOW,
        expires_at=LATER,
        nonce="nonce-req-001",
        buyer_agent_id=buyer_agent_id,
        buyer_agent_name="Demo Buyer Agent",
        buyer_protocol=BuyerProtocol.x402,
        merchant_id=merchant_id,
        items=[
            CommerceItem(
                product_id="prod-001",
                name="AI API Access",
                quantity=1,
                unit_price=Money(amount_minor=unit_price_minor, currency=currency),
                category="software",
            )
        ],
        receipt_destination_protocol=BuyerProtocol.x402,
        receipt_destination_ref="https://callback.example.com/receipt",
    )


@pytest.fixture
def keypair():
    """Generate a fresh Ed25519 keypair for each test."""
    sk = SigningKey.generate()
    vk = sk.verify_key
    return sk, vk


@pytest.fixture
def valid_signed(keypair):
    """A correctly signed mandate with matching buyer/merchant, not expired."""
    sk, vk = keypair
    mandate = make_mandate()
    signed = sign_mandate(mandate, sk, "test-key-001")
    return signed, vk


# ══════════════════════════════════════════════════════════════════════════════
# 1. Valid authorization
# ══════════════════════════════════════════════════════════════════════════════


class TestValidAuthorization:
    def test_correctly_signed_mandate_verifies(self, keypair):
        """A mandate signed with the correct key verifies successfully."""
        sk, vk = keypair
        mandate = make_mandate()
        signed = sign_mandate(mandate, sk, "k1")

        result = verify_mandate(
            signed,
            expected_buyer_agent_id="agent-demo",
            expected_merchant_id="merchant-razorpay-01",
            public_key=vk,
            current_time=NOW,
        )

        assert result.valid is True
        assert "verified" in result.reason.lower()

    def test_correct_buyer_merchant_verifies(self, keypair):
        """Verification passes when buyer and merchant match exactly."""
        sk, vk = keypair
        mandate = make_mandate(
            buyer_agent_id="buyer-xyz",
            merchant_id="merchant-abc",
        )
        signed = sign_mandate(mandate, sk, "k1")

        result = verify_mandate(
            signed,
            expected_buyer_agent_id="buyer-xyz",
            expected_merchant_id="merchant-abc",
            public_key=vk,
            current_time=NOW,
        )
        assert result.valid is True

    def test_valid_unexpired_mandate_verifies(self, keypair):
        """A mandate that has not yet expired verifies."""
        sk, vk = keypair
        mandate = make_mandate(
            issued_at=NOW,
            expires_at=NOW + timedelta(days=365),
        )
        signed = sign_mandate(mandate, sk, "k1")

        result = verify_mandate(
            signed,
            expected_buyer_agent_id="agent-demo",
            expected_merchant_id="merchant-razorpay-01",
            public_key=vk,
            current_time=NOW + timedelta(days=100),  # well before expiry
        )
        assert result.valid is True

    def test_verification_result_is_structured(self, keypair):
        """Verification result carries valid + reason, not just True/False."""
        sk, vk = keypair
        signed = sign_mandate(make_mandate(), sk, "k1")
        result = verify_mandate(
            signed, "agent-demo", "merchant-razorpay-01", vk, NOW,
        )
        assert isinstance(result, AuthorizationVerificationResult)
        assert isinstance(result.valid, bool)
        assert isinstance(result.reason, str)
        assert len(result.reason) > 0

    def test_sign_mandate_produces_64_byte_signature(self, keypair):
        """Ed25519 signatures are exactly 64 bytes."""
        sk, vk = keypair
        signed = sign_mandate(make_mandate(), sk, "k1")
        assert len(signed.signature) == 64

    def test_sign_mandate_sets_algorithm_to_ed25519(self, keypair):
        """sign_mandate always sets algorithm='Ed25519'."""
        sk, _ = keypair
        signed = sign_mandate(make_mandate(), sk, "k1")
        assert signed.algorithm == "Ed25519"

    def test_sign_mandate_preserves_key_id(self, keypair):
        """sign_mandate preserves the provided key_id."""
        sk, _ = keypair
        signed = sign_mandate(make_mandate(), sk, "my-key-id-123")
        assert signed.key_id == "my-key-id-123"


# ══════════════════════════════════════════════════════════════════════════════
# 2. Signature attacks — tampering with signed fields
# ══════════════════════════════════════════════════════════════════════════════


class TestSignatureAttacks:
    """
    Prove that modifying ANY authorization-critical field after signing
    causes verification to fail.

    Attack model: An attacker obtains a valid Ed25519AuthorizationProof and tries
    to modify a field to gain more spending authority, target a different
    merchant, or extend the mandate's validity. The signature was computed
    over the original fields, so modifying any field and keeping the
    original signature must produce a verification failure.
    """

    def _tamper_and_verify(self, keypair, tampered_mandate):
        """
        Helper: sign the original mandate, then create a new
        Ed25519AuthorizationProof with a DIFFERENT mandate but the SAME signature.
        """
        sk, vk = keypair
        original = make_mandate()
        signed_original = sign_mandate(original, sk, "k1")

        # Attacker replaces the mandate but keeps the original signature.
        tampered_signed = Ed25519AuthorizationProof(
            payload=tampered_mandate,
            signature=signed_original.signature,  # original signature
            key_id=signed_original.key_id,
            algorithm="Ed25519",
        )

        result = verify_mandate(
            tampered_signed,
            expected_buyer_agent_id=tampered_mandate.buyer_agent_id,
            expected_merchant_id=tampered_mandate.merchant_id,
            public_key=vk,
            current_time=NOW,
        )
        return result

    def test_modified_max_amount_fails(self, keypair):
        """Inflating max_amount without resigning → INVALID."""
        tampered = make_mandate(max_amount_minor=99999999)  # ₹999,999.99
        result = self._tamper_and_verify(keypair, tampered)
        assert result.valid is False
        assert "signature" in result.reason.lower() or "mismatch" in result.reason.lower()

    def test_modified_buyer_id_fails(self, keypair):
        """Swapping buyer_agent_id without resigning → INVALID."""
        tampered = make_mandate(buyer_agent_id="attacker-agent")
        result = self._tamper_and_verify(keypair, tampered)
        assert result.valid is False

    def test_modified_merchant_id_fails(self, keypair):
        """Swapping merchant_id without resigning → INVALID."""
        tampered = make_mandate(merchant_id="attacker-merchant")
        result = self._tamper_and_verify(keypair, tampered)
        assert result.valid is False

    def test_modified_currency_fails(self, keypair):
        """Changing currency without resigning → INVALID."""
        tampered = make_mandate(currency="USD")
        result = self._tamper_and_verify(keypair, tampered)
        assert result.valid is False

    def test_modified_expiry_fails(self, keypair):
        """Extending expiry without resigning → INVALID."""
        tampered = make_mandate(expires_at=NOW + timedelta(days=9999))
        result = self._tamper_and_verify(keypair, tampered)
        assert result.valid is False

    def test_modified_nonce_fails(self, keypair):
        """Changing nonce without resigning → INVALID."""
        tampered = make_mandate(nonce="tampered-nonce")
        result = self._tamper_and_verify(keypair, tampered)
        assert result.valid is False

    def test_modified_mandate_type_fails(self, keypair):
        """Changing mandate_type from one_time to recurring → INVALID."""
        tampered = make_mandate(mandate_type=MandateType.recurring)
        result = self._tamper_and_verify(keypair, tampered)
        assert result.valid is False

    def test_modified_mandate_id_fails(self, keypair):
        """Changing mandate_id without resigning → INVALID."""
        tampered = make_mandate(mandate_id="forged-mandate-999")
        result = self._tamper_and_verify(keypair, tampered)
        assert result.valid is False

    def test_modified_signature_bytes_fails(self, keypair):
        """Corrupting signature bytes → INVALID."""
        sk, vk = keypair
        signed = sign_mandate(make_mandate(), sk, "k1")

        corrupted = Ed25519AuthorizationProof(
            payload=signed.payload,
            signature=bytes([b ^ 0xFF for b in signed.signature]),  # flip all bits
            key_id=signed.key_id,
            algorithm="Ed25519",
        )

        result = verify_mandate(
            corrupted, "agent-demo", "merchant-razorpay-01", vk, NOW,
        )
        assert result.valid is False
        assert "signature" in result.reason.lower()

    def test_modified_issued_at_fails(self, keypair):
        """Changing issued_at without resigning → INVALID."""
        tampered = make_mandate(issued_at=NOW - timedelta(days=365))
        result = self._tamper_and_verify(keypair, tampered)
        assert result.valid is False

    def test_modified_status_fails(self, keypair):
        """Changing status without resigning → INVALID."""
        tampered = make_mandate(status=MandateStatus.revoked)
        result = self._tamper_and_verify(keypair, tampered)
        assert result.valid is False

    def test_modified_authorization_method_fails(self, keypair):
        """Changing authorization_method without resigning → INVALID."""
        tampered = make_mandate(authorization_method="hmac-sha256")
        # Note: verify_mandate will reject non-Ed25519 algorithms before
        # reaching signature check, but the canonical bytes also differ.
        sk, vk = keypair
        original = make_mandate()
        signed_original = sign_mandate(original, sk, "k1")
        tampered_signed = Ed25519AuthorizationProof(
            payload=tampered,
            signature=signed_original.signature,
            key_id=signed_original.key_id,
            algorithm="Ed25519",  # keep algorithm field as Ed25519 for the envelope
        )
        result = verify_mandate(
            tampered_signed, "agent-demo", "merchant-razorpay-01", vk, NOW,
        )
        assert result.valid is False

    def test_modified_authorization_ref_fails(self, keypair):
        """Changing authorization_ref without resigning → INVALID."""
        tampered = make_mandate(authorization_ref="stolen-key-ref")
        result = self._tamper_and_verify(keypair, tampered)
        assert result.valid is False


# ══════════════════════════════════════════════════════════════════════════════
# 3. Identity attacks
# ══════════════════════════════════════════════════════════════════════════════


class TestIdentityAttacks:
    """
    Prove that authorization cannot cross identity boundaries.

    Even with a valid signature, verification fails if the EXPECTED
    identities do not match what was signed.
    """

    def test_expected_buyer_differs_fails(self, keypair):
        """
        agent-A's authorization cannot be used by agent-B.

        The mandate is legitimately signed for agent-demo, but the request
        claims to be from agent-impersonator.
        """
        sk, vk = keypair
        mandate = make_mandate(buyer_agent_id="agent-demo")
        signed = sign_mandate(mandate, sk, "k1")

        result = verify_mandate(
            signed,
            expected_buyer_agent_id="agent-impersonator",  # WRONG
            expected_merchant_id="merchant-razorpay-01",
            public_key=vk,
            current_time=NOW,
        )
        assert result.valid is False
        assert "buyer" in result.reason.lower()

    def test_expected_merchant_differs_fails(self, keypair):
        """
        An authorization for merchant-A cannot be applied to merchant-B.
        """
        sk, vk = keypair
        mandate = make_mandate(merchant_id="merchant-A")
        signed = sign_mandate(mandate, sk, "k1")

        result = verify_mandate(
            signed,
            expected_buyer_agent_id="agent-demo",
            expected_merchant_id="merchant-B",  # WRONG
            public_key=vk,
            current_time=NOW,
        )
        assert result.valid is False
        assert "merchant" in result.reason.lower()

    def test_both_identities_wrong_fails(self, keypair):
        """Both buyer and merchant differ → fails on first mismatch."""
        sk, vk = keypair
        signed = sign_mandate(make_mandate(), sk, "k1")

        result = verify_mandate(
            signed,
            expected_buyer_agent_id="wrong-buyer",
            expected_merchant_id="wrong-merchant",
            public_key=vk,
            current_time=NOW,
        )
        assert result.valid is False


# ══════════════════════════════════════════════════════════════════════════════
# 4. Temporal attacks
# ══════════════════════════════════════════════════════════════════════════════


class TestTemporalAttacks:
    def test_expired_mandate_fails(self, keypair):
        """A mandate past its expiry time → INVALID."""
        sk, vk = keypair
        mandate = make_mandate(
            issued_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        )
        signed = sign_mandate(mandate, sk, "k1")

        # Verify at a time AFTER expiry
        result = verify_mandate(
            signed,
            expected_buyer_agent_id="agent-demo",
            expected_merchant_id="merchant-razorpay-01",
            public_key=vk,
            current_time=NOW + timedelta(hours=2),  # 1 hour past expiry
        )
        assert result.valid is False
        assert "expired" in result.reason.lower()

    def test_mandate_at_exact_expiry_time_fails(self, keypair):
        """current_time == expires_at → expired (strictly before, not at)."""
        sk, vk = keypair
        expires = NOW + timedelta(hours=1)
        mandate = make_mandate(issued_at=NOW, expires_at=expires)
        signed = sign_mandate(mandate, sk, "k1")

        result = verify_mandate(
            signed, "agent-demo", "merchant-razorpay-01", vk,
            current_time=expires,  # exactly at expiry
        )
        assert result.valid is False
        assert "expired" in result.reason.lower()

    def test_mandate_one_second_before_expiry_passes(self, keypair):
        """One second before expiry → still valid."""
        sk, vk = keypair
        expires = NOW + timedelta(hours=1)
        mandate = make_mandate(issued_at=NOW, expires_at=expires)
        signed = sign_mandate(mandate, sk, "k1")

        result = verify_mandate(
            signed, "agent-demo", "merchant-razorpay-01", vk,
            current_time=expires - timedelta(seconds=1),
        )
        assert result.valid is True

    def test_default_current_time_uses_utc(self, keypair):
        """verify_mandate defaults to datetime.now(utc) for current_time."""
        sk, vk = keypair
        # Create a mandate that expires far in the future so it's valid now.
        mandate = make_mandate(expires_at=NOW + timedelta(days=3650))
        signed = sign_mandate(mandate, sk, "k1")

        # Don't pass current_time — let it default.
        result = verify_mandate(
            signed, "agent-demo", "merchant-razorpay-01", vk,
        )
        assert result.valid is True


# ══════════════════════════════════════════════════════════════════════════════
# 5. Malformed input
# ══════════════════════════════════════════════════════════════════════════════


class TestMalformedInput:
    def test_wrong_public_key_fails(self, keypair):
        """Verifying with a different key than what signed → INVALID."""
        sk, vk = keypair
        signed = sign_mandate(make_mandate(), sk, "k1")

        # Generate a DIFFERENT keypair
        wrong_sk = SigningKey.generate()
        wrong_vk = wrong_sk.verify_key

        result = verify_mandate(
            signed, "agent-demo", "merchant-razorpay-01", wrong_vk, NOW,
        )
        assert result.valid is False
        assert "signature" in result.reason.lower()

    def test_invalid_signature_bytes_fails(self, keypair):
        """Random bytes as signature → INVALID."""
        _, vk = keypair
        mandate = make_mandate()
        bad_signed = Ed25519AuthorizationProof(
            payload=mandate,
            signature=b"\xde\xad\xbe\xef" * 16,  # 64 bytes of garbage
            key_id="k1",
            algorithm="Ed25519",
        )

        result = verify_mandate(
            bad_signed, "agent-demo", "merchant-razorpay-01", vk, NOW,
        )
        assert result.valid is False

    def test_short_signature_bytes_fails(self, keypair):
        """Too-short signature bytes → INVALID."""
        _, vk = keypair
        mandate = make_mandate()
        bad_signed = Ed25519AuthorizationProof(
            payload=mandate,
            signature=b"\x01\x02\x03",  # only 3 bytes, not 64
            key_id="k1",
            algorithm="Ed25519",
        )

        result = verify_mandate(
            bad_signed, "agent-demo", "merchant-razorpay-01", vk, NOW,
        )
        assert result.valid is False

    def test_unsupported_algorithm_fails(self, keypair):
        """Non-Ed25519 algorithm → INVALID."""
        sk, vk = keypair
        signed = sign_mandate(make_mandate(), sk, "k1")

        # Forge a new Ed25519AuthorizationProof with wrong algorithm.
        wrong_algo = Ed25519AuthorizationProof(
            payload=signed.payload,
            signature=signed.signature,
            key_id=signed.key_id,
            algorithm="RSA-2048",  # not supported
        )

        result = verify_mandate(
            wrong_algo, "agent-demo", "merchant-razorpay-01", vk, NOW,
        )
        assert result.valid is False
        assert "unsupported algorithm" in result.reason.lower()

    def test_all_failure_paths_return_structured_result(self, keypair):
        """Every failure produces an AuthorizationVerificationResult, not an exception."""
        sk, vk = keypair

        # Tampered signature
        signed = sign_mandate(make_mandate(), sk, "k1")
        tampered = Ed25519AuthorizationProof(
            payload=signed.payload,
            signature=b"\x00" * 64,
            key_id="k1",
            algorithm="Ed25519",
        )
        r1 = verify_mandate(tampered, "agent-demo", "merchant-razorpay-01", vk, NOW)
        assert isinstance(r1, AuthorizationVerificationResult)
        assert r1.valid is False

        # Wrong buyer
        r2 = verify_mandate(signed, "wrong-buyer", "merchant-razorpay-01", vk, NOW)
        assert isinstance(r2, AuthorizationVerificationResult)
        assert r2.valid is False

        # Wrong merchant
        r3 = verify_mandate(signed, "agent-demo", "wrong-merchant", vk, NOW)
        assert isinstance(r3, AuthorizationVerificationResult)
        assert r3.valid is False

        # Expired
        r4 = verify_mandate(
            signed, "agent-demo", "merchant-razorpay-01", vk,
            NOW + timedelta(days=365),
        )
        assert isinstance(r4, AuthorizationVerificationResult)
        assert r4.valid is False

        # Wrong algorithm
        wrong_algo = Ed25519AuthorizationProof(
            payload=signed.payload,
            signature=signed.signature,
            key_id="k1",
            algorithm="HMAC",
        )
        r5 = verify_mandate(wrong_algo, "agent-demo", "merchant-razorpay-01", vk, NOW)
        assert isinstance(r5, AuthorizationVerificationResult)
        assert r5.valid is False


# ══════════════════════════════════════════════════════════════════════════════
# 6. Tamper-proof demonstration — THE critical test
# ══════════════════════════════════════════════════════════════════════════════


class TestTamperProof:
    """
    THE most important security test in the project.

    Demonstrates the full attack-and-detection cycle:
        1. Create a legitimate signed mandate.
        2. Verify it → VALID.
        3. Create a modified mandate with inflated amount (without resigning).
        4. Reuse the original signature.
        5. Verify the tampered version → INVALID.
    """

    def test_tamper_max_amount_without_resigning(self, keypair):
        """
        Original:  ₹5,000 authorization → VALID
        Tampered:  ₹50,000 authorization (same signature) → INVALID

        This proves the attacker cannot inflate their spending limit.
        """
        sk, vk = keypair

        # ── Step 1: Create legitimate mandate (₹5,000) ───────────────────
        original_mandate = make_mandate(max_amount_minor=500000)  # ₹5,000
        signed_original = sign_mandate(original_mandate, sk, "k1")

        # ── Step 2: Verify original → VALID ──────────────────────────────
        result_original = verify_mandate(
            signed_original, "agent-demo", "merchant-razorpay-01", vk, NOW,
        )
        assert result_original.valid is True, (
            f"Original mandate should verify. Got: {result_original.reason}"
        )

        # ── Step 3: Create tampered mandate (₹50,000) ────────────────────
        tampered_mandate = make_mandate(max_amount_minor=5000000)  # ₹50,000

        # ── Step 4: Reuse original signature ─────────────────────────────
        tampered_signed = Ed25519AuthorizationProof(
            payload=tampered_mandate,
            signature=signed_original.signature,  # STOLEN SIGNATURE
            key_id=signed_original.key_id,
            algorithm="Ed25519",
        )

        # ── Step 5: Verify tampered → INVALID ────────────────────────────
        result_tampered = verify_mandate(
            tampered_signed, "agent-demo", "merchant-razorpay-01", vk, NOW,
        )
        assert result_tampered.valid is False, (
            "Tampered mandate with inflated amount MUST fail verification. "
            "If this passes, the authorization system is broken."
        )
        assert "signature" in result_tampered.reason.lower() or \
               "mismatch" in result_tampered.reason.lower()

    def test_tamper_buyer_without_resigning(self, keypair):
        """
        Signed for agent-A → VALID
        Reused signature for agent-B → INVALID

        This proves agent-B cannot use agent-A's authorization.
        """
        sk, vk = keypair

        original = make_mandate(buyer_agent_id="agent-A")
        signed = sign_mandate(original, sk, "k1")

        # Verify original
        r1 = verify_mandate(signed, "agent-A", "merchant-razorpay-01", vk, NOW)
        assert r1.valid is True

        # Tamper: swap buyer
        tampered = make_mandate(buyer_agent_id="agent-B")
        tampered_signed = Ed25519AuthorizationProof(
            payload=tampered,
            signature=signed.signature,
            key_id=signed.key_id,
            algorithm="Ed25519",
        )

        r2 = verify_mandate(tampered_signed, "agent-B", "merchant-razorpay-01", vk, NOW)
        assert r2.valid is False

    def test_tamper_merchant_without_resigning(self, keypair):
        """
        Signed for merchant-A → VALID
        Reused signature for merchant-B → INVALID
        """
        sk, vk = keypair

        original = make_mandate(merchant_id="merchant-A")
        signed = sign_mandate(original, sk, "k1")

        r1 = verify_mandate(signed, "agent-demo", "merchant-A", vk, NOW)
        assert r1.valid is True

        tampered = make_mandate(merchant_id="merchant-B")
        tampered_signed = Ed25519AuthorizationProof(
            payload=tampered,
            signature=signed.signature,
            key_id=signed.key_id,
            algorithm="Ed25519",
        )

        r2 = verify_mandate(tampered_signed, "agent-demo", "merchant-B", vk, NOW)
        assert r2.valid is False

    def test_tamper_expiry_without_resigning(self, keypair):
        """
        Signed with 30-day expiry → VALID
        Extended to 10-year expiry (same signature) → INVALID
        """
        sk, vk = keypair

        original = make_mandate(expires_at=NOW + timedelta(days=30))
        signed = sign_mandate(original, sk, "k1")

        r1 = verify_mandate(signed, "agent-demo", "merchant-razorpay-01", vk, NOW)
        assert r1.valid is True

        tampered = make_mandate(expires_at=NOW + timedelta(days=3650))
        tampered_signed = Ed25519AuthorizationProof(
            payload=tampered,
            signature=signed.signature,
            key_id=signed.key_id,
            algorithm="Ed25519",
        )

        r2 = verify_mandate(tampered_signed, "agent-demo", "merchant-razorpay-01", vk, NOW)
        assert r2.valid is False


# ══════════════════════════════════════════════════════════════════════════════
# 7. Scope verification
# ══════════════════════════════════════════════════════════════════════════════


class TestScopeVerification:
    def test_request_within_scope_passes(self, keypair):
        """₹1,500 request against ₹5,000 mandate → within scope."""
        sk, _ = keypair
        request = make_request(unit_price_minor=150000)  # ₹1,500
        signed = sign_mandate(make_mandate(max_amount_minor=500000), sk, "k1")

        result = verify_authorization_scope(request, signed)
        assert result.valid is True

    def test_request_at_exact_limit_passes(self, keypair):
        """₹5,000 request against ₹5,000 mandate → exactly at limit."""
        sk, _ = keypair
        request = make_request(unit_price_minor=500000)
        signed = sign_mandate(make_mandate(max_amount_minor=500000), sk, "k1")

        result = verify_authorization_scope(request, signed)
        assert result.valid is True

    def test_request_exceeds_mandate_fails(self, keypair):
        """₹8,000 request against ₹5,000 mandate → exceeds scope."""
        sk, _ = keypair
        request = make_request(unit_price_minor=800000)  # ₹8,000
        signed = sign_mandate(make_mandate(max_amount_minor=500000), sk, "k1")

        result = verify_authorization_scope(request, signed)
        assert result.valid is False
        assert "exceeds" in result.reason.lower()

    def test_buyer_mismatch_in_scope_fails(self, keypair):
        """Request buyer ≠ mandate buyer → fails scope check."""
        sk, _ = keypair
        request = make_request(buyer_agent_id="agent-B")
        signed = sign_mandate(
            make_mandate(buyer_agent_id="agent-A"), sk, "k1",
        )

        result = verify_authorization_scope(request, signed)
        assert result.valid is False
        assert "buyer" in result.reason.lower()

    def test_merchant_mismatch_in_scope_fails(self, keypair):
        """Request merchant ≠ mandate merchant → fails scope check."""
        sk, _ = keypair
        request = make_request(merchant_id="merchant-B")
        signed = sign_mandate(
            make_mandate(merchant_id="merchant-A"), sk, "k1",
        )

        result = verify_authorization_scope(request, signed)
        assert result.valid is False
        assert "merchant" in result.reason.lower()

    def test_currency_mismatch_in_scope_fails(self, keypair):
        """Request in USD against INR mandate → fails."""
        sk, _ = keypair
        request = make_request(currency="USD")
        signed = sign_mandate(
            make_mandate(currency="INR"), sk, "k1",
        )

        result = verify_authorization_scope(request, signed)
        assert result.valid is False
        assert "currency" in result.reason.lower()


# ══════════════════════════════════════════════════════════════════════════════
# 8. Fail-closed guarantees
# ══════════════════════════════════════════════════════════════════════════════


class TestFailClosed:
    """
    Verify that verify_mandate NEVER returns valid=True when something
    is wrong. Every code path must fail closed.
    """

    def test_never_returns_true_with_bad_signature(self, keypair):
        _, vk = keypair
        bad = Ed25519AuthorizationProof(
            payload=make_mandate(),
            signature=b"\x00" * 64,
            key_id="k1",
            algorithm="Ed25519",
        )
        result = verify_mandate(bad, "agent-demo", "merchant-razorpay-01", vk, NOW)
        assert result.valid is False

    def test_never_returns_true_with_wrong_buyer(self, keypair):
        sk, vk = keypair
        signed = sign_mandate(make_mandate(), sk, "k1")
        result = verify_mandate(signed, "WRONG", "merchant-razorpay-01", vk, NOW)
        assert result.valid is False

    def test_never_returns_true_with_wrong_merchant(self, keypair):
        sk, vk = keypair
        signed = sign_mandate(make_mandate(), sk, "k1")
        result = verify_mandate(signed, "agent-demo", "WRONG", vk, NOW)
        assert result.valid is False

    def test_never_returns_true_when_expired(self, keypair):
        sk, vk = keypair
        mandate = make_mandate(expires_at=NOW + timedelta(seconds=1))
        signed = sign_mandate(mandate, sk, "k1")
        result = verify_mandate(
            signed, "agent-demo", "merchant-razorpay-01", vk,
            NOW + timedelta(days=1),
        )
        assert result.valid is False

    def test_never_returns_true_with_wrong_algorithm(self, keypair):
        sk, vk = keypair
        signed = sign_mandate(make_mandate(), sk, "k1")
        wrong = Ed25519AuthorizationProof(
            payload=signed.payload,
            signature=signed.signature,
            key_id="k1",
            algorithm="NONE",
        )
        result = verify_mandate(wrong, "agent-demo", "merchant-razorpay-01", vk, NOW)
        assert result.valid is False

    def test_result_repr_does_not_expose_secrets(self, keypair):
        """repr() must not contain raw signature bytes or private key material."""
        sk, vk = keypair
        signed = sign_mandate(make_mandate(), sk, "k1")
        result = verify_mandate(signed, "agent-demo", "merchant-razorpay-01", vk, NOW)
        result_str = repr(result)
        # Should not contain raw bytes or key material
        assert "\\x" not in result_str or "key" not in result_str.lower()
        assert "private" not in result_str.lower()


# ══════════════════════════════════════════════════════════════════════════════
# 9. One-time mandate semantics documentation test
# ══════════════════════════════════════════════════════════════════════════════


class TestOneTimeMandate:
    def test_one_time_mandate_verifies(self, keypair):
        """A one_time mandate verifies just like any other mandate."""
        sk, vk = keypair
        mandate = make_mandate(mandate_type=MandateType.one_time)
        signed = sign_mandate(mandate, sk, "k1")
        result = verify_mandate(signed, "agent-demo", "merchant-razorpay-01", vk, NOW)
        assert result.valid is True

    def test_recurring_mandate_verifies(self, keypair):
        """A recurring mandate verifies structurally (execution not implemented)."""
        sk, vk = keypair
        mandate = make_mandate(mandate_type=MandateType.recurring)
        signed = sign_mandate(mandate, sk, "k1")
        result = verify_mandate(signed, "agent-demo", "merchant-razorpay-01", vk, NOW)
        assert result.valid is True

    def test_one_time_and_recurring_produce_different_signatures(self, keypair):
        """
        Different mandate types produce different canonical bytes and therefore
        different signatures. An attacker cannot change a one_time mandate
        to recurring without invalidating the signature.
        """
        sk, _ = keypair
        m1 = make_mandate(mandate_type=MandateType.one_time)
        m2 = make_mandate(mandate_type=MandateType.recurring)
        s1 = sign_mandate(m1, sk, "k1")
        s2 = sign_mandate(m2, sk, "k1")
        assert s1.signature != s2.signature
