from __future__ import annotations

import io
import json
import logging
import stat
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from search_quality.bad_cases.contracts import (
    BadCaseCategoryCounts,
    BadCaseDiagnosticArtifact,
    BadCaseDisplayHit,
    BadCaseObservation,
    BadCaseSample,
    derive_diagnostic,
    diagnostic_id,
    display_hit_sha256,
    ordered_results_sha256,
    product_key_sha256,
    result_set_sha256,
)
from search_quality.catalog import (
    CatalogProduct,
    CatalogSearchHit,
    CatalogSearchResult,
)
from search_quality.data.contracts import canonical_json_sha256
from search_quality.human_oracle import (
    BehaviorJudgment,
    BehaviorReason,
    BehaviorSubmission,
    HumanOracleRepository,
    IntentJudgment,
    IntentReason,
    IntentSubmission,
    OracleActor,
    OracleBatchIncomplete,
    OracleBatchSealed,
    OracleBatchStatus,
    OracleClientActionConflict,
    OracleCompareAndSwapConflict,
    OracleInvalidDecision,
    OracleStorageError,
    SealSubmission,
    build_behavior_view,
    build_intent_view,
    build_oracle_batch,
    collect_behavior_samples_for_unit,
    validate_oracle_batch,
)
from search_quality.human_oracle import storage as oracle_storage
from search_quality.human_oracle.contracts import (
    OracleBatchArtifact,
    OracleBehaviorViewCandidate,
    OracleReviewUnit,
    oracle_batch_id,
    oracle_unit_id,
)
from search_quality.observability import JsonLogFormatter
from search_quality.query_constructor import builder as query_builder
from search_quality.query_constructor.contracts import QueryConstruction

ROOT = Path(__file__).resolve().parents[1]
REVISION = "a" * 40
ACTOR = OracleActor(
    principal_hmac_sha256="f" * 64,
    actor_key_id="oracle-actor-key-v1",
)


@pytest.fixture(scope="module")
def query_set():
    return query_builder.build_smoke_query_set(
        project_root=ROOT,
        revision_provider=lambda _root: REVISION,
    )


@pytest.fixture(scope="module")
def diagnostic(query_set):
    identities = {
        case.source.query_id: case
        for case in query_set.cases
        if case.construction == QueryConstruction.IDENTITY
    }
    with_reversal = {
        case.source.query_id
        for case in query_set.cases
        if case.construction == QueryConstruction.TOKEN_ORDER_REVERSAL
    }
    source_zero_ids = set(sorted(with_reversal)[:10])
    assert len(source_zero_ids) == 10

    observations = []
    for case in query_set.cases:
        source_case = identities[case.source.query_id]
        source_is_zero = case.source.query_id in source_zero_ids
        variant_is_typo = case.construction == QueryConstruction.ADJACENT_TRANSPOSITION
        product_ids = (
            []
            if source_is_zero or variant_is_typo
            else [f"P{case.source.query_id:05d}{rank:02d}" for rank in range(1, 4)]
        )
        product_keys = [
            product_key_sha256(locale="us", product_id=product_id)
            for product_id in product_ids
        ]
        observations.append(
            BadCaseObservation(
                case_id=case.case_id,
                source_case_id=source_case.case_id,
                source_query_id=case.source.query_id,
                query_sha256=case.normalized_query_sha256,
                construction=case.construction,
                returned_at_k=len(product_ids),
                ordered_product_key_sha256s=product_keys,
                ordered_display_hit_sha256s=[
                    display_hit_sha256(
                        locale="us",
                        product_id=product_id,
                        title=f"Fixture product {case.source.query_id}",
                        rank=rank,
                    )
                    for rank, product_id in enumerate(product_ids, start=1)
                ],
                ordered_results_sha256=ordered_results_sha256(product_keys),
                result_set_sha256=result_set_sha256(product_keys),
            )
        )
    construction_order = {
        QueryConstruction.IDENTITY: 0,
        QueryConstruction.ADJACENT_TRANSPOSITION: 1,
        QueryConstruction.TOKEN_ORDER_REVERSAL: 2,
    }
    observations.sort(
        key=lambda item: (
            item.source_query_id,
            construction_order[item.construction],
            item.case_id,
        )
    )
    by_case = {item.case_id: item for item in observations}
    diagnostics = []
    for observation in observations:
        candidate = derive_diagnostic(
            observation,
            by_case[observation.source_case_id],
        )
        if candidate is not None:
            diagnostics.append(candidate)
    category_counts = Counter(
        category.value for candidate in diagnostics for category in candidate.categories
    )
    assert len(diagnostics) == 40
    body = {
        "catalog_product_count": 100,
        "category_counts": BadCaseCategoryCounts(
            zero_result=category_counts["zero_result"],
            spelling_sensitive=category_counts["spelling_sensitive"],
            order_sensitive=category_counts["order_sensitive"],
            ranking_instability_needs_judgment=category_counts[
                "ranking_instability_needs_judgment"
            ],
        ).model_dump(mode="json"),
        "completed": True,
        "construction_counts": {
            "identity": 20,
            "adjacent_transposition": 20,
            "token_order_reversal": 19,
        },
        "diagnostic_candidate_count": len(diagnostics),
        "diagnostics": [item.model_dump(mode="json") for item in diagnostics],
        "executor_revision": REVISION,
        "formal_evaluation_allowed": False,
        "index_build_revision": "b" * 40,
        "index_config_sha256": canonical_json_sha256({"fixture": "human-oracle"}),
        "index_id": "catalog-baseline-v1-aaaaaaaaaaaa",
        "index_source_sha256": "d" * 64,
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
        "original_count": 20,
        "protected_profile_dispatch_count": 0,
        "quality_metrics_computed": False,
        "query_count": 59,
        "query_set_id": query_set.query_set_id,
        "query_set_revision": query_set.code_revision,
        "query_source_contract_sha256": query_set.source_contract_sha256,
        "raw_product_content_stored": False,
        "raw_query_text_stored": False,
        "relevance_labels_used": False,
        "relevance_metrics_computed": False,
        "runner_id": "source-bounded-bad-case-runner-v1",
        "schema_version": "bad-case-diagnostic-v1",
        "search_call_count": 59,
        "search_strategy_id": "sqlite-fts5-bm25",
        "stage_drop_diagnostics_computed": False,
        "strategy_write_count": 0,
        "synthetic_count": 39,
        "top_k": 10,
    }
    return BadCaseDiagnosticArtifact.model_validate(
        {**body, "diagnostic_id": diagnostic_id(body)}
    )


