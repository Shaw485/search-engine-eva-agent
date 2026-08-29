"""Transient owner-only views; raw Query and product content is never persisted."""

from __future__ import annotations

import logging
import re
import unicodedata

from search_quality.bad_cases.contracts import (
    BadCaseDiagnosticArtifact,
    BadCaseDisplayHit,
    BadCaseSample,
    display_hit_sha256,
    ordered_results_sha256,
    product_key_sha256,
    result_set_sha256,
)
from search_quality.catalog import CatalogSearchService
from search_quality.data.contracts import canonical_json_sha256
from search_quality.query_constructor.builder import validate_query_set
from search_quality.query_constructor.contracts import (
    QueryConstruction,
    QuerySetArtifact,
)

from .contracts import (
    IntentAnnotation,
    OracleBatchArtifact,
    OracleBehaviorView,
    OracleBehaviorViewCandidate,
    OracleIntentView,
    OracleIntentViewCandidate,
    OracleReviewUnit,
)
from .policy import validate_oracle_batch

logger = logging.getLogger("search_quality.human_oracle")


def build_intent_view(
    *,
    batch: OracleBatchArtifact,
    query_set: QuerySetArtifact,
    unit_id: str,
) -> OracleIntentView:
    try:
        return _build_intent_view(batch=batch, query_set=query_set, unit_id=unit_id)
    except Exception as exc:
        _log_view_failure(
            operation="build_intent_view",
            error=exc,
            batch=batch,
            unit_id=unit_id,
        )
        raise


def _build_intent_view(
    *,
    batch: OracleBatchArtifact,
    query_set: QuerySetArtifact,
    unit_id: str,
) -> OracleIntentView:
    """Return Query text only, before result evidence is presented."""

    query_set = validate_query_set(query_set)
    if query_set.query_set_id != batch.query_set_id:
        raise ValueError("intent view Query set does not match its Oracle batch")
    unit = _unit(batch, unit_id)
    cases = {item.case_id: item for item in query_set.cases}
    source = cases.get(unit.source_case_id)
    if source is None:
        raise ValueError("intent view source Query is unavailable")
    candidates = []
    for candidate in unit.candidates:
        case = cases.get(candidate.case_id)
        if (
            case is None
            or case.normalized_query_sha256 != candidate.variant_query_sha256
            or source.normalized_query_sha256 != candidate.source_query_sha256
        ):
            raise ValueError("intent view Query content contradicts its batch")
        candidates.append(
            OracleIntentViewCandidate(
                case_id=candidate.case_id,
                construction=candidate.construction,
                query_text=case.query_text,
                requires_intent_annotation=(
                    candidate.construction != QueryConstruction.IDENTITY
                ),
                intent_context_sha256=candidate.intent_context_sha256,
            )
        )
    logger.debug(
        "human_oracle_intent_view_built",
        extra={
            "candidate_count": len(candidates),
            "oracle_batch_id": batch.oracle_batch_id,
            "unit_id": unit.unit_id,
        },
    )
    return OracleIntentView(
        oracle_batch_id=batch.oracle_batch_id,
        unit_id=unit.unit_id,
        source_case_id=unit.source_case_id,
        source_query_text=source.query_text,
        candidates=candidates,
    )


def build_behavior_view(
    *,
    batch: OracleBatchArtifact,
    diagnostic: BadCaseDiagnosticArtifact,
    query_set: QuerySetArtifact,
    unit_id: str,
    samples: list[BadCaseSample],
    active_intents: dict[str, IntentAnnotation],
) -> OracleBehaviorView:
    try:
        return _build_behavior_view(
            batch=batch,
            diagnostic=diagnostic,
            query_set=query_set,
            unit_id=unit_id,
            samples=samples,
            active_intents=active_intents,
        )
    except Exception as exc:
        _log_view_failure(
            operation="build_behavior_view",
            error=exc,
            batch=batch,
            unit_id=unit_id,
        )
        raise


