"""
Merchant Policy Engine — Agent Commerce Gateway
==================================================

Answers the question:
    **Given an authenticated transaction, should this merchant ALLOW it,
    send it for REVIEW, or BLOCK it?**

This module evaluates a cryptographically validated CommerceContext against
a deterministic Merchant PolicyConfig.

Security boundaries:
    - Assumes the mandate signature has already been verified (`app/core/mandate.py`).
    - Does NOT verify signatures or temporal state.
    - Does NOT execute payments or manage replay protection.

Precedence rules (evaluated in order):
    1. Missing / malformed policy input → BLOCK
    2. Hard security/business violation (e.g. amount exceeds absolute max) → BLOCK
    3. Explicit blocked category/currency/mandate rule → BLOCK
    4. Review condition → REVIEW
    5. All configured checks pass → ALLOW

Fail-closed contract:
    - If a rule is CONFIGURED but the request data required to evaluate it
      is missing or malformed, the evaluation fails closed → BLOCK.
    - (If a rule is NOT configured, it does not apply).
    - Internal exceptions fail closed → BLOCK.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Set

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.schemas import CommerceContext, GatewayDecision, MandateType, Money


# ══════════════════════════════════════════════════════════════════════════════
# Output Model
# ══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class TriggeredRule:
    rule_id: str
    decision: GatewayDecision
    reason: str


@dataclass(frozen=True)
class PolicyDecision:
    """
    Result of a policy evaluation.

    Attributes:
        decision:        ALLOW, REVIEW, or BLOCK.
        primary_reason:  A clear, human-readable explanation of the primary outcome.
        triggered_rules: List of specific rules that influenced the decision.
    """
    decision: GatewayDecision
    primary_reason: str
    triggered_rules: List[TriggeredRule]

    def __repr__(self) -> str:
        return f"PolicyDecision({self.decision.value}: {self.primary_reason})"


# ══════════════════════════════════════════════════════════════════════════════
# Configuration Models
# ══════════════════════════════════════════════════════════════════════════════


class RecurringMandatePolicy(str, Enum):
    allowed = "allowed"
    review = "review"
    blocked = "blocked"


class PolicyConfig(BaseModel):
    """
    Merchant policy configuration.

    Defines the deterministic rules against which transactions are evaluated.
    All monetary amounts are in minor units (e.g., paise, cents) and must
    match the currency of the transaction. For simplicity in this early stage,
    the policy engine assumes the config limits are in the same currency as
    the request (cross-currency policy evaluation is not implemented).
    """
    model_config = ConfigDict(frozen=True)

    # ── Limits ──────────────────────────────────────────────────────────────
    max_transaction_amount: Optional[int] = Field(
        default=None,
        ge=0,
        description="Absolute maximum allowed transaction amount (minor units). "
                    "Transactions above this are BLOCKED.",
    )
    review_threshold_amount: Optional[int] = Field(
        default=None,
        ge=0,
        description="Amount (minor units) above which a transaction requires REVIEW. "
                    "Must be <= max_transaction_amount if both are set.",
    )

    # ── Categorization ──────────────────────────────────────────────────────
    allowed_categories: Optional[Set[str]] = Field(
        default=None,
        description="If set, ANY item category not in this list results in a BLOCK.",
    )
    blocked_categories: Optional[Set[str]] = Field(
        default=None,
        description="If set, ANY item category in this list results in a BLOCK.",
    )

    # ── Currency ────────────────────────────────────────────────────────────
    allowed_currencies: Optional[Set[str]] = Field(
        default=None,
        description="If set, the transaction currency must be in this list or BLOCK.",
    )

    # ── Mandate specific ────────────────────────────────────────────────────
    recurring_mandate_policy: RecurringMandatePolicy = Field(
        default=RecurringMandatePolicy.blocked,
        description="How to handle recurring mandates.",
    )

    @model_validator(mode="after")
    def _validate_thresholds(self) -> "PolicyConfig":
        if (
            self.max_transaction_amount is not None
            and self.review_threshold_amount is not None
            and self.review_threshold_amount > self.max_transaction_amount
        ):
            raise ValueError("review_threshold_amount cannot exceed max_transaction_amount")
        return self


# ══════════════════════════════════════════════════════════════════════════════
# Evaluation Logic
# ══════════════════════════════════════════════════════════════════════════════


def _fail_closed(reason: str, rule_id: str = "INTERNAL_ERROR") -> PolicyDecision:
    return PolicyDecision(
        decision=GatewayDecision.BLOCK,
        primary_reason=reason,
        triggered_rules=[TriggeredRule(rule_id=rule_id, decision=GatewayDecision.BLOCK, reason=reason)],
    )


def evaluate_policy(context: CommerceContext, config: PolicyConfig) -> PolicyDecision:
    """
    Evaluate a transaction against merchant policy.

    Args:
        context: The cryptographically authorized CommerceContext.
        config:  The merchant's PolicyConfig.

    Returns:
        A deterministic PolicyDecision.
    """
    try:
        req = context.request
        total = req.calculated_total.amount_minor
        currency = req.calculated_total.currency
        is_recurring = context.is_recurring
        
        triggered_rules: List[TriggeredRule] = []

        # ── 1. HARD BLOCK RULES (Highest Precedence) ──────────────────────────

        # Currency check
        if config.allowed_currencies is not None:
            if currency not in config.allowed_currencies:
                rule = TriggeredRule(
                    rule_id="UNSUPPORTED_CURRENCY",
                    decision=GatewayDecision.BLOCK,
                    reason=f"Currency '{currency}' is not supported by merchant policy.",
                )
                triggered_rules.append(rule)
                # Fail fast on block
                return PolicyDecision(GatewayDecision.BLOCK, rule.reason, triggered_rules)

        # Max Amount check
        if config.max_transaction_amount is not None:
            if total > config.max_transaction_amount:
                rule = TriggeredRule(
                    rule_id="MAX_TRANSACTION_AMOUNT",
                    decision=GatewayDecision.BLOCK,
                    reason=f"Transaction exceeds the merchant maximum of \u20b9{config.max_transaction_amount/100:,.0f}.",
                )
                triggered_rules.append(rule)
                return PolicyDecision(GatewayDecision.BLOCK, rule.reason, triggered_rules)

        # Blocked Categories check
        if config.blocked_categories is not None:
            for item in req.items:
                if not item.category:
                    # Fail closed: rule is configured, but we can't evaluate it safely
                    rule = TriggeredRule(
                        rule_id="MISSING_CATEGORY",
                        decision=GatewayDecision.BLOCK,
                        reason="Item is missing a category, but merchant has a blocked categories list.",
                    )
                    triggered_rules.append(rule)
                    return PolicyDecision(GatewayDecision.BLOCK, rule.reason, triggered_rules)
                if item.category in config.blocked_categories:
                    rule = TriggeredRule(
                        rule_id="BLOCKED_CATEGORY",
                        decision=GatewayDecision.BLOCK,
                        reason=f"Product category '{item.category}' is blocked by merchant policy.",
                    )
                    triggered_rules.append(rule)
                    return PolicyDecision(GatewayDecision.BLOCK, rule.reason, triggered_rules)

        # Allowed Categories check
        if config.allowed_categories is not None:
            for item in req.items:
                if not item.category:
                    rule = TriggeredRule(
                        rule_id="MISSING_CATEGORY",
                        decision=GatewayDecision.BLOCK,
                        reason="Item is missing a category, but merchant has an allowed categories list.",
                    )
                    triggered_rules.append(rule)
                    return PolicyDecision(GatewayDecision.BLOCK, rule.reason, triggered_rules)
                if item.category not in config.allowed_categories:
                    rule = TriggeredRule(
                        rule_id="CATEGORY_NOT_ALLOWED",
                        decision=GatewayDecision.BLOCK,
                        reason=f"Product category '{item.category}' is not on the merchant's allow-list.",
                    )
                    triggered_rules.append(rule)
                    return PolicyDecision(GatewayDecision.BLOCK, rule.reason, triggered_rules)

        # Recurring transaction check (BLOCK cases)
        if is_recurring:
            if config.recurring_mandate_policy == RecurringMandatePolicy.blocked:
                rule = TriggeredRule(
                    rule_id="RECURRING_MANDATES_BLOCKED",
                    decision=GatewayDecision.BLOCK,
                    reason="Recurring mandates are disabled for this merchant.",
                )
                triggered_rules.append(rule)
                return PolicyDecision(GatewayDecision.BLOCK, rule.reason, triggered_rules)


        # ── 2. REVIEW RULES (Lower Precedence) ────────────────────────────────

        review_reasons = []

        # Review Threshold check
        if config.review_threshold_amount is not None:
            if total > config.review_threshold_amount:
                rule = TriggeredRule(
                    rule_id="REVIEW_THRESHOLD",
                    decision=GatewayDecision.REVIEW,
                    reason=f"Transaction exceeds the merchant review threshold of \u20b9{config.review_threshold_amount/100:,.0f}.",
                )
                triggered_rules.append(rule)
                review_reasons.append(rule.reason)

        # Recurring transaction check (REVIEW cases)
        if is_recurring:
            if config.recurring_mandate_policy == RecurringMandatePolicy.review:
                rule = TriggeredRule(
                    rule_id="RECURRING_MANDATES_REVIEW",
                    decision=GatewayDecision.REVIEW,
                    reason="Recurring mandates require secondary review by merchant policy.",
                )
                triggered_rules.append(rule)
                review_reasons.append(rule.reason)

        if review_reasons:
            return PolicyDecision(
                decision=GatewayDecision.REVIEW,
                primary_reason=review_reasons[0], # Surface the first review reason as primary
                triggered_rules=triggered_rules,
            )


        # ── 3. ALLOW (All checks passed) ──────────────────────────────────────

        rule = TriggeredRule(
            rule_id="ALL_CHECKS_PASSED",
            decision=GatewayDecision.ALLOW,
            reason="Transaction is within merchant limits and all configured policies passed.",
        )
        triggered_rules.append(rule)
        return PolicyDecision(GatewayDecision.ALLOW, rule.reason, triggered_rules)

    except Exception as e:
        logging.exception("Internal policy evaluation error")
        return _fail_closed(f"Internal policy evaluation error: {e}")