@pytest.fixture(scope="module")
def batch(query_set, diagnostic):
    return build_oracle_batch(diagnostic=diagnostic, query_set=query_set)


@pytest.fixture
def oracle_logs():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger = logging.getLogger("search_quality.human_oracle")
    previous_handlers = list(logger.handlers)
    previous_level = logger.level
    previous_propagate = logger.propagate
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        yield stream
    finally:
        logger.handlers = previous_handlers
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate


def _action_id() -> str:
    return str(uuid.uuid4())


def _intent_submission(
    batch,
    unit,
    candidate,
    *,
    judgment=IntentJudgment.EQUIVALENT,
    reason=None,
    expected=None,
    action_id=None,
    actor=ACTOR,
):
    if reason is None:
        reason = (
            IntentReason.OBVIOUS_TYPO_SAME_INTENT
            if candidate.construction == QueryConstruction.ADJACENT_TRANSPOSITION
            else IntentReason.SAME_PRODUCT_INTENT
        )
    return IntentSubmission(
        oracle_batch_id=batch.oracle_batch_id,
        unit_id=unit.unit_id,
        case_id=candidate.case_id,
        presentation_context_sha256=candidate.intent_context_sha256,
        judgment=judgment,
        reason_code=reason,
        actor=actor,
        client_action_id=action_id or _action_id(),
        expected_previous_annotation_id=expected,
    )


def _behavior_submission(
    batch,
    unit,
    candidate,
    *,
    intent=None,
    judgment=BehaviorJudgment.CONFIRMED_ISSUE,
    reason=None,
    expected=None,
    action_id=None,
    actor=ACTOR,
):
    if reason is None:
        reason = (
            BehaviorReason.OWNER_CATALOG_EXPECTATION
            if candidate.construction == QueryConstruction.IDENTITY
            else BehaviorReason.EQUIVALENT_INTENT_SHOULD_PRESERVE_BEHAVIOR
        )
    return BehaviorSubmission(
        oracle_batch_id=batch.oracle_batch_id,
        unit_id=unit.unit_id,
        case_id=candidate.case_id,
        presentation_context_sha256=candidate.behavior_context_sha256,
        judgment=judgment,
        reason_code=reason,
        intent_annotation_id=(intent.intent_annotation_id if intent else None),
        actor=actor,
        client_action_id=action_id or _action_id(),
        expected_previous_annotation_id=expected,
    )


def _display_hits(observation):
    if observation.returned_at_k == 0:
        return []
    return [
        BadCaseDisplayHit(
            product_id=f"P{observation.source_query_id:05d}{rank:02d}",
            locale="us",
            title=f"Fixture product {observation.source_query_id}",
            rank=rank,
        )
        for rank in range(1, observation.returned_at_k + 1)
    ]


