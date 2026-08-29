from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from search_quality.bad_cases import runner as bad_runner
from search_quality.catalog import (
    CatalogProduct,
    CatalogSearchHit,
    CatalogSearchResult,
)
from search_quality.diagnostic_experiments import (
    QualityEvidenceStatus,
    StrategySpec,
    generate_query_routes,
    load_resolved_diagnostic_evidence,
    resolve_diagnostic_evidence,
    route_diagnostic_evidence,
    zero_result_backoff_strategy,
)
from search_quality.query_constructor import build_smoke_query_set
from search_quality.query_constructor.contracts import QueryConstruction

ROOT = Path(__file__).resolve().parents[1]
REVISION = "a" * 40


class _FakeCatalogService:
    def __init__(
        self,
        *,
        index_path: Path,
        results_by_query: dict[str, CatalogSearchResult],
    ) -> None:
        index_path.write_bytes(b"immutable-diagnostic-experiment-index")
        self.index_path = index_path
        self.results_by_query = results_by_query
        self.metadata = SimpleNamespace(
            index_id="catalog-baseline-v1-aaaaaaaaaaaa",
            product_count=100,
            locale_counts={"us": 100},
            code_revision="b" * 40,
            source_sha256="c" * 64,
            index_config={"backend": "sqlite-fts5", "test": True},
        )

    def search_many(self, queries, **kwargs):
        assert kwargs == {
            "top_k": 10,
            "max_elapsed_ms": 120_000,
            "max_query_elapsed_ms": 5_000,
        }
        return tuple(self.results_by_query[query] for query in queries)


@pytest.fixture(scope="module")
def query_set():
    return build_smoke_query_set(
        project_root=ROOT,
        revision_provider=lambda _root: REVISION,
    )


def _result(
    service: _FakeCatalogService,
    *,
    source_query_id: int,
) -> CatalogSearchResult:
    hits = tuple(
        CatalogSearchHit(
            product=CatalogProduct(
                product_id=f"P{source_query_id:07d}{suffix}",
                locale="us",
                title=f"Private result {source_query_id} {suffix}",
                brand="",
                color="",
            ),
            score=float(3 - rank),
            rank=rank,
        )
        for rank, suffix in enumerate(("A", "B"), start=1)
    )
    return CatalogSearchResult(
        index_id=service.metadata.index_id,
        product_count=service.metadata.product_count,
        locale_counts=dict(service.metadata.locale_counts),
        hits=hits,
    )


def _empty_result(service: _FakeCatalogService) -> CatalogSearchResult:
    return CatalogSearchResult(
        index_id=service.metadata.index_id,
        product_count=service.metadata.product_count,
        locale_counts=dict(service.metadata.locale_counts),
        hits=(),
    )


def _diagnostic_run(
    *,
    query_set,
    tmp_path: Path,
    identity_zero_count: int,
    spelling_sensitive_count: int,
):
    service = _FakeCatalogService(
        index_path=tmp_path / "catalog.sqlite3",
        results_by_query={},
    )
    identity_ids = sorted(
        case.source.query_id
        for case in query_set.cases
        if case.construction == QueryConstruction.IDENTITY
    )
    reversal_source_ids = {
        case.source.query_id
        for case in query_set.cases
        if case.construction == QueryConstruction.TOKEN_ORDER_REVERSAL
    }
    identity_zero_candidates = [
        query_id for query_id in identity_ids if query_id in reversal_source_ids
    ]
    identity_zero_ids = set(identity_zero_candidates[:identity_zero_count])
    remaining_identity_ids = [
        query_id for query_id in identity_ids if query_id not in identity_zero_ids
    ]
    spelling_source_ids = set(remaining_identity_ids[:spelling_sensitive_count])
    for case in query_set.cases:
        query_id = case.source.query_id
        empty = query_id in identity_zero_ids or (
            case.construction == QueryConstruction.ADJACENT_TRANSPOSITION
            and query_id in spelling_source_ids
        )
        service.results_by_query[case.query_text] = (
            _empty_result(service)
            if empty
            else _result(service, source_query_id=query_id)
        )
    return bad_runner.run_bad_case_diagnostics(
        project_root=ROOT,
        artifact_root=tmp_path / "runs",
        revision_provider=lambda _root: REVISION,
        search_service=service,
    )


