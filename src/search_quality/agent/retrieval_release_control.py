"""Immutable Owner control plane for stage-aware retrieval releases.

The stage-aware Runtime produces experiment evidence.  This module turns one
gate-passing Runtime result into a reviewable proposal, records the Owner's
decision, and finally records the serving validator's outcome.  Approval alone
never writes the serving pointer and never claims that a strategy is active.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import (
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

from search_quality.evaluation.artifacts import (
    require_clean_code_revision,
    write_immutable_json,
)
from search_quality.evaluation.retrieval_comparison import compare_retrieval_runs
from search_quality.evaluation.retrieval_validation import validate_retrieval_run

from .contracts import StrictModel, TerminalOutcome
from .replay import TraceReplayer
from .retrieval_tools import CandidateGateSummary
from .stage_diagnosis import StageDiagnosis, diagnose_retrieval_stages
from .trace import TraceStore

logger = logging.getLogger("search_quality.retrieval_release")

PROPOSAL_SCHEMA_VERSION = "retrieval-release-proposal-v1"
PROPOSAL_INTENT_SCHEMA_VERSION = "retrieval-release-proposal-intent-v1"
DECISION_SCHEMA_VERSION = "retrieval-release-decision-v1"
DECISION_INTENT_SCHEMA_VERSION = "retrieval-release-decision-intent-v1"
OUTCOME_SCHEMA_VERSION = "retrieval-release-outcome-v1"
OUTCOME_INTENT_SCHEMA_VERSION = "retrieval-release-outcome-intent-v1"
ROLLBACK_SCHEMA_VERSION = "retrieval-release-rollback-v1"
ROLLBACK_INTENT_SCHEMA_VERSION = "retrieval-release-rollback-intent-v1"
CATALOG_SCHEMA_VERSION = "retrieval-release-catalog-v1"
ACTIVATION_RECEIPT_SCHEMA_VERSION = "retrieval-serving-activation-receipt-v1"
VALIDATION_FAILURE_SCHEMA_VERSION = "retrieval-serving-validation-failure-v1"
SERVING_ROLLBACK_RECEIPT_SCHEMA_VERSION = "retrieval-serving-rollback-receipt-v1"

PROPOSAL_ID_PATTERN = re.compile(r"retrieval-proposal-[0-9a-f]{12}\Z")
DECISION_ID_PATTERN = re.compile(r"retrieval-decision-[0-9a-f]{12}\Z")
OUTCOME_ID_PATTERN = re.compile(r"retrieval-outcome-[0-9a-f]{12}\Z")
ROLLBACK_ID_PATTERN = re.compile(r"retrieval-rollback-[0-9a-f]{12}\Z")
ACTIVATION_RECEIPT_ID_PATTERN = re.compile(r"activation-[0-9a-f]{12}\Z")
VALIDATION_FAILURE_ID_PATTERN = re.compile(r"validation-failure-[0-9a-f]{12}\Z")
SERVING_ROLLBACK_RECEIPT_ID_PATTERN = re.compile(r"rollback-[0-9a-f]{12}\Z")
RUN_ID_PATTERN = r"retrieval-[0-9a-f]{12}"
COMPARISON_ID_PATTERN = r"retrieval-comparison-[0-9a-f]{12}"
DIAGNOSIS_ID_PATTERN = r"stage-diagnosis-[0-9a-f]{12}"
PIPELINE_ID_PATTERN = r"pipeline-[0-9a-f]{12}"
TRACE_ID_PATTERN = r"[0-9a-f]{32}"
SHA256_FIELD_PATTERN = r"^[0-9a-f]{64}$"
CODE_REVISION_FIELD_PATTERN = r"^[0-9a-f]{40}$"
SAFE_COMPONENT_PATTERN = r"^[a-z][a-z0-9-]{0,63}$"
SAFE_ACTION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
SAFE_ACTOR_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._@:-]{0,127}$"
SAFE_ERROR_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"

SELECTED_STRATEGY_ID = "multi-field-bm25-weighted-rrf-v1"
SELECTED_PIPELINE_VARIANT = "title-exact-multifield-weighted-v1"
MAX_RELEASE_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_RELEASE_PROPOSALS = 1_000
MAX_PUBLIC_RELEASES = 100
RELEASE_LOCK_TIMEOUT_SECONDS = 5.0

FiniteNonNegative = Annotated[
    float,
    Field(strict=True, allow_inf_nan=False, ge=0.0),
]


class RetrievalReleaseError(ValueError):
    """Stable public failure category for the release control plane."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SelectedPipeline(StrictModel):
    strategy_id: Literal[SELECTED_STRATEGY_ID] = SELECTED_STRATEGY_ID
    pipeline_id: StrictStr = Field(pattern=rf"^{PIPELINE_ID_PATTERN}$")
    config: dict[str, Any]
    config_sha256: StrictStr = Field(pattern=SHA256_FIELD_PATTERN)

    @model_validator(mode="after")
    def validate_config_identity(self) -> Self:
        _ensure_json_value(self.config)
        if self.config.get("variant") != SELECTED_PIPELINE_VARIANT:
            raise ValueError("selected retrieval pipeline variant is not releasable")
        if self.config_sha256 != _sha256_payload(self.config):
            raise ValueError("selected retrieval config hash does not match config")
        if self.pipeline_id != f"pipeline-{self.config_sha256[:12]}":
            raise ValueError("selected retrieval pipeline ID does not match config")
        return self


class RetrievalEvidenceRefs(StrictModel):
    baseline_run_id: StrictStr = Field(pattern=rf"^{RUN_ID_PATTERN}$")
    candidate_run_id: StrictStr = Field(pattern=rf"^{RUN_ID_PATTERN}$")
    comparison_id: StrictStr = Field(pattern=rf"^{COMPARISON_ID_PATTERN}$")
    baseline_diagnosis_id: StrictStr = Field(pattern=rf"^{DIAGNOSIS_ID_PATTERN}$")
    candidate_diagnosis_id: StrictStr = Field(pattern=rf"^{DIAGNOSIS_ID_PATTERN}$")
    trace_id: StrictStr = Field(pattern=rf"^{TRACE_ID_PATTERN}$")

    @model_validator(mode="after")
    def validate_distinct_evidence(self) -> Self:
        if self.baseline_run_id == self.candidate_run_id:
            raise ValueError("retrieval proposal baseline and candidate must differ")
        if self.baseline_diagnosis_id == self.candidate_diagnosis_id:
            raise ValueError("retrieval proposal diagnoses must differ")
        return self


class RetrievalReleaseProposal(StrictModel):
    schema_version: Literal[PROPOSAL_SCHEMA_VERSION] = PROPOSAL_SCHEMA_VERSION
    proposal_id: StrictStr = Field(pattern=r"^retrieval-proposal-[0-9a-f]{12}$")
    proposal_revision: StrictStr = Field(pattern=SHA256_FIELD_PATTERN)
    lifecycle: Literal["pending_owner_review"] = "pending_owner_review"
    approval_eligible: Literal[True] = True
    profile: Literal["smoke"] = "smoke"
    analysis_schema_version: Literal["retrieval-stage-analysis-response-v1"] = (
        "retrieval-stage-analysis-response-v1"
    )
    analysis_status: Literal["proposal_ready"] = "proposal_ready"
    trace_terminal_reason_code: StrictStr = Field(pattern=SAFE_ERROR_PATTERN)
    code_revision: StrictStr = Field(pattern=CODE_REVISION_FIELD_PATTERN)
    parent_active_revision: StrictStr | None = Field(
        default=None,
        pattern=SHA256_FIELD_PATTERN,
    )
    selected_pipeline: SelectedPipeline
    evidence: RetrievalEvidenceRefs
    release_gate: CandidateGateSummary

    @model_validator(mode="after")
    def validate_proposal_identity(self) -> Self:
        if self.release_gate.passed is not True:
            raise ValueError("only a gate-passing retrieval candidate is reviewable")
        body = self.model_dump(
            mode="json",
            exclude={"proposal_id", "proposal_revision"},
        )
        expected_revision = _sha256_payload(body)
        if self.proposal_revision != expected_revision:
            raise ValueError("retrieval proposal revision does not match content")
        if self.proposal_id != f"retrieval-proposal-{expected_revision[:12]}":
            raise ValueError("retrieval proposal ID does not match content")
        return self


class RetrievalProposalIntent(StrictModel):
    schema_version: Literal[PROPOSAL_INTENT_SCHEMA_VERSION] = (
        PROPOSAL_INTENT_SCHEMA_VERSION
    )
    trace_id: StrictStr = Field(pattern=rf"^{TRACE_ID_PATTERN}$")
    proposal_id: StrictStr = Field(pattern=r"^retrieval-proposal-[0-9a-f]{12}$")
    proposal_revision: StrictStr = Field(pattern=SHA256_FIELD_PATTERN)
    parent_active_revision: StrictStr | None = Field(
        default=None,
        pattern=SHA256_FIELD_PATTERN,
    )
    code_revision: StrictStr = Field(pattern=CODE_REVISION_FIELD_PATTERN)
    config_sha256: StrictStr = Field(pattern=SHA256_FIELD_PATTERN)
    baseline_run_id: StrictStr = Field(pattern=rf"^{RUN_ID_PATTERN}$")
    candidate_run_id: StrictStr = Field(pattern=rf"^{RUN_ID_PATTERN}$")
    comparison_id: StrictStr = Field(pattern=rf"^{COMPARISON_ID_PATTERN}$")


class RetrievalReleaseDecision(StrictModel):
    schema_version: Literal[DECISION_SCHEMA_VERSION] = DECISION_SCHEMA_VERSION
    decision_id: StrictStr = Field(pattern=r"^retrieval-decision-[0-9a-f]{12}$")
    proposal_id: StrictStr = Field(pattern=r"^retrieval-proposal-[0-9a-f]{12}$")
    proposal_revision: StrictStr = Field(pattern=SHA256_FIELD_PATTERN)
    decision: Literal["approve", "reject"]
    lifecycle: Literal["approved_for_validation", "rejected"]
    client_action_id: StrictStr = Field(pattern=SAFE_ACTION_PATTERN)
    actor_id: StrictStr = Field(pattern=SAFE_ACTOR_PATTERN)
    strategy_id: Literal[SELECTED_STRATEGY_ID] = SELECTED_STRATEGY_ID
    pipeline_id: StrictStr = Field(pattern=rf"^{PIPELINE_ID_PATTERN}$")
    config_sha256: StrictStr = Field(pattern=SHA256_FIELD_PATTERN)
    parent_active_revision: StrictStr | None = Field(
        default=None,
        pattern=SHA256_FIELD_PATTERN,
    )
    validation_required: StrictBool
    activation_status: Literal["not_active"] = "not_active"

    @model_validator(mode="after")
    def validate_decision_semantics(self) -> Self:
        expected_lifecycle = (
            "approved_for_validation" if self.decision == "approve" else "rejected"
        )
        if self.lifecycle != expected_lifecycle:
            raise ValueError("retrieval decision lifecycle is inconsistent")
        if self.validation_required is not (self.decision == "approve"):
            raise ValueError("retrieval decision validation flag is inconsistent")
        body = self.model_dump(mode="json", exclude={"decision_id"})
        if self.decision_id != _content_id("retrieval-decision", body):
            raise ValueError("retrieval decision ID does not match content")
        return self


