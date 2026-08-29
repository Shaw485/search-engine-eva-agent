"""Run and replay the fixed 59-Query behavioral diagnostic batch."""

from __future__ import annotations

import hashlib
import logging
import re
import stat
import time
import unicodedata
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from search_quality.catalog import (
    DEFAULT_CATALOG_INDEX,
    CatalogSearchResult,
    CatalogSearchService,
    validate_catalog_query,
)
from search_quality.data.contracts import canonical_json_sha256
from search_quality.evaluation.artifacts import require_clean_code_revision
from search_quality.observability import classify_error, new_trace_id
from search_quality.query_constructor import (
    build_smoke_query_set,
    store_query_set,
    validate_query_set,
)
from search_quality.query_constructor.contracts import (
    QueryCase,
    QueryConstruction,
    QuerySetArtifact,
)

from .artifacts import (
    bad_case_run_lock,
    ensure_bad_case_capacity,
    store_bad_case_artifacts,
    store_failed_attempt,
)
from .contracts import (
    CATEGORY_ORDER,
    EXPECTED_IDENTITY_COUNT,
    EXPECTED_QUERY_COUNT,
    EXPECTED_REVERSAL_COUNT,
    EXPECTED_TRANSPOSITION_COUNT,
    RUNNER_ID,
    TOP_K,
    BadCaseDiagnosticArtifact,
    BadCaseDisplayHit,
    BadCaseExecutionReceipt,
    BadCaseFailedAttempt,
    BadCaseObservation,
    BadCaseRun,
    BadCaseSample,
    derive_diagnostic,
    diagnostic_id,
    display_hit_sha256,
    ordered_results_sha256,
    product_key_sha256,
    result_set_sha256,
)

logger = logging.getLogger("search_quality.bad_case")
MAX_BATCH_ELAPSED_MS = 120_000
MAX_QUERY_ELAPSED_MS = 5_000
MAX_DISPLAY_SAMPLES = 12
MAX_DISPLAY_HITS = 3
_FORBIDDEN_AUTHORITY_NAMES = frozenset(
    {
        "active-strategy.json",
        "search-strategies",
        "strategy-decisions",
        "strategy-proposals",
    }
)
_EXECUTION_ID_RE = re.compile(r"bad-case-execution-[0-9a-f]{32}\Z")