def test_existing_evidence_shape_prioritizes_zero_result_backoff(
    query_set,
    tmp_path: Path,
) -> None:
    run = _diagnostic_run(
        query_set=query_set,
        tmp_path=tmp_path,
        identity_zero_count=10,
        spelling_sensitive_count=10,
    )
    evidence = resolve_diagnostic_evidence(
        artifact=run.artifact,
        query_set=query_set,
    )

    assert evidence.identity_zero_result_count == 10
    assert evidence.spelling_sensitive_count == 10
    assert evidence.diagnostic_candidate_count == 40

    plan = route_diagnostic_evidence(evidence)

    assert plan.status == "experiment_planned"
    assert plan.reason_code == "identity_zero_result_backoff_prioritized"
    assert plan.strategy is not None
    assert plan.strategy.strategy_id == "zero-result-drop-one-token-backoff-v1"
    assert plan.behavioral_lane.query_count == 59
    assert plan.behavioral_lane.relevance_labels_used is False
    assert plan.behavioral_lane.quality_metrics_allowed is False
    assert plan.quality_lane.evidence_status == QualityEvidenceStatus.BEHAVIOR_ONLY
    assert plan.quality_conclusion_allowed is False
    assert plan.activation_eligible is False
    assert plan.strategy_write_count == 0
    assert len(plan.target_case_ids) == 10

    repeated = route_diagnostic_evidence(evidence)
    assert repeated == plan
    assert repeated.experiment_plan_id == plan.experiment_plan_id


def test_spelling_only_without_oracle_stops_for_independent_judgment(
    query_set,
    tmp_path: Path,
) -> None:
    run = _diagnostic_run(
        query_set=query_set,
        tmp_path=tmp_path,
        identity_zero_count=0,
        spelling_sensitive_count=10,
    )
    evidence = resolve_diagnostic_evidence(
        artifact=run.artifact,
        query_set=query_set,
    )

    assert evidence.identity_zero_result_count == 0
    assert evidence.spelling_sensitive_count == 10

    plan = route_diagnostic_evidence(evidence)

    assert plan.status == "requires_oracle"
    assert plan.reason_code == "spelling_sensitive_requires_independent_oracle"
    assert plan.recommended_next_action == "create_independent_relevance_oracle"
    assert plan.strategy is None
    assert plan.quality_conclusion_allowed is False
    assert plan.activation_eligible is False
    assert plan.strategy_write_count == 0

    with_oracle = route_diagnostic_evidence(
        evidence,
        quality_evidence_status=QualityEvidenceStatus.INDEPENDENT_ORACLE,
        oracle_id="oracle-aaaaaaaaaaaa",
    )
    assert with_oracle.status == "requires_engineering"
    assert with_oracle.reason_code == "spelling_correction_requires_engineering"
    assert with_oracle.strategy is None
    assert with_oracle.quality_conclusion_allowed is False


