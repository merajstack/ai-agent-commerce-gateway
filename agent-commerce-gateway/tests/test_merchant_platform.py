import pytest
import json
from fastapi.testclient import TestClient

from app.main import app
from app.core.merchant_store import merchant_store, default_merchant_id, MerchantConfig, MerchantSecrets

client = TestClient(app)

@pytest.fixture(autouse=True)
def setup_merchant_store():
    # Setup test merchant in memory
    merchant_id = "test-merchant-001"
    config = MerchantConfig(
        merchant_id=merchant_id,
        merchant_name="Test Merchant",
        razorpay_key_id="rzp_test_123",
        max_transaction_amount=50000,
        allowed_currency="INR",
        blocked_categories=["weapons"]
    )
    # The real api_key_hash for 'sk_test_fake'
    import hashlib
    raw_key = "sk_test_fake"
    hashed_key = hashlib.sha256(raw_key.encode('utf-8')).hexdigest()
    
    secrets = MerchantSecrets(
        merchant_id=merchant_id,
        razorpay_key_secret="secret_123",
        api_key_hash=hashed_key
    )
    
    merchant_store.save_merchant(config, secrets)
    
    # Let tests run
    yield
    
def test_merchant_secrets_not_exposed_on_dashboard():
    # We mock the default merchant id for dashboard to our test merchant
    from app.api.dashboard import get_current_merchant_id
    app.dependency_overrides[get_current_merchant_id] = lambda: "test-merchant-001"
    
    try:
        response = client.get("/api/dashboard/merchant")
        assert response.status_code == 200
        data = response.json()
        assert data["merchant_id"] == "test-merchant-001"
        assert data["max_transaction_amount"] == 50000
        assert "razorpay_key_id" in data
        assert "razorpay_key_secret" not in data
        assert "api_key_hash" not in data
    finally:
        app.dependency_overrides.clear()

def test_api_key_generation_returns_once():
    from app.api.dashboard import get_current_merchant_id
    app.dependency_overrides[get_current_merchant_id] = lambda: "test-merchant-001"
    
    try:
        response = client.post("/api/dashboard/merchant/api-key", headers={"X-Dashboard-Token": "secret_dashboard_token"})
        assert response.status_code == 200
        data = response.json()
        assert "api_key" in data
        assert data["api_key"].startswith("sk_test_")
        
        # Verify it was saved as a hash
        secrets = merchant_store.get_merchant_secrets("test-merchant-001")
        assert secrets.api_key_hash != data["api_key"]
        
        # Verify dashboard still doesn't expose it
        dash_resp = client.get("/api/dashboard/merchant")
        assert "api_key" not in dash_resp.json()
        assert "api_key_hash" not in dash_resp.json()
    finally:
        app.dependency_overrides.clear()

def test_unauthenticated_dashboard_mutation_rejected():
    # If the user doesn't pass the right X-Dashboard-Token, mutations should 401
    from app.api.dashboard import get_current_merchant_id
    app.dependency_overrides[get_current_merchant_id] = lambda: "test-merchant-001"
    
    try:
        # 1. Missing header
        res1 = client.post("/api/dashboard/merchant/api-key")
        assert res1.status_code == 401
        
        # 2. Wrong token
        res2 = client.post("/api/dashboard/merchant/razorpay", 
                           headers={"X-Dashboard-Token": "wrong_token"},
                           json={"key_id": "test", "key_secret": "test"})
        assert res2.status_code == 401
        
        # 3. Policy update without token
        res3 = client.post("/api/dashboard/merchant/policy", 
                           json={"max_transaction_amount": 1000, "allowed_currency": "INR", "blocked_categories": []})
        assert res3.status_code == 401
    finally:
        app.dependency_overrides.clear()

def test_gateway_execution_missing_auth():
    response = client.post("/api/v1/execute", json={"protocol": "acp", "raw_payload": {}})
    assert response.status_code == 401
    assert "Missing or invalid Authorization header" in response.text

def test_gateway_execution_invalid_auth():
    response = client.post("/api/v1/execute", 
                           headers={"Authorization": "Bearer sk_test_invalid"},
                           json={"protocol": "acp", "raw_payload": {}})
    assert response.status_code == 401
    assert "Unauthorized" in response.text

def test_gateway_execution_valid_auth_but_wrong_merchant_id():
    # Payload claims it's for some other merchant, but we authenticated as test-merchant-001
    payload = {
        "protocol": "acp",
        "raw_payload": {
            "merchant_id": "different-merchant-999",
            "buyer": {"agent_id": "agent-1"},
            "items": [{"id": "p1", "quantity": 1, "unit_amount": 1000, "currency": "INR"}],
            "idempotency_key": "dummy-idem",
            "bearer_token": "dummy-token"
        }
    }

    response = client.post("/api/v1/execute", 
                           headers={"Authorization": "Bearer sk_test_fake"},
                           json=payload)
    print("Response:", response.text)
    assert response.status_code == 403
    assert "Merchant ID mismatch" in response.text