class RetrievalDecisionIntent(StrictModel):
    schema_version: Literal[DECISION_INTENT_SCHEMA_VERSION] = (
        DECISION_INTENT_SCHEMA_VERSION
    )
    proposal_id: StrictStr = Field(pattern=r"^retrieval-proposal-[0-9a-f]{12}$")
    proposal_revision: StrictStr = Field(pattern=SHA256_FIELD_PATTERN)
    decision: Literal["approve", "reject"]
    client_action_id: StrictStr = Field(pattern=SAFE_ACTION_PATTERN)
    actor_id: StrictStr = Field(pattern=SAFE_ACTOR_PATTERN)
    decision_id: StrictStr = Field(pattern=r"^retrieval-decision-[0-9a-f]{12}$")


class ActivationChannelCounts(StrictModel):
    title: StrictInt = Field(ge=0)
    exact: StrictInt = Field(ge=0)
    multi_field: StrictInt = Field(ge=0)
    union: StrictInt = Field(ge=0)
    fused: StrictInt = Field(ge=0)
    coarse: StrictInt = Field(ge=0)


class ActivationLatency(StrictModel):
    max: FiniteNonNegative
    p95: FiniteNonNegative
    total: FiniteNonNegative

    @model_validator(mode="after")
    def validate_latency_order(self) -> Self:
        if self.p95 > self.max or self.max > self.total:
            raise ValueError("activation latency summary is inconsistent")
        return self


class RetrievalServingActivationReceipt(StrictModel):
    schema_version: Literal[ACTIVATION_RECEIPT_SCHEMA_VERSION] = (
        ACTIVATION_RECEIPT_SCHEMA_VERSION
    )
    receipt_id: StrictStr = Field(pattern=r"^activation-[0-9a-f]{12}$")
    proposal_id: StrictStr = Field(pattern=r"^retrieval-proposal-[0-9a-f]{12}$")
    proposal_revision: StrictStr = Field(pattern=SHA256_FIELD_PATTERN)
    strategy_id: Literal[SELECTED_STRATEGY_ID] = SELECTED_STRATEGY_ID
    strategy_revision: StrictStr = Field(pattern=SHA256_FIELD_PATTERN)
    pipeline_id: StrictStr = Field(pattern=rf"^{PIPELINE_ID_PATTERN}$")
    config_sha256: StrictStr = Field(pattern=SHA256_FIELD_PATTERN)
    index_id: StrictStr = Field(pattern=SAFE_COMPONENT_PATTERN)
    index_schema_version: StrictStr = Field(pattern=SAFE_COMPONENT_PATTERN)
    parent_active_revision: StrictStr | None = Field(
        default=None,
        pattern=SHA256_FIELD_PATTERN,
    )
    previous_strategy_revision: StrictStr | None = Field(
        default=None,
        pattern=SHA256_FIELD_PATTERN,
    )
    rollback_strategy_revision: StrictStr = Field(pattern=SHA256_FIELD_PATTERN)
    code_revision: StrictStr = Field(pattern=CODE_REVISION_FIELD_PATTERN)
    channel_counts: ActivationChannelCounts
    sentinel_count: StrictInt = Field(ge=1, le=10_000)
    latency_ms: ActivationLatency
    passed: Literal[True] = True
    active: Literal[True] = True

    @model_validator(mode="after")
    def validate_receipt_identity(self) -> Self:
        if self.previous_strategy_revision != self.parent_active_revision:
            raise ValueError("activation receipt parent revision is inconsistent")
        body = self.model_dump(mode="json", exclude={"receipt_id"})
        if self.receipt_id != _content_id("activation", body):
            raise ValueError("activation receipt ID does not match content")
        return self


class RetrievalServingValidationFailure(StrictModel):
    schema_version: Literal[VALIDATION_FAILURE_SCHEMA_VERSION] = (
        VALIDATION_FAILURE_SCHEMA_VERSION
    )
    receipt_id: StrictStr = Field(pattern=r"^validation-failure-[0-9a-f]{12}$")
    proposal_id: StrictStr = Field(pattern=r"^retrieval-proposal-[0-9a-f]{12}$")
    proposal_revision: StrictStr = Field(pattern=SHA256_FIELD_PATTERN)
    strategy_id: Literal[SELECTED_STRATEGY_ID] = SELECTED_STRATEGY_ID
    pipeline_id: StrictStr = Field(pattern=rf"^{PIPELINE_ID_PATTERN}$")
    config_sha256: StrictStr = Field(pattern=SHA256_FIELD_PATTERN)
    parent_active_revision: StrictStr | None = Field(
        default=None,
        pattern=SHA256_FIELD_PATTERN,
    )
    code_revision: StrictStr = Field(pattern=CODE_REVISION_FIELD_PATTERN)
    error_code: StrictStr = Field(pattern=SAFE_ERROR_PATTERN)
    passed: Literal[False] = False
    active: Literal[False] = False

    @model_validator(mode="after")
    def validate_receipt_identity(self) -> Self:
        body = self.model_dump(mode="json", exclude={"receipt_id"})
        if self.receipt_id != _content_id("validation-failure", body):
            raise ValueError("validation failure receipt ID does not match content")
        return self


class RetrievalReleaseOutcome(StrictModel):
    schema_version: Literal[OUTCOME_SCHEMA_VERSION] = OUTCOME_SCHEMA_VERSION
    outcome_id: StrictStr = Field(pattern=r"^retrieval-outcome-[0-9a-f]{12}$")
    proposal_id: StrictStr = Field(pattern=r"^retrieval-proposal-[0-9a-f]{12}$")
    proposal_revision: StrictStr = Field(pattern=SHA256_FIELD_PATTERN)
    decision_id: StrictStr = Field(pattern=r"^retrieval-decision-[0-9a-f]{12}$")
    lifecycle: Literal["active", "validation_failed"]
    validation_receipt: dict[str, Any]
    validation_receipt_sha256: StrictStr = Field(pattern=SHA256_FIELD_PATTERN)
    active_strategy_revision: StrictStr | None = Field(
        default=None,
        pattern=SHA256_FIELD_PATTERN,
    )
    strategy_id: Literal[SELECTED_STRATEGY_ID] = SELECTED_STRATEGY_ID
    pipeline_id: StrictStr = Field(pattern=rf"^{PIPELINE_ID_PATTERN}$")
    config_sha256: StrictStr = Field(pattern=SHA256_FIELD_PATTERN)

    @model_validator(mode="after")
    def validate_outcome_semantics(self) -> Self:
        _ensure_json_value(self.validation_receipt)
        if self.validation_receipt_sha256 != _sha256_payload(self.validation_receipt):
            raise ValueError("retrieval outcome receipt hash is invalid")
        if (self.lifecycle == "active") != (self.active_strategy_revision is not None):
            raise ValueError("retrieval outcome active revision is inconsistent")
        body = self.model_dump(mode="json", exclude={"outcome_id"})
        if self.outcome_id != _content_id("retrieval-outcome", body):
            raise ValueError("retrieval outcome ID does not match content")
        return self


class RetrievalOutcomeIntent(StrictModel):
    schema_version: Literal[OUTCOME_INTENT_SCHEMA_VERSION] = (
        OUTCOME_INTENT_SCHEMA_VERSION
    )
    proposal_id: StrictStr = Field(pattern=r"^retrieval-proposal-[0-9a-f]{12}$")
    proposal_revision: StrictStr = Field(pattern=SHA256_FIELD_PATTERN)
    outcome_id: StrictStr = Field(pattern=r"^retrieval-outcome-[0-9a-f]{12}$")
    lifecycle: Literal["active", "validation_failed"]
    validation_receipt_sha256: StrictStr = Field(pattern=SHA256_FIELD_PATTERN)
    active_strategy_revision: StrictStr | None = Field(
        default=None,
        pattern=SHA256_FIELD_PATTERN,
    )


class RetrievalServingRollbackReceipt(StrictModel):
    schema_version: Literal[SERVING_ROLLBACK_RECEIPT_SCHEMA_VERSION] = (
        SERVING_ROLLBACK_RECEIPT_SCHEMA_VERSION
    )
    receipt_id: StrictStr = Field(pattern=r"^rollback-[0-9a-f]{12}$")
    from_strategy_revision: StrictStr = Field(pattern=SHA256_FIELD_PATTERN)
    index_id: StrictStr = Field(pattern=r"^[a-z][a-z0-9-]{0,127}$")
    index_schema_version: StrictStr = Field(pattern=r"^[a-z][a-z0-9-]{0,127}$")
    mode: Literal["baseline", "v2"]
    strategy_id: StrictStr = Field(pattern=r"^[a-z][a-z0-9-]{0,127}$")
    strategy_revision: StrictStr = Field(pattern=SHA256_FIELD_PATTERN)
    succeeded: Literal[True] = True

    @model_validator(mode="after")
    def validate_receipt_identity(self) -> Self:
        if self.from_strategy_revision == self.strategy_revision:
            raise ValueError("rollback must move to a different strategy revision")
        body = self.model_dump(mode="json", exclude={"receipt_id"})
        if self.receipt_id != _content_id("rollback", body):
            raise ValueError("rollback receipt ID does not match content")
        return self


class RetrievalReleaseRollback(StrictModel):
    schema_version: Literal[ROLLBACK_SCHEMA_VERSION] = ROLLBACK_SCHEMA_VERSION
    rollback_id: StrictStr = Field(pattern=r"^retrieval-rollback-[0-9a-f]{12}$")
    proposal_id: StrictStr = Field(pattern=r"^retrieval-proposal-[0-9a-f]{12}$")
    proposal_revision: StrictStr = Field(pattern=SHA256_FIELD_PATTERN)
    outcome_id: StrictStr = Field(pattern=r"^retrieval-outcome-[0-9a-f]{12}$")
    lifecycle: Literal["rolled_back"] = "rolled_back"
    from_strategy_revision: StrictStr = Field(pattern=SHA256_FIELD_PATTERN)
    target_strategy_revision: StrictStr = Field(pattern=SHA256_FIELD_PATTERN)
    rollback_receipt: dict[str, Any]
    rollback_receipt_sha256: StrictStr = Field(pattern=SHA256_FIELD_PATTERN)

    @model_validator(mode="after")
    def validate_rollback_semantics(self) -> Self:
        _ensure_json_value(self.rollback_receipt)
        if self.rollback_receipt_sha256 != _sha256_payload(self.rollback_receipt):
            raise ValueError("release rollback receipt hash is invalid")
        parsed = RetrievalServingRollbackReceipt.model_validate(
            self.rollback_receipt,
            strict=True,
        )
        if (
            parsed.from_strategy_revision != self.from_strategy_revision
            or parsed.strategy_revision != self.target_strategy_revision
        ):
            raise ValueError("release rollback receipt revisions are inconsistent")
        if self.from_strategy_revision == self.target_strategy_revision:
            raise ValueError("release rollback did not change strategy revision")
        body = self.model_dump(mode="json", exclude={"rollback_id"})
        if self.rollback_id != _content_id("retrieval-rollback", body):
            raise ValueError("release rollback ID does not match content")
        return self


