"""
Canonical Commerce Object — Agent Commerce Gateway
===================================================

This module defines the gateway's internal commerce language.

All security, business, and policy decisions happen on these models.
Protocol adapters (ACP, x402, AP2, UAP) translate their native formats
INTO these models. The core never sees protocol-specific concepts.

Pipeline:
    Protocol payload
        ↓
    ProtocolAdapter.parse_request()
        ↓
    CommerceRequest  +  AuthorizationProof
        ↓
    Authorization Verification
        ↓
    Replay / Idempotency
        ↓
    Merchant Policy (app/core/policy.py)
        ↓
    GatewayDecision  (ALLOW / REVIEW / BLOCK)
        ↓
    CommerceReceipt
        ↓
    ProtocolAdapter.build_receipt()

IMPORTANT NOTES ON AUTHORIZATION:
    - Real authorization requires cryptographic signature verification or native token verification, delegated to an AuthorizationProvider.

MONETARY REPRESENTATION:
    All amounts are stored as integer minor units (e.g., paise for INR).
    Floating-point monetary values are never used or stored.
    ₹25.50  →  Money(amount_minor=2550, currency="INR")

IMMUTABILITY:
    All authorization-path models are frozen (model_config frozen=True).
    Mutating critical fields after validation raises TypeError.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ══════════════════════════════════════════════════════════════════════════════
# Enums
# ══════════════════════════════════════════════════════════════════════════════


class BuyerProtocol(str, Enum):
    """
    Known AI-commerce protocols the gateway plans to support.

    These are protocol IDENTIFIERS, not business rules.  Adding a new
    protocol later means adding a value here and writing an adapter —
    the core logic does not change.
    """

    acp = "acp"
    x402 = "x402"
    ap2 = "ap2"
    uap = "uap"


class MandateType(str, Enum):
    """
    The spending scope granted by a mandate.

    one_time:  A single-use authorization for one transaction.
    recurring: Ongoing authorization subject to renewal rules.
                (Recurring payments are not implemented yet.)
    """

    one_time = "one_time"
    recurring = "recurring"


class MandateStatus(str, Enum):
    """
    Reported lifecycle status of a mandate.

    ⚠️  This is metadata carried on the mandate object.
        It is NOT proof of authorization.
        A mandate must be cryptographically verified regardless of status.
        A malicious actor could fabricate a payload with status='active'.
        The future authorization layer (app/core/mandate.py) is the
        source of truth, not this field.
    """

    active = "active"
    expired = "expired"
    revoked = "revoked"


class GatewayDecision(str, Enum):
    """
    Explicit, named transaction outcome states.

    UNDECIDED: Structural validation passed.  Authorization not yet evaluated.
               This is the initial state after CommerceContext validation.
    ALLOW:     Future authorization + policy layers approved the transaction.
    REVIEW:    Future policy layer flagged for human or secondary review.
    BLOCK:     Transaction is rejected.

    Do NOT use exceptions to represent ALLOW, REVIEW, or UNDECIDED.
    Only GatewayBlockedError represents an active BLOCK decision propagated
    as an exception (e.g., to abort a processing pipeline).
    """

    UNDECIDED = "UNDECIDED"
    ALLOW = "ALLOW"
    REVIEW = "REVIEW"
    BLOCK = "BLOCK"


# ══════════════════════════════════════════════════════════════════════════════
# Money — integer minor-unit representation
# ══════════════════════════════════════════════════════════════════════════════


class Money(BaseModel):
    """
    Validated monetary value in integer minor units.

    Rules:
      - amount_minor must be a non-negative integer.
      - Floats are rejected (Pydantic strict int validation).
      - currency is required, must be a 3-character ISO 4217 code.
      - currency is always stored uppercase.

    Examples:
        ₹25.50  →  Money(amount_minor=2550,  currency="INR")
        $10.00  →  Money(amount_minor=1000,  currency="USD")
        ₹0      →  Money(amount_minor=0,     currency="INR")   # zero is valid
    """

    model_config = ConfigDict(frozen=True)

    amount_minor: int = Field(
        ...,
        ge=0,
        strict=True,
        description="Amount in minor currency units (paise, cents, etc.). "
                    "Must be a non-negative integer. Never use floats.",
    )
    currency: str = Field(
        ...,
        min_length=3,
        max_length=3,
        description="ISO 4217 currency code (e.g., 'INR', 'USD').",
    )

    @model_validator(mode="after")
    def _uppercase_currency(self) -> "Money":
        object.__setattr__(self, "currency", self.currency.upper())
        return self


# ══════════════════════════════════════════════════════════════════════════════
# CommerceItem — single line item
# ══════════════════════════════════════════════════════════════════════════════


class CommerceItem(BaseModel):
    """
    A single product or service line within a purchase request.

    Line total is calculated deterministically from quantity × unit_price.
    It is never supplied externally for a single item — only the
    CommerceRequest supplies an optional overall total for compatibility.
    """

    model_config = ConfigDict(frozen=True)

    product_id: str = Field(..., min_length=1, description="Unique product identifier.")
    name: str = Field(..., min_length=1, description="Human-readable product name.")
    quantity: int = Field(
        ...,
        gt=0,
        strict=True,
        description="Quantity ordered. Must be a positive integer (≥ 1).",
    )
    unit_price: Money
    category: Optional[str] = Field(
        default=None,
        description="Optional product category (e.g., 'software', 'subscription').",
    )

    @property
    def line_total(self) -> Money:
        """
        Deterministic line total: quantity × unit_price.amount_minor.

        Returns a new Money object; never mutates self.
        """
        return Money(
            amount_minor=self.quantity * self.unit_price.amount_minor,
            currency=self.unit_price.currency,
        )


# ══════════════════════════════════════════════════════════════════════════════
# CommerceRequest — what the agent wants to buy
# ══════════════════════════════════════════════════════════════════════════════


class CommerceRequest(BaseModel):
    """
    Normalized, protocol-agnostic purchase request.

    Produced by a ProtocolAdapter.parse_request() call.
    Answers the question: "What does the agent want to buy?"

    This object does NOT answer "Is the agent allowed to buy it?"
    That is the responsibility of the authorization layer.

    Receipt destination fields are intentionally abstract — they carry
    enough information for the gateway to route the resulting receipt
    back to the originating protocol/client, without coupling to HTTP
    or any specific transport.
    """

    model_config = ConfigDict(frozen=True)

    # ── Transaction identity ──────────────────────────────────────────────────
    transaction_id: str = Field(..., min_length=1, description="Unique transaction identifier.")
    created_at: datetime = Field(..., description="When this request was created (UTC recommended).")
    expires_at: datetime = Field(..., description="When this request expires.")
    nonce: str = Field(..., min_length=1, description="Replay-protection nonce. Must be unique per request.")

    # ── Buyer identity ────────────────────────────────────────────────────────
    buyer_agent_id: str = Field(..., min_length=1, description="Stable identifier of the buying agent.")
    buyer_agent_name: Optional[str] = Field(default=None, description="Optional human-readable agent name.")
    buyer_protocol: BuyerProtocol = Field(..., description="Protocol the buyer used to submit this request.")

    # ── Merchant ─────────────────────────────────────────────────────────────
    merchant_id: str = Field(..., min_length=1, description="Gateway merchant identifier. No Razorpay credentials here.")

    # ── Items ─────────────────────────────────────────────────────────────────
    items: List[CommerceItem] = Field(..., min_length=1, description="One or more line items.")

    # ── Optional compatibility total ──────────────────────────────────────────
    supplied_total: Optional[Money] = Field(
        default=None,
        description="Optional total supplied by the protocol for compatibility. "
                    "If present, must exactly match the calculated total. "
                    "Never used as the authoritative total — calculated_total is.",
    )

    # ── Receipt destination ───────────────────────────────────────────────────
    receipt_destination_protocol: BuyerProtocol = Field(
        ...,
        description="Protocol to use when delivering the receipt.",
    )
    receipt_destination_ref: str = Field(
        ...,
        min_length=1,
        description="Opaque protocol-specific reference for receipt delivery "
                    "(e.g., callback URL, queue ID, session token). "
                    "Not coupled to HTTP or any specific transport.",
    )

    # ── Validators ────────────────────────────────────────────────────────────

    @model_validator(mode="after")
    def _check_expires_after_created(self) -> "CommerceRequest":
        if self.expires_at <= self.created_at:
            raise ValueError(
                f"expires_at ({self.expires_at.isoformat()}) must be strictly "
                f"after created_at ({self.created_at.isoformat()})"
            )
        return self

    @model_validator(mode="after")
    def _check_item_currencies_consistent(self) -> "CommerceRequest":
        """All items must share a single currency."""
        currencies = {item.unit_price.currency for item in self.items}
        if len(currencies) > 1:
            raise ValueError(
                f"All items must share the same currency. "
                f"Found multiple currencies: {sorted(currencies)}"
            )
        return self

    @model_validator(mode="after")
    def _check_supplied_total_matches_calculated(self) -> "CommerceRequest":
        """
        If a protocol supplies a total for compatibility, it must match exactly.

        This prevents a protocol from supplying a lower total to mask the
        real purchase amount.
        """
        if self.supplied_total is not None:
            calc = self.calculated_total
            if self.supplied_total.currency != calc.currency:
                raise ValueError(
                    f"supplied_total currency '{self.supplied_total.currency}' "
                    f"does not match item currency '{calc.currency}'"
                )
            if self.supplied_total.amount_minor != calc.amount_minor:
                raise ValueError(
                    f"supplied_total ({self.supplied_total.amount_minor} minor units) "
                    f"does not match calculated total ({calc.amount_minor} minor units). "
                    f"Supplying an inconsistent total is not permitted."
                )
        return self

    @property
    def calculated_total(self) -> Money:
        """
        Authoritative transaction total, computed from items.

        This is ALWAYS computed from items. A supplied_total is validated
        against this value, never used in its place.
        """
        total_minor = sum(item.line_total.amount_minor for item in self.items)
        currency = self.items[0].unit_price.currency
        return Money(amount_minor=total_minor, currency=currency)


# ══════════════════════════════════════════════════════════════════════════════
# Mandate — authorization object
# ══════════════════════════════════════════════════════════════════════════════


class Mandate(BaseModel):
    """
    Authorization object representing what a buyer agent is permitted to spend.

    ⚠️  CRITICAL:
        The existence of a Mandate object does NOT constitute proof of
        authorization. The `status` field is METADATA ONLY.
        A mandate claiming status='active' has not been verified.

        Real authorization requires:
          1. Signature verification (Ed25519) — future app/core/mandate.py
          2. Expiry check against current wall-clock time — future layer
          3. Revocation check — future layer

        This object is frozen to prevent casual mutation. If authorization
        fields (max_amount, merchant_id, buyer_agent_id, nonce, etc.) are
        changed after validation, the signature would no longer match —
        and the frozen model prevents that mutation entirely.

    mandate_type='recurring' is defined here for structural completeness.
    Recurring payment execution is NOT implemented.
    """

    model_config = ConfigDict(frozen=True)

    # ── Identity ──────────────────────────────────────────────────────────────
    mandate_id: str = Field(..., min_length=1, description="Unique mandate identifier.")
    buyer_agent_id: str = Field(..., min_length=1, description="Buyer agent this mandate covers.")
    merchant_id: str = Field(..., min_length=1, description="Merchant this mandate applies to.")

    # ── Authorization scope ───────────────────────────────────────────────────
    max_amount: Money = Field(..., description="Maximum amount authorized per transaction.")
    mandate_type: MandateType

    # ── Status (metadata only — NOT proof of authorization) ───────────────────
    status: MandateStatus = Field(
        ...,
        description="Reported lifecycle status. "
                    "NOT proof of authorization. "
                    "Treat as untrusted metadata until cryptographically verified.",
    )

    # ── Timestamps ────────────────────────────────────────────────────────────
    issued_at: datetime = Field(..., description="When this mandate was issued.")
    expires_at: datetime = Field(..., description="When this mandate expires.")

    # ── Replay protection ─────────────────────────────────────────────────────
    nonce: str = Field(..., min_length=1, description="Unique nonce for replay protection.")

    # ── Authorization credential reference ────────────────────────────────────
    authorization_method: str = Field(
        ...,
        min_length=1,
        description="Authorization method identifier (e.g., 'ed25519', 'hmac-sha256').",
    )
    authorization_ref: str = Field(
        ...,
        min_length=1,
        description="Opaque reference to the authorization credential "
                    "(e.g., key ID, token ID). Not the credential itself.",
    )

    # ── Validators ────────────────────────────────────────────────────────────

    @model_validator(mode="after")
    def _check_expiry_after_issue(self) -> "Mandate":
        if self.expires_at <= self.issued_at:
            raise ValueError(
                f"Mandate expires_at ({self.expires_at.isoformat()}) must be "
                f"strictly after issued_at ({self.issued_at.isoformat()})"
            )
        return self


# ══════════════════════════════════════════════════════════════════════════════
# Authorization Abstractions
# ══════════════════════════════════════════════════════════════════════════════


class AuthorizationProof(BaseModel, ABC):
    """
    Abstract base class for all protocol-specific authorization proofs.
    """
    auth_type: str


class ReplayNamespace(str, Enum):
    AUTHORIZATION_NONCE = "authorization_nonce"
    TRANSACTION_ID = "transaction_id"


class AuthorizationVerificationResult(BaseModel):
    """
    Generic result from any authorization provider.
    """
    valid: bool
    reason: str
    
    # Generic credential identity for replay protection
    requires_replay_check: bool = False
    replay_namespace: Optional[ReplayNamespace] = None
    replay_key: Optional[str] = None
    
    # Scoped authorization concepts for policy evaluation
    is_recurring: bool = False
    
    # Provider-specific details can be attached here if needed
    provider_details: Optional[dict] = None


class AuthorizationProvider(ABC):
    """
    Abstract interface for verifying authorization proofs.
    """
    @abstractmethod
    def verify(self, request: CommerceRequest, proof: AuthorizationProof) -> AuthorizationVerificationResult:
        ...


# ══════════════════════════════════════════════════════════════════════════════
# Ed25519 Authorization (Concrete Provider Models)
# ══════════════════════════════════════════════════════════════════════════════


class Ed25519AuthorizationProof(AuthorizationProof):
    """
    Envelope pairing a Mandate with its digital signature.

    Implementation of the generic AuthorizationProof for Ed25519 mandates.
    """
    model_config = ConfigDict(frozen=True)

    auth_type: str = Field(default="ed25519_mandate")
    payload: Mandate = Field(..., description="The mandate being signed.")
    signature: bytes = Field(
        ...,
        min_length=1,
        description="Raw signature bytes.",
    )
    key_id: str = Field(
        ...,
        min_length=1,
        description="Identifier of the signing key used to produce the signature.",
    )
    algorithm: str = Field(
        default="Ed25519",
        description="Signature algorithm identifier.",
    )

    def canonical_payload_bytes(self) -> bytes:
        """
        Deterministic canonical serialization of the mandate's critical fields.
        """
        critical_fields = {
            "authorization_method": self.payload.authorization_method,
            "authorization_ref": self.payload.authorization_ref,
            "buyer_agent_id": self.payload.buyer_agent_id,
            "currency": self.payload.max_amount.currency,
            "expires_at": self.payload.expires_at.isoformat(),
            "issued_at": self.payload.issued_at.isoformat(),
            "mandate_id": self.payload.mandate_id,
            "mandate_type": self.payload.mandate_type.value,
            "max_amount_minor": self.payload.max_amount.amount_minor,
            "merchant_id": self.payload.merchant_id,
            "nonce": self.payload.nonce,
            "status": self.payload.status.value,
        }
        return json.dumps(critical_fields, sort_keys=True, separators=(",", ":")).encode("utf-8")


# ══════════════════════════════════════════════════════════════════════════════
# CommerceContext — paired request + context
# ══════════════════════════════════════════════════════════════════════════════


class CommerceContext(BaseModel):
    """
    Pairs a CommerceRequest with an AuthorizationProof.
    
    Cross-validation (e.g. amount limits, buyer match) is now handled by the
    AuthorizationProvider during the verification phase.
    """

    model_config = ConfigDict(frozen=True)

    request: CommerceRequest
    auth_proof: AuthorizationProof
    is_recurring: bool = Field(default=False, description="Whether this context is for a recurring transaction.")
    decision: GatewayDecision = Field(
        default=GatewayDecision.UNDECIDED,
        description="Transaction decision state. "
                    "Always UNDECIDED after schema validation. "
                    "Only future authorization/policy layers may produce "
                    "ALLOW, REVIEW, or BLOCK.",
    )


# ══════════════════════════════════════════════════════════════════════════════
# CommerceReceipt — canonical outcome after gateway processing
# ══════════════════════════════════════════════════════════════════════════════


class CommerceReceipt(BaseModel):
    """
    Canonical receipt produced after the gateway processes a transaction.

    Protocol-agnostic.  ProtocolAdapter.build_receipt() translates this
    into the protocol-specific receipt format (ACP, x402, etc.).

    payment_reference is optional at creation — it is populated once a
    real payment processor (Razorpay) returns an order/payment ID.
    That integration is implemented in a future prompt.
    """

    model_config = ConfigDict(frozen=True)

    transaction_id: str = Field(..., min_length=1)
    merchant_id: str = Field(..., min_length=1)
    buyer_agent_id: str = Field(..., min_length=1)
    final_amount: Money = Field(..., description="Amount actually processed (or authorized).")
    payment_reference: Optional[str] = Field(
        default=None,
        description="Payment processor reference (e.g., Razorpay order ID). "
                    "None until payment execution is implemented.",
    )
    status: str = Field(
        ...,
        min_length=1,
        description="Receipt status string (e.g., 'completed', 'pending', 'failed').",
    )
    timestamp: datetime = Field(..., description="When this receipt was produced.")
    originating_protocol: BuyerProtocol = Field(
        ...,
        description="Protocol that originated the request. "
                    "Used by the adapter layer to route the receipt correctly.",
    )
    decision: GatewayDecision = Field(
        ...,
        description="The gateway's decision that produced this receipt.",
    )


# ══════════════════════════════════════════════════════════════════════════════
# GatewayBlockedError — exception for active BLOCK decisions in pipelines
# ══════════════════════════════════════════════════════════════════════════════


class GatewayBlockedError(Exception):
    """
    Raised when the gateway makes an explicit BLOCK decision and needs to
    abort the current processing pipeline.

    Usage:
        Use this exception to propagate a block decision through call stacks
        that do not return GatewayDecision values directly.

    Do NOT use this exception for:
        - Normal validation failures (raise ValueError / ValidationError).
        - ALLOW or REVIEW outcomes (use GatewayDecision enum).
        - UNDECIDED state (that is normal after schema validation).
        - Every business rule check — exceptions are not control flow here.

    GatewayBlockedError always carries GatewayDecision.BLOCK.
    It cannot be constructed with any other decision state.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.decision: GatewayDecision = GatewayDecision.BLOCK
        super().__init__(f"[{self.decision.value}] {reason}")

    def __repr__(self) -> str:
        return f"GatewayBlockedError(reason={self.reason!r})"
