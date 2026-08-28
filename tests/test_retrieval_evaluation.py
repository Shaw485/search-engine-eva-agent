from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from search_quality.evaluation import retrieval
from search_quality.evaluation.datasets import EvaluationProfile
from search_quality.evaluation.metrics import mean_recall_at_k, recall_at_k
from search_quality.evaluation.relevance import RelevancePolicy
from search_quality.evaluation.retrieval import run_query_scoped_retrieval
from search_quality.observability import configure_logging, logging_context

ROOT = Path(__file__).resolve().parents[1]


def _profile() -> EvaluationProfile:
    return EvaluationProfile.from_stage1_manifest(
        profile_id="smoke",
        project_root=ROOT,
        manifest_path=ROOT / "data/manifests/esci-stage1.json",
    )


def _policy() -> RelevancePolicy:
    return RelevancePolicy.from_path(ROOT / "configs/evaluation/esci-primary-v1.json")


def _run() -> dict:
    return run_query_scoped_retrieval(
        _profile(),
        policy=_policy(),
        policy_path=ROOT / "configs/evaluation/esci-primary-v1.json",
        project_root=ROOT,
        code_revision="a" * 40,
    )


def test_recall_uses_explicit_complete_denominator() -> None:
    assert recall_at_k(
        [True, False, True, True], total_relevant=5, k=3
    ) == pytest.approx(0.4)
    assert mean_recall_at_k(
        [[True, False], [False, True]],
        total_relevant_by_query=[2, 4],
        k=2,
    ) == pytest.approx(0.375)
    with pytest.raises(ValueError, match="at least one"):
        recall_at_k([False], total_relevant=0, k=1)
    with pytest.raises(ValueError, match="exceed"):
        recall_at_k([True, True], total_relevant=1, k=2)


def test_real_smoke_retrieval_run_is_deterministic_and_scoped() -> None:
    first = _run()
    second = _run()

    assert first == second
    assert first["run_id"].startswith("retrieval-")
    assert first["dataset"]["query_count"] == 20
    assert first["dataset"]["judged_pairs"] == 416
    assert first["dataset"]["possible_pairs_in_scope"] == 416
    assert first["dataset"]["unknown_pairs_in_scope"] == 0
    assert first["evaluation_boundary"] == {
        "denominator_complete": True,
        "eligible_metrics": [
            "judged_recall@5",
            "judged_recall@10",
            "mrr@10",
            "ndcg@10",
        ],
        "forbidden_claims": [
            "amazon_full_catalog_recall",
            "production_search_quality",
            "unjudged_products_are_irrelevant",
            "smoke_results_generalize",
        ],
        "full_catalog_recall_claimed": False,
        "pool_construction": "per_query_fully_judged_candidate_pool",
        "shared_corpus_recall_claimed": False,
        "task": "query-scoped-judged-pool-candidate-retention",
        "unjudged_products_are_irrelevant": False,
        "unjudged_treatment": "exclude_out_of_scope",
    }
    assert first["aggregate"]["exact_unique_relevant_count"] == 0
    assert first["aggregate"]["stages"]["recall-union-v1"][
        "mean_judged_relevant_coverage"
    ] == pytest.approx(0.8114716964070412)
    assert sum(first["aggregate"]["first_loss_counts"].values()) == 321
    assert all(
        len(item["lineage"]) == item["relevant_count"] for item in first["per_query"]
    )


def test_dev_is_rejected_before_the_data_file_is_checked() -> None:
    locked = EvaluationProfile(
        profile_id="dev",
        path=ROOT / "private-does-not-exist.parquet",
        file_sha256="x",
        canonical_sha256="x",
        stage1_manifest_sha256="x",
        stage1_schema_version="x",
        source_commit="x",
        expected_rows=1,
        expected_queries=1,
        expected_products=1,
    )

    with pytest.raises(RuntimeError, match="500-Query dev profile is locked"):
        run_query_scoped_retrieval(
            locked,
            policy=_policy(),
            policy_path=ROOT / "configs/evaluation/esci-primary-v1.json",
            project_root=ROOT,
            code_revision="a" * 40,
        )


def test_smoke_symlink_is_rejected_before_source_hash_or_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    samples = tmp_path / "data" / "samples"
    samples.mkdir(parents=True)
    protected = tmp_path / "locked-test.parquet"
    protected.write_bytes(b"protected sentinel must not be opened")
    linked = samples / "esci-stage1-smoke.parquet"
    linked.symlink_to(protected)
    profile = EvaluationProfile(
        profile_id="smoke",
        path=linked,
        file_sha256="a" * 64,
        canonical_sha256="b" * 64,
        stage1_manifest_sha256="c" * 64,
        stage1_schema_version="esci-stage1-manifest-v1",
        source_commit="d" * 40,
        expected_rows=1,
        expected_queries=1,
        expected_products=1,
    )
    source_opened = False

    def forbidden_hash(_path):
        nonlocal source_opened
        source_opened = True
        raise AssertionError("source hashing must not run")

    monkeypatch.setattr(retrieval, "sha256_file", forbidden_hash)
    with pytest.raises(ValueError, match="symbolic link"):
        run_query_scoped_retrieval(
            profile,
            policy=_policy(),
            policy_path=ROOT / "configs/evaluation/esci-primary-v1.json",
            project_root=tmp_path,
            code_revision="a" * 40,
        )
    assert source_opened is False


def test_retrieval_logs_are_module_scoped_and_do_not_leak_evidence() -> None:
    stream = io.StringIO()
    configure_logging(
        default_level="OFF",
        module_levels={"retrieval": "DEBUG"},
        stream=stream,
    )
    with logging_context(trace_id="retrieval-safe-trace"):
        result = _run()

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert events
    assert {event["module"] for event in events} == {"retrieval"}
    assert all(event["trace_id"] == "retrieval-safe-trace" for event in events)
    serialized = stream.getvalue().lower()
    assert result["per_query"][0]["query_text"].lower() not in serialized
    assert result["per_query"][0]["lineage"][0]["product_id"].lower() not in serialized
    assert "product_title" not in serialized


def test_tampered_retrieval_profile_fails_closed(tmp_path: Path) -> None:
    original = json.loads(
        (ROOT / "configs/evaluation/query-scoped-retrieval-smoke-v0.json").read_text()
    )
    original["unknown_pairs_in_scope"] = 1
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(original), encoding="utf-8")

    with pytest.raises(ValueError, match="zero unknown"):
        run_query_scoped_retrieval(
            _profile(),
            policy=_policy(),
            policy_path=ROOT / "configs/evaluation/esci-primary-v1.json",
            project_root=ROOT,
            profile_config_path=path,
            code_revision="a" * 40,
        )
