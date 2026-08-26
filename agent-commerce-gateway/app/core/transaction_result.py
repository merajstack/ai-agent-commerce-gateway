"""
Transaction Result — Agent Commerce Gateway
============================================

Defines the immutable structured result of the full security pipeline.

The `TransactionResult` carries enough information for:
    - Audit logging (without exposing cryptographic secrets)
    - Downstream consumers to understand what happened and why
    - Future Razorpay execution layer to confirm ALLOW before proceeding

Pipeline stages:
    VALIDATION   → Structural validation of CommerceContext
    AUTHORIZATION → Ed25519 signature + scope verification
    REPLAY       → Nonce/idempotency check-and-reserve
    POLICY       → Merchant policy evaluation
    FINAL        → Pipeline completed; result ready

Processing states (track progress through the pipeline):
    NOT_STARTED       → Initial state before any stage runs
    AUTHORIZED        → Authorization check passed
    REPLAY_CHECKED    → Replay check passed (nonce reserved)
    POLICY_EVALUATED  → Policy returned ALLOW / REVIEW / BLOCK
    BLOCKED           → Pipeline stopped with BLOCK decision
    REVIEW            → Pipeline completed with REVIEW decision
    ALLOWED           → Pipeline completed with ALLOW decision

Security invariants:
    - TransactionResult is frozen (immutable after construction).
    - No cryptographic secrets (private keys, raw signature bytes) are stored.
    - Sub-results (authorization_result, replay_result, policy_result) are
      the same safe structured types produced by their respective modules.
    - A TransactionResult with decision=ALLOW was produced by the orchestrator
      ONLY after all prior gates (authorization, replay, policy) passed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from app.core.mandate import AuthorizationVerificationResult
from app.core.replay import ReplayResult
from app.core.policy import PolicyDecision
from app.core.schemas import GatewayDecision


# ══════════════════════════════════════════════════════════════════════════════
# Pipeline stage enum
# ══════════════════════════════════════════════════════════════════════════════


class PipelineStage(str, Enum):
    """
    The stage the pipeline reached before producing a result.

    Used to understand WHERE the pipeline stopped (not just the decision).
    """

    VALIDATION = "VALIDATION"
    """Structural validation of CommerceContext."""

    AUTHORIZATION = "AUTHORIZATION"
    """Ed25519 signature and scope verification."""

    REPLAY = "REPLAY"
    """Nonce/idempotency check-and-reserve."""

    POLICY = "POLICY"
    """Merchant policy evaluation."""

    FINAL = "FINAL"
    """Pipeline ran to completion; final decision issued."""


# ══════════════════════════════════════════════════════════════════════════════
# Processing state enum
# ══════════════════════════════════════════════════════════════════════════════


class ProcessingState(str, Enum):
    """
    Granular state of the transaction as it moves through the pipeline.

    Each state maps to one stage of the pipeline.  Terminal states
    (BLOCKED, REVIEW, ALLOWED) carry the final GatewayDecision.
    """

    NOT_STARTED = "NOT_STARTED"
    """Pipeline has not begun processing."""

    AUTHORIZED = "AUTHORIZED"
    """Authorization check passed; nonce not yet reserved."""

    REPLAY_CHECKED = "REPLAY_CHECKED"
    """Replay check passed; nonce reserved; policy not yet evaluated."""

    POLICY_EVALUATED = "POLICY_EVALUATED"
    """Policy returned a decision; final result being assembled."""

    BLOCKED = "BLOCKED"
    """Pipeline stopped with BLOCK (any stage)."""

    REVIEW = "REVIEW"
    """Pipeline completed; transaction flagged for review."""

    ALLOWED = "ALLOWED"
    """All gates passed; transaction approved for payment execution."""


# ══════════════════════════════════════════════════════════════════════════════
# TransactionResult — immutable pipeline outcome
# ══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class TransactionResult:
    """
    Immutable result of the full security pipeline.

    Produced by `process_transaction()` in `app/core/orchestrator.py`.
    Every field is safe for audit logging — no cryptographic secrets.

    Attributes:
        transaction_id:
            The CommerceRequest.transaction_id this result belongs to.

        decision:
            Final GatewayDecision: ALLOW, REVIEW, or BLOCK.
            UNDECIDED is never present in a TransactionResult.

        stage_reached:
            The last PipelineStage that ran before the result was produced.
            Useful for audit logging ("blocked at authorization vs. replay
            vs. policy").

        processing_state:
            Granular state at the time the result was produced.
            Maps 1-to-1 with the final step the pipeline completed.

        reason:
            Human-readable explanation of the outcome.
            Safe for audit logging.
            Must NOT contain raw database internals or cryptographic secrets.

        authorization_result:
            The AuthorizationVerificationResult from verify_mandate().
            None if the pipeline was blocked before authorization ran.

        replay_result:
            The ReplayResult from the replay store.
            None if the pipeline was blocked before replay ran.

        policy_result:
            The PolicyDecision from evaluate_policy().
            None if the pipeline was blocked before policy ran.

        timestamp:
            UTC datetime when this result was produced.

    Security invariants:
        - `decision` is set by the orchestrator, not copied from sub-results.
        - A ALLOW decision can only be constructed by the orchestrator after
          all three prior gates passed.
        - This dataclass is frozen; no post-construction mutation is possible.
    """

    transaction_id: str
    decision: GatewayDecision
    stage_reached: PipelineStage
    processing_state: ProcessingState
    reason: str
    authorization_result: Optional[AuthorizationVerificationResult]
    replay_result: Optional[ReplayResult]
    policy_result: Optional[PolicyDecision]
    timestamp: datetime

    def __post_init__(self) -> None:
        # Enforce that UNDECIDED never appears in a TransactionResult.
        if self.decision == GatewayDecision.UNDECIDED:
            raise ValueError(
                "TransactionResult.decision must not be UNDECIDED. "
                "The orchestrator must produce ALLOW, REVIEW, or BLOCK."
            )

    @classmethod
    def blocked(
        cls,
        transaction_id: str,
        stage: PipelineStage,
        reason: str,
        authorization_result: Optional[AuthorizationVerificationResult] = None,
        replay_result: Optional[ReplayResult] = None,
        policy_result: Optional[PolicyDecision] = None,
    ) -> "TransactionResult":
        """Convenience constructor for BLOCK results."""
        return cls(
            transaction_id=transaction_id,
            decision=GatewayDecision.BLOCK,
            stage_reached=stage,
            processing_state=ProcessingState.BLOCKED,
            reason=reason,
            authorization_result=authorization_result,
            replay_result=replay_result,
            policy_result=policy_result,
            timestamp=datetime.now(timezone.utc),
        )

    @classmethod
    def review(
        cls,
        transaction_id: str,
        reason: str,
        authorization_result: AuthorizationVerificationResult,
        replay_result: ReplayResult,
        policy_result: PolicyDecision,
    ) -> "TransactionResult":
        """Convenience constructor for REVIEW results."""
        return cls(
            transaction_id=transaction_id,
            decision=GatewayDecision.REVIEW,
            stage_reached=PipelineStage.FINAL,
            processing_state=ProcessingState.REVIEW,
            reason=reason,
            authorization_result=authorization_result,
            replay_result=replay_result,
            policy_result=policy_result,
            timestamp=datetime.now(timezone.utc),
        )

    @classmethod
    def allowed(
        cls,
        transaction_id: str,
        reason: str,
        authorization_result: AuthorizationVerificationResult,
        replay_result: ReplayResult,
        policy_result: PolicyDecision,
    ) -> "TransactionResult":
        """Convenience constructor for ALLOW results."""
        return cls(
            transaction_id=transaction_id,
            decision=GatewayDecision.ALLOW,
            stage_reached=PipelineStage.FINAL,
            processing_state=ProcessingState.ALLOWED,
            reason=reason,
            authorization_result=authorization_result,
            replay_result=replay_result,
            policy_result=policy_result,
            timestamp=datetime.now(timezone.utc),
        )

    def __repr__(self) -> str:
        return (
            f"TransactionResult("
            f"txn={self.transaction_id!r}, "
            f"decision={self.decision.value}, "
            f"stage={self.stage_reached.value}, "
            f"reason={self.reason!r}"
            f")"
        )
