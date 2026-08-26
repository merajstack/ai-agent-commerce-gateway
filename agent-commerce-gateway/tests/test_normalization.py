"""
test_normalization.py — Protocol Normalization & Canonical Parity Tests
========================================================================

Tests proving that:
1. ACP normalizes correctly into CommerceRequest.
2. x402 v2 normalizes correctly into CommerceRequest.
3. ACP and x402 produce the exact same canonical schema and commerce facts.
4. Policy BLOCK stops execution before Razorpay order creation (zero order ID).
5. Gateway execute endpoint returns raw_payload, adapter_used, canonical_request,
   pipeline_stages, and final_decision for both protocols.
"""

import hashlib
import uuid
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.db.models  # ensure models are registered on Base
from app.db.database import Base
from app.main import app
from app.api.gateway import get_db
from app.adapters.acp_adapter import ACPAdapter
from app.adapters.x402_adapter import X402Adapter
from app.core.schemas import CommerceRequest, BuyerProtocol
from app.core.merchant_store import merchant_store, MerchantConfig, MerchantSecrets

MERCHANT_ID = "merchant-demo-001"
MERCHANT_API_KEY = "sk_test_normalization_key_12345"
API_KEY_HASH = hashlib.sha256(MERCHANT_API_KEY.encode("utf-8")).hexdigest()


@pytest.fixture
def in_memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    
    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()
            
    app.dependency_overrides[get_db] = override_get_db
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def setup_test_merchant():
    merchant_store.save_merchant(
        MerchantConfig(
            merchant_id=MERCHANT_ID,
            merchant_name="Normalization Test Store",
            max_transaction_amount=5000000,  # 50,000 INR
            allowed_currency="INR",
            razorpay_key_id="rzp_test_norm_key"
        ),
        MerchantSecrets(
            merchant_id=MERCHANT_ID,
            api_key_hash=API_KEY_HASH,
            razorpay_key_secret="rzp_test_norm_secret"
        )
    )
    yield


# ══════════════════════════════════════════════════════════════════════════════
# 1. Direct Adapter Unit Tests
# ══════════════════════════════════════════════════════════════════════════════

def test_acp_normalization_direct():
    """Prove that ACP raw payload normalizes directly to canonical CommerceRequest."""
    adapter = ACPAdapter()
    raw_acp = {
        "items": [
            {
                "id": "prod_shoe_001",
                "name": "AeroGlide Runner",
                "quantity": 2,
                "unit_amount": 850000,
                "currency": "INR",
                "category": "Running"
            }
        ],
        "buyer": {
            "agent_id": "agent_shopper_001",
            "first_name": "Buyer",
            "last_name": "Agent",
            "email": "buyer@agentcommerce.ai"
        },
        "merchant_id": MERCHANT_ID,
        "idempotency_key": f"acp_idemp_{uuid.uuid4().hex}",
        "bearer_token": "acp_token_xyz",
        "api_version": "2026-01-16"
    }

    req = adapter.parse_request(raw_acp)
    assert isinstance(req, CommerceRequest)
    assert req.merchant_id == MERCHANT_ID
    assert req.buyer_agent_id == "agent_shopper_001"
    assert req.buyer_protocol == BuyerProtocol.acp
    assert req.calculated_total.amount_minor == 1700000
    assert req.calculated_total.currency == "INR"
    assert len(req.items) == 1
    assert req.items[0].product_id == "prod_shoe_001"
    assert req.items[0].quantity == 2
    assert req.items[0].unit_price.amount_minor == 850000
    assert req.items[0].unit_price.currency == "INR"