def test_resolver_rejects_artifact_query_set_and_label_inheritance_tampering(
    query_set,
    tmp_path: Path,
) -> None:
    run = _diagnostic_run(
        query_set=query_set,
        tmp_path=tmp_path,
        identity_zero_count=10,
        spelling_sensitive_count=10,
    )
    tampered_artifact = run.artifact.model_copy(
        update={"diagnostic_id": "bad-case-000000000000"}
    )
    with pytest.raises(ValidationError):
        resolve_diagnostic_evidence(
            artifact=tampered_artifact,
            query_set=query_set,
        )

    tampered_query_set = query_set.model_copy(
        update={"query_set_id": "query-set-000000000000"}
    )
    with pytest.raises(ValidationError):
        resolve_diagnostic_evidence(
            artifact=run.artifact,
            query_set=tampered_query_set,
        )

    cases = list(query_set.cases)
    synthetic_index = next(
        index
        for index, case in enumerate(cases)
        if case.construction != QueryConstruction.IDENTITY
    )
    cases[synthetic_index] = cases[synthetic_index].model_copy(
        update={"synthetic_labels_inherited": True}
    )
    inherited_labels = query_set.model_copy(update={"cases": cases})
    with pytest.raises(ValidationError):
        resolve_diagnostic_evidence(
            artifact=run.artifact,
            query_set=inherited_labels,
        )

    ineligible_artifact = run.artifact.model_copy(update={"strategy_write_count": 1})
    with pytest.raises(ValidationError):
        resolve_diagnostic_evidence(
            artifact=ineligible_artifact,
            query_set=query_set,
        )