def _samples_for_unit(batch, unit, diagnostic, query_set):
    observations = {item.case_id: item for item in diagnostic.observations}
    diagnostics = {item.case_id: item for item in diagnostic.diagnostics}
    cases = {item.case_id: item for item in query_set.cases}
    samples = []
    for candidate in unit.candidates:
        diagnosed = diagnostics[candidate.case_id]
        source = observations[candidate.source_case_id]
        variant = observations[candidate.case_id]
        samples.append(
            BadCaseSample(
                case_id=candidate.case_id,
                source_case_id=candidate.source_case_id,
                construction=candidate.construction,
                categories=diagnosed.categories,
                reason_code=diagnosed.reason_code,
                query_text=cases[candidate.case_id].query_text,
                source_query_text=cases[candidate.source_case_id].query_text,
                source_returned_at_k=diagnosed.source_returned_at_k,
                variant_returned_at_k=diagnosed.variant_returned_at_k,
                overlap_at_k=diagnosed.overlap_at_k,
                source_top_hits=_display_hits(source),
                variant_top_hits=_display_hits(variant),
            )
        )
    return samples


class _OracleCatalogService:
    def __init__(self, diagnostic, query_set, *, tamper_title=False):
        self.metadata = SimpleNamespace(
            index_id=diagnostic.index_id,
            product_count=diagnostic.catalog_product_count,
            code_revision=diagnostic.index_build_revision,
            source_sha256=diagnostic.index_source_sha256,
            index_config={"fixture": "human-oracle"},
        )
        self.query_set = query_set
        self.diagnostic = diagnostic
        self.tamper_title = tamper_title
        self.search_call_count = 0

    def search_many(self, queries, **kwargs):
        assert kwargs == {
            "top_k": 10,
            "max_elapsed_ms": 30_000,
            "max_query_elapsed_ms": 5_000,
        }
        cases = {item.query_text: item for item in self.query_set.cases}
        observations = {item.case_id: item for item in self.diagnostic.observations}
        results = []
        for query in queries:
            case = cases[query]
            observation = observations[case.case_id]
            hits = ()
            if observation.returned_at_k:
                title = f"Fixture product {case.source.query_id}"
                if self.tamper_title:
                    title = "Tampered title"
                hits = tuple(
                    CatalogSearchHit(
                        product=CatalogProduct(
                            product_id=(f"P{case.source.query_id:05d}{rank:02d}"),
                            locale="us",
                            title=title,
                            brand="",
                            color="",
                        ),
                        score=1.0,
                        rank=rank,
                    )
                    for rank in range(1, observation.returned_at_k + 1)
                )
            results.append(
                CatalogSearchResult(
                    index_id=self.metadata.index_id,
                    product_count=self.metadata.product_count,
                    locale_counts={"us": self.metadata.product_count},
                    hits=hits,
                )
            )
        self.search_call_count += len(queries)
        return tuple(results)


def test_batch_is_complete_cluster_census_without_raw_content(
    batch,
    diagnostic,
    query_set,
) -> None:
    assert batch.population_candidate_count == 40
    assert batch.population_cluster_count == 20
    assert batch.selected_candidate_count == 40
    assert batch.selected_cluster_count == 20
    assert batch.synthetic_intent_candidate_count == 30
    assert batch.stratum_counts == {
        "source_zero_cluster": 10,
        "source_nonzero_variant_zero": 10,
    }
    assert Counter(len(item.candidates) for item in batch.units) == {3: 10, 1: 10}
    assert (
        validate_oracle_batch(
            batch=batch,
            diagnostic=diagnostic,
            query_set=query_set,
        )
        == batch
    )

    serialized = json.dumps(batch.model_dump(mode="json"), sort_keys=True)
    assert query_set.cases[0].query_text not in serialized
    assert "Fixture product" not in serialized
    assert "P0000561" not in serialized
    assert batch.raw_query_text_stored is False
    assert batch.raw_product_content_stored is False
    assert batch.source_labels_inherited is False
    assert batch.product_relevance_labels_created == 0
    assert batch.formal_evaluation_allowed is False
    assert batch.quality_conclusion_allowed is False