def run_bad_case_diagnostics(
    *,
    project_root: str | Path,
    artifact_root: str | Path | None = None,
    source_profile: str = "smoke",
    revision_provider: Callable[[Path], str] = require_clean_code_revision,
    search_service: CatalogSearchService | None = None,
    execution_id: str | None = None,
    execution_started_at_utc: datetime | None = None,
) -> BadCaseRun:
    """Publish evidence only after all 59 bounded searches succeed."""

    root = Path(project_root).resolve(strict=True)
    run_root = root / "runs" if artifact_root is None else Path(artifact_root)
    execution_id = _validated_execution_id(execution_id)
    started_at = _validated_started_at(execution_started_at_utc)
    started = time.perf_counter()
    completed_query_count = 0
    failure_stage = "source_preflight"
    attempt_storage_allowed = False

    with bad_case_run_lock(run_root) as base:
        try:
            ensure_bad_case_capacity(base)
            attempt_storage_allowed = True
            executor_revision = _clean_revision(root, revision_provider)
            authority_before = _authority_snapshot(run_root)
            protected_dispatches: list[str] = []

            failure_stage = "query_construction"
            query_set = build_smoke_query_set(
                project_root=root,
                source_profile=source_profile,
                revision_provider=lambda _root: executor_revision,
                profile_access_recorder=protected_dispatches.append,
            )
            query_set = _validate_fixed_query_set(query_set)
            protected_count = sum(
                profile in {"dev", "test"} for profile in protected_dispatches
            )
            if protected_count != 0 or protected_dispatches != ["smoke"]:
                raise RuntimeError("Bad Case Query source access escaped smoke")

            # This is deliberately a separate whole-batch preflight before the
            # service is asked to execute even one Query.
            failure_stage = "source_preflight"
            for case in query_set.cases:
                validate_catalog_query(case.query_text, top_k=TOP_K)

            failure_stage = "catalog_search"
            service = search_service or CatalogSearchService(
                root / DEFAULT_CATALOG_INDEX
            )
            index_before = _index_snapshot(service)
            logger.info(
                "bad_case_batch_started",
                extra={
                    "execution_id": execution_id,
                    "index_id": service.metadata.index_id,
                    "query_count": EXPECTED_QUERY_COUNT,
                    "query_set_id": query_set.query_set_id,
                    "top_k": TOP_K,
                },
            )
            results = service.search_many(
                tuple(case.query_text for case in query_set.cases),
                top_k=TOP_K,
                max_elapsed_ms=MAX_BATCH_ELAPSED_MS,
                max_query_elapsed_ms=MAX_QUERY_ELAPSED_MS,
            )
            completed_query_count = len(results)
            if completed_query_count != EXPECTED_QUERY_COUNT:
                raise RuntimeError("catalog batch returned partial diagnostics")
            index_after = _index_snapshot(service)
            if index_after != index_before:
                raise RuntimeError("catalog index changed during diagnostics")
            _validate_catalog_results(results, service=service)

            failure_stage = "evidence_validation"
            artifact, samples = _build_artifact_and_samples(
                query_set=query_set,
                results=results,
                service=service,
                executor_revision=executor_revision,
                protected_profile_dispatch_count=protected_count,
            )
            artifact = validate_bad_case_diagnostic(
                artifact=artifact,
                query_set=query_set,
            )

            failure_stage = "authority_check"
            authority_after = _authority_snapshot(run_root)
            if authority_after != authority_before:
                raise RuntimeError(
                    "strategy authority changed during Bad Case diagnostics"
                )

            completed_at = _utc_now()
            execution = BadCaseExecutionReceipt(
                execution_id=execution_id,
                diagnostic_id=artifact.diagnostic_id,
                query_set_id=query_set.query_set_id,
                index_id=artifact.index_id,
                completed_query_count=EXPECTED_QUERY_COUNT,
                started_at_utc=started_at,
                completed_at_utc=completed_at,
                duration_ms=_elapsed_ms(started),
            )
            # Validate transient display content against hashed evidence before
            # any completed artifact is published.
            BadCaseRun(
                artifact=artifact,
                execution=execution,
                samples=samples,
                artifact_path="pending",
                execution_path="pending",
            )

            failure_stage = "artifact_storage"
            store_query_set(query_set, artifact_root=run_root)
            artifact_path, execution_path = store_bad_case_artifacts(
                artifact_root=run_root,
                artifact=artifact,
                execution=execution,
            )
            run = BadCaseRun(
                artifact=artifact,
                execution=execution,
                samples=samples,
                artifact_path=str(artifact_path),
                execution_path=str(execution_path),
            )
            logger.info(
                "bad_case_batch_completed",
                extra={
                    "diagnostic_candidate_count": (artifact.diagnostic_candidate_count),
                    "diagnostic_id": artifact.diagnostic_id,
                    "duration_ms": round(_elapsed_ms(started), 3),
                    "execution_id": execution_id,
                    "query_count": artifact.query_count,
                },
            )
            return run
        except Exception as exc:
            safe_completed_count = getattr(exc, "completed_query_count", 0)
            if (
                isinstance(safe_completed_count, int)
                and not isinstance(safe_completed_count, bool)
                and 0 <= safe_completed_count <= EXPECTED_QUERY_COUNT
            ):
                completed_query_count = max(
                    completed_query_count,
                    safe_completed_count,
                )
            logger.error(
                "bad_case_batch_failed",
                extra={
                    "completed_query_count": completed_query_count,
                    "duration_ms": round(_elapsed_ms(started), 3),
                    "error_code": classify_error(exc),
                    "error_type": type(exc).__name__,
                    "execution_id": execution_id,
                    "failure_stage": failure_stage,
                },
            )
            if attempt_storage_allowed:
                attempt = BadCaseFailedAttempt(
                    execution_id=execution_id,
                    failure_stage=failure_stage,
                    completed_query_count=completed_query_count,
                    error_code=classify_error(exc),
                    started_at_utc=started_at,
                    completed_at_utc=_utc_now(),
                    duration_ms=_elapsed_ms(started),
                )
                try:
                    store_failed_attempt(artifact_root=run_root, attempt=attempt)
                except Exception as storage_exc:
                    logger.error(
                        "bad_case_failed_attempt_storage_failed",
                        extra={
                            "error_code": classify_error(storage_exc),
                            "error_type": type(storage_exc).__name__,
                            "execution_id": execution_id,
                        },
                    )
            raise


