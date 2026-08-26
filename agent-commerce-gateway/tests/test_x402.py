"""
x402 Adapter & Provider Tests — Agent Commerce Gateway
======================================================

Comprehensive tests for:
  - Authoritative x402 v2 PaymentPayload parsing & normalization
  - Legacy v1 header format support (backwards compatibility)
  - Strict schema & parameter validation (schemes, networks, tokens)
  - Fail-closed behavior on missing buyer/merchant canonical identity
  - SandboxX402Verifier & X402AuthorizationProvider
  - End-to-end orchestrator pipeline (Validation -> Auth -> Replay -> Policy -> Decision)
  - Gateway /api/v1/execute endpoint integration for x402 and ACP
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.adapters.x402_adapter import (
    X402Adapter,
    X402AuthorizationProof,
    X402V2PaymentPayload,
)
from app.adapters.x402_provider import (
    SandboxX402Verifier,
    X402AuthorizationProvider,
    X402PaymentVerifier,
)
from app.core.merchant_store import merchant_store
from app.core.orchestrator import process_transaction
from app.core.policy import PolicyConfig, RecurringMandatePolicy
from app.core.replay import SQLiteReplayStore
from app.core.schemas import (
    BuyerProtocol,
    CommerceItem,
    CommerceReceipt,
    GatewayDecision,
    Money,
)
from app.core.transaction_result import PipelineStage
from app.main import app
from app.razorpay.client import ExecutionStatus, RazorpayOrderResult


# ══════════════════════════════════════════════════════════════════════════════
# Test Setup & Fixtures
# ══════════════════════════════════════════════════════════════════════════════


class MockX402PaymentVerifier(X402PaymentVerifier):
    """Mock verifier that returns True for specific hashes/nonces, False otherwise."""
    
    def __init__(self, valid_hashes: set[str]):
        self.valid_hashes = valid_hashes
        self.called_with = []

    def verify_transaction(self, tx_hash: str, network: str, token: str, required_amount_minor: int) -> bool:
        self.called_with.append((tx_hash, network, token, required_amount_minor))
        return tx_hash in self.valid_hashes


def make_replay_store():
    """Create a fresh in-memory SQLite replay store for tests."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.db.database import Base
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = Session(engine)
    return SQLiteReplayStore(session)


@pytest.fixture
def adapter():
    return X402Adapter()


@pytest.fixture
def valid_hash():
    return f"tx_valid_{uuid.uuid4().hex}"


@pytest.fixture
def mock_verifier(valid_hash):
    return MockX402PaymentVerifier(valid_hashes={valid_hash})


@pytest.fixture
def provider(mock_verifier):
    return X402AuthorizationProvider(verifier=mock_verifier)


@pytest.fixture
def sandbox_provider():
    return X402AuthorizationProvider(verifier=SandboxX402Verifier())


@pytest.fixture
def base_payload(valid_hash):
    """A valid legacy x402 payload with caller-provided identity."""
    x_payment = {
        "hash": valid_hash,
        "amount": "1000",
        "network": "solana",
        "token": "USDC"
    }
    return {
        "buyer_agent_id": "caller-provided-id-123",
        "merchant_id": "merchant-xyz",
        "items": [
            {
                "product_id": "prod-001",
                "quantity": 1,
                "name": "Widget",
                "unit_price": {"amount_minor": 1000, "currency": "USD"},
            }
        ],
        "x_payment": json.dumps(x_payment),
    }


@pytest.fixture
def valid_v2_payload():
    """Authoritative x402 v2 PaymentPayload fixture."""
    nonce = f"0x{uuid.uuid4().hex}{uuid.uuid4().hex}"
    sig = f"0x{uuid.uuid4().hex}{uuid.uuid4().hex}"
    return {
        "x402Version": 2,
        "resource": {
            "url": "http://localhost:3000/api/checkout-intent",
            "description": "Order checkout for 1x Aqua Walker",
            "mimeType": "application/json"
        },
        "accepted": {
            "scheme": "exact",
            "network": "eip155:84532",
            "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
            "amount": "420000",
            "payTo": "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
            "maxTimeoutSeconds": 300,
            "extra": {
                "merchant_id": "merchant-xyz",
                "buyer_agent_id": "agent_buyer_007",
                "currency": "INR",
                "items": [
                    {
                        "id": "prod_shoe_008",
                        "name": "Aqua Walker",
                        "quantity": 1,
                        "unit_amount": 420000,
                        "currency": "INR",
                        "category": "Outdoor"
                    }
                ]
            }
        },
        "payload": {
            "authorization": {
                "from": "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC",
                "to": "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
                "value": "420000",
                "validAfter": 0,
                "validBefore": 1787763101,
                "nonce": nonce
            },
            "signature": sig
        },
        "extensions": {}
    }


