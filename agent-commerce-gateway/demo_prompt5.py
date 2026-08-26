"""
Prompt 5 Demonstration — Security-Controlled Transaction Pipeline
=================================================================

Six scenarios demonstrating the complete security pipeline:

    Case 1 — Legitimate first attempt       → ALLOW
    Case 2 — Replay (same nonce)            → BLOCK (REPLAY stage)
    Case 3 — Forged authorization           → BLOCK (AUTHORIZATION stage)
    Case 4 — Policy violation               → BLOCK (POLICY stage)
    Case 5 — Policy review                  → REVIEW
    Case 6 — Replay store database failure  → BLOCK (REPLAY stage)

NOTE:
    - Razorpay is NOT invoked in any scenario.
    - An ALLOW result is NOT a payment. It is authorization for a future
      payment execution layer (not implemented in this prompt).
    - A REVIEW result requires secondary review before any payment.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from nacl.signing import SigningKey

from app.core.mandate import sign_mandate
from app.core.orchestrator import process_transaction
from app.core.policy import PolicyConfig, RecurringMandatePolicy
from app.core.replay import ReplayResult, ReplayStore, SQLiteReplayStore
from app.core.schemas import (
    BuyerProtocol,
    CommerceItem,
    CommerceRequest,
    GatewayDecision,
    Mandate,
    MandateStatus,
    MandateType,
    Money,
)
from app.core.transaction_result import PipelineStage, TransactionResult
from app.db.database import get_engine, get_session_factory, init_db

# ── Shared time reference ────────────────────────────────────────────────────

NOW = datetime(2026, 8, 21, 15, 0, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=2)
MUCH_LATER = NOW + timedelta(days=30)


# ── Helpers ──────────────────────────────────────────────────────────────────


def make_inr(amount_minor: int) -> Money:
    return Money(amount_minor=amount_minor, currency="INR")


def make_mandate(
    nonce: str = "mandate-nonce-demo",
    mandate_type: MandateType = MandateType.one_time,
    expires_at: datetime = MUCH_LATER,
) -> Mandate:
    return Mandate(
        mandate_id="mandate-demo-001",
        buyer_agent_id="agent-demo",
        merchant_id="merchant-demo-01",
        max_amount=make_inr(1_000_000),  # ₹10,000
        mandate_type=mandate_type,
        status=MandateStatus.active,
        issued_at=NOW,
        expires_at=expires_at,
        nonce=nonce,
        authorization_method="ed25519",
        authorization_ref="key-demo-001",
    )


def make_request(
    transaction_id: str = "txn-demo-001",
    amount_minor: int = 150_000,  # ₹1,500
) -> CommerceRequest:
    item = CommerceItem(
        product_id="prod-software-001",
        name="Analytics Pro Subscription",
        quantity=1,
        unit_price=make_inr(amount_minor),
        category="software",
    )
    return CommerceRequest(
        transaction_id=transaction_id,
        created_at=NOW,
        expires_at=LATER,
        nonce=f"req-{transaction_id}",
        buyer_agent_id="agent-demo",
        buyer_protocol=BuyerProtocol.x402,
        merchant_id="merchant-demo-01",
        items=[item],
        receipt_destination_protocol=BuyerProtocol.x402,
        receipt_destination_ref="callback://agent-demo/receipts",
    )


# ── In-memory DB for demo ────────────────────────────────────────────────────

engine = get_engine("sqlite:///:memory:")
init_db(engine)
factory = get_session_factory(engine)


def fresh_store() -> SQLiteReplayStore:
    return SQLiteReplayStore(factory())


# ── Replay stores for specific scenarios ─────────────────────────────────────


class _DatabaseFailureReplayStore(ReplayStore):
    """Simulates a replay store where the database is unavailable."""

    def check_and_reserve_authorization_nonce(self, nonce: str) -> ReplayResult:
        return ReplayResult(
            allowed=False,
            was_replay=False,
            reason="Replay protection unavailable; transaction blocked.",
        )

    def check_and_reserve_transaction_id(self, transaction_id: str) -> ReplayResult:
        return ReplayResult(
            allowed=False,
            was_replay=False,
            reason="Replay protection unavailable; transaction blocked.",
        )


# ── Signing key pair for demo ────────────────────────────────────────────────

SIGNING_KEY = SigningKey.generate()
VERIFY_KEY = SIGNING_KEY.verify_key

# ── Policy: allows ₹1,500 software (below ₹2,000 review threshold) ──────────

STANDARD_POLICY = PolicyConfig(
    max_transaction_amount=1_000_000,  # ₹10,000 max
    review_threshold_amount=200_000,   # ₹2,000 review threshold
    allowed_currencies={"INR"},
    allowed_categories={"software", "subscription"},
)

# ── Display helpers ──────────────────────────────────────────────────────────

SEPARATOR = "─" * 65


def print_result(result: TransactionResult, label: str) -> None:
    icon = {"ALLOW": "✅", "REVIEW": "🔍", "BLOCK": "🚫"}.get(result.decision.value, "?")
    print(f"\n{SEPARATOR}")
    print(f"  {icon}  {label}")
    print(SEPARATOR)
    print(f"  Transaction ID : {result.transaction_id}")
    print(f"  Decision       : {result.decision.value}")
    print(f"  Stage reached  : {result.stage_reached.value}")
    print(f"  State          : {result.processing_state.value}")
    print(f"  Reason         : {result.reason}")

    if result.authorization_result is not None:
        auth_icon = "✓" if result.authorization_result.valid else "✗"
        print(f"  Auth check     : {auth_icon}  {result.authorization_result.reason}")

    if result.replay_result is not None:
        replay_icon = "✓" if result.replay_result.allowed else "✗"
        print(f"  Replay check   : {replay_icon}  {result.replay_result.reason}")

    if result.policy_result is not None:
        policy_icon = {"ALLOW": "✓", "REVIEW": "~", "BLOCK": "✗"}.get(
            result.policy_result.decision.value, "?"
        )
        print(f"  Policy check   : {policy_icon}  {result.policy_result.primary_reason}")

    print()


# ════════════════════════════════════════════════════════════════════════════
# SCENARIO 1 — Legitimate first attempt
# ════════════════════════════════════════════════════════════════════════════

print("\n" + "═" * 65)
print("  PROMPT 5 DEMO — Transaction Security Pipeline")
print("═" * 65)

mandate_s1 = make_mandate(nonce="nonce-case-1")
signed_s1 = sign_mandate(mandate_s1, SIGNING_KEY, "key-demo-001")
store_s1 = fresh_store()

result_s1 = process_transaction(
    request=make_request(transaction_id="txn-case-1"),
    signed_authorization=signed_s1,
    policy_config=STANDARD_POLICY,
    public_key=VERIFY_KEY,
    replay_store=store_s1,
    current_time=NOW,
)
print_result(result_s1, "Case 1 — Legitimate first attempt")
assert result_s1.decision == GatewayDecision.ALLOW, f"Case 1 failed: {result_s1}"

# ════════════════════════════════════════════════════════════════════════════
# SCENARIO 2 — Replay: same authorization submitted again
# ════════════════════════════════════════════════════════════════════════════

# Reuse the same store (same DB) — nonce-case-1 is already consumed
result_s2 = process_transaction(
    request=make_request(transaction_id="txn-case-2-replay"),
    signed_authorization=signed_s1,  # same signed authorization as Case 1
    policy_config=STANDARD_POLICY,
    public_key=VERIFY_KEY,
    replay_store=store_s1,
    current_time=NOW,
)
print_result(result_s2, "Case 2 — Replay (same nonce resubmitted)")
assert result_s2.decision == GatewayDecision.BLOCK, f"Case 2 failed: {result_s2}"
assert result_s2.stage_reached == PipelineStage.REPLAY, f"Case 2 stage wrong: {result_s2}"
assert result_s2.policy_result is None, "Case 2: policy must NOT be evaluated on replay"

# ════════════════════════════════════════════════════════════════════════════
# SCENARIO 3 — Forged authorization (wrong signing key)
# ════════════════════════════════════════════════════════════════════════════

forged_key = SigningKey.generate()  # attacker's key — not trusted
mandate_s3 = make_mandate(nonce="nonce-case-3")
signed_s3_forged = sign_mandate(mandate_s3, forged_key, "key-demo-001")

result_s3 = process_transaction(
    request=make_request(transaction_id="txn-case-3"),
    signed_authorization=signed_s3_forged,
    policy_config=STANDARD_POLICY,
    public_key=VERIFY_KEY,  # legitimate verify key — won't match forged sig
    replay_store=fresh_store(),
    current_time=NOW,
)
print_result(result_s3, "Case 3 — Forged authorization (wrong key)")
assert result_s3.decision == GatewayDecision.BLOCK, f"Case 3 failed: {result_s3}"
assert result_s3.stage_reached == PipelineStage.AUTHORIZATION, f"Case 3 stage wrong: {result_s3}"
assert result_s3.policy_result is None, "Case 3: policy must NOT be called on forged auth"
assert result_s3.replay_result is None, "Case 3: replay must NOT run on forged auth"

# ════════════════════════════════════════════════════════════════════════════
# SCENARIO 4 — Policy violation (blocked category)
# ════════════════════════════════════════════════════════════════════════════

policy_block = PolicyConfig(
    max_transaction_amount=1_000_000,
    blocked_categories={"software"},  # our category is blocked
)

mandate_s4 = make_mandate(nonce="nonce-case-4")
signed_s4 = sign_mandate(mandate_s4, SIGNING_KEY, "key-demo-001")

result_s4 = process_transaction(
    request=make_request(transaction_id="txn-case-4"),
    signed_authorization=signed_s4,
    policy_config=policy_block,
    public_key=VERIFY_KEY,
    replay_store=fresh_store(),
    current_time=NOW,
)
print_result(result_s4, "Case 4 — Policy violation (blocked category)")
assert result_s4.decision == GatewayDecision.BLOCK, f"Case 4 failed: {result_s4}"
assert result_s4.stage_reached == PipelineStage.POLICY, f"Case 4 stage wrong: {result_s4}"
assert result_s4.authorization_result is not None, "Case 4: auth result must be present"
assert result_s4.replay_result is not None, "Case 4: replay result must be present"
assert result_s4.policy_result is not None, "Case 4: policy result must be present"

# ════════════════════════════════════════════════════════════════════════════
# SCENARIO 5 — Policy review (amount above review threshold)
# ════════════════════════════════════════════════════════════════════════════

policy_review = PolicyConfig(
    max_transaction_amount=1_000_000,
    review_threshold_amount=100_000,  # ₹1,000 — our ₹1,500 exceeds this
)

mandate_s5 = make_mandate(nonce="nonce-case-5")
signed_s5 = sign_mandate(mandate_s5, SIGNING_KEY, "key-demo-001")

result_s5 = process_transaction(
    request=make_request(transaction_id="txn-case-5", amount_minor=150_000),
    signed_authorization=signed_s5,
    policy_config=policy_review,
    public_key=VERIFY_KEY,
    replay_store=fresh_store(),
    current_time=NOW,
)
print_result(result_s5, "Case 5 — Policy REVIEW (amount above review threshold)")
assert result_s5.decision == GatewayDecision.REVIEW, f"Case 5 failed: {result_s5}"
assert result_s5.stage_reached == PipelineStage.FINAL, f"Case 5 stage wrong: {result_s5}"

# ════════════════════════════════════════════════════════════════════════════
# SCENARIO 6 — Replay store database failure (fail closed)
# ════════════════════════════════════════════════════════════════════════════

mandate_s6 = make_mandate(nonce="nonce-case-6")
signed_s6 = sign_mandate(mandate_s6, SIGNING_KEY, "key-demo-001")

result_s6 = process_transaction(
    request=make_request(transaction_id="txn-case-6"),
    signed_authorization=signed_s6,
    policy_config=STANDARD_POLICY,
    public_key=VERIFY_KEY,
    replay_store=_DatabaseFailureReplayStore(),
    current_time=NOW,
)
print_result(result_s6, "Case 6 — Replay store unavailable (fail closed)")
assert result_s6.decision == GatewayDecision.BLOCK, f"Case 6 failed: {result_s6}"
assert result_s6.stage_reached == PipelineStage.REPLAY, f"Case 6 stage wrong: {result_s6}"
assert result_s6.policy_result is None, "Case 6: policy must NOT run on store failure"

# ════════════════════════════════════════════════════════════════════════════
# Summary
# ════════════════════════════════════════════════════════════════════════════

print(SEPARATOR)
print("  All 6 scenarios passed.")
print(SEPARATOR)
print()
print("  Security guarantees demonstrated:")
print("  ✅ Case 1: Valid authorization, unused nonce, policy ALLOW → ALLOW")
print("  🚫 Case 2: Same nonce resubmitted → BLOCK at REPLAY (policy not reached)")
print("  🚫 Case 3: Forged signature → BLOCK at AUTHORIZATION (replay not reached)")
print("  🚫 Case 4: Policy violation → BLOCK at POLICY (auth + replay passed)")
print("  🔍 Case 5: Policy REVIEW → REVIEW (no payment execution)")
print("  🚫 Case 6: Replay store down → BLOCK at REPLAY (fail closed)")
print()
print("  ⚠️  NOTE: No Razorpay transaction was initiated.")
print("           An ALLOW result is authorization for a future payment layer only.")
print()

# Prove Razorpay was not imported
razorpay_modules = [k for k in sys.modules if "razorpay" in k.lower()]
if razorpay_modules:
    print(f"  ❌ RAZORPAY MODULES DETECTED: {razorpay_modules}")
    sys.exit(1)
else:
    print("  ✅ Razorpay was NOT imported or invoked.")
print()