class RetrievalRollbackIntent(StrictModel):
    schema_version: Literal[ROLLBACK_INTENT_SCHEMA_VERSION] = (
        ROLLBACK_INTENT_SCHEMA_VERSION
    )
    proposal_id: StrictStr = Field(pattern=r"^retrieval-proposal-[0-9a-f]{12}$")
    proposal_revision: StrictStr = Field(pattern=SHA256_FIELD_PATTERN)
    rollback_id: StrictStr = Field(pattern=r"^retrieval-rollback-[0-9a-f]{12}$")
    from_strategy_revision: StrictStr = Field(pattern=SHA256_FIELD_PATTERN)
    target_strategy_revision: StrictStr = Field(pattern=SHA256_FIELD_PATTERN)
    rollback_receipt_sha256: StrictStr = Field(pattern=SHA256_FIELD_PATTERN)


class _ValidatedAnalysis(StrictModel):
    """Private, bounded projection rebuilt from authoritative artifacts."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    code_revision: StrictStr = Field(pattern=CODE_REVISION_FIELD_PATTERN)
    selected_pipeline: SelectedPipeline
    evidence: RetrievalEvidenceRefs
    release_gate: CandidateGateSummary
    trace_terminal_reason_code: StrictStr = Field(pattern=SAFE_ERROR_PATTERN)


def create_or_load_retrieval_proposal(
    analysis: Mapping[str, Any],
    *,
    project_root: str | Path,
    artifact_root: str | Path | None = None,
    revision_provider: Callable[[Path], str] = require_clean_code_revision,
) -> dict[str, Any]:
    """Persist one formal proposal for a replay-validated Runtime analysis.

    The same Trace is anchored to the same parent active revision on its first
    call.  Later retries load that proposal even if serving state has moved.
    """

    safe_trace_id = _safe_trace_id_from_analysis(analysis)
    logger.info(
        "retrieval_release_proposal_started",
        extra={"agent_trace_id": safe_trace_id, "profile_id": "smoke"},
    )
    try:
        root = _resolve_project_root(project_root)
        run_store = _resolve_artifact_root(root, artifact_root)
        validated = _validate_analysis(
            analysis,
            project_root=root,
            run_store=run_store,
            revision_provider=revision_provider,
        )
        with _release_lock(run_store):
            intent_path = _proposal_intent_path(
                run_store,
                validated.evidence.trace_id,
            )
            if intent_path.exists() or intent_path.is_symlink():
                intent = RetrievalProposalIntent.model_validate(
                    _load_bounded_json_object(intent_path),
                    strict=True,
                )
                _validate_intent_matches_analysis(intent, validated)
                proposal = _build_proposal(
                    validated,
                    parent_active_revision=intent.parent_active_revision,
                )
                if (
                    proposal.proposal_id != intent.proposal_id
                    or proposal.proposal_revision != intent.proposal_revision
                ):
                    raise RetrievalReleaseError(
                        "proposal_intent_mismatch",
                        "retrieval proposal intent does not match evidence",
                    )
                stored = _store_or_load_proposal(run_store, proposal)
                logger.info(
                    "retrieval_release_proposal_loaded",
                    extra={
                        "agent_trace_id": validated.evidence.trace_id,
                        "proposal_id": stored.proposal_id,
                    },
                )
                return stored.model_dump(mode="json")

            parent_revision = _load_active_revision(run_store)
            proposal = _build_proposal(
                validated,
                parent_active_revision=parent_revision,
            )
            intent = RetrievalProposalIntent(
                trace_id=validated.evidence.trace_id,
                proposal_id=proposal.proposal_id,
                proposal_revision=proposal.proposal_revision,
                parent_active_revision=proposal.parent_active_revision,
                code_revision=proposal.code_revision,
                config_sha256=proposal.selected_pipeline.config_sha256,
                baseline_run_id=proposal.evidence.baseline_run_id,
                candidate_run_id=proposal.evidence.candidate_run_id,
                comparison_id=proposal.evidence.comparison_id,
            )
            write_immutable_json(intent_path, intent.model_dump(mode="json"))
            stored = _store_or_load_proposal(run_store, proposal)
        logger.info(
            "retrieval_release_proposal_stored",
            extra={
                "agent_trace_id": stored.evidence.trace_id,
                "baseline_run_id": stored.evidence.baseline_run_id,
                "candidate_run_id": stored.evidence.candidate_run_id,
                "comparison_id": stored.evidence.comparison_id,
                "config_sha256": stored.selected_pipeline.config_sha256,
                "has_parent_active_revision": (
                    stored.parent_active_revision is not None
                ),
                "pipeline_id": stored.selected_pipeline.pipeline_id,
                "proposal_id": stored.proposal_id,
            },
        )
        return stored.model_dump(mode="json")
    except Exception as exc:
        _log_failure("proposal", exc, proposal_id=None, trace_id=safe_trace_id)
        raise


def apply_retrieval_release_decision(
    *,
    project_root: str | Path,
    artifact_root: str | Path | None = None,
    proposal_id: str,
    proposal_revision: str,
    decision: Literal["approve", "reject"],
    client_action_id: str,
    actor_id: str,
    revision_provider: Callable[[Path], str] = require_clean_code_revision,
) -> dict[str, Any]:
    """Record exactly one idempotent Owner decision for a proposal revision."""

    logger.info(
        "retrieval_release_decision_started",
        extra={"decision": decision, "proposal_id": _safe_proposal_id(proposal_id)},
    )
    try:
        root = _resolve_project_root(project_root)
        run_store = _resolve_artifact_root(root, artifact_root)
        _validate_decision_request(
            proposal_id=proposal_id,
            proposal_revision=proposal_revision,
            decision=decision,
            client_action_id=client_action_id,
            actor_id=actor_id,
        )
        with _release_lock(run_store):
            proposal = _load_proposal(run_store, proposal_id)
            if proposal.proposal_revision != proposal_revision:
                raise RetrievalReleaseError(
                    "proposal_revision_conflict",
                    "proposal revision does not match the stored proposal",
                )
            candidate = _build_decision(
                proposal,
                decision=decision,
                client_action_id=client_action_id,
                actor_id=actor_id,
            )
            existing = _load_existing_decision(run_store, proposal_id)
            action_existing = _load_client_action_decision(
                run_store,
                client_action_id,
            )
            for stored in (existing, action_existing):
                if stored is None:
                    continue
                if stored != candidate:
                    raise RetrievalReleaseError(
                        "decision_idempotency_conflict",
                        "proposal or client action already has a different decision",
                    )
            if existing is not None or action_existing is not None:
                if existing is None or action_existing is None:
                    _complete_decision_publication(run_store, candidate)
                logger.info(
                    "retrieval_release_decision_replayed",
                    extra={
                        "decision": candidate.decision,
                        "decision_id": candidate.decision_id,
                        "proposal_id": candidate.proposal_id,
                    },
                )
                return candidate.model_dump(mode="json")

            if decision == "approve":
                _validate_approval(
                    proposal,
                    project_root=root,
                    run_store=run_store,
                    revision_provider=revision_provider,
                )
            intent = RetrievalDecisionIntent(
                proposal_id=proposal.proposal_id,
                proposal_revision=proposal.proposal_revision,
                decision=candidate.decision,
                client_action_id=candidate.client_action_id,
                actor_id=candidate.actor_id,
                decision_id=candidate.decision_id,
            )
            _prepare_decision_intent(run_store, intent)
            _complete_decision_publication(run_store, candidate)
        logger.info(
            "retrieval_release_decision_recorded",
            extra={
                "decision": candidate.decision,
                "decision_id": candidate.decision_id,
                "lifecycle": candidate.lifecycle,
                "proposal_id": candidate.proposal_id,
                "validation_required": candidate.validation_required,
            },
        )
        return candidate.model_dump(mode="json")
    except Exception as exc:
        _log_failure(
            "decision",
            exc,
            proposal_id=_safe_proposal_id(proposal_id),
            trace_id=None,
        )
        raise


def record_retrieval_release_outcome(
    *,
    project_root: str | Path,
    artifact_root: str | Path | None = None,
    proposal_id: str,
    proposal_revision: str,
    outcome: Literal["active", "validation_failed"],
    validation_receipt: Mapping[str, Any],
    active_strategy_revision: str | None = None,
) -> dict[str, Any]:
    """Record the serving validator's terminal result after Owner approval."""

    logger.info(
        "retrieval_release_outcome_started",
        extra={"outcome": outcome, "proposal_id": _safe_proposal_id(proposal_id)},
    )
    try:
        root = _resolve_project_root(project_root)
        run_store = _resolve_artifact_root(root, artifact_root)
        if not PROPOSAL_ID_PATTERN.fullmatch(proposal_id):
            raise RetrievalReleaseError("invalid_proposal_id", "invalid proposal ID")
        if not re.fullmatch(SHA256_FIELD_PATTERN, proposal_revision):
            raise RetrievalReleaseError(
                "invalid_proposal_revision", "invalid proposal revision"
            )
        if outcome not in {"active", "validation_failed"}:
            raise RetrievalReleaseError("invalid_outcome", "invalid release outcome")
        with _release_lock(run_store):
            proposal = _load_proposal(run_store, proposal_id)
            if proposal.proposal_revision != proposal_revision:
                raise RetrievalReleaseError(
                    "proposal_revision_conflict",
                    "proposal revision does not match the stored proposal",
                )
            decision = _load_existing_decision(run_store, proposal_id)
            if decision is None or decision.lifecycle != "approved_for_validation":
                raise RetrievalReleaseError(
                    "release_not_approved",
                    "only an approved proposal may record validation",
                )
            receipt_payload = dict(validation_receipt)
            _ensure_json_value(receipt_payload)
            _validate_validation_receipt(
                proposal,
                receipt_payload,
                outcome=outcome,
                active_strategy_revision=active_strategy_revision,
                run_store=run_store,
            )
            release_outcome = _build_outcome(
                proposal,
                decision,
                outcome=outcome,
                validation_receipt=receipt_payload,
                active_strategy_revision=active_strategy_revision,
            )
            existing = _load_existing_outcome(run_store, proposal_id)
            if existing is not None:
                if existing != release_outcome:
                    raise RetrievalReleaseError(
                        "outcome_idempotency_conflict",
                        "proposal already has a different release outcome",
                    )
                logger.info(
                    "retrieval_release_outcome_replayed",
                    extra={
                        "lifecycle": existing.lifecycle,
                        "outcome_id": existing.outcome_id,
                        "proposal_id": existing.proposal_id,
                    },
                )
                return existing.model_dump(mode="json")
            intent = RetrievalOutcomeIntent(
                proposal_id=release_outcome.proposal_id,
                proposal_revision=release_outcome.proposal_revision,
                outcome_id=release_outcome.outcome_id,
                lifecycle=release_outcome.lifecycle,
                validation_receipt_sha256=(release_outcome.validation_receipt_sha256),
                active_strategy_revision=release_outcome.active_strategy_revision,
            )
            _prepare_outcome_intent(run_store, intent)
            _complete_outcome_publication(run_store, release_outcome)
        logger.info(
            "retrieval_release_outcome_recorded",
            extra={
                "active_strategy_revision": (release_outcome.active_strategy_revision),
                "lifecycle": release_outcome.lifecycle,
                "outcome_id": release_outcome.outcome_id,
                "proposal_id": release_outcome.proposal_id,
                "validation_receipt_id": receipt_payload["receipt_id"],
            },
        )
        return release_outcome.model_dump(mode="json")
    except Exception as exc:
        _log_failure(
            "outcome",
            exc,
            proposal_id=_safe_proposal_id(proposal_id),
            trace_id=None,
        )
        raise


