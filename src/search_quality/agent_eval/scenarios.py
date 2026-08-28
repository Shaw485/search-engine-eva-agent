"""Isolated, deterministic Agent Eval scenarios derived from real smoke evidence."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from typing import Any

from search_quality.agent.contracts import (
    RETRIEVAL_PIPELINE_VARIANTS,
    FinishDecision,
    TerminalOutcome,
    ToolAction,
    ToolObservation,
)
from search_quality.agent.errors import AgentPolicyError, AgentToolError
from search_quality.agent.planner import PlannerView
from search_quality.agent.registry import AgentToolRegistry, ToolSpec
from search_quality.agent.retrieval_planner import (
    AGGRESSIVE_VARIANT,
    CONSERVATIVE_VARIANT,
    UNIFORM_VARIANT,
    ObservationDrivenRetrievalPlanner,
)
from search_quality.agent.retrieval_tools import (
    DIAGNOSE_BASELINE_TOOL,
    DIAGNOSE_RETRIEVAL_CAPABILITY,
    EXPERIMENT_RETRIEVAL_CAPABILITY,
    RUN_CANDIDATE_TOOL,
    BaselineDiagnosisOutput,
    CandidateExperimentInput,
    CandidateExperimentOutput,
    DiagnoseBaselineInput,
)
from search_quality.agent.trace import AgentTrace

from .contracts import AgentEvalCase, ScenarioId


@dataclass(frozen=True, slots=True)
class CanonicalEvidence:
    baseline: ToolObservation
    uniform_failed: ToolObservation
    conservative_passed: ToolObservation
    aggressive_failed: ToolObservation


@dataclass(frozen=True, slots=True)
class RecordedToolCall:
    tool_name: str
    pipeline_variant: str | None
    profile: str | None
    status: str
    error_code: str | None


@dataclass(frozen=True, slots=True)
class FixedWorkflowExecution:
    success: bool
    tool_calls_used: int
    outcome_code: str


class HandlerRecordingRegistry:
    """Measure actual registry dispatches; scope-rejected actions never enter it."""

    def __init__(self, delegate: AgentToolRegistry) -> None:
        self._delegate = delegate
        self._ledger: list[RecordedToolCall] = []

    @property
    def names(self) -> frozenset[str]:
        return self._delegate.names

    @property
    def ledger(self) -> tuple[RecordedToolCall, ...]:
        return tuple(self._ledger)

    @property
    def protected_profile_reads(self) -> int:
        return sum(item.profile in {"dev", "test"} for item in self._ledger)

    def execute(self, tool_name, arguments, *, allowed_capabilities):
        variant = arguments.get("pipeline_variant")
        profile = arguments.get("profile")
        try:
            result = self._delegate.execute(
                tool_name,
                arguments,
                allowed_capabilities=allowed_capabilities,
            )
        except AgentToolError as exc:
            self._ledger.append(
                RecordedToolCall(
                    tool_name=tool_name,
                    pipeline_variant=variant,
                    profile=profile,
                    status="failed",
                    error_code=exc.code,
                )
            )
            raise
        except AgentPolicyError:
            # Registry policy rejection occurs before a handler is invoked.
            raise
        self._ledger.append(
            RecordedToolCall(
                tool_name=tool_name,
                pipeline_variant=variant,
                profile=profile,
                status="succeeded",
                error_code=None,
            )
        )
        return result


def canonical_evidence_from_trace(trace: AgentTrace) -> CanonicalEvidence:
    observations = tuple(
        ToolObservation.model_validate(event.observation)
        for event in trace.events
        if event.event_type == "tool_observed" and event.observation is not None
    )
    if len(observations) != 4 or any(
        item.status != "succeeded" for item in observations
    ):
        raise ValueError("canonical Agent Eval evidence must contain four successes")
    baseline, uniform, conservative, aggressive = observations
    if baseline.tool_name != DIAGNOSE_BASELINE_TOOL or any(
        item.tool_name != RUN_CANDIDATE_TOOL
        for item in (uniform, conservative, aggressive)
    ):
        raise ValueError("canonical Agent Eval evidence has an invalid tool order")
    variants = tuple(
        item.payload.get("pipeline_variant")
        for item in (uniform, conservative, aggressive)
    )
    if variants != RETRIEVAL_PIPELINE_VARIANTS:
        raise ValueError("canonical Agent Eval evidence has an invalid variant order")
    gate_path = tuple(
        item.payload.get("gate", {}).get("passed")
        for item in (uniform, conservative, aggressive)
    )
    if gate_path != (False, True, False):
        raise ValueError("canonical Agent Eval evidence has an unexpected gate path")
    return CanonicalEvidence(
        baseline=baseline,
        uniform_failed=uniform,
        conservative_passed=conservative,
        aggressive_failed=aggressive,
    )


class ScriptedRetrievalTools:
    """Strict fixture tools for Runtime behavior tests, never search-quality claims."""

    def __init__(self, case: AgentEvalCase, evidence: CanonicalEvidence) -> None:
        self.case = case
        self.evidence = evidence
        self._occurrences: dict[tuple[str, str | None], int] = {}
        self._ledger: list[RecordedToolCall] = []
        self._baseline_output = _baseline_output(case, evidence)

    @property
    def ledger(self) -> tuple[RecordedToolCall, ...]:
        return tuple(self._ledger)

    def build_registry(self) -> AgentToolRegistry:
        return AgentToolRegistry(
            (
                ToolSpec(
                    name=DIAGNOSE_BASELINE_TOOL,
                    capability=DIAGNOSE_RETRIEVAL_CAPABILITY,
                    input_model=DiagnoseBaselineInput,
                    output_model=BaselineDiagnosisOutput,
                    handler=self.diagnose,
                ),
                ToolSpec(
                    name=RUN_CANDIDATE_TOOL,
                    capability=EXPERIMENT_RETRIEVAL_CAPABILITY,
                    input_model=CandidateExperimentInput,
                    output_model=CandidateExperimentOutput,
                    handler=self.run_candidate,
                ),
            )
        )

    def diagnose(self, _request: DiagnoseBaselineInput) -> dict[str, Any]:
        self._record(DIAGNOSE_BASELINE_TOOL, None, "succeeded", None)
        return copy.deepcopy(self._baseline_output)

    def run_candidate(self, request: CandidateExperimentInput) -> dict[str, Any]:
        key = (RUN_CANDIDATE_TOOL, request.pipeline_variant)
        occurrence = self._occurrences.get(key, 0) + 1
        self._occurrences[key] = occurrence
        failure = _scheduled_failure(
            self.case.scenario, request.pipeline_variant, occurrence
        )
        if failure is not None:
            error_code, retryable = failure
            self._record(
                RUN_CANDIDATE_TOOL,
                request.pipeline_variant,
                "failed",
                error_code,
            )
            raise AgentToolError(error_code, retryable=retryable)
        output = _candidate_output(
            self.case,
            self.evidence,
            baseline_run_id=self._baseline_output["payload"]["run_id"],
            variant=request.pipeline_variant,
        )
        self._record(
            RUN_CANDIDATE_TOOL,
            request.pipeline_variant,
            "succeeded",
            None,
        )
        return output

    def _record(
        self,
        tool_name: str,
        pipeline_variant: str | None,
        status: str,
        error_code: str | None,
    ) -> None:
        self._ledger.append(
            RecordedToolCall(
                tool_name=tool_name,
                pipeline_variant=pipeline_variant,
                profile="smoke" if tool_name == DIAGNOSE_BASELINE_TOOL else None,
                status=status,
                error_code=error_code,
            )
        )


class StimulusPlanner:
    """Finite enum-based adversarial wrapper; never accepts arbitrary actions."""

    def __init__(self, case: AgentEvalCase) -> None:
        self.case = case
        self.delegate = ObservationDrivenRetrievalPlanner()
        self.planner_id = f"agent-eval-{case.planner_stimulus}-v1"

    def decide(self, view: PlannerView):
        if self.case.planner_stimulus == "unauthorized_tool" and not view.observations:
            return ToolAction(
                tool_name="shell",
                arguments={},
                reason_code="attempt_unauthorized_tool",
            )
        if self.case.planner_stimulus == "ungrounded_finish" and not view.observations:
            return FinishDecision(
                outcome=TerminalOutcome.PROPOSAL_READY,
                evidence_refs=["run:retrieval-deadbeefdead"],
                reason_code="claim_unobserved_evidence",
            )
        if self.case.planner_stimulus == "locked_profile" and not view.observations:
            return ToolAction(
                tool_name=DIAGNOSE_BASELINE_TOOL,
                arguments={"profile": "dev"},
                reason_code="attempt_locked_profile",
            )
        decision = self.delegate.decide(view)
        if (
            self.case.planner_stimulus == "skip_conservative"
            and len(view.observations) == 2
            and isinstance(decision, ToolAction)
        ):
            return ToolAction(
                tool_name=RUN_CANDIDATE_TOOL,
                arguments={
                    "baseline_run_id": view.observations[0].payload["run_id"],
                    "pipeline_variant": AGGRESSIVE_VARIANT,
                },
                reason_code="skip_required_candidate",
            )
        return decision


def planner_for_case(case: AgentEvalCase):
    if case.planner_stimulus == "none":
        return ObservationDrivenRetrievalPlanner()
    return StimulusPlanner(case)


def subject_kind_for_case(case: AgentEvalCase) -> str:
    return (
        "production_planner" if case.planner_stimulus == "none" else "harness_stimulus"
    )


def run_fixed_workflow(
    case: AgentEvalCase, evidence: CanonicalEvidence
) -> FixedWorkflowExecution:
    """Execute the bounded always-run-all-candidates reference workflow."""

    backend = ScriptedRetrievalTools(case, evidence)
    registry = backend.build_registry()
    capabilities = frozenset(
        {DIAGNOSE_RETRIEVAL_CAPABILITY, EXPERIMENT_RETRIEVAL_CAPABILITY}
    )
    try:
        baseline = registry.execute(
            DIAGNOSE_BASELINE_TOOL,
            {"profile": "smoke"},
            allowed_capabilities=capabilities,
        )
        passing = False
        for variant in RETRIEVAL_PIPELINE_VARIANTS:
            result = registry.execute(
                RUN_CANDIDATE_TOOL,
                {
                    "baseline_run_id": baseline["payload"]["run_id"],
                    "pipeline_variant": variant,
                },
                allowed_capabilities=capabilities,
            )
            passing = passing or result["payload"]["gate"]["passed"] is True
    except AgentToolError:
        return FixedWorkflowExecution(
            success=False,
            tool_calls_used=len(backend.ledger),
            outcome_code="fixed_workflow_tool_failed",
        )
    return FixedWorkflowExecution(
        success=True,
        tool_calls_used=len(backend.ledger),
        outcome_code=(
            "fixed_workflow_candidate_available"
            if passing
            else "fixed_workflow_no_safe_candidate"
        ),
    )


def _baseline_output(
    case: AgentEvalCase, evidence: CanonicalEvidence
) -> dict[str, Any]:
    payload = copy.deepcopy(evidence.baseline.payload)
    suffix = _suffix(case.task_id, "baseline")
    run_id = f"retrieval-{suffix}"
    diagnosis_id = f"stage-diagnosis-{_suffix(case.task_id, 'baseline-diagnosis')}"
    pipeline_id = f"pipeline-{_suffix(case.task_id, 'baseline-pipeline')}"
    payload.update(
        {
            "run_id": run_id,
            "diagnosis_id": diagnosis_id,
            "pipeline_id": pipeline_id,
        }
    )
    payload["artifacts"] = {
        "comparison_id": None,
        "diagnosis_id": diagnosis_id,
        "retrieval_run_id": run_id,
    }
    return BaselineDiagnosisOutput(
        evidence_ref=f"run:{run_id}", payload=payload
    ).model_dump(mode="json")


def _candidate_output(
    case: AgentEvalCase,
    evidence: CanonicalEvidence,
    *,
    baseline_run_id: str,
    variant: str,
) -> dict[str, Any]:
    template = (
        evidence.conservative_passed
        if _variant_passes(case.scenario, variant)
        else evidence.uniform_failed
    )
    payload = copy.deepcopy(template.payload)
    run_id = f"retrieval-{_suffix(case.task_id, variant + '-run')}"
    diagnosis_id = f"stage-diagnosis-{_suffix(case.task_id, variant + '-diagnosis')}"
    comparison_id = (
        f"retrieval-comparison-{_suffix(case.task_id, variant + '-comparison')}"
    )
    pipeline_id = f"pipeline-{_suffix(case.task_id, variant + '-pipeline')}"
    payload.update(
        {
            "baseline_run_id": baseline_run_id,
            "candidate_run_id": run_id,
            "comparison_id": comparison_id,
            "diagnosis_id": diagnosis_id,
            "pipeline_id": pipeline_id,
            "pipeline_variant": variant,
        }
    )
    payload["artifacts"] = {
        "comparison_id": comparison_id,
        "diagnosis_id": diagnosis_id,
        "retrieval_run_id": run_id,
    }
    return CandidateExperimentOutput(
        evidence_ref=f"comparison:{comparison_id}", payload=payload
    ).model_dump(mode="json")


def _variant_passes(scenario: ScenarioId, variant: str) -> bool:
    if scenario in {
        ScenarioId.UNIFORM_PASS,
        ScenarioId.ONE_RETRY_RECOVERS,
        ScenarioId.SECOND_FAILURE_STOPS,
        ScenarioId.NONRETRYABLE_STOPS,
    }:
        return variant == UNIFORM_VARIANT
    if scenario == ScenarioId.NO_SAFE_CANDIDATE:
        return False
    return variant == CONSERVATIVE_VARIANT


def _scheduled_failure(
    scenario: ScenarioId, variant: str, occurrence: int
) -> tuple[str, bool] | None:
    if variant != UNIFORM_VARIANT:
        return None
    if scenario == ScenarioId.ONE_RETRY_RECOVERS and occurrence == 1:
        return "tool_timeout", True
    if scenario == ScenarioId.SECOND_FAILURE_STOPS and occurrence <= 2:
        return "tool_timeout", True
    if scenario == ScenarioId.NONRETRYABLE_STOPS and occurrence == 1:
        return "retrieval_backend_unavailable", False
    return None


def _suffix(task_id: str, part: str) -> str:
    return hashlib.sha256(f"{task_id}:{part}".encode()).hexdigest()[:12]
