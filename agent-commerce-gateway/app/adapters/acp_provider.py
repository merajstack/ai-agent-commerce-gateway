"""
ACP Authorization Provider — Agent Commerce Gateway
=====================================================

Implements AuthorizationProvider for ACP bearer-token authorization.

ACP Authorization Model (from verified spec):
    Source: rfcs/rfc.agentic_checkout.md §3.1 (version 2026-01-16)
    "Authorization: Bearer <token> (REQUIRED)"

    Bearer tokens in ACP are OPAQUE to the gateway. The merchant's PSP or
    identity provider is the authority for token verification. The gateway:

    1. Verifies the bearer token is PRESENT and non-empty.
    2. Verifies the ACP Idempotency-Key is PRESENT.
    3. Verifies structural consistency: buyer_agent_id and merchant_id in the
       proof must match those in the CommerceRequest (cross-check).
    4. Verifies the claimed amount is ≥ the request's computed total (the
       token should cover at least the requested amount).

    It does NOT:
    - Cryptographically verify the bearer token (requires IDP call).
    - Verify payment token validity (PSP-side, not gateway-side).
    - Consume or invalidate the token (PSP lifecycle).

ACP Replay / Idempotency:
    ACP specifies Idempotency-Key on every POST (UUID v4 recommended, max 255 chars).
    We treat this as a TRANSACTION_ID namespace replay key — one unique outcome
    per idempotency key. A repeated idempotency key after a first success →
    BLOCK at replay stage. This is safer than allowing duplicate processing.

    Unlike Ed25519 one-time mandates, this is idempotency, not credential
    consumption. The orchestrator's ReplayNamespace.TRANSACTION_ID handles this.

Security boundaries:
    - This provider CANNOT guarantee the bearer token is authentic without an
      IDP integration. This is explicitly documented and is a known limitation
      of ACP's opaque token model at this stage of the gateway.
    - Full ACP bearer token verification requires a future IDP/introspection
      integration. The provider is designed to support this upgrade without
      modifying the orchestrator or policy engine.
"""

from __future__ import annotations

from app.adapters.acp_adapter import ACPAuthorizationProof
from app.core.schemas import (
    AuthorizationProof,
    AuthorizationProvider,
    AuthorizationVerificationResult,
    CommerceRequest,
    ReplayNamespace,
)


class ACPAuthorizationProvider(AuthorizationProvider):
    """
    Authorization provider for ACP bearer-token requests.

    Performs structural and presence checks on the ACP authorization proof.
    Cryptographic bearer token verification requires an IDP integration
    (not implemented; documented limitation).

    Fail-closed contract:
        - Missing bearer token → invalid
        - Missing idempotency key → invalid
        - Buyer identity mismatch between proof and request → invalid
        - Merchant identity mismatch → invalid
        - Claimed amount < request total → invalid
        - Currency mismatch → invalid
        - Wrong proof type → invalid
        - Any unexpected exception → invalid
    """

    def verify(
        self,
        request: CommerceRequest,
        proof: AuthorizationProof,
    ) -> AuthorizationVerificationResult:
        """
        Verify an ACPAuthorizationProof against the given CommerceRequest.

        Args:
            request: The canonical CommerceRequest.
            proof:   The ACPAuthorizationProof from the adapter.

        Returns:
            AuthorizationVerificationResult — always; never raises.
        """
        try:
            # ── Type check ────────────────────────────────────────────────────
            if not isinstance(proof, ACPAuthorizationProof):
                return AuthorizationVerificationResult(
                    valid=False,
                    reason=(
                        f"Invalid proof type for ACPAuthorizationProvider: "
                        f"expected ACPAuthorizationProof, got {type(proof).__name__}."
                    ),
                )

            # ── Bearer token presence check ───────────────────────────────────
            # ACP spec §3.1: "Authorization: Bearer <token> (REQUIRED)"
            if not proof.bearer_token or not proof.bearer_token.strip():
                return AuthorizationVerificationResult(
                    valid=False,
                    reason=(
                        "ACP authorization failed: bearer token is missing or empty. "
                        "All ACP requests must include 'Authorization: Bearer <token>'."
                    ),
                )

            # ── Idempotency-Key presence check ────────────────────────────────
            # ACP spec §3.1: "Idempotency-Key (REQUIRED on all POST requests)"
            if not proof.idempotency_key or not proof.idempotency_key.strip():
                return AuthorizationVerificationResult(
                    valid=False,
                    reason=(
                        "ACP authorization failed: Idempotency-Key is missing or empty. "
                        "All ACP POST requests must include an Idempotency-Key header."
                    ),
                )

            # ── Buyer identity consistency check ──────────────────────────────
            # The proof's claimed_buyer_agent_id must match the CommerceRequest.
            if proof.claimed_buyer_agent_id != request.buyer_agent_id:
                return AuthorizationVerificationResult(
                    valid=False,
                    reason=(
                        f"ACP buyer identity mismatch: proof claims buyer "
                        f"'{proof.claimed_buyer_agent_id}', but request has "
                        f"'{request.buyer_agent_id}'. "
                        f"Authorization cannot be applied to a different buyer."
                    ),
                )

            # ── Merchant identity consistency check ───────────────────────────
            if proof.claimed_merchant_id != request.merchant_id:
                return AuthorizationVerificationResult(
                    valid=False,
                    reason=(
                        f"ACP merchant identity mismatch: proof claims merchant "
                        f"'{proof.claimed_merchant_id}', but request targets "
                        f"'{request.merchant_id}'. "
                        f"Authorization cannot be applied to a different merchant."
                    ),
                )

            # ── Currency consistency check ─────────────────────────────────────
            req_currency = request.calculated_total.currency
            proof_currency = proof.claimed_currency.upper()
            if req_currency != proof_currency:
                return AuthorizationVerificationResult(
                    valid=False,
                    reason=(
                        f"ACP currency mismatch: proof claims currency "
                        f"'{proof_currency}', but request total is in '{req_currency}'."
                    ),
                )

            # ── Amount coverage check ─────────────────────────────────────────
            # The proof's claimed_amount_minor (computed from item prices) must
            # match the request's calculated total exactly. This prevents
            # submitting items with higher prices than those in the proof.
            req_total = request.calculated_total.amount_minor
            if proof.claimed_amount_minor != req_total:
                return AuthorizationVerificationResult(
                    valid=False,
                    reason=(
                        f"ACP amount mismatch: proof claims {proof.claimed_amount_minor} "
                        f"minor units, but request computed total is {req_total} "
                        f"minor units. The request and proof must agree on the total."
                    ),
                )

            # ── All structural checks passed ──────────────────────────────────
            # ACP uses Idempotency-Key as the replay key (TRANSACTION_ID namespace).
            # This is idempotency semantics, not one-time credential consumption.
            return AuthorizationVerificationResult(
                valid=True,
                reason=(
                    "ACP bearer token present, identity consistent, amount and "
                    "currency verified. Note: cryptographic bearer token "
                    "verification requires IDP integration (not yet implemented)."
                ),
                requires_replay_check=True,
                replay_namespace=ReplayNamespace.TRANSACTION_ID,
                replay_key=proof.idempotency_key,
                is_recurring=False,
            )

        except Exception as exc:
            # Fail-closed: any unexpected error → invalid
            return AuthorizationVerificationResult(
                valid=False,
                reason=(
                    f"Unexpected ACP authorization error "
                    f"({type(exc).__name__}): {exc}"
                ),
            )
