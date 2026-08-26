"""
Prompt 4 Demonstration — Deterministic Policy Engine
=====================================================

Scenario 1 — allowed
Scenario 2 — review
Scenario 3 — blocked
Scenario 4 — blocked category
Scenario 5 — fail closed
"""

from datetime import datetime, timezone
from app.core.schemas import (
    BuyerProtocol,
    CommerceContext,
    CommerceItem,
    CommerceRequest,
    Mandate,
    MandateStatus,
    MandateType,
    Money,
)
from app.core.policy import (
    PolicyConfig,
    evaluate_policy,
)
from datetime import timedelta

NOW = datetime(2026, 8, 21, 15, 0, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=1)

def make_test_context(
    amount_minor: int,
    category: str | None,
) -> CommerceContext:
    item = CommerceItem(
        product_id="prod-001",
        name="Test Product",
        quantity=1,
        unit_price=Money(amount_minor=amount_minor, currency="INR"),
        category=category,
    )
    request = CommerceRequest(
        transaction_id="txn-001",
        created_at=NOW,
        expires_at=LATER,
        nonce="req-nonce",
        buyer_agent_id="agent-01",
        buyer_protocol=BuyerProtocol.x402,
        merchant_id="merchant-01",
        items=[item],
        receipt_destination_protocol=BuyerProtocol.x402,
        receipt_destination_ref="ref",
    )
    # Mandate is large enough to pass structural validation
    mandate = Mandate(
        mandate_id="mandate-01",
        buyer_agent_id="agent-01",
        merchant_id="merchant-01",
        max_amount=Money(amount_minor=99999999, currency="INR"),
        mandate_type=MandateType.one_time,
        status=MandateStatus.active,
        issued_at=NOW,
        expires_at=LATER,
        nonce="mandate-nonce",
        authorization_method="test",
        authorization_ref="test",
    )
    return CommerceContext(request=request, mandate=mandate)


def run_scenario(name: str, amount_minor: int, category: str | None, config: PolicyConfig):
    print("=" * 60)
    print(f"{name}")
    print("=" * 60)
    
    ctx = make_test_context(amount_minor, category)
    print(f"Request: \u20b9{amount_minor/100:,.0f}")
    if config.max_transaction_amount:
        print(f"Maximum: \u20b9{config.max_transaction_amount/100:,.0f}")
    if config.review_threshold_amount:
        print(f"Review threshold: \u20b9{config.review_threshold_amount/100:,.0f}")
    print(f"Category: {category}")
    
    decision = evaluate_policy(ctx, config)
    print(f"Result: {decision.decision.value}")
    if decision.decision.value != "ALLOW":
        print(f"Reason: {decision.primary_reason}")
    print()


# ── Scenario 1 — allowed ──────────────────────────────────────────────────────
# Request: ₹1,500
# Maximum: ₹5,000
# Review threshold: ₹3,000
# Category: software
config_1 = PolicyConfig(
    max_transaction_amount=500000,
    review_threshold_amount=300000,
    allowed_categories={"software"}
)
run_scenario("Scenario 1 — allowed", 150000, "software", config_1)


# ── Scenario 2 — review ───────────────────────────────────────────────────────
# Request: ₹4,000
# Maximum: ₹5,000
# Review threshold: ₹3,000
config_2 = PolicyConfig(
    max_transaction_amount=500000,
    review_threshold_amount=300000,
)
run_scenario("Scenario 2 — review", 400000, "software", config_2)


# ── Scenario 3 — blocked ──────────────────────────────────────────────────────
# Request: ₹8,000
# Maximum: ₹5,000
config_3 = PolicyConfig(
    max_transaction_amount=500000,
)
run_scenario("Scenario 3 — blocked", 800000, "software", config_3)


# ── Scenario 4 — blocked category ─────────────────────────────────────────────
# Category: blocked category
# Amount otherwise valid
config_4 = PolicyConfig(
    max_transaction_amount=500000,
    blocked_categories={"blocked category"}
)
run_scenario("Scenario 4 — blocked category", 150000, "blocked category", config_4)


# ── Scenario 5 — fail closed ──────────────────────────────────────────────────
# Allowed-category rule configured
# Item category missing
config_5 = PolicyConfig(
    allowed_categories={"software"}
)
run_scenario("Scenario 5 — fail closed", 150000, None, config_5)


print("NOTE: These are policy decisions only.")
print("They are NOT paid or Razorpay-approved transactions.")