def validate_bad_case_diagnostic(
    *,
    artifact: BadCaseDiagnosticArtifact,
    query_set: QuerySetArtifact,
) -> BadCaseDiagnosticArtifact:
    """Offline validation against the trusted Query set; performs no search."""

    validated_artifact = BadCaseDiagnosticArtifact.model_validate(
        artifact.model_dump(mode="json")
    )
    validated_query_set = _validate_fixed_query_set(validate_query_set(query_set))
    if (
        validated_query_set.query_set_id != validated_artifact.query_set_id
        or validated_query_set.code_revision != validated_artifact.query_set_revision
        or validated_query_set.source_contract_sha256
        != validated_artifact.query_source_contract_sha256
    ):
        raise ValueError("trusted Query set does not match diagnostic provenance")
    cases = {item.case_id: item for item in validated_query_set.cases}
    if set(cases) != {item.case_id for item in validated_artifact.observations}:
        raise ValueError("diagnostic observations do not match trusted Query cases")
    for observation in validated_artifact.observations:
        case = cases[observation.case_id]
        source = next(
            item
            for item in validated_query_set.cases
            if item.source.query_id == case.source.query_id
            and item.construction == QueryConstruction.IDENTITY
        )
        if (
            observation.source_case_id != source.case_id
            or observation.source_query_id != case.source.query_id
            or observation.query_sha256 != case.normalized_query_sha256
            or observation.construction != case.construction
        ):
            raise ValueError("diagnostic observation contradicts trusted Query case")
    return validated_artifact


def rerun_bad_case_diagnostic(
    *,
    artifact: BadCaseDiagnosticArtifact,
    query_set: QuerySetArtifact,
    search_service: CatalogSearchService,
) -> BadCaseDiagnosticArtifact:
    """Re-run catalog searches for reproducibility; this is not offline Replay."""

    artifact = validate_bad_case_diagnostic(artifact=artifact, query_set=query_set)
    validated_query_set = _validate_fixed_query_set(validate_query_set(query_set))
    for case in validated_query_set.cases:
        validate_catalog_query(case.query_text, top_k=TOP_K)
    index_before = _index_snapshot(search_service)
    results = search_service.search_many(
        tuple(case.query_text for case in validated_query_set.cases),
        top_k=TOP_K,
        max_elapsed_ms=MAX_BATCH_ELAPSED_MS,
        max_query_elapsed_ms=MAX_QUERY_ELAPSED_MS,
    )
    if len(results) != EXPECTED_QUERY_COUNT:
        raise RuntimeError("catalog rerun returned partial diagnostics")
    if _index_snapshot(search_service) != index_before:
        raise RuntimeError("catalog index changed during diagnostic rerun")
    _validate_catalog_results(results, service=search_service)
    replayed, _samples = _build_artifact_and_samples(
        query_set=validated_query_set,
        results=results,
        service=search_service,
        executor_revision=artifact.executor_revision,
        protected_profile_dispatch_count=0,
    )
    if replayed != artifact:
        raise ValueError("Bad Case diagnostic rerun does not match evidence")
    logger.info(
        "bad_case_rerun_completed",
        extra={
            "diagnostic_id": artifact.diagnostic_id,
            "query_count": artifact.query_count,
        },
    )
    return replayed


