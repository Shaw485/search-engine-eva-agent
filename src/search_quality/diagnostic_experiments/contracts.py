"""Strict contracts for diagnostic-guided, non-mutating experiment plans."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from search_quality.agent.contracts import StrictModel
from search_quality.data.contracts import canonical_json_sha256

SHA256_PATTERN = r"^[0-9a-f]{64}$"
DIAGNOSTIC_ID_PATTERN = r"^bad-case-[0-9a-f]{12}$"
QUERY_SET_ID_PATTERN = r"^query-set-[0-9a-f]{12}$"
INDEX_ID_PATTERN = r"^catalog-baseline-v1-[0-9a-f]{12}$"
QUERY_CASE_ID_PATTERN = r"^query-case-[0-9a-f]{12}$"
STRATEGY_SPEC_ID_PATTERN = r"^strategy-spec-[0-9a-f]{12}$"
EXPERIMENT_PLAN_ID_PATTERN = r"^diagnostic-experiment-plan-[0-9a-f]{12}$"
ORACLE_ID_PATTERN = r"^oracle-[0-9a-f]{12}$"
QUERY_ROUTE_ID_PATTERN = r"^query-route-[0-9a-f]{12}$"
QUERY_ROUTE_PLAN_ID_PATTERN = r"^query-route-plan-[0-9a-f]{12}$"

Sha256 = Annotated[StrictStr, Field(pattern=SHA256_PATTERN)]


class QualityEvidenceStatus(StrEnum):
    """How independent the labels available to a future quality run are."""

    BEHAVIOR_ONLY = "behavior_only"
    DEVELOPMENT_SMOKE = "development_smoke"
    INDEPENDENT_ORACLE = "independent_oracle"


class ResolvedDiagnosticEvidence(StrictModel):
    """Privacy-safe facts reconstructed from a trusted diagnostic and Query set."""

    schema_version: Literal["resolved-diagnostic-evidence-v1"] = (
        "resolved-diagnostic-evidence-v1"
    )
    diagnostic_id: StrictStr = Field(pattern=DIAGNOSTIC_ID_PATTERN)
    query_set_id: StrictStr = Field(pattern=QUERY_SET_ID_PATTERN)
    index_id: StrictStr = Field(pattern=INDEX_ID_PATTERN)
    search_strategy_id: Literal["sqlite-fts5-bm25"] = "sqlite-fts5-bm25"
    query_count: Literal[59] = 59
    original_count: Literal[20] = 20
    synthetic_count: Literal[39] = 39
    diagnostic_candidate_count: StrictInt = Field(ge=0, le=59)
    identity_zero_result_case_ids: tuple[StrictStr, ...] = Field(max_length=20)
    spelling_sensitive_case_ids: tuple[StrictStr, ...] = Field(max_length=20)
    order_sensitive_case_ids: tuple[StrictStr, ...] = Field(max_length=19)
    ranking_instability_case_ids: tuple[StrictStr, ...] = Field(max_length=39)
    formal_evaluation_allowed: Literal[False] = False
    relevance_labels_used: Literal[False] = False
    quality_metrics_computed: Literal[False] = False
    stage_drop_diagnostics_computed: Literal[False] = False
    protected_profile_dispatch_count: Literal[0] = 0
    strategy_write_count: Literal[0] = 0

    @model_validator(mode="after")
    def validate_case_sets(self) -> Self:
        case_groups = (
            self.identity_zero_result_case_ids,
            self.spelling_sensitive_case_ids,
            self.order_sensitive_case_ids,
            self.ranking_instability_case_ids,
        )
        for items in case_groups:
            if tuple(sorted(items)) != items or len(items) != len(set(items)):
                raise ValueError(
                    "resolved diagnostic case IDs must be unique and sorted"
                )
            if any(not _matches_query_case_id(item) for item in items):
                raise ValueError(
                    "resolved diagnostic contains an invalid Query case ID"
                )
        return self

    @property
    def identity_zero_result_count(self) -> int:
        return len(self.identity_zero_result_case_ids)

    @property
    def spelling_sensitive_count(self) -> int:
        return len(self.spelling_sensitive_case_ids)


class StrategySpec(StrictModel):
    """The only strategy DSL admitted by the first experiment-planning slice."""

    schema_version: Literal["diagnostic-strategy-spec-v1"] = (
        "diagnostic-strategy-spec-v1"
    )
    strategy_spec_id: StrictStr = Field(pattern=STRATEGY_SPEC_ID_PATTERN)
    strategy_id: Literal["zero-result-drop-one-token-backoff-v1"]
    family: Literal["zero_result_backoff"]
    primary_operator: Literal["strict_and"]
    fallback_trigger: Literal["primary_zero_result"]
    fallback_operator: Literal["drop_one_non_protected_token"]
    protected_token_policy: Literal["numeric_model_and_explicit_product_id_required"]
    fusion: Literal["rrf"]
    top_k: Literal[10] = 10
    max_fallback_routes: Literal[16] = 16

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        expected = strategy_spec_id(
            self.model_dump(mode="json", exclude={"strategy_spec_id"})
        )
        if self.strategy_spec_id != expected:
            raise ValueError("strategy spec ID does not match its content")
        return self


class BehavioralLanePlan(StrictModel):
    schema_version: Literal["behavioral-experiment-lane-v1"] = (
        "behavioral-experiment-lane-v1"
    )
    lane_id: Literal["full-catalog-59-case-behavioral-v1"] = (
        "full-catalog-59-case-behavioral-v1"
    )
    query_count: Literal[59] = 59
    relevance_labels_used: Literal[False] = False
    quality_metrics_allowed: Literal[False] = False
    observables: tuple[
        Literal[
            "zero_result_recovery_count",
            "ordered_top_k_change_count",
            "operational_failure_count",
            "nonzero_baseline_preservation_count",
        ],
        ...,
    ] = (
        "zero_result_recovery_count",
        "ordered_top_k_change_count",
        "operational_failure_count",
        "nonzero_baseline_preservation_count",
    )


class QualityLanePlan(StrictModel):
    schema_version: Literal["quality-experiment-lane-v1"] = "quality-experiment-lane-v1"
    evidence_status: QualityEvidenceStatus
    query_scope: Literal[
        "not_scheduled",
        "development_identity_queries_only",
        "independent_oracle_queries",
    ]
    label_source_ref: StrictStr | None = None
    labels_may_be_used_by_harness: StrictBool
    synthetic_labels_may_be_inherited: Literal[False] = False
    quality_conclusion_allowed: Literal[False] = False
    reason_code: Literal[
        "no_eligible_quality_labels_resolved",
        "development_smoke_is_not_independent",
        "experiment_not_yet_run_against_independent_oracle",
    ]

    @model_validator(mode="after")
    def validate_evidence_boundary(self) -> Self:
        expected = {
            QualityEvidenceStatus.BEHAVIOR_ONLY: (
                "not_scheduled",
                None,
                False,
                "no_eligible_quality_labels_resolved",
            ),
            QualityEvidenceStatus.DEVELOPMENT_SMOKE: (
                "development_identity_queries_only",
                "esci-stage1-smoke-v1",
                True,
                "development_smoke_is_not_independent",
            ),
        }
        if self.evidence_status in expected:
            observed = (
                self.query_scope,
                self.label_source_ref,
                self.labels_may_be_used_by_harness,
                self.reason_code,
            )
            if observed != expected[self.evidence_status]:
                raise ValueError("quality lane does not match its evidence status")
        else:
            if (
                self.query_scope != "independent_oracle_queries"
                or self.label_source_ref is None
                or not _matches_oracle_id(self.label_source_ref)
                or not self.labels_may_be_used_by_harness
                or self.reason_code
                != "experiment_not_yet_run_against_independent_oracle"
            ):
                raise ValueError("independent Oracle quality lane is malformed")
        return self


PlanStatus = Literal[
    "experiment_planned",
    "requires_oracle",
    "requires_engineering",
    "no_supported_experiment",
]

FalsifierCode = Literal[
    "no_zero_result_recovery",
    "no_independently_judged_relevant_gain",
    "quality_or_safety_gate_regression",
    "nonzero_baseline_results_changed",
    "execution_budget_exceeded",
]


class DiagnosticExperimentPlan(StrictModel):
    """One deterministic plan. It is not an experiment Run or quality verdict."""

    schema_version: Literal["diagnostic-experiment-plan-v1"] = (
        "diagnostic-experiment-plan-v1"
    )
    experiment_plan_id: StrictStr = Field(pattern=EXPERIMENT_PLAN_ID_PATTERN)
    router_id: Literal["diagnostic-evidence-router-v1"] = (
        "diagnostic-evidence-router-v1"
    )
    status: PlanStatus
    reason_code: Literal[
        "identity_zero_result_backoff_prioritized",
        "spelling_sensitive_requires_independent_oracle",
        "spelling_correction_requires_engineering",
        "unjudged_ranking_change_requires_oracle",
        "no_allowlisted_strategy_matches_evidence",
    ]
    recommended_next_action: Literal[
        "run_bounded_two_lane_experiment",
        "create_independent_relevance_oracle",
        "implement_allowlisted_spelling_recall",
        "stop_without_strategy_change",
    ]
    diagnostic_id: StrictStr = Field(pattern=DIAGNOSTIC_ID_PATTERN)
    query_set_id: StrictStr = Field(pattern=QUERY_SET_ID_PATTERN)
    index_id: StrictStr = Field(pattern=INDEX_ID_PATTERN)
    hypothesis: StrictStr = Field(min_length=1, max_length=1000)
    target_case_ids: tuple[StrictStr, ...] = Field(max_length=59)
    strategy: StrategySpec | None
    behavioral_lane: BehavioralLanePlan
    quality_lane: QualityLanePlan
    falsifiers: tuple[FalsifierCode, ...] = Field(max_length=5)
    quality_conclusion_allowed: Literal[False] = False
    activation_eligible: Literal[False] = False
    strategy_write_count: Literal[0] = 0

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        if tuple(sorted(self.target_case_ids)) != self.target_case_ids or len(
            self.target_case_ids
        ) != len(set(self.target_case_ids)):
            raise ValueError("experiment target case IDs must be unique and sorted")
        if self.status == "experiment_planned":
            if self.strategy is None or not self.target_case_ids:
                raise ValueError("runnable plan needs a strategy and target cases")
            expected_falsifiers = (
                "no_zero_result_recovery",
                "no_independently_judged_relevant_gain",
                "quality_or_safety_gate_regression",
                "nonzero_baseline_results_changed",
                "execution_budget_exceeded",
            )
            if self.falsifiers != expected_falsifiers:
                raise ValueError("runnable plan has an incomplete falsifier set")
        elif self.strategy is not None:
            raise ValueError("blocked plan must not carry an executable strategy")
        expected_id = experiment_plan_id(
            self.model_dump(mode="json", exclude={"experiment_plan_id"})
        )
        if self.experiment_plan_id != expected_id:
            raise ValueError("experiment plan ID does not match its content")
        return self


class QueryRoute(StrictModel):
    route_id: StrictStr = Field(pattern=QUERY_ROUTE_ID_PATTERN)
    kind: Literal["primary", "fallback"]
    operator: Literal["strict_and"] = "strict_and"
    tokens: tuple[StrictStr, ...] = Field(min_length=1, max_length=16)
    dropped_token_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def validate_route(self) -> Self:
        if len(self.tokens) != len(set(self.tokens)):
            raise ValueError("Query route tokens must be deduplicated")
        if (self.kind == "primary") != (self.dropped_token_sha256 is None):
            raise ValueError("Query route drop evidence does not match route kind")
        expected_id = query_route_id(self.model_dump(mode="json", exclude={"route_id"}))
        if self.route_id != expected_id:
            raise ValueError("Query route ID does not match its content")
        return self


class QueryRoutePlan(StrictModel):
    schema_version: Literal["query-route-plan-v1"] = "query-route-plan-v1"
    route_plan_id: StrictStr = Field(pattern=QUERY_ROUTE_PLAN_ID_PATTERN)
    strategy_spec_id: StrictStr = Field(pattern=STRATEGY_SPEC_ID_PATTERN)
    query_sha256: Sha256
    primary_returned_at_k: StrictInt = Field(ge=0, le=10)
    primary: QueryRoute
    fallback_routes: tuple[QueryRoute, ...] = Field(max_length=16)
    fallback_triggered: StrictBool
    protected_token_sha256s: tuple[Sha256, ...] = Field(max_length=16)

    @model_validator(mode="after")
    def validate_routes(self) -> Self:
        if self.primary.kind != "primary":
            raise ValueError("route plan primary route is malformed")
        if any(route.kind != "fallback" for route in self.fallback_routes):
            raise ValueError("route plan contains a non-fallback route")
        if len({route.route_id for route in self.fallback_routes}) != len(
            self.fallback_routes
        ):
            raise ValueError("fallback Query routes must be unique")
        expected_trigger = self.primary_returned_at_k == 0
        if self.fallback_triggered is not expected_trigger:
            raise ValueError("fallback trigger does not match primary results")
        if not expected_trigger and self.fallback_routes:
            raise ValueError("fallback routes require a zero-result primary route")
        expected_id = query_route_plan_id(
            self.model_dump(mode="json", exclude={"route_plan_id"})
        )
        if self.route_plan_id != expected_id:
            raise ValueError("Query route plan ID does not match its content")
        return self


def strategy_spec_id(payload_without_id: dict[str, object]) -> str:
    return f"strategy-spec-{canonical_json_sha256(payload_without_id)[:12]}"


def experiment_plan_id(payload_without_id: dict[str, object]) -> str:
    return (
        f"diagnostic-experiment-plan-{canonical_json_sha256(payload_without_id)[:12]}"
    )


def query_route_id(payload_without_id: dict[str, object]) -> str:
    return f"query-route-{canonical_json_sha256(payload_without_id)[:12]}"


def query_route_plan_id(payload_without_id: dict[str, object]) -> str:
    return f"query-route-plan-{canonical_json_sha256(payload_without_id)[:12]}"


def _matches_query_case_id(value: str) -> bool:
    import re

    return re.fullmatch(QUERY_CASE_ID_PATTERN, value) is not None


def _matches_oracle_id(value: str) -> bool:
    import re

    return re.fullmatch(ORACLE_ID_PATTERN, value) is not None
