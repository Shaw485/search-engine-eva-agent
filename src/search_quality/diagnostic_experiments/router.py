"""Deterministic evidence routing into bounded, falsifiable experiment plans."""

from __future__ import annotations

import logging

from .contracts import (
    BehavioralLanePlan,
    DiagnosticExperimentPlan,
    QualityEvidenceStatus,
    QualityLanePlan,
    ResolvedDiagnosticEvidence,
    StrategySpec,
    experiment_plan_id,
    strategy_spec_id,
)

logger = logging.getLogger("search_quality.diagnostic_experiments")


class EvidenceRouter:
    """Choose only from implemented strategy families using validated facts."""

    router_id = "diagnostic-evidence-router-v1"

    def route(
        self,
        evidence: ResolvedDiagnosticEvidence,
        *,
        quality_evidence_status: QualityEvidenceStatus = (
            QualityEvidenceStatus.BEHAVIOR_ONLY
        ),
        oracle_id: str | None = None,
    ) -> DiagnosticExperimentPlan:
        validated = ResolvedDiagnosticEvidence.model_validate(
            evidence.model_dump(mode="python"),
            strict=True,
        )
        quality_lane = _quality_lane(quality_evidence_status, oracle_id=oracle_id)
        behavioral_lane = BehavioralLanePlan()

        if validated.identity_zero_result_case_ids:
            strategy = zero_result_backoff_strategy()
            plan_body = {
                "activation_eligible": False,
                "behavioral_lane": behavioral_lane.model_dump(mode="python"),
                "diagnostic_id": validated.diagnostic_id,
                "falsifiers": (
                    "no_zero_result_recovery",
                    "no_independently_judged_relevant_gain",
                    "quality_or_safety_gate_regression",
                    "nonzero_baseline_results_changed",
                    "execution_budget_exceeded",
                ),
                "hypothesis": (
                    "The strict all-token baseline may be over-constrained for the "
                    "observed identity zero-result cases. A zero-result-only, "
                    "drop-one-non-protected-token recall backoff can falsify this "
                    "without changing Queries that already return results."
                ),
                "index_id": validated.index_id,
                "quality_conclusion_allowed": False,
                "quality_lane": quality_lane.model_dump(mode="python"),
                "query_set_id": validated.query_set_id,
                "reason_code": "identity_zero_result_backoff_prioritized",
                "recommended_next_action": "run_bounded_two_lane_experiment",
                "router_id": self.router_id,
                "schema_version": "diagnostic-experiment-plan-v1",
                "status": "experiment_planned",
                "strategy": strategy.model_dump(mode="python"),
                "strategy_write_count": 0,
                "target_case_ids": validated.identity_zero_result_case_ids,
            }
        elif validated.spelling_sensitive_case_ids:
            independent = (
                quality_evidence_status == QualityEvidenceStatus.INDEPENDENT_ORACLE
            )
            plan_body = _blocked_plan_body(
                evidence=validated,
                behavioral_lane=behavioral_lane,
                quality_lane=quality_lane,
                status="requires_engineering" if independent else "requires_oracle",
                reason_code=(
                    "spelling_correction_requires_engineering"
                    if independent
                    else "spelling_sensitive_requires_independent_oracle"
                ),
                recommended_next_action=(
                    "implement_allowlisted_spelling_recall"
                    if independent
                    else "create_independent_relevance_oracle"
                ),
                hypothesis=(
                    "The result change after a spelling perturbation is behavioral "
                    "evidence only. Synthetic Queries must receive an independent "
                    "relevance Oracle before a spelling strategy can support a "
                    "quality conclusion."
                ),
                target_case_ids=validated.spelling_sensitive_case_ids,
            )
        elif (
            validated.order_sensitive_case_ids or validated.ranking_instability_case_ids
        ):
            targets = tuple(
                sorted(
                    set(validated.order_sensitive_case_ids)
                    | set(validated.ranking_instability_case_ids)
                )
            )
            plan_body = _blocked_plan_body(
                evidence=validated,
                behavioral_lane=behavioral_lane,
                quality_lane=quality_lane,
                status="requires_oracle",
                reason_code="unjudged_ranking_change_requires_oracle",
                recommended_next_action="create_independent_relevance_oracle",
                hypothesis=(
                    "An ordered result change is not a relevance regression or "
                    "improvement until an independent Oracle judges the changed pool."
                ),
                target_case_ids=targets,
            )
        else:
            plan_body = _blocked_plan_body(
                evidence=validated,
                behavioral_lane=behavioral_lane,
                quality_lane=quality_lane,
                status="no_supported_experiment",
                reason_code="no_allowlisted_strategy_matches_evidence",
                recommended_next_action="stop_without_strategy_change",
                hypothesis=(
                    "The validated diagnostic does not contain a behavior pattern "
                    "addressed by the current allowlisted strategy family."
                ),
                target_case_ids=(),
            )

        plan = DiagnosticExperimentPlan.model_validate(
            {
                **plan_body,
                "experiment_plan_id": experiment_plan_id(plan_body),
            },
            strict=True,
        )
        logger.info(
            "diagnostic_experiment_plan_created",
            extra={
                "diagnostic_id": plan.diagnostic_id,
                "experiment_plan_id": plan.experiment_plan_id,
                "quality_evidence_status": plan.quality_lane.evidence_status.value,
                "reason_code": plan.reason_code,
                "status": plan.status,
                "strategy_spec_id": (
                    plan.strategy.strategy_spec_id if plan.strategy else None
                ),
                "target_case_count": len(plan.target_case_ids),
            },
        )
        return plan