def _validate_fixed_query_set(query_set: QuerySetArtifact) -> QuerySetArtifact:
    query_set = validate_query_set(query_set)
    counts = Counter(item.construction for item in query_set.cases)
    expected = {
        QueryConstruction.IDENTITY: EXPECTED_IDENTITY_COUNT,
        QueryConstruction.ADJACENT_TRANSPOSITION: EXPECTED_TRANSPOSITION_COUNT,
        QueryConstruction.TOKEN_ORDER_REVERSAL: EXPECTED_REVERSAL_COUNT,
    }
    if (
        query_set.source_profile != "smoke"
        or query_set.query_count != EXPECTED_QUERY_COUNT
        or query_set.original_count != EXPECTED_IDENTITY_COUNT
        or query_set.synthetic_count
        != EXPECTED_TRANSPOSITION_COUNT + EXPECTED_REVERSAL_COUNT
        or counts != expected
        or query_set.formal_evaluation_allowed
        or query_set.locked_profiles_not_read != ("dev", "test")
    ):
        raise ValueError("Bad Case executor requires the fixed 59-Query smoke set")
    identity_ids = {
        item.source.query_id
        for item in query_set.cases
        if item.construction == QueryConstruction.IDENTITY
    }
    synthetic_pairs = {
        (item.source.query_id, item.construction)
        for item in query_set.cases
        if item.construction != QueryConstruction.IDENTITY
    }
    if len(identity_ids) != EXPECTED_IDENTITY_COUNT or len(synthetic_pairs) != 39:
        raise ValueError("Bad Case Query pairs are incomplete")
    return query_set


