"""Diagnostic evidence routing and query-plan construction."""

from .contracts import (
    DiagnosticExperimentPlan,
    QualityEvidenceStatus,
    QueryRoute,
    QueryRoutePlan,
    ResolvedDiagnosticEvidence,
    StrategySpec,
)
from .loader import load_diagnostic_artifacts, load_resolved_diagnostic_evidence
from .query_routes import generate_query_routes
from .resolver import resolve_diagnostic_evidence
from .router import (
    EvidenceRouter,
    route_diagnostic_evidence,
    zero_result_backoff_strategy,
)

__all__ = [
    "DiagnosticExperimentPlan",
    "EvidenceRouter",
    "QualityEvidenceStatus",
    "QueryRoute",
    "QueryRoutePlan",
    "ResolvedDiagnosticEvidence",
    "StrategySpec",
    "generate_query_routes",
    "load_diagnostic_artifacts",
    "load_resolved_diagnostic_evidence",
    "resolve_diagnostic_evidence",
    "route_diagnostic_evidence",
    "zero_result_backoff_strategy",
]
