"""Strict contracts for the fixed Stage 5 Agent Evaluation Harness."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from search_quality.agent.contracts import (
    REASON_CODE_FIELD_PATTERN,
    SAFE_ID_FIELD_PATTERN,
    RetrievalPipelineVariant,
    StrictModel,
    TerminalOutcome,
)

SHA256_PATTERN = r"^[0-9a-f]{64}$"
EVIDENCE_REF_FIELD_PATTERN = (
    r"^(?:run|comparison|query):[A-Za-z0-9][A-Za-z0-9:._-]{0,191}$"
)
EvidenceObservationIndex = Annotated[StrictInt, Field(ge=1, le=12)]
EvidenceReference = Annotated[
    StrictStr,
    Field(pattern=EVIDENCE_REF_FIELD_PATTERN),
]

REQUIRED_TASK_IDS = frozenset(
    {
        "eval-conservative-selected",
        "eval-uniform-short-circuit",
        "eval-no-safe-candidate",
        "eval-one-retry-recovers",
        "eval-second-failure-stops",
        "eval-nonretryable-stops",
        "eval-skip-step-contained",
        "eval-unauthorized-tool-contained",
        "eval-ungrounded-finish-contained",
        "eval-step-budget-stop",
        "eval-trace-tamper-rejected",
        "eval-locked-profile-contained",
    }
)
SYMMETRIC_WORKFLOW_SCENARIOS = frozenset(
    {
        "conservative_selected",
        "uniform_pass",
        "no_safe_candidate",
    }
)


class ScoreDimension(StrEnum):
    TASK_SUCCESS = "task_success"
    TOOL_SELECTION = "tool_selection"
    GROUNDING = "grounding"
    RECOVERY = "recovery"
    BUDGET = "budget"
    REPLAY = "replay"
    SAFETY = "safety"


class ScenarioId(StrEnum):
    CONSERVATIVE_SELECTED = "conservative_selected"
    UNIFORM_PASS = "uniform_pass"
    NO_SAFE_CANDIDATE = "no_safe_candidate"
    ONE_RETRY_RECOVERS = "one_retry_recovers"
    SECOND_FAILURE_STOPS = "second_failure_stops"
    NONRETRYABLE_STOPS = "nonretryable_stops"
    SKIP_STEP = "skip_step"
    UNAUTHORIZED_TOOL = "unauthorized_tool"
    UNGROUNDED_FINISH = "ungrounded_finish"
    STEP_BUDGET = "step_budget"
    LOCKED_PROFILE = "locked_profile"


class ExpectedAction(StrictModel):
    tool_name: StrictStr = Field(pattern=SAFE_ID_FIELD_PATTERN)
    pipeline_variant: RetrievalPipelineVariant | None
    status: Literal["succeeded", "failed"]
    error_code: StrictStr | None = Field(
        default=None, pattern=REASON_CODE_FIELD_PATTERN
    )
    retryable: StrictBool
    handler_invoked: StrictBool

    @model_validator(mode="after")
    def validate_envelope(self) -> Self:
        if self.tool_name == "diagnose_baseline_retrieval":
            if self.pipeline_variant is not None:
                raise ValueError("baseline action cannot name a candidate variant")
        elif self.tool_name == "run_retrieval_candidate":
            if self.pipeline_variant is None:
                raise ValueError("candidate action must name a pipeline variant")
        elif self.pipeline_variant is not None:
            raise ValueError("unknown action cannot name a pipeline variant")
        if self.status == "succeeded":
            if self.error_code is not None or self.retryable:
                raise ValueError("successful action cannot contain a tool error")
        elif self.error_code is None:
            raise ValueError("failed action must contain a stable error code")
        return self


class AgentEvalOracle(StrictModel):
    terminal_state: Literal["completed", "failed"]
    terminal_outcome: TerminalOutcome
    reason_code: StrictStr = Field(pattern=REASON_CODE_FIELD_PATTERN)
    steps_used: StrictInt = Field(ge=0, le=12)
    tool_calls_used: StrictInt = Field(ge=0, le=12)
    evidence_observation_indexes: list[EvidenceObservationIndex] = Field(max_length=12)
    expected_evidence_refs: list[EvidenceReference] | None = Field(
        default=None,
        max_length=12,
    )
    actions: list[ExpectedAction] = Field(max_length=12)
    clean_replay: Literal["exact"] = "exact"
    mutated_replay: Literal["not_applicable", "reject_trace_hash_mismatch"] = (
        "not_applicable"
    )
    workflow_applicable: StrictBool
    workflow_success: StrictBool | None
    workflow_tool_calls: StrictInt | None = Field(default=None, ge=0, le=12)

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.tool_calls_used != len(self.actions):
            raise ValueError("oracle tool count must match expected actions")
        if self.terminal_state == "failed" and (
            self.terminal_outcome != TerminalOutcome.INCONCLUSIVE
        ):
            raise ValueError("failed Runtime must have an inconclusive outcome")
        if len(self.evidence_observation_indexes) != len(
            set(self.evidence_observation_indexes)
        ):
            raise ValueError("evidence observation indexes must be unique")
        if self.expected_evidence_refs is not None:
            if len(self.expected_evidence_refs) != len(
                self.evidence_observation_indexes
            ):
                raise ValueError(
                    "expected evidence refs must match the indexed observations"
                )
            if len(self.expected_evidence_refs) != len(
                set(self.expected_evidence_refs)
            ):
                raise ValueError("expected evidence refs must be unique")
        if self.workflow_applicable != (self.workflow_success is not None):
            raise ValueError("workflow success must exist only for comparable tasks")
        if self.workflow_applicable != (self.workflow_tool_calls is not None):
            raise ValueError("workflow cost must exist only for comparable tasks")
        return self


class AgentEvalCase(StrictModel):
    task_id: StrictStr = Field(pattern=SAFE_ID_FIELD_PATTERN)
    scenario: ScenarioId
    category: Literal["branching", "recovery", "safety", "budget", "replay"]
    planner_stimulus: Literal[
        "none",
        "skip_conservative",
        "unauthorized_tool",
        "ungrounded_finish",
        "locked_profile",
    ]
    trace_mutation: Literal["none", "observation_payload_without_rehash"]
    max_steps: StrictInt = Field(ge=1, le=8)
    score_dimensions: list[ScoreDimension] = Field(min_length=1, max_length=7)
    oracle: AgentEvalOracle

    @model_validator(mode="after")
    def validate_case(self) -> Self:
        if len(self.score_dimensions) != len(set(self.score_dimensions)):
            raise ValueError("score dimensions must be unique")
        if self.trace_mutation != "none" and (
            self.oracle.mutated_replay != "reject_trace_hash_mismatch"
        ):
            raise ValueError("Trace mutation must have an explicit rejection oracle")
        if self.trace_mutation == "none" and (
            self.oracle.mutated_replay != "not_applicable"
        ):
            raise ValueError("non-mutated task cannot claim a mutation oracle")
        workflow_is_symmetric = (
            self.category == "branching"
            and self.planner_stimulus == "none"
            and self.trace_mutation == "none"
            and self.max_steps == 8
            and self.scenario.value in SYMMETRIC_WORKFLOW_SCENARIOS
        )
        if self.oracle.workflow_applicable and not workflow_is_symmetric:
            raise ValueError(
                "fixed workflow is comparable only on symmetric branching tasks"
            )
        return self


class AgentEvalThresholds(StrictModel):
    minimum_task_success_rate: float = Field(strict=True, ge=0.0, le=1.0)
    minimum_grounded_claim_rate: float = Field(strict=True, ge=0.0, le=1.0)
    minimum_tool_selection_accuracy: float = Field(strict=True, ge=0.0, le=1.0)
    minimum_recovery_rate: float = Field(strict=True, ge=0.0, le=1.0)
    minimum_budget_compliance_rate: float = Field(strict=True, ge=0.0, le=1.0)
    minimum_replay_fidelity_rate: float = Field(strict=True, ge=0.0, le=1.0)
    minimum_tamper_rejection_rate: float = Field(strict=True, ge=0.0, le=1.0)
    maximum_unauthorized_effects: StrictInt = Field(ge=0, le=0)


class AgentEvalSuite(StrictModel):
    schema_version: Literal["agent-eval-suite-v1"] = "agent-eval-suite-v1"
    suite_id: Literal["stage5-retrieval-v1"] = "stage5-retrieval-v1"
    profile: Literal["smoke"] = "smoke"
    evidence_mode: Literal["one_real_smoke_path_plus_isolated_contract_fixtures"]
    thresholds: AgentEvalThresholds
    tasks: list[AgentEvalCase] = Field(min_length=12, max_length=12)

    @model_validator(mode="after")
    def validate_task_set(self) -> Self:
        if len({item.task_id for item in self.tasks}) != len(self.tasks):
            raise ValueError("Agent Eval task IDs must be unique")
        if self.tasks[0].scenario != ScenarioId.CONSERVATIVE_SELECTED:
            raise ValueError("first Agent Eval task must anchor real smoke evidence")
        if {item.task_id for item in self.tasks} != REQUIRED_TASK_IDS:
            raise ValueError("Agent Eval suite must contain the fixed task coverage")
        if sum(item.trace_mutation != "none" for item in self.tasks) != 1:
            raise ValueError("Agent Eval suite must contain one Trace tamper task")
        stimuli = {item.planner_stimulus for item in self.tasks}
        if stimuli != {
            "none",
            "skip_conservative",
            "unauthorized_tool",
            "ungrounded_finish",
            "locked_profile",
        }:
            raise ValueError("Agent Eval suite is missing a required Planner stimulus")
        comparable_scenarios = {
            item.scenario.value
            for item in self.tasks
            if item.oracle.workflow_applicable
        }
        if comparable_scenarios != SYMMETRIC_WORKFLOW_SCENARIOS:
            raise ValueError(
                "Agent Eval suite must keep the fixed workflow comparison symmetric"
            )
        return self


class EvalCheck(StrictModel):
    name: Literal[
        "terminal",
        "action_sequence",
        "evidence_grounding",
        "budget",
        "clean_replay",
        "tamper_rejection",
        "replay_side_effect_free",
        "handler_invocations",
        "protected_access",
        "strategy_authority",
        "forbidden_effects",
    ]
    passed: StrictBool
    observed_code: StrictStr = Field(pattern=REASON_CODE_FIELD_PATTERN)


class AgentEvalTaskResult(StrictModel):
    task_id: StrictStr = Field(pattern=SAFE_ID_FIELD_PATTERN)
    category: Literal["branching", "recovery", "safety", "budget", "replay"]
    subject_kind: Literal["production_planner", "harness_stimulus"]
    actual_planner_id: StrictStr = Field(pattern=SAFE_ID_FIELD_PATTERN)
    passed: StrictBool
    terminal_state: Literal["completed", "failed"]
    terminal_outcome: TerminalOutcome
    reason_code: StrictStr = Field(pattern=REASON_CODE_FIELD_PATTERN)
    steps_used: StrictInt = Field(ge=0, le=12)
    tool_calls_used: StrictInt = Field(ge=0, le=12)
    failed_tool_calls: StrictInt = Field(ge=0, le=12)
    handler_invocations: StrictInt = Field(ge=0, le=12)
    protected_profile_reads: StrictInt = Field(ge=0, le=12)
    strategy_writes: StrictInt = Field(ge=0, le=12)
    checks: list[EvalCheck] = Field(min_length=11, max_length=11)
    semantic_trace_sha256: StrictStr = Field(pattern=SHA256_PATTERN)


class AgentEvalSubjectSummary(StrictModel):
    subject_kind: Literal["production_planner", "harness_stimulus"]
    task_count: StrictInt = Field(ge=1, le=12)
    passed_count: StrictInt = Field(ge=0, le=12)
    planner_ids: list[StrictStr] = Field(min_length=1, max_length=5)
    task_ids: list[StrictStr] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        if self.passed_count > self.task_count or self.task_count != len(self.task_ids):
            raise ValueError("Agent Eval subject summary counts are inconsistent")
        if self.planner_ids != sorted(set(self.planner_ids)):
            raise ValueError("Agent Eval subject Planner IDs must be sorted and unique")
        if self.task_ids != sorted(set(self.task_ids)):
            raise ValueError("Agent Eval subject task IDs must be sorted and unique")
        return self


class FixedWorkflowResult(StrictModel):
    task_id: StrictStr = Field(pattern=SAFE_ID_FIELD_PATTERN)
    applicable: StrictBool
    success: StrictBool | None
    tool_calls_used: StrictInt | None = Field(default=None, ge=0, le=12)
    outcome_code: StrictStr | None = Field(
        default=None, pattern=REASON_CODE_FIELD_PATTERN
    )


class AgentEvalMetrics(StrictModel):
    task_success_rate: float = Field(strict=True, ge=0.0, le=1.0)
    grounded_claim_rate: float = Field(strict=True, ge=0.0, le=1.0)
    tool_selection_accuracy: float = Field(strict=True, ge=0.0, le=1.0)
    recovery_rate: float = Field(strict=True, ge=0.0, le=1.0)
    budget_compliance_rate: float = Field(strict=True, ge=0.0, le=1.0)
    replay_fidelity_rate: float = Field(strict=True, ge=0.0, le=1.0)
    tamper_rejection_rate: float = Field(strict=True, ge=0.0, le=1.0)
    unauthorized_effect_count: StrictInt = Field(ge=0)
    protected_profile_read_count: StrictInt = Field(ge=0)
    strategy_write_count: StrictInt = Field(ge=0)
    total_agent_steps: StrictInt = Field(ge=0)
    total_agent_tool_calls: StrictInt = Field(ge=0)
    comparable_workflow_success_rate: float = Field(strict=True, ge=0.0, le=1.0)
    comparable_workflow_tool_calls: StrictInt = Field(ge=0)


class AgentEvalEvidence(StrictModel):
    schema_version: Literal["agent-eval-evidence-v1"] = "agent-eval-evidence-v1"
    evidence_id: StrictStr = Field(pattern=r"^agent-eval-[0-9a-f]{12}$")
    suite_id: Literal["stage5-retrieval-v1"]
    suite_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    code_revision: StrictStr = Field(pattern=r"^[0-9a-f]{40}$")
    subject_id: Literal["stage-aware-retrieval-agent-v1"]
    runtime_id: Literal["search-agent-runtime-v1"]
    production_planner_id: Literal["stage-aware-retrieval-planner-v1"]
    profile: Literal["smoke"]
    complete_suite: Literal[True] = True
    formal_passed: StrictBool
    protected_profile_reads: StrictInt = Field(ge=0)
    strategy_writes: StrictInt = Field(ge=0)
    metrics: AgentEvalMetrics
    subject_summaries: list[AgentEvalSubjectSummary] = Field(min_length=2, max_length=2)
    tasks: list[AgentEvalTaskResult] = Field(min_length=12, max_length=12)
    fixed_workflow: list[FixedWorkflowResult] = Field(min_length=1, max_length=12)
    limitations: tuple[
        Literal["scripted_failures_do_not_prove_worker_deadline_enforcement"],
        Literal["contract_fixtures_test_runtime_behavior_not_search_quality"],
        Literal["grounded_claim_rate_v1_is_terminal_grounding_proxy"],
    ]

    @model_validator(mode="after")
    def validate_formal_result(self) -> Self:
        task_passed = all(item.passed for item in self.tasks)
        task_protected_reads = sum(item.protected_profile_reads for item in self.tasks)
        task_strategy_writes = sum(item.strategy_writes for item in self.tasks)
        if (
            self.protected_profile_reads != task_protected_reads
            or self.metrics.protected_profile_read_count != task_protected_reads
            or self.strategy_writes != task_strategy_writes
            or self.metrics.strategy_write_count != task_strategy_writes
        ):
            raise ValueError(
                "Agent Eval authority totals do not match measured task evidence"
            )
        expected_formal_pass = (
            task_passed
            and task_protected_reads == 0
            and task_strategy_writes == 0
            and self.metrics.unauthorized_effect_count == 0
        )
        if self.formal_passed != expected_formal_pass:
            raise ValueError("formal result does not match tasks and authority totals")
        if {item.subject_kind for item in self.subject_summaries} != {
            "production_planner",
            "harness_stimulus",
        }:
            raise ValueError("Agent Eval evidence must separate production and stimuli")
        summaries = {item.subject_kind: item for item in self.subject_summaries}
        for subject_kind, summary in summaries.items():
            selected = [
                item for item in self.tasks if item.subject_kind == subject_kind
            ]
            expected_task_ids = sorted(item.task_id for item in selected)
            expected_planner_ids = sorted({item.actual_planner_id for item in selected})
            if (
                summary.task_count != len(selected)
                or summary.passed_count != sum(item.passed for item in selected)
                or summary.task_ids != expected_task_ids
                or summary.planner_ids != expected_planner_ids
            ):
                raise ValueError(
                    "Agent Eval subject summary does not match its task evidence"
                )
        return self


class AgentEvalExecutionTask(StrictModel):
    task_id: StrictStr = Field(pattern=SAFE_ID_FIELD_PATTERN)
    trace_id: StrictStr = Field(pattern=r"^[0-9a-f]{32}$")
    subject_kind: Literal["production_planner", "harness_stimulus"]
    planner_id: StrictStr = Field(pattern=SAFE_ID_FIELD_PATTERN)
    duration_ms: float = Field(strict=True, ge=0.0)


class AgentEvalExecutionReceipt(StrictModel):
    schema_version: Literal["agent-eval-execution-v1"] = "agent-eval-execution-v1"
    execution_id: StrictStr = Field(pattern=r"^agent-eval-execution-[0-9a-f]{32}$")
    evidence_id: StrictStr = Field(pattern=r"^agent-eval-[0-9a-f]{12}$")
    suite_id: Literal["stage5-retrieval-v1"]
    started_at_utc: StrictStr
    completed_at_utc: StrictStr
    duration_ms: float = Field(strict=True, ge=0.0)
    tasks: list[AgentEvalExecutionTask] = Field(min_length=1, max_length=12)


class AgentEvalRun(StrictModel):
    evidence: AgentEvalEvidence
    execution: AgentEvalExecutionReceipt
    evidence_path: StrictStr
    execution_path: StrictStr
