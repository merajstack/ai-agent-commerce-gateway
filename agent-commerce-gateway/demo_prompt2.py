"""Prompt 2 demonstration — valid and invalid canonical commerce examples."""
from datetime import datetime, timedelta, timezone
from pydantic import ValidationError

from app.core.schemas import (
    BuyerProtocol, CommerceContext, CommerceItem, GatewayDecision,
    Mandate, MandateStatus, MandateType, Money, CommerceRequest,
)

NOW = datetime(2026, 8, 21, 15, 0, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=1)
MUCH_LATER = NOW + timedelta(days=30)

# ── VALID: ₹1,500 against ₹5,000 mandate ─────────────────────
print("=" * 60)
print("VALID EXAMPLE — x402 simulated, ₹1,500 against ₹5,000 mandate")
print("=" * 60)

request = CommerceRequest(
    transaction_id="txn-demo-001",
    created_at=NOW,
    expires_at=LATER,
    nonce="nonce-demo-xyz-001",
    buyer_agent_id="agent-demo",
    buyer_agent_name="Demo Buyer Agent",
    buyer_protocol=BuyerProtocol.x402,
    merchant_id="merchant-razorpay-01",
    items=[
        CommerceItem(
            product_id="prod-ai-api",
            name="AI API Credits - 1000 calls",
            quantity=1,
            unit_price=Money(amount_minor=150000, currency="INR"),
            category="software",
        )
    ],
    receipt_destination_protocol=BuyerProtocol.x402,
    receipt_destination_ref="https://callback.buyer.ai/receipt/txn-demo-001",
)

mandate = Mandate(
    mandate_id="mandate-demo-001",
    buyer_agent_id="agent-demo",
    merchant_id="merchant-razorpay-01",
    max_amount=Money(amount_minor=500000, currency="INR"),
    mandate_type=MandateType.one_time,
    status=MandateStatus.active,
    issued_at=NOW,
    expires_at=MUCH_LATER,
    nonce="mandate-nonce-demo-001",
    authorization_method="ed25519",
    authorization_ref="key-ed25519-pub-001",
)

ctx = CommerceContext(request=request, mandate=mandate)
print(f"  Protocol:            {request.buyer_protocol.value}")
print(f"  Buyer:               {request.buyer_agent_id}")
print(f"  Requested amount:    Rs {request.calculated_total.amount_minor / 100:,.2f}  ({request.calculated_total.amount_minor} paise)")
print(f"  Authorized maximum:  Rs {mandate.max_amount.amount_minor / 100:,.2f}  ({mandate.max_amount.amount_minor} paise)")
print(f"  Decision state:      {ctx.decision.value}")
print(f"  Validation:          PASS")
print()
print("  NOTE: UNDECIDED = structurally valid, NOT an approved payment.")
print("  Ed25519 authorization verification is a future step.")

# ── INVALID 1: amount exceeds authorization ───────────────────
print()
print("=" * 60)
print("INVALID 1 — Amount exceeds authorization (Rs 8,000 > Rs 5,000)")
print("=" * 60)
try:
    r2 = CommerceRequest(
        transaction_id="txn-bad-001",
        created_at=NOW, expires_at=LATER, nonce="nonce-bad-001",
        buyer_agent_id="agent-demo", buyer_protocol=BuyerProtocol.x402,
        merchant_id="merchant-razorpay-01",
        items=[CommerceItem(
            product_id="prod-expensive", name="Expensive Item", quantity=1,
            unit_price=Money(amount_minor=800000, currency="INR"),
        )],
        receipt_destination_protocol=BuyerProtocol.x402,
        receipt_destination_ref="https://cb.example.com/r",
    )
    m2 = Mandate(
        mandate_id="mandate-002", buyer_agent_id="agent-demo",
        merchant_id="merchant-razorpay-01",
        max_amount=Money(amount_minor=500000, currency="INR"),
        mandate_type=MandateType.one_time, status=MandateStatus.active,
        issued_at=NOW, expires_at=MUCH_LATER, nonce="n2",
        authorization_method="ed25519", authorization_ref="k2",
    )
    CommerceContext(request=r2, mandate=m2)
    print("  UNEXPECTED: should have been rejected!")
except ValidationError as e:
    print(f"  Validation result:   REJECTED")
    print(f"  Reason:              {e.errors()[0]['msg']}")

# ── INVALID 2: buyer mismatch ─────────────────────────────────
print()
print("=" * 60)
print("INVALID 2 — Buyer mismatch (request='agent-A', mandate='agent-B')")
print("=" * 60)
try:
    r3 = CommerceRequest(
        transaction_id="txn-bad-002",
        created_at=NOW, expires_at=LATER, nonce="nonce-bad-002",
        buyer_agent_id="agent-A", buyer_protocol=BuyerProtocol.x402,
        merchant_id="merchant-razorpay-01",
        items=[CommerceItem(product_id="p1", name="Item", quantity=1,
               unit_price=Money(amount_minor=100000, currency="INR"))],
        receipt_destination_protocol=BuyerProtocol.x402,
        receipt_destination_ref="https://cb.example.com/r",
    )
    m3 = Mandate(
        mandate_id="mandate-003", buyer_agent_id="agent-B",
        merchant_id="merchant-razorpay-01",
        max_amount=Money(amount_minor=500000, currency="INR"),
        mandate_type=MandateType.one_time, status=MandateStatus.active,
        issued_at=NOW, expires_at=MUCH_LATER, nonce="n3",
        authorization_method="ed25519", authorization_ref="k3",
    )
    CommerceContext(request=r3, mandate=m3)
    print("  UNEXPECTED: should have been rejected!")
except ValidationError as e:
    print(f"  Validation result:   REJECTED")
    print(f"  Reason:              {e.errors()[0]['msg']}")

# ── INVALID 3: float money ────────────────────────────────────
print()
print("=" * 60)
print("INVALID 3 — Float money rejected (25.5 instead of integer 2550)")
print("=" * 60)
try:
    Money(amount_minor=25.5, currency="INR")
    print("  UNEXPECTED: should have been rejected!")
except ValidationError as e:
    print(f"  Validation result:   REJECTED")
    print(f"  Reason:              {e.errors()[0]['msg']}")

# ── SECURITY: immutability ────────────────────────────────────
print()
print("=" * 60)
print("SECURITY — Mandate spending limit cannot be inflated after creation")
print("=" * 60)
try:
    mandate.max_amount = Money(amount_minor=99999999, currency="INR")
    print("  UNEXPECTED: mutation should have been blocked!")
except (TypeError, ValidationError) as err:
    print(f"  Mutation blocked:    TypeError raised (frozen model)")
    print(f"  Original limit:      Rs {mandate.max_amount.amount_minor / 100:,.2f} (unchanged)")

print()
print("Done.")
