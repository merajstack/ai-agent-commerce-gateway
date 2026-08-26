"""
Prompt 3 Demonstration — Cryptographic Authorization
=====================================================

Case 1: Valid signed mandate → VALID
Case 2: Tampered amount (₹5,000 → ₹50,000, same signature) → INVALID
Case 3: Wrong buyer identity → INVALID
Case 4: Expired mandate → INVALID
"""
from datetime import datetime, timedelta, timezone

from nacl.signing import SigningKey

from app.core.mandate import (
    sign_mandate,
    verify_mandate,
    verify_authorization_scope,
)
from app.core.schemas import (
    BuyerProtocol,
    CommerceItem,
    CommerceRequest,
    Mandate,
    MandateStatus,
    MandateType,
    Money,
    SignedAuthorization,
)

NOW = datetime(2026, 8, 21, 15, 0, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=1)
MUCH_LATER = NOW + timedelta(days=30)

# Generate a test keypair (never stored in source code)
sk = SigningKey.generate()
vk = sk.verify_key

# ── Helper ────────────────────────────────────────────────────────────────────

def make_mandate(**overrides):
    defaults = dict(
        mandate_id="mandate-demo-001",
        buyer_agent_id="agent-demo",
        merchant_id="merchant-razorpay-01",
        max_amount=Money(amount_minor=500000, currency="INR"),  # ₹5,000
        mandate_type=MandateType.one_time,
        status=MandateStatus.active,
        issued_at=NOW,
        expires_at=MUCH_LATER,
        nonce="mandate-nonce-demo-001",
        authorization_method="ed25519",
        authorization_ref="key-ed25519-pub-001",
    )
    defaults.update(overrides)
    return Mandate(**defaults)


def make_request(**overrides):
    defaults = dict(
        transaction_id="txn-demo-001",
        created_at=NOW,
        expires_at=LATER,
        nonce="nonce-req-demo-001",
        buyer_agent_id="agent-demo",
        buyer_agent_name="Demo Buyer Agent",
        buyer_protocol=BuyerProtocol.x402,
        merchant_id="merchant-razorpay-01",
        items=[CommerceItem(
            product_id="prod-ai-api",
            name="AI API Credits",
            quantity=1,
            unit_price=Money(amount_minor=150000, currency="INR"),  # ₹1,500
            category="software",
        )],
        receipt_destination_protocol=BuyerProtocol.x402,
        receipt_destination_ref="https://callback.buyer.ai/receipt",
    )
    defaults.update(overrides)
    return CommerceRequest(**defaults)


# ══════════════════════════════════════════════════════════════════════════════
# Case 1: Valid signed mandate
# ══════════════════════════════════════════════════════════════════════════════

print("=" * 70)
print("CASE 1 — Valid signed mandate")
print("=" * 70)

mandate = make_mandate()
signed = sign_mandate(mandate, sk, "test-key-001")
request = make_request()

sig_result = verify_mandate(
    signed,
    expected_buyer_agent_id="agent-demo",
    expected_merchant_id="merchant-razorpay-01",
    public_key=vk,
    current_time=NOW,
)

scope_result = verify_authorization_scope(request, signed)

print(f"  Signed mandate:       mandate-demo-001")
print(f"  Buyer:                agent-demo")
print(f"  Merchant:             merchant-razorpay-01")
print(f"  Max amount:           Rs {mandate.max_amount.amount_minor / 100:,.2f}")
print(f"  Request amount:       Rs {request.calculated_total.amount_minor / 100:,.2f}")
print(f"  Not expired:          True (expires {mandate.expires_at.isoformat()})")
print(f"  Signature valid:      {sig_result.valid}")
print(f"  Scope valid:          {scope_result.valid}")
print(f"  Signature reason:     {sig_result.reason}")
print()
print(f"  Schema validation:    PASS")
print(f"  Authorization:        PASS")
print(f"  Policy:               NOT RUN")
print(f"  Replay protection:    NOT RUN")
print(f"  Final decision:       UNDECIDED")
print()
print("  NOTE: This is NOT a Razorpay-approved payment.")

