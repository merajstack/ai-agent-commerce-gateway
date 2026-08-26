"""
Cryptographic Authorization & Mandate Verification — Agent Commerce Gateway
=============================================================================

This module answers one question:

    **Is this purchase request backed by a valid, unmodified authorization
    from the actual buyer identity, within the authorized mandate?**

Security model — four distinct levels of validity:

    1. Structurally valid     (Pydantic schema — app/core/schemas.py)
    2. Cryptographically authentic  (THIS MODULE — Ed25519 signature verification)
    3. Policy-approved        (FUTURE — app/core/policy.py)
    4. Payment executed       (FUTURE — app/razorpay/)

    A valid Pydantic object is NEVER sufficient for authorization.
    A valid signature is NEVER sufficient for policy approval.
    A policy approval is NEVER sufficient for payment execution.

Pipeline position:

    CommerceRequest + SignedAuthorization
            ↓
    verify_mandate()       ← this module
            ↓
    AuthorizationVerificationResult
            ↓
    [FUTURE] policy engine
            ↓
    [FUTURE] replay protection
            ↓
    [FUTURE] Razorpay execution

Fail-closed contract:
    - Every code path in verify_mandate() that does not reach an explicit
      successful verification returns valid=False.
    - Internal exceptions are caught and converted to failed verification
      results with diagnostic detail for audit logging.
    - Missing, malformed, invalid, or unverifiable inputs → valid=False.
    - No silent error swallowing.

Ed25519 specifics:
    - 32-byte public keys, 64-byte private keys (seed), 64-byte signatures.
    - Signing uses nacl.signing.SigningKey.sign(message).signature
    - Verification uses nacl.signing.VerifyKey.verify(message, signature)
    - Key generation via nacl.signing.SigningKey.generate() for tests.

One-time mandate note:
    MandateType.one_time mandates must eventually be protected against replay.
    This module does NOT consume or mark mandates as used. Replay protection
    belongs to the next security stage (app/core/replay.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from nacl.exceptions import BadSignatureError, CryptoError
from nacl.signing import SigningKey, VerifyKey

from app.core.schemas import (
    CommerceRequest,
    Mandate,
    MandateStatus,
    MandateType,
    Money,
    Ed25519AuthorizationProof,
    AuthorizationProof,
    AuthorizationVerificationResult,
    AuthorizationProvider,
    ReplayNamespace,
)


# ══════════════════════════════════════════════════════════════════════════════
# AuthorizationVerificationResult — structured verification outcome
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# sign_mandate — signing helper for tests and development
# ══════════════════════════════════════════════════════════════════════════════


def sign_mandate(
    mandate: Mandate,
    private_key: SigningKey,
    key_id: str,
) -> Ed25519AuthorizationProof:
    """
    Sign a Mandate using Ed25519 and return an Ed25519AuthorizationProof.

    This is a SIGNING HELPER for tests and development tooling.
    It should never be used inside the verification pipeline.

    Process:
        1. Construct a temporary Ed25519AuthorizationProof with a placeholder
           signature (to access canonical_payload_bytes()).
        2. Compute canonical_payload_bytes() — the deterministic byte
           representation of all authorization-critical fields.
        3. Sign those bytes with the Ed25519 private key.
        4. Return a new Ed25519AuthorizationProof with the real signature.

    Args:
        mandate:     The Mandate to sign.
        private_key: An Ed25519 SigningKey (nacl.signing.SigningKey).
                     Must NOT be stored in source code.
        key_id:      Identifier for the signing key. Used by the verifier
                     to look up the corresponding public key.

    Returns:
        An Ed25519AuthorizationProof containing the mandate, Ed25519 signature,
        key_id, and algorithm="Ed25519".
    """
    # Step 1: Build a temporary Ed25519AuthorizationProof to access canonical bytes.
    # The placeholder signature is structurally valid but cryptographically
    # meaningless — it is immediately replaced.
    temp = Ed25519AuthorizationProof(
        payload=mandate,
        signature=b"\x00" * 64,  # placeholder — 64 bytes (Ed25519 sig size)
        key_id=key_id,
        algorithm="Ed25519",
    )

    # Step 2: Get the canonical bytes that the signature must cover.
    canonical_bytes = temp.canonical_payload_bytes()

    # Step 3: Sign the canonical bytes with Ed25519.
    # SigningKey.sign() returns a SignedMessage; we extract just the signature.
    signed_message = private_key.sign(canonical_bytes)
    signature = signed_message.signature  # 64 bytes

    # Step 4: Build the real Ed25519AuthorizationProof with the actual signature.
    return Ed25519AuthorizationProof(
        payload=mandate,
        signature=signature,
        key_id=key_id,
        algorithm="Ed25519",
    )


# ══════════════════════════════════════════════════════════════════════════════
# verify_mandate — the cryptographic authorization verification function
# ══════════════════════════════════════════════════════════════════════════════


def verify_mandate(
    signed_authorization: Ed25519AuthorizationProof,
    expected_buyer_agent_id: str,
    expected_merchant_id: str,
    public_key: VerifyKey,
    current_time: datetime | None = None,
) -> AuthorizationVerificationResult:
    """
    Verify a Ed25519AuthorizationProof is authentic, unmodified, correctly bound,
    and temporally valid.

    This is the gateway's cryptographic authorization function.

    Verification steps (all must pass):
        1. Algorithm check     — must be Ed25519.
        2. Signature check     — Ed25519 signature over canonical bytes
                                 must verify with the provided public key.
        3. Identity binding    — signed buyer_agent_id must match
                                 expected_buyer_agent_id.
                                 signed merchant_id must match
                                 expected_merchant_id.
        4. Temporal validation — mandate must not have expired against
                                 current wall-clock time.
                                 mandate must have valid temporal state
                                 (expires_at > issued_at — enforced by
                                 schema, but re-checked defensively).

    Fail-closed contract:
        - If ANY check fails → valid=False with a specific reason.
        - If an internal exception occurs → valid=False with diagnostic detail.
        - valid=True is returned ONLY when ALL checks pass.
        - Exceptions are caught, not swallowed — they produce failed results
          with enough detail for audit logging.

    What this function does NOT do:
        - Policy evaluation (spending limits beyond the mandate, categories,
          velocity, review thresholds).
        - Replay protection (checking if a one-time mandate has been used).
        - Payment execution.
        - Mutate GatewayDecision to ALLOW — a successful verification still
          leaves the transaction UNDECIDED until policy runs.

    Args:
        signed_authorization:    The Ed25519AuthorizationProof to verify.
        expected_buyer_agent_id: The buyer agent ID the request claims.
                                 Must match what is inside the signed mandate.
        expected_merchant_id:    The merchant ID the request targets.
                                 Must match what is inside the signed mandate.
        public_key:              The Ed25519 VerifyKey to verify against.
                                 In production, this would be obtained via
                                 a trusted key registry keyed by key_id.
        current_time:            UTC-aware datetime for expiry checking.
                                 Defaults to datetime.now(timezone.utc).

    Returns:
        AuthorizationVerificationResult — always carries valid + reason.
    """
    # Default current_time to now (UTC).
    if current_time is None:
        current_time = datetime.now(timezone.utc)

    try:
        # ── Step 0: Basic structural checks ───────────────────────────────

        if signed_authorization.signature is None or len(signed_authorization.signature) == 0:
            return AuthorizationVerificationResult(
                valid=False,
                reason="Missing signature: signed authorization has no signature bytes.",
            )

        if not signed_authorization.key_id:
            return AuthorizationVerificationResult(
                valid=False,
                reason="Missing key_id: signed authorization has no key identifier.",
            )

        # ── Step 1: Algorithm check ───────────────────────────────────────

        if signed_authorization.algorithm != "Ed25519":
            return AuthorizationVerificationResult(
                valid=False,
                reason=f"Unsupported algorithm: '{signed_authorization.algorithm}'. "
                       f"Only 'Ed25519' is supported.",
            )

        # ── Step 2: Ed25519 signature verification ────────────────────────

        canonical_bytes = signed_authorization.canonical_payload_bytes()

        try:
            public_key.verify(canonical_bytes, signed_authorization.signature)
        except BadSignatureError:
            return AuthorizationVerificationResult(
                valid=False,
                reason="Signature mismatch: Ed25519 signature does not match "
                       "the canonical payload. The authorization may have been "
                       "tampered with or signed by a different key.",
            )
        except CryptoError as e:
            return AuthorizationVerificationResult(
                valid=False,
                reason=f"Cryptographic error during signature verification: {e}",
            )

        # ── Step 3: Identity binding ──────────────────────────────────────

        mandate = signed_authorization.payload

        if mandate.buyer_agent_id != expected_buyer_agent_id:
            return AuthorizationVerificationResult(
                valid=False,
                reason=f"Buyer identity mismatch: signed mandate authorizes "
                       f"'{mandate.buyer_agent_id}', but the request claims "
                       f"'{expected_buyer_agent_id}'. An authorization cannot "
                       f"be used by a different agent.",
            )

        if mandate.merchant_id != expected_merchant_id:
            return AuthorizationVerificationResult(
                valid=False,
                reason=f"Merchant identity mismatch: signed mandate targets "
                       f"'{mandate.merchant_id}', but the request targets "
                       f"'{expected_merchant_id}'. An authorization cannot "
                       f"be applied to a different merchant.",
            )

        # ── Step 4: Temporal validation ───────────────────────────────────

        # Defensive re-check: expires_at must be after issued_at.
        # (Schema enforces this, but we do not trust upstream validation
        # alone for security-critical decisions.)
        if mandate.expires_at <= mandate.issued_at:
            return AuthorizationVerificationResult(
                valid=False,
                reason=f"Invalid temporal state: expires_at "
                       f"({mandate.expires_at.isoformat()}) is not after "
                       f"issued_at ({mandate.issued_at.isoformat()}).",
            )

        # Check expiry against current wall-clock time.
        if current_time >= mandate.expires_at:
            return AuthorizationVerificationResult(
                valid=False,
                reason=f"Authorization expired: mandate expired at "
                       f"{mandate.expires_at.isoformat()}, current time is "
                       f"{current_time.isoformat()}.",
            )

        # ── All checks passed ─────────────────────────────────────────────

        return AuthorizationVerificationResult(
            valid=True,
            reason="Signature verified, identity bound, and temporally valid.",
        )

    except Exception as e:
        # Fail-closed: ANY unexpected exception → invalid.
        # Never return valid=True after an internal error.
        # Preserve detail for audit logging.
        return AuthorizationVerificationResult(
            valid=False,
            reason=f"Unexpected verification error ({type(e).__name__}): {e}",
        )


# ══════════════════════════════════════════════════════════════════════════════
# verify_authorization_scope — check request against signed mandate scope
# ══════════════════════════════════════════════════════════════════════════════


def verify_authorization_scope(
    request: CommerceRequest,
    signed_authorization: Ed25519AuthorizationProof,
) -> AuthorizationVerificationResult:
    """
    Verify that a CommerceRequest falls within the signed mandate's scope.

    This uses the SIGNED mandate as the authority — not fields supplied
    separately by the requester.

    Checks:
        1. Buyer identity match:    request.buyer_agent_id == mandate.buyer_agent_id
        2. Merchant identity match: request.merchant_id == mandate.merchant_id
        3. Currency match:          request currency == mandate currency
        4. Amount within limit:     request.calculated_total <= mandate.max_amount

    This function does NOT verify the signature itself — call verify_mandate()
    first. This is a supplementary scope check that uses the verified mandate
    as ground truth.

    Args:
        request:              The CommerceRequest to check.
        signed_authorization: The Ed25519AuthorizationProof whose mandate to check against.

    Returns:
        AuthorizationVerificationResult.
    """
    try:
        mandate = signed_authorization.payload

        # ── Buyer match ───────────────────────────────────────────────────
        if request.buyer_agent_id != mandate.buyer_agent_id:
            return AuthorizationVerificationResult(
                valid=False,
                reason=f"Buyer identity mismatch: request buyer "
                       f"'{request.buyer_agent_id}' does not match "
                       f"signed mandate buyer '{mandate.buyer_agent_id}'.",
            )

        # ── Merchant match ────────────────────────────────────────────────
        if request.merchant_id != mandate.merchant_id:
            return AuthorizationVerificationResult(
                valid=False,
                reason=f"Merchant identity mismatch: request merchant "
                       f"'{request.merchant_id}' does not match "
                       f"signed mandate merchant '{mandate.merchant_id}'.",
            )

        # ── Currency match ────────────────────────────────────────────────
        req_currency = request.calculated_total.currency
        mandate_currency = mandate.max_amount.currency
        if req_currency != mandate_currency:
            return AuthorizationVerificationResult(
                valid=False,
                reason=f"Currency mismatch: request uses '{req_currency}', "
                       f"signed mandate authorizes '{mandate_currency}'.",
            )

        # ── Amount within limit ───────────────────────────────────────────
        req_total = request.calculated_total.amount_minor
        max_amount = mandate.max_amount.amount_minor
        if req_total > max_amount:
            return AuthorizationVerificationResult(
                valid=False,
                reason=f"Amount exceeds authorization: request total "
                       f"{req_total} minor units exceeds signed mandate "
                       f"maximum {max_amount} minor units.",
            )

        return AuthorizationVerificationResult(
            valid=True,
            reason="Request is within signed mandate scope.",
        )

    except Exception as e:
        return AuthorizationVerificationResult(
            valid=False,
            reason=f"Unexpected scope verification error ({type(e).__name__}): {e}",
        )


# ══════════════════════════════════════════════════════════════════════════════
# Ed25519MandateProvider — concrete AuthorizationProvider implementation
# ══════════════════════════════════════════════════════════════════════════════


class Ed25519MandateProvider(AuthorizationProvider):
    """
    Authorization provider that verifies Ed25519 signatures and limits against
    the requested CommerceRequest.
    """

    def __init__(self, public_key: VerifyKey, current_time: datetime | None = None):
        self.public_key = public_key
        self.current_time = current_time

    def verify(self, request: CommerceRequest, proof: AuthorizationProof) -> AuthorizationVerificationResult:
        if not isinstance(proof, Ed25519AuthorizationProof):
            return AuthorizationVerificationResult(
                valid=False,
                reason=f"Invalid proof type: expected Ed25519AuthorizationProof, got {type(proof).__name__}",
            )

        auth_result = verify_mandate(
            signed_authorization=proof,
            expected_buyer_agent_id=request.buyer_agent_id,
            expected_merchant_id=request.merchant_id,
            public_key=self.public_key,
            current_time=self.current_time,
        )
        if not auth_result.valid:
            return auth_result

        scope_result = verify_authorization_scope(
            request=request,
            signed_authorization=proof,
        )
        if not scope_result.valid:
            return scope_result

        # Both checks passed. Determine replay semantics.
        mandate = proof.payload
        is_recurring = (mandate.mandate_type == MandateType.recurring)

        return AuthorizationVerificationResult(
            valid=True,
            reason="Signature verified, identity bound, temporally valid, and within scope.",
            requires_replay_check=True,
            replay_namespace=ReplayNamespace.TRANSACTION_ID if is_recurring else ReplayNamespace.AUTHORIZATION_NONCE,
            replay_key=request.transaction_id if is_recurring else mandate.nonce,
            is_recurring=is_recurring,
        )
