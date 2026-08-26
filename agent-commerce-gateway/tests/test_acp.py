"""
ACP Adapter & Provider Tests — Agent Commerce Gateway
======================================================

Tests for:
  - ACPAdapter.parse_request()       (normalization)
  - ACPAdapter.parse_authorization_proof()
  - ACPAdapter.build_receipt()       (receipt translation)
  - ACPAuthorizationProvider.verify() (structural checks)
  - End-to-end: ACP → orchestrator → ALLOW/REVIEW/BLOCK
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.adapters.acp_adapter import (
    ACPAdapter,
    ACPAuthorizationProof,
    ACPCheckoutRequest,
)
from app.adapters.acp_provider import ACPAuthorizationProvider
from app.core.policy import PolicyConfig, RecurringMandatePolicy
from app.core.replay import ReplayStore, ReplayResult, SQLiteReplayStore
from app.core.schemas import (
    BuyerProtocol,
    CommerceReceipt,
    GatewayDecision,
    Money,
)
from app.core.orchestrator import process_transaction
from app.core.transaction_result import PipelineStage

try:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.db.database import Base
    _HAS_SQLALCHEMY = True
except ImportError:
    _HAS_SQLALCHEMY = False


def make_replay_store():
    """Create a fresh in-memory SQLite replay store for tests."""
    from sqlalchemy import create_engine
    from app.db.database import Base
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    from sqlalchemy.orm import Session
    session = Session(engine)
    return SQLiteReplayStore(session)


# ══════════════════════════════════════════════════════════════════════════════
# Shared fixtures
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def adapter():
    return ACPAdapter()


@pytest.fixture
def provider():
    return ACPAuthorizationProvider()


@pytest.fixture
def base_payload():
    """A valid minimal ACP checkout payload (body + extracted headers)."""
    return {
        "items": [
            {
                "id": "prod-001",
                "quantity": 2,
                "unit_amount": 1000,
                "currency": "INR",
                "name": "Widget",
                "category": "electronics",
            }
        ],
        "buyer": {
            "agent_id": "buyer-agent-42",
            "first_name": "Test",
            "last_name": "Buyer",
            "email": "test@example.com",
        },
        "merchant_id": "merchant-xyz",
        "idempotency_key": str(uuid.uuid4()),
        "bearer_token": "tok_valid_bearer_abc123",
        "api_version": "2026-01-16",
    }


@pytest.fixture
def permissive_policy():
    return PolicyConfig(
        max_transaction_amount=1_000_000,
        recurring_mandate_policy=RecurringMandatePolicy.allowed,
    )


# ══════════════════════════════════════════════════════════════════════════════
# ACPAdapter.parse_request() — normalization
# ══════════════════════════════════════════════════════════════════════════════


class TestACPAdapterParseRequest:

    def test_valid_single_item(self, adapter, base_payload):
        request = adapter.parse_request(base_payload)
        assert request.buyer_protocol == BuyerProtocol.acp
        assert request.buyer_agent_id == "buyer-agent-42"
        assert request.merchant_id == "merchant-xyz"
        assert len(request.items) == 1
        assert request.items[0].product_id == "prod-001"
        assert request.items[0].quantity == 2
        assert request.items[0].unit_price.amount_minor == 1000
        assert request.items[0].unit_price.currency == "INR"
        assert request.calculated_total.amount_minor == 2000
        assert request.calculated_total.currency == "INR"

    def test_valid_multi_item(self, adapter, base_payload):
        base_payload["items"].append({
            "id": "prod-002",
            "quantity": 1,
            "unit_amount": 500,
            "currency": "INR",
            "name": "Gadget",
        })
        request = adapter.parse_request(base_payload)
        assert len(request.items) == 2
        assert request.calculated_total.amount_minor == 2500

    def test_currency_is_uppercased(self, adapter, base_payload):
        base_payload["items"][0]["currency"] = "inr"
        request = adapter.parse_request(base_payload)
        assert request.items[0].unit_price.currency == "INR"

    def test_transaction_id_uses_idempotency_key(self, adapter, base_payload):
        key = "my-unique-key-123"
        base_payload["idempotency_key"] = key
        request = adapter.parse_request(base_payload)
        # No checkout_session_id → transaction_id = "acp-{idempotency_key}"
        assert request.transaction_id == f"acp-{key}"

    def test_checkout_session_id_used_as_transaction_id(self, adapter, base_payload):
        base_payload["checkout_session_id"] = "sess_abc123"
        request = adapter.parse_request(base_payload)
        assert request.transaction_id == "sess_abc123"

    def test_item_name_defaults_to_product_id(self, adapter, base_payload):
        del base_payload["items"][0]["name"]
        request = adapter.parse_request(base_payload)
        assert request.items[0].name == "prod-001"

    def test_missing_bearer_token_raises(self, adapter, base_payload):
        base_payload["bearer_token"] = ""
        with pytest.raises((ValueError, Exception), match="bearer_token|bearer token"):
            adapter.parse_request(base_payload)

    def test_missing_idempotency_key_raises(self, adapter, base_payload):
        base_payload["idempotency_key"] = ""
        with pytest.raises((ValueError, Exception), match="idempotency_key|Idempotency-Key"):
            adapter.parse_request(base_payload)

    def test_unsupported_api_version_raises(self, adapter, base_payload):
        base_payload["api_version"] = "1999-01-01"
        with pytest.raises(ValueError, match="Unsupported ACP API-Version"):
            adapter.parse_request(base_payload)

    def test_supported_api_version_2026_04_17(self, adapter, base_payload):
        base_payload["api_version"] = "2026-04-17"
        request = adapter.parse_request(base_payload)
        assert request is not None

    def test_mixed_currencies_raises(self, adapter, base_payload):
        base_payload["items"].append({
            "id": "prod-usd",
            "quantity": 1,
            "unit_amount": 100,
            "currency": "USD",
            "name": "USD Item",
        })
        with pytest.raises(ValueError, match="mixed currencies"):
            adapter.parse_request(base_payload)

    def test_missing_items_raises(self, adapter, base_payload):
        base_payload["items"] = []
        with pytest.raises(Exception):
            adapter.parse_request(base_payload)

    def test_zero_quantity_raises(self, adapter, base_payload):
        base_payload["items"][0]["quantity"] = 0
        with pytest.raises(Exception):
            adapter.parse_request(base_payload)

    def test_result_is_frozen(self, adapter, base_payload):
        request = adapter.parse_request(base_payload)
        with pytest.raises(Exception):
            request.merchant_id = "tampered"

    def test_nonce_is_idempotency_key(self, adapter, base_payload):
        """Request nonce must be the idempotency key for ACP."""
        key = "nonce-for-replay"
        base_payload["idempotency_key"] = key
        request = adapter.parse_request(base_payload)
        assert request.nonce == key


# ══════════════════════════════════════════════════════════════════════════════
# ACPAdapter.parse_authorization_proof()
# ══════════════════════════════════════════════════════════════════════════════


class TestACPAdapterParseAuthorizationProof:

    def test_valid_proof(self, adapter, base_payload):
        proof = adapter.parse_authorization_proof(base_payload)
        assert isinstance(proof, ACPAuthorizationProof)
        assert proof.auth_type == "acp_bearer_token"
        assert proof.bearer_token == "tok_valid_bearer_abc123"
        assert proof.claimed_buyer_agent_id == "buyer-agent-42"
        assert proof.claimed_merchant_id == "merchant-xyz"
        assert proof.claimed_amount_minor == 2000  # 2 × 1000
        assert proof.claimed_currency == "INR"

    def test_idempotency_key_is_replay_key(self, adapter, base_payload):
        key = "replay-test-key"
        base_payload["idempotency_key"] = key
        proof = adapter.parse_authorization_proof(base_payload)
        assert proof.idempotency_key == key

    def test_missing_bearer_token_raises(self, adapter, base_payload):
        base_payload["bearer_token"] = "   "
        with pytest.raises(ValueError, match="bearer token"):
            adapter.parse_authorization_proof(base_payload)

    def test_missing_idempotency_key_raises(self, adapter, base_payload):
        base_payload["idempotency_key"] = ""
        with pytest.raises((ValueError, Exception), match="idempotency_key|Idempotency-Key"):
            adapter.parse_authorization_proof(base_payload)

    def test_proof_is_frozen(self, adapter, base_payload):
        proof = adapter.parse_authorization_proof(base_payload)
        with pytest.raises(Exception):
            proof.bearer_token = "tampered"


# ══════════════════════════════════════════════════════════════════════════════
# ACPAdapter.build_receipt()
# ══════════════════════════════════════════════════════════════════════════════


class TestACPAdapterBuildReceipt:

    def _make_receipt(self, status="completed", decision=GatewayDecision.ALLOW, ref=None):
        return CommerceReceipt(
            transaction_id="txn-001",
            merchant_id="merchant-xyz",
            buyer_agent_id="buyer-agent-42",
            final_amount=Money(amount_minor=2000, currency="INR"),
            payment_reference=ref,
            status=status,
            timestamp=datetime.now(timezone.utc),
            originating_protocol=BuyerProtocol.acp,
            decision=decision,
        )

    def test_completed_receipt(self, adapter):
        receipt = self._make_receipt(status="completed", ref="razorpay-order-123")
        result = adapter.build_receipt(receipt)
        assert result["id"] == "txn-001"
        assert result["status"] == "completed"
        assert result["currency"] == "inr"
        assert result["totals"][0]["amount"] == 2000
        assert result["order"]["id"] == "razorpay-order-123"
        assert result["gateway_decision"] == "ALLOW"

    def test_pending_receipt_maps_to_ready_for_payment(self, adapter):
        receipt = self._make_receipt(status="pending", decision=GatewayDecision.REVIEW)
        result = adapter.build_receipt(receipt)
        assert result["status"] == "ready_for_payment"

    def test_failed_receipt_maps_to_canceled(self, adapter):
        receipt = self._make_receipt(status="failed", decision=GatewayDecision.BLOCK)
        result = adapter.build_receipt(receipt)
        assert result["status"] == "canceled"

    def test_no_order_when_no_payment_reference(self, adapter):
        receipt = self._make_receipt(status="completed", ref=None)
        result = adapter.build_receipt(receipt)
        assert "order" not in result


# ══════════════════════════════════════════════════════════════════════════════
# ACPAuthorizationProvider.verify()
# ══════════════════════════════════════════════════════════════════════════════


class TestACPAuthorizationProvider:

    def _make_request_and_proof(self, adapter, payload):
        request = adapter.parse_request(payload)
        proof = adapter.parse_authorization_proof(payload)
        return request, proof

    def test_valid_proof_passes(self, adapter, provider, base_payload):
        request, proof = self._make_request_and_proof(adapter, base_payload)
        result = provider.verify(request, proof)
        assert result.valid is True
        assert result.requires_replay_check is True
        assert result.replay_namespace.value == "transaction_id"
        assert result.is_recurring is False

    def test_replay_key_is_idempotency_key(self, adapter, provider, base_payload):
        key = "replay-key-test"
        base_payload["idempotency_key"] = key
        request, proof = self._make_request_and_proof(adapter, base_payload)
        result = provider.verify(request, proof)
        assert result.replay_key == key

    def test_wrong_proof_type_rejected(self, provider, adapter, base_payload):
        """Non-ACP proof must be rejected by ACPAuthorizationProvider."""
        from app.core.schemas import Ed25519AuthorizationProof, Mandate, MandateStatus, MandateType
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        mandate = Mandate(
            mandate_id="m1",
            buyer_agent_id="buyer-agent-42",
            merchant_id="merchant-xyz",
            max_amount=Money(amount_minor=9999, currency="INR"),
            mandate_type=MandateType.one_time,
            status=MandateStatus.active,
            issued_at=now,
            expires_at=now + timedelta(hours=1),
            nonce="test-nonce",
            authorization_method="ed25519",
            authorization_ref="key-ref",
        )
        wrong_proof = Ed25519AuthorizationProof(
            payload=mandate,
            signature=b"\x00" * 64,
            key_id="key1",
            algorithm="Ed25519",
        )
        request = adapter.parse_request(base_payload)
        result = provider.verify(request, wrong_proof)
        assert result.valid is False
        assert "ACPAuthorizationProof" in result.reason

    def test_buyer_mismatch_rejected(self, adapter, provider, base_payload):
        request = adapter.parse_request(base_payload)
        # Tamper: change claimed buyer in proof
        proof = adapter.parse_authorization_proof(base_payload)
        tampered_proof = ACPAuthorizationProof(
            bearer_token=proof.bearer_token,
            claimed_buyer_agent_id="different-buyer",  # mismatch
            claimed_merchant_id=proof.claimed_merchant_id,
            idempotency_key=proof.idempotency_key,
            claimed_amount_minor=proof.claimed_amount_minor,
            claimed_currency=proof.claimed_currency,
        )
        result = provider.verify(request, tampered_proof)
        assert result.valid is False
        assert "buyer" in result.reason.lower()

    def test_merchant_mismatch_rejected(self, adapter, provider, base_payload):
        request = adapter.parse_request(base_payload)
        proof = adapter.parse_authorization_proof(base_payload)
        tampered_proof = ACPAuthorizationProof(
            bearer_token=proof.bearer_token,
            claimed_buyer_agent_id=proof.claimed_buyer_agent_id,
            claimed_merchant_id="different-merchant",  # mismatch
            idempotency_key=proof.idempotency_key,
            claimed_amount_minor=proof.claimed_amount_minor,
            claimed_currency=proof.claimed_currency,
        )
        result = provider.verify(request, tampered_proof)
        assert result.valid is False
        assert "merchant" in result.reason.lower()

    def test_currency_mismatch_rejected(self, adapter, provider, base_payload):
        request = adapter.parse_request(base_payload)
        proof = adapter.parse_authorization_proof(base_payload)
        tampered_proof = ACPAuthorizationProof(
            bearer_token=proof.bearer_token,
            claimed_buyer_agent_id=proof.claimed_buyer_agent_id,
            claimed_merchant_id=proof.claimed_merchant_id,
            idempotency_key=proof.idempotency_key,
            claimed_amount_minor=proof.claimed_amount_minor,
            claimed_currency="USD",  # wrong currency
        )
        result = provider.verify(request, tampered_proof)
        assert result.valid is False
        assert "currency" in result.reason.lower()

    def test_amount_mismatch_rejected(self, adapter, provider, base_payload):
        request = adapter.parse_request(base_payload)
        proof = adapter.parse_authorization_proof(base_payload)
        tampered_proof = ACPAuthorizationProof(
            bearer_token=proof.bearer_token,
            claimed_buyer_agent_id=proof.claimed_buyer_agent_id,
            claimed_merchant_id=proof.claimed_merchant_id,
            idempotency_key=proof.idempotency_key,
            claimed_amount_minor=999,  # does not match 2000
            claimed_currency=proof.claimed_currency,
        )
        result = provider.verify(request, tampered_proof)
        assert result.valid is False
        assert "amount" in result.reason.lower()

    def test_empty_bearer_token_rejected(self, provider):
        """ACPAuthorizationProof with empty bearer token must fail."""
        with pytest.raises(Exception):
            # min_length=1 on bearer_token
            ACPAuthorizationProof(
                bearer_token="",
                claimed_buyer_agent_id="buyer",
                claimed_merchant_id="merchant",
                idempotency_key="key",
                claimed_amount_minor=100,
                claimed_currency="INR",
            )


# ══════════════════════════════════════════════════════════════════════════════
# End-to-End: ACP → orchestrator
# ══════════════════════════════════════════════════════════════════════════════


class TestACPEndToEnd:
    """
    Single end-to-end test: ACP payload → adapter → orchestrator → decision.
    Uses InMemoryReplayStore (safe for tests, does not persist).
    """

    def test_valid_acp_flow_allows(self, adapter, provider, permissive_policy):
        payload = {
            "items": [
                {
                    "id": "e2e-prod-001",
                    "quantity": 1,
                    "unit_amount": 5000,
                    "currency": "INR",
                    "name": "E2E Product",
                    "category": "software",
                }
            ],
            "buyer": {
                "agent_id": "e2e-buyer-agent",
                "email": "e2e@test.com",
            },
            "merchant_id": "e2e-merchant",
            "idempotency_key": str(uuid.uuid4()),
            "bearer_token": "tok_e2e_valid_token",
            "api_version": "2026-01-16",
        }

        request = adapter.parse_request(payload)
        proof = adapter.parse_authorization_proof(payload)
        replay_store = make_replay_store()

        result = process_transaction(
            request=request,
            auth_proof=proof,
            auth_provider=provider,
            policy_config=permissive_policy,
            replay_store=replay_store,
        )

        assert result.decision == GatewayDecision.ALLOW
        assert result.transaction_id == request.transaction_id

    def test_replay_same_idempotency_key_blocked(self, adapter, provider, permissive_policy):
        """
        Replaying the exact same Idempotency-Key must be blocked at the replay stage.
        This enforces ACP's idempotency guarantee: same key → same outcome, not double-process.
        """
        key = str(uuid.uuid4())
        payload = {
            "items": [{"id": "prod-r", "quantity": 1, "unit_amount": 100, "currency": "INR"}],
            "buyer": {"agent_id": "replay-buyer"},
            "merchant_id": "replay-merchant",
            "idempotency_key": key,
            "bearer_token": "tok_replay_test",
            "api_version": "2026-01-16",
        }

        replay_store = make_replay_store()

        # First call — should pass
        r1 = process_transaction(
            request=adapter.parse_request(payload),
            auth_proof=adapter.parse_authorization_proof(payload),
            auth_provider=provider,
            policy_config=permissive_policy,
            replay_store=replay_store,
        )
        assert r1.decision == GatewayDecision.ALLOW

        # Second call with same key — must be blocked
        r2 = process_transaction(
            request=adapter.parse_request(payload),
            auth_proof=adapter.parse_authorization_proof(payload),
            auth_provider=provider,
            policy_config=permissive_policy,
            replay_store=replay_store,
        )
        assert r2.decision == GatewayDecision.BLOCK
        assert r2.stage_reached == PipelineStage.REPLAY

    def test_missing_bearer_token_blocked_at_validation(self, adapter, provider, permissive_policy):
        """Missing bearer token must fail at the adapter before the orchestrator."""
        payload = {
            "items": [{"id": "p1", "quantity": 1, "unit_amount": 100, "currency": "INR"}],
            "buyer": {"agent_id": "buyer"},
            "merchant_id": "merchant",
            "idempotency_key": str(uuid.uuid4()),
            "bearer_token": "",  # missing!
            "api_version": "2026-01-16",
        }
        with pytest.raises((ValueError, Exception), match="bearer_token|bearer token"):
            adapter.parse_request(payload)

    def test_policy_blocks_large_amount(self, adapter, provider):
        """Transactions above policy max must be blocked by the policy engine."""
        strict_policy = PolicyConfig(max_transaction_amount=500)

        payload = {
            "items": [{"id": "expensive", "quantity": 1, "unit_amount": 10000, "currency": "INR"}],
            "buyer": {"agent_id": "policy-buyer"},
            "merchant_id": "policy-merchant",
            "idempotency_key": str(uuid.uuid4()),
            "bearer_token": "tok_policy_test",
            "api_version": "2026-01-16",
        }

        result = process_transaction(
            request=adapter.parse_request(payload),
            auth_proof=adapter.parse_authorization_proof(payload),
            auth_provider=provider,
            policy_config=strict_policy,
            replay_store=make_replay_store(),
        )

        assert result.decision == GatewayDecision.BLOCK
        assert result.stage_reached == PipelineStage.POLICY
