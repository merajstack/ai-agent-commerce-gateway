import hashlib
from typing import Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.merchant_store import merchant_store
from app.core.policy import PolicyConfig
from app.core.orchestrator import process_transaction
from app.db.database import get_engine, get_session_factory
from app.core.replay import SQLiteReplayStore
from app.core.audit import (
    AuditLogger,
    AuditStage,
    audit_pipeline_decision,
    audit_razorpay_execution,
    audit_request_received,
)
from app.razorpay.client import (
    RazorpayClient,
    execute_razorpay_payment,
    verify_payment_and_capture,
    ExecutionStatus,
)

from app.adapters.acp_adapter import ACPAdapter
from app.adapters.acp_provider import ACPAuthorizationProvider
from app.adapters.x402_adapter import X402Adapter
from app.adapters.x402_provider import X402AuthorizationProvider, SandboxX402Verifier
from app.core.schemas import CommerceReceipt, GatewayDecision
from datetime import datetime, timezone

router = APIRouter(prefix="/api/v1", tags=["Gateway"])

def get_db():
    engine = get_engine()
    factory = get_session_factory(engine)
    session = factory()
    try:
        yield session
    finally:
        session.close()

def authenticate_merchant(request: Request) -> str:
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    
    token = auth_header[7:].strip()
    token_hash = hashlib.sha256(token.encode('utf-8')).hexdigest()
    
    merchant_id = merchant_store.get_merchant_id_by_api_key_hash(token_hash)
    if not merchant_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    return merchant_id

class PaymentVerificationInput(BaseModel):
    razorpay_order_id: str = Field(..., min_length=1)
    razorpay_payment_id: str = Field(..., min_length=1)
    razorpay_signature: str = Field(..., min_length=1)
    amount_minor: int = Field(..., gt=0)
    currency: str = Field(default="INR", min_length=3, max_length=3)
    auto_captured: bool = Field(default=False)

@router.get("/verify")
async def verify_merchant_credentials(merchant_id: str = Depends(authenticate_merchant)):
    """Verifies that the merchant's API key is valid and returns merchant ID."""
    return {"valid": True, "merchant_id": merchant_id}