def test_batch_strata_are_bound_to_shape_and_storage_rebuilds_trusted_evidence(
    batch,
    diagnostic,
    query_set,
    tmp_path: Path,
) -> None:
    source_zero = next(item for item in batch.units if len(item.candidates) == 3)
    swapped = source_zero.model_dump(mode="json")
    swapped["stratum"] = "source_nonzero_variant_zero"
    swapped["unit_id"] = oracle_unit_id(
        {key: value for key, value in swapped.items() if key != "unit_id"}
    )
    with pytest.raises(ValidationError, match="source-nonzero unit"):
        OracleReviewUnit.model_validate(swapped)

    forged_payload = batch.model_dump(mode="json")
    forged_unit = next(
        item for item in forged_payload["units"] if len(item["candidates"]) == 1
    )
    forged_unit["candidates"][0]["source_observation_sha256"] = "0" * 64
    forged_unit["unit_id"] = oracle_unit_id(
        {key: value for key, value in forged_unit.items() if key != "unit_id"}
    )
    forged_payload["oracle_batch_id"] = oracle_batch_id(
        {
            key: value
            for key, value in forged_payload.items()
            if key != "oracle_batch_id"
        }
    )
    forged = OracleBatchArtifact.model_validate(forged_payload)
    repo = HumanOracleRepository(tmp_path / "runs")
    with pytest.raises(ValueError, match="trusted diagnostic evidence"):
        repo.create_batch(forged, diagnostic=diagnostic, query_set=query_set)
    assert not list((repo.root / "batches").glob("*.json"))


def test_intent_and_behavior_views_are_phase_separated_and_hash_verified(
    batch,
    diagnostic,
    query_set,
    tmp_path: Path,
    oracle_logs,
) -> None:
    repo = HumanOracleRepository(tmp_path / "runs")
    repo.create_batch(batch, diagnostic=diagnostic, query_set=query_set)
    unit = next(item for item in batch.units if len(item.candidates) == 1)
    candidate = unit.candidates[0]

    intent_view = build_intent_view(
        batch=batch,
        query_set=query_set,
        unit_id=unit.unit_id,
    )
    assert intent_view.source_query_text
    assert intent_view.candidates[0].query_text
    assert intent_view.result_evidence_included is False
    assert "top_hits" not in intent_view.model_dump(mode="json")

    with pytest.raises(ValueError, match="active intent"):
        build_behavior_view(
            batch=batch,
            diagnostic=diagnostic,
            query_set=query_set,
            unit_id=unit.unit_id,
            samples=_samples_for_unit(batch, unit, diagnostic, query_set),
            active_intents={},
        )
    intent = repo.submit_intent(_intent_submission(batch, unit, candidate))
    samples = _samples_for_unit(batch, unit, diagnostic, query_set)
    behavior_view = build_behavior_view(
        batch=batch,
        diagnostic=diagnostic,
        query_set=query_set,
        unit_id=unit.unit_id,
        samples=samples,
        active_intents={candidate.case_id: intent},
    )
    assert behavior_view.candidates[0].source_top_hits
    assert behavior_view.candidates[0].variant_top_hits == []
    assert behavior_view.synthetic_product_relevance_labels_included is False
    assert behavior_view.cache_allowed is False

    tampered_payload = samples[0].model_dump(mode="json")
    tampered_payload["source_top_hits"][0]["title"] = "Tampered title"
    tampered = BadCaseSample.model_validate(tampered_payload)
    with pytest.raises(ValueError, match="display hits"):
        build_behavior_view(
            batch=batch,
            diagnostic=diagnostic,
            query_set=query_set,
            unit_id=unit.unit_id,
            samples=[tampered],
            active_intents={candidate.case_id: intent},
        )

    for retained in (0, 2):
        incomplete_payload = samples[0].model_dump(mode="json")
        incomplete_payload["source_top_hits"] = incomplete_payload["source_top_hits"][
            :retained
        ]
        incomplete = BadCaseSample.model_validate(incomplete_payload)
        with pytest.raises(ValueError, match="complete Top-3"):
            build_behavior_view(
                batch=batch,
                diagnostic=diagnostic,
                query_set=query_set,
                unit_id=unit.unit_id,
                samples=[incomplete],
                active_intents={candidate.case_id: intent},
            )
    view_logs = oracle_logs.getvalue()
    assert view_logs.count('"event":"human_oracle_view_failed"') == 4
    assert view_logs.count('"error_code":"view_context_or_evidence_invalid"') == 4
    assert samples[0].query_text not in view_logs
    assert samples[0].source_top_hits[0].title not in view_logs