def _build_behavior_view(
    *,
    batch: OracleBatchArtifact,
    diagnostic: BadCaseDiagnosticArtifact,
    query_set: QuerySetArtifact,
    unit_id: str,
    samples: list[BadCaseSample],
    active_intents: dict[str, IntentAnnotation],
) -> OracleBehaviorView:
    """Verify transient Top-3 evidence before returning one cluster to the owner."""

    batch = validate_oracle_batch(
        batch=batch,
        diagnostic=diagnostic,
        query_set=query_set,
    )
    unit = _unit(batch, unit_id)
    expected_case_ids = {item.case_id for item in unit.candidates}
    sample_by_case = {item.case_id: item for item in samples}
    if len(sample_by_case) != len(samples) or set(sample_by_case) != expected_case_ids:
        raise ValueError("behavior view samples must cover exactly one Oracle unit")
    query_cases = {item.case_id: item for item in query_set.cases}
    observations = {item.case_id: item for item in diagnostic.observations}
    diagnostics = {item.case_id: item for item in diagnostic.diagnostics}
    output = []
    for candidate in unit.candidates:
        sample = sample_by_case[candidate.case_id]
        case = query_cases[candidate.case_id]
        source_case = query_cases[candidate.source_case_id]
        observation = observations[candidate.case_id]
        source_observation = observations[candidate.source_case_id]
        diagnosed = diagnostics[candidate.case_id]
        if (
            sample.query_text != case.query_text
            or sample.source_query_text != source_case.query_text
            or sample.case_id != candidate.case_id
            or sample.source_case_id != candidate.source_case_id
            or sample.construction != candidate.construction
            or sample.categories != diagnosed.categories
            or sample.reason_code != diagnosed.reason_code
            or sample.source_returned_at_k != diagnosed.source_returned_at_k
            or sample.variant_returned_at_k != diagnosed.variant_returned_at_k
            or sample.overlap_at_k != diagnosed.overlap_at_k
        ):
            raise ValueError("behavior view sample contradicts diagnostic evidence")
        _validate_hits(sample.source_top_hits, source_observation)
        _validate_hits(sample.variant_top_hits, observation)
        intent_id = None
        if candidate.construction != QueryConstruction.IDENTITY:
            intent = active_intents.get(candidate.case_id)
            if (
                intent is None
                or intent.oracle_batch_id != batch.oracle_batch_id
                or intent.unit_id != unit.unit_id
                or intent.case_context_sha256 != candidate.case_context_sha256
            ):
                raise ValueError("behavior view requires the active intent annotation")
            intent_id = intent.intent_annotation_id
        output.append(
            OracleBehaviorViewCandidate(
                case_id=candidate.case_id,
                construction=candidate.construction,
                query_text=sample.query_text,
                source_query_text=sample.source_query_text,
                categories=sample.categories,
                reason_code=sample.reason_code,
                source_returned_at_k=sample.source_returned_at_k,
                variant_returned_at_k=sample.variant_returned_at_k,
                overlap_at_k=sample.overlap_at_k,
                source_top_hits=sample.source_top_hits,
                variant_top_hits=sample.variant_top_hits,
                behavior_context_sha256=candidate.behavior_context_sha256,
                intent_annotation_id=intent_id,
            )
        )
    logger.debug(
        "human_oracle_behavior_view_built",
        extra={
            "candidate_count": len(output),
            "diagnostic_id": diagnostic.diagnostic_id,
            "oracle_batch_id": batch.oracle_batch_id,
            "unit_id": unit.unit_id,
        },
    )
    return OracleBehaviorView(
        oracle_batch_id=batch.oracle_batch_id,
        diagnostic_id=diagnostic.diagnostic_id,
        unit_id=unit.unit_id,
        source_case_id=unit.source_case_id,
        candidates=output,
    )


def collect_behavior_samples_for_unit(
    *,
    batch: OracleBatchArtifact,
    diagnostic: BadCaseDiagnosticArtifact,
    query_set: QuerySetArtifact,
    unit_id: str,
    search_service: CatalogSearchService,
) -> list[BadCaseSample]:
    try:
        return _collect_behavior_samples_for_unit(
            batch=batch,
            diagnostic=diagnostic,
            query_set=query_set,
            unit_id=unit_id,
            search_service=search_service,
        )
    except Exception as exc:
        extra = {
            "diagnostic_id": diagnostic.diagnostic_id,
            "error_code": "evidence_collection_or_validation_failed",
            "error_type": type(exc).__name__,
            "oracle_batch_id": batch.oracle_batch_id,
        }
        if isinstance(unit_id, str) and re.fullmatch(
            r"^oracle-unit-[0-9a-f]{12}$",
            unit_id,
        ):
            extra["unit_id"] = unit_id
        logger.error(
            "human_oracle_behavior_collection_failed",
            extra=extra,
        )
        raise