def test_x402_normalization_direct():
    """Prove that x402 v2 PaymentPayload normalizes directly to canonical CommerceRequest."""
    adapter = X402Adapter()
    raw_x402 = {
        "x402Version": 2,
        "resource": {
            "url": "http://localhost:3000/api/checkout-intent",
            "description": "Order checkout for 2x AeroGlide Runner",
            "mimeType": "application/json"
        },
        "accepted": {
            "scheme": "exact",
            "network": "eip155:84532",
            "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
            "amount": "1700000",
            "payTo": "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
            "maxTimeoutSeconds": 300,
            "extra": {
                "merchant_id": MERCHANT_ID,
                "buyer_agent_id": "agent_shopper_001",
                "currency": "INR",
                "items": [
                    {
                        "id": "prod_shoe_001",
                        "name": "AeroGlide Runner",
                        "quantity": 2,
                        "unit_amount": 850000,
                        "currency": "INR",
                        "category": "Running"
                    }
                ]
            }
        },
        "payload": {
            "authorization": {
                "from": "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC",
                "to": "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
                "value": "1700000",
                "validAfter": 0,
                "validBefore": 1800000000,
                "nonce": f"0x{uuid.uuid4().hex}{uuid.uuid4().hex}"
            },
            "signature": f"0x{uuid.uuid4().hex}"
        },
        "extensions": {}
    }

    req = adapter.parse_request(raw_x402)
    assert isinstance(req, CommerceRequest)
    assert req.merchant_id == MERCHANT_ID
    assert req.buyer_agent_id == "agent_shopper_001"
    assert req.buyer_protocol == BuyerProtocol.x402
    assert req.calculated_total.amount_minor == 1700000
    assert req.calculated_total.currency == "INR"
    assert len(req.items) == 1
    assert req.items[0].product_id == "prod_shoe_001"
    assert req.items[0].quantity == 2
    assert req.items[0].unit_price.amount_minor == 850000
    assert req.items[0].unit_price.currency == "INR"


def test_canonical_schema_parity():
    """
    Prove that ACP and x402 produce IDENTICAL canonical commerce values
    when fed the same underlying purchase.
    """
    acp_adapter = ACPAdapter()
    x402_adapter = X402Adapter()

    raw_acp = {
        "items": [
            {"id": "shoe_aqua", "name": "Aqua Walker", "quantity": 1, "unit_amount": 420000, "currency": "INR", "category": "Outdoor"},
            {"id": "shoe_urban", "name": "Urban Kicks Classic", "quantity": 2, "unit_amount": 520000, "currency": "INR", "category": "Casual"}
        ],
        "buyer": {"agent_id": "shopper_agent_99"},
        "merchant_id": MERCHANT_ID,
        "idempotency_key": f"parity_nonce_acp_{uuid.uuid4().hex}",
        "bearer_token": "token_acp_1",
        "api_version": "2026-01-16"
    }

    raw_x402 = {
        "x402Version": 2,
        "resource": {"url": "http://localhost:3000/checkout"},
        "accepted": {
            "scheme": "exact",
            "network": "eip155:84532",
            "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
            "amount": str(420000 + 2 * 520000),  # 1,460,000 minor
            "payTo": "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
            "extra": {
                "merchant_id": MERCHANT_ID,
                "buyer_agent_id": "shopper_agent_99",
                "currency": "INR",
                "items": [
                    {"id": "shoe_aqua", "name": "Aqua Walker", "quantity": 1, "unit_amount": 420000, "currency": "INR", "category": "Outdoor"},
                    {"id": "shoe_urban", "name": "Urban Kicks Classic", "quantity": 2, "unit_amount": 520000, "currency": "INR", "category": "Casual"}
                ]
            }
        },
        "payload": {
            "authorization": {"nonce": f"parity_nonce_x402_{uuid.uuid4().hex}", "from": "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC"},
            "signature": f"0x{uuid.uuid4().hex}"
        },
        "extensions": {}
    }

    acp_req = acp_adapter.parse_request(raw_acp)
    x402_req = x402_adapter.parse_request(raw_x402)

    # 1. Total Amount & Currency Parity
    assert acp_req.calculated_total.amount_minor == x402_req.calculated_total.amount_minor == 1460000
    assert acp_req.calculated_total.currency == x402_req.calculated_total.currency == "INR"

    # 2. Merchant & Buyer Identity Parity
    assert acp_req.merchant_id == x402_req.merchant_id == MERCHANT_ID
    assert acp_req.buyer_agent_id == x402_req.buyer_agent_id == "shopper_agent_99"

    # 3. Line Items Structural Parity
    assert len(acp_req.items) == len(x402_req.items) == 2
    for item_acp, item_x402 in zip(acp_req.items, x402_req.items):
        assert item_acp.product_id == item_x402.product_id
        assert item_acp.name == item_x402.name
        assert item_acp.quantity == item_x402.quantity
        assert item_acp.unit_price.amount_minor == item_x402.unit_price.amount_minor
        assert item_acp.unit_price.currency == item_x402.unit_price.currency
        assert item_acp.category == item_x402.category