def test_behavior_view_candidate_contract_requires_exact_unique_top3(
    batch,
    diagnostic,
    query_set,
    tmp_path: Path,
) -> None:
    repo = HumanOracleRepository(tmp_path / "runs")
    repo.create_batch(batch, diagnostic=diagnostic, query_set=query_set)
    unit = next(item for item in batch.units if len(item.candidates) == 1)
    candidate = unit.candidates[0]
    intent = repo.submit_intent(_intent_submission(batch, unit, candidate))
    view = build_behavior_view(
        batch=batch,
        diagnostic=diagnostic,
        query_set=query_set,
        unit_id=unit.unit_id,
        samples=_samples_for_unit(batch, unit, diagnostic, query_set),
        active_intents={candidate.case_id: intent},
    )
    payload = view.candidates[0].model_dump(mode="json")

    incomplete = {**payload, "source_top_hits": payload["source_top_hits"][:-1]}
    with pytest.raises(ValidationError, match="complete Top-3"):
        OracleBehaviorViewCandidate.model_validate(incomplete)

    non_contiguous = json.loads(json.dumps(payload))
    non_contiguous["source_top_hits"][1]["rank"] = 3
    with pytest.raises(ValidationError, match="ranks must be contiguous"):
        OracleBehaviorViewCandidate.model_validate(non_contiguous)

    duplicate = json.loads(json.dumps(payload))
    duplicate["source_top_hits"][1]["product_id"] = duplicate["source_top_hits"][0][
        "product_id"
    ]
    with pytest.raises(ValidationError, match="product keys must be unique"):
        OracleBehaviorViewCandidate.model_validate(duplicate)


def test_unit_collector_reruns_only_cluster_and_rejects_stale_evidence(
    batch,
    diagnostic,
    query_set,
    oracle_logs,
) -> None:
    unit = next(item for item in batch.units if len(item.candidates) == 1)
    service = _OracleCatalogService(diagnostic, query_set)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger = logging.getLogger("search_quality.human_oracle")
    previous_handlers = list(logger.handlers)
    previous_level = logger.level
    previous_propagate = logger.propagate
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        samples = collect_behavior_samples_for_unit(
            batch=batch,
            diagnostic=diagnostic,
            query_set=query_set,
            unit_id=unit.unit_id,
            search_service=service,
        )
    finally:
        logger.handlers = previous_handlers
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate
    assert service.search_call_count == 2
    assert len(samples) == 1
    assert samples[0].source_top_hits
    assert samples[0].variant_top_hits == []
    logs = stream.getvalue()
    assert samples[0].query_text not in logs
    assert samples[0].source_query_text not in logs
    assert samples[0].source_top_hits[0].product_id not in logs
    assert samples[0].source_top_hits[0].title not in logs
    assert "human_oracle_behavior_collection_completed" in logs

    stale_index = _OracleCatalogService(diagnostic, query_set)
    stale_index.metadata.index_id = "catalog-baseline-v1-bbbbbbbbbbbb"
    with pytest.raises(RuntimeError, match="catalog identity"):
        collect_behavior_samples_for_unit(
            batch=batch,
            diagnostic=diagnostic,
            query_set=query_set,
            unit_id=unit.unit_id,
            search_service=stale_index,
        )
    assert stale_index.search_call_count == 0

    stale_result = _OracleCatalogService(
        diagnostic,
        query_set,
        tamper_title=True,
    )
    with pytest.raises(RuntimeError, match="does not match"):
        collect_behavior_samples_for_unit(
            batch=batch,
            diagnostic=diagnostic,
            query_set=query_set,
            unit_id=unit.unit_id,
            search_service=stale_result,
        )
    assert stale_result.search_call_count == 2
    failure_logs = oracle_logs.getvalue()
    assert failure_logs.count('"event":"human_oracle_behavior_collection_failed"') == 2
    assert (
        failure_logs.count('"error_code":"evidence_collection_or_validation_failed"')
        == 2
    )
    assert samples[0].source_query_text not in failure_logs
    assert samples[0].source_top_hits[0].title not in failure_logs


