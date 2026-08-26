"""
ACP Adapter — Agent Commerce Gateway
=====================================

Converts ACP (Agentic Commerce Protocol) checkout session requests into the
gateway's canonical models, and translates canonical receipts back to ACP
response format.

Authoritative source:
    GitHub: agentic-commerce-protocol/agentic-commerce-protocol
    Stable spec: spec/2026-04-17  (RFC version 2026-01-16)
    RFCs used:   rfcs/rfc.agentic_checkout.md (2026-01-16)
                 rfcs/rfc.payment_handlers.md (2026-01-22)

ACP Authorization model (from RFC):
    - The agent sends `Authorization: Bearer <token>` on all requests (REQUIRED).
    - The token is OPAQUE to the gateway; the merchant's backend is responsible
      for verifying it with their identity provider or PSP.
    - The gateway CANNOT cryptographically verify ACP bearer tokens — it only
      checks their presence, format, and that associated claims (buyer id,
      merchant id, allowed amount) are structurally consistent.
    - Payment data is a delegated payment token from `delegate_payment`; its
      validity is verified by the PSP at execution time, not the gateway.

ACP Idempotency:
    - Every POST request carries `Idempotency-Key` (REQUIRED by spec).
    - We map this to our replay idempotency check using TRANSACTION_ID namespace,
      keyed by the idempotency key value (NOT a nonce-consumption model).
    - This means: same Idempotency-Key replayed after a success = BLOCK at replay.

ACP semantics that CANNOT safely be mapped to this gateway:
    1. Bearer token cryptographic verification — ACP bearer tokens are opaque;
       the gateway cannot validate them without an identity provider integration.
       ACPAuthorizationProvider performs presence + structural checks only.
       Full verification requires a token introspection call (not implemented).
    2. ACP does NOT use Ed25519 or any gateway-verifiable signature on the
       bearer token. Do NOT invent one.
    3. Payment delegation (`delegate_payment` / SPT flow) is PSP-side; the
       gateway cannot verify the payment token at this stage.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.adapters.base_adapter import ProtocolAdapter
from app.core.schemas import (
    AuthorizationProof,
    BuyerProtocol,
    CommerceItem,
    CommerceReceipt,
    CommerceRequest,
    Money,
)


# ══════════════════════════════════════════════════════════════════════════════
# ACP raw request model — parsed from incoming dict before canonicalization
# ══════════════════════════════════════════════════════════════════════════════


class ACPLineItem(BaseModel):
    """
    ACP item in the create/complete checkout request body.

    Source: RFC rfc.agentic_checkout.md §5 Data Model
    Fields: id (product identifier), quantity (int ≥ 1), unit_amount (int,
    minor units), currency (ISO 4217), name (display name), category (optional).
    """
    model_config = ConfigDict(extra="allow")

    id: str = Field(..., min_length=1, description="ACP item/product ID")
    quantity: int = Field(..., gt=0, strict=True)
    unit_amount: int = Field(..., ge=0, strict=True, description="Unit price in minor units")
    currency: str = Field(..., min_length=3, max_length=3, description="ISO 4217 currency code")
    name: Optional[str] = Field(default=None)
    category: Optional[str] = Field(default=None)


class ACPBuyer(BaseModel):
    """
    ACP buyer object within a checkout request.

    Source: RFC rfc.agentic_checkout.md §4.1 Create Session
    Required: agent_id (we use this as buyer_agent_id in canonical model).
    """
    model_config = ConfigDict(extra="allow")

    agent_id: str = Field(..., min_length=1, description="Stable buyer agent identifier")
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None


class ACPCheckoutRequest(BaseModel):
    """
    Normalized ACP checkout session request.

    Maps to the POST /checkout_sessions or POST /checkout_sessions/{id}/complete
    body, combined with HTTP headers provided separately.

    Source: RFC rfc.agentic_checkout.md §4.1, §4.4
    """
    model_config = ConfigDict(extra="allow")

    # From request body
    items: list[ACPLineItem] = Field(..., min_length=1)
    buyer: ACPBuyer
    merchant_id: str = Field(..., min_length=1)

    # From HTTP headers (caller must extract and inject)
    idempotency_key: str = Field(..., min_length=1, max_length=255,
                                 description="From Idempotency-Key header (REQUIRED by spec)")
    bearer_token: str = Field(..., min_length=1,
                              description="Opaque bearer token from Authorization header")
    api_version: str = Field(default="2026-01-16",
                             description="From API-Version header")

    # Optional: checkout session id (for complete/update flows)
    checkout_session_id: Optional[str] = Field(default=None)

    # Optional: payment token (on complete)
    payment_token: Optional[str] = Field(default=None)


# ══════════════════════════════════════════════════════════════════════════════
# ACPAuthorizationProof — concrete AuthorizationProof for ACP bearer tokens
# ══════════════════════════════════════════════════════════════════════════════


class ACPAuthorizationProof(AuthorizationProof):
    """
    Authorization proof for ACP requests.

    ACP authorization is based on opaque Bearer tokens.
    The token is OPAQUE — we cannot cryptographically verify it without an
    identity provider (IDP) integration. The gateway performs structural
    presence checks only.

    This proof preserves the native ACP authorization concept rather than
    inventing an Ed25519 mandate or nonce.

    Source: RFC rfc.agentic_checkout.md §3.1 Common Requirements
        "Authorization: Bearer <token> (REQUIRED)"
    """
    model_config = ConfigDict(frozen=True)

    auth_type: str = Field(default="acp_bearer_token")

    # The opaque bearer token from the Authorization header
    bearer_token: str = Field(..., min_length=1,
                              description="Opaque bearer token. Presence required; "
                                          "cryptographic verification requires IDP call.")

    # Buyer agent identifier claimed in the request (not cryptographically bound)
    claimed_buyer_agent_id: str = Field(..., min_length=1)

    # Merchant identifier (from request body)
    claimed_merchant_id: str = Field(..., min_length=1)

    # The idempotency key — drives replay protection for ACP
    idempotency_key: str = Field(..., min_length=1, max_length=255,
                                 description="From Idempotency-Key header. "
                                             "Used as replay key per ACP spec.")

    # The total amount claimed by the request (minor units). Used for
    # structural consistency checks.
    claimed_amount_minor: int = Field(..., ge=0)
    claimed_currency: str = Field(..., min_length=3, max_length=3)

    # ACP API version from the request header
    api_version: str = Field(default="2026-01-16")

    # Optional: payment token present on complete requests
    payment_token: Optional[str] = Field(default=None)


# ══════════════════════════════════════════════════════════════════════════════
# ACPAdapter — parses ACP payloads and builds ACP receipts
# ══════════════════════════════════════════════════════════════════════════════


class ACPAdapter(ProtocolAdapter):
    """
    Protocol adapter for ACP (Agentic Commerce Protocol).

    Converts ACP checkout session payloads into canonical CommerceRequest
    objects and ACPAuthorizationProof objects.

    Translates canonical CommerceReceipt back into ACP-format responses.

    Source: agentic-commerce-protocol/agentic-commerce-protocol @ spec/2026-04-17
    RFC:    rfcs/rfc.agentic_checkout.md (version 2026-01-16)
    """

    # Supported ACP API versions
    SUPPORTED_API_VERSIONS = {"2026-01-16", "2026-04-17"}

    def parse_request(self, payload: dict) -> CommerceRequest:
        """
        Parse an ACP checkout payload dict into a canonical CommerceRequest.

        The payload dict must include both body fields AND extracted headers:
            payload = {
                "items": [...],
                "buyer": {"agent_id": "...", ...},
                "merchant_id": "...",
                "idempotency_key": "...",    # from Idempotency-Key header
                "bearer_token": "...",       # from Authorization header (Bearer)
                "api_version": "2026-01-16", # from API-Version header
            }

        Fails closed:
            - Missing required ACP fields → ValueError (BLOCK at adapter boundary)
            - Mismatched currencies across items → ValueError
            - Pydantic ValidationError propagates from CommerceRequest
            - Never returns a partial CommerceRequest

        Args:
            payload: Combined ACP request body + extracted headers as a dict.

        Returns:
            A fully validated CommerceRequest.

        Raises:
            ValueError: For ACP-level semantic errors.
            pydantic.ValidationError: If the resulting CommerceRequest is invalid.
        """
        # --- Step 1: Parse and validate the ACP-specific request structure ---
        try:
            acp_req = ACPCheckoutRequest.model_validate(payload)
        except Exception as exc:
            raise ValueError(f"ACP request structure invalid: {exc}") from exc

        # --- Step 2: API version check (required by spec) ---
        if acp_req.api_version not in self.SUPPORTED_API_VERSIONS:
            raise ValueError(
                f"Unsupported ACP API-Version: '{acp_req.api_version}'. "
                f"Supported versions: {sorted(self.SUPPORTED_API_VERSIONS)}"
            )

        # --- Step 3: Bearer token presence check (fail closed if missing) ---
        token = acp_req.bearer_token.strip()
        if not token:
            raise ValueError(
                "ACP request missing Authorization bearer token. "
                "All ACP requests must include 'Authorization: Bearer <token>'."
            )

        # --- Step 4: Idempotency-Key presence check ---
        idempotency_key = acp_req.idempotency_key.strip()
        if not idempotency_key:
            raise ValueError(
                "ACP request missing Idempotency-Key. "
                "All ACP POST requests must include an Idempotency-Key header."
            )

        # --- Step 5: Map ACP line items → canonical CommerceItems ---
        # Verify currency consistency across items
        currencies = {item.currency.upper() for item in acp_req.items}
        if len(currencies) > 1:
            raise ValueError(
                f"ACP request contains mixed currencies across items: {sorted(currencies)}. "
                f"All items must share a single currency."
            )

        commerce_items: list[CommerceItem] = []
        for acp_item in acp_req.items:
            commerce_items.append(
                CommerceItem(
                    product_id=acp_item.id,
                    name=acp_item.name or acp_item.id,
                    quantity=acp_item.quantity,
                    unit_price=Money(
                        amount_minor=acp_item.unit_amount,
                        currency=acp_item.currency.upper(),
                    ),
                    category=acp_item.category,
                )
            )

        # --- Step 6: Build canonical CommerceRequest ---
        # ACP checkout session IDs serve as our transaction_id when present.
        # For new sessions (no checkout_session_id), we generate a stable
        # transaction ID from the idempotency key to maintain determinism.
        transaction_id = (
            acp_req.checkout_session_id
            if acp_req.checkout_session_id
            else f"acp-{idempotency_key}"
        )

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=30)

        request = CommerceRequest(
            transaction_id=transaction_id,
            created_at=now,
            expires_at=expires_at,
            nonce=idempotency_key,  # ACP Idempotency-Key is our request nonce
            buyer_agent_id=acp_req.buyer.agent_id,
            buyer_agent_name=(
                f"{acp_req.buyer.first_name or ''} {acp_req.buyer.last_name or ''}".strip()
                or None
            ),
            buyer_protocol=BuyerProtocol.acp,
            merchant_id=acp_req.merchant_id,
            items=commerce_items,
            receipt_destination_protocol=BuyerProtocol.acp,
            receipt_destination_ref=idempotency_key,
        )

        return request

    def parse_authorization_proof(self, payload: dict) -> ACPAuthorizationProof:
        """
        Extract and construct an ACPAuthorizationProof from an ACP payload.

        This is separate from parse_request() so callers can pass both the
        CommerceRequest and the proof to the orchestrator independently.

        Fails closed if bearer token or idempotency key are missing.

        Args:
            payload: The same combined dict passed to parse_request().

        Returns:
            ACPAuthorizationProof ready to pass to ACPAuthorizationProvider.

        Raises:
            ValueError: If authorization data is missing or structurally invalid.
        """
        try:
            acp_req = ACPCheckoutRequest.model_validate(payload)
        except Exception as exc:
            raise ValueError(f"ACP request structure invalid: {exc}") from exc

        bearer_token = acp_req.bearer_token.strip()
        if not bearer_token:
            raise ValueError("ACP Authorization bearer token is missing or empty.")

        idempotency_key = acp_req.idempotency_key.strip()
        if not idempotency_key:
            raise ValueError("ACP Idempotency-Key is missing or empty.")

        # Compute the total amount from items for structural consistency checks
        if not acp_req.items:
            raise ValueError("ACP request contains no items; cannot extract amount.")

        currencies = {item.currency.upper() for item in acp_req.items}
        if len(currencies) > 1:
            raise ValueError(
                f"ACP request contains mixed currencies: {sorted(currencies)}"
            )

        total_amount = sum(
            item.unit_amount * item.quantity for item in acp_req.items
        )
        currency = next(iter(currencies))

        return ACPAuthorizationProof(
            bearer_token=bearer_token,
            claimed_buyer_agent_id=acp_req.buyer.agent_id,
            claimed_merchant_id=acp_req.merchant_id,
            idempotency_key=idempotency_key,
            claimed_amount_minor=total_amount,
            claimed_currency=currency,
            api_version=acp_req.api_version,
            payment_token=acp_req.payment_token,
        )

    def build_receipt(self, receipt: CommerceReceipt) -> dict:
        """
        Translate a canonical CommerceReceipt into an ACP-format response dict.

        The ACP Complete Session response includes:
        - status: "completed" | "canceled" | ...
        - order: { id, checkout_session_id, permalink_url }

        Source: RFC rfc.agentic_checkout.md §4.4 Complete Session

        Args:
            receipt: A fully populated CommerceReceipt.

        Returns:
            ACP-format response dict.
        """
        status_map = {
            "completed": "completed",
            "pending": "ready_for_payment",
            "failed": "canceled",
        }
        acp_status = status_map.get(receipt.status, receipt.status)

        response: dict = {
            "id": receipt.transaction_id,
            "status": acp_status,
            "currency": receipt.final_amount.currency.lower(),
            "totals": [
                {
                    "type": "total",
                    "display_text": "Total",
                    "amount": receipt.final_amount.amount_minor,
                }
            ],
            "gateway_decision": receipt.decision.value,
        }

        if receipt.payment_reference:
            response["order"] = {
                "id": receipt.payment_reference,
                "checkout_session_id": receipt.transaction_id,
                "permalink_url": None,  # Not available until Razorpay execution
            }

        return response
