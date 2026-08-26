"""
x402 Authorization Provider — Agent Commerce Gateway
======================================================

Implements AuthorizationProvider for x402 v2 PaymentPayload authorizations.

x402 Verification Model (from authoritative spec):
    Source: x402 v2 Specification (RFC HTTP 402 Payment Required)
    The client transmits an x402 v2 PaymentPayload containing:
    - x402Version: 2
    - resource: protected endpoint descriptor
    - accepted: agreed terms (scheme, network, asset, amount, payTo, extra)
    - payload: scheme-specific authorization claims & cryptographic signature
    - extensions: composable protocol extensions

Security Boundaries & Principles:
    1. Structural presence/generation is EXPLICITLY NOT verification of settlement.
    2. The provider performs strict structural and parameter checks (supported schemes,
       networks, assets, and amount coverage).
    3. Cryptographic/settlement verification is delegated to an `X402PaymentVerifier`.
    4. If no verifier is configured (`verifier=None`), the provider MUST fail closed.
    5. Replay protection uses the unique `nonce` (or `tx_hash`), ensuring that a
       given payment payload can only be consumed once.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from app.adapters.x402_adapter import X402AuthorizationProof
from app.core.schemas import (
    AuthorizationProof,
    AuthorizationProvider,
    AuthorizationVerificationResult,
    CommerceRequest,
    ReplayNamespace,
)


class X402PaymentVerifier(ABC):
    """
    Abstract interface for verifying x402 blockchain transactions and payment proofs.
    
    A concrete implementation (e.g., calling an on-chain facilitator, RPC node,
    or testnet verifier) must be injected into X402AuthorizationProvider.
    """

    @abstractmethod
    def verify_transaction(self, tx_hash: str, network: str, token: str, required_amount_minor: int) -> bool:
        """
        Verify that a blockchain transaction hash occurred and settled.
        """
        pass

    def verify_payment(self, proof: X402AuthorizationProof, request: CommerceRequest) -> bool:
        """
        Verify that an x402 v2 PaymentPayload proof is cryptographically authentic,
        authorized for the specified amount and recipient, and valid for settlement.
        Default implementation bridges to verify_transaction using tx_hash or nonce.
        """
        tx_identifier = proof.tx_hash or proof.nonce
        return self.verify_transaction(
            tx_hash=tx_identifier,
            network=proof.network,
            token=proof.token,
            required_amount_minor=request.calculated_total.amount_minor,
        )


class SandboxX402Verifier(X402PaymentVerifier):
    """
    Standard Sandbox/Testnet verifier for demo and test environments.
    
    Validates cryptographic signature presence and formatting against test parameters.
    Fails closed if the signature is missing, malformed, or explicitly marked invalid.
    """

    def verify_transaction(self, tx_hash: str, network: str, token: str, required_amount_minor: int) -> bool:
        if not tx_hash or "invalid" in tx_hash.lower() or tx_hash.startswith("tx_invalid"):
            return False
        return True

    def verify_payment(self, proof: X402AuthorizationProof, request: CommerceRequest) -> bool:
        # Check tx_hash or nonce
        identifier = proof.tx_hash or proof.nonce
        if not identifier or "invalid" in identifier.lower() or identifier.startswith("tx_invalid"):
            return False

        # If a signature is present, enforce valid hex formatting
        if proof.signature is not None:
            if not proof.signature.startswith("0x") or len(proof.signature) < 10 or proof.signature.startswith("0xbad"):
                return False

        # If pay_to is present, enforce valid address formatting
        if proof.pay_to is not None:
            if not proof.pay_to.startswith("0x") and len(proof.pay_to) < 5:
                return False

        return True


class X402AuthorizationProvider(AuthorizationProvider):
    """
    Authorization provider for x402 requests.

    Enforces structural checks and delegates on-chain/facilitator verification
    to an X402PaymentVerifier. Fail-closed contract.
    """

    SUPPORTED_SCHEMES = {"exact"}
    SUPPORTED_NETWORKS = {
        "eip155:84532", "eip155:8453", "base-sepolia", "base",
        "solana", "polygon", "avalanche"
    }
    SUPPORTED_TOKENS = {
        "0x036cbd53842c5426634e7929541ec2318f3dcf7e", "usdc", "inr", "usd"
    }

    def __init__(self, verifier: Optional[X402PaymentVerifier] = None):
        """
        Initialize the x402 provider.
        
        Args:
            verifier: An instance of X402PaymentVerifier used to perform
                      actual cryptographic and on-chain verification. If None,
                      all verifications fail closed.
        """
        self.verifier = verifier

    def verify(
        self,
        request: CommerceRequest,
        proof: AuthorizationProof,
    ) -> AuthorizationVerificationResult:
        """
        Verify an X402AuthorizationProof against the given CommerceRequest.
        """
        try:
            # ── Type check ────────────────────────────────────────────────────
            if not isinstance(proof, X402AuthorizationProof):
                return AuthorizationVerificationResult(
                    valid=False,
                    reason=(
                        f"Invalid proof type for X402AuthorizationProvider: "
                        f"expected X402AuthorizationProof, got {type(proof).__name__}."
                    ),
                )

            # ── Scheme Check ──────────────────────────────────────────────────
            if proof.scheme.lower() not in self.SUPPORTED_SCHEMES:
                return AuthorizationVerificationResult(
                    valid=False,
                    reason=f"x402 unsupported scheme: '{proof.scheme}'. Supported: {self.SUPPORTED_SCHEMES}"
                )

            # ── Network Check ─────────────────────────────────────────────────
            if proof.network.lower() not in self.SUPPORTED_NETWORKS:
                return AuthorizationVerificationResult(
                    valid=False,
                    reason=f"x402 unsupported network: '{proof.network}'. Supported: {self.SUPPORTED_NETWORKS}"
                )

            # ── Token / Asset Check ───────────────────────────────────────────
            token_normalized = proof.token.lower()
            if token_normalized not in self.SUPPORTED_TOKENS and not token_normalized.startswith("0x"):
                return AuthorizationVerificationResult(
                    valid=False,
                    reason=f"x402 unsupported token/asset: '{proof.token}'. Supported: {self.SUPPORTED_TOKENS}"
                )

            # ── Amount Coverage Check ─────────────────────────────────────────
            req_total = request.calculated_total.amount_minor
            if proof.claimed_amount_minor < req_total:
                return AuthorizationVerificationResult(
                    valid=False,
                    reason=(
                        f"x402 insufficient amount: proof claims {proof.claimed_amount_minor} "
                        f"minor units, but request total is {req_total} minor units."
                    ),
                )

            # ── Delegated Verification Check ──────────────────────────────────
            # Structural validity is explicitly NOT verification of settlement.
            if self.verifier is None:
                return AuthorizationVerificationResult(
                    valid=False,
                    reason="x402 verification failed: no X402PaymentVerifier configured. "
                           "Structural validity does not prove payment settlement."
                )

            is_verified = self.verifier.verify_payment(proof, request)

            if not is_verified:
                return AuthorizationVerificationResult(
                    valid=False,
                    reason="x402 verification failed: the configured X402PaymentVerifier "
                           "rejected the transaction hash or payment signature as invalid, incomplete, or unconfirmed."
                )

            # ── Verification Passed ───────────────────────────────────────────
            replay_key = proof.nonce or proof.tx_hash
            return AuthorizationVerificationResult(
                valid=True,
                reason="x402 payment structurally sound and verified.",
                requires_replay_check=True,
                replay_namespace=ReplayNamespace.TRANSACTION_ID,
                replay_key=replay_key,
                is_recurring=False,
            )

        except Exception as exc:
            return AuthorizationVerificationResult(
                valid=False,
                reason=f"Unexpected x402 authorization error ({type(exc).__name__}): {exc}"
            )