def test_append_only_idempotency_cas_and_intent_invalidation(
    batch,
    diagnostic,
    query_set,
    tmp_path: Path,
) -> None:
    repo = HumanOracleRepository(tmp_path / "runs")
    repo.create_batch(batch, diagnostic=diagnostic, query_set=query_set)
    unit = next(item for item in batch.units if len(item.candidates) == 1)
    candidate = unit.candidates[0]
    first_request = _intent_submission(batch, unit, candidate)
    first = repo.submit_intent(first_request)
    assert repo.submit_intent(first_request) == first

    conflicting = _intent_submission(
        batch,
        unit,
        candidate,
        judgment=IntentJudgment.UNCERTAIN,
        reason=IntentReason.AMBIGUOUS_INTENT,
        action_id=first_request.client_action_id,
    )
    with pytest.raises(OracleClientActionConflict):
        repo.submit_intent(conflicting)
    with pytest.raises(OracleCompareAndSwapConflict):
        repo.submit_intent(_intent_submission(batch, unit, candidate))

    behavior = repo.submit_behavior(
        _behavior_submission(batch, unit, candidate, intent=first)
    )
    assert repo.project(batch.oracle_batch_id).active_behavior_annotation_count == 1

    replacement = repo.submit_intent(
        _intent_submission(
            batch,
            unit,
            candidate,
            judgment=IntentJudgment.NOT_EQUIVALENT,
            reason=IntentReason.MEANING_CHANGED,
            expected=first.intent_annotation_id,
        )
    )
    projected = repo.project(batch.oracle_batch_id)
    assert projected.active_behavior_annotation_count == 0
    assert projected.invalidated_behavior_annotation_count == 1
    state = repo.review_state(batch.oracle_batch_id)
    case_state = next(item for item in state.cases if item.case_id == candidate.case_id)
    assert case_state.active_intent_annotation_id == replacement.intent_annotation_id
    assert case_state.expected_previous_behavior_annotation_id == (
        behavior.behavior_annotation_id
    )
    assert case_state.active_behavior_annotation_id is None
    assert case_state.behavior_invalidated_by_intent_change is True
    assert (
        repo.behavior_head_for_case(
            batch.oracle_batch_id,
            candidate.case_id,
        )
        == behavior
    )
    assert (
        repo.active_behavior_for_case(
            batch.oracle_batch_id,
            candidate.case_id,
        )
        is None
    )

    with pytest.raises(OracleInvalidDecision, match="cannot confirm"):
        repo.submit_behavior(
            _behavior_submission(
                batch,
                unit,
                candidate,
                intent=replacement,
                expected=behavior.behavior_annotation_id,
            )
        )
    replacement_behavior = repo.submit_behavior(
        _behavior_submission(
            batch,
            unit,
            candidate,
            intent=replacement,
            judgment=BehaviorJudgment.ACCEPTABLE,
            reason=BehaviorReason.INTENT_NOT_EQUIVALENT,
            expected=behavior.behavior_annotation_id,
        )
    )
    assert (
        replacement_behavior.supersedes_annotation_id == behavior.behavior_annotation_id
    )
    assert repo.project(batch.oracle_batch_id).active_behavior_annotation_count == 1
    assert (
        repo.active_behavior_for_case(
            batch.oracle_batch_id,
            candidate.case_id,
        )
        == replacement_behavior
    )
    assert len(list((repo.root / "intent-annotations").glob("*.json"))) == 2
    assert len(list((repo.root / "behavior-annotations").glob("*.json"))) == 2


def test_uncertain_intent_cannot_become_non_uncertain_behavior(
    batch,
    diagnostic,
    query_set,
    tmp_path: Path,
) -> None:
    repo = HumanOracleRepository(tmp_path / "runs")
    repo.create_batch(batch, diagnostic=diagnostic, query_set=query_set)
    unit = next(item for item in batch.units if len(item.candidates) == 1)
    candidate = unit.candidates[0]
    intent = repo.submit_intent(
        _intent_submission(
            batch,
            unit,
            candidate,
            judgment=IntentJudgment.UNCERTAIN,
            reason=IntentReason.INSUFFICIENT_CONTEXT,
        )
    )
    with pytest.raises(OracleInvalidDecision, match="uncertain intent"):
        repo.submit_behavior(
            _behavior_submission(
                batch,
                unit,
                candidate,
                intent=intent,
                judgment=BehaviorJudgment.ACCEPTABLE,
                reason=BehaviorReason.BEHAVIOR_IS_EXPECTED,
            )
        )
    behavior = repo.submit_behavior(
        _behavior_submission(
            batch,
            unit,
            candidate,
            intent=intent,
            judgment=BehaviorJudgment.UNCERTAIN,
            reason=BehaviorReason.INSUFFICIENT_RESULT_EVIDENCE,
        )
    )
    assert behavior.judgment == BehaviorJudgment.UNCERTAIN


