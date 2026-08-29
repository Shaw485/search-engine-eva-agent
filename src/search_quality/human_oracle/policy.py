"""Deterministic source-cluster census for the fixed Human Oracle batch."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from search_quality.bad_cases.contracts import (
    BadCaseCategory,
    BadCaseDiagnostic,
    BadCaseDiagnosticArtifact,
    BadCaseObservation,
    DiagnosticReason,
)
from search_quality.bad_cases.runner import validate_bad_case_diagnostic
from search_quality.data.contracts import canonical_json_sha256
from search_quality.query_constructor.contracts import (
    QueryCase,
    QueryConstruction,
    QuerySetArtifact,
)

from .contracts import (
    CONSTRUCTION_ORDER,
    EXPECTED_CANDIDATE_COUNT,
    EXPECTED_CLUSTER_COUNT,
    EXPECTED_SOURCE_NONZERO_VARIANT_ZERO_CLUSTERS,
    EXPECTED_SOURCE_ZERO_CLUSTERS,
    EXPECTED_SYNTHETIC_CANDIDATE_COUNT,
    OracleBatchArtifact,
    OracleCandidate,
    OracleReviewUnit,
    OracleStratum,
    oracle_batch_id,
    oracle_unit_id,
)


def build_oracle_batch(
    *,
    diagnostic: BadCaseDiagnosticArtifact,
    query_set: QuerySetArtifact,
) -> OracleBatchArtifact:
    """Build the complete 20-cluster census without storing raw content."""

    diagnostic = validate_bad_case_diagnostic(
        artifact=diagnostic,
        query_set=query_set,
    )
    if diagnostic.diagnostic_candidate_count != EXPECTED_CANDIDATE_COUNT:
        raise ValueError("Human Oracle requires exactly 40 diagnostic candidates")

    observations = {item.case_id: item for item in diagnostic.observations}
    query_cases = {item.case_id: item for item in query_set.cases}
    grouped: dict[str, list[BadCaseDiagnostic]] = defaultdict(list)
    for candidate in diagnostic.diagnostics:
        grouped[candidate.source_case_id].append(candidate)
    if len(grouped) != EXPECTED_CLUSTER_COUNT:
        raise ValueError("Human Oracle requires exactly 20 source clusters")

    units: list[OracleReviewUnit] = []
    for source_case_id, candidates in grouped.items():
        source_observation = observations.get(source_case_id)
        source_query_case = query_cases.get(source_case_id)
        if source_observation is None or source_query_case is None:
            raise ValueError("Oracle source cluster lacks trusted evidence")
        candidates.sort(
            key=lambda item: (
                CONSTRUCTION_ORDER[item.construction],
                item.case_id,
            )
        )
        stratum = _validate_cluster_shape(
            candidates,
            source=source_observation,
            observations=observations,
        )
        oracle_candidates = [
            _build_candidate(
                diagnostic_id=diagnostic.diagnostic_id,
                candidate=item,
                source_observation=source_observation,
                variant_observation=observations[item.case_id],
                source_query_case=source_query_case,
                variant_query_case=query_cases[item.case_id],
            )
            for item in candidates
        ]
        unit_body: dict[str, Any] = {
            "candidates": [item.model_dump(mode="json") for item in oracle_candidates],
            "source_case_id": source_case_id,
            "source_query_id": source_observation.source_query_id,
            "stratum": stratum.value,
        }
        units.append(
            OracleReviewUnit.model_validate(
                {**unit_body, "unit_id": oracle_unit_id(unit_body)}
            )
        )

    units.sort(key=lambda item: (item.source_query_id, item.source_case_id))
    strata = Counter(item.stratum.value for item in units)
    candidate_count = sum(len(item.candidates) for item in units)
    synthetic_count = sum(
        candidate.construction != QueryConstruction.IDENTITY
        for unit in units
        for candidate in unit.candidates
    )
    if (
        candidate_count != EXPECTED_CANDIDATE_COUNT
        or synthetic_count != EXPECTED_SYNTHETIC_CANDIDATE_COUNT
        or strata
        != Counter(
            {
                OracleStratum.SOURCE_ZERO_CLUSTER.value: (
                    EXPECTED_SOURCE_ZERO_CLUSTERS
                ),
                OracleStratum.SOURCE_NONZERO_VARIANT_ZERO.value: (
                    EXPECTED_SOURCE_NONZERO_VARIANT_ZERO_CLUSTERS
                ),
            }
        )
    ):
        raise ValueError("Human Oracle fixed census composition is invalid")

    body: dict[str, Any] = {
        "cluster_cap": 24,
        "formal_evaluation_allowed": False,
        "index_id": diagnostic.index_id,
        "mechanism_smoke_only": True,
        "population_candidate_count": EXPECTED_CANDIDATE_COUNT,
        "population_cluster_count": EXPECTED_CLUSTER_COUNT,
        "product_relevance_labels_created": 0,
        "quality_conclusion_allowed": False,
        "query_set_id": diagnostic.query_set_id,
        "raw_product_content_stored": False,
        "raw_query_text_stored": False,
        "root_cause_claimed": False,
        "sampling_fraction": 1.0,
        "sampling_policy_id": "source-cluster-census-up-to-24-v1",
        "schema_version": "human-oracle-batch-v1",
        "search_strategy_id": diagnostic.search_strategy_id,
        "selected_candidate_count": EXPECTED_CANDIDATE_COUNT,
        "selected_cluster_count": EXPECTED_CLUSTER_COUNT,
        "selection_mode": "cluster_census",
        "source_labels_inherited": False,
        "strategy_write_count": 0,
        "stratum_counts": {
            "source_zero_cluster": strata["source_zero_cluster"],
            "source_nonzero_variant_zero": strata["source_nonzero_variant_zero"],
        },
        "synthetic_intent_candidate_count": EXPECTED_SYNTHETIC_CANDIDATE_COUNT,
        "top_k": diagnostic.top_k,
        "diagnostic_id": diagnostic.diagnostic_id,
        "units": [item.model_dump(mode="json") for item in units],
    }
    return OracleBatchArtifact.model_validate(
        {**body, "oracle_batch_id": oracle_batch_id(body)}
    )


def validate_oracle_batch(
    *,
    batch: OracleBatchArtifact,
    diagnostic: BadCaseDiagnosticArtifact,
    query_set: QuerySetArtifact,
) -> OracleBatchArtifact:
    """Rebuild the deterministic census and reject any changed selection."""

    validated = OracleBatchArtifact.model_validate(batch.model_dump(mode="json"))
    expected = build_oracle_batch(diagnostic=diagnostic, query_set=query_set)
    if validated != expected:
        raise ValueError("Oracle batch does not match trusted diagnostic evidence")
    return validated


def _validate_cluster_shape(
    candidates: list[BadCaseDiagnostic],
    *,
    source: BadCaseObservation,
    observations: dict[str, BadCaseObservation],
) -> OracleStratum:
    constructions = [item.construction for item in candidates]
    if source.returned_at_k == 0:
        if constructions != [
            QueryConstruction.IDENTITY,
            QueryConstruction.ADJACENT_TRANSPOSITION,
            QueryConstruction.TOKEN_ORDER_REVERSAL,
        ]:
            raise ValueError("source-zero cluster must contain all three cases")
        for item in candidates:
            if observations[item.case_id].returned_at_k != 0 or item.categories != [
                BadCaseCategory.ZERO_RESULT
            ]:
                raise ValueError("source-zero cluster has an unexpected signal")
        return OracleStratum.SOURCE_ZERO_CLUSTER

    if len(candidates) != 1:
        raise ValueError("source-nonzero cluster must contain one candidate")
    candidate = candidates[0]
    if (
        candidate.construction != QueryConstruction.ADJACENT_TRANSPOSITION
        or observations[candidate.case_id].returned_at_k != 0
        or candidate.categories
        != [BadCaseCategory.ZERO_RESULT, BadCaseCategory.SPELLING_SENSITIVE]
        or candidate.reason_code != DiagnosticReason.VARIANT_ZERO_RESULT
    ):
        raise ValueError("source-nonzero cluster is not the fixed typo-zero stratum")
    return OracleStratum.SOURCE_NONZERO_VARIANT_ZERO


def _build_candidate(
    *,
    diagnostic_id: str,
    candidate: BadCaseDiagnostic,
    source_observation: BadCaseObservation,
    variant_observation: BadCaseObservation,
    source_query_case: QueryCase,
    variant_query_case: QueryCase,
) -> OracleCandidate:
    source_observation_sha256 = canonical_json_sha256(
        source_observation.model_dump(mode="json")
    )
    variant_observation_sha256 = canonical_json_sha256(
        variant_observation.model_dump(mode="json")
    )
    core = {
        "diagnostic": candidate.model_dump(mode="json"),
        "diagnostic_id": diagnostic_id,
        "source_observation_sha256": source_observation_sha256,
        "source_query_sha256": source_query_case.normalized_query_sha256,
        "variant_observation_sha256": variant_observation_sha256,
        "variant_query_sha256": variant_query_case.normalized_query_sha256,
    }
    case_context_sha256 = canonical_json_sha256(core)
    intent_context_sha256 = None
    if candidate.construction != QueryConstruction.IDENTITY:
        intent_context_sha256 = canonical_json_sha256(
            {
                "case_context_sha256": case_context_sha256,
                "presentation": "query_text_only",
                "source_query_sha256": source_query_case.normalized_query_sha256,
                "variant_query_sha256": variant_query_case.normalized_query_sha256,
            }
        )
    behavior_context_sha256 = canonical_json_sha256(
        {
            "case_context_sha256": case_context_sha256,
            "presentation": "verified_top3_from_diagnostic_observations",
            "source_observation_sha256": source_observation_sha256,
            "variant_observation_sha256": variant_observation_sha256,
        }
    )
    return OracleCandidate(
        case_id=candidate.case_id,
        source_case_id=candidate.source_case_id,
        construction=candidate.construction,
        categories=candidate.categories,
        reason_code=candidate.reason_code,
        source_returned_at_k=candidate.source_returned_at_k,
        variant_returned_at_k=candidate.variant_returned_at_k,
        overlap_at_k=candidate.overlap_at_k,
        source_observation_sha256=source_observation_sha256,
        variant_observation_sha256=variant_observation_sha256,
        source_query_sha256=source_query_case.normalized_query_sha256,
        variant_query_sha256=variant_query_case.normalized_query_sha256,
        case_context_sha256=case_context_sha256,
        intent_context_sha256=intent_context_sha256,
        behavior_context_sha256=behavior_context_sha256,
    )