def test_confined_loader_accepts_only_fixed_safe_id_artifacts(
    query_set,
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "runs"
    run = _diagnostic_run(
        query_set=query_set,
        tmp_path=tmp_path,
        identity_zero_count=10,
        spelling_sensitive_count=10,
    )

    evidence = load_resolved_diagnostic_evidence(
        artifact_root=run_root,
        diagnostic_id=run.artifact.diagnostic_id,
        query_set_id=query_set.query_set_id,
    )

    assert evidence.diagnostic_id == run.artifact.diagnostic_id
    assert evidence.query_set_id == query_set.query_set_id
    with pytest.raises(ValueError, match="diagnostic_id has an invalid format"):
        load_resolved_diagnostic_evidence(
            artifact_root=run_root,
            diagnostic_id="../latest",
            query_set_id=query_set.query_set_id,
        )


def test_confined_loader_rejects_id_mismatch_symlink_non_file_and_oversize(
    query_set,
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "runs"
    run = _diagnostic_run(
        query_set=query_set,
        tmp_path=tmp_path,
        identity_zero_count=10,
        spelling_sensitive_count=10,
    )
    evidence_dir = run_root / "bad-case-diagnostics" / "evidence"
    real_path = evidence_dir / f"{run.artifact.diagnostic_id}.json"

    mismatched_id = "bad-case-000000000000"
    (evidence_dir / f"{mismatched_id}.json").write_bytes(real_path.read_bytes())
    with pytest.raises(ValueError, match="does not match its filename"):
        load_resolved_diagnostic_evidence(
            artifact_root=run_root,
            diagnostic_id=mismatched_id,
            query_set_id=query_set.query_set_id,
        )

    query_set_dir = run_root / "query-sets"
    mismatched_query_set_id = "query-set-000000000000"
    (query_set_dir / f"{mismatched_query_set_id}.json").write_bytes(
        (query_set_dir / f"{query_set.query_set_id}.json").read_bytes()
    )
    with pytest.raises(ValueError, match="does not match its filename"):
        load_resolved_diagnostic_evidence(
            artifact_root=run_root,
            diagnostic_id=run.artifact.diagnostic_id,
            query_set_id=mismatched_query_set_id,
        )

    symlink_id = "bad-case-111111111111"
    (evidence_dir / f"{symlink_id}.json").symlink_to(real_path)
    with pytest.raises(ValueError, match="symbolic link"):
        load_resolved_diagnostic_evidence(
            artifact_root=run_root,
            diagnostic_id=symlink_id,
            query_set_id=query_set.query_set_id,
        )

    directory_id = "bad-case-222222222222"
    (evidence_dir / f"{directory_id}.json").mkdir()
    with pytest.raises(ValueError, match="regular file"):
        load_resolved_diagnostic_evidence(
            artifact_root=run_root,
            diagnostic_id=directory_id,
            query_set_id=query_set.query_set_id,
        )

    oversize_id = "bad-case-333333333333"
    (evidence_dir / f"{oversize_id}.json").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="size limit"):
        load_resolved_diagnostic_evidence(
            artifact_root=run_root,
            diagnostic_id=oversize_id,
            query_set_id=query_set.query_set_id,
        )


def test_strategy_dsl_is_exact_and_rejects_unknown_fields_or_strategies() -> None:
    strategy = zero_result_backoff_strategy()
    payload = strategy.model_dump(mode="json")

    with pytest.raises(ValidationError):
        StrategySpec.model_validate({**payload, "shell_command": "private"})
    with pytest.raises(ValidationError):
        StrategySpec.model_validate({**payload, "strategy_id": "unknown-v1"})
    with pytest.raises(ValidationError):
        StrategySpec.model_validate(
            {**payload, "strategy_spec_id": "strategy-spec-000000000000"}
        )


def test_query_routes_are_zero_result_only_protected_bounded_and_deterministic() -> (
    None
):
    strategy = zero_result_backoff_strategy()
    query = "stock remington 700 long action x1000 gray"

    no_fallback = generate_query_routes(
        query,
        strategy=strategy,
        primary_returned_at_k=3,
        model_tokens=("action",),
        product_id_tokens=("remington",),
    )
    assert no_fallback.fallback_triggered is False
    assert no_fallback.fallback_routes == ()

    plan = generate_query_routes(
        query,
        strategy=strategy,
        primary_returned_at_k=0,
        model_tokens=("action",),
        product_id_tokens=("remington",),
    )
    repeated = generate_query_routes(
        query,
        strategy=strategy,
        primary_returned_at_k=0,
        model_tokens=("action",),
        product_id_tokens=("remington",),
    )

    assert plan == repeated
    assert plan.route_plan_id == repeated.route_plan_id
    assert plan.primary.tokens == (
        "stock",
        "remington",
        "700",
        "long",
        "action",
        "x1000",
        "gray",
    )
    assert len(plan.fallback_routes) == 3
    for route in plan.fallback_routes:
        assert "remington" in route.tokens
        assert "700" in route.tokens
        assert "action" in route.tokens
        assert "x1000" in route.tokens
        assert len(route.tokens) == len(plan.primary.tokens) - 1
    assert [route.tokens for route in plan.fallback_routes] == [
        ("remington", "700", "long", "action", "x1000", "gray"),
        ("stock", "remington", "700", "action", "x1000", "gray"),
        ("stock", "remington", "700", "long", "action", "x1000"),
    ]
    assert len(plan.fallback_routes) <= strategy.max_fallback_routes

    with pytest.raises(ValueError):
        generate_query_routes(
            query,
            strategy=strategy,
            primary_returned_at_k=0,
            product_id_tokens=("absent-id",),
        )
    with pytest.raises(TypeError):
        generate_query_routes(
            query,
            strategy=strategy,
            primary_returned_at_k=0,
            product_id_tokens=["remington"],  # type: ignore[arg-type]
        )


def test_diagnostic_experiment_logs_are_module_scoped_and_omit_raw_content(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_query = "private unreleased product 700 x1000"
    module_logger = logging.getLogger("search_quality.diagnostic_experiments")
    monkeypatch.setattr(
        module_logger,
        "disabled",
        False,
    )
    module_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(
            logging.INFO,
            logger="search_quality.diagnostic_experiments",
        ):
            plan = generate_query_routes(
                raw_query,
                strategy=zero_result_backoff_strategy(),
                primary_returned_at_k=0,
                product_id_tokens=(),
            )
    finally:
        module_logger.removeHandler(caplog.handler)

    matching = [
        record
        for record in caplog.records
        if record.name == "search_quality.diagnostic_experiments"
    ]
    assert matching
    assert plan.route_plan_id in {
        getattr(record, "query_route_plan_id", None) for record in matching
    }
    serialized_records = repr([record.__dict__ for record in matching])
    assert raw_query not in serialized_records
    assert "unreleased" not in serialized_records
    assert "x1000" not in serialized_records