def test_behavior_matrix_owner_binding_and_public_cas_state(
    batch,
    diagnostic,
    query_set,
    tmp_path: Path,
) -> None:
    repo = HumanOracleRepository(tmp_path / "runs")
    repo.create_batch(batch, diagnostic=diagnostic, query_set=query_set)
    unit = next(item for item in batch.units if len(item.candidates) == 1)
    candidate = unit.candidates[0]
    intent = repo.submit_intent(_intent_submission(batch, unit, candidate))

    with pytest.raises(OracleInvalidDecision, match="contradict"):
        repo.submit_behavior(
            _behavior_submission(
                batch,
                unit,
                candidate,
                intent=intent,
                judgment=BehaviorJudgment.ACCEPTABLE,
                reason=BehaviorReason.INTENT_NOT_EQUIVALENT,
            )
        )

    identity_unit = next(item for item in batch.units if len(item.candidates) == 3)
    identity = identity_unit.candidates[0]
    with pytest.raises(OracleInvalidDecision, match="contradict"):
        repo.submit_behavior(
            _behavior_submission(
                batch,
                identity_unit,
                identity,
                judgment=BehaviorJudgment.ACCEPTABLE,
                reason=BehaviorReason.INTENT_NOT_EQUIVALENT,
            )
        )

    other_actor = OracleActor(
        principal_hmac_sha256="e" * 64,
        actor_key_id="oracle-actor-key-v2",
    )
    other_unit = next(
        item
        for item in batch.units
        if len(item.candidates) == 1 and item.unit_id != unit.unit_id
    )
    with pytest.raises(OracleInvalidDecision, match="different owner"):
        repo.submit_intent(
            _intent_submission(
                batch,
                other_unit,
                other_unit.candidates[0],
                actor=other_actor,
            )
        )

    state = repo.review_state(batch.oracle_batch_id)
    case_state = next(item for item in state.cases if item.case_id == candidate.case_id)
    assert case_state.expected_previous_intent_annotation_id == (
        intent.intent_annotation_id
    )
    assert case_state.expected_previous_behavior_annotation_id is None


def test_complete_batch_seals_once_and_remains_non_formal(
    batch,
    diagnostic,
    query_set,
    tmp_path: Path,
) -> None:
    repo = HumanOracleRepository(tmp_path / "runs")
    repo.create_batch(batch, diagnostic=diagnostic, query_set=query_set)
    with pytest.raises(OracleBatchIncomplete):
        repo.seal(
            SealSubmission(
                oracle_batch_id=batch.oracle_batch_id,
                actor=ACTOR,
                client_action_id=_action_id(),
            )
        )

    for unit in batch.units:
        for candidate in unit.candidates:
            intent = None
            if candidate.construction != QueryConstruction.IDENTITY:
                intent = repo.submit_intent(_intent_submission(batch, unit, candidate))
            repo.submit_behavior(
                _behavior_submission(batch, unit, candidate, intent=intent)
            )
    assert repo.project(batch.oracle_batch_id).status == OracleBatchStatus.READY_TO_SEAL
    other_actor = OracleActor(
        principal_hmac_sha256="e" * 64,
        actor_key_id="oracle-actor-key-v2",
    )
    with pytest.raises(OracleInvalidDecision, match="different owner"):
        repo.seal(
            SealSubmission(
                oracle_batch_id=batch.oracle_batch_id,
                actor=other_actor,
                client_action_id=_action_id(),
            )
        )
    seal_request = SealSubmission(
        oracle_batch_id=batch.oracle_batch_id,
        actor=ACTOR,
        client_action_id=_action_id(),
    )
    oracle = repo.seal(seal_request)
    assert repo.seal(seal_request) == oracle
    assert repo.project(batch.oracle_batch_id).status == OracleBatchStatus.SEALED
    sealed_state = repo.review_state(batch.oracle_batch_id)
    assert sealed_state.projection.status == OracleBatchStatus.SEALED
    assert len(sealed_state.cases) == 40
    assert oracle.synthetic_intent_annotation_count == 30
    assert oracle.behavior_annotation_count == 40
    assert oracle.intent_counts.equivalent == 30
    assert oracle.behavior_counts.confirmed_issue == 40
    assert oracle.synthetic_label_inheritance_count == 0
    assert oracle.product_relevance_labels_created == 0
    assert oracle.formal_evaluation_allowed is False
    assert oracle.quality_conclusion_allowed is False
    assert oracle.mechanism_smoke_only is True
    assert oracle.root_cause_claimed is False
    assert oracle.strategy_write_count == 0

    identity_unit = next(
        unit
        for unit in batch.units
        if any(
            candidate.construction == QueryConstruction.IDENTITY
            for candidate in unit.candidates
        )
    )
    identity = next(
        candidate
        for candidate in identity_unit.candidates
        if candidate.construction == QueryConstruction.IDENTITY
    )
    with pytest.raises(OracleBatchSealed):
        repo.submit_behavior(_behavior_submission(batch, identity_unit, identity))

    assert stat.S_IMODE(repo.root.stat().st_mode) == 0o700
    paths = list(repo.root.rglob("*.json"))
    assert paths
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in paths)
    persisted = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    assert "Fixture product" not in persisted
    assert "P0000561" not in persisted