def _collect_behavior_samples_for_unit(
    *,
    batch: OracleBatchArtifact,
    diagnostic: BadCaseDiagnosticArtifact,
    query_set: QuerySetArtifact,
    unit_id: str,
    search_service: CatalogSearchService,
) -> list[BadCaseSample]:
    """Re-run only one source cluster and verify every byte-relevant digest.

    The returned raw Query/product display content is transient.  This function
    performs no storage and logs only IDs/counts.
    """

    batch = validate_oracle_batch(
        batch=batch,
        diagnostic=diagnostic,
        query_set=query_set,
    )
    unit = _unit(batch, unit_id)
    _validate_service_identity(search_service, diagnostic)
    query_cases = {item.case_id: item for item in query_set.cases}
    ordered_case_ids = [unit.source_case_id]
    ordered_case_ids.extend(
        candidate.case_id
        for candidate in unit.candidates
        if candidate.case_id != unit.source_case_id
    )
    if (
        len(ordered_case_ids) != len(set(ordered_case_ids))
        or not 1 <= len(ordered_case_ids) <= 3
    ):
        raise ValueError("Oracle unit Query replay must contain 1 to 3 unique cases")
    for case_id in ordered_case_ids:
        if case_id not in query_cases:
            raise ValueError("Oracle unit Query case is unavailable")

    logger.info(
        "human_oracle_behavior_collection_started",
        extra={
            "diagnostic_id": diagnostic.diagnostic_id,
            "oracle_batch_id": batch.oracle_batch_id,
            "query_count": len(ordered_case_ids),
            "unit_id": unit.unit_id,
        },
    )
    results = search_service.search_many(
        tuple(query_cases[case_id].query_text for case_id in ordered_case_ids),
        top_k=10,
        max_elapsed_ms=30_000,
        max_query_elapsed_ms=5_000,
    )
    if len(results) != len(ordered_case_ids):
        raise RuntimeError("Oracle unit replay returned partial results")
    _validate_service_identity(search_service, diagnostic)
    observations = {item.case_id: item for item in diagnostic.observations}
    results_by_case = dict(zip(ordered_case_ids, results, strict=True))
    for case_id, result in results_by_case.items():
        _validate_result_against_observation(
            result,
            observations[case_id],
            diagnostic=diagnostic,
        )

    diagnostics = {item.case_id: item for item in diagnostic.diagnostics}
    samples = []
    for candidate in unit.candidates:
        diagnosed = diagnostics[candidate.case_id]
        source_result = results_by_case[candidate.source_case_id]
        variant_result = results_by_case[candidate.case_id]
        source_case = query_cases[candidate.source_case_id]
        variant_case = query_cases[candidate.case_id]
        if (
            source_case.normalized_query_sha256 != candidate.source_query_sha256
            or variant_case.normalized_query_sha256 != candidate.variant_query_sha256
        ):
            raise ValueError("Oracle unit Query hash changed before display")
        samples.append(
            BadCaseSample(
                case_id=candidate.case_id,
                source_case_id=candidate.source_case_id,
                construction=candidate.construction,
                categories=diagnosed.categories,
                reason_code=diagnosed.reason_code,
                query_text=variant_case.query_text,
                source_query_text=source_case.query_text,
                source_returned_at_k=diagnosed.source_returned_at_k,
                variant_returned_at_k=diagnosed.variant_returned_at_k,
                overlap_at_k=diagnosed.overlap_at_k,
                source_top_hits=[_display_hit(hit) for hit in source_result.hits[:3]],
                variant_top_hits=[_display_hit(hit) for hit in variant_result.hits[:3]],
            )
        )
    logger.info(
        "human_oracle_behavior_collection_completed",
        extra={
            "diagnostic_id": diagnostic.diagnostic_id,
            "oracle_batch_id": batch.oracle_batch_id,
            "query_count": len(ordered_case_ids),
            "sample_count": len(samples),
            "unit_id": unit.unit_id,
        },
    )
    return samples