def route_diagnostic_evidence(
    evidence: ResolvedDiagnosticEvidence,
    *,
    quality_evidence_status: QualityEvidenceStatus = (
        QualityEvidenceStatus.BEHAVIOR_ONLY
    ),
    oracle_id: str | None = None,
) -> DiagnosticExperimentPlan:
    return EvidenceRouter().route(
        evidence,
        quality_evidence_status=quality_evidence_status,
        oracle_id=oracle_id,
    )


def zero_result_backoff_strategy() -> StrategySpec:
    body = {
        "fallback_operator": "drop_one_non_protected_token",
        "fallback_trigger": "primary_zero_result",
        "family": "zero_result_backoff",
        "fusion": "rrf",
        "max_fallback_routes": 16,
        "primary_operator": "strict_and",
        "protected_token_policy": ("numeric_model_and_explicit_product_id_required"),
        "schema_version": "diagnostic-strategy-spec-v1",
        "strategy_id": "zero-result-drop-one-token-backoff-v1",
        "top_k": 10,
    }
    return StrategySpec.model_validate(
        {**body, "strategy_spec_id": strategy_spec_id(body)},
        strict=True,
    )


def _quality_lane(
    status: QualityEvidenceStatus,
    *,
    oracle_id: str | None,
) -> QualityLanePlan:
    if not isinstance(status, QualityEvidenceStatus):
        raise TypeError("quality_evidence_status must be a QualityEvidenceStatus")
    if status == QualityEvidenceStatus.BEHAVIOR_ONLY:
        if oracle_id is not None:
            raise ValueError("behavior-only planning must not declare an Oracle")
        return QualityLanePlan(
            evidence_status=status,
            query_scope="not_scheduled",
            label_source_ref=None,
            labels_may_be_used_by_harness=False,
            reason_code="no_eligible_quality_labels_resolved",
        )
    if status == QualityEvidenceStatus.DEVELOPMENT_SMOKE:
        if oracle_id is not None:
            raise ValueError("development smoke must not declare an Oracle")
        return QualityLanePlan(
            evidence_status=status,
            query_scope="development_identity_queries_only",
            label_source_ref="esci-stage1-smoke-v1",
            labels_may_be_used_by_harness=True,
            reason_code="development_smoke_is_not_independent",
        )
    if oracle_id is None:
        raise ValueError("independent Oracle planning requires an oracle_id")
    return QualityLanePlan(
        evidence_status=status,
        query_scope="independent_oracle_queries",
        label_source_ref=oracle_id,
        labels_may_be_used_by_harness=True,
        reason_code="experiment_not_yet_run_against_independent_oracle",
    )


def _blocked_plan_body(
    *,
    evidence: ResolvedDiagnosticEvidence,
    behavioral_lane: BehavioralLanePlan,
    quality_lane: QualityLanePlan,
    status: str,
    reason_code: str,
    recommended_next_action: str,
    hypothesis: str,
    target_case_ids: tuple[str, ...],
) -> dict[str, object]:
    return {
        "activation_eligible": False,
        "behavioral_lane": behavioral_lane.model_dump(mode="python"),
        "diagnostic_id": evidence.diagnostic_id,
        "falsifiers": (),
        "hypothesis": hypothesis,
        "index_id": evidence.index_id,
        "quality_conclusion_allowed": False,
        "quality_lane": quality_lane.model_dump(mode="python"),
        "query_set_id": evidence.query_set_id,
        "reason_code": reason_code,
        "recommended_next_action": recommended_next_action,
        "router_id": EvidenceRouter.router_id,
        "schema_version": "diagnostic-experiment-plan-v1",
        "status": status,
        "strategy": None,
        "strategy_write_count": 0,
        "target_case_ids": target_case_ids,
    }