def _build_artifact_and_samples(
    *,
    query_set: QuerySetArtifact,
    results: tuple[CatalogSearchResult, ...],
    service: CatalogSearchService,
    executor_revision: str,
    protected_profile_dispatch_count: int,
) -> tuple[BadCaseDiagnosticArtifact, list[BadCaseSample]]:
    if len(query_set.cases) != len(results):
        raise ValueError("Query cases and catalog results do not align")
    identity_cases = {
        item.source.query_id: item
        for item in query_set.cases
        if item.construction == QueryConstruction.IDENTITY
    }
    observations: list[BadCaseObservation] = []
    result_by_case: dict[str, CatalogSearchResult] = {}
    case_by_id = {item.case_id: item for item in query_set.cases}
    for case, result in zip(query_set.cases, results, strict=True):
        source_case = identity_cases[case.source.query_id]
        product_keys = [
            product_key_sha256(
                locale=hit.product.locale,
                product_id=hit.product.product_id,
            )
            for hit in result.hits
        ]
        observations.append(
            BadCaseObservation(
                case_id=case.case_id,
                source_case_id=source_case.case_id,
                source_query_id=case.source.query_id,
                query_sha256=case.normalized_query_sha256,
                construction=case.construction,
                returned_at_k=len(result.hits),
                ordered_product_key_sha256s=product_keys,
                ordered_display_hit_sha256s=[
                    display_hit_sha256(
                        locale=hit.product.locale,
                        product_id=hit.product.product_id,
                        title=_safe_display_title(hit.product.title),
                        rank=hit.rank,
                    )
                    for hit in result.hits
                ],
                ordered_results_sha256=ordered_results_sha256(product_keys),
                result_set_sha256=result_set_sha256(product_keys),
            )
        )
        result_by_case[case.case_id] = result
    observations.sort(
        key=lambda item: (
            item.source_query_id,
            {
                QueryConstruction.IDENTITY: 0,
                QueryConstruction.ADJACENT_TRANSPOSITION: 1,
                QueryConstruction.TOKEN_ORDER_REVERSAL: 2,
            }[item.construction],
            item.case_id,
        )
    )
    observations_by_case = {item.case_id: item for item in observations}
    diagnostics = []
    for observation in observations:
        source = observations_by_case[observation.source_case_id]
        candidate = derive_diagnostic(observation, source)
        if candidate is not None:
            diagnostics.append(candidate)
    category_counts = Counter(
        category.value
        for diagnostic in diagnostics
        for category in diagnostic.categories
    )
    metadata = service.metadata
    body: dict[str, Any] = {
        "catalog_product_count": metadata.product_count,
        "category_counts": {
            category.value: category_counts[category.value]
            for category in CATEGORY_ORDER
        },
        "completed": True,
        "construction_counts": {
            "identity": EXPECTED_IDENTITY_COUNT,
            "adjacent_transposition": EXPECTED_TRANSPOSITION_COUNT,
            "token_order_reversal": EXPECTED_REVERSAL_COUNT,
        },
        "diagnostic_candidate_count": len(diagnostics),
        "diagnostics": [item.model_dump(mode="json") for item in diagnostics],
        "executor_revision": executor_revision,
        "formal_evaluation_allowed": False,
        "index_build_revision": metadata.code_revision,
        "index_config_sha256": canonical_json_sha256(metadata.index_config),
        "index_id": metadata.index_id,
        "index_source_sha256": metadata.source_sha256,
        "limitations": [
            "synthetic_queries_are_unjudged",
            "diagnostics_do_not_claim_relevance_improvement",
            "development_smoke_is_not_final_evaluation",
            "single_stage_catalog_cannot_diagnose_stage_drop",
            "no_hard_worker_deadline_enforcement",
        ],
        "locked_profiles_not_read": ["dev", "test"],
        "observations": [item.model_dump(mode="json") for item in observations],
        "operational_failure_count": 0,
        "original_count": EXPECTED_IDENTITY_COUNT,
        "protected_profile_dispatch_count": protected_profile_dispatch_count,
        "quality_metrics_computed": False,
        "query_count": EXPECTED_QUERY_COUNT,
        "query_set_id": query_set.query_set_id,
        "query_set_revision": query_set.code_revision,
        "query_source_contract_sha256": query_set.source_contract_sha256,
        "raw_product_content_stored": False,
        "raw_query_text_stored": False,
        "relevance_labels_used": False,
        "relevance_metrics_computed": False,
        "runner_id": RUNNER_ID,
        "schema_version": "bad-case-diagnostic-v1",
        "search_call_count": EXPECTED_QUERY_COUNT,
        "search_strategy_id": "sqlite-fts5-bm25",
        "stage_drop_diagnostics_computed": False,
        "strategy_write_count": 0,
        "synthetic_count": EXPECTED_TRANSPOSITION_COUNT + EXPECTED_REVERSAL_COUNT,
        "top_k": TOP_K,
    }
    artifact = BadCaseDiagnosticArtifact.model_validate(
        {**body, "diagnostic_id": diagnostic_id(body)}
    )
    samples = _build_samples(
        diagnostics=artifact.diagnostics,
        case_by_id=case_by_id,
        result_by_case=result_by_case,
    )
    return artifact, samples


