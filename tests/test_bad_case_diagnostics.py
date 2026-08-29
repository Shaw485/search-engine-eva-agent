from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from search_quality.bad_cases import artifacts as bad_artifacts
from search_quality.bad_cases import runner as bad_runner
from search_quality.bad_cases.artifacts import (
    BadCaseRunInProgress,
    bad_case_run_lock,
    trusted_bad_case_root,
)
from search_quality.bad_cases.contracts import (
    BadCaseDiagnosticArtifact,
    BadCaseRun,
    diagnostic_id,
    display_hit_sha256,
    ordered_results_sha256,
    result_set_sha256,
)
from search_quality.catalog import (
    CatalogProduct,
    CatalogSearchDeadlineExceeded,
    CatalogSearchHit,
    CatalogSearchResult,
)
from search_quality.observability import configure_logging
from search_quality.query_constructor import builder as query_builder
from search_quality.query_constructor.contracts import QueryConstruction

ROOT = Path(__file__).resolve().parents[1]
REVISION = "a" * 40


class FakeCatalogService:
    def __init__(self, *, index_path: Path, results_by_query: dict[str, object]):
        index_path.write_bytes(b"immutable-test-index")
        self.index_path = index_path
        self.results_by_query = results_by_query
        self.search_call_count = 0
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
        self.search_call_count += len(queries)
        return tuple(self.results_by_query[query] for query in queries)


class FailingCatalogService(FakeCatalogService):
    def search_many(self, queries, **kwargs):
        self.search_call_count = 0
        raise CatalogSearchDeadlineExceeded("private Query timed out")


@pytest.fixture
def query_set():
    return query_builder.build_smoke_query_set(
        project_root=ROOT,
        revision_provider=lambda _root: REVISION,
    )


def _result(service: FakeCatalogService, product_ids: list[str]):
    hits = tuple(
        CatalogSearchHit(
            product=CatalogProduct(
                product_id=product_id,
                locale="us",
                title=f"Readable product {product_id}",
                brand="",
                color="",
            ),
            score=float(len(product_ids) - rank + 1),
            rank=rank,
        )
        for rank, product_id in enumerate(product_ids, start=1)
    )
    return CatalogSearchResult(
        index_id=service.metadata.index_id,
        product_count=service.metadata.product_count,
        locale_counts=dict(service.metadata.locale_counts),
        hits=hits,
    )


def _service_for_query_set(query_set, tmp_path: Path) -> FakeCatalogService:
    service = FakeCatalogService(
        index_path=tmp_path / "catalog.sqlite3",
        results_by_query={},
    )
    identities = {
        case.source.query_id: case
        for case in query_set.cases
        if case.construction == QueryConstruction.IDENTITY
    }
    query_ids = sorted(identities)
    for case in query_set.cases:
        source_ids = [
            f"P{case.source.query_id:07d}A",
            f"P{case.source.query_id:07d}B",
        ]
        product_ids = source_ids
        if (
            case.construction == QueryConstruction.ADJACENT_TRANSPOSITION
            and case.source.query_id == query_ids[0]
        ):
            product_ids = []
        elif (
            case.construction == QueryConstruction.ADJACENT_TRANSPOSITION
            and case.source.query_id == query_ids[1]
        ):
            product_ids = list(reversed(source_ids))
        elif (
            case.construction == QueryConstruction.TOKEN_ORDER_REVERSAL
            and case.source.query_id == query_ids[0]
        ):
            product_ids = list(reversed(source_ids))
        service.results_by_query[case.query_text] = _result(service, product_ids)
    return service


def _patch_builder(monkeypatch: pytest.MonkeyPatch, query_set) -> None:
    def build(**kwargs):
        assert kwargs["source_profile"] == "smoke"
        assert kwargs["revision_provider"](ROOT) == REVISION
        kwargs["profile_access_recorder"]("smoke")
        return query_set

    monkeypatch.setattr(bad_runner, "build_smoke_query_set", build)