def test_gateway_verify_credentials():
    # Valid auth
    res_ok = client.get("/api/v1/verify", headers={"Authorization": "Bearer sk_test_fake"})
    assert res_ok.status_code == 200
    assert res_ok.json() == {"valid": True, "merchant_id": "test-merchant-001"}

    # Invalid auth
    res_bad = client.get("/api/v1/verify", headers={"Authorization": "Bearer sk_test_wrong"})
    assert res_bad.status_code == 401

def test_gateway_execution_valid_auth_creates_order():
    payload = {
        "protocol": "acp",
        "raw_payload": {
            "merchant_id": "test-merchant-001",
            "buyer": {"agent_id": "agent-1"},
            "items": [{"id": "shoe-1", "quantity": 1, "unit_amount": 8500, "currency": "INR", "category": "footwear"}],
            "idempotency_key": "dummy-idem-allow-1",
            "bearer_token": "dummy-token"
        }
    }
    
    from app.core.schemas import AuthorizationVerificationResult
    from unittest.mock import patch
    import httpx
    
    mock_order_response = {
        "id": "order_test_12345",
        "status": "created",
        "amount": 8500,
        "currency": "INR",
        "receipt": "acp-dummy-idem-allow-1"
    }

    with patch("app.adapters.acp_provider.ACPAuthorizationProvider.verify", 
               return_value=AuthorizationVerificationResult(valid=True, reason="Mocked valid", requires_replay_check=False)), \
         patch("httpx.Client") as MockClient:
        
        MockClient.return_value.__enter__.return_value.post.return_value = httpx.Response(
            200, 
            content=json.dumps(mock_order_response).encode(),
            headers={"content-type": "application/json"}
        )
        
        response = client.post("/api/v1/execute", 
                               headers={"Authorization": "Bearer sk_test_fake"},
                               json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["gateway_decision"] == "ALLOW"
        assert data["razorpay_order_id"] == "order_test_12345"
        assert data["razorpay_key_id"] == "rzp_test_123"
        assert data["amount_minor"] == 8500
        assert "canonical_request" in data
        assert data["canonical_request"]["total"]["amount_minor"] == 8500
        assert "pipeline_stages" in data
        stages = [s["stage"] for s in data["pipeline_stages"]]
        assert "REQUEST" in stages
        assert "PROTOCOL_ADAPTER" in stages
        assert "AUTHORIZATION" in stages
        assert "REPLAY" in stages
        assert "POLICY" in stages
        assert "DECISION" in stages
        assert "RAZORPAY_ORDER" in stages

def test_gateway_execution_policy_block_never_creates_order():
    # Transaction amount 60,000 exceeds max_transaction_amount 50,000
    payload = {
        "protocol": "acp",
        "raw_payload": {
            "merchant_id": "test-merchant-001",
            "buyer": {"agent_id": "agent-1"},
            "items": [{"id": "shoe-exorbitant", "quantity": 1, "unit_amount": 60000, "currency": "INR", "category": "footwear"}],
            "idempotency_key": "dummy-idem-block-1",
            "bearer_token": "dummy-token"
        }
    }
    
    from app.core.schemas import AuthorizationVerificationResult
    from unittest.mock import patch
    
    with patch("app.adapters.acp_provider.ACPAuthorizationProvider.verify", 
               return_value=AuthorizationVerificationResult(valid=True, reason="Mocked valid", requires_replay_check=False)), \
         patch("httpx.Client") as MockClient:
        
        response = client.post("/api/v1/execute", 
                               headers={"Authorization": "Bearer sk_test_fake"},
                               json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["gateway_decision"] == "BLOCK"
        assert data["razorpay_order_id"] is None
        assert data["razorpay_key_id"] is None
        assert "exceeds" in data["reason"].lower()
        # MockClient.post should NEVER be called on BLOCK
        MockClient.return_value.__enter__.return_value.post.assert_not_called()

def test_payment_verification_endpoint():
    import hmac
    import hashlib

    order_id = "order_test_12345"
    payment_id = "pay_test_67890"
    secret = "secret_123"
    msg = f"{order_id}|{payment_id}".encode("utf-8")
    valid_sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()

    payload = {
        "razorpay_order_id": order_id,
        "razorpay_payment_id": payment_id,
        "razorpay_signature": valid_sig,
        "amount_minor": 8500,
        "currency": "INR",
        "auto_captured": True
    }

    response = client.post("/api/v1/payments/verify",
                           headers={"Authorization": "Bearer sk_test_fake"},
                           json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["execution_status"] == "payment_captured"
    assert data["razorpay_payment_id"] == payment_id


