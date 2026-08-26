"""
Transaction Orchestrator — Agent Commerce Gateway
=================================================

Single entry point for processing a normalized commerce transaction through
the complete security pipeline.

Pipeline (enforced in strict order):
    ┌─────────────────────────────────────────────┐
    │  CommerceRequest + SignedAuthorization       │
    │              ↓                              │
    │  1. Structural validation                   │
    │     (CommerceContext — schema + consistency) │
    │              ↓                              │
    │  2. Cryptographic authorization             │
    │     (verify_mandate + scope check)          │
    │              ↓                              │
    │  3. Replay / nonce protection               │
    │     (atomic DB reservation)                 │
    │              ↓                              │
    │  4. Merchant policy                         │
    │     (evaluate_policy)                       │
    │              ↓                              │
    │  5. Final decision                          │
    │     ALLOW / REVIEW / BLOCK                  │
    └─────────────────────────────────────────────┘

Security invariants enforced structurally (not by documentation):
    - `evaluate_policy` is NEVER called before authorization succeeds.
    - `evaluate_policy` is NEVER called if replay check fails.
    - The nonce is NEVER consumed if authorization fails.
    - No Razorpay client is invoked here.  Only ALLOW results are eligible
      for payment execution (future layer).

Replay nonce semantics (one_time mandates):
    The authorization nonce is reserved AFTER authorization succeeds but
    BEFORE policy runs.  If policy subsequently BLOCKs the transaction,
    the nonce remains consumed.  Rationale: a one_time credential is
    single-use from the moment the gateway verifies and reserves it.
    Allowing retry of the same credential after a policy block would permit
    policy-condition-based retry attacks.

    To retry after a policy block on a one_time mandate, the buyer must
    obtain a fresh signed authorization with a new nonce.

Replay nonce semantics (recurring mandates):
    The mandate nonce is NOT consumed.  Instead, the transaction_id is
    reserved as an idempotency key.  The mandate stays alive for future
    distinct transactions.  If policy BLOCKs, the transaction_id remains
    reserved (preventing duplicate submission) but the mandate is unaffected.

REVIEW semantics:
    A REVIEW result stops before any payment execution.
    Only ALLOW may eventually proceed to the Razorpay execution layer
    (not implemented in this module).

No bypass:
    There is no `approve_transaction` function that skips gates.
    The policy engine (`evaluate_policy`) remains independently callable
    for unit tests — but the production transaction entry point is
    `process_transaction`, and it ALWAYS enforces the full sequence.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from pydantic import ValidationError
from nacl.signing import VerifyKey

from app.core.mandate import (
    AuthorizationVerificationResult,
)
from app.core.policy import PolicyConfig, evaluate_policy
from app.core.replay import ReplayStore, ReplayResult
from app.core.schemas import (
    CommerceContext,
    CommerceRequest,
    GatewayDecision,
    MandateType,
    AuthorizationProof,
    AuthorizationProvider,
    ReplayNamespace,
)
from app.core.transaction_result import (
    PipelineStage,
    ProcessingState,
    TransactionResult,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# process_transaction — the production security pipeline entry point
# ══════════════════════════════════════════════════════════════════════════════


def process_transaction(
    request: CommerceRequest,
    auth_proof: AuthorizationProof,
    auth_provider: AuthorizationProvider,
    policy_config: PolicyConfig,
    replay_store: ReplayStore,
) -> TransactionResult:
    """
    Process a normalized commerce transaction through the full security pipeline.

    This is the ONLY production entry point for transaction processing.
    It enforces the security pipeline in strict order and returns a
    structured, immutable `TransactionResult`.

    Pipeline:
        1. Structural validation   — build CommerceContext
        2. Cryptographic auth      — verify_mandate() + verify_authorization_scope()
        3. Replay protection       — atomic nonce reservation
        4. Merchant policy         — evaluate_policy()
        5. Final decision          — ALLOW / REVIEW / BLOCK

    Args:
        request:              The normalized CommerceRequest.
        auth_proof:           The buyer's AuthorizationProof.
        auth_provider:        The AuthorizationProvider to verify the proof.
        policy_config:        The merchant's PolicyConfig.
        replay_store:         The ReplayStore implementation (SQLite or test double).

    Returns:
        TransactionResult — always; never raises.

    Security guarantees:
        - Returns BLOCK at VALIDATION stage if structural validation fails.
        - Returns BLOCK at AUTHORIZATION stage if signature/scope check fails.
          Nonce is NOT consumed.
        - Returns BLOCK at REPLAY stage if nonce already used or store is down.
          Policy is NOT evaluated.
        - Returns BLOCK/REVIEW/ALLOW at POLICY/FINAL stage based on policy.
        - No Razorpay client is invoked.
    """
    transaction_id = request.transaction_id

    logger.info(
        "Processing transaction: txn=%r, buyer=%r, merchant=%r",
        transaction_id,
        request.buyer_agent_id,
        request.merchant_id,
    )

    # ── Stage 1: Structural Validation ────────────────────────────────────────
    # Build a CommerceContext, which enforces buyer/merchant/currency/amount
    # cross-consistency.  ValidationError → BLOCK immediately.

    try:
        context = CommerceContext(
            request=request,
            auth_proof=auth_proof,
        )
    except ValidationError as exc:
        reason = f"Structural validation failed: {exc.error_count()} error(s). " \
                 f"First: {exc.errors()[0]['msg']}"
        logger.warning("BLOCK at VALIDATION: txn=%r — %s", transaction_id, reason)
        return TransactionResult.blocked(
            transaction_id=transaction_id,
            stage=PipelineStage.VALIDATION,
            reason=reason,
        )
    except Exception as exc:  # noqa: BLE001
        reason = f"Unexpected validation error ({type(exc).__name__}): {exc}"
        logger.error("BLOCK at VALIDATION (unexpected): txn=%r — %s", transaction_id, reason)
        return TransactionResult.blocked(
            transaction_id=transaction_id,
            stage=PipelineStage.VALIDATION,
            reason=reason,
        )

    # ── Stage 2: Cryptographic Authorization ─────────────────────────────────
    # The provider verifies the generic proof against the request.
    # Failure → BLOCK at AUTHORIZATION.
    # The replay identity is NOT consumed here.

    auth_result: AuthorizationVerificationResult = auth_provider.verify(request, auth_proof)

    if not auth_result.valid:
        logger.warning(
            "BLOCK at AUTHORIZATION: txn=%r — %s",
            transaction_id, auth_result.reason
        )
        return TransactionResult.blocked(
            transaction_id=transaction_id,
            stage=PipelineStage.AUTHORIZATION,
            reason=auth_result.reason,
            authorization_result=auth_result,
        )

    logger.debug("Authorization passed: txn=%r", transaction_id)

    # ── Stage 3: Replay Protection ────────────────────────────────────────────
    # Atomically reserve the appropriate nonce based on the auth_result.
    #
    # Failure (replay detected OR store unavailable) → BLOCK at REPLAY.
    # Policy is NEVER evaluated after a replay failure.

    if auth_result.requires_replay_check:
        if auth_result.replay_namespace == ReplayNamespace.AUTHORIZATION_NONCE:
            if not auth_result.replay_key:
                return TransactionResult.blocked(
                    transaction_id=transaction_id,
                    stage=PipelineStage.REPLAY,
                    reason="Authorization nonce requires a replay key, but none provided.",
                    authorization_result=auth_result,
                )
            replay_result = replay_store.check_and_reserve_authorization_nonce(
                nonce=auth_result.replay_key
            )
        elif auth_result.replay_namespace == ReplayNamespace.TRANSACTION_ID:
            replay_result = replay_store.check_and_reserve_transaction_id(
                transaction_id=request.transaction_id
            )
        else:
            return TransactionResult.blocked(
                transaction_id=transaction_id,
                stage=PipelineStage.REPLAY,
                reason=f"Unknown replay namespace: {auth_result.replay_namespace}",
                authorization_result=auth_result,
            )
    else:
        replay_result = ReplayResult(allowed=True, was_replay=False, reason="Replay check not required by provider")

    if not replay_result.allowed:
        logger.warning(
            "BLOCK at REPLAY: txn=%r — %s",
            transaction_id, replay_result.reason
        )
        return TransactionResult.blocked(
            transaction_id=transaction_id,
            stage=PipelineStage.REPLAY,
            reason=replay_result.reason,
            authorization_result=auth_result,
            replay_result=replay_result,
        )

    logger.debug("Replay check passed: txn=%r", transaction_id)

    # ── Stage 4: Merchant Policy ──────────────────────────────────────────────
    # evaluate_policy is called ONLY after both authorization and replay passed.
    # A policy BLOCK does not un-reserve the nonce (see module docstring).
    
    # Recreate context with is_recurring knowledge for the policy engine
    context = CommerceContext(
        request=request,
        auth_proof=auth_proof,
        is_recurring=auth_result.is_recurring,
    )

    policy_result = evaluate_policy(context=context, config=policy_config)

    logger.info(
        "Policy decision: txn=%r → %s (%s)",
        transaction_id, policy_result.decision.value, policy_result.primary_reason
    )

    # ── Stage 5: Final Decision ───────────────────────────────────────────────

    if policy_result.decision == GatewayDecision.BLOCK:
        return TransactionResult.blocked(
            transaction_id=transaction_id,
            stage=PipelineStage.POLICY,
            reason=policy_result.primary_reason,
            authorization_result=auth_result,
            replay_result=replay_result,
            policy_result=policy_result,
        )

    if policy_result.decision == GatewayDecision.REVIEW:
        return TransactionResult.review(
            transaction_id=transaction_id,
            reason=policy_result.primary_reason,
            authorization_result=auth_result,
            replay_result=replay_result,
            policy_result=policy_result,
        )

    # GatewayDecision.ALLOW — all gates passed.
    # NOTE: Razorpay execution is NOT invoked here.
    # The future Razorpay layer must receive an explicit ALLOW result
    # and must never be called with REVIEW or BLOCK.
    return TransactionResult.allowed(
        transaction_id=transaction_id,
        reason=policy_result.primary_reason,
        authorization_result=auth_result,
        replay_result=replay_result,
        policy_result=policy_result,
    )