def test_fixed_batch_builds_replayable_label_blind_evidence(
    query_set,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service_for_query_set(query_set, tmp_path)
    _patch_builder(monkeypatch, query_set)

    run = bad_runner.run_bad_case_diagnostics(
        project_root=ROOT,
        artifact_root=tmp_path / "runs",
        revision_provider=lambda _root: REVISION,
        search_service=service,
    )

    artifact = run.artifact
    assert service.search_call_count == 59
    assert artifact.query_count == 59
    assert artifact.construction_counts == {
        "identity": 20,
        "adjacent_transposition": 20,
        "token_order_reversal": 19,
    }
    assert artifact.category_counts.zero_result == 1
    assert artifact.category_counts.spelling_sensitive == 2
    assert artifact.category_counts.order_sensitive == 1
    assert artifact.category_counts.ranking_instability_needs_judgment == 2
    assert artifact.diagnostic_candidate_count == 3
    assert artifact.relevance_labels_used is False
    assert artifact.relevance_metrics_computed is False
    assert artifact.quality_metrics_computed is False
    assert artifact.formal_evaluation_allowed is False
    assert artifact.stage_drop_diagnostics_computed is False
    assert artifact.protected_profile_dispatch_count == 0
    assert artifact.strategy_write_count == 0
    assert run.execution.execution_id.startswith("bad-case-execution-")
    assert len(run.samples) == 3
    assert run.samples[0].query_text
    assert run.samples[0].source_top_hits

    stored = Path(run.artifact_path).read_text(encoding="utf-8")
    assert query_set.cases[0].query_text not in stored
    assert "Readable product" not in stored
    assert "query_text" not in json.loads(stored)["observations"][0]
    assert (
        bad_runner.validate_bad_case_diagnostic(
            artifact=artifact,
            query_set=query_set,
        )
        == artifact
    )
    assert (
        bad_runner.rerun_bad_case_diagnostic(
            artifact=artifact,
            query_set=query_set,
            search_service=service,
        )
        == artifact
    )
    assert service.search_call_count == 118


def test_supervisor_can_bind_the_execution_id_and_start_time(
    query_set,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service_for_query_set(query_set, tmp_path)
    _patch_builder(monkeypatch, query_set)
    execution_id = "bad-case-execution-" + ("d" * 32)
    started_at = datetime(2026, 8, 29, 1, 2, 3, tzinfo=UTC)

    run = bad_runner.run_bad_case_diagnostics(
        project_root=ROOT,
        artifact_root=tmp_path / "runs",
        revision_provider=lambda _root: REVISION,
        search_service=service,
        execution_id=execution_id,
        execution_started_at_utc=started_at,
    )

    assert run.execution.execution_id == execution_id
    assert run.execution.started_at_utc == started_at
    stored = json.loads(Path(run.execution_path).read_text(encoding="utf-8"))
    assert stored["execution_id"] == execution_id


@pytest.mark.parametrize(
    "execution_id",
    [
        "",
        False,
        "bad-case-execution-short",
        "bad-case-execution-" + ("A" * 32),
        7,
    ],
)
def test_supervisor_execution_id_injection_rejects_invalid_values(
    tmp_path: Path,
    execution_id,
) -> None:
    with pytest.raises(ValueError, match="execution ID"):
        bad_runner.run_bad_case_diagnostics(
            project_root=ROOT,
            artifact_root=tmp_path / "runs",
            revision_provider=lambda _root: REVISION,
            execution_id=execution_id,
        )


def test_supervisor_start_time_injection_requires_timezone(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone"):
        bad_runner.run_bad_case_diagnostics(
            project_root=ROOT,
            artifact_root=tmp_path / "runs",
            revision_provider=lambda _root: REVISION,
            execution_started_at_utc=datetime(2026, 8, 29),
        )


def test_display_sample_and_trusted_query_tampering_are_rejected(
    query_set,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service_for_query_set(query_set, tmp_path)
    _patch_builder(monkeypatch, query_set)
    run = bad_runner.run_bad_case_diagnostics(
        project_root=ROOT,
        artifact_root=tmp_path / "runs",
        revision_provider=lambda _root: REVISION,
        search_service=service,
    )

    run_payload = run.model_dump(mode="json")
    run_payload["samples"][0]["source_top_hits"][0]["title"] = "Tampered"
    with pytest.raises(ValidationError, match="display hits"):
        BadCaseRun.model_validate(run_payload)

    synchronized_payload = run.model_dump(mode="json")
    sample = synchronized_payload["samples"][0]
    sample["source_top_hits"][0]["product_id"] = "B000OTHER1"
    source_case_id = sample["source_case_id"]
    source_observation = next(
        item
        for item in synchronized_payload["artifact"]["observations"]
        if item["case_id"] == source_case_id
    )
    changed_hit = sample["source_top_hits"][0]
    source_observation["ordered_display_hit_sha256s"][0] = display_hit_sha256(
        locale=changed_hit["locale"],
        product_id=changed_hit["product_id"],
        title=changed_hit["title"],
        rank=changed_hit["rank"],
    )
    artifact_body = {
        key: value
        for key, value in synchronized_payload["artifact"].items()
        if key != "diagnostic_id"
    }
    synchronized_payload["artifact"]["diagnostic_id"] = diagnostic_id(artifact_body)
    synchronized_payload["execution"]["diagnostic_id"] = synchronized_payload[
        "artifact"
    ]["diagnostic_id"]
    with pytest.raises(ValidationError, match="product keys"):
        BadCaseRun.model_validate(synchronized_payload)

    artifact_payload = run.artifact.model_dump(mode="json")
    artifact_payload["observations"][0]["query_sha256"] = "d" * 64
    artifact_payload["diagnostic_id"] = diagnostic_id(
        {
            key: value
            for key, value in artifact_payload.items()
            if key != "diagnostic_id"
        }
    )
    internally_valid = BadCaseDiagnosticArtifact.model_validate(artifact_payload)
    with pytest.raises(ValueError, match="trusted Query case"):
        bad_runner.validate_bad_case_diagnostic(
            artifact=internally_valid,
            query_set=query_set,
        )

    raw_hash_payload = run.artifact.model_dump(mode="json")
    observation = raw_hash_payload["observations"][0]
    observation["ordered_product_key_sha256s"][0] = "B000RAWID1"
    observation["ordered_results_sha256"] = ordered_results_sha256(
        observation["ordered_product_key_sha256s"]
    )
    observation["result_set_sha256"] = result_set_sha256(
        observation["ordered_product_key_sha256s"]
    )
    with pytest.raises(ValidationError):
        BadCaseDiagnosticArtifact.model_validate(raw_hash_payload)


def test_preflight_failure_runs_no_search_and_publishes_no_evidence(
    query_set,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service_for_query_set(query_set, tmp_path)
    _patch_builder(monkeypatch, query_set)
    calls = 0
    real_validate = bad_runner.validate_catalog_query

    def fail_on_fifth(query: str, *, top_k: int):
        nonlocal calls
        calls += 1
        if calls == 5:
            raise ValueError("private fifth Query is incompatible")
        return real_validate(query, top_k=top_k)

    monkeypatch.setattr(bad_runner, "validate_catalog_query", fail_on_fifth)
    with pytest.raises(ValueError, match="private fifth"):
        bad_runner.run_bad_case_diagnostics(
            project_root=ROOT,
            artifact_root=tmp_path / "runs",
            revision_provider=lambda _root: REVISION,
            search_service=service,
        )

    assert service.search_call_count == 0
    base = tmp_path / "runs" / "bad-case-diagnostics"
    assert not (base / "evidence").exists()
    attempts = list((base / "attempts").glob("*.json"))
    assert len(attempts) == 1
    attempt = json.loads(attempts[0].read_text(encoding="utf-8"))
    assert attempt["completed_query_count"] == 0
    assert attempt["failure_stage"] == "source_preflight"


def test_catalog_deadline_failure_stores_attempt_without_content_leak(
    query_set,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_service = _service_for_query_set(query_set, tmp_path)
    service = FailingCatalogService(
        index_path=tmp_path / "failing.sqlite3",
        results_by_query=base_service.results_by_query,
    )
    _patch_builder(monkeypatch, query_set)
    stream = io.StringIO()
    configure_logging(
        default_level="OFF",
        module_levels={"bad_case": "DEBUG"},
        stream=stream,
    )

    with pytest.raises(CatalogSearchDeadlineExceeded):
        bad_runner.run_bad_case_diagnostics(
            project_root=ROOT,
            artifact_root=tmp_path / "runs",
            revision_provider=lambda _root: REVISION,
            search_service=service,
        )

    base = tmp_path / "runs" / "bad-case-diagnostics"
    assert not (base / "evidence").exists()
    assert len(list((base / "attempts").glob("*.json"))) == 1
    logs = stream.getvalue()
    assert "private Query" not in logs
    assert query_set.cases[0].query_text not in logs
    assert "bad_case_batch_failed" in logs


def test_mid_batch_failure_receipt_preserves_safe_completed_count(
    query_set,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service_for_query_set(query_set, tmp_path)

    def fail_mid_batch(_queries, **_kwargs):
        raise CatalogSearchDeadlineExceeded(
            "private Query timed out",
            completed_query_count=7,
        )

    service.search_many = fail_mid_batch
    _patch_builder(monkeypatch, query_set)
    with pytest.raises(CatalogSearchDeadlineExceeded):
        bad_runner.run_bad_case_diagnostics(
            project_root=ROOT,
            artifact_root=tmp_path / "runs",
            revision_provider=lambda _root: REVISION,
            search_service=service,
        )
    attempt_path = next(
        (tmp_path / "runs" / "bad-case-diagnostics" / "attempts").glob("*.json")
    )
    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    assert attempt["completed_query_count"] == 7


def test_capacity_rejection_does_not_grow_attempt_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bad_runner,
        "ensure_bad_case_capacity",
        lambda _base: (_ for _ in ()).throw(RuntimeError("capacity exceeded")),
    )
    with pytest.raises(RuntimeError, match="capacity"):
        bad_runner.run_bad_case_diagnostics(
            project_root=ROOT,
            artifact_root=tmp_path / "runs",
            revision_provider=lambda _root: REVISION,
        )
    base = tmp_path / "runs" / "bad-case-diagnostics"
    assert not (base / "attempts").exists()
    assert not (base / "evidence").exists()


def test_second_artifact_write_failure_never_publishes_completed_receipt(
    query_set,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service_for_query_set(query_set, tmp_path)
    _patch_builder(monkeypatch, query_set)
    run = bad_runner.run_bad_case_diagnostics(
        project_root=ROOT,
        artifact_root=tmp_path / "source-runs",
        revision_provider=lambda _root: REVISION,
        search_service=service,
    )
    real_write = bad_artifacts.write_immutable_json
    calls = 0

    def fail_second_write(path, payload):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated receipt storage failure")
        return real_write(path, payload)

    monkeypatch.setattr(bad_artifacts, "write_immutable_json", fail_second_write)
    failed_root = tmp_path / "failed-runs"
    with pytest.raises(OSError, match="receipt storage"):
        bad_artifacts.store_bad_case_artifacts(
            artifact_root=failed_root,
            artifact=run.artifact,
            execution=run.execution,
        )

    base = failed_root / "bad-case-diagnostics"
    assert len(list((base / "evidence").glob("*.json"))) == 1
    assert list((base / "executions").glob("*.json")) == []
    assert not (base / "latest.txt").exists()


def test_latest_symlink_is_rejected_before_completed_artifacts_are_published(
    query_set,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service_for_query_set(query_set, tmp_path)
    _patch_builder(monkeypatch, query_set)
    failed_root = tmp_path / "failed-runs"
    base = trusted_bad_case_root(failed_root)
    pointer_target = tmp_path / "pointer-target"
    pointer_target.write_text("unchanged\n", encoding="utf-8")
    (base / "latest.txt").symlink_to(pointer_target)

    with pytest.raises(ValueError, match="latest pointer"):
        bad_runner.run_bad_case_diagnostics(
            project_root=ROOT,
            artifact_root=failed_root,
            revision_provider=lambda _root: REVISION,
            search_service=service,
        )

    assert not (base / "evidence").exists()
    assert not (base / "executions").exists()
    assert len(list((base / "attempts").glob("*.json"))) == 1
    assert pointer_target.read_text(encoding="utf-8") == "unchanged\n"


def test_cross_process_lock_and_symlink_components_are_rejected(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "runs"
    with bad_case_run_lock(run_root):
        with pytest.raises(BadCaseRunInProgress):
            with bad_case_run_lock(run_root):
                pass

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        trusted_bad_case_root(link / "runs")