def _unit(batch: OracleBatchArtifact, unit_id: str) -> OracleReviewUnit:
    unit = next((item for item in batch.units if item.unit_id == unit_id), None)
    if unit is None:
        raise ValueError("Oracle unit does not belong to its batch")
    return unit


def _log_view_failure(
    *,
    operation: str,
    error: Exception,
    batch: OracleBatchArtifact,
    unit_id: object,
) -> None:
    extra = {
        "error_code": "view_context_or_evidence_invalid",
        "error_type": type(error).__name__,
        "operation": operation,
        "oracle_batch_id": batch.oracle_batch_id,
    }
    if isinstance(unit_id, str) and re.fullmatch(
        r"^oracle-unit-[0-9a-f]{12}$",
        unit_id,
    ):
        extra["unit_id"] = unit_id
    logger.warning("human_oracle_view_failed", extra=extra)


def _validate_hits(hits, observation) -> None:
    expected_count = min(3, observation.returned_at_k)
    if len(hits) != expected_count:
        raise ValueError("behavior view must contain the complete Top-3 evidence")
    product_keys = [
        product_key_sha256(locale=item.locale, product_id=item.product_id)
        for item in hits
    ]
    if product_keys != observation.ordered_product_key_sha256s[: len(hits)]:
        raise ValueError("behavior view product keys do not match evidence")
    display_hashes = [
        display_hit_sha256(
            locale=item.locale,
            product_id=item.product_id,
            title=item.title,
            rank=item.rank,
        )
        for item in hits
    ]
    if display_hashes != observation.ordered_display_hit_sha256s[: len(hits)]:
        raise ValueError("behavior view display hits do not match evidence")


def _validate_service_identity(
    service: CatalogSearchService,
    diagnostic: BadCaseDiagnosticArtifact,
) -> None:
    metadata = service.metadata
    if (
        metadata.index_id != diagnostic.index_id
        or metadata.product_count != diagnostic.catalog_product_count
        or metadata.code_revision != diagnostic.index_build_revision
        or metadata.source_sha256 != diagnostic.index_source_sha256
        or canonical_json_sha256(metadata.index_config)
        != diagnostic.index_config_sha256
    ):
        raise RuntimeError("Oracle catalog identity does not match diagnostic evidence")


def _validate_result_against_observation(
    result,
    observation,
    *,
    diagnostic: BadCaseDiagnosticArtifact,
) -> None:
    if (
        result.index_id != diagnostic.index_id
        or result.product_count != diagnostic.catalog_product_count
        or len(result.hits) > 10
        or [item.rank for item in result.hits] != list(range(1, len(result.hits) + 1))
        or any(item.strategy != diagnostic.search_strategy_id for item in result.hits)
    ):
        raise RuntimeError("Oracle catalog result violates diagnostic identity")
    product_keys = [
        product_key_sha256(
            locale=item.product.locale,
            product_id=item.product.product_id,
        )
        for item in result.hits
    ]
    display_hashes = [
        display_hit_sha256(
            locale=item.product.locale,
            product_id=item.product.product_id,
            title=_safe_display_title(item.product.title),
            rank=item.rank,
        )
        for item in result.hits
    ]
    if (
        observation.returned_at_k != len(result.hits)
        or observation.ordered_product_key_sha256s != product_keys
        or observation.ordered_display_hit_sha256s != display_hashes
        or observation.ordered_results_sha256 != ordered_results_sha256(product_keys)
        or observation.result_set_sha256 != result_set_sha256(product_keys)
    ):
        raise RuntimeError("Oracle replay result does not match diagnostic evidence")


def _display_hit(hit) -> BadCaseDisplayHit:
    return BadCaseDisplayHit(
        product_id=hit.product.product_id,
        locale=hit.product.locale,
        title=_safe_display_title(hit.product.title),
        rank=hit.rank,
    )


def _safe_display_title(value: str) -> str:
    sanitized = "".join(
        " " if unicodedata.category(character) == "Cc" else character
        for character in value
    ).strip()
    if not sanitized:
        raise ValueError("catalog result title is empty after display sanitization")
    return sanitized[:256]