# ══════════════════════════════════════════════════════════════════════════════
# 2. Full Endpoint Normalization & Execution Verification
# ══════════════════════════════════════════════════════════════════════════════

@patch("app.api.gateway.execute_razorpay_payment")
def test_gateway_execute_acp_allow_and_normalization(mock_execute_rzp, in_memory_db):
    """Test full ACP execution through /api/v1/execute."""
    mock_receipt = MagicMock()
    mock_receipt.decision.value = "ALLOW"
    mock_receipt.payment_reference = "order_rzp_acp_test_001"
    mock_receipt.status = "order_created"
    mock_receipt.transaction_id = f"acp-idemp-{uuid.uuid4().hex}"
    mock_execute_rzp.return_value = mock_receipt

    client = TestClient(app)
    headers = {"Authorization": f"Bearer {MERCHANT_API_KEY}"}
    idemp_key = f"acp_idemp_allow_{uuid.uuid4().hex}"
    raw_payload = {
        "items": [{"id": "prod_001", "name": "AeroGlide", "quantity": 1, "unit_amount": 850000, "currency": "INR"}],
        "buyer": {"agent_id": "agent_001"},
        "merchant_id": MERCHANT_ID,
        "idempotency_key": idemp_key,
        "bearer_token": "valid_token",
        "api_version": "2026-01-16"
    }

    res = client.post("/api/v1/execute", json={"protocol": "acp", "raw_payload": raw_payload}, headers=headers)
    assert res.status_code == 200
    data = res.json()

    assert data["protocol"] == "acp"
    assert data["adapter_used"] == "ACPAdapter"
    assert data["gateway_decision"] == "ALLOW"
    assert data["final_decision"] == "ALLOW"
    assert data["razorpay_order_id"] == "order_rzp_acp_test_001"
    assert data["raw_payload"] == raw_payload

    # Canonical request assertions
    canonical = data["canonical_request"]
    assert canonical["merchant_id"] == MERCHANT_ID
    assert canonical["buyer_agent_id"] == "agent_001"
    assert canonical["total"]["amount_minor"] == 850000
    assert canonical["total"]["currency"] == "INR"
    assert len(canonical["line_items"]) == 1

    # Pipeline stages assertions
    stages = data["pipeline_stages"]
    assert len(stages) >= 7
    stage_names = [s["stage"] for s in stages]
    assert "REQUEST" in stage_names
    assert "PROTOCOL_ADAPTER" in stage_names
    assert "CANONICAL_NORMALIZATION" in stage_names
    assert "AUTHORIZATION" in stage_names
    assert "REPLAY" in stage_names
    assert "POLICY" in stage_names
    assert "DECISION" in stage_names
    assert "RAZORPAY_ORDER" in stage_names


