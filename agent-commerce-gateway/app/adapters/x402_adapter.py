"""
x402 Adapter — Agent Commerce Gateway
======================================

Converts authoritative x402 v2 PaymentPayload requests (as well as legacy v1
payment proof headers) into the gateway's canonical CommerceRequest and
X402AuthorizationProof models, and translates canonical receipts back to
x402 format.

Authoritative source:
    Specification: x402 v2 Specification (RFC HTTP 402 Payment Required)
    Schema:        PaymentPayload (x402Version: 2, resource, accepted, payload, extensions)
    Networks:      EVM (eip155:84532 Base Sepolia, eip155:8453 Base), Solana, Polygon, Avalanche
    Schemes:       "exact" (exact fixed-amount authorization)

Fail-Closed Boundaries:
    - x402Version != 2 (or unsupported legacy version) -> raises ValueError
    - Missing required buyer identity (`buyer_agent_id` or `from` address) -> raises ValueError
    - Missing required merchant identity (`merchant_id`) -> raises ValueError
    - Missing or malformed `resource`, `accepted`, or `payload` -> raises ValueError
    - Unsupported scheme, network, or asset -> raises ValueError
    - Mismatch between line items total and accepted amount -> raises ValueError
    - Actual settlement verification is explicitly delegated to an X402PaymentVerifier.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
# x402 v2 Pydantic Schemas
# ══════════════════════════════════════════════════════════════════════════════


class X402LineItem(BaseModel):
    """Line item carried in x402 v2 accepted.extra metadata."""
    model_config = ConfigDict(extra="allow")

    id: str = Field(..., min_length=1, description="Item product ID")
    name: Optional[str] = Field(default=None, description="Product display name")
    quantity: int = Field(..., gt=0, description="Item quantity")
    unit_amount: int = Field(..., ge=0, description="Unit price in minor currency units")
    currency: Optional[str] = Field(default="INR", min_length=3, max_length=5)
    category: Optional[str] = None


class X402AcceptedExtra(BaseModel):
    """Extra metadata inside x402 accepted requirements."""
    model_config = ConfigDict(extra="allow")

    merchant_id: Optional[str] = None
    buyer_agent_id: Optional[str] = None
    currency: Optional[str] = "INR"
    items: Optional[List[X402LineItem]] = None


class X402Accepted(BaseModel):
    """Accepted payment terms per x402 v2 specification."""
    model_config = ConfigDict(extra="allow")

    scheme: str = Field(..., min_length=1, description="e.g. 'exact'")
    network: str = Field(..., min_length=1, description="CAIP-2 or network identifier, e.g. 'eip155:84532'")
    asset: str = Field(..., min_length=1, description="Token address or asset code, e.g. 'USDC'")
    amount: str = Field(..., min_length=1, description="Atomic amount as integer string")
    payTo: str = Field(..., min_length=1, description="Merchant recipient address")
    maxTimeoutSeconds: Optional[int] = 300
    extra: Optional[Union[X402AcceptedExtra, Dict[str, Any]]] = None


class X402Authorization(BaseModel):
    """Authorization block in x402 v2 payload."""
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    from_address: Optional[str] = Field(default=None, alias="from")
    to_address: Optional[str] = Field(default=None, alias="to")
    value: Optional[str] = None
    validAfter: Optional[int] = 0
    validBefore: Optional[int] = None
    nonce: Optional[str] = None


class X402PayloadData(BaseModel):
    """Client authorization and proof in x402 v2."""
    model_config = ConfigDict(extra="allow")

    authorization: Optional[Union[X402Authorization, Dict[str, Any]]] = None
    signature: Optional[str] = None
    transaction: Optional[str] = None


class X402Resource(BaseModel):
    """Protected resource descriptor in x402 v2."""
    model_config = ConfigDict(extra="allow")

    url: str = Field(..., min_length=1)
    description: Optional[str] = None
    mimeType: Optional[str] = None


class X402V2PaymentPayload(BaseModel):
    """
    Authoritative x402 v2 PaymentPayload schema.
    """
    model_config = ConfigDict(extra="allow")

    x402Version: int = Field(default=2, description="Protocol version, must be 2")
    resource: X402Resource
    accepted: X402Accepted
    payload: X402PayloadData
    extensions: Optional[Dict[str, Any]] = Field(default_factory=dict)


# ══════════════════════════════════════════════════════════════════════════════
# Legacy v1 Request Model (for backwards compatibility with existing test cases)
# ══════════════════════════════════════════════════════════════════════════════


class X402PaymentProofSchema(BaseModel):
    """Schema for parsed legacy X-Payment header JSON."""
    model_config = ConfigDict(extra="ignore")

    hash: str = Field(..., min_length=1)
    amount: str = Field(..., min_length=1)
    network: str = Field(..., min_length=1)
    token: str = Field(..., min_length=1)


class X402InputPayload(BaseModel):
    """Legacy v1 input payload schema."""
    model_config = ConfigDict(extra="ignore")

    buyer_agent_id: str = Field(..., min_length=1)
    merchant_id: str = Field(..., min_length=1)
    items: list[CommerceItem] = Field(..., min_length=1)
    x_payment: Union[str, dict] = Field(...)


# ══════════════════════════════════════════════════════════════════════════════
# X402AuthorizationProof — concrete AuthorizationProof for x402
# ══════════════════════════════════════════════════════════════════════════════


class X402AuthorizationProof(AuthorizationProof):
    """
    Authorization proof for x402 requests.

    Carries structural authorization parameters from the x402 PaymentPayload.
    Unverified until an X402PaymentVerifier validates the cryptographic signature
    or settlement proof.
    """
    model_config = ConfigDict(frozen=True)

    auth_type: str = Field(default="x402_payment_proof")
    x402_version: int = Field(default=2)
    scheme: str = Field(default="exact")
    network: str = Field(..., min_length=1)
    token: str = Field(..., min_length=1)
    claimed_amount_minor: int = Field(..., ge=0)
    pay_to: Optional[str] = None
    from_address: Optional[str] = None
    nonce: str = Field(..., min_length=1)
    signature: Optional[str] = None
    tx_hash: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════════════
# X402Adapter
# ══════════════════════════════════════════════════════════════════════════════


class X402Adapter(ProtocolAdapter):
    """
    Protocol adapter for x402 v2 (and legacy v1).

    Converts x402 payloads into canonical CommerceRequest and X402AuthorizationProof
    models with strict schema validation and fail-closed security.
    """

    SUPPORTED_SCHEMES = {"exact"}
    SUPPORTED_NETWORKS = {
        "eip155:84532", "eip155:8453", "base-sepolia", "base",
        "solana", "polygon", "avalanche"
    }
    SUPPORTED_ASSETS = {
        "0x036cbd53842c5426634e7929541ec2318f3dcf7e", "usdc", "inr", "usd"
    }

    def _parse_legacy_payment_header(self, x_payment: Union[str, dict]) -> X402PaymentProofSchema:
        """Parse legacy X-Payment header."""
        if isinstance(x_payment, str):
            try:
                parsed_json = json.loads(x_payment)
            except json.JSONDecodeError as exc:
                raise ValueError("X-Payment header is not valid JSON") from exc
        else:
            parsed_json = x_payment

        try:
            return X402PaymentProofSchema.model_validate(parsed_json)
        except ValidationError as exc:
            raise ValueError(f"X-Payment structural validation failed: {exc}") from exc

    def parse_request(self, payload: dict) -> CommerceRequest:
        """
        Parse an x402 payload dict into a canonical CommerceRequest.
        Supports both authoritative x402 v2 PaymentPayload and legacy v1 formats.
        """
        if not payload or not isinstance(payload, dict):
            raise ValueError("x402 payload must be a non-empty dictionary")

        # ── 1. Authoritative x402 v2 PaymentPayload Flow ──────────────────────
        if "x402Version" in payload or "accepted" in payload:
            try:
                v2_data = X402V2PaymentPayload.model_validate(payload)
            except ValidationError as exc:
                raise ValueError(f"x402 v2 PaymentPayload validation failed: {exc}") from exc

            if v2_data.x402Version != 2:
                raise ValueError(f"Unsupported x402 version: {v2_data.x402Version}. Gateway requires x402 v2.")

            # Validate Scheme
            scheme = v2_data.accepted.scheme.lower()
            if scheme not in self.SUPPORTED_SCHEMES:
                raise ValueError(f"Unsupported x402 scheme: '{v2_data.accepted.scheme}'. Supported: {self.SUPPORTED_SCHEMES}")

            # Validate Network
            network = v2_data.accepted.network.lower()
            if network not in self.SUPPORTED_NETWORKS:
                raise ValueError(f"Unsupported x402 network: '{v2_data.accepted.network}'. Supported: {self.SUPPORTED_NETWORKS}")

            # Validate Asset
            asset = v2_data.accepted.asset.lower()
            if asset not in self.SUPPORTED_ASSETS and not asset.startswith("0x"):
                raise ValueError(f"Unsupported x402 asset: '{v2_data.accepted.asset}'.")

            # Parse amount
            try:
                total_amount_minor = int(v2_data.accepted.amount)
            except ValueError as exc:
                raise ValueError(f"x402 accepted.amount must be an integer string: '{v2_data.accepted.amount}'") from exc

            if total_amount_minor < 0:
                raise ValueError("x402 amount cannot be negative")

            # Extract Extra Metadata
            extra = v2_data.accepted.extra
            extra_dict = extra.model_dump() if isinstance(extra, BaseModel) else (extra if isinstance(extra, dict) else {})

            # Extract Buyer Identity (fail closed if missing)
            auth_data = v2_data.payload.authorization
            from_addr = None
            if isinstance(auth_data, X402Authorization):
                from_addr = auth_data.from_address
            elif isinstance(auth_data, dict):
                from_addr = auth_data.get("from") or auth_data.get("from_address")

            buyer_agent_id = (
                extra_dict.get("buyer_agent_id")
                or payload.get("buyer_agent_id")
                or from_addr
            )
            if not buyer_agent_id:
                raise ValueError("Missing required buyer identity: buyer_agent_id or from address required")

            # Extract Merchant Identity (fail closed if missing)
            merchant_id = (
                extra_dict.get("merchant_id")
                or payload.get("merchant_id")
            )
            if not merchant_id:
                raise ValueError("Missing required merchant_id in x402 payload")

            currency = (extra_dict.get("currency") or "INR").upper()

            # Parse Items
            raw_items = extra_dict.get("items")
            items: List[CommerceItem] = []

            if raw_items and len(raw_items) > 0:
                for idx, raw_it in enumerate(raw_items):
                    if isinstance(raw_it, dict):
                        item_id = raw_it.get("id") or f"prod_{idx+1}"
                        item_name = raw_it.get("name") or item_id
                        qty = int(raw_it.get("quantity") or 1)
                        unit_amount = int(raw_it.get("unit_amount") or 0)
                        item_curr = (raw_it.get("currency") or currency).upper()
                        cat = raw_it.get("category")
                    else:
                        item_id = getattr(raw_it, "id", f"prod_{idx+1}")
                        item_name = getattr(raw_it, "name", item_id)
                        qty = int(getattr(raw_it, "quantity", 1))
                        unit_amount = int(getattr(raw_it, "unit_amount", 0))
                        item_curr = (getattr(raw_it, "currency", currency) or currency).upper()
                        cat = getattr(raw_it, "category", None)

                    items.append(CommerceItem(
                        product_id=item_id,
                        name=item_name,
                        quantity=qty,
                        unit_price=Money(amount_minor=unit_amount, currency=item_curr),
                        category=cat,
                    ))

                # Verify items sum against accepted amount
                calculated_items_total = sum(i.quantity * i.unit_price.amount_minor for i in items)
                if calculated_items_total != total_amount_minor:
                    raise ValueError(
                        f"Line items total ({calculated_items_total}) does not match accepted amount ({total_amount_minor})"
                    )
            else:
                # Single resource item fallback
                items.append(CommerceItem(
                    product_id="x402_resource",
                    name=v2_data.resource.description or "x402 Protected Resource",
                    quantity=1,
                    unit_price=Money(amount_minor=total_amount_minor, currency=currency),
                    category="Resource",
                ))

            # Nonce & Transaction ID
            auth_nonce = None
            if isinstance(auth_data, X402Authorization):
                auth_nonce = auth_data.nonce
            elif isinstance(auth_data, dict):
                auth_nonce = auth_data.get("nonce")

            nonce = auth_nonce or v2_data.payload.signature or f"x402_nonce_{int(datetime.now(timezone.utc).timestamp())}"
            transaction_id = f"x402-{nonce}"

            now = datetime.now(timezone.utc)
            max_timeout = v2_data.accepted.maxTimeoutSeconds or 300
            expires_at = now + timedelta(seconds=max_timeout)

            return CommerceRequest(
                transaction_id=transaction_id,
                created_at=now,
                expires_at=expires_at,
                nonce=nonce,
                buyer_agent_id=str(buyer_agent_id),
                buyer_agent_name=None,
                buyer_protocol=BuyerProtocol.x402,
                merchant_id=str(merchant_id),
                items=items,
                receipt_destination_protocol=BuyerProtocol.x402,
                receipt_destination_ref=v2_data.resource.url,
            )

        # ── 2. Legacy v1 Payload Flow ─────────────────────────────────────────
        try:
            input_data = X402InputPayload.model_validate(payload)
        except ValidationError as exc:
            raise ValueError(f"x402 input payload invalid or missing required identity: {exc}") from exc

        payment_data = self._parse_legacy_payment_header(input_data.x_payment)
        transaction_id = f"x402-{payment_data.hash}"
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=15)

        return CommerceRequest(
            transaction_id=transaction_id,
            created_at=now,
            expires_at=expires_at,
            nonce=payment_data.hash,
            buyer_agent_id=input_data.buyer_agent_id,
            buyer_agent_name=None,
            buyer_protocol=BuyerProtocol.x402,
            merchant_id=input_data.merchant_id,
            items=input_data.items,
            receipt_destination_protocol=BuyerProtocol.x402,
            receipt_destination_ref=payment_data.hash,
        )

    def parse_authorization_proof(self, payload: dict) -> X402AuthorizationProof:
        """
        Extract and construct an X402AuthorizationProof from an x402 payload.
        """
        if not payload or not isinstance(payload, dict):
            raise ValueError("x402 payload must be a non-empty dictionary")

        # ── 1. Authoritative x402 v2 Flow ─────────────────────────────────────
        if "x402Version" in payload or "accepted" in payload:
            try:
                v2_data = X402V2PaymentPayload.model_validate(payload)
            except ValidationError as exc:
                raise ValueError(f"x402 v2 PaymentPayload validation failed: {exc}") from exc

            try:
                amount_minor = int(v2_data.accepted.amount)
            except ValueError as exc:
                raise ValueError(f"x402 amount must be an integer string: '{v2_data.accepted.amount}'") from exc

            if amount_minor < 0:
                raise ValueError("x402 amount cannot be negative")

            auth_data = v2_data.payload.authorization
            from_addr = None
            auth_nonce = None
            if isinstance(auth_data, X402Authorization):
                from_addr = auth_data.from_address
                auth_nonce = auth_data.nonce
            elif isinstance(auth_data, dict):
                from_addr = auth_data.get("from") or auth_data.get("from_address")
                auth_nonce = auth_data.get("nonce")

            nonce = auth_nonce or v2_data.payload.signature or "x402_proof_nonce"

            return X402AuthorizationProof(
                auth_type="x402_payment_proof",
                x402_version=v2_data.x402Version,
                scheme=v2_data.accepted.scheme.lower(),
                network=v2_data.accepted.network.lower(),
                token=v2_data.accepted.asset,
                claimed_amount_minor=amount_minor,
                pay_to=v2_data.accepted.payTo,
                from_address=from_addr,
                nonce=nonce,
                signature=v2_data.payload.signature,
                tx_hash=v2_data.payload.transaction or nonce,
            )

        # ── 2. Legacy v1 Flow ─────────────────────────────────────────────────
        try:
            input_data = X402InputPayload.model_validate(payload)
        except ValidationError as exc:
            raise ValueError(f"x402 input payload invalid: {exc}") from exc

        payment_data = self._parse_legacy_payment_header(input_data.x_payment)

        try:
            amount_minor = int(payment_data.amount)
        except ValueError as exc:
            raise ValueError(f"x402 amount must be an integer string: {payment_data.amount}") from exc

        if amount_minor < 0:
            raise ValueError("x402 amount cannot be negative")

        return X402AuthorizationProof(
            auth_type="x402_payment_proof",
            x402_version=1,
            scheme="exact",
            network=payment_data.network.lower(),
            token=payment_data.token,
            claimed_amount_minor=amount_minor,
            nonce=payment_data.hash,
            tx_hash=payment_data.hash,
        )

    def build_receipt(self, receipt: CommerceReceipt) -> dict:
        """
        Translate a canonical CommerceReceipt into an x402-format response dict.
        """
        if receipt.status == "completed" or receipt.decision.value == "ALLOW":
            return {
                "status": "success",
                "message": "Payment verified and resource granted",
                "transaction_id": receipt.transaction_id,
            }

        return {
            "status": "payment_required",
            "message": f"Payment invalid or incomplete: {receipt.status}",
            "amount": str(receipt.final_amount.amount_minor),
            "currency": receipt.final_amount.currency,
        }