@pytest.fixture
def permissive_policy():
    return PolicyConfig(
        max_transaction_amount=1_000_000,
        recurring_mandate_policy=RecurringMandatePolicy.allowed,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Authoritative x402 v2 PaymentPayload Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestX402V2Adapter:
    def test_valid_v2_payload_parsed_to_canonical_request(self, adapter, valid_v2_payload):
        req = adapter.parse_request(valid_v2_payload)

        assert req.buyer_agent_id == "agent_buyer_007"
        assert req.merchant_id == "merchant-xyz"
        assert req.buyer_protocol == BuyerProtocol.x402
        assert req.calculated_total.amount_minor == 420000
        assert req.calculated_total.currency == "INR"
        assert len(req.items) == 1
        assert req.items[0].product_id == "prod_shoe_008"
        assert req.items[0].name == "Aqua Walker"
        assert req.items[0].quantity == 1
        assert req.items[0].unit_price.amount_minor == 420000
        assert req.items[0].category == "Outdoor"
        assert req.nonce == valid_v2_payload["payload"]["authorization"]["nonce"]
        assert req.transaction_id == f"x402-{req.nonce}"

    def test_v2_fallback_to_from_address_if_no_buyer_agent_id(self, adapter, valid_v2_payload):
        del valid_v2_payload["accepted"]["extra"]["buyer_agent_id"]
        req = adapter.parse_request(valid_v2_payload)
        assert req.buyer_agent_id == "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC"

    def test_v2_without_explicit_items_creates_resource_item(self, adapter, valid_v2_payload):
        del valid_v2_payload["accepted"]["extra"]["items"]
        req = adapter.parse_request(valid_v2_payload)
        assert len(req.items) == 1
        assert req.items[0].product_id == "x402_resource"
        assert req.items[0].name == "Order checkout for 1x Aqua Walker"
        assert req.items[0].unit_price.amount_minor == 420000

    def test_v2_unsupported_version_fails_closed(self, adapter, valid_v2_payload):
        valid_v2_payload["x402Version"] = 1
        with pytest.raises(ValueError, match="Gateway requires x402 v2"):
            adapter.parse_request(valid_v2_payload)

    def test_v2_unsupported_scheme_fails_closed(self, adapter, valid_v2_payload):
        valid_v2_payload["accepted"]["scheme"] = "auction"
        with pytest.raises(ValueError, match="Unsupported x402 scheme"):
            adapter.parse_request(valid_v2_payload)

    def test_v2_unsupported_network_fails_closed(self, adapter, valid_v2_payload):
        valid_v2_payload["accepted"]["network"] = "dogechain:1"
        with pytest.raises(ValueError, match="Unsupported x402 network"):
            adapter.parse_request(valid_v2_payload)

    def test_v2_missing_merchant_id_fails_closed(self, adapter, valid_v2_payload):
        del valid_v2_payload["accepted"]["extra"]["merchant_id"]
        with pytest.raises(ValueError, match="Missing required merchant_id"):
            adapter.parse_request(valid_v2_payload)

    def test_v2_missing_buyer_identity_fails_closed(self, adapter, valid_v2_payload):
        del valid_v2_payload["accepted"]["extra"]["buyer_agent_id"]
        valid_v2_payload["payload"]["authorization"]["from"] = ""
        with pytest.raises(ValueError, match="Missing required buyer identity"):
            adapter.parse_request(valid_v2_payload)

    def test_v2_line_items_total_mismatch_fails_closed(self, adapter, valid_v2_payload):
        # Change accepted amount without changing item price
        valid_v2_payload["accepted"]["amount"] = "500000"
        with pytest.raises(ValueError, match="does not match accepted amount"):
            adapter.parse_request(valid_v2_payload)

    def test_v2_negative_amount_fails_closed(self, adapter, valid_v2_payload):
        valid_v2_payload["accepted"]["amount"] = "-420000"
        with pytest.raises(ValueError, match="cannot be negative"):
            adapter.parse_request(valid_v2_payload)

    def test_v2_parse_authorization_proof(self, adapter, valid_v2_payload):
        proof = adapter.parse_authorization_proof(valid_v2_payload)
        assert isinstance(proof, X402AuthorizationProof)
        assert proof.auth_type == "x402_payment_proof"
        assert proof.x402_version == 2
        assert proof.scheme == "exact"
        assert proof.network == "eip155:84532"
        assert proof.token == "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
        assert proof.claimed_amount_minor == 420000
        assert proof.pay_to == "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
        assert proof.from_address == "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC"
        assert proof.nonce == valid_v2_payload["payload"]["authorization"]["nonce"]
        assert proof.signature == valid_v2_payload["payload"]["signature"]


# ══════════════════════════════════════════════════════════════════════════════
# Legacy v1 Parsing & Backward Compatibility Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestX402LegacyAdapter:
    def test_valid_legacy_payload(self, adapter, base_payload, valid_hash):
        request = adapter.parse_request(base_payload)
        assert request.buyer_agent_id == "caller-provided-id-123"
        assert request.merchant_id == "merchant-xyz"
        assert request.buyer_protocol == BuyerProtocol.x402
        assert request.nonce == valid_hash
        assert request.transaction_id == f"x402-{valid_hash}"
        assert len(request.items) == 1
        assert request.calculated_total.amount_minor == 1000

    def test_missing_identity_fails_closed(self, adapter, base_payload):
        del base_payload["buyer_agent_id"]
        with pytest.raises((ValueError, Exception)):
            adapter.parse_request(base_payload)

    def test_missing_x_payment_fails_closed(self, adapter, base_payload):
        del base_payload["x_payment"]
        with pytest.raises((ValueError, Exception)):
            adapter.parse_request(base_payload)

    def test_invalid_x_payment_json_fails_closed(self, adapter, base_payload):
        base_payload["x_payment"] = "not json"
        with pytest.raises(ValueError, match="not valid JSON"):
            adapter.parse_request(base_payload)

    def test_x_payment_dict_supported(self, adapter, base_payload):
        base_payload["x_payment"] = json.loads(base_payload["x_payment"])
        request = adapter.parse_request(base_payload)
        assert request is not None


# ══════════════════════════════════════════════════════════════════════════════
# Provider & Verifier Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestX402ProviderAndVerifier:
    def test_no_verifier_configured_fails_closed(self, adapter, valid_v2_payload):
        unverified_provider = X402AuthorizationProvider(verifier=None)
        req = adapter.parse_request(valid_v2_payload)
        proof = adapter.parse_authorization_proof(valid_v2_payload)
        result = unverified_provider.verify(req, proof)

        assert result.valid is False
        assert "no X402PaymentVerifier configured" in result.reason

    def test_sandbox_verifier_approves_valid_v2(self, adapter, sandbox_provider, valid_v2_payload):
        req = adapter.parse_request(valid_v2_payload)
        proof = adapter.parse_authorization_proof(valid_v2_payload)
        result = sandbox_provider.verify(req, proof)

        assert result.valid is True
        assert result.requires_replay_check is True
        assert result.replay_namespace.value == "transaction_id"
        assert result.replay_key == proof.nonce

    def test_sandbox_verifier_rejects_bad_signature(self, adapter, sandbox_provider, valid_v2_payload):
        valid_v2_payload["payload"]["signature"] = "0xbad_signature"
        req = adapter.parse_request(valid_v2_payload)
        proof = adapter.parse_authorization_proof(valid_v2_payload)
        result = sandbox_provider.verify(req, proof)

        assert result.valid is False
        assert "rejected the transaction hash or payment signature" in result.reason

    def test_provider_rejects_insufficient_amount(self, adapter, sandbox_provider, valid_v2_payload):
        req = adapter.parse_request(valid_v2_payload)
        proof_nonce = f"0x{uuid.uuid4().hex}"
        # Construct proof claiming less amount
        proof = X402AuthorizationProof(
            auth_type="x402_payment_proof",
            x402_version=2,
            scheme="exact",
            network="eip155:84532",
            token="0x036CbD53842c5426634e7929541eC2318f3dCF7e",
            claimed_amount_minor=1000, # less than 420000
            nonce=proof_nonce,
            signature="0x1234567890abcdef1234567890abcdef"
        )
        result = sandbox_provider.verify(req, proof)
        assert result.valid is False
        assert "insufficient amount" in result.reason


# ══════════════════════════════════════════════════════════════════════════════
# Full Pipeline & Router Integration Tests (/api/v1/execute)
# ══════════════════════════════════════════════════════════════════════════════


class TestGatewayProtocolRouter:
    @pytest.fixture(autouse=True)
    def setup_merchant(self):
        import hashlib
        from app.core.merchant_store import MerchantConfig, MerchantSecrets

        api_key = "key_test_12345"
        api_key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()

        merchant_store.save_merchant(
            MerchantConfig(
                merchant_id="merchant-demo-001",
                merchant_name="Test Merchant",
                razorpay_key_id="rzp_test_demo",
                max_transaction_amount=500000, # ₹5,000 max
                allowed_currency="INR",
                blocked_categories=["Weapons", "Gambling"]
            ),
            MerchantSecrets(
                merchant_id="merchant-demo-001",
                razorpay_key_secret="rzp_secret_demo",
                api_key_hash=api_key_hash
            )
        )

    @patch("app.razorpay.client.RazorpayClient.create_order")
    def test_execute_x402_v2_success_reaches_allow_and_creates_order(self, mock_create, valid_v2_payload):
        mock_create.return_value = RazorpayOrderResult(
            execution_status=ExecutionStatus.ORDER_CREATED,
            razorpay_order_id="order_x402_test123",
            razorpay_order_status="created",
            razorpay_amount=420000,
            razorpay_currency="INR",
            razorpay_receipt="x402-rcpt",
            razorpay_payment_id=None,
            razorpay_payment_status=None,
            error_code=None,
            error_description=None,
            http_status_code=200,
            timestamp=datetime.now(timezone.utc)
        )

        valid_v2_payload["accepted"]["extra"]["merchant_id"] = "merchant-demo-001"

        with TestClient(app) as client:
            res = client.post(
                "/api/v1/execute",
                json={"protocol": "x402", "raw_payload": valid_v2_payload},
                headers={"Authorization": "Bearer key_test_12345"}
            )

        assert res.status_code == 200
        data = res.json()
        assert data["gateway_decision"] == "ALLOW"
        assert data["protocol"] == "x402"
        assert data["razorpay_order_id"] == "order_x402_test123"
        assert data["canonical_request"]["total"]["amount_minor"] == 420000
        assert data["canonical_request"]["buyer_agent_id"] == "agent_buyer_007"
        assert len(data["canonical_request"]["line_items"]) == 1

        # Zero secret exposure
        serialized = json.dumps(data)
        assert "rzp_secret_demo" not in serialized

    @patch("app.razorpay.client.RazorpayClient.create_order")
    def test_execute_x402_v2_policy_limit_blocks_and_no_order(self, mock_create, valid_v2_payload):
        # Exceed merchant limit of ₹5,000 (500000 paise) with ₹10,000 (1000000 paise)
        valid_v2_payload["accepted"]["extra"]["merchant_id"] = "merchant-demo-001"
        valid_v2_payload["accepted"]["amount"] = "1000000"
        valid_v2_payload["accepted"]["extra"]["items"][0]["unit_amount"] = 1000000

        with TestClient(app) as client:
            res = client.post(
                "/api/v1/execute",
                json={"protocol": "x402", "raw_payload": valid_v2_payload},
                headers={"Authorization": "Bearer key_test_12345"}
            )

        assert res.status_code == 200
        data = res.json()
        assert data["gateway_decision"] == "BLOCK"
        assert "exceeds" in data["reason"].lower()
        mock_create.assert_not_called()

    @patch("app.razorpay.client.RazorpayClient.create_order")
    def test_execute_acp_regression_continues_working(self, mock_create):
        mock_create.return_value = RazorpayOrderResult(
            execution_status=ExecutionStatus.ORDER_CREATED,
            razorpay_order_id="order_acp_regression",
            razorpay_order_status="created",
            razorpay_amount=85000,
            razorpay_currency="INR",
            razorpay_receipt="acp-rcpt",
            razorpay_payment_id=None,
            razorpay_payment_status=None,
            error_code=None,
            error_description=None,
            http_status_code=200,
            timestamp=datetime.now(timezone.utc)
        )

        acp_payload = {
            "items": [
                {
                    "id": "prod_shoe_001",
                    "name": "AeroGlide",
                    "quantity": 1,
                    "unit_amount": 85000,
                    "currency": "INR",
                    "category": "Running"
                }
            ],
            "buyer": {"agent_id": "buyer_001"},
            "merchant_id": "merchant-demo-001",
            "idempotency_key": f"acp_reg_{uuid.uuid4().hex}",
            "bearer_token": f"acp_token_{uuid.uuid4().hex}",
            "api_version": "2026-01-16"
        }

        with TestClient(app) as client:
            res = client.post(
                "/api/v1/execute",
                json={"protocol": "acp", "raw_payload": acp_payload},
                headers={"Authorization": "Bearer key_test_12345"}
            )

        assert res.status_code == 200
        data = res.json()
        assert data["gateway_decision"] == "ALLOW"
        assert data["protocol"] == "acp"
        assert data["razorpay_order_id"] == "order_acp_regression"

    def test_execute_unsupported_protocol_fails_with_400(self):
        with TestClient(app) as client:
            res = client.post(
                "/api/v1/execute",
                json={"protocol": "ap2", "raw_payload": {"some": "data"}},
                headers={"Authorization": "Bearer key_test_12345"}
            )
        assert res.status_code == 400
        assert "Unsupported protocol: ap2" in res.json()["detail"]
