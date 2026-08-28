"""Strict, evidence-only tools for the stage-aware retrieval Agent.

The runtime receives privacy-safe summaries.  Full query-level evidence is kept in
content-addressed immutable artifacts and is only exposed through the bounded
analysis-response builder after terminal semantics have been validated.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from search_quality.evaluation.artifacts import (
    require_clean_code_revision,
    write_immutable_json,
)
from search_quality.evaluation.datasets import EvaluationProfile, sha256_file
from search_quality.evaluation.relevance import RelevancePolicy
from search_quality.evaluation.retrieval import run_query_scoped_retrieval
from search_quality.evaluation.retrieval_comparison import (
    GATE_POLICY_VERSION,
    compare_retrieval_runs,
)
from search_quality.evaluation.retrieval_validation import validate_retrieval_run

from .contracts import (
    RETRIEVAL_PIPELINE_VARIANTS,
    RetrievalPipelineVariant,
    StrictModel,
    TerminalOutcome,
    TerminalResult,
)
from .errors import AgentToolError
from .registry import AgentToolRegistry, ToolSpec
from .stage_diagnosis import StageDiagnosis, diagnose_retrieval_stages

logger = logging.getLogger("search_quality.agent_tools")

DIAGNOSE_BASELINE_TOOL = "diagnose_baseline_retrieval"
RUN_CANDIDATE_TOOL = "run_retrieval_candidate"
DIAGNOSE_RETRIEVAL_CAPABILITY = "diagnose_smoke_retrieval"
EXPERIMENT_RETRIEVAL_CAPABILITY = "experiment_smoke_retrieval"
RETRIEVAL_TOOL_CAPABILITIES = frozenset(
    {DIAGNOSE_RETRIEVAL_CAPABILITY, EXPERIMENT_RETRIEVAL_CAPABILITY}
)

BASELINE_VARIANT = "title-exact-v1"
RETRIEVAL_RUN_ID_PATTERN = r"retrieval-[0-9a-f]{12}"
DIAGNOSIS_ID_PATTERN = r"stage-diagnosis-[0-9a-f]{12}"
COMPARISON_ID_PATTERN = r"retrieval-comparison-[0-9a-f]{12}"
PIPELINE_ID_PATTERN = r"pipeline-[0-9a-f]{12}"

GateName = Literal[
    "unique_relevant_contribution",
    "union_coverage_improvement",
    "fusion_recall_at_10_floor",
    "fusion_ndcg_at_10_floor",
    "fusion_mrr_at_10_floor",
    "coarse_recall_at_10_floor",
    "coarse_ndcg_at_10_floor",
    "coarse_mrr_at_10_floor",
    "worst_query_coarse_ndcg_delta_floor",
    "regressed_query_rate_ceiling",
    "worst_query_fusion_ndcg_delta_floor",
    "fusion_regressed_query_rate_ceiling",
]

GATE_POLICY: tuple[tuple[GateName, Literal[">", ">=", "<="], float], ...] = (
    ("unique_relevant_contribution", ">", 0.0),
    ("union_coverage_improvement", ">", 0.0),
    ("fusion_recall_at_10_floor", ">=", 0.0),
    ("fusion_ndcg_at_10_floor", ">=", 0.0),
    ("fusion_mrr_at_10_floor", ">=", 0.0),
    ("coarse_recall_at_10_floor", ">=", 0.0),
    ("coarse_ndcg_at_10_floor", ">=", 0.0),
    ("coarse_mrr_at_10_floor", ">=", 0.0),
    ("worst_query_coarse_ndcg_delta_floor", ">=", -0.02),
    ("regressed_query_rate_ceiling", "<=", 0.1),
    ("worst_query_fusion_ndcg_delta_floor", ">=", -0.02),
    ("fusion_regressed_query_rate_ceiling", "<=", 0.1),
)
GATE_POLICY_BY_NAME = {
    name: (comparator, threshold) for name, comparator, threshold in GATE_POLICY
}

FiniteFloat = Annotated[float, Field(strict=True, allow_inf_nan=False)]
UnitFloat = Annotated[
    float,
    Field(strict=True, allow_inf_nan=False, ge=0.0, le=1.0),
]
DeltaFloat = Annotated[
    float,
    Field(strict=True, allow_inf_nan=False, ge=-1.0, le=1.0),
]
StageCategory = Literal[
    "recall",
    "fusion",
    "coarse_rank",
    "post_retrieval_ranking",
    "data_or_labels",
]


class DiagnoseBaselineInput(StrictModel):
    profile: Literal["smoke"] = "smoke"


class CandidateExperimentInput(StrictModel):
    baseline_run_id: StrictStr = Field(pattern=rf"^{RETRIEVAL_RUN_ID_PATTERN}$")
    pipeline_variant: RetrievalPipelineVariant


class ArtifactRefs(StrictModel):
    retrieval_run_id: StrictStr = Field(pattern=rf"^{RETRIEVAL_RUN_ID_PATTERN}$")
    diagnosis_id: StrictStr = Field(pattern=rf"^{DIAGNOSIS_ID_PATTERN}$")
    comparison_id: StrictStr | None = Field(
        default=None,
        pattern=rf"^{COMPARISON_ID_PATTERN}$",
    )


class StageMetricSummary(StrictModel):
    judged_recall_at_10: UnitFloat
    mrr_at_10: UnitFloat
    ndcg_at_10: UnitFloat


class FirstLossCounts(StrictModel):
    recall: StrictInt = Field(ge=0)
    fusion: StrictInt = Field(ge=0)
    coarse_rank: StrictInt = Field(ge=0)
    retained: StrictInt = Field(ge=0)


class BaselineAggregateSummary(StrictModel):
    recall_union_coverage: UnitFloat
    fusion: StageMetricSummary
    coarse_rank: StageMetricSummary
    first_loss_counts: FirstLossCounts


class FindingSummary(StrictModel):
    finding_id: StrictStr = Field(pattern=r"^finding-[0-9a-f]{12}$")
    category: StageCategory
    subtype: StrictStr = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    verdict: Literal["confirmed", "suspected", "blocked"]
    impact: UnitFloat


class BaselineDiagnosisPayload(StrictModel):
    schema_version: Literal["stage-retrieval-baseline-summary-v1"] = (
        "stage-retrieval-baseline-summary-v1"
    )
    profile: Literal["smoke"]
    query_count: StrictInt = Field(ge=1, le=100)
    judged_pair_count: StrictInt = Field(ge=1, le=10_000)
    run_id: StrictStr = Field(pattern=rf"^{RETRIEVAL_RUN_ID_PATTERN}$")
    diagnosis_id: StrictStr = Field(pattern=rf"^{DIAGNOSIS_ID_PATTERN}$")
    pipeline_id: StrictStr = Field(pattern=rf"^{PIPELINE_ID_PATTERN}$")
    pipeline_variant: Literal["title-exact-v1"]
    diagnosis_status: Literal[
        "diagnosable",
        "no_failure",
        "insufficient_evidence",
        "requires_engineering",
    ]
    primary_category: StageCategory | None
    recommended_next_action: StrictStr = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    findings: list[FindingSummary] = Field(max_length=8)
    aggregate: BaselineAggregateSummary
    artifacts: ArtifactRefs

    @model_validator(mode="after")
    def validate_artifact_refs(self) -> Self:
        if self.artifacts.retrieval_run_id != self.run_id:
            raise ValueError("baseline artifact Run ID does not match payload")
        if self.artifacts.diagnosis_id != self.diagnosis_id:
            raise ValueError("baseline artifact diagnosis ID does not match payload")
        if self.artifacts.comparison_id is not None:
            raise ValueError("baseline must not claim a comparison artifact")
        return self


class BaselineDiagnosisOutput(StrictModel):
    evidence_ref: StrictStr = Field(pattern=rf"^run:{RETRIEVAL_RUN_ID_PATTERN}$")
    payload: BaselineDiagnosisPayload

    @model_validator(mode="after")
    def validate_evidence_ref(self) -> Self:
        if self.evidence_ref != f"run:{self.payload.run_id}":
            raise ValueError("baseline evidence reference does not match Run")
        return self


class StageDeltaSummary(StrictModel):
    judged_recall_at_10: DeltaFloat
    mrr_at_10: DeltaFloat
    ndcg_at_10: DeltaFloat


class CandidateAggregateDeltaSummary(StrictModel):
    recall_union_coverage: DeltaFloat
    fusion: StageDeltaSummary
    coarse_rank: StageDeltaSummary


class CandidateRiskSummary(StrictModel):
    unique_relevant_contribution: StrictInt = Field(ge=0, le=10_000)
    worst_coarse_query_ndcg_at_10_delta: DeltaFloat
    coarse_regressed_query_rate: UnitFloat
    worst_fusion_query_ndcg_at_10_delta: DeltaFloat
    fusion_regressed_query_rate: UnitFloat


class GateCheckSummary(StrictModel):
    name: GateName
    comparator: Literal[">", ">=", "<="]
    threshold: FiniteFloat
    observed: FiniteFloat
    passed: StrictBool

    @model_validator(mode="after")
    def validate_policy_and_result(self) -> Self:
        expected_comparator, expected_threshold = GATE_POLICY_BY_NAME[self.name]
        if (
            self.comparator != expected_comparator
            or self.threshold != expected_threshold
        ):
            raise ValueError(
                "gate comparator or threshold does not match trusted policy"
            )
        if self.comparator == ">":
            expected_passed = self.observed > self.threshold
        elif self.comparator == ">=":
            expected_passed = self.observed >= self.threshold
        else:
            expected_passed = self.observed <= self.threshold
        if self.passed is not expected_passed:
            raise ValueError("gate result does not match its observation")
        return self


class CandidateGateSummary(StrictModel):
    policy_version: Literal["closed-retrieval-experiment-gates-v1"] = (
        GATE_POLICY_VERSION
    )
    passed: StrictBool
    checks: list[GateCheckSummary] = Field(min_length=12, max_length=12)
    failed_gates: list[GateName]

    @model_validator(mode="after")
    def validate_checks(self) -> Self:
        expected_names = tuple(item[0] for item in GATE_POLICY)
        if tuple(item.name for item in self.checks) != expected_names:
            raise ValueError("gate checks must use the trusted order exactly once")
        failed = [item.name for item in self.checks if not item.passed]
        if self.failed_gates != failed:
            raise ValueError("failed gate list does not match checks")
        if self.passed is not all(item.passed for item in self.checks):
            raise ValueError("aggregate gate result does not match checks")
        return self


class CandidateExperimentPayload(StrictModel):
    schema_version: Literal["stage-retrieval-candidate-summary-v1"] = (
        "stage-retrieval-candidate-summary-v1"
    )
    profile: Literal["smoke"]
    baseline_run_id: StrictStr = Field(pattern=rf"^{RETRIEVAL_RUN_ID_PATTERN}$")
    candidate_run_id: StrictStr = Field(pattern=rf"^{RETRIEVAL_RUN_ID_PATTERN}$")
    diagnosis_id: StrictStr = Field(pattern=rf"^{DIAGNOSIS_ID_PATTERN}$")
    comparison_id: StrictStr = Field(pattern=rf"^{COMPARISON_ID_PATTERN}$")
    pipeline_id: StrictStr = Field(pattern=rf"^{PIPELINE_ID_PATTERN}$")
    pipeline_variant: RetrievalPipelineVariant
    diagnosis_status: Literal[
        "diagnosable",
        "no_failure",
        "insufficient_evidence",
        "requires_engineering",
    ]
    aggregate_deltas: CandidateAggregateDeltaSummary
    risk: CandidateRiskSummary
    gate: CandidateGateSummary
    recommendation: Literal["review_candidate", "reject_candidate"]
    next_action: Literal[
        "request_owner_review",
        "run_recall_channel_and_rrf_ablation",
        "replace_recall_candidate",
    ]
    artifacts: ArtifactRefs

    @model_validator(mode="after")
    def validate_cross_field_evidence(self) -> Self:
        if self.baseline_run_id == self.candidate_run_id:
            raise ValueError("candidate Run must differ from baseline")
        if self.artifacts.retrieval_run_id != self.candidate_run_id:
            raise ValueError("candidate artifact Run ID does not match payload")
        if self.artifacts.diagnosis_id != self.diagnosis_id:
            raise ValueError("candidate artifact diagnosis ID does not match payload")
        if self.artifacts.comparison_id != self.comparison_id:
            raise ValueError("candidate artifact comparison ID does not match payload")

        observations: dict[GateName, float] = {
            "unique_relevant_contribution": float(
                self.risk.unique_relevant_contribution
            ),
            "union_coverage_improvement": self.aggregate_deltas.recall_union_coverage,
            "fusion_recall_at_10_floor": (
                self.aggregate_deltas.fusion.judged_recall_at_10
            ),
            "fusion_ndcg_at_10_floor": self.aggregate_deltas.fusion.ndcg_at_10,
            "fusion_mrr_at_10_floor": self.aggregate_deltas.fusion.mrr_at_10,
            "coarse_recall_at_10_floor": (
                self.aggregate_deltas.coarse_rank.judged_recall_at_10
            ),
            "coarse_ndcg_at_10_floor": self.aggregate_deltas.coarse_rank.ndcg_at_10,
            "coarse_mrr_at_10_floor": self.aggregate_deltas.coarse_rank.mrr_at_10,
            "worst_query_coarse_ndcg_delta_floor": (
                self.risk.worst_coarse_query_ndcg_at_10_delta
            ),
            "regressed_query_rate_ceiling": self.risk.coarse_regressed_query_rate,
            "worst_query_fusion_ndcg_delta_floor": (
                self.risk.worst_fusion_query_ndcg_at_10_delta
            ),
            "fusion_regressed_query_rate_ceiling": (
                self.risk.fusion_regressed_query_rate
            ),
        }
        for check in self.gate.checks:
            if not math.isclose(
                check.observed,
                observations[check.name],
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                raise ValueError("gate observation does not match summarized evidence")
        expected_recommendation = (
            "review_candidate" if self.gate.passed else "reject_candidate"
        )
        if self.recommendation != expected_recommendation:
            raise ValueError("recommendation does not match gate result")
        if self.gate.passed and self.next_action != "request_owner_review":
            raise ValueError("passing candidate must request owner review")
        if not self.gate.passed and self.next_action == "request_owner_review":
            raise ValueError("failed candidate cannot request owner review")
        return self

    @property
    def selection_key(self) -> tuple[float, float, str]:
        return (
            self.risk.worst_fusion_query_ndcg_at_10_delta,
            self.aggregate_deltas.coarse_rank.ndcg_at_10,
            self.pipeline_variant,
        )


class CandidateExperimentOutput(StrictModel):
    evidence_ref: StrictStr = Field(pattern=rf"^comparison:{COMPARISON_ID_PATTERN}$")
    payload: CandidateExperimentPayload

    @model_validator(mode="after")
    def validate_evidence_ref(self) -> Self:
        if self.evidence_ref != f"comparison:{self.payload.comparison_id}":
            raise ValueError("candidate evidence reference does not match comparison")
        return self


class StageRetrievalTools:
    """Two allowlisted tools over the fixed 20-Query retrieval Harness."""

    def __init__(
        self,
        *,
        project_root: str | Path,
        artifact_root: str | Path,
        revision_provider: Callable[[Path], str] = require_clean_code_revision,
    ) -> None:
        self.project_root = Path(project_root).resolve(strict=True)
        configured_root = Path(artifact_root)
        if not configured_root.is_absolute():
            raise ValueError("retrieval artifact root must be absolute")
        if configured_root.is_symlink():
            raise ValueError("retrieval artifact root must not be a symbolic link")
        configured_root.mkdir(parents=True, exist_ok=True)
        self.artifact_root = configured_root.resolve(strict=True)
        if not self.artifact_root.is_dir():
            raise ValueError("retrieval artifact root must be a directory")
        self.manifest_path = (
            self.project_root / "data" / "manifests" / "esci-stage1.json"
        )
        self.policy_path = (
            self.project_root / "configs" / "evaluation" / "esci-primary-v1.json"
        )
        self.revision_provider = revision_provider
        self._lock = threading.RLock()
        self._baseline_pins: dict[str, tuple[str, str]] = {}
        self._artifact_cache: dict[tuple[str, str], bytes] = {}
        self._experiment_variants: list[RetrievalPipelineVariant] = []

    def build_registry(self) -> AgentToolRegistry:
        return AgentToolRegistry(
            (
                ToolSpec(
                    name=DIAGNOSE_BASELINE_TOOL,
                    capability=DIAGNOSE_RETRIEVAL_CAPABILITY,
                    input_model=DiagnoseBaselineInput,
                    output_model=BaselineDiagnosisOutput,
                    handler=self.diagnose_baseline_retrieval,
                ),
                ToolSpec(
                    name=RUN_CANDIDATE_TOOL,
                    capability=EXPERIMENT_RETRIEVAL_CAPABILITY,
                    input_model=CandidateExperimentInput,
                    output_model=CandidateExperimentOutput,
                    handler=self.run_retrieval_candidate,
                ),
            )
        )

    def diagnose_baseline_retrieval(
        self,
        request: DiagnoseBaselineInput,
    ) -> dict[str, Any]:
        logger.info(
            "retrieval_baseline_tool_started", extra={"profile_id": request.profile}
        )
        revision = self._revision()
        profile, policy = self._evaluation_inputs()
        try:
            run = run_query_scoped_retrieval(
                profile,
                policy=policy,
                policy_path=self.policy_path,
                project_root=self.project_root,
                code_revision=revision,
                pipeline_variant=BASELINE_VARIANT,
            )
            validate_retrieval_run(run, role="retrieval Agent baseline")
            self._validate_content_id(run, "run_id", "retrieval")
            diagnosis = diagnose_retrieval_stages(run)
            StageDiagnosis.model_validate(diagnosis)
            self._validate_content_id(diagnosis, "diagnosis_id", "stage-diagnosis")
            self._store("retrieval-runs", run["run_id"], run)
            self._store("stage-diagnoses", diagnosis["diagnosis_id"], diagnosis)
            run_path = self._artifact_path("retrieval-runs", run["run_id"])
            run_sha256 = sha256_file(run_path)
            with self._lock:
                self._baseline_pins[run["run_id"]] = (run_sha256, revision)
            output = BaselineDiagnosisOutput(
                evidence_ref=f"run:{run['run_id']}",
                payload=self._baseline_summary(run, diagnosis),
            ).model_dump(mode="json")
        except AgentToolError:
            raise
        except OSError as exc:
            raise AgentToolError("artifact_store_unavailable", retryable=True) from exc
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            raise AgentToolError("retrieval_evidence_invalid") from exc
        logger.info(
            "retrieval_baseline_tool_completed",
            extra={
                "diagnosis_id": diagnosis["diagnosis_id"],
                "pipeline_run_id": run["run_id"],
                "profile_id": request.profile,
                "query_count": run["dataset"]["query_count"],
            },
        )
        return output

    def run_retrieval_candidate(
        self,
        request: CandidateExperimentInput,
    ) -> dict[str, Any]:
        logger.info(
            "retrieval_candidate_tool_started",
            extra={
                "baseline_run_id": request.baseline_run_id,
                "pipeline_variant": request.pipeline_variant,
                "profile_id": "smoke",
            },
        )
        baseline, baseline_revision = self._trusted_baseline(request.baseline_run_id)
        revision = self._revision()
        if revision != baseline_revision or baseline.get("code_revision") != revision:
            raise AgentToolError("code_revision_changed")
        profile, policy = self._evaluation_inputs()
        try:
            candidate = run_query_scoped_retrieval(
                profile,
                policy=policy,
                policy_path=self.policy_path,
                project_root=self.project_root,
                code_revision=revision,
                pipeline_variant=request.pipeline_variant,
            )
            validate_retrieval_run(candidate, role="retrieval Agent candidate")
            self._validate_content_id(candidate, "run_id", "retrieval")
            diagnosis = diagnose_retrieval_stages(candidate)
            StageDiagnosis.model_validate(diagnosis)
            self._validate_content_id(diagnosis, "diagnosis_id", "stage-diagnosis")
            comparison = compare_retrieval_runs(baseline, candidate)
            self._validate_content_id(
                comparison,
                "comparison_id",
                "retrieval-comparison",
            )
            summary = self._candidate_summary(candidate, diagnosis, comparison)
            CandidateExperimentPayload.model_validate(summary)
            self._store("retrieval-runs", candidate["run_id"], candidate)
            self._store("stage-diagnoses", diagnosis["diagnosis_id"], diagnosis)
            self._store(
                "retrieval-comparisons",
                comparison["comparison_id"],
                comparison,
            )
            with self._lock:
                if request.pipeline_variant not in self._experiment_variants:
                    self._experiment_variants.append(request.pipeline_variant)
            output = CandidateExperimentOutput(
                evidence_ref=f"comparison:{comparison['comparison_id']}",
                payload=summary,
            ).model_dump(mode="json")
        except AgentToolError:
            raise
        except OSError as exc:
            raise AgentToolError("artifact_store_unavailable", retryable=True) from exc
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            raise AgentToolError("retrieval_evidence_invalid") from exc
        logger.info(
            "retrieval_candidate_tool_completed",
            extra={
                "candidate_run_id": candidate["run_id"],
                "comparison_id": comparison["comparison_id"],
                "failed_gate_count": len(summary["gate"]["failed_gates"]),
                "gate_passed": summary["gate"]["passed"],
                "pipeline_variant": request.pipeline_variant,
                "profile_id": "smoke",
            },
        )
        return output

    def build_analysis_response(self, terminal: TerminalResult) -> dict[str, Any]:
        """Rebuild the existing workbench response from validated cached evidence.

        This method performs no writes.  It is intentionally unavailable for an
        incomplete run because a partial experiment set cannot support a proposal.
        """

        if terminal.state != "completed" or terminal.outcome not in {
            TerminalOutcome.PROPOSAL_READY,
            TerminalOutcome.NO_SAFE_IMPROVEMENT,
        }:
            raise AgentToolError("retrieval_analysis_incomplete")
        with self._lock:
            if len(self._baseline_pins) != 1 or not self._experiment_variants:
                raise AgentToolError("retrieval_analysis_incomplete")
            baseline_run_id = next(iter(self._baseline_pins))
            variants = tuple(self._experiment_variants)
        baseline = self._cached("retrieval-runs", baseline_run_id)
        diagnosis = self._diagnosis_for_run(baseline_run_id)
        experiments = [self._cached_experiment(variant) for variant in variants]
        selected = self._selected_experiment(terminal, experiments)
        candidate = selected["candidate"]
        candidate_diagnosis = selected["diagnosis"]
        comparison = selected["comparison"]
        passed = comparison["gate_result"]["passed"] is True
        if terminal.outcome == TerminalOutcome.PROPOSAL_READY and not passed:
            raise AgentToolError("retrieval_terminal_inconsistent")
        if terminal.outcome == TerminalOutcome.NO_SAFE_IMPROVEMENT and any(
            item["comparison"]["gate_result"]["passed"] is True for item in experiments
        ):
            raise AgentToolError("retrieval_terminal_inconsistent")
        expected_selected_ref = f"comparison:{comparison['comparison_id']}"
        if terminal.outcome == TerminalOutcome.PROPOSAL_READY and (
            expected_selected_ref not in terminal.evidence_refs
        ):
            raise AgentToolError("retrieval_terminal_inconsistent")

        return {
            "aggregate": copy.deepcopy(baseline["aggregate"]),
            "candidate_aggregate": copy.deepcopy(candidate["aggregate"]),
            "candidate_diagnosis": copy.deepcopy(candidate_diagnosis),
            "candidate_diagnosis_id": candidate_diagnosis["diagnosis_id"],
            "candidate_run_id": candidate["run_id"],
            "comparison": copy.deepcopy(comparison),
            "comparison_id": comparison["comparison_id"],
            "diagnosis": copy.deepcopy(diagnosis),
            "diagnosis_id": diagnosis["diagnosis_id"],
            "evaluation_boundary": copy.deepcopy(baseline["evaluation_boundary"]),
            "experiments": [
                self._public_experiment_summary(item) for item in experiments
            ],
            "pipeline": copy.deepcopy(baseline["pipeline"]),
            "pipeline_id": baseline["pipeline_id"],
            "profile": "smoke",
            "proposal": {
                "candidate_strategy_id": (
                    "multi-field-bm25-weighted-rrf-v1"
                    if passed
                    else "multi-field-bm25-recall-v1"
                ),
                "decision": ("request_owner_review" if passed else "reject_candidate"),
                "next_action": comparison["next_action"],
                "reason": (
                    "A bounded RRF weight ablation preserved final quality while expanding closed-pool coverage."
                    if passed
                    else "The channel recovered relevant products, but no bounded fusion candidate passed all gates."
                ),
            },
            "retrieval_run_id": baseline["run_id"],
            "schema_version": "retrieval-stage-analysis-response-v1",
            "status": terminal.outcome.value,
        }

    def analysis_evidence(self) -> dict[str, Any]:
        """Return IDs only for diagnostics or bounded response assembly."""

        with self._lock:
            baseline_ids = tuple(sorted(self._baseline_pins))
            variants = tuple(self._experiment_variants)
            keys = tuple(sorted(self._artifact_cache))
        return {
            "baseline_run_ids": list(baseline_ids),
            "experiment_variants": list(variants),
            "artifacts": [
                {"artifact_type": artifact_type, "artifact_id": artifact_id}
                for artifact_type, artifact_id in keys
            ],
        }

    def _revision(self) -> str:
        try:
            return self.revision_provider(self.project_root)
        except RuntimeError as exc:
            raise AgentToolError("worktree_dirty") from exc

    def _evaluation_inputs(self) -> tuple[EvaluationProfile, RelevancePolicy]:
        try:
            profile = EvaluationProfile.from_stage1_manifest(
                profile_id="smoke",
                project_root=self.project_root,
                manifest_path=self.manifest_path,
            )
            policy = RelevancePolicy.from_path(self.policy_path)
        except OSError as exc:
            raise AgentToolError(
                "evaluation_input_unavailable", retryable=True
            ) from exc
        except (TypeError, ValueError) as exc:
            raise AgentToolError("evaluation_input_invalid") from exc
        return profile, policy

    def _trusted_baseline(self, run_id: str) -> tuple[dict[str, Any], str]:
        with self._lock:
            pin = self._baseline_pins.get(run_id)
        if pin is None:
            raise AgentToolError("baseline_not_admitted")
        expected_sha256, revision = pin
        path = self._artifact_path("retrieval-runs", run_id)
        if path.is_symlink() or not path.is_file():
            raise AgentToolError("retrieval_artifact_integrity_failed")
        try:
            if sha256_file(path) != expected_sha256:
                raise AgentToolError("retrieval_artifact_integrity_failed")
            baseline = self._cached("retrieval-runs", run_id)
            validate_retrieval_run(baseline, role="retrieval Agent admitted baseline")
            self._validate_content_id(baseline, "run_id", "retrieval")
        except AgentToolError:
            raise
        except OSError as exc:
            raise AgentToolError("artifact_store_unavailable", retryable=True) from exc
        except (TypeError, ValueError) as exc:
            raise AgentToolError("retrieval_artifact_integrity_failed") from exc
        if baseline.get("pipeline", {}).get("variant") != BASELINE_VARIANT:
            raise AgentToolError("baseline_not_admitted")
        return baseline, revision

    def _store(
        self, artifact_type: str, artifact_id: str, payload: dict[str, Any]
    ) -> None:
        path = self._artifact_path(artifact_type, artifact_id)
        if path.parent.is_symlink():
            raise AgentToolError("artifact_store_unavailable")
        try:
            write_immutable_json(path, payload)
        except RuntimeError as exc:
            raise AgentToolError("artifact_collision") from exc
        except OSError as exc:
            raise AgentToolError("artifact_store_unavailable", retryable=True) from exc
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        with self._lock:
            self._artifact_cache[(artifact_type, artifact_id)] = canonical

    def _artifact_path(self, artifact_type: str, artifact_id: str) -> Path:
        if artifact_type not in {
            "retrieval-runs",
            "stage-diagnoses",
            "retrieval-comparisons",
        }:
            raise AgentToolError("artifact_type_not_allowed")
        directory = self.artifact_root / artifact_type
        if directory.is_symlink():
            raise AgentToolError("artifact_store_unavailable")
        directory.mkdir(parents=True, exist_ok=True)
        resolved = directory.resolve(strict=True)
        if resolved.parent != self.artifact_root:
            raise AgentToolError("artifact_store_unavailable")
        return resolved / f"{artifact_id}.json"

    def _cached(self, artifact_type: str, artifact_id: str) -> dict[str, Any]:
        with self._lock:
            serialized = self._artifact_cache.get((artifact_type, artifact_id))
        if serialized is None:
            raise AgentToolError("retrieval_analysis_incomplete")
        value = json.loads(serialized)
        if not isinstance(value, dict):
            raise AgentToolError("retrieval_artifact_integrity_failed")
        return value

    def _diagnosis_for_run(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            matches = [
                artifact_id
                for artifact_type, artifact_id in self._artifact_cache
                if artifact_type == "stage-diagnoses"
            ]
        diagnoses = [self._cached("stage-diagnoses", item) for item in matches]
        matching = [item for item in diagnoses if item["pipeline_run_id"] == run_id]
        if len(matching) != 1:
            raise AgentToolError("retrieval_analysis_incomplete")
        return matching[0]

    def _cached_experiment(
        self,
        variant: RetrievalPipelineVariant,
    ) -> dict[str, dict[str, Any]]:
        with self._lock:
            run_ids = [
                artifact_id
                for artifact_type, artifact_id in self._artifact_cache
                if artifact_type == "retrieval-runs"
            ]
        candidates = [
            self._cached("retrieval-runs", run_id)
            for run_id in run_ids
            if self._cached("retrieval-runs", run_id)["pipeline"]["variant"] == variant
        ]
        if len(candidates) != 1:
            raise AgentToolError("retrieval_analysis_incomplete")
        candidate = candidates[0]
        diagnosis = self._diagnosis_for_run(candidate["run_id"])
        with self._lock:
            comparison_ids = [
                artifact_id
                for artifact_type, artifact_id in self._artifact_cache
                if artifact_type == "retrieval-comparisons"
            ]
        comparisons = [
            self._cached("retrieval-comparisons", item) for item in comparison_ids
        ]
        matching = [
            item
            for item in comparisons
            if item["candidate_run_id"] == candidate["run_id"]
        ]
        if len(matching) != 1:
            raise AgentToolError("retrieval_analysis_incomplete")
        return {
            "candidate": candidate,
            "comparison": matching[0],
            "diagnosis": diagnosis,
        }

    @staticmethod
    def _selected_experiment(
        terminal: TerminalResult,
        experiments: list[dict[str, dict[str, Any]]],
    ) -> dict[str, dict[str, Any]]:
        reason_to_variant = {
            "uniform_candidate_passed": RETRIEVAL_PIPELINE_VARIANTS[0],
            "conservative_candidate_selected": RETRIEVAL_PIPELINE_VARIANTS[1],
            "aggressive_candidate_selected": RETRIEVAL_PIPELINE_VARIANTS[2],
        }
        selected_variant = reason_to_variant.get(terminal.reason_code)
        if terminal.outcome == TerminalOutcome.PROPOSAL_READY:
            matches = [
                item
                for item in experiments
                if item["candidate"]["pipeline"]["variant"] == selected_variant
            ]
            if len(matches) != 1:
                raise AgentToolError("retrieval_terminal_inconsistent")
            return matches[0]
        return max(
            experiments,
            key=lambda item: (
                min(
                    row["fusion_ndcg@10_delta"]
                    for row in item["comparison"]["per_query"]
                ),
                item["comparison"]["aggregate_deltas"]["coarse_rank"]["ndcg@10"][
                    "delta"
                ],
                item["candidate"]["pipeline"]["variant"],
            ),
        )

    @staticmethod
    def _public_experiment_summary(
        experiment: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        candidate = experiment["candidate"]
        comparison = experiment["comparison"]
        return {
            "candidate_run_id": candidate["run_id"],
            "comparison_id": comparison["comparison_id"],
            "failed_gates": [
                check["name"]
                for check in comparison["gate_result"]["checks"]
                if not check["passed"]
            ],
            "fusion_mrr_at_10_delta": comparison["aggregate_deltas"]["fusion"][
                "mrr@10"
            ]["delta"],
            "fusion_ndcg_at_10_delta": comparison["aggregate_deltas"]["fusion"][
                "ndcg@10"
            ]["delta"],
            "fusion_weights": copy.deepcopy(candidate["pipeline"]["fusion"]["weights"]),
            "gate_passed": comparison["gate_result"]["passed"],
            "pipeline_variant": candidate["pipeline"]["variant"],
            "worst_fusion_query_ndcg_at_10_delta": min(
                item["fusion_ndcg@10_delta"] for item in comparison["per_query"]
            ),
        }

    @staticmethod
    def _baseline_summary(
        run: dict[str, Any],
        diagnosis: dict[str, Any],
    ) -> dict[str, Any]:
        stages = run["aggregate"]["stages"]
        return {
            "aggregate": {
                "coarse_rank": StageRetrievalTools._stage_metrics(
                    stages["coarse-title-bm25-v1"]
                ),
                "first_loss_counts": copy.deepcopy(
                    run["aggregate"]["first_loss_counts"]
                ),
                "fusion": StageRetrievalTools._stage_metrics(stages["rrf-v1"]),
                "recall_union_coverage": stages["recall-union-v1"][
                    "mean_judged_relevant_coverage"
                ],
            },
            "artifacts": {
                "comparison_id": None,
                "diagnosis_id": diagnosis["diagnosis_id"],
                "retrieval_run_id": run["run_id"],
            },
            "diagnosis_id": diagnosis["diagnosis_id"],
            "diagnosis_status": diagnosis["status"],
            "findings": [
                {
                    "category": item["category"],
                    "finding_id": item["finding_id"],
                    "impact": item["impact"],
                    "subtype": item["subtype"],
                    "verdict": item["verdict"],
                }
                for item in diagnosis["findings"]
            ],
            "judged_pair_count": run["dataset"]["judged_pairs"],
            "pipeline_id": run["pipeline_id"],
            "pipeline_variant": run["pipeline"]["variant"],
            "primary_category": diagnosis["primary_category"],
            "profile": run["dataset"]["profile"],
            "query_count": run["dataset"]["query_count"],
            "recommended_next_action": diagnosis["recommended_next_action"],
            "run_id": run["run_id"],
            "schema_version": "stage-retrieval-baseline-summary-v1",
        }

    @staticmethod
    def _candidate_summary(
        candidate: dict[str, Any],
        diagnosis: dict[str, Any],
        comparison: dict[str, Any],
    ) -> dict[str, Any]:
        deltas = comparison["aggregate_deltas"]
        per_query = comparison["per_query"]
        checks = comparison["gate_result"]["checks"]
        return {
            "aggregate_deltas": {
                "coarse_rank": StageRetrievalTools._stage_deltas(deltas["coarse_rank"]),
                "fusion": StageRetrievalTools._stage_deltas(deltas["fusion"]),
                "recall_union_coverage": deltas["recall_union"][
                    "judged_relevant_coverage"
                ]["delta"],
            },
            "artifacts": {
                "comparison_id": comparison["comparison_id"],
                "diagnosis_id": diagnosis["diagnosis_id"],
                "retrieval_run_id": candidate["run_id"],
            },
            "baseline_run_id": comparison["baseline_run_id"],
            "candidate_run_id": candidate["run_id"],
            "comparison_id": comparison["comparison_id"],
            "diagnosis_id": diagnosis["diagnosis_id"],
            "diagnosis_status": diagnosis["status"],
            "gate": {
                "checks": copy.deepcopy(checks),
                "failed_gates": [
                    item["name"] for item in checks if item["passed"] is not True
                ],
                "passed": comparison["gate_result"]["passed"],
                "policy_version": comparison["gate_result"]["policy_version"],
            },
            "next_action": comparison["next_action"],
            "pipeline_id": candidate["pipeline_id"],
            "pipeline_variant": candidate["pipeline"]["variant"],
            "profile": candidate["dataset"]["profile"],
            "recommendation": comparison["recommendation"],
            "risk": {
                "coarse_regressed_query_rate": sum(
                    item["coarse_ndcg@10_delta"] < -1e-12 for item in per_query
                )
                / len(per_query),
                "fusion_regressed_query_rate": sum(
                    item["fusion_ndcg@10_delta"] < -1e-12 for item in per_query
                )
                / len(per_query),
                "unique_relevant_contribution": comparison["candidate_strategy"][
                    "unique_relevant_contribution"
                ],
                "worst_coarse_query_ndcg_at_10_delta": min(
                    item["coarse_ndcg@10_delta"] for item in per_query
                ),
                "worst_fusion_query_ndcg_at_10_delta": min(
                    item["fusion_ndcg@10_delta"] for item in per_query
                ),
            },
            "schema_version": "stage-retrieval-candidate-summary-v1",
        }

    @staticmethod
    def _stage_metrics(stage: dict[str, Any]) -> dict[str, Any]:
        return {
            "judged_recall_at_10": stage["mean_judged_recall@10"],
            "mrr_at_10": stage["mean_mrr@10"],
            "ndcg_at_10": stage["mean_ndcg@10"],
        }

    @staticmethod
    def _stage_deltas(stage: dict[str, Any]) -> dict[str, Any]:
        return {
            "judged_recall_at_10": stage["judged_recall@10"]["delta"],
            "mrr_at_10": stage["mrr@10"]["delta"],
            "ndcg_at_10": stage["ndcg@10"]["delta"],
        }

    @staticmethod
    def _validate_content_id(
        payload: dict[str, Any],
        id_field: str,
        prefix: str,
    ) -> None:
        observed = payload.get(id_field)
        body = {key: value for key, value in payload.items() if key != id_field}
        canonical = json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        expected = f"{prefix}-{hashlib.sha256(canonical).hexdigest()[:12]}"
        if observed != expected:
            raise ValueError(f"{id_field} is not content-addressed")
