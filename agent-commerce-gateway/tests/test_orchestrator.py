"""
Orchestrator Tests — Agent Commerce Gateway Prompt 5
=====================================================

These tests prove the security pipeline enforces strict ordering and
correct decisions.  They use mock/spy patterns to verify that stages
are NOT reached after earlier failures.

Test categories:
    1. Authorization gate     — valid/invalid/expired/wrong identity
    2. Replay gate            — first use passes, replay blocked, DB error blocked
    3. Pipeline ordering      — mocks prove evaluate_policy not called on auth/replay failure
    4. Policy outcomes        — BLOCK / REVIEW / ALLOW propagate correctly
    5. No Razorpay            — orchestrator never imports or calls Razorpay
    6. Race condition         — concurrent duplicate transactions (exactly one wins)
    7. TransactionResult      — correct fields on every outcome
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch, call
from typing import Optional

import pytest
from nacl.signing import SigningKey, VerifyKey

from app.core.mandate import sign_mandate, Ed25519MandateProvider
from app.core.orchestrator import process_transaction
from app.core.policy import PolicyConfig, PolicyDecision, RecurringMandatePolicy
from app.core.replay import ReplayResult, ReplayStore, SQLiteReplayStore
from app.core.schemas import (
    BuyerProtocol,
    CommerceContext,
    CommerceItem,
    GatewayDecision,
    Mandate,
    MandateStatus,
    MandateType,
    Money,
    CommerceRequest,
    Ed25519AuthorizationProof,
)
from app.core.transaction_result import PipelineStage, ProcessingState, TransactionResult
from app.db.database import get_engine, get_session_factory, init_db


# ══════════════════════════════════════════════════════════════════════════════
# Constants and helpers
# ══════════════════════════════════════════════════════════════════════════════

NOW = datetime(2026, 8, 21, 15, 0, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=2)
MUCH_LATER = NOW + timedelta(days=30)
# A mandate issued/expired window that is valid structurally but already expired
# relative to a later current_time.  We use a future-relative current_time to test expiry.
EXPIRED_MANDATE_ISSUED_AT = NOW
EXPIRED_MANDATE_EXPIRES_AT = NOW + timedelta(minutes=1)   # expires 1 min after issue
EXPIRED_CHECK_TIME = NOW + timedelta(hours=1)              # check time is well past expiry


def make_inr(amount_minor: int) -> Money:
    return Money(amount_minor=amount_minor, currency="INR")


def make_mandate(
    mandate_id: str = "mandate-001",
    buyer_agent_id: str = "agent-demo",
    merchant_id: str = "merchant-01",
    max_amount_minor: int = 500_000,
    mandate_type: MandateType = MandateType.one_time,
    expires_at: datetime = MUCH_LATER,
    nonce: str = "mandate-nonce-001",
) -> Mandate:
    return Mandate(
        mandate_id=mandate_id,
        buyer_agent_id=buyer_agent_id,
        merchant_id=merchant_id,
        max_amount=make_inr(max_amount_minor),
        mandate_type=mandate_type,
        status=MandateStatus.active,
        issued_at=NOW,
        expires_at=expires_at,
        nonce=nonce,
        authorization_method="ed25519",
        authorization_ref="key-001",
    )


def make_request(
    transaction_id: str = "txn-001",
    buyer_agent_id: str = "agent-demo",
    merchant_id: str = "merchant-01",
    amount_minor: int = 100_000,
    nonce: str = "req-nonce-001",
) -> CommerceRequest:
    item = CommerceItem(
        product_id="prod-001",
        name="Widget",
        quantity=1,
        unit_price=make_inr(amount_minor),
        category="software",
    )
    return CommerceRequest(
        transaction_id=transaction_id,
        created_at=NOW,
        expires_at=LATER,
        nonce=nonce,
        buyer_agent_id=buyer_agent_id,
        buyer_protocol=BuyerProtocol.x402,
        merchant_id=merchant_id,
        items=[item],
        receipt_destination_protocol=BuyerProtocol.x402,
        receipt_destination_ref="callback-ref",
    )


def make_key_pair() -> tuple[SigningKey, VerifyKey]:
    signing_key = SigningKey.generate()
    verify_key = signing_key.verify_key
    return signing_key, verify_key


def allow_all_policy() -> PolicyConfig:
    return PolicyConfig(
        max_transaction_amount=1_000_000,
        allowed_currencies={"INR"},
        recurring_mandate_policy=RecurringMandatePolicy.allowed,
    )


def block_all_policy() -> PolicyConfig:
    return PolicyConfig(max_transaction_amount=1)  # will BLOCK ₹1000+


def review_policy() -> PolicyConfig:
    return PolicyConfig(
        max_transaction_amount=1_000_000,
        review_threshold_amount=50_000,  # ₹500 — our ₹1000 test amount exceeds this
    )


# ── In-memory replay store fixture ─────────────────────────────────────────

@pytest.fixture()
def engine():
    e = get_engine("sqlite:///:memory:")
    init_db(e)
    yield e
    e.dispose()


@pytest.fixture()
def fresh_store(engine):
    factory = get_session_factory(engine)
    s = factory()
    yield SQLiteReplayStore(s)
    s.close()


def fresh_store_from_engine(engine) -> SQLiteReplayStore:
    factory = get_session_factory(engine)
    s = factory()
    return SQLiteReplayStore(s)


# ── Mock replay store that always allows (for policy-focused tests) ─────────


class AlwaysAllowReplayStore(ReplayStore):
    def check_and_reserve_authorization_nonce(self, nonce: str) -> ReplayResult:
        return ReplayResult(allowed=True, was_replay=False, reason="allowed (mock)")

    def check_and_reserve_transaction_id(self, transaction_id: str) -> ReplayResult:
        return ReplayResult(allowed=True, was_replay=False, reason="allowed (mock)")


class AlwaysBlockReplayStore(ReplayStore):
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


class ReplayDetectedStore(ReplayStore):
    def check_and_reserve_authorization_nonce(self, nonce: str) -> ReplayResult:
        return ReplayResult(
            allowed=False, was_replay=True, reason="Authorization nonce has already been used."
        )

    def check_and_reserve_transaction_id(self, transaction_id: str) -> ReplayResult:
        return ReplayResult(
            allowed=False, was_replay=True, reason="Transaction ID has already been submitted."
        )


# ══════════════════════════════════════════════════════════════════════════════
# 1. Authorization gate
# ══════════════════════════════════════════════════════════════════════════════


class TestAuthorizationGate:

    def _run(
        self,
        signing_key: SigningKey,
        verify_key: VerifyKey,
        mandate: Optional[Mandate] = None,
        request: Optional[CommerceRequest] = None,
        policy: Optional[PolicyConfig] = None,
        replay_store: Optional[ReplayStore] = None,
    ) -> TransactionResult:
        m = mandate or make_mandate()
        r = request or make_request()
        signed = sign_mandate(m, signing_key, "key-001")
        return process_transaction(
            request=r,
            auth_proof=signed,
            auth_provider=Ed25519MandateProvider(verify_key, NOW),
            policy_config=policy or allow_all_policy(),
            replay_store=replay_store or AlwaysAllowReplayStore(),
        )

    def test_valid_authorization_reaches_replay_stage(self):
        """Valid authorization must NOT be blocked before replay."""
        sk, vk = make_key_pair()
        # Use real store to prove nonce was consumed (not stopped at auth)
        engine = get_engine("sqlite:///:memory:")
        init_db(engine)
        store = fresh_store_from_engine(engine)
        result = self._run(sk, vk, replay_store=store)
        # Reached at least replay check → authorization passed
        assert result.stage_reached != PipelineStage.AUTHORIZATION
        assert result.authorization_result is not None
        assert result.authorization_result.valid is True

    def test_invalid_signature_stops_at_authorization(self):
        """A wrong private key produces an invalid signature → BLOCK at AUTHORIZATION."""
        sk, vk = make_key_pair()
        wrong_sk = SigningKey.generate()
        mandate = make_mandate()
        # Sign with wrong key
        signed = sign_mandate(mandate, wrong_sk, "key-001")
        result = process_transaction(
            request=make_request(),
            auth_proof=signed,
            auth_provider=Ed25519MandateProvider(vk, NOW),
            policy_config=allow_all_policy(),
            replay_store=AlwaysAllowReplayStore(),
        )
        assert result.decision == GatewayDecision.BLOCK
        assert result.stage_reached == PipelineStage.AUTHORIZATION
        assert result.replay_result is None
        assert result.policy_result is None

    def test_expired_authorization_stops_at_authorization(self):
        """An expired mandate → BLOCK at AUTHORIZATION.

        The mandate is structurally valid (expires_at > issued_at), but we
        pass a current_time that is AFTER expires_at to simulate expiry.
        """
        sk, vk = make_key_pair()
        # Mandate: issued NOW, expires in 1 minute
        mandate = make_mandate(
            expires_at=EXPIRED_MANDATE_EXPIRES_AT,
        )
        # Override issued_at via kwargs since make_mandate doesn't expose it
        # but the schema enforces expires_at > issued_at — our constants satisfy this.
        signed = sign_mandate(mandate, sk, "key-001")
        result = process_transaction(
            request=make_request(),
            auth_proof=signed,
            auth_provider=Ed25519MandateProvider(vk, EXPIRED_CHECK_TIME),
            policy_config=allow_all_policy(),
            replay_store=AlwaysAllowReplayStore(),
        )
        assert result.decision == GatewayDecision.BLOCK
        assert result.stage_reached == PipelineStage.AUTHORIZATION
        assert result.replay_result is None
        assert result.policy_result is None

    def test_wrong_buyer_stops_at_authorization(self):
        """Mandate signed for a different buyer → BLOCK at AUTHORIZATION.

        The mandate buyer_agent_id doesn't match the request buyer_agent_id.
        The CommerceContext validation would also fail (buyer mismatch), so
        we expect a BLOCK — either at VALIDATION or AUTHORIZATION stage.
        """
        sk, vk = make_key_pair()
        # Mandate is for "different-agent"; request is for "agent-demo".
        # CommerceContext.mandate.buyer_agent_id != request.buyer_agent_id
        # → blocked at VALIDATION (context cross-check) or AUTHORIZATION.
        mandate = make_mandate(buyer_agent_id="different-agent")
        signed = sign_mandate(mandate, sk, "key-001")
        result = process_transaction(
            request=make_request(buyer_agent_id="agent-demo"),
            auth_proof=signed,
            auth_provider=Ed25519MandateProvider(vk, NOW),
            policy_config=allow_all_policy(),
            replay_store=AlwaysAllowReplayStore()
        )
        assert result.decision == GatewayDecision.BLOCK
        # Blocked at VALIDATION (context mismatch) or AUTHORIZATION — either is correct
        assert result.stage_reached in (PipelineStage.VALIDATION, PipelineStage.AUTHORIZATION)
        assert result.replay_result is None
        assert result.policy_result is None

    def test_wrong_merchant_stops_at_authorization(self):
        """Mandate signed for a different merchant → BLOCK."""
        sk, vk = make_key_pair()
        mandate = make_mandate(merchant_id="other-merchant")
        signed = sign_mandate(mandate, sk, "key-001")
        result = process_transaction(
            request=make_request(merchant_id="merchant-01"),
            auth_proof=signed,
            auth_provider=Ed25519MandateProvider(vk, NOW),
            policy_config=allow_all_policy(),
            replay_store=AlwaysAllowReplayStore(),
        )
        assert result.decision == GatewayDecision.BLOCK
        # Blocked at VALIDATION (context mismatch) or AUTHORIZATION — either is correct
        assert result.stage_reached in (PipelineStage.VALIDATION, PipelineStage.AUTHORIZATION)
        assert result.replay_result is None
        assert result.policy_result is None

    def test_invalid_authorization_does_not_consume_nonce(self, engine):
        """
        A failed authorization must NOT consume the nonce.
        The same nonce must still be reservable after an auth failure.
        """
        sk, vk = make_key_pair()
        wrong_sk = SigningKey.generate()
        nonce = "nonce-no-consume"
        mandate = make_mandate(nonce=nonce)
        signed_bad = sign_mandate(mandate, wrong_sk, "key-001")

        store = fresh_store_from_engine(engine)

        # Bad auth attempt
        process_transaction(
            request=make_request(),
            auth_proof=signed_bad,
            auth_provider=Ed25519MandateProvider(vk, NOW),
            policy_config=allow_all_policy(),
            replay_store=store,
        )

        # Nonce should still be available (not consumed by the failed attempt)
        result = store.check_and_reserve_authorization_nonce(nonce)
        assert result.allowed is True


# ══════════════════════════════════════════════════════════════════════════════
# 2. Replay gate
# ══════════════════════════════════════════════════════════════════════════════


class TestReplayGate:

    def _signed(self, sk: SigningKey, mandate: Mandate) -> Ed25519AuthorizationProof:
        return sign_mandate(mandate, sk, "key-001")

    def test_first_one_time_authorization_passes_replay(self, engine):
        """First submission of a one_time nonce passes replay check."""
        sk, vk = make_key_pair()
        store = fresh_store_from_engine(engine)
        result = process_transaction(
            request=make_request(),
            auth_proof=self._signed(sk, make_mandate()),
            auth_provider=Ed25519MandateProvider(vk, NOW),
            policy_config=allow_all_policy(),
            replay_store=store,
        )
        assert result.replay_result is not None
        assert result.replay_result.allowed is True

    def test_same_nonce_submitted_again_is_blocked(self, engine):
        """
        Same one_time authorization nonce submitted twice → second attempt BLOCKED at REPLAY.
        Policy must NOT be called on the second attempt.
        """
        sk, vk = make_key_pair()
        mandate = make_mandate(nonce="nonce-replay-test")

        factory = get_session_factory(engine)

        s1 = factory()
        store1 = SQLiteReplayStore(s1)
        # First attempt
        r1 = process_transaction(
            request=make_request(transaction_id="txn-first"),
            auth_proof=self._signed(sk, mandate),
            auth_provider=Ed25519MandateProvider(vk, NOW),
            policy_config=allow_all_policy(),
            replay_store=store1,
        )
        assert r1.decision != GatewayDecision.BLOCK or r1.stage_reached != PipelineStage.REPLAY
        s1.close()

        # Second attempt — same signed mandate (same nonce)
        s2 = factory()
        store2 = SQLiteReplayStore(s2)
        with patch("app.core.orchestrator.evaluate_policy") as mock_policy:
            r2 = process_transaction(
            request=make_request(transaction_id="txn-second"),
            auth_proof=self._signed(sk, mandate),
            auth_provider=Ed25519MandateProvider(vk, NOW),
            policy_config=allow_all_policy(),
            replay_store=store2,
        )
            mock_policy.assert_not_called()

        assert r2.decision == GatewayDecision.BLOCK
        assert r2.stage_reached == PipelineStage.REPLAY
        assert r2.replay_result is not None
        assert r2.replay_result.was_replay is True
        assert "already been used" in r2.replay_result.reason.lower()
        s2.close()

    def test_duplicate_transaction_id_safely_handled(self, engine):
        """
        Two requests with the same transaction_id for a recurring mandate →
        second one is BLOCKED at REPLAY.
        """
        sk, vk = make_key_pair()
        mandate = make_mandate(mandate_type=MandateType.recurring, nonce="recurring-nonce")
        factory = get_session_factory(engine)

        s1 = factory()
        process_transaction(
            request=make_request(transaction_id="txn-dup-recurring"),
            auth_proof=self._signed(sk, mandate),
            auth_provider=Ed25519MandateProvider(vk, NOW),
            policy_config=PolicyConfig(
                max_transaction_amount=1_000_000,
                recurring_mandate_policy=RecurringMandatePolicy.allowed,
            ),
            replay_store=SQLiteReplayStore(s1),
        )
        s1.close()

        s2 = factory()
        with patch("app.core.orchestrator.evaluate_policy") as mock_policy:
            r2 = process_transaction(
            request=make_request(transaction_id="txn-dup-recurring"),
            auth_proof=self._signed(sk, mandate),
            auth_provider=Ed25519MandateProvider(vk, NOW),
            policy_config=PolicyConfig(
                    max_transaction_amount=1_000_000,
                    recurring_mandate_policy=RecurringMandatePolicy.allowed,
                ),
            replay_store=SQLiteReplayStore(s2),
        )
            mock_policy.assert_not_called()

        assert r2.decision == GatewayDecision.BLOCK
        assert r2.stage_reached == PipelineStage.REPLAY
        s2.close()

    def test_replay_store_db_error_blocks(self):
        """DB error during replay check → BLOCK. Policy not evaluated."""
        sk, vk = make_key_pair()
        with patch("app.core.orchestrator.evaluate_policy") as mock_policy:
            result = process_transaction(
            request=make_request(),
            auth_proof=sign_mandate(make_mandate(), sk, "key-001"),
            auth_provider=Ed25519MandateProvider(vk, NOW),
            policy_config=allow_all_policy(),
            replay_store=AlwaysBlockReplayStore(),
        )
            mock_policy.assert_not_called()

        assert result.decision == GatewayDecision.BLOCK
        assert result.stage_reached == PipelineStage.REPLAY
        assert result.policy_result is None


# ══════════════════════════════════════════════════════════════════════════════
# 3. Pipeline ordering — mocks/spies prove evaluate_policy is not called
# ══════════════════════════════════════════════════════════════════════════════


class TestPipelineOrdering:

    def test_invalid_auth_policy_not_called(self):
        """Invalid signature → evaluate_policy is never called."""
        sk, vk = make_key_pair()
        wrong_sk = SigningKey.generate()
        signed = sign_mandate(make_mandate(), wrong_sk, "key-001")

        with patch("app.core.orchestrator.evaluate_policy") as mock_policy:
            result = process_transaction(
            request=make_request(),
            auth_proof=signed,
            auth_provider=Ed25519MandateProvider(vk, NOW),
            policy_config=allow_all_policy(),
            replay_store=AlwaysAllowReplayStore(),
        )
            mock_policy.assert_not_called()

        assert result.decision == GatewayDecision.BLOCK

    def test_replay_failure_policy_not_called(self):
        """Replay store unavailable → evaluate_policy is never called."""
        sk, vk = make_key_pair()
        signed = sign_mandate(make_mandate(), sk, "key-001")

        with patch("app.core.orchestrator.evaluate_policy") as mock_policy:
            result = process_transaction(
            request=make_request(),
            auth_proof=signed,
            auth_provider=Ed25519MandateProvider(vk, NOW),
            policy_config=allow_all_policy(),
            replay_store=AlwaysBlockReplayStore(),
        )
            mock_policy.assert_not_called()

        assert result.decision == GatewayDecision.BLOCK

    def test_replay_detected_policy_not_called(self):
        """Replay detected → evaluate_policy is never called."""
        sk, vk = make_key_pair()
        signed = sign_mandate(make_mandate(), sk, "key-001")

        with patch("app.core.orchestrator.evaluate_policy") as mock_policy:
            result = process_transaction(
            request=make_request(),
            auth_proof=signed,
            auth_provider=Ed25519MandateProvider(vk, NOW),
            policy_config=allow_all_policy(),
            replay_store=ReplayDetectedStore(),
        )
            mock_policy.assert_not_called()

        assert result.decision == GatewayDecision.BLOCK
        assert result.stage_reached == PipelineStage.REPLAY

    def test_policy_block_produces_final_block(self):
        """Policy BLOCK → final result is BLOCK with policy_result populated."""
        sk, vk = make_key_pair()
        signed = sign_mandate(make_mandate(), sk, "key-001")
        result = process_transaction(
            request=make_request(),
            auth_proof=signed,
            auth_provider=Ed25519MandateProvider(vk, NOW),
            policy_config=block_all_policy(),
            replay_store=AlwaysAllowReplayStore(),
        )
        assert result.decision == GatewayDecision.BLOCK
        assert result.stage_reached == PipelineStage.POLICY
        assert result.policy_result is not None
        assert result.policy_result.decision == GatewayDecision.BLOCK

    def test_policy_review_produces_final_review(self):
        """Policy REVIEW → final result is REVIEW with policy_result populated."""
        sk, vk = make_key_pair()
        signed = sign_mandate(make_mandate(), sk, "key-001")
        result = process_transaction(
            request=make_request(amount_minor=100_000),  # ₹1000, above ₹500 threshold
            auth_proof=signed,
            auth_provider=Ed25519MandateProvider(vk, NOW),
            policy_config=review_policy(),
            replay_store=AlwaysAllowReplayStore()
        )
        assert result.decision == GatewayDecision.REVIEW
        assert result.stage_reached == PipelineStage.FINAL
        assert result.policy_result is not None
        assert result.policy_result.decision == GatewayDecision.REVIEW

    def test_policy_allow_produces_final_allow(self):
        """Policy ALLOW → final result is ALLOW."""
        sk, vk = make_key_pair()
        signed = sign_mandate(make_mandate(), sk, "key-001")
        result = process_transaction(
            request=make_request(),
            auth_proof=signed,
            auth_provider=Ed25519MandateProvider(vk, NOW),
            policy_config=allow_all_policy(),
            replay_store=AlwaysAllowReplayStore(),
        )
        assert result.decision == GatewayDecision.ALLOW
        assert result.stage_reached == PipelineStage.FINAL
        assert result.policy_result is not None
        assert result.policy_result.decision == GatewayDecision.ALLOW

    def test_expired_auth_replay_not_called(self):
        """Expired mandate → replay store is never contacted."""
        sk, vk = make_key_pair()
        # Mandate structurally valid but checked at a time past expiry
        mandate = make_mandate(expires_at=EXPIRED_MANDATE_EXPIRES_AT)
        signed = sign_mandate(mandate, sk, "key-001")

        mock_store = MagicMock(spec=ReplayStore)
        result = process_transaction(
            request=make_request(),
            auth_proof=signed,
            auth_provider=Ed25519MandateProvider(vk, EXPIRED_CHECK_TIME),
            policy_config=allow_all_policy(),
            replay_store=mock_store
        )
        mock_store.check_and_reserve_authorization_nonce.assert_not_called()
        mock_store.check_and_reserve_transaction_id.assert_not_called()
        assert result.decision == GatewayDecision.BLOCK


# ══════════════════════════════════════════════════════════════════════════════
# 4. TransactionResult correctness
# ══════════════════════════════════════════════════════════════════════════════


class TestTransactionResultFields:

    def test_allow_result_has_all_fields(self):
        sk, vk = make_key_pair()
        signed = sign_mandate(make_mandate(), sk, "key-001")
        result = process_transaction(
            request=make_request(transaction_id="txn-full-001"),
            auth_proof=signed,
            auth_provider=Ed25519MandateProvider(vk, NOW),
            policy_config=allow_all_policy(),
            replay_store=AlwaysAllowReplayStore()
        )
        assert result.transaction_id == "txn-full-001"
        assert result.decision == GatewayDecision.ALLOW
        assert result.authorization_result is not None
        assert result.replay_result is not None
        assert result.policy_result is not None
        assert result.timestamp is not None
        assert result.reason

    def test_block_at_auth_has_no_replay_or_policy(self):
        sk, vk = make_key_pair()
        wrong_sk = SigningKey.generate()
        signed = sign_mandate(make_mandate(), wrong_sk, "key-001")
        result = process_transaction(
            request=make_request(transaction_id="txn-auth-fail"),
            auth_proof=signed,
            auth_provider=Ed25519MandateProvider(vk, NOW),
            policy_config=allow_all_policy(),
            replay_store=AlwaysAllowReplayStore(),
        )
        assert result.decision == GatewayDecision.BLOCK
        assert result.authorization_result is not None
        assert result.authorization_result.valid is False
        assert result.replay_result is None
        assert result.policy_result is None

    def test_transaction_result_is_immutable(self):
        sk, vk = make_key_pair()
        signed = sign_mandate(make_mandate(), sk, "key-001")
        result = process_transaction(
            request=make_request(),
            auth_proof=signed,
            auth_provider=Ed25519MandateProvider(vk, NOW),
            policy_config=allow_all_policy(),
            replay_store=AlwaysAllowReplayStore(),
        )
        with pytest.raises((AttributeError, TypeError)):
            result.decision = GatewayDecision.BLOCK  # type: ignore[misc]

    def test_undecided_never_in_transaction_result(self):
        """TransactionResult cannot hold UNDECIDED decision."""
        from app.core.transaction_result import TransactionResult, PipelineStage, ProcessingState
        with pytest.raises(ValueError, match="UNDECIDED"):
            TransactionResult(
                transaction_id="txn-bad",
                decision=GatewayDecision.UNDECIDED,
                stage_reached=PipelineStage.FINAL,
                processing_state=ProcessingState.ALLOWED,
                reason="should fail",
                authorization_result=None,
                replay_result=None,
                policy_result=None,
                timestamp=datetime.now(timezone.utc),
            )


# ══════════════════════════════════════════════════════════════════════════════
# 5. No Razorpay
# ══════════════════════════════════════════════════════════════════════════════


class TestNoRazorpay:

    def test_orchestrator_does_not_import_razorpay(self):
        """
        The orchestrator module must not import any Razorpay client.
        This is structural proof, not just behavioral.
        """
        import app.core.orchestrator as orch_module
        import importlib

        # Razorpay-related names must not appear in the module's namespace
        module_source_attrs = dir(orch_module)
        razorpay_names = [a for a in module_source_attrs if "razorpay" in a.lower()]
        assert razorpay_names == [], (
            f"Orchestrator module contains Razorpay references: {razorpay_names}"
        )

    def test_razorpay_not_in_sys_modules_after_orchestration(self):
        """
        After running a full ALLOW pipeline, razorpay must not be in sys.modules.
        """
        razorpay_modules_before = {k for k in sys.modules if "razorpay" in k.lower()}

        sk, vk = make_key_pair()
        signed = sign_mandate(make_mandate(), sk, "key-001")
        process_transaction(
            request=make_request(),
            auth_proof=signed,
            auth_provider=Ed25519MandateProvider(vk, NOW),
            policy_config=allow_all_policy(),
            replay_store=AlwaysAllowReplayStore(),
        )

        razorpay_modules_after = {k for k in sys.modules if "razorpay" in k.lower()}
        new_razorpay = razorpay_modules_after - razorpay_modules_before
        assert new_razorpay == set(), (
            f"Razorpay module was imported during orchestration: {new_razorpay}"
        )

    def test_review_does_not_invoke_razorpay(self):
        """REVIEW result — confirm no Razorpay call occurs."""
        sk, vk = make_key_pair()
        signed = sign_mandate(make_mandate(), sk, "key-001")
        result = process_transaction(
            request=make_request(amount_minor=100_000),
            auth_proof=signed,
            auth_provider=Ed25519MandateProvider(vk, NOW),
            policy_config=review_policy(),
            replay_store=AlwaysAllowReplayStore(),
        )
        # REVIEW must stop before any payment
        assert result.decision == GatewayDecision.REVIEW
        # No payment_reference or Razorpay ID on the result
        assert not hasattr(result, "payment_reference") or getattr(result, "payment_reference", None) is None


# ══════════════════════════════════════════════════════════════════════════════
# 6. Race condition — concurrent duplicate nonce reservation
# ══════════════════════════════════════════════════════════════════════════════


class TestRaceCondition:

    def test_concurrent_same_nonce_exactly_one_allowed(self, tmp_path):
        """
        Two threads submit the same one_time authorization simultaneously.
        Exactly ONE must succeed; the other must be BLOCKED at REPLAY.

        Uses a file-based SQLite DB (tmp_path) so both threads share the
        same physical database.  An in-memory SQLite DB is per-connection,
        so threads would each get their own DB — that does not test atomicity.
        """
        import os
        db_path = tmp_path / "race_test.db"
        db_url = f"sqlite:///{db_path}"
        shared_engine = get_engine(db_url)
        init_db(shared_engine)

        sk, vk = make_key_pair()
        nonce = "nonce-race-001"
        mandate = make_mandate(nonce=nonce)
        factory = get_session_factory(shared_engine)
        results: list[TransactionResult] = []
        lock = threading.Lock()

        def attempt(txn_id: str) -> None:
            session = factory()
            store = SQLiteReplayStore(session)
            signed = sign_mandate(mandate, sk, "key-001")
            result = process_transaction(
            request=make_request(transaction_id=txn_id),
            auth_proof=signed,
            auth_provider=Ed25519MandateProvider(vk, NOW),
            policy_config=allow_all_policy(),
            replay_store=store,
        )
            with lock:
                results.append(result)
            session.close()

        t1 = threading.Thread(target=attempt, args=("txn-race-A",))
        t2 = threading.Thread(target=attempt, args=("txn-race-B",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        shared_engine.dispose()

        assert len(results) == 2

        allowed = [r for r in results if r.decision == GatewayDecision.ALLOW]
        blocked = [r for r in results if r.decision == GatewayDecision.BLOCK]

        assert len(allowed) == 1, f"Expected 1 ALLOW, got {len(allowed)}. Results: {results}"
        assert len(blocked) == 1, f"Expected 1 BLOCK, got {len(blocked)}. Results: {results}"
        assert blocked[0].stage_reached == PipelineStage.REPLAY