def test_concurrent_first_writers_use_compare_and_swap(
    batch,
    diagnostic,
    query_set,
    tmp_path: Path,
) -> None:
    repo = HumanOracleRepository(tmp_path / "runs")
    repo.create_batch(batch, diagnostic=diagnostic, query_set=query_set)
    unit = next(
        unit
        for unit in batch.units
        if any(
            candidate.construction == QueryConstruction.IDENTITY
            for candidate in unit.candidates
        )
    )
    candidate = next(
        item
        for item in unit.candidates
        if item.construction == QueryConstruction.IDENTITY
    )
    submissions = [
        _behavior_submission(batch, unit, candidate),
        _behavior_submission(batch, unit, candidate),
    ]

    def submit(item):
        try:
            return repo.submit_behavior(item)
        except Exception as exc:  # surfaced for exact assertion below
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(submit, submissions))
    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    assert sum(isinstance(item, OracleCompareAndSwapConflict) for item in outcomes) == 1
    assert len(list((repo.root / "behavior-annotations").glob("*.json"))) == 1


def test_path_capacity_privacy_and_invalid_write_fail_closed(
    batch,
    diagnostic,
    query_set,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    oracle_logs,
) -> None:
    with pytest.raises(ValueError, match="absolute"):
        HumanOracleRepository("relative-runs")

    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        HumanOracleRepository(linked / "runs")

    repo = HumanOracleRepository(tmp_path / "runs")
    monkeypatch.setattr(oracle_storage, "MAX_ORACLE_STORE_BYTES", 0)
    with pytest.raises(OracleStorageError, match="size limit"):
        repo.create_batch(batch, diagnostic=diagnostic, query_set=query_set)
    assert list((repo.root / "batches").glob("*.json")) == []
    monkeypatch.setattr(oracle_storage, "MAX_ORACLE_STORE_BYTES", 64 * 1024 * 1024)
    repo.create_batch(batch, diagnostic=diagnostic, query_set=query_set)

    batch_path = repo.root / "batches" / f"{batch.oracle_batch_id}.json"
    for untrusted_id in (
        "../outside/oracle-batch-aaaaaaaaaaaa",
        str(batch_path.with_suffix("")),
    ):
        with pytest.raises(OracleStorageError, match="artifact ID"):
            repo.load_batch(untrusted_id)
        with pytest.raises(OracleStorageError, match="artifact ID"):
            repo.project(untrusted_id)
    path_logs = oracle_logs.getvalue()
    assert '"event":"human_oracle_operation_failed"' in path_logs
    assert '"operation":"project"' in path_logs

    unit = next(item for item in batch.units if len(item.candidates) == 1)
    candidate = unit.candidates[0]
    invalid_payload = _intent_submission(batch, unit, candidate).model_dump(mode="json")
    invalid_payload["presentation_context_sha256"] = "0" * 64
    with pytest.raises(OracleInvalidDecision, match="context"):
        repo.submit_intent(IntentSubmission.model_validate(invalid_payload))
    assert list((repo.root / "intent-annotations").glob("*.json")) == []

    with pytest.raises(ValidationError, match="Extra inputs"):
        IntentSubmission.model_validate(
            {
                **_intent_submission(batch, unit, candidate).model_dump(mode="json"),
                "free_text_note": "must never be stored",
            }
        )

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger = logging.getLogger("search_quality.human_oracle")
    previous_handlers = list(logger.handlers)
    previous_level = logger.level
    previous_propagate = logger.propagate
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        repo.submit_intent(_intent_submission(batch, unit, candidate))
        with pytest.raises(OracleCompareAndSwapConflict):
            repo.submit_intent(_intent_submission(batch, unit, candidate))
    finally:
        logger.handlers = previous_handlers
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate
    logs = stream.getvalue()
    assert query_set.cases[0].query_text not in logs
    assert "Fixture product" not in logs
    assert ACTOR.principal_hmac_sha256 not in logs
    assert "obvious_typo_same_intent" not in logs
    assert "human_oracle_intent_stored" in logs
    assert "human_oracle_operation_failed" in logs
    assert "compare_and_swap_conflict" in logs

    original = batch_path.read_text(encoding="utf-8")
    batch_path.write_text('{"tampered":true}\n', encoding="utf-8")
    with pytest.raises((RuntimeError, OracleStorageError)):
        repo.create_batch(batch, diagnostic=diagnostic, query_set=query_set)
    assert batch_path.read_text(encoding="utf-8") != original
