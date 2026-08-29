"""Resolve trusted Bad Case artifacts into privacy-safe planning evidence."""

from __future__ import annotations

import logging

from search_quality.bad_cases.contracts import (
    BadCaseCategory,
    BadCaseDiagnosticArtifact,
)
from search_quality.bad_cases.runner import validate_bad_case_diagnostic
from search_quality.query_constructor.contracts import (
    QueryConstruction,
    QuerySetArtifact,
)

from .contracts import ResolvedDiagnosticEvidence

logger = logging.getLogger("search_quality.diagnostic_experiments")


def resolve_diagnostic_evidence(
    *,
    artifact: BadCaseDiagnosticArtifact,
    query_set: QuerySetArtifact,
) -> ResolvedDiagnosticEvidence:
    """Fail closed, then expose only IDs and deterministic behavior facts."""

    try:
        validated = validate_bad_case_diagnostic(
            artifact=artifact,
            query_set=query_set,
        )
        identity_zero = tuple(
            sorted(
                item.case_id
                for item in validated.diagnostics
                if item.construction == QueryConstruction.IDENTITY
                and BadCaseCategory.ZERO_RESULT in item.categories
            )
        )
        spelling = tuple(
            sorted(
                item.case_id
                for item in validated.diagnostics
                if BadCaseCategory.SPELLING_SENSITIVE in item.categories
            )
        )
        order = tuple(
            sorted(
                item.case_id
                for item in validated.diagnostics
                if BadCaseCategory.ORDER_SENSITIVE in item.categories
            )
        )
        instability = tuple(
            sorted(
                item.case_id
                for item in validated.diagnostics
                if BadCaseCategory.RANKING_INSTABILITY_NEEDS_JUDGMENT in item.categories
            )
        )
        evidence = ResolvedDiagnosticEvidence(
            diagnostic_id=validated.diagnostic_id,
            query_set_id=validated.query_set_id,
            index_id=validated.index_id,
            search_strategy_id=validated.search_strategy_id,
            query_count=validated.query_count,
            original_count=validated.original_count,
            synthetic_count=validated.synthetic_count,
            diagnostic_candidate_count=validated.diagnostic_candidate_count,
            identity_zero_result_case_ids=identity_zero,
            spelling_sensitive_case_ids=spelling,
            order_sensitive_case_ids=order,
            ranking_instability_case_ids=instability,
            formal_evaluation_allowed=validated.formal_evaluation_allowed,
            relevance_labels_used=validated.relevance_labels_used,
            quality_metrics_computed=validated.quality_metrics_computed,
            stage_drop_diagnostics_computed=(validated.stage_drop_diagnostics_computed),
            protected_profile_dispatch_count=(
                validated.protected_profile_dispatch_count
            ),
            strategy_write_count=validated.strategy_write_count,
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "diagnostic_evidence_resolution_failed",
            extra={"error_type": type(exc).__name__},
        )
        raise
    logger.info(
        "diagnostic_evidence_resolved",
        extra={
            "diagnostic_candidate_count": evidence.diagnostic_candidate_count,
            "diagnostic_id": evidence.diagnostic_id,
            "identity_zero_result_count": evidence.identity_zero_result_count,
            "query_set_id": evidence.query_set_id,
            "spelling_sensitive_count": evidence.spelling_sensitive_count,
        },
    )
    return evidence
