from fastapi.testclient import TestClient
from unittest.mock import patch
import json

from app.main import app
from app.config import settings
from app.razorpay.client import ExecutionStatus

def test_ui_loads():
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "Agent Commerce Gateway" in response.text
        assert "checkout.razorpay.com" in response.text

def test_config_endpoint_safe():
    with TestClient(app) as client:
        response = client.get("/api/demo/config")
        assert response.status_code == 200
        data = response.json()
        assert "razorpay_key_id" in data
        assert "razorpay_key_secret" not in data
        assert data["razorpay_key_id"] == settings.razorpay_key_id

@patch("app.razorpay.client.RazorpayClient.verify_payment_signature")
def test_verify_endpoint_fails_closed_on_invalid_sig(mock_verify):
    mock_verify.return_value = False
    with TestClient(app) as client:
        response = client.post("/api/demo/payment/verify", json={
            "razorpay_order_id": "order_test",
            "razorpay_payment_id": "pay_test",
            "razorpay_signature": "bad_sig",
            "amount_minor": 5000,
            "currency": "INR"
        })
        assert response.status_code == 200
        data = response.json()
        assert data["is_success"] is False
        assert data["status"] == ExecutionStatus.SIGNATURE_INVALID.value

def test_purchase_endpoint_blocks_limit():
    payload = {
        "buyer_id": "demo-buyer",
        "merchant_id": "demo-merchant",
        "protocol": "acp",
        "item_name": "test",
        "quantity": 1,
        "amount_minor": 50000,
        "currency": "INR",
        "merchant_limit": 10000, # Limit lower than amount
        "blocked_categories": [],
        "authorization_valid": True
    }
    with patch("httpx.Client") as mock_client:
        with TestClient(app) as client:
            response = client.post("/api/demo/purchase", json=payload)
            mock_client.return_value.__enter__.assert_not_called() # Razorpay must not be called
            
        assert response.status_code == 200
        data = response.json()
        print(f"DEBUG REASON: {data.get('reason')}")
        assert data["decision"] == "BLOCK"
        assert data["stage"] == "POLICY"
        assert data["receipt_status"] == "execution_refused"

def test_purchase_endpoint_blocks_invalid_auth():
    payload = {
        "buyer_id": "demo-buyer",
        "merchant_id": "demo-merchant",
        "protocol": "acp",
        "item_name": "test",
        "quantity": 1,
        "amount_minor": 5000,
        "currency": "INR",
        "merchant_limit": 100000,
        "blocked_categories": [],
        "authorization_valid": False # Force auth fail
    }
    with patch("httpx.Client") as mock_client:
        with TestClient(app) as client:
            response = client.post("/api/demo/purchase", json=payload)
            mock_client.return_value.__enter__.assert_not_called()
            
        assert response.status_code == 200
        data = response.json()
        assert data["decision"] == "BLOCK"
        assert data["stage"] == "AUTHORIZATION"

def test_purchase_endpoint_allows_valid():
    payload = {
        "buyer_id": "demo-buyer",
        "merchant_id": "demo-merchant",
        "protocol": "acp",
        "item_name": "test",
        "quantity": 1,
        "amount_minor": 5000,
        "currency": "INR",
        "merchant_limit": 100000,
        "blocked_categories": [],
        "authorization_valid": True
    }
    
    with patch("app.razorpay.client.RazorpayClient.create_order") as mock_create:
        from app.razorpay.client import RazorpayOrderResult
        from datetime import datetime, timezone
        mock_create.return_value = RazorpayOrderResult(
            execution_status=ExecutionStatus.ORDER_CREATED,
            razorpay_order_id="order_demo123",
            razorpay_order_status="created",
            razorpay_amount=5000,
            razorpay_currency="INR",
            razorpay_receipt="txn-test",
            razorpay_payment_id=None,
            razorpay_payment_status=None,
            error_code=None,
            error_description=None,
            http_status_code=200,
            timestamp=datetime.now(timezone.utc)
        )
        
        with TestClient(app) as client:
            response = client.post("/api/demo/purchase", json=payload)
        
    assert response.status_code == 200
    data = response.json()
    assert data["decision"] == "ALLOW"
    assert data["stage"] == "FINAL"
    assert data["receipt_status"] == "order_created"
    assert data["razorpay_order_id"] == "order_demo123"

def test_audit_endpoint_returns_list():
    with TestClient(app) as client:
        response = client.get("/api/demo/audit")
        assert response.status_code == 200
        assert isinstance(response.json(), list)