@router.post("/execute")
async def execute_protocol_request(
    request: Request,
    merchant_id: str = Depends(authenticate_merchant),
    db: Session = Depends(get_db)
):
    """
    Unified entrypoint for all AI protocol requests.
    Validates protocol, normalizes to CommerceRequest, and runs through security pipeline.
    """
    body = await request.json()
    
    protocol = body.get("protocol")
    raw_payload = body.get("raw_payload")
    
    if not protocol or not raw_payload:
        raise HTTPException(status_code=400, detail="Missing protocol or raw_payload")
    
    # Route to adapter
    if protocol == "acp":
        adapter = ACPAdapter()
        auth_provider = ACPAuthorizationProvider()
    elif protocol == "x402":
        adapter = X402Adapter()
        auth_provider = X402AuthorizationProvider(verifier=SandboxX402Verifier())
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported protocol: {protocol}")
        
    try:
        commerce_req = adapter.parse_request(raw_payload)
        auth_proof = adapter.parse_authorization_proof(raw_payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Protocol parsing error: {str(e)}")

    # Initialize Audit Logger (in-memory AuditStore)
    audit = AuditLogger()

    # Immediately audit request received BEFORE any policy checks or Razorpay orders
    audit.record(audit_request_received(
        transaction_id=commerce_req.transaction_id,
        protocol=protocol,
        merchant_id=merchant_id,
        buyer_agent_id=commerce_req.buyer_agent_id,
        amount_minor=commerce_req.calculated_total.amount_minor,
        currency=commerce_req.calculated_total.currency,
        reason=f"Received {protocol.upper()} transaction request for {commerce_req.calculated_total.amount_minor} {commerce_req.calculated_total.currency}"
    ))

    # Enforce Merchant Isolation:
    if commerce_req.merchant_id != merchant_id:
        audit.record(audit_pipeline_decision(
            transaction_id=commerce_req.transaction_id,
            stage=AuditStage.VALIDATION,
            decision="BLOCK",
            reason="Merchant ID mismatch against authenticated API key",
            merchant_id=merchant_id,
            buyer_agent_id=commerce_req.buyer_agent_id,
            protocol=protocol,
            amount_minor=commerce_req.calculated_total.amount_minor,
            currency=commerce_req.calculated_total.currency,
        ))
        raise HTTPException(status_code=403, detail="Merchant ID mismatch")
    
    # Load merchant policy
    config = merchant_store.get_merchant(merchant_id)
    if not config:
        raise HTTPException(status_code=404, detail="Merchant configuration not found")
        
    policy_config = PolicyConfig(
        max_transaction_amount=config.max_transaction_amount,
        allowed_currencies={config.allowed_currency} if config.allowed_currency else None,
        blocked_categories=set(config.blocked_categories) if config.blocked_categories else None,
    )
    
    # Run Orchestrator Pipeline (Validation -> Auth -> Replay -> Policy)
    replay_store = SQLiteReplayStore(db)
    
    pipeline_result = process_transaction(
        request=commerce_req,
        auth_proof=auth_proof,
        auth_provider=auth_provider,
        policy_config=policy_config,
        replay_store=replay_store,
    )
    
    # Audit pipeline decision
    audit.record(audit_pipeline_decision(
        transaction_id=commerce_req.transaction_id,
        stage=AuditStage.FINAL if pipeline_result.decision.value == "ALLOW" else AuditStage.POLICY,
        decision=pipeline_result.decision.value,
        reason=pipeline_result.reason,
        merchant_id=merchant_id,
        buyer_agent_id=commerce_req.buyer_agent_id,
        protocol=protocol,
        amount_minor=commerce_req.calculated_total.amount_minor,
        currency=commerce_req.calculated_total.currency,
    ))

    # Form Normalized Canonical Request object
    canonical_request_data = {
        "transaction_id": commerce_req.transaction_id,
        "merchant_id": commerce_req.merchant_id,
        "buyer_agent_id": commerce_req.buyer_agent_id,
        "protocol": protocol,
        "total": {
            "amount_minor": commerce_req.calculated_total.amount_minor,
            "currency": commerce_req.calculated_total.currency,
            "amount_major": commerce_req.calculated_total.amount_minor / 100
        },
        "line_items": [
            {
                "id": getattr(item, "product_id", getattr(item, "id", "item-1")),
                "name": getattr(item, "name", None) or getattr(item, "id", "Footwear Item"),
                "quantity": getattr(item, "quantity", 1),
                "unit_amount_minor": item.unit_price.amount_minor if hasattr(item, "unit_price") else getattr(item, "unit_amount", 0),
                "currency": item.unit_price.currency if hasattr(item, "unit_price") else commerce_req.calculated_total.currency
            }
            for item in (commerce_req.items if hasattr(commerce_req, "items") and commerce_req.items else [])
        ]
    }

    auth_passed = bool(pipeline_result.authorization_result and pipeline_result.authorization_result.valid)
    replay_passed = bool((pipeline_result.replay_result and pipeline_result.replay_result.allowed) or (pipeline_result.authorization_result and not pipeline_result.authorization_result.requires_replay_check))
    policy_passed = bool(pipeline_result.policy_result and pipeline_result.policy_result.decision == GatewayDecision.ALLOW)

    adapter_name = adapter.__class__.__name__
    timestamp_iso = datetime.now(timezone.utc).isoformat()

    pipeline_stages = [
        {
            "stage": "REQUEST",
            "name": f"Incoming Protocol Request ({protocol.upper()})",
            "status": "PASSED",
            "protocol": protocol,
            "details": f"Received {protocol.upper()} payload (tx: {commerce_req.transaction_id})"
        },
        {
            "stage": "PROTOCOL_ADAPTER",
            "name": f"Protocol Adapter ({adapter_name})",
            "status": "PASSED",
            "adapter": adapter_name,
            "details": f"Mapped {protocol.upper()} fields into canonical schema"
        },
        {
            "stage": "CANONICAL_NORMALIZATION",
            "name": "Canonical CommerceRequest",
            "status": "PASSED",
            "details": f"Unified to CommerceRequest: {commerce_req.calculated_total.amount_minor} {commerce_req.calculated_total.currency} ({len(commerce_req.items)} item(s))"
        },
        {
            "stage": "AUTHORIZATION",
            "name": "Cryptographic Mandate / Proof Check",
            "status": "PASSED" if auth_passed else ("FAILED" if pipeline_result.stage_reached.value == "AUTHORIZATION" else "PENDING"),
            "details": pipeline_result.authorization_result.reason if pipeline_result.authorization_result else "Authorized"
        },
        {
            "stage": "REPLAY",
            "name": "Anti-Replay & Nonce Defense",
            "status": "PASSED" if replay_passed else ("FAILED" if pipeline_result.stage_reached.value == "REPLAY" else "PENDING"),
            "details": pipeline_result.replay_result.reason if pipeline_result.replay_result else "Nonce valid & atomically reserved"
        },
        {
            "stage": "POLICY",
            "name": "Merchant Policy Engine",
            "status": "PASSED" if policy_passed else ("FAILED" if pipeline_result.stage_reached.value == "POLICY" else ("REVIEW" if pipeline_result.decision.value == "REVIEW" else "PENDING")),
            "details": pipeline_result.policy_result.primary_reason if pipeline_result.policy_result else "Policy limits evaluated"
        },
        {
            "stage": "DECISION",
            "name": "Gateway Final Decision",
            "status": pipeline_result.decision.value,
            "decision": pipeline_result.decision.value,
            "reason": pipeline_result.reason,
            "details": f"Decision: {pipeline_result.decision.value} — {pipeline_result.reason}"
        }
    ]
    
    if pipeline_result.decision.value == "ALLOW":
        # Execute Razorpay ONLY on explicit ALLOW
        secrets = merchant_store.get_merchant_secrets(merchant_id)
        if not config.razorpay_key_id or not secrets or not secrets.razorpay_key_secret:
            reason = "Merchant Razorpay credentials not configured in Gateway Dashboard"
            audit.record(audit_razorpay_execution(
                transaction_id=commerce_req.transaction_id,
                razorpay_order_id=None,
                razorpay_payment_id=None,
                razorpay_payment_status=None,
                decision="BLOCK",
                reason=reason,
                amount_minor=commerce_req.calculated_total.amount_minor,
                currency=commerce_req.calculated_total.currency,
                merchant_id=merchant_id,
                buyer_agent_id=commerce_req.buyer_agent_id,
                protocol=protocol,
            ))
            pipeline_stages.append({
                "stage": "RAZORPAY_ORDER",
                "name": "Razorpay Test Mode Order",
                "status": "FAILED",
                "details": reason
            })
            return {
                "gateway_decision": "BLOCK",
                "final_decision": "BLOCK",
                "protocol": protocol,
                "adapter_used": adapter_name,
                "status": "failed",
                "reason": reason,
                "transaction_id": commerce_req.transaction_id,
                "timestamp": timestamp_iso,
                "raw_payload": raw_payload,
                "canonical_request": canonical_request_data,
                "pipeline_stages": pipeline_stages,
                "razorpay_order_id": None,
                "razorpay_key_id": None,
                "amount_minor": commerce_req.calculated_total.amount_minor,
                "currency": commerce_req.calculated_total.currency,
            }
            
        razorpay_client = RazorpayClient(
            key_id=config.razorpay_key_id, 
            key_secret=secrets.razorpay_key_secret
        )
        
        receipt = execute_razorpay_payment(
            pipeline_result=pipeline_result,
            buyer_agent_id=commerce_req.buyer_agent_id,
            merchant_id=merchant_id,
            amount=commerce_req.calculated_total,
            razorpay_client=razorpay_client,
            originating_protocol=protocol
        )
        
        # Audit Razorpay order execution result
        audit.record(audit_razorpay_execution(
            transaction_id=commerce_req.transaction_id,
            razorpay_order_id=receipt.payment_reference,
            razorpay_payment_id=None,
            razorpay_payment_status=None,
            decision=receipt.decision.value,
            reason=f"Razorpay order creation: {receipt.status}",
            amount_minor=commerce_req.calculated_total.amount_minor,
            currency=commerce_req.calculated_total.currency,
            merchant_id=merchant_id,
            buyer_agent_id=commerce_req.buyer_agent_id,
            protocol=protocol,
        ))

        pipeline_stages.append({
            "stage": "RAZORPAY_ORDER",
            "name": "Razorpay Test Mode Order",
            "status": "PASSED" if receipt.status == "order_created" else "FAILED",
            "order_id": receipt.payment_reference,
            "details": f"Order {receipt.payment_reference} created in Razorpay Sandbox"
        })
        
        # Return minimum checkout data along with stage & canonical metadata (Never expose secrets)
        return {
            "gateway_decision": receipt.decision.value,
            "final_decision": receipt.decision.value,
            "protocol": protocol,
            "adapter_used": adapter_name,
            "transaction_id": receipt.transaction_id,
            "status": receipt.status,
            "timestamp": timestamp_iso,
            "raw_payload": raw_payload,
            "canonical_request": canonical_request_data,
            "pipeline_stages": pipeline_stages,
            "razorpay_order_id": receipt.payment_reference,
            "razorpay_key_id": config.razorpay_key_id,
            "amount_minor": commerce_req.calculated_total.amount_minor,
            "currency": commerce_req.calculated_total.currency,
            "order": {
                "id": receipt.payment_reference,
                "checkout_session_id": receipt.transaction_id
            }
        }
    else:
        # Build a receipt based on the blocked/review decision
        # NEVER invoke Razorpay on BLOCK / REVIEW
        return {
            "gateway_decision": pipeline_result.decision.value,
            "final_decision": pipeline_result.decision.value,
            "protocol": protocol,
            "adapter_used": adapter_name,
            "transaction_id": commerce_req.transaction_id,
            "status": "failed" if pipeline_result.decision.value == "BLOCK" else "pending_review",
            "reason": pipeline_result.reason,
            "timestamp": timestamp_iso,
            "raw_payload": raw_payload,
            "canonical_request": canonical_request_data,
            "pipeline_stages": pipeline_stages,
            "razorpay_order_id": None,
            "razorpay_key_id": None,
            "amount_minor": commerce_req.calculated_total.amount_minor,
            "currency": commerce_req.calculated_total.currency,
        }


@router.post("/payments/verify")
async def verify_payment(
    data: PaymentVerificationInput,
    merchant_id: str = Depends(authenticate_merchant),
    db: Session = Depends(get_db)
):
    """
    Verifies a client-side completed Razorpay payment using server-side HMAC-SHA256
    and captures the payment.
    """
    config = merchant_store.get_merchant(merchant_id)
    secrets = merchant_store.get_merchant_secrets(merchant_id)
    if not config or not secrets or not config.razorpay_key_id or not secrets.razorpay_key_secret:
        raise HTTPException(status_code=400, detail="Merchant Razorpay credentials not configured")
        
    razorpay_client = RazorpayClient(
        key_id=config.razorpay_key_id,
        key_secret=secrets.razorpay_key_secret
    )
    
    result = verify_payment_and_capture(
        razorpay_client=razorpay_client,
        expected_order_id=data.razorpay_order_id,
        razorpay_payment_id=data.razorpay_payment_id,
        razorpay_signature=data.razorpay_signature,
        amount_minor=data.amount_minor,
        currency=data.currency,
        auto_captured=data.auto_captured
    )
    
    audit = AuditLogger()
    audit.record(audit_razorpay_execution(
        transaction_id=f"verify-{data.razorpay_order_id}",
        razorpay_order_id=result.razorpay_order_id,
        razorpay_payment_id=result.razorpay_payment_id,
        razorpay_payment_status=result.razorpay_payment_status,
        decision="ALLOW" if result.is_success() else "BLOCK",
        reason=f"Payment capture & verification: {result.execution_status.value}",
        amount_minor=data.amount_minor,
        currency=data.currency,
        merchant_id=merchant_id,
        protocol="acp",
    ))
    
    return {
        "success": result.is_success(),
        "execution_status": result.execution_status.value,
        "razorpay_order_id": result.razorpay_order_id,
        "razorpay_payment_id": result.razorpay_payment_id,
        "razorpay_payment_status": result.razorpay_payment_status,
        "error_code": result.error_code,
        "error_description": result.error_description
    }
