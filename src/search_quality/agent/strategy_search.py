"""Deterministic diagnosis and bounded exact-boost strategy search.

This module deliberately does not execute Rankers or mutate the active strategy.
It turns privacy-sensitive bad-case evidence into safe structured diagnoses,
chooses from a fixed parameter allowlist, scores trusted comparison summaries,
and selects an evidence-backed winner for later human review.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections import Counter
from collections.abc import Sequence
from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from search_quality.text import tokenize

from .contracts import RUN_ID_PATTERN, StrictModel
from .tools import COMPARISON_ID_PATTERN, CompareRunsPayload, MetricDeltaOutput

logger = logging.getLogger("search_quality.agent_optimization.strategy_search")

STRATEGY_SEARCH_SCHEMA_VERSION = "strategy-search-v1"
SELECTION_SCORE_POLICY_VERSION = "exact-boost-selection-score-v1"
GATE_POLICY_VERSION = "smoke-release-gates-v2"
ROOT_CAUSE_ORDER = (
    "numeric_token",
    "coverage_gap",
    "exact_phrase_displaced",
    "missing_title_signal",
)
QUERY_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
DIAGNOSIS_ID_PATTERN = r"diagnosis-[0-9a-f]{12}"
CANDIDATE_SELECTION_ID_PATTERN = r"candidate-selection-[0-9a-f]{12}"
EVALUATION_ID_PATTERN = r"strategy-evaluation-[0-9a-f]{12}"
WINNER_SELECTION_ID_PATTERN = r"winner-selection-[0-9a-f]{12}"

FiniteFloat = Annotated[float, Field(strict=True, allow_inf_nan=False)]
UnitFloat = Annotated[
    float,
    Field(strict=True, allow_inf_nan=False, ge=0.0, le=1.0),
]
DeltaFloat = Annotated[
    float,
    Field(strict=True, allow_inf_nan=False, ge=-1.0, le=1.0),
]
NonNegativeBoost = Annotated[
    float,
    Field(strict=True, allow_inf_nan=False, ge=0.0, le=3.0),
]

RootCause = Literal[
    "numeric_token",
    "coverage_gap",
    "exact_phrase_displaced",
    "missing_title_signal",
]
CandidateId = Literal[
    "exact-conservative-v1",
    "exact-numeric-v1",
    "exact-coverage-v1",
    "exact-phrase-v1",
]
SelectionScoreComponentName = Literal[
    "ndcg@10_delta",
    "ndcg@5_delta",
    "mrr@10_delta",
    "success@1_delta",
    "success@5_delta",
    "ndcg@10_regression_rate",
    "worst_ndcg@10_regression_magnitude",
]
GateName = Literal[
    "ndcg@10_minimum",
    "ndcg@5_floor",
    "mrr@10_floor",
    "success@1_floor",
    "success@5_floor",
    "ndcg@10_regression_rate_ceiling",
    "worst_ndcg@10_regression_ceiling",
]


class BadCaseInput(StrictModel):
    """Minimal evidence needed to diagnose title-ranking symptoms.

    Text is accepted at this boundary but is never copied into outputs or logs.
    """

    query_id: StrictStr = Field(pattern=rf"^{QUERY_ID_PATTERN}$")
    query_text: StrictStr = Field(min_length=1, max_length=4096)
    relevant_title: StrictStr = Field(min_length=1, max_length=4096)
    baseline_top_title: StrictStr | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
    )
    relevant_rank: StrictInt = Field(ge=1)
    title_signal_used: StrictBool

    @model_validator(mode="after")
    def validate_searchable_text(self) -> Self:
        if not self.query_text.strip() or not tokenize(self.query_text):
            raise ValueError("query_text must contain an ASCII alphanumeric token")
        if not self.relevant_title.strip():
            raise ValueError("relevant_title must not be blank")
        if self.baseline_top_title is not None and not self.baseline_top_title.strip():
            raise ValueError("baseline_top_title must not be blank")
        return self


class DiagnosisSignals(StrictModel):
    query_token_count: StrictInt = Field(ge=1)
    numeric_query_token_count: StrictInt = Field(ge=0)
    numeric_missing_from_top_count: StrictInt = Field(ge=0)
    relevant_query_coverage: UnitFloat
    baseline_top_query_coverage: UnitFloat
    coverage_gap: UnitFloat
    exact_phrase_in_relevant: StrictBool
    exact_phrase_in_baseline_top: StrictBool
    relevant_rank: StrictInt = Field(ge=1)
    title_signal_used: StrictBool

    @model_validator(mode="after")
    def validate_signal_relationships(self) -> Self:
        if self.numeric_missing_from_top_count > self.numeric_query_token_count:
            raise ValueError("missing numeric count exceeds numeric Query count")
        expected_gap = max(
            0.0,
            self.relevant_query_coverage - self.baseline_top_query_coverage,
        )
        if not math.isclose(self.coverage_gap, expected_gap, abs_tol=1e-12):
            raise ValueError("coverage_gap does not match the coverage values")
        return self


class RootCauseFinding(StrictModel):
    cause: RootCause
    confidence: UnitFloat
    reason_code: RootCause

    @model_validator(mode="after")
    def validate_reason_code(self) -> Self:
        if self.reason_code != self.cause:
            raise ValueError("reason_code must match cause")
        return self


class BadCaseDiagnosis(StrictModel):
    schema_version: Literal["strategy-search-v1"] = STRATEGY_SEARCH_SCHEMA_VERSION
    diagnosis_id: StrictStr = Field(pattern=rf"^{DIAGNOSIS_ID_PATTERN}$")
    query_id: StrictStr = Field(pattern=rf"^{QUERY_ID_PATTERN}$")
    findings: list[RootCauseFinding] = Field(max_length=4)
    signals: DiagnosisSignals

    @model_validator(mode="after")
    def validate_findings(self) -> Self:
        causes = [finding.cause for finding in self.findings]
        if len(causes) != len(set(causes)):
            raise ValueError("diagnosis causes must be unique")
        expected = sorted(causes, key=ROOT_CAUSE_ORDER.index)
        if causes != expected:
            raise ValueError("diagnosis causes must use canonical order")
        return self


class ExactBoostParameters(StrictModel):
    coverage_boost: NonNegativeBoost
    numeric_boost: NonNegativeBoost
    phrase_boost: NonNegativeBoost

    @property
    def complexity(self) -> float:
        return round(
            self.coverage_boost + self.numeric_boost + self.phrase_boost,
            12,
        )


class ExactBoostCandidate(StrictModel):
    candidate_id: CandidateId
    ranker_id: Literal["candidate-title-bm25-exact-boost-v1"] = (
        "candidate-title-bm25-exact-boost-v1"
    )
    trigger_cause: RootCause
    parameters: ExactBoostParameters
    supporting_diagnosis_ids: list[StrictStr] = Field(min_length=1)
    supporting_query_ids: list[StrictStr] = Field(min_length=1)
    support_count: StrictInt = Field(ge=1)

    @model_validator(mode="after")
    def validate_support(self) -> Self:
        if self.supporting_diagnosis_ids != sorted(set(self.supporting_diagnosis_ids)):
            raise ValueError("supporting diagnosis IDs must be unique and sorted")
        if self.supporting_query_ids != sorted(set(self.supporting_query_ids)):
            raise ValueError("supporting Query IDs must be unique and sorted")
        if self.support_count != len(self.supporting_query_ids):
            raise ValueError("support_count must match supporting Query IDs")
        return self


class CandidateSelection(StrictModel):
    schema_version: Literal["strategy-search-v1"] = STRATEGY_SEARCH_SCHEMA_VERSION
    selection_id: StrictStr = Field(pattern=rf"^{CANDIDATE_SELECTION_ID_PATTERN}$")
    diagnosis_count: StrictInt = Field(ge=0)
    candidates: list[ExactBoostCandidate] = Field(max_length=4)

    @model_validator(mode="after")
    def validate_candidates(self) -> Self:
        candidate_ids = [candidate.candidate_id for candidate in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate IDs must be unique")
        return self


class GatePolicy(StrictModel):
    policy_version: Literal["smoke-release-gates-v2"] = GATE_POLICY_VERSION
    min_ndcg_at_10_delta: DeltaFloat = 1e-12
    min_ndcg_at_5_delta: DeltaFloat = -0.02
    min_mrr_at_10_delta: DeltaFloat = 0.0
    min_success_at_1_delta: DeltaFloat = 0.0
    min_success_at_5_delta: DeltaFloat = 0.0
    max_ndcg_at_10_regression_rate: UnitFloat = 0.15
    max_worst_ndcg_at_10_regression_magnitude: UnitFloat = 0.05


class CoreMetricDeltas(StrictModel):
    """Three user-facing quality metrics for one controlled experiment."""

    success_at_5: MetricDeltaOutput = Field(alias="success@5")
    mrr_at_10: MetricDeltaOutput = Field(alias="mrr@10")
    ndcg_at_10: MetricDeltaOutput = Field(alias="ndcg@10")


TRUSTED_SMOKE_GATE_POLICY = GatePolicy()


class SelectionScoreComponent(StrictModel):
    name: SelectionScoreComponentName
    value: FiniteFloat
    weight: FiniteFloat
    contribution: FiniteFloat

    @model_validator(mode="after")
    def validate_contribution(self) -> Self:
        if not math.isclose(
            self.contribution,
            round(self.value * self.weight, 12),
            abs_tol=1e-12,
        ):
            raise ValueError(
                "selection-score contribution does not match value times weight"
            )
        return self


class SelectionScoreBreakdown(StrictModel):
    policy_version: Literal["exact-boost-selection-score-v1"] = (
        SELECTION_SCORE_POLICY_VERSION
    )
    components: list[SelectionScoreComponent] = Field(min_length=7, max_length=7)
    total: FiniteFloat

    @model_validator(mode="after")
    def validate_total(self) -> Self:
        names = [component.name for component in self.components]
        if len(names) != len(set(names)):
            raise ValueError("selection-score component names must be unique")
        expected = round(sum(item.contribution for item in self.components), 12)
        if not math.isclose(self.total, expected, abs_tol=1e-12):
            raise ValueError("selection-score total does not match components")
        return self


class GateCheck(StrictModel):
    name: GateName
    comparator: Literal[">", ">=", "<="]
    observed: FiniteFloat
    threshold: FiniteFloat
    passed: StrictBool

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        expected = (
            self.observed > self.threshold
            if self.comparator == ">"
            else self.observed >= self.threshold
            if self.comparator == ">="
            else self.observed <= self.threshold
        )
        if self.passed is not expected:
            raise ValueError("gate result does not match observed value")
        return self


class GateResult(StrictModel):
    policy: GatePolicy
    checks: list[GateCheck] = Field(min_length=7, max_length=7)
    passed: StrictBool

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        names = [check.name for check in self.checks]
        if len(names) != len(set(names)):
            raise ValueError("gate names must be unique")
        if self.passed is not all(check.passed for check in self.checks):
            raise ValueError("gate summary does not match checks")
        return self


class StrategyEvaluation(StrictModel):
    schema_version: Literal["strategy-search-v1"] = STRATEGY_SEARCH_SCHEMA_VERSION
    evaluation_id: StrictStr = Field(pattern=rf"^{EVALUATION_ID_PATTERN}$")
    candidate: ExactBoostCandidate
    comparison_id: StrictStr = Field(pattern=rf"^{COMPARISON_ID_PATTERN}$")
    baseline_run_id: StrictStr = Field(pattern=rf"^{RUN_ID_PATTERN}$")
    candidate_run_id: StrictStr = Field(pattern=rf"^{RUN_ID_PATTERN}$")
    query_count: StrictInt = Field(ge=1)
    metrics: CoreMetricDeltas
    ndcg_at_10_delta: DeltaFloat
    mrr_at_10_delta: DeltaFloat
    success_at_5_delta: DeltaFloat
    selection_score: SelectionScoreBreakdown
    gates: GateResult
    eligible: StrictBool

    @model_validator(mode="after")
    def validate_eligibility(self) -> Self:
        if self.baseline_run_id == self.candidate_run_id:
            raise ValueError("comparison Runs must differ")
        if self.eligible is not self.gates.passed:
            raise ValueError("eligibility must match hard-gate result")
        legacy_deltas = (
            (self.ndcg_at_10_delta, self.metrics.ndcg_at_10.delta),
            (self.mrr_at_10_delta, self.metrics.mrr_at_10.delta),
            (self.success_at_5_delta, self.metrics.success_at_5.delta),
        )
        if any(
            not math.isclose(legacy, metric, rel_tol=0.0, abs_tol=1e-12)
            for legacy, metric in legacy_deltas
        ):
            raise ValueError("legacy metric deltas must match the core metrics")
        return self


class WinnerSelection(StrictModel):
    schema_version: Literal["strategy-search-v1"] = STRATEGY_SEARCH_SCHEMA_VERSION
    selection_id: StrictStr = Field(pattern=rf"^{WINNER_SELECTION_ID_PATTERN}$")
    status: Literal["winner_selected", "no_passing_candidate"]
    baseline_run_id: StrictStr | None = Field(
        default=None,
        pattern=rf"^{RUN_ID_PATTERN}$",
    )
    evaluated_candidate_count: StrictInt = Field(ge=0)
    eligible_candidate_count: StrictInt = Field(ge=0)
    ranked_candidate_ids: list[CandidateId]
    winner_candidate_id: CandidateId | None = None
    winner_evaluation_id: StrictStr | None = Field(
        default=None,
        pattern=rf"^{EVALUATION_ID_PATTERN}$",
    )

    @model_validator(mode="after")
    def validate_winner(self) -> Self:
        if self.eligible_candidate_count > self.evaluated_candidate_count:
            raise ValueError("eligible candidate count exceeds evaluated count")
        if len(self.ranked_candidate_ids) != self.eligible_candidate_count:
            raise ValueError("ranked candidate IDs must contain every eligible item")
        if len(self.ranked_candidate_ids) != len(set(self.ranked_candidate_ids)):
            raise ValueError("ranked candidate IDs must be unique")
        if self.status == "winner_selected":
            if not self.ranked_candidate_ids:
                raise ValueError("winner status requires an eligible candidate")
            if self.winner_candidate_id != self.ranked_candidate_ids[0]:
                raise ValueError("winner must be the first ranked candidate")
            if self.winner_evaluation_id is None:
                raise ValueError("winner status requires an evaluation ID")
        elif (
            self.winner_candidate_id is not None
            or self.winner_evaluation_id is not None
        ):
            raise ValueError("no-winner status cannot contain a winner")
        return self


_CANDIDATE_LIBRARY: dict[
    RootCause,
    tuple[CandidateId, ExactBoostParameters],
] = {
    "numeric_token": (
        "exact-numeric-v1",
        ExactBoostParameters(
            coverage_boost=0.8,
            numeric_boost=1.8,
            phrase_boost=1.0,
        ),
    ),
    "coverage_gap": (
        "exact-coverage-v1",
        ExactBoostParameters(
            coverage_boost=1.4,
            numeric_boost=1.0,
            phrase_boost=1.0,
        ),
    ),
    "exact_phrase_displaced": (
        "exact-phrase-v1",
        ExactBoostParameters(
            coverage_boost=0.6,
            numeric_boost=0.8,
            phrase_boost=1.8,
        ),
    ),
}

_CONSERVATIVE_CANDIDATE_ID: CandidateId = "exact-conservative-v1"
_CONSERVATIVE_PARAMETERS = ExactBoostParameters(
    coverage_boost=0.2,
    numeric_boost=0.3,
    phrase_boost=0.3,
)

_SELECTION_SCORE_WEIGHTS: tuple[tuple[SelectionScoreComponentName, float], ...] = (
    ("ndcg@10_delta", 0.50),
    ("ndcg@5_delta", 0.15),
    ("mrr@10_delta", 0.15),
    ("success@1_delta", 0.15),
    ("success@5_delta", 0.05),
    ("ndcg@10_regression_rate", -0.10),
    ("worst_ndcg@10_regression_magnitude", -0.10),
)


def diagnose_bad_case(case: BadCaseInput) -> BadCaseDiagnosis:
    """Return deterministic root-cause findings without retaining raw text."""

    query_terms = tuple(dict.fromkeys(tokenize(case.query_text)))
    query_set = frozenset(query_terms)
    relevant_terms = frozenset(tokenize(case.relevant_title))
    top_terms = frozenset(tokenize(case.baseline_top_title or ""))
    relevant_coverage = len(query_set & relevant_terms) / len(query_set)
    top_coverage = len(query_set & top_terms) / len(query_set)
    coverage_gap = max(0.0, relevant_coverage - top_coverage)
    numeric_terms = frozenset(
        term for term in query_terms if any(char.isdigit() for char in term)
    )
    relevant_numeric_evidence = numeric_terms & relevant_terms
    missing_numeric = relevant_numeric_evidence - top_terms
    normalized_query = " ".join(query_terms)
    relevant_text = " ".join(tokenize(case.relevant_title))
    top_text = " ".join(tokenize(case.baseline_top_title or ""))
    phrase_in_relevant = len(query_terms) >= 2 and normalized_query in relevant_text
    phrase_in_top = len(query_terms) >= 2 and normalized_query in top_text
    displaced = case.relevant_rank > 1

    findings: list[RootCauseFinding] = []
    if displaced and missing_numeric:
        findings.append(
            RootCauseFinding(
                cause="numeric_token",
                confidence=_rounded_unit(len(missing_numeric) / len(numeric_terms)),
                reason_code="numeric_token",
            )
        )
    if displaced and relevant_coverage > 0.0 and coverage_gap >= 0.25:
        findings.append(
            RootCauseFinding(
                cause="coverage_gap",
                confidence=_rounded_unit(coverage_gap),
                reason_code="coverage_gap",
            )
        )
    if displaced and phrase_in_relevant and not phrase_in_top:
        findings.append(
            RootCauseFinding(
                cause="exact_phrase_displaced",
                confidence=1.0,
                reason_code="exact_phrase_displaced",
            )
        )
    if displaced and not case.title_signal_used and relevant_coverage == 0.0:
        findings.append(
            RootCauseFinding(
                cause="missing_title_signal",
                confidence=1.0,
                reason_code="missing_title_signal",
            )
        )

    signals = DiagnosisSignals(
        query_token_count=len(query_terms),
        numeric_query_token_count=len(numeric_terms),
        numeric_missing_from_top_count=len(missing_numeric),
        relevant_query_coverage=_rounded_unit(relevant_coverage),
        baseline_top_query_coverage=_rounded_unit(top_coverage),
        coverage_gap=_rounded_unit(coverage_gap),
        exact_phrase_in_relevant=phrase_in_relevant,
        exact_phrase_in_baseline_top=phrase_in_top,
        relevant_rank=case.relevant_rank,
        title_signal_used=case.title_signal_used,
    )
    diagnosis_id = _content_id(
        "diagnosis",
        {
            "input": case.model_dump(mode="json"),
            "findings": [item.model_dump(mode="json") for item in findings],
            "signals": signals.model_dump(mode="json"),
        },
    )
    diagnosis = BadCaseDiagnosis(
        diagnosis_id=diagnosis_id,
        query_id=case.query_id,
        findings=findings,
        signals=signals,
    )
    logger.info(
        "bad_case_diagnosed",
        extra={
            "diagnosis_id": diagnosis.diagnosis_id,
            "finding_count": len(diagnosis.findings),
            "query_id": diagnosis.query_id,
        },
    )
    return diagnosis


def select_exact_boost_candidates(
    diagnoses: Sequence[BadCaseDiagnosis],
    *,
    max_candidates: int = 4,
) -> CandidateSelection:
    """Choose a bounded, deterministic subset of the exact-boost allowlist."""

    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int):
        raise TypeError("max_candidates must be an integer")
    if not 1 <= max_candidates <= 4:
        raise ValueError("max_candidates must be between 1 and 4")
    diagnosis_ids = [diagnosis.diagnosis_id for diagnosis in diagnoses]
    if len(diagnosis_ids) != len(set(diagnosis_ids)):
        raise ValueError("diagnoses must not contain duplicate IDs")

    support: dict[RootCause, list[BadCaseDiagnosis]] = {
        cause: [] for cause in ROOT_CAUSE_ORDER
    }
    for diagnosis in diagnoses:
        for finding in diagnosis.findings:
            support[finding.cause].append(diagnosis)
    cause_counts = Counter(
        {
            cause: len({item.query_id for item in items})
            for cause, items in support.items()
            if items
        }
    )
    ordered_causes = sorted(
        (cause for cause in cause_counts if cause in _CANDIDATE_LIBRARY),
        key=lambda cause: (-cause_counts[cause], ROOT_CAUSE_ORDER.index(cause)),
    )
    selected_causes = ordered_causes[: max(0, max_candidates - 1)]
    candidates: list[ExactBoostCandidate] = []
    addressable_diagnoses = [
        diagnosis
        for diagnosis in diagnoses
        if any(finding.cause in _CANDIDATE_LIBRARY for finding in diagnosis.findings)
    ]
    if addressable_diagnoses:
        primary_cause = ordered_causes[0]
        query_ids = sorted({item.query_id for item in addressable_diagnoses})
        candidates.append(
            ExactBoostCandidate(
                candidate_id=_CONSERVATIVE_CANDIDATE_ID,
                trigger_cause=primary_cause,
                parameters=_CONSERVATIVE_PARAMETERS,
                supporting_diagnosis_ids=sorted(
                    {item.diagnosis_id for item in addressable_diagnoses}
                ),
                supporting_query_ids=query_ids,
                support_count=len(query_ids),
            )
        )
    for cause in selected_causes:
        candidate_id, parameters = _CANDIDATE_LIBRARY[cause]
        supporting = support[cause]
        query_ids = sorted({item.query_id for item in supporting})
        candidates.append(
            ExactBoostCandidate(
                candidate_id=candidate_id,
                trigger_cause=cause,
                parameters=parameters,
                supporting_diagnosis_ids=sorted(
                    {item.diagnosis_id for item in supporting}
                ),
                supporting_query_ids=query_ids,
                support_count=len(query_ids),
            )
        )

    selection_payload = {
        "diagnosis_ids": sorted(diagnosis_ids),
        "candidate_ids": [candidate.candidate_id for candidate in candidates],
        "max_candidates": max_candidates,
    }
    selection = CandidateSelection(
        selection_id=_content_id("candidate-selection", selection_payload),
        diagnosis_count=len(diagnoses),
        candidates=candidates,
    )
    logger.info(
        "strategy_candidates_selected",
        extra={
            "candidate_count": len(selection.candidates),
            "diagnosis_count": selection.diagnosis_count,
            "selection_id": selection.selection_id,
        },
    )
    return selection


def score_strategy_comparison(
    candidate: ExactBoostCandidate,
    comparison: CompareRunsPayload,
    *,
    gate_policy: GatePolicy | None = None,
) -> StrategyEvaluation:
    """Compute a relative selection score and non-negotiable hard gates."""

    policy = gate_policy or TRUSTED_SMOKE_GATE_POLICY
    metric_values = {
        "ndcg@10_delta": comparison.aggregate_metrics.ndcg_at_10.delta,
        "ndcg@5_delta": comparison.aggregate_metrics.ndcg_at_5.delta,
        "mrr@10_delta": comparison.aggregate_metrics.mrr_at_10.delta,
        "success@1_delta": comparison.aggregate_metrics.success_at_1.delta,
        "success@5_delta": comparison.aggregate_metrics.success_at_5.delta,
    }
    regression_rate = (
        comparison.outcome_counts.ndcg_at_10.regressed / comparison.query_count
    )
    worst_regression_magnitude = max(
        (
            -item.ndcg_at_10_delta
            for item in comparison.regressions
            if item.ndcg_at_10_delta < 0.0
        ),
        default=0.0,
    )
    metric_values.update(
        {
            "ndcg@10_regression_rate": _rounded_unit(regression_rate),
            "worst_ndcg@10_regression_magnitude": _rounded_unit(
                worst_regression_magnitude
            ),
        }
    )
    components = [
        SelectionScoreComponent(
            name=name,
            value=float(metric_values[name]),
            weight=weight,
            contribution=round(float(metric_values[name]) * weight, 12),
        )
        for name, weight in _SELECTION_SCORE_WEIGHTS
    ]
    selection_score = SelectionScoreBreakdown(
        components=components,
        total=round(sum(item.contribution for item in components), 12),
    )
    checks = [
        _gate_check(
            "ndcg@10_minimum",
            ">",
            comparison.aggregate_metrics.ndcg_at_10.delta,
            policy.min_ndcg_at_10_delta,
        ),
        _gate_check(
            "ndcg@5_floor",
            ">=",
            comparison.aggregate_metrics.ndcg_at_5.delta,
            policy.min_ndcg_at_5_delta,
        ),
        _gate_check(
            "mrr@10_floor",
            ">=",
            comparison.aggregate_metrics.mrr_at_10.delta,
            policy.min_mrr_at_10_delta,
        ),
        _gate_check(
            "success@1_floor",
            ">=",
            comparison.aggregate_metrics.success_at_1.delta,
            policy.min_success_at_1_delta,
        ),
        _gate_check(
            "success@5_floor",
            ">=",
            comparison.aggregate_metrics.success_at_5.delta,
            policy.min_success_at_5_delta,
        ),
        _gate_check(
            "ndcg@10_regression_rate_ceiling",
            "<=",
            regression_rate,
            policy.max_ndcg_at_10_regression_rate,
        ),
        _gate_check(
            "worst_ndcg@10_regression_ceiling",
            "<=",
            worst_regression_magnitude,
            policy.max_worst_ndcg_at_10_regression_magnitude,
        ),
    ]
    gates = GateResult(
        policy=policy,
        checks=checks,
        passed=all(check.passed for check in checks),
    )
    evaluation_body = {
        "candidate": candidate.model_dump(mode="json"),
        "comparison_id": comparison.comparison_id,
        "selection_score": selection_score.model_dump(mode="json"),
        "gates": gates.model_dump(mode="json"),
    }
    evaluation = StrategyEvaluation(
        evaluation_id=_content_id("strategy-evaluation", evaluation_body),
        candidate=candidate,
        comparison_id=comparison.comparison_id,
        baseline_run_id=comparison.baseline_run_id,
        candidate_run_id=comparison.candidate_run_id,
        query_count=comparison.query_count,
        metrics=CoreMetricDeltas.model_validate(
            {
                "success@5": comparison.aggregate_metrics.success_at_5,
                "mrr@10": comparison.aggregate_metrics.mrr_at_10,
                "ndcg@10": comparison.aggregate_metrics.ndcg_at_10,
            }
        ),
        ndcg_at_10_delta=comparison.aggregate_metrics.ndcg_at_10.delta,
        mrr_at_10_delta=comparison.aggregate_metrics.mrr_at_10.delta,
        success_at_5_delta=comparison.aggregate_metrics.success_at_5.delta,
        selection_score=selection_score,
        gates=gates,
        eligible=gates.passed,
    )
    logger.info(
        "strategy_comparison_scored",
        extra={
            "candidate_id": candidate.candidate_id,
            "comparison_id": comparison.comparison_id,
            "gate_failure_count": sum(not check.passed for check in checks),
            "gate_passed": gates.passed,
        },
    )
    return evaluation


def select_winner(evaluations: Sequence[StrategyEvaluation]) -> WinnerSelection:
    """Rank only gate-passing candidates by a relative, deterministic score."""

    candidate_ids = [evaluation.candidate.candidate_id for evaluation in evaluations]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("evaluations must have unique candidate IDs")
    baseline_ids = {evaluation.baseline_run_id for evaluation in evaluations}
    if len(baseline_ids) > 1:
        raise ValueError("winner candidates must share one baseline Run")
    eligible = sorted(
        (evaluation for evaluation in evaluations if evaluation.eligible),
        key=lambda item: (
            -item.selection_score.total,
            -item.ndcg_at_10_delta,
            item.candidate.parameters.complexity,
            item.candidate.candidate_id,
        ),
    )
    selection_body = {
        "evaluation_ids": sorted(
            evaluation.evaluation_id for evaluation in evaluations
        ),
        "ranked_candidate_ids": [item.candidate.candidate_id for item in eligible],
    }
    winner = eligible[0] if eligible else None
    selection = WinnerSelection(
        selection_id=_content_id("winner-selection", selection_body),
        status="winner_selected" if winner is not None else "no_passing_candidate",
        baseline_run_id=next(iter(baseline_ids), None),
        evaluated_candidate_count=len(evaluations),
        eligible_candidate_count=len(eligible),
        ranked_candidate_ids=[item.candidate.candidate_id for item in eligible],
        winner_candidate_id=(winner.candidate.candidate_id if winner else None),
        winner_evaluation_id=(winner.evaluation_id if winner else None),
    )
    logger.info(
        "strategy_winner_selected",
        extra={
            "candidate_count": len(evaluations),
            "eligible_candidate_count": len(eligible),
            "selection_id": selection.selection_id,
            "winner_candidate_id": selection.winner_candidate_id,
        },
    )
    return selection


def _gate_check(
    name: GateName,
    comparator: Literal[">", ">=", "<="],
    observed: float,
    threshold: float,
) -> GateCheck:
    observed_value = round(float(observed), 12)
    threshold_value = round(float(threshold), 12)
    passed = (
        observed_value > threshold_value
        if comparator == ">"
        else observed_value >= threshold_value
        if comparator == ">="
        else observed_value <= threshold_value
    )
    return GateCheck(
        name=name,
        comparator=comparator,
        observed=observed_value,
        threshold=threshold_value,
        passed=passed,
    )


def _rounded_unit(value: float) -> float:
    return round(min(1.0, max(0.0, value)), 12)


def _content_id(prefix: str, payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(canonical).hexdigest()[:12]}"