def record_retrieval_release_rollback(
    *,
    project_root: str | Path,
    artifact_root: str | Path | None = None,
    proposal_id: str,
    proposal_revision: str,
    rollback_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Record a serving rollback and project the release as no longer active.

    Serving owns the atomic pointer change.  This control-plane record is
    accepted only after that pointer names the receipt's target revision.  An
    exact retry returns the immutable record even if serving later moves on.
    """

    logger.info(
        "retrieval_release_rollback_started",
        extra={"proposal_id": _safe_proposal_id(proposal_id)},
    )
    try:
        root = _resolve_project_root(project_root)
        run_store = _resolve_artifact_root(root, artifact_root)
        if not isinstance(proposal_id, str) or not PROPOSAL_ID_PATTERN.fullmatch(
            proposal_id
        ):
            raise RetrievalReleaseError("invalid_proposal_id", "invalid proposal ID")
        if not isinstance(proposal_revision, str) or not re.fullmatch(
            SHA256_FIELD_PATTERN, proposal_revision
        ):
            raise RetrievalReleaseError(
                "invalid_proposal_revision", "invalid proposal revision"
            )
        if not isinstance(rollback_receipt, Mapping):
            raise RetrievalReleaseError(
                "rollback_receipt_invalid", "serving rollback receipt is invalid"
            )
        receipt_payload = dict(rollback_receipt)
        _ensure_json_value(receipt_payload)
        with _release_lock(run_store):
            proposal = _load_proposal(run_store, proposal_id)
            if proposal.proposal_revision != proposal_revision:
                raise RetrievalReleaseError(
                    "proposal_revision_conflict",
                    "proposal revision does not match the stored proposal",
                )
            decision = _load_existing_decision(run_store, proposal_id)
            outcome = _load_existing_outcome(run_store, proposal_id)
            if decision is None or decision.lifecycle != "approved_for_validation":
                raise RetrievalReleaseError(
                    "release_not_approved",
                    "only an approved release may record a rollback",
                )
            if outcome is None or outcome.lifecycle != "active":
                raise RetrievalReleaseError(
                    "release_not_active",
                    "only an activated release may record a rollback",
                )
            parsed_receipt = _validate_rollback_receipt(
                proposal,
                outcome,
                receipt_payload,
            )
            rollback = _build_rollback(
                proposal,
                outcome,
                rollback_receipt=parsed_receipt.model_dump(mode="json"),
            )
            existing = _load_existing_rollback(run_store, proposal_id)
            if existing is not None:
                if existing != rollback:
                    raise RetrievalReleaseError(
                        "rollback_idempotency_conflict",
                        "proposal already has a different rollback record",
                    )
                logger.info(
                    "retrieval_release_rollback_replayed",
                    extra={
                        "proposal_id": existing.proposal_id,
                        "rollback_id": existing.rollback_id,
                        "target_strategy_revision": (existing.target_strategy_revision),
                    },
                )
                return existing.model_dump(mode="json")
            if _load_active_revision(run_store) != parsed_receipt.strategy_revision:
                raise RetrievalReleaseError(
                    "rollback_pointer_conflict",
                    "active serving pointer differs from rollback receipt",
                )
            intent = RetrievalRollbackIntent(
                proposal_id=rollback.proposal_id,
                proposal_revision=rollback.proposal_revision,
                rollback_id=rollback.rollback_id,
                from_strategy_revision=rollback.from_strategy_revision,
                target_strategy_revision=rollback.target_strategy_revision,
                rollback_receipt_sha256=rollback.rollback_receipt_sha256,
            )
            _prepare_rollback_intent(run_store, intent)
            _complete_rollback_publication(run_store, rollback)
        logger.info(
            "retrieval_release_rollback_recorded",
            extra={
                "from_strategy_revision": rollback.from_strategy_revision,
                "proposal_id": rollback.proposal_id,
                "rollback_id": rollback.rollback_id,
                "rollback_receipt_id": parsed_receipt.receipt_id,
                "target_strategy_revision": rollback.target_strategy_revision,
            },
        )
        return rollback.model_dump(mode="json")
    except Exception as exc:
        _log_failure(
            "rollback",
            exc,
            proposal_id=_safe_proposal_id(proposal_id),
            trace_id=None,
        )
        raise


def build_retrieval_validation_failure_receipt(
    proposal: Mapping[str, Any],
    *,
    error_code: str,
) -> dict[str, Any]:
    """Build a privacy-safe receipt when serving validation fails before activation."""

    validated = RetrievalReleaseProposal.model_validate(dict(proposal), strict=True)
    body = {
        "active": False,
        "code_revision": validated.code_revision,
        "config_sha256": validated.selected_pipeline.config_sha256,
        "error_code": error_code,
        "parent_active_revision": validated.parent_active_revision,
        "passed": False,
        "pipeline_id": validated.selected_pipeline.pipeline_id,
        "proposal_id": validated.proposal_id,
        "proposal_revision": validated.proposal_revision,
        "schema_version": VALIDATION_FAILURE_SCHEMA_VERSION,
        "strategy_id": validated.selected_pipeline.strategy_id,
    }
    payload = {**body, "receipt_id": _content_id("validation-failure", body)}
    return RetrievalServingValidationFailure.model_validate(
        payload,
        strict=True,
    ).model_dump(mode="json")


def load_retrieval_activation_envelope(
    *,
    project_root: str | Path,
    artifact_root: str | Path | None = None,
    proposal_id: str,
    proposal_revision: str,
) -> dict[str, Any]:
    """Return immutable proposal + approval evidence for the serving validator."""

    root = _resolve_project_root(project_root)
    run_store = _resolve_artifact_root(root, artifact_root)
    with _release_lock(run_store):
        proposal = _load_proposal(run_store, proposal_id)
        if proposal.proposal_revision != proposal_revision:
            raise RetrievalReleaseError(
                "proposal_revision_conflict",
                "proposal revision does not match the stored proposal",
            )
        decision = _load_existing_decision(run_store, proposal_id)
        if decision is None or decision.lifecycle != "approved_for_validation":
            raise RetrievalReleaseError(
                "release_not_approved",
                "retrieval release is not approved for validation",
            )
        if _load_existing_outcome(run_store, proposal_id) is not None:
            raise RetrievalReleaseError(
                "release_already_finalized",
                "retrieval release already has a validation outcome",
            )
        return {
            "proposal": proposal.model_dump(mode="json"),
            "decision": decision.model_dump(mode="json"),
        }


def load_retrieval_release(
    *,
    project_root: str | Path,
    artifact_root: str | Path | None = None,
    proposal_id: str,
    proposal_revision: str | None = None,
) -> dict[str, Any]:
    """Load one private release record and derive its current lifecycle."""

    root = _resolve_project_root(project_root)
    run_store = _resolve_artifact_root(root, artifact_root)
    with _release_lock(run_store):
        proposal = _load_proposal(run_store, proposal_id)
        if (
            proposal_revision is not None
            and proposal.proposal_revision != proposal_revision
        ):
            raise RetrievalReleaseError(
                "proposal_revision_conflict",
                "proposal revision does not match the stored proposal",
            )
        decision = _load_existing_decision(run_store, proposal_id)
        outcome = _load_existing_outcome(run_store, proposal_id)
        rollback = _load_existing_rollback(run_store, proposal_id)
        active_revision = _load_active_revision(run_store)
        return {
            "proposal": proposal.model_dump(mode="json"),
            "decision": decision.model_dump(mode="json") if decision else None,
            "outcome": outcome.model_dump(mode="json") if outcome else None,
            "rollback": rollback.model_dump(mode="json") if rollback else None,
            "lifecycle": _release_lifecycle(
                decision,
                outcome,
                rollback,
                active_revision=active_revision,
            ),
        }


def load_retrieval_release_catalog(
    *,
    project_root: str | Path,
    artifact_root: str | Path | None = None,
) -> dict[str, Any]:
    """Return a bounded public-safe projection of retrieval release history."""

    try:
        root = _resolve_project_root(project_root)
        run_store = _resolve_artifact_root(root, artifact_root)
        with _release_lock(run_store):
            proposal_dir = _release_directory(run_store, "retrieval-release-proposals")
            paths = sorted(proposal_dir.glob("retrieval-proposal-*.json"))
            if len(paths) > MAX_RELEASE_PROPOSALS:
                raise RetrievalReleaseError(
                    "release_catalog_too_large",
                    "retrieval release catalog exceeds its bounded size",
                )
            active_revision = _load_active_revision(run_store)
            releases: list[dict[str, Any]] = []
            active_release: dict[str, Any] | None = None
            for path in paths:
                proposal = _load_proposal(run_store, path.stem)
                decision = _load_existing_decision(run_store, proposal.proposal_id)
                outcome = _load_existing_outcome(run_store, proposal.proposal_id)
                rollback = _load_existing_rollback(run_store, proposal.proposal_id)
                entry = _safe_release_entry(
                    proposal,
                    decision,
                    outcome,
                    rollback,
                    active_revision=active_revision,
                )
                releases.append(entry)
                if entry["lifecycle"] == "active":
                    if active_release is not None:
                        raise RetrievalReleaseError(
                            "active_release_ambiguous",
                            "multiple releases claim the current active revision",
                        )
                    active_release = entry
            releases = list(reversed(releases[-MAX_PUBLIC_RELEASES:]))
        logger.info(
            "retrieval_release_catalog_loaded",
            extra={
                "active_release_present": active_release is not None,
                "release_count": len(releases),
            },
        )
        return {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "releases": releases,
            "active_retrieval_release": active_release,
        }
    except Exception as exc:
        _log_failure("catalog", exc, proposal_id=None, trace_id=None)
        raise


def _validate_analysis(
    analysis: Mapping[str, Any],
    *,
    project_root: Path,
    run_store: Path,
    revision_provider: Callable[[Path], str],
) -> _ValidatedAnalysis:
    if not isinstance(analysis, Mapping):
        raise RetrievalReleaseError(
            "analysis_invalid", "retrieval analysis must be an object"
        )
    value = dict(analysis)
    required = {
        "agent_run",
        "candidate_diagnosis",
        "candidate_diagnosis_id",
        "candidate_run_id",
        "comparison",
        "comparison_id",
        "diagnosis",
        "diagnosis_id",
        "profile",
        "proposal",
        "retrieval_run_id",
        "schema_version",
        "status",
    }
    if not required <= set(value):
        raise RetrievalReleaseError(
            "analysis_invalid", "retrieval analysis is missing required evidence"
        )
    if (
        value.get("schema_version") != "retrieval-stage-analysis-response-v1"
        or value.get("status") != "proposal_ready"
        or value.get("profile") != "smoke"
    ):
        raise RetrievalReleaseError(
            "analysis_not_reviewable", "retrieval analysis is not reviewable"
        )
    proposal_summary = value.get("proposal")
    if not isinstance(proposal_summary, dict) or (
        proposal_summary.get("candidate_strategy_id") != SELECTED_STRATEGY_ID
        or proposal_summary.get("decision") != "request_owner_review"
        or proposal_summary.get("next_action") != "request_owner_review"
    ):
        raise RetrievalReleaseError(
            "analysis_not_reviewable", "retrieval analysis did not request review"
        )

    baseline_run_id = _require_pattern(
        value.get("retrieval_run_id"),
        RUN_ID_PATTERN,
        "baseline Run ID",
    )
    candidate_run_id = _require_pattern(
        value.get("candidate_run_id"),
        RUN_ID_PATTERN,
        "candidate Run ID",
    )
    comparison_id = _require_pattern(
        value.get("comparison_id"),
        COMPARISON_ID_PATTERN,
        "comparison ID",
    )
    baseline_diagnosis_id = _require_pattern(
        value.get("diagnosis_id"),
        DIAGNOSIS_ID_PATTERN,
        "baseline diagnosis ID",
    )
    candidate_diagnosis_id = _require_pattern(
        value.get("candidate_diagnosis_id"),
        DIAGNOSIS_ID_PATTERN,
        "candidate diagnosis ID",
    )
    agent_run = value.get("agent_run")
    if not isinstance(agent_run, dict):
        raise RetrievalReleaseError("analysis_invalid", "Agent run is missing")
    trace_id = _require_pattern(
        agent_run.get("trace_id"),
        TRACE_ID_PATTERN,
        "Trace ID",
    )
    if (
        agent_run.get("schema_version") != "retrieval-agent-run-summary-v2"
        or agent_run.get("state") != "completed"
        or agent_run.get("outcome") != "proposal_ready"
        or agent_run.get("replay_mode") != "recorded_trace"
    ):
        raise RetrievalReleaseError(
            "analysis_trace_invalid", "Agent run is not replay-validated"
        )

    baseline = _load_evidence(run_store, "retrieval-runs", baseline_run_id)
    candidate = _load_evidence(run_store, "retrieval-runs", candidate_run_id)
    comparison = _load_evidence(
        run_store,
        "retrieval-comparisons",
        comparison_id,
    )
    baseline_diagnosis = _load_evidence(
        run_store,
        "stage-diagnoses",
        baseline_diagnosis_id,
    )
    candidate_diagnosis = _load_evidence(
        run_store,
        "stage-diagnoses",
        candidate_diagnosis_id,
    )
    try:
        validate_retrieval_run(baseline, role="retrieval release baseline")
        validate_retrieval_run(candidate, role="retrieval release candidate")
        rebuilt_comparison = compare_retrieval_runs(baseline, candidate)
        if rebuilt_comparison != comparison:
            raise ValueError("stored comparison does not match retrieval Runs")
        StageDiagnosis.model_validate(baseline_diagnosis, strict=True)
        StageDiagnosis.model_validate(candidate_diagnosis, strict=True)
        if diagnose_retrieval_stages(baseline) != baseline_diagnosis:
            raise ValueError("baseline diagnosis does not match its Run")
        if diagnose_retrieval_stages(candidate) != candidate_diagnosis:
            raise ValueError("candidate diagnosis does not match its Run")
    except (TypeError, ValueError) as exc:
        raise RetrievalReleaseError(
            "retrieval_evidence_invalid",
            "retrieval proposal evidence failed semantic validation",
        ) from exc

    if (
        comparison.get("comparison_id") != comparison_id
        or comparison.get("baseline_run_id") != baseline_run_id
        or comparison.get("candidate_run_id") != candidate_run_id
        or baseline_diagnosis.get("pipeline_run_id") != baseline_run_id
        or candidate_diagnosis.get("pipeline_run_id") != candidate_run_id
    ):
        raise RetrievalReleaseError(
            "retrieval_evidence_invalid", "retrieval evidence references conflict"
        )
    if (
        value.get("comparison") != comparison
        or value.get("diagnosis") != baseline_diagnosis
        or value.get("candidate_diagnosis") != candidate_diagnosis
    ):
        raise RetrievalReleaseError(
            "analysis_evidence_mismatch",
            "retrieval analysis projection differs from stored evidence",
        )
    if baseline.get("pipeline", {}).get("variant") != "title-exact-v1":
        raise RetrievalReleaseError(
            "baseline_pipeline_invalid", "retrieval baseline is not trusted"
        )
    candidate_pipeline = candidate.get("pipeline")
    pipeline_id = candidate.get("pipeline_id")
    if not isinstance(candidate_pipeline, dict) or not isinstance(pipeline_id, str):
        raise RetrievalReleaseError(
            "candidate_pipeline_invalid", "candidate pipeline is missing"
        )
    try:
        selected_pipeline = SelectedPipeline(
            pipeline_id=pipeline_id,
            config=candidate_pipeline,
            config_sha256=_sha256_payload(candidate_pipeline),
        )
        raw_gate = comparison.get("gate_result")
        if not isinstance(raw_gate, dict) or not isinstance(
            raw_gate.get("checks"), list
        ):
            raise ValueError("retrieval comparison gate is missing")
        release_gate = CandidateGateSummary.model_validate(
            {
                **raw_gate,
                "failed_gates": [
                    item.get("name")
                    for item in raw_gate["checks"]
                    if isinstance(item, dict) and item.get("passed") is not True
                ],
            },
            strict=True,
        )
    except (TypeError, ValueError) as exc:
        raise RetrievalReleaseError(
            "candidate_pipeline_invalid", "candidate pipeline is not releasable"
        ) from exc
    if release_gate.passed is not True:
        raise RetrievalReleaseError(
            "release_gate_failed", "candidate did not pass retrieval gates"
        )

    experiments = value.get("experiments")
    if not isinstance(experiments, list):
        raise RetrievalReleaseError(
            "analysis_invalid", "retrieval experiment summary is missing"
        )
    selected_summaries = [
        item
        for item in experiments
        if isinstance(item, dict)
        and item.get("candidate_run_id") == candidate_run_id
        and item.get("comparison_id") == comparison_id
    ]
    if len(selected_summaries) != 1 or (
        selected_summaries[0].get("pipeline_variant") != SELECTED_PIPELINE_VARIANT
        or selected_summaries[0].get("gate_passed") is not True
    ):
        raise RetrievalReleaseError(
            "analysis_selection_invalid",
            "retrieval analysis selected candidate is inconsistent",
        )

    trace_store = TraceStore(run_store / "agent-traces")
    try:
        trace = TraceReplayer(trace_store).replay_trace(trace_id)
    except (TypeError, ValueError, RuntimeError) as exc:
        raise RetrievalReleaseError(
            "analysis_trace_invalid", "retrieval Trace failed Replay"
        ) from exc
    terminal = trace.terminal
    decision_report = terminal.report.get("decision")
    if (
        terminal.state != "completed"
        or terminal.outcome != TerminalOutcome.PROPOSAL_READY
        or not isinstance(decision_report, dict)
        or decision_report.get("selected_comparison_id") != comparison_id
        or decision_report.get("selected_pipeline_variant") != SELECTED_PIPELINE_VARIANT
        or f"run:{baseline_run_id}" not in terminal.evidence_refs
        or f"comparison:{comparison_id}" not in terminal.evidence_refs
    ):
        raise RetrievalReleaseError(
            "analysis_trace_invalid", "retrieval Trace selected different evidence"
        )
    _validate_trace_observation_refs(
        trace,
        baseline_run_id=baseline_run_id,
        candidate_run_id=candidate_run_id,
        comparison_id=comparison_id,
        baseline_diagnosis_id=baseline_diagnosis_id,
        candidate_diagnosis_id=candidate_diagnosis_id,
    )

    try:
        revision = revision_provider(project_root).strip()
    except (OSError, RuntimeError) as exc:
        raise RetrievalReleaseError(
            "code_revision_unavailable",
            "retrieval proposal requires a clean code revision",
        ) from exc
    if not re.fullmatch(CODE_REVISION_FIELD_PATTERN, revision):
        raise RetrievalReleaseError(
            "code_revision_invalid", "code revision must be a full Git SHA"
        )
    if (
        baseline.get("code_revision") != revision
        or candidate.get("code_revision") != revision
    ):
        raise RetrievalReleaseError(
            "code_revision_mismatch",
            "retrieval evidence code revision differs from deployment",
        )
    evidence = RetrievalEvidenceRefs(
        baseline_run_id=baseline_run_id,
        candidate_run_id=candidate_run_id,
        comparison_id=comparison_id,
        baseline_diagnosis_id=baseline_diagnosis_id,
        candidate_diagnosis_id=candidate_diagnosis_id,
        trace_id=trace_id,
    )
    return _ValidatedAnalysis(
        code_revision=revision,
        selected_pipeline=selected_pipeline,
        evidence=evidence,
        release_gate=release_gate,
        trace_terminal_reason_code=terminal.reason_code,
    )


def _validate_trace_observation_refs(
    trace,
    *,
    baseline_run_id: str,
    candidate_run_id: str,
    comparison_id: str,
    baseline_diagnosis_id: str,
    candidate_diagnosis_id: str,
) -> None:
    baseline_matches = []
    candidate_matches = []
    for event in trace.events:
        if event.event_type != "tool_observed" or not isinstance(
            event.observation, dict
        ):
            continue
        observation = event.observation
        if observation.get("status") != "succeeded":
            continue
        payload = observation.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("run_id") == baseline_run_id:
            baseline_matches.append(payload)
        if payload.get("candidate_run_id") == candidate_run_id:
            candidate_matches.append(payload)
    if len(baseline_matches) != 1 or (
        baseline_matches[0].get("diagnosis_id") != baseline_diagnosis_id
    ):
        raise RetrievalReleaseError(
            "analysis_trace_invalid", "Trace baseline diagnosis reference differs"
        )
    if len(candidate_matches) != 1 or (
        candidate_matches[0].get("comparison_id") != comparison_id
        or candidate_matches[0].get("diagnosis_id") != candidate_diagnosis_id
    ):
        raise RetrievalReleaseError(
            "analysis_trace_invalid", "Trace candidate evidence reference differs"
        )


def _build_proposal(
    validated: _ValidatedAnalysis,
    *,
    parent_active_revision: str | None,
) -> RetrievalReleaseProposal:
    body = {
        "analysis_schema_version": "retrieval-stage-analysis-response-v1",
        "analysis_status": "proposal_ready",
        "approval_eligible": True,
        "code_revision": validated.code_revision,
        "evidence": validated.evidence.model_dump(mode="json"),
        "lifecycle": "pending_owner_review",
        "parent_active_revision": parent_active_revision,
        "profile": "smoke",
        "release_gate": validated.release_gate.model_dump(mode="json"),
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "selected_pipeline": validated.selected_pipeline.model_dump(mode="json"),
        "trace_terminal_reason_code": validated.trace_terminal_reason_code,
    }
    revision = _sha256_payload(body)
    return RetrievalReleaseProposal.model_validate(
        {
            **body,
            "proposal_id": f"retrieval-proposal-{revision[:12]}",
            "proposal_revision": revision,
        },
        strict=True,
    )


def _store_or_load_proposal(
    run_store: Path,
    proposal: RetrievalReleaseProposal,
) -> RetrievalReleaseProposal:
    path = _proposal_path(run_store, proposal.proposal_id)
    write_immutable_json(path, proposal.model_dump(mode="json"))
    return _load_proposal(run_store, proposal.proposal_id)


def _validate_intent_matches_analysis(
    intent: RetrievalProposalIntent,
    validated: _ValidatedAnalysis,
) -> None:
    if (
        intent.trace_id != validated.evidence.trace_id
        or intent.code_revision != validated.code_revision
        or intent.config_sha256 != validated.selected_pipeline.config_sha256
        or intent.baseline_run_id != validated.evidence.baseline_run_id
        or intent.candidate_run_id != validated.evidence.candidate_run_id
        or intent.comparison_id != validated.evidence.comparison_id
    ):
        raise RetrievalReleaseError(
            "proposal_intent_mismatch",
            "Trace is already bound to different retrieval proposal evidence",
        )


def _validate_approval(
    proposal: RetrievalReleaseProposal,
    *,
    project_root: Path,
    run_store: Path,
    revision_provider: Callable[[Path], str],
) -> None:
    current_parent = _load_active_revision(run_store)
    if current_parent != proposal.parent_active_revision:
        raise RetrievalReleaseError(
            "parent_active_revision_conflict",
            "retrieval proposal is stale relative to active serving",
        )
    try:
        revision = revision_provider(project_root).strip()
    except (OSError, RuntimeError) as exc:
        raise RetrievalReleaseError(
            "code_revision_unavailable",
            "retrieval approval requires a clean code revision",
        ) from exc
    if revision != proposal.code_revision:
        raise RetrievalReleaseError(
            "code_revision_mismatch",
            "retrieval proposal code revision differs from deployment",
        )
    _revalidate_proposal_evidence(proposal, run_store=run_store)
    if _load_active_revision(run_store) != proposal.parent_active_revision:
        raise RetrievalReleaseError(
            "parent_active_revision_conflict",
            "active serving changed while proposal evidence was validated",
        )


def _revalidate_proposal_evidence(
    proposal: RetrievalReleaseProposal,
    *,
    run_store: Path,
) -> None:
    refs = proposal.evidence
    baseline = _load_evidence(run_store, "retrieval-runs", refs.baseline_run_id)
    candidate = _load_evidence(run_store, "retrieval-runs", refs.candidate_run_id)
    comparison = _load_evidence(
        run_store,
        "retrieval-comparisons",
        refs.comparison_id,
    )
    baseline_diagnosis = _load_evidence(
        run_store,
        "stage-diagnoses",
        refs.baseline_diagnosis_id,
    )
    candidate_diagnosis = _load_evidence(
        run_store,
        "stage-diagnoses",
        refs.candidate_diagnosis_id,
    )
    try:
        validate_retrieval_run(baseline, role="retrieval approval baseline")
        validate_retrieval_run(candidate, role="retrieval approval candidate")
        rebuilt = compare_retrieval_runs(baseline, candidate)
        if rebuilt != comparison:
            raise ValueError("comparison changed")
        if diagnose_retrieval_stages(baseline) != baseline_diagnosis:
            raise ValueError("baseline diagnosis changed")
        if diagnose_retrieval_stages(candidate) != candidate_diagnosis:
            raise ValueError("candidate diagnosis changed")
        trace = TraceReplayer(TraceStore(run_store / "agent-traces")).replay_trace(
            refs.trace_id
        )
    except (TypeError, ValueError, RuntimeError) as exc:
        raise RetrievalReleaseError(
            "retrieval_evidence_invalid",
            "retrieval approval evidence failed semantic validation",
        ) from exc
    if (
        baseline.get("code_revision") != proposal.code_revision
        or candidate.get("code_revision") != proposal.code_revision
        or candidate.get("pipeline_id") != proposal.selected_pipeline.pipeline_id
        or candidate.get("pipeline") != proposal.selected_pipeline.config
        or comparison.get("gate_result")
        != proposal.release_gate.model_dump(mode="json", exclude={"failed_gates"})
        or trace.terminal.outcome != TerminalOutcome.PROPOSAL_READY
        or trace.terminal.report.get("decision", {}).get("selected_comparison_id")
        != refs.comparison_id
    ):
        raise RetrievalReleaseError(
            "retrieval_evidence_invalid",
            "retrieval approval evidence does not match proposal",
        )


def _build_decision(
    proposal: RetrievalReleaseProposal,
    *,
    decision: Literal["approve", "reject"],
    client_action_id: str,
    actor_id: str,
) -> RetrievalReleaseDecision:
    body = {
        "activation_status": "not_active",
        "actor_id": actor_id,
        "client_action_id": client_action_id,
        "config_sha256": proposal.selected_pipeline.config_sha256,
        "decision": decision,
        "lifecycle": (
            "approved_for_validation" if decision == "approve" else "rejected"
        ),
        "parent_active_revision": proposal.parent_active_revision,
        "pipeline_id": proposal.selected_pipeline.pipeline_id,
        "proposal_id": proposal.proposal_id,
        "proposal_revision": proposal.proposal_revision,
        "schema_version": DECISION_SCHEMA_VERSION,
        "strategy_id": proposal.selected_pipeline.strategy_id,
        "validation_required": decision == "approve",
    }
    return RetrievalReleaseDecision.model_validate(
        {**body, "decision_id": _content_id("retrieval-decision", body)},
        strict=True,
    )


def _prepare_decision_intent(
    run_store: Path,
    intent: RetrievalDecisionIntent,
) -> None:
    path = _decision_intent_path(run_store, intent.proposal_id)
    if path.exists() or path.is_symlink():
        existing = RetrievalDecisionIntent.model_validate(
            _load_bounded_json_object(path), strict=True
        )
        if existing != intent:
            raise RetrievalReleaseError(
                "decision_idempotency_conflict",
                "proposal already has a different decision intent",
            )
        return
    write_immutable_json(path, intent.model_dump(mode="json"))


def _complete_decision_publication(
    run_store: Path,
    decision: RetrievalReleaseDecision,
) -> None:
    payload = decision.model_dump(mode="json")
    write_immutable_json(_decision_path(run_store, decision.decision_id), payload)
    write_immutable_json(_decision_pointer(run_store, decision.proposal_id), payload)
    write_immutable_json(
        _client_action_pointer(run_store, decision.client_action_id),
        payload,
    )


def _build_outcome(
    proposal: RetrievalReleaseProposal,
    decision: RetrievalReleaseDecision,
    *,
    outcome: Literal["active", "validation_failed"],
    validation_receipt: dict[str, Any],
    active_strategy_revision: str | None,
) -> RetrievalReleaseOutcome:
    body = {
        "active_strategy_revision": active_strategy_revision,
        "config_sha256": proposal.selected_pipeline.config_sha256,
        "decision_id": decision.decision_id,
        "lifecycle": outcome,
        "pipeline_id": proposal.selected_pipeline.pipeline_id,
        "proposal_id": proposal.proposal_id,
        "proposal_revision": proposal.proposal_revision,
        "schema_version": OUTCOME_SCHEMA_VERSION,
        "strategy_id": proposal.selected_pipeline.strategy_id,
        "validation_receipt": validation_receipt,
        "validation_receipt_sha256": _sha256_payload(validation_receipt),
    }
    return RetrievalReleaseOutcome.model_validate(
        {**body, "outcome_id": _content_id("retrieval-outcome", body)},
        strict=True,
    )


def _validate_validation_receipt(
    proposal: RetrievalReleaseProposal,
    receipt: dict[str, Any],
    *,
    outcome: Literal["active", "validation_failed"],
    active_strategy_revision: str | None,
    run_store: Path,
) -> None:
    try:
        if outcome == "active":
            parsed: (
                RetrievalServingActivationReceipt | RetrievalServingValidationFailure
            )
            parsed = RetrievalServingActivationReceipt.model_validate(
                receipt,
                strict=True,
            )
            if active_strategy_revision != parsed.strategy_revision:
                raise ValueError("active revision differs from activation receipt")
            if _load_active_revision(run_store) != parsed.strategy_revision:
                raise ValueError("active serving pointer differs from receipt")
            if parsed.parent_active_revision != proposal.parent_active_revision:
                raise ValueError("activation receipt parent differs from proposal")
        else:
            parsed = RetrievalServingValidationFailure.model_validate(
                receipt,
                strict=True,
            )
            if active_strategy_revision is not None:
                raise ValueError("failed validation cannot claim an active revision")
        if (
            parsed.proposal_id != proposal.proposal_id
            or parsed.proposal_revision != proposal.proposal_revision
            or parsed.strategy_id != proposal.selected_pipeline.strategy_id
            or parsed.pipeline_id != proposal.selected_pipeline.pipeline_id
            or parsed.config_sha256 != proposal.selected_pipeline.config_sha256
            or parsed.code_revision != proposal.code_revision
            or parsed.parent_active_revision != proposal.parent_active_revision
        ):
            raise ValueError("validation receipt does not match proposal")
    except (TypeError, ValueError) as exc:
        raise RetrievalReleaseError(
            "validation_receipt_invalid",
            "serving validation receipt is invalid",
        ) from exc


def _prepare_outcome_intent(
    run_store: Path,
    intent: RetrievalOutcomeIntent,
) -> None:
    path = _outcome_intent_path(run_store, intent.proposal_id)
    if path.exists() or path.is_symlink():
        existing = RetrievalOutcomeIntent.model_validate(
            _load_bounded_json_object(path), strict=True
        )
        if existing != intent:
            raise RetrievalReleaseError(
                "outcome_idempotency_conflict",
                "proposal already has a different outcome intent",
            )
        return
    write_immutable_json(path, intent.model_dump(mode="json"))


def _complete_outcome_publication(
    run_store: Path,
    outcome: RetrievalReleaseOutcome,
) -> None:
    payload = outcome.model_dump(mode="json")
    write_immutable_json(_outcome_path(run_store, outcome.outcome_id), payload)
    write_immutable_json(_outcome_pointer(run_store, outcome.proposal_id), payload)


def _validate_rollback_receipt(
    proposal: RetrievalReleaseProposal,
    outcome: RetrievalReleaseOutcome,
    receipt: dict[str, Any],
) -> RetrievalServingRollbackReceipt:
    try:
        parsed = RetrievalServingRollbackReceipt.model_validate(receipt, strict=True)
        if (
            parsed.from_strategy_revision != outcome.active_strategy_revision
            or outcome.proposal_id != proposal.proposal_id
            or outcome.proposal_revision != proposal.proposal_revision
            or outcome.strategy_id != proposal.selected_pipeline.strategy_id
            or outcome.pipeline_id != proposal.selected_pipeline.pipeline_id
            or outcome.config_sha256 != proposal.selected_pipeline.config_sha256
        ):
            raise ValueError("rollback receipt does not match active release")
    except (TypeError, ValueError) as exc:
        raise RetrievalReleaseError(
            "rollback_receipt_invalid",
            "serving rollback receipt is invalid",
        ) from exc
    return parsed


def _build_rollback(
    proposal: RetrievalReleaseProposal,
    outcome: RetrievalReleaseOutcome,
    *,
    rollback_receipt: dict[str, Any],
) -> RetrievalReleaseRollback:
    body = {
        "from_strategy_revision": rollback_receipt["from_strategy_revision"],
        "lifecycle": "rolled_back",
        "outcome_id": outcome.outcome_id,
        "proposal_id": proposal.proposal_id,
        "proposal_revision": proposal.proposal_revision,
        "rollback_receipt": rollback_receipt,
        "rollback_receipt_sha256": _sha256_payload(rollback_receipt),
        "schema_version": ROLLBACK_SCHEMA_VERSION,
        "target_strategy_revision": rollback_receipt["strategy_revision"],
    }
    return RetrievalReleaseRollback.model_validate(
        {**body, "rollback_id": _content_id("retrieval-rollback", body)},
        strict=True,
    )


def _prepare_rollback_intent(
    run_store: Path,
    intent: RetrievalRollbackIntent,
) -> None:
    path = _rollback_intent_path(run_store, intent.proposal_id)
    if path.exists() or path.is_symlink():
        existing = RetrievalRollbackIntent.model_validate(
            _load_bounded_json_object(path), strict=True
        )
        if existing != intent:
            raise RetrievalReleaseError(
                "rollback_idempotency_conflict",
                "proposal already has a different rollback intent",
            )
        return
    write_immutable_json(path, intent.model_dump(mode="json"))


def _complete_rollback_publication(
    run_store: Path,
    rollback: RetrievalReleaseRollback,
) -> None:
    payload = rollback.model_dump(mode="json")
    write_immutable_json(_rollback_path(run_store, rollback.rollback_id), payload)
    write_immutable_json(
        _rollback_pointer(run_store, rollback.proposal_id),
        payload,
    )


def _load_proposal(run_store: Path, proposal_id: str) -> RetrievalReleaseProposal:
    if not isinstance(proposal_id, str) or not PROPOSAL_ID_PATTERN.fullmatch(
        proposal_id
    ):
        raise RetrievalReleaseError("invalid_proposal_id", "invalid proposal ID")
    path = _proposal_path(run_store, proposal_id)
    try:
        proposal = RetrievalReleaseProposal.model_validate(
            _load_bounded_json_object(path), strict=True
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, RetrievalReleaseError):
            raise
        raise RetrievalReleaseError(
            "proposal_artifact_invalid", "retrieval proposal artifact is invalid"
        ) from exc
    if proposal.proposal_id != path.stem:
        raise RetrievalReleaseError(
            "proposal_artifact_invalid", "proposal filename differs from content"
        )
    return proposal


def _load_existing_decision(
    run_store: Path,
    proposal_id: str,
) -> RetrievalReleaseDecision | None:
    path = _decision_pointer(run_store, proposal_id)
    if not path.exists() and not path.is_symlink():
        return None
    try:
        decision = RetrievalReleaseDecision.model_validate(
            _load_bounded_json_object(path), strict=True
        )
    except (TypeError, ValueError) as exc:
        raise RetrievalReleaseError(
            "decision_artifact_invalid", "retrieval decision artifact is invalid"
        ) from exc
    if decision.proposal_id != proposal_id:
        raise RetrievalReleaseError(
            "decision_artifact_invalid", "decision pointer references another proposal"
        )
    immutable = RetrievalReleaseDecision.model_validate(
        _load_bounded_json_object(_decision_path(run_store, decision.decision_id)),
        strict=True,
    )
    if immutable != decision:
        raise RetrievalReleaseError(
            "decision_artifact_invalid",
            "decision pointer differs from immutable record",
        )
    return decision


def _load_client_action_decision(
    run_store: Path,
    client_action_id: str,
) -> RetrievalReleaseDecision | None:
    path = _client_action_pointer(run_store, client_action_id)
    if not path.exists() and not path.is_symlink():
        return None
    try:
        return RetrievalReleaseDecision.model_validate(
            _load_bounded_json_object(path), strict=True
        )
    except (TypeError, ValueError) as exc:
        raise RetrievalReleaseError(
            "decision_artifact_invalid", "client action pointer is invalid"
        ) from exc


def _load_existing_outcome(
    run_store: Path,
    proposal_id: str,
) -> RetrievalReleaseOutcome | None:
    path = _outcome_pointer(run_store, proposal_id)
    if not path.exists() and not path.is_symlink():
        return None
    try:
        outcome = RetrievalReleaseOutcome.model_validate(
            _load_bounded_json_object(path), strict=True
        )
        immutable = RetrievalReleaseOutcome.model_validate(
            _load_bounded_json_object(_outcome_path(run_store, outcome.outcome_id)),
            strict=True,
        )
    except (TypeError, ValueError) as exc:
        raise RetrievalReleaseError(
            "outcome_artifact_invalid", "retrieval outcome artifact is invalid"
        ) from exc
    if outcome.proposal_id != proposal_id or immutable != outcome:
        raise RetrievalReleaseError(
            "outcome_artifact_invalid", "retrieval outcome pointer is inconsistent"
        )
    return outcome


def _load_existing_rollback(
    run_store: Path,
    proposal_id: str,
) -> RetrievalReleaseRollback | None:
    path = _rollback_pointer(run_store, proposal_id)
    if not path.exists() and not path.is_symlink():
        return None
    try:
        rollback = RetrievalReleaseRollback.model_validate(
            _load_bounded_json_object(path), strict=True
        )
        immutable = RetrievalReleaseRollback.model_validate(
            _load_bounded_json_object(_rollback_path(run_store, rollback.rollback_id)),
            strict=True,
        )
    except (TypeError, ValueError) as exc:
        raise RetrievalReleaseError(
            "rollback_artifact_invalid", "retrieval rollback artifact is invalid"
        ) from exc
    if rollback.proposal_id != proposal_id or immutable != rollback:
        raise RetrievalReleaseError(
            "rollback_artifact_invalid",
            "retrieval rollback pointer is inconsistent",
        )
    return rollback


def _release_lifecycle(
    decision: RetrievalReleaseDecision | None,
    outcome: RetrievalReleaseOutcome | None,
    rollback: RetrievalReleaseRollback | None,
    *,
    active_revision: str | None,
) -> str:
    if rollback is not None:
        if (
            decision is None
            or decision.lifecycle != "approved_for_validation"
            or outcome is None
            or outcome.lifecycle != "active"
            or rollback.outcome_id != outcome.outcome_id
            or rollback.proposal_revision != outcome.proposal_revision
            or rollback.from_strategy_revision != outcome.active_strategy_revision
        ):
            raise RetrievalReleaseError(
                "release_lifecycle_invalid",
                "rollback has no matching active outcome",
            )
        return "rolled_back"
    if outcome is not None:
        if decision is None or decision.lifecycle != "approved_for_validation":
            raise RetrievalReleaseError(
                "release_lifecycle_invalid", "outcome has no approval decision"
            )
        if (
            outcome.lifecycle == "active"
            and outcome.active_strategy_revision != active_revision
        ):
            # The serving pointer is authoritative.  Fail closed if a rollback
            # succeeded but its control-plane projection has not been recorded.
            return "rolled_back"
        return outcome.lifecycle
    if decision is not None:
        return decision.lifecycle
    return "pending_owner_review"


def _safe_release_entry(
    proposal: RetrievalReleaseProposal,
    decision: RetrievalReleaseDecision | None,
    outcome: RetrievalReleaseOutcome | None,
    rollback: RetrievalReleaseRollback | None,
    *,
    active_revision: str | None,
) -> dict[str, Any]:
    lifecycle = _release_lifecycle(
        decision,
        outcome,
        rollback,
        active_revision=active_revision,
    )
    validation_receipt = outcome.validation_receipt if outcome is not None else {}
    return {
        "active_strategy_revision": (
            outcome.active_strategy_revision if lifecycle == "active" else None
        ),
        "activated_strategy_revision": (
            outcome.active_strategy_revision if outcome is not None else None
        ),
        "approval_eligible": lifecycle == "pending_owner_review",
        "code_revision": proposal.code_revision,
        "config_sha256": proposal.selected_pipeline.config_sha256,
        "decision_id": decision.decision_id if decision is not None else None,
        "evidence": proposal.evidence.model_dump(mode="json"),
        "health": "ready" if lifecycle == "active" else "not_ready",
        "index_id": validation_receipt.get("index_id"),
        "index_schema_version": validation_receipt.get("index_schema_version"),
        "lifecycle": lifecycle,
        "outcome_id": outcome.outcome_id if outcome is not None else None,
        "parent_active_revision": proposal.parent_active_revision,
        "pipeline_id": proposal.selected_pipeline.pipeline_id,
        "pipeline_variant": proposal.selected_pipeline.config["variant"],
        "previous_revision": validation_receipt.get("rollback_strategy_revision"),
        "proposal_id": proposal.proposal_id,
        "proposal_revision": proposal.proposal_revision,
        "release_gate_passed": proposal.release_gate.passed,
        "rollback_id": rollback.rollback_id if rollback is not None else None,
        "rollback_receipt_id": (
            rollback.rollback_receipt["receipt_id"] if rollback is not None else None
        ),
        "rollback_target_revision": (
            rollback.target_strategy_revision if rollback is not None else None
        ),
        "rollout": "explicit_active_lane" if lifecycle == "active" else None,
        "ready": lifecycle == "active",
        "strategy_id": proposal.selected_pipeline.strategy_id,
        "strategy_revision": (
            outcome.active_strategy_revision if outcome is not None else None
        ),
    }


def _validate_decision_request(
    *,
    proposal_id: str,
    proposal_revision: str,
    decision: str,
    client_action_id: str,
    actor_id: str,
) -> None:
    if not isinstance(proposal_id, str) or not PROPOSAL_ID_PATTERN.fullmatch(
        proposal_id
    ):
        raise RetrievalReleaseError("invalid_proposal_id", "invalid proposal ID")
    if not isinstance(proposal_revision, str) or not re.fullmatch(
        SHA256_FIELD_PATTERN, proposal_revision
    ):
        raise RetrievalReleaseError(
            "invalid_proposal_revision", "invalid proposal revision"
        )
    if decision not in {"approve", "reject"}:
        raise RetrievalReleaseError("invalid_decision", "invalid Owner decision")
    if not isinstance(client_action_id, str) or not re.fullmatch(
        SAFE_ACTION_PATTERN, client_action_id
    ):
        raise RetrievalReleaseError(
            "invalid_client_action_id", "invalid client action ID"
        )
    if not isinstance(actor_id, str) or not re.fullmatch(SAFE_ACTOR_PATTERN, actor_id):
        raise RetrievalReleaseError("invalid_actor_id", "invalid actor ID")


def _load_active_revision(run_store: Path) -> str | None:
    # Import lazily so the release controller and serving resolver can evolve
    # independently without a module-import cycle.
    from search_quality.catalog.serving import load_active_retrieval_revision

    try:
        revision = load_active_retrieval_revision(run_store)
    except (OSError, TypeError, ValueError) as exc:
        raise RetrievalReleaseError(
            "active_revision_invalid", "active retrieval revision is invalid"
        ) from exc
    if revision is not None and not re.fullmatch(SHA256_FIELD_PATTERN, revision):
        raise RetrievalReleaseError(
            "active_revision_invalid", "active retrieval revision is invalid"
        )
    return revision


def _load_evidence(run_store: Path, directory: str, artifact_id: str) -> dict[str, Any]:
    if directory not in {
        "retrieval-runs",
        "retrieval-comparisons",
        "stage-diagnoses",
    }:
        raise RetrievalReleaseError(
            "evidence_type_invalid", "unsupported retrieval evidence type"
        )
    path = _release_directory(run_store, directory) / f"{artifact_id}.json"
    return _load_bounded_json_object(path)


def _resolve_project_root(project_root: str | Path) -> Path:
    try:
        root = Path(project_root).resolve(strict=True)
    except OSError as exc:
        raise RetrievalReleaseError(
            "project_root_unavailable", "project root is unavailable"
        ) from exc
    if not root.is_dir():
        raise RetrievalReleaseError(
            "project_root_invalid", "project root must be a directory"
        )
    return root


def _resolve_artifact_root(
    project_root: Path,
    artifact_root: str | Path | None,
) -> Path:
    requested = project_root / "runs" if artifact_root is None else Path(artifact_root)
    if not requested.is_absolute():
        raise RetrievalReleaseError(
            "artifact_root_invalid", "retrieval artifact root must be absolute"
        )
    if requested.is_symlink():
        raise RetrievalReleaseError(
            "artifact_root_invalid", "retrieval artifact root must not be a symlink"
        )
    try:
        resolved = requested.resolve(strict=True)
    except OSError as exc:
        raise RetrievalReleaseError(
            "artifact_root_unavailable", "retrieval artifact root is unavailable"
        ) from exc
    if not resolved.is_dir():
        raise RetrievalReleaseError(
            "artifact_root_invalid", "retrieval artifact root must be a directory"
        )
    return resolved


def _release_directory(run_store: Path, name: str) -> Path:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name):
        raise RetrievalReleaseError(
            "artifact_directory_invalid", "invalid release artifact directory"
        )
    directory = run_store / name
    if directory.is_symlink():
        raise RetrievalReleaseError(
            "artifact_store_invalid", "release artifact directory is a symlink"
        )
    directory.mkdir(parents=True, exist_ok=True)
    try:
        resolved = directory.resolve(strict=True)
    except OSError as exc:
        raise RetrievalReleaseError(
            "artifact_store_unavailable", "release artifact directory is unavailable"
        ) from exc
    if resolved.parent != run_store or not resolved.is_dir():
        raise RetrievalReleaseError(
            "artifact_store_invalid", "release artifact directory escaped its root"
        )
    return resolved


def _proposal_path(run_store: Path, proposal_id: str) -> Path:
    return _release_directory(run_store, "retrieval-release-proposals") / (
        f"{proposal_id}.json"
    )


def _proposal_intent_path(run_store: Path, trace_id: str) -> Path:
    root = _release_directory(run_store, "retrieval-release-proposal-intents")
    by_trace = root / "by-trace"
    if by_trace.is_symlink():
        raise RetrievalReleaseError(
            "artifact_store_invalid", "proposal intent directory is a symlink"
        )
    by_trace.mkdir(parents=True, exist_ok=True)
    return by_trace / f"{trace_id}.json"


def _decision_path(run_store: Path, decision_id: str) -> Path:
    return _release_directory(run_store, "retrieval-release-decisions") / (
        f"{decision_id}.json"
    )


def _decision_pointer(run_store: Path, proposal_id: str) -> Path:
    root = _release_directory(run_store, "retrieval-release-decisions")
    directory = _validated_child_directory(root, "by-proposal")
    return directory / f"{proposal_id}.json"


def _decision_intent_path(run_store: Path, proposal_id: str) -> Path:
    root = _release_directory(run_store, "retrieval-release-decision-intents")
    directory = _validated_child_directory(root, "by-proposal")
    return directory / f"{proposal_id}.json"


def _client_action_pointer(run_store: Path, client_action_id: str) -> Path:
    root = _release_directory(run_store, "retrieval-release-decisions")
    directory = _validated_child_directory(root, "by-client-action")
    digest = hashlib.sha256(client_action_id.encode("utf-8")).hexdigest()
    return directory / f"{digest}.json"


def _outcome_path(run_store: Path, outcome_id: str) -> Path:
    return _release_directory(run_store, "retrieval-release-outcomes") / (
        f"{outcome_id}.json"
    )


def _outcome_pointer(run_store: Path, proposal_id: str) -> Path:
    root = _release_directory(run_store, "retrieval-release-outcomes")
    directory = _validated_child_directory(root, "by-proposal")
    return directory / f"{proposal_id}.json"


def _outcome_intent_path(run_store: Path, proposal_id: str) -> Path:
    root = _release_directory(run_store, "retrieval-release-outcome-intents")
    directory = _validated_child_directory(root, "by-proposal")
    return directory / f"{proposal_id}.json"


def _rollback_path(run_store: Path, rollback_id: str) -> Path:
    return _release_directory(run_store, "retrieval-release-rollbacks") / (
        f"{rollback_id}.json"
    )


def _rollback_pointer(run_store: Path, proposal_id: str) -> Path:
    root = _release_directory(run_store, "retrieval-release-rollbacks")
    directory = _validated_child_directory(root, "by-proposal")
    return directory / f"{proposal_id}.json"


def _rollback_intent_path(run_store: Path, proposal_id: str) -> Path:
    root = _release_directory(run_store, "retrieval-release-rollback-intents")
    directory = _validated_child_directory(root, "by-proposal")
    return directory / f"{proposal_id}.json"


def _validated_child_directory(parent: Path, name: str) -> Path:
    directory = parent / name
    if directory.is_symlink():
        raise RetrievalReleaseError(
            "artifact_store_invalid", "release pointer directory is a symlink"
        )
    directory.mkdir(parents=True, exist_ok=True)
    resolved = directory.resolve(strict=True)
    if resolved.parent != parent:
        raise RetrievalReleaseError(
            "artifact_store_invalid", "release pointer directory escaped its root"
        )
    return resolved


def _load_bounded_json_object(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RetrievalReleaseError(
            "artifact_unavailable", "release artifact is unavailable"
        )
    try:
        if path.stat().st_size > MAX_RELEASE_ARTIFACT_BYTES:
            raise RetrievalReleaseError(
                "artifact_too_large", "release artifact exceeds its size limit"
            )
        with path.open("rb") as handle:
            encoded = handle.read(MAX_RELEASE_ARTIFACT_BYTES + 1)
    except OSError as exc:
        raise RetrievalReleaseError(
            "artifact_unavailable", "release artifact is unavailable"
        ) from exc
    if len(encoded) > MAX_RELEASE_ARTIFACT_BYTES:
        raise RetrievalReleaseError(
            "artifact_too_large", "release artifact exceeds its size limit"
        )

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RetrievalReleaseError(
                    "artifact_duplicate_key", "release artifact has duplicate keys"
                )
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise RetrievalReleaseError(
            "artifact_non_finite", "release artifact has a non-finite number"
        )

    try:
        payload = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetrievalReleaseError(
            "artifact_invalid_json", "release artifact is not valid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise RetrievalReleaseError(
            "artifact_invalid_json", "release artifact must be an object"
        )
    return payload


@contextmanager
def _release_lock(run_store: Path) -> Iterator[None]:
    lock_root = _release_directory(run_store, "retrieval-release-locks")
    lock_path = lock_root / ".control-plane.lock"
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    elif lock_path.is_symlink():
        raise RetrievalReleaseError(
            "lock_invalid", "retrieval release lock is a symlink"
        )
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise RetrievalReleaseError(
            "lock_unavailable", "retrieval release lock is unavailable"
        ) from exc
    locked = False
    try:
        deadline = time.monotonic() + RELEASE_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise RetrievalReleaseError(
                        "lock_timeout", "retrieval release lock timed out"
                    ) from exc
                time.sleep(0.05)
        yield
    finally:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _require_pattern(value: Any, pattern: str, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise RetrievalReleaseError("analysis_invalid", f"invalid {field}")
    return value


def _content_id(prefix: str, payload: object) -> str:
    return f"{prefix}-{_sha256_payload(payload)[:12]}"


def _sha256_payload(payload: object) -> str:
    _ensure_json_value(payload)
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _ensure_json_value(value: object) -> None:
    try:
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be finite JSON") from exc


def _safe_trace_id_from_analysis(analysis: Mapping[str, Any] | Any) -> str | None:
    if not isinstance(analysis, Mapping):
        return None
    agent_run = analysis.get("agent_run")
    trace_id = agent_run.get("trace_id") if isinstance(agent_run, dict) else None
    return (
        trace_id
        if isinstance(trace_id, str) and re.fullmatch(TRACE_ID_PATTERN, trace_id)
        else None
    )


def _safe_proposal_id(value: Any) -> str | None:
    return (
        value
        if isinstance(value, str) and PROPOSAL_ID_PATTERN.fullmatch(value)
        else None
    )


def _log_failure(
    operation: str,
    exc: Exception,
    *,
    proposal_id: str | None,
    trace_id: str | None,
) -> None:
    error_code = (
        exc.code
        if isinstance(exc, RetrievalReleaseError)
        else "retrieval_release_internal_error"
    )
    logger.error(
        f"retrieval_release_{operation}_failed",
        extra={
            "agent_trace_id": trace_id,
            "error_code": error_code,
            "error_type": type(exc).__name__,
            "proposal_id": proposal_id,
        },
    )


__all__ = [
    "RetrievalReleaseDecision",
    "RetrievalReleaseError",
    "RetrievalReleaseOutcome",
    "RetrievalReleaseProposal",
    "RetrievalReleaseRollback",
    "RetrievalServingActivationReceipt",
    "RetrievalServingRollbackReceipt",
    "RetrievalServingValidationFailure",
    "apply_retrieval_release_decision",
    "build_retrieval_validation_failure_receipt",
    "create_or_load_retrieval_proposal",
    "load_retrieval_activation_envelope",
    "load_retrieval_release",
    "load_retrieval_release_catalog",
    "record_retrieval_release_outcome",
    "record_retrieval_release_rollback",
]