@patch("app.api.gateway.execute_razorpay_payment")
def test_gateway_execute_x402_allow_and_normalization(mock_execute_rzp, in_memory_db):
    """Test full x402 execution through /api/v1/execute."""
    mock_receipt = MagicMock()
    mock_receipt.decision.value = "ALLOW"
    mock_receipt.payment_reference = "order_rzp_x402_test_001"
    mock_receipt.status = "order_created"
    mock_receipt.transaction_id = f"x402-nonce-{uuid.uuid4().hex}"
    mock_execute_rzp.return_value = mock_receipt

    client = TestClient(app)
    headers = {"Authorization": f"Bearer {MERCHANT_API_KEY}"}
    raw_payload = {
        "x402Version": 2,
        "resource": {"url": "http://localhost:3000/api/checkout"},
        "accepted": {
            "scheme": "exact",
            "network": "eip155:84532",
            "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
            "amount": "850000",
            "payTo": "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
            "extra": {
                "merchant_id": MERCHANT_ID,
                "buyer_agent_id": "agent_001",
                "currency": "INR",
                "items": [{"id": "prod_001", "name": "AeroGlide", "quantity": 1, "unit_amount": 850000, "currency": "INR"}]
            }
        },
        "payload": {
            "authorization": {
                "from": "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC",
                "nonce": f"0xnonce_x402_{uuid.uuid4().hex}"
            },
            "signature": f"0x{uuid.uuid4().hex}"
        },
        "extensions": {}
    }

    res = client.post("/api/v1/execute", json={"protocol": "x402", "raw_payload": raw_payload}, headers=headers)
    assert res.status_code == 200
    data = res.json()

    assert data["protocol"] == "x402"
    assert data["adapter_used"] == "X402Adapter"
    assert data["gateway_decision"] == "ALLOW"
    assert data["final_decision"] == "ALLOW"
    assert data["razorpay_order_id"] == "order_rzp_x402_test_001"
    assert data["raw_payload"] == raw_payload

    # Canonical request structure invariance check
    canonical = data["canonical_request"]
    assert canonical["merchant_id"] == MERCHANT_ID
    assert canonical["buyer_agent_id"] == "agent_001"
    assert canonical["total"]["amount_minor"] == 850000
    assert canonical["total"]["currency"] == "INR"


@patch("app.api.gateway.execute_razorpay_payment")
def test_policy_block_prevents_razorpay_order(mock_execute_rzp, in_memory_db):
    """Prove that when Policy Engine returns BLOCK, Razorpay is NEVER called."""
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {MERCHANT_API_KEY}"}
    
    # Configure merchant with small limit: 5,000 INR (500,000 minor)
    merchant_store.save_merchant(
        MerchantConfig(
            merchant_id=MERCHANT_ID,
            merchant_name="Strict Limit Store",
            max_transaction_amount=500000,  # 5,000 INR max
            allowed_currency="INR",
            razorpay_key_id="rzp_test_norm_key"
        ),
        MerchantSecrets(
            merchant_id=MERCHANT_ID,
            api_key_hash=API_KEY_HASH,
            razorpay_key_secret="rzp_test_norm_secret"
        )
    )

    # Attempt transaction of 14,500 INR (1,450,000 minor)
    raw_acp_expensive = {
        "items": [{"id": "prod_007", "name": "Leather Oxford Elite", "quantity": 1, "unit_amount": 1450000, "currency": "INR"}],
        "buyer": {"agent_id": "agent_over_limit"},
        "merchant_id": MERCHANT_ID,
        "idempotency_key": f"acp_expensive_block_{uuid.uuid4().hex}",
        "bearer_token": "valid_token",
        "api_version": "2026-01-16"
    }

    res = client.post("/api/v1/execute", json={"protocol": "acp", "raw_payload": raw_acp_expensive}, headers=headers)
    assert res.status_code == 200
    data = res.json()

    # Must be BLOCKED
    assert data["gateway_decision"] == "BLOCK"
    assert data["final_decision"] == "BLOCK"
    assert data["razorpay_order_id"] is None
    assert data["raw_payload"] == raw_acp_expensive
    assert "exceeds" in data["reason"].lower() or "limit" in data["reason"].lower()

    # Razorpay execution MUST NOT have been called!
    mock_execute_rzp.assert_not_called()


def test_merchant_id_mismatch_fails_closed(in_memory_db):
    """Prove that merchant isolation strictly blocks mismatched merchant IDs."""
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {MERCHANT_API_KEY}"}
    raw_payload_wrong_merchant = {
        "items": [{"id": "prod_001", "name": "AeroGlide", "quantity": 1, "unit_amount": 850000, "currency": "INR"}],
        "buyer": {"agent_id": "agent_001"},
        "merchant_id": "attacker-merchant-999",  # Mismatch!
        "idempotency_key": f"acp_idemp_mismatch_{uuid.uuid4().hex}",
        "bearer_token": "valid_token",
        "api_version": "2026-01-16"
    }

    res = client.post("/api/v1/execute", json={"protocol": "acp", "raw_payload": raw_payload_wrong_merchant}, headers=headers)
    assert res.status_code == 403
    assert "mismatch" in res.json()["detail"].lower()
