"""
Policy Engine Tests — Agent Commerce Gateway Prompt 4
======================================================

Tests the deterministic merchant policy and guardrail engine.
Verifies ALLOW, REVIEW, BLOCK decisions, precedence rules, and fail-closed logic.
"""

from datetime import datetime, timezone
import pytest
from pydantic import ValidationError

from app.core.schemas import (
    BuyerProtocol,
    CommerceContext,
    CommerceItem,
    CommerceRequest,
    GatewayDecision,
    Mandate,
    MandateStatus,
    MandateType,
    Money,
)
from app.core.policy import (
    PolicyConfig,
    PolicyDecision,
    RecurringMandatePolicy,
    evaluate_policy,
)
from datetime import timedelta

NOW = datetime(2026, 8, 21, 15, 0, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=1)

def make_test_context(
    amount_minor: int = 150000,
    category: str | None = "software",
    currency: str = "INR",
    mandate_type: MandateType = MandateType.one_time,
) -> CommerceContext:
    item = CommerceItem(
        product_id="prod-001",
        name="Test Product",
        quantity=1,
        unit_price=Money(amount_minor=amount_minor, currency=currency),
        category=category,
    )
    request = CommerceRequest(
        transaction_id="txn-001",
        created_at=NOW,
        expires_at=LATER, # Must be strictly after created_at
        nonce="req-nonce",
        buyer_agent_id="agent-01",
        buyer_protocol=BuyerProtocol.x402,
        merchant_id="merchant-01",
        items=[item],
        receipt_destination_protocol=BuyerProtocol.x402,
        receipt_destination_ref="ref",
    )
    # We use a large mandate so context validation passes
    mandate = Mandate(
        mandate_id="mandate-01",
        buyer_agent_id="agent-01",
        merchant_id="merchant-01",
        max_amount=Money(amount_minor=99999999, currency=currency),
        mandate_type=mandate_type,
        status=MandateStatus.active,
        issued_at=NOW,
        expires_at=LATER,
        nonce="mandate-nonce",
        authorization_method="test",
        authorization_ref="test",
    )
    from nacl.signing import SigningKey
    from app.core.mandate import sign_mandate
    sk = SigningKey.generate()
    auth_proof = sign_mandate(mandate, sk, "test")
    return CommerceContext(
        request=request, 
        auth_proof=auth_proof,
        is_recurring=mandate_type == MandateType.recurring
    )


class TestAllow:
    def test_amount_below_maximum(self):
        ctx = make_test_context(amount_minor=5000)
        config = PolicyConfig(max_transaction_amount=10000)
        decision = evaluate_policy(ctx, config)
        assert decision.decision == GatewayDecision.ALLOW

    def test_amount_below_review_threshold(self):
        ctx = make_test_context(amount_minor=5000)
        config = PolicyConfig(max_transaction_amount=10000, review_threshold_amount=6000)
        decision = evaluate_policy(ctx, config)
        assert decision.decision == GatewayDecision.ALLOW

    def test_allowed_category(self):
        ctx = make_test_context(category="software")
        config = PolicyConfig(allowed_categories={"software", "books"})
        decision = evaluate_policy(ctx, config)
        assert decision.decision == GatewayDecision.ALLOW

    def test_allowed_currency(self):
        ctx = make_test_context(currency="INR")
        config = PolicyConfig(allowed_currencies={"INR", "USD"})
        decision = evaluate_policy(ctx, config)
        assert decision.decision == GatewayDecision.ALLOW

    def test_allowed_mandate_type(self):
        ctx = make_test_context(mandate_type=MandateType.recurring)
        config = PolicyConfig(recurring_mandate_policy=RecurringMandatePolicy.allowed)
        decision = evaluate_policy(ctx, config)
        assert decision.decision == GatewayDecision.ALLOW

    def test_empty_config(self):
        ctx = make_test_context()
        config = PolicyConfig()
        decision = evaluate_policy(ctx, config)
        assert decision.decision == GatewayDecision.ALLOW


class TestReview:
    def test_amount_above_review_but_below_max(self):
        ctx = make_test_context(amount_minor=6000)
        config = PolicyConfig(max_transaction_amount=10000, review_threshold_amount=5000)
        decision = evaluate_policy(ctx, config)
        assert decision.decision == GatewayDecision.REVIEW
        assert "REVIEW_THRESHOLD" in [r.rule_id for r in decision.triggered_rules]

    def test_recurring_mandate_configured_as_review(self):
        ctx = make_test_context(mandate_type=MandateType.recurring)
        config = PolicyConfig(recurring_mandate_policy=RecurringMandatePolicy.review)
        decision = evaluate_policy(ctx, config)
        assert decision.decision == GatewayDecision.REVIEW
        assert "RECURRING_MANDATES_REVIEW" in [r.rule_id for r in decision.triggered_rules]


