import hashlib
import os
import secrets
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Header
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from app.config import settings
from app.core.merchant_store import merchant_store, default_merchant_id, MerchantConfig, MerchantSecrets
from app.core.audit import audit_store

router = APIRouter(prefix="/api/dashboard", tags=["Dashboard"])


# Currently hardcoded to the default demo merchant to avoid adding full login for the hackathon.
# All mutation endpoints ALSO require X-Dashboard-Token authentication (see require_dashboard_auth).
def get_current_merchant_id() -> str:
    return default_merchant_id


def require_dashboard_auth(
    x_dashboard_token: Optional[str] = Header(default=None, alias="X-Dashboard-Token")
) -> None:
    """
    Dependency enforcing dashboard session authentication for all mutation endpoints.

    The DASHBOARD_SECRET env var can be set to a custom value or defaults to secret_dashboard_token.
    Callers must include: X-Dashboard-Token: <DASHBOARD_SECRET>
    """
    expected_secret = settings.dashboard_secret or os.getenv("DASHBOARD_SECRET", "secret_dashboard_token")
    if not expected_secret:
        raise HTTPException(
            status_code=503,
            detail="Dashboard mutations are disabled: DASHBOARD_SECRET is not configured."
        )
    if not x_dashboard_token:
        raise HTTPException(
            status_code=401,
            detail="Missing X-Dashboard-Token header. Dashboard mutations require authentication."
        )
    # Constant-time comparison prevents timing-based secret extraction
    if not secrets.compare_digest(x_dashboard_token, expected_secret):
        raise HTTPException(
            status_code=401,
            detail="Invalid X-Dashboard-Token."
        )


class RazorpayCredentialsInput(BaseModel):
    key_id: str
    key_secret: str

class PolicyInput(BaseModel):
    max_transaction_amount: Optional[int]
    allowed_currency: Optional[str]
    blocked_categories: List[str]

@router.get("/", response_class=HTMLResponse)
def get_dashboard_ui():
    """Serves the dashboard HTML."""
    import os
    html_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    with open(html_path, "r") as f:
        return f.read()

@router.get("/merchant", response_model=MerchantConfig)
def get_merchant_profile(merchant_id: str = Depends(get_current_merchant_id)):
    """Returns the merchant configuration safely, explicitly excluding secrets."""
    config = merchant_store.get_merchant(merchant_id)
    if not config:
        raise HTTPException(status_code=404, detail="Merchant not found")
    return config

@router.post("/merchant/razorpay")
def update_razorpay_credentials(
    creds: RazorpayCredentialsInput, 
    merchant_id: str = Depends(get_current_merchant_id),
    _auth = Depends(require_dashboard_auth)
):
    """Securely saves Razorpay credentials. Returns nothing."""
    config = merchant_store.get_merchant(merchant_id)
    secrets_obj = merchant_store.get_merchant_secrets(merchant_id)
    
    if not config or not secrets_obj:
        raise HTTPException(status_code=404, detail="Merchant not found")
        
    config.razorpay_key_id = creds.key_id
    secrets_obj.razorpay_key_secret = creds.key_secret
    
    merchant_store.save_merchant(config, secrets_obj)
    return {"status": "success"}

@router.post("/merchant/policy")
def update_policy(
    policy: PolicyInput,
    merchant_id: str = Depends(get_current_merchant_id),
    _auth = Depends(require_dashboard_auth)
):
    """Updates AI Commerce policies."""
    config = merchant_store.get_merchant(merchant_id)
    secrets_obj = merchant_store.get_merchant_secrets(merchant_id)
    
    if not config or not secrets_obj:
        raise HTTPException(status_code=404, detail="Merchant not found")
        
    config.max_transaction_amount = policy.max_transaction_amount
    config.allowed_currency = policy.allowed_currency
    config.blocked_categories = policy.blocked_categories
    
    merchant_store.save_merchant(config, secrets_obj)
    return {"status": "success"}

@router.post("/merchant/api-key")
def generate_api_key(
    merchant_id: str = Depends(get_current_merchant_id),
    _auth = Depends(require_dashboard_auth)
):
    """Generates a new API key, hashes it, and returns the plaintext ONCE."""
    config = merchant_store.get_merchant(merchant_id)
    secrets_obj = merchant_store.get_merchant_secrets(merchant_id)
    
    if not config or not secrets_obj:
        raise HTTPException(status_code=404, detail="Merchant not found")
        
    # Generate standard sk_test_... key
    raw_key = f"sk_test_{secrets.token_hex(24)}"
    
    # Hash and save
    hashed_key = hashlib.sha256(raw_key.encode('utf-8')).hexdigest()
    secrets_obj.api_key_hash = hashed_key
    merchant_store.save_merchant(config, secrets_obj)
    
    # Return exactly once
    return {"api_key": raw_key, "message": "Save this key now. It will not be shown again."}

@router.get("/transactions")
def get_transactions(
    limit: int = 50,
    merchant_id: str = Depends(get_current_merchant_id),
):
    """Retrieves in-memory audit events for the authenticated merchant."""
    events = audit_store.get_events(merchant_id=merchant_id, limit=limit)
    
    return [
        {
            "transaction_id": e.transaction_id,
            "timestamp": e.timestamp.isoformat() if hasattr(e.timestamp, "isoformat") else str(e.timestamp),
            "stage": e.stage.value if hasattr(e.stage, "value") else str(e.stage),
            "event_type": e.event_type.value if hasattr(e.event_type, "value") else str(e.event_type),
            "decision": e.decision,
            "reason": e.reason,
            "amount_minor": e.amount_minor,
            "currency": e.currency,
            "protocol": e.protocol,
            "razorpay_order_id": e.razorpay_order_id,
            "razorpay_payment_id": e.razorpay_payment_id,
            "razorpay_payment_status": e.razorpay_payment_status,
        }
        for e in events
    ]