def _build_samples(
    *,
    diagnostics,
    case_by_id: dict[str, QueryCase],
    result_by_case: dict[str, CatalogSearchResult],
) -> list[BadCaseSample]:
    selected = []
    selected_ids: set[str] = set()
    # First cover every observed category once, then fill in deterministic
    # diagnostic order. This prevents one common typo class hiding rarer cases.
    for category in CATEGORY_ORDER:
        candidate = next(
            (
                item
                for item in diagnostics
                if category in item.categories and item.case_id not in selected_ids
            ),
            None,
        )
        if candidate is not None:
            selected.append(candidate)
            selected_ids.add(candidate.case_id)
    for candidate in diagnostics:
        if len(selected) >= MAX_DISPLAY_SAMPLES:
            break
        if candidate.case_id not in selected_ids:
            selected.append(candidate)
            selected_ids.add(candidate.case_id)

    samples = []
    for diagnostic in selected:
        case = case_by_id[diagnostic.case_id]
        source_case = case_by_id[diagnostic.source_case_id]
        source_result = result_by_case[source_case.case_id]
        variant_result = result_by_case[case.case_id]
        samples.append(
            BadCaseSample(
                case_id=case.case_id,
                source_case_id=source_case.case_id,
                construction=case.construction,
                categories=diagnostic.categories,
                reason_code=diagnostic.reason_code,
                query_text=case.query_text,
                source_query_text=source_case.query_text,
                source_returned_at_k=diagnostic.source_returned_at_k,
                variant_returned_at_k=diagnostic.variant_returned_at_k,
                overlap_at_k=diagnostic.overlap_at_k,
                source_top_hits=[
                    _display_hit(hit) for hit in source_result.hits[:MAX_DISPLAY_HITS]
                ],
                variant_top_hits=[
                    _display_hit(hit) for hit in variant_result.hits[:MAX_DISPLAY_HITS]
                ],
            )
        )
    return samples


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


def _validate_catalog_results(
    results: tuple[CatalogSearchResult, ...],
    *,
    service: CatalogSearchService,
) -> None:
    metadata = service.metadata
    for result in results:
        if (
            result.index_id != metadata.index_id
            or result.product_count != metadata.product_count
            or result.locale_counts != metadata.locale_counts
            or len(result.hits) > TOP_K
            or any(hit.strategy != "sqlite-fts5-bm25" for hit in result.hits)
            or [hit.rank for hit in result.hits] != list(range(1, len(result.hits) + 1))
        ):
            raise RuntimeError("catalog result violates the fixed batch contract")


def _clean_revision(
    root: Path,
    revision_provider: Callable[[Path], str],
) -> str:
    revision = revision_provider(root).strip()
    if len(revision) != 40 or any(
        character not in "0123456789abcdef" for character in revision
    ):
        raise ValueError("Bad Case executor requires a clean full Git revision")
    return revision


def _index_snapshot(service: CatalogSearchService) -> tuple[object, ...]:
    path = Path(service.index_path)
    details = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(details.st_mode):
        raise RuntimeError("catalog index is no longer a regular file")
    metadata = service.metadata
    return (
        details.st_dev,
        details.st_ino,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
        metadata.index_id,
        metadata.source_sha256,
        metadata.code_revision,
        canonical_json_sha256(metadata.index_config),
    )


def _authority_snapshot(root: Path) -> tuple[tuple[str, str, int], ...]:
    records: list[tuple[str, str, int]] = []
    for name in sorted(_FORBIDDEN_AUTHORITY_NAMES):
        authority = root / name
        if authority.is_symlink():
            records.append((name, "symlink", authority.lstat().st_size))
            continue
        if not authority.exists():
            records.append((name, "absent", 0))
            continue
        candidates = (authority,) if authority.is_file() else authority.rglob("*")
        for candidate in candidates:
            relative = str(candidate.relative_to(root))
            if candidate.is_symlink():
                records.append((relative, "symlink", candidate.lstat().st_size))
            elif candidate.is_file():
                records.append(
                    (relative, _file_sha256(candidate), candidate.stat().st_size)
                )
            elif candidate.is_dir():
                records.append((relative, "directory", 0))
    return tuple(sorted(records))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1_000.0


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _validated_execution_id(value: str | None) -> str:
    execution_id = f"bad-case-execution-{new_trace_id()}" if value is None else value
    if type(execution_id) is not str or not _EXECUTION_ID_RE.fullmatch(execution_id):
        raise ValueError("Bad Case execution ID is invalid")
    return execution_id


def _validated_started_at(value: datetime | None) -> datetime:
    started_at = value or _utc_now()
    if not isinstance(started_at, datetime) or started_at.utcoffset() is None:
        raise ValueError("Bad Case execution start time must include a timezone")
    return started_at