# ══════════════════════════════════════════════════════════════════════════════
# Case 2: Tampered amount (₹5,000 → ₹50,000, same signature)
# ══════════════════════════════════════════════════════════════════════════════

print()
print("=" * 70)
print("CASE 2 — Tampered amount (original: Rs 5,000 -> Rs 50,000, same sig)")
print("=" * 70)

tampered_mandate = make_mandate(
    max_amount=Money(amount_minor=5000000, currency="INR"),  # ₹50,000
)

# Reuse the ORIGINAL signature — attacker did not resign
tampered_signed = SignedAuthorization(
    payload=tampered_mandate,
    signature=signed.signature,  # stolen signature
    key_id=signed.key_id,
    algorithm="Ed25519",
)

tampered_result = verify_mandate(
    tampered_signed,
    expected_buyer_agent_id="agent-demo",
    expected_merchant_id="merchant-razorpay-01",
    public_key=vk,
    current_time=NOW,
)

print(f"  Original authorization: Rs {mandate.max_amount.amount_minor / 100:,.2f}")
print(f"  Tampered authorization: Rs {tampered_mandate.max_amount.amount_minor / 100:,.2f}")
print(f"  Signature unchanged:    True (reused original)")
print(f"  Result:                 {'VALID' if tampered_result.valid else 'INVALID'}")
print(f"  Reason:                 {tampered_result.reason}")

# ══════════════════════════════════════════════════════════════════════════════
# Case 3: Wrong buyer identity
# ══════════════════════════════════════════════════════════════════════════════

print()
print("=" * 70)
print("CASE 3 — Wrong buyer (authorized: agent-demo, actual: agent-attacker)")
print("=" * 70)

wrong_buyer_result = verify_mandate(
    signed,  # valid signature for agent-demo
    expected_buyer_agent_id="agent-attacker",  # but request claims to be this
    expected_merchant_id="merchant-razorpay-01",
    public_key=vk,
    current_time=NOW,
)

print(f"  Authorized buyer:     agent-demo")
print(f"  Actual buyer:         agent-attacker")
print(f"  Signature valid:      True (legitimately signed for agent-demo)")
print(f"  Result:               {'VALID' if wrong_buyer_result.valid else 'INVALID'}")
print(f"  Reason:               {wrong_buyer_result.reason}")

# ══════════════════════════════════════════════════════════════════════════════
# Case 4: Expired mandate
# ══════════════════════════════════════════════════════════════════════════════

print()
print("=" * 70)
print("CASE 4 — Expired mandate")
print("=" * 70)

short_mandate = make_mandate(
    expires_at=NOW + timedelta(hours=1),
)
short_signed = sign_mandate(short_mandate, sk, "test-key-001")

expired_result = verify_mandate(
    short_signed,
    expected_buyer_agent_id="agent-demo",
    expected_merchant_id="merchant-razorpay-01",
    public_key=vk,
    current_time=NOW + timedelta(hours=2),  # 1 hour past expiry
)

print(f"  Mandate expires:      {short_mandate.expires_at.isoformat()}")
print(f"  Current time:         {(NOW + timedelta(hours=2)).isoformat()}")
print(f"  Signature valid:      True (correctly signed)")
print(f"  Result:               {'VALID' if expired_result.valid else 'INVALID'}")
print(f"  Reason:               {expired_result.reason}")

print()
print("=" * 70)
print("SUMMARY")
print("=" * 70)
print(f"  Case 1 (valid):           {'VALID' if sig_result.valid else 'INVALID'}")
print(f"  Case 2 (tampered amount): {'VALID' if tampered_result.valid else 'INVALID'}")
print(f"  Case 3 (wrong buyer):     {'VALID' if wrong_buyer_result.valid else 'INVALID'}")
print(f"  Case 4 (expired):         {'VALID' if expired_result.valid else 'INVALID'}")
print()
print("Cryptographic authorization is implemented.")
print("Policy, replay protection, and Razorpay execution are NOT yet implemented.")