class TestBlock:
    def test_amount_above_maximum(self):
        ctx = make_test_context(amount_minor=15000)
        config = PolicyConfig(max_transaction_amount=10000)
        decision = evaluate_policy(ctx, config)
        assert decision.decision == GatewayDecision.BLOCK
        assert "MAX_TRANSACTION_AMOUNT" in [r.rule_id for r in decision.triggered_rules]
        assert "Transaction exceeds the merchant maximum" in decision.primary_reason

    def test_blocked_category(self):
        ctx = make_test_context(category="gambling")
        config = PolicyConfig(blocked_categories={"gambling", "crypto"})
        decision = evaluate_policy(ctx, config)
        assert decision.decision == GatewayDecision.BLOCK
        assert "BLOCKED_CATEGORY" in [r.rule_id for r in decision.triggered_rules]
        assert "gambling" in decision.primary_reason

    def test_unsupported_currency(self):
        ctx = make_test_context(currency="USD")
        config = PolicyConfig(allowed_currencies={"INR"})
        decision = evaluate_policy(ctx, config)
        assert decision.decision == GatewayDecision.BLOCK
        assert "UNSUPPORTED_CURRENCY" in [r.rule_id for r in decision.triggered_rules]

    def test_recurring_mandate_blocked(self):
        ctx = make_test_context(mandate_type=MandateType.recurring)
        config = PolicyConfig(recurring_mandate_policy=RecurringMandatePolicy.blocked)
        decision = evaluate_policy(ctx, config)
        assert decision.decision == GatewayDecision.BLOCK
        assert "RECURRING_MANDATES_BLOCKED" in [r.rule_id for r in decision.triggered_rules]

    def test_configured_allowlist_missing_category(self):
        ctx = make_test_context(category="electronics")
        config = PolicyConfig(allowed_categories={"software", "books"})
        decision = evaluate_policy(ctx, config)
        assert decision.decision == GatewayDecision.BLOCK
        assert "CATEGORY_NOT_ALLOWED" in [r.rule_id for r in decision.triggered_rules]

    def test_fail_closed_missing_category_on_allowlist(self):
        ctx = make_test_context(category=None)
        config = PolicyConfig(allowed_categories={"software"})
        decision = evaluate_policy(ctx, config)
        assert decision.decision == GatewayDecision.BLOCK
        assert "MISSING_CATEGORY" in [r.rule_id for r in decision.triggered_rules]

    def test_fail_closed_missing_category_on_denylist(self):
        ctx = make_test_context(category=None)
        config = PolicyConfig(blocked_categories={"gambling"})
        decision = evaluate_policy(ctx, config)
        assert decision.decision == GatewayDecision.BLOCK
        assert "MISSING_CATEGORY" in [r.rule_id for r in decision.triggered_rules]


class TestPrecedence:
    def test_block_overrides_review(self):
        # Above max amount (BLOCK) AND recurring mandate review (REVIEW)
        ctx = make_test_context(amount_minor=15000, mandate_type=MandateType.recurring)
        config = PolicyConfig(
            max_transaction_amount=10000,
            recurring_mandate_policy=RecurringMandatePolicy.review,
        )
        decision = evaluate_policy(ctx, config)
        assert decision.decision == GatewayDecision.BLOCK

    def test_review_overrides_allow(self):
        ctx = make_test_context(amount_minor=6000)
        config = PolicyConfig(review_threshold_amount=5000, allowed_currencies={"INR"})
        decision = evaluate_policy(ctx, config)
        assert decision.decision == GatewayDecision.REVIEW


class TestConfiguration:
    def test_review_threshold_greater_than_max_raises_error(self):
        with pytest.raises(ValidationError):
            PolicyConfig(max_transaction_amount=5000, review_threshold_amount=10000)


class TestExplainabilityAndDeterminism:
    def test_decision_is_deterministic(self):
        ctx = make_test_context(amount_minor=6000)
        config = PolicyConfig(review_threshold_amount=5000)
        decision1 = evaluate_policy(ctx, config)
        decision2 = evaluate_policy(ctx, config)
        assert decision1 == decision2

    def test_reason_is_clear(self):
        ctx = make_test_context(amount_minor=1500000)
        config = PolicyConfig(max_transaction_amount=1000000)
        decision = evaluate_policy(ctx, config)
        assert "10,000" in decision.primary_reason
