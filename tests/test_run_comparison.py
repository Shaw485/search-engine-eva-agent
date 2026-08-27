from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path

import pytest

from search_quality.evaluation.baseline import run_candidate_baseline
from search_quality.evaluation.comparison import (
    METRIC_NAMES,
    compare_runs,
    load_run,
    load_run_from_store,
    render_comparison_markdown,
)
from search_quality.evaluation.datasets import EvaluationProfile
from search_quality.evaluation.relevance import RelevancePolicy
from search_quality.observability import configure_logging, logging_context

ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "data/manifests/esci-stage1.json"
POLICY = ROOT / "configs/evaluation/esci-primary-v1.json"
BASELINE_REVISION = "a" * 40
CANDIDATE_REVISION = "b" * 40
COMPARATOR_REVISION = "c" * 40


@pytest.fixture(scope="module")
def smoke_runs() -> tuple[dict, dict]:
    profile = EvaluationProfile.from_stage1_manifest(
        profile_id="smoke",
        project_root=ROOT,
        manifest_path=MANIFEST,
    )
    policy = RelevancePolicy.from_path(POLICY)
    baseline = run_candidate_baseline(
        profile,
        policy=policy,
        code_revision=BASELINE_REVISION,
        ranker_name="random",
    )
    candidate = run_candidate_baseline(
        profile,
        policy=policy,
        code_revision=CANDIDATE_REVISION,
        ranker_name="title-bm25",
    )
    return baseline, candidate


def _reseal(run: dict) -> dict:
    updated = copy.deepcopy(run)
    prefix = str(updated.pop("run_id")).rsplit("-", maxsplit=1)[0]
    canonical = json.dumps(
        updated,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    updated["run_id"] = f"{prefix}-{hashlib.sha256(canonical).hexdigest()[:12]}"
    return updated


def _compare(
    baseline: dict,
    candidate: dict,
    *,
    revision: str = COMPARATOR_REVISION,
) -> dict:
    return compare_runs(
        baseline,
        candidate,
        comparator_revision=revision,
        expected_profile="smoke",
        project_root=ROOT,
        manifest_path=MANIFEST,
    )


def test_comparison_is_deterministic_complete_and_directional(smoke_runs) -> None:
    baseline, candidate = smoke_runs
    baseline_before = copy.deepcopy(baseline)
    candidate_before = copy.deepcopy(candidate)

    first = _compare(baseline, candidate)
    second = _compare(baseline, candidate)

    assert first == second
    assert baseline == baseline_before
    assert candidate == candidate_before
    assert first["aggregate_metrics"]["ndcg@10"] == {
        "baseline": pytest.approx(0.545786144784376),
        "candidate": pytest.approx(0.7190978854121438),
        "delta": pytest.approx(0.17331174062776777),
    }
    assert first["aggregate_metrics"]["mrr@10"]["delta"] == pytest.approx(
        0.04750000000000021
    )
    assert len(first["per_query"]) == 20
    assert [
        (query["locale"], query["query_id"]) for query in first["per_query"]
    ] == sorted((query["locale"], query["query_id"]) for query in first["per_query"])
    for metric in METRIC_NAMES:
        assert sum(first["outcome_counts"][metric].values()) == 20
    for query in first["per_query"]:
        assert len(query["ranking_diff"]) == query["candidate_count"]
        assert query["changed_rank_count"] == sum(
            item["rank_delta"] != 0 for item in query["ranking_diff"]
        )
        assert all(
            item["rank_delta"] == item["baseline_rank"] - item["candidate_rank"]
            for item in query["ranking_diff"]
        )
    query_15281 = next(
        query for query in first["per_query"] if query["query_id"] == 15281
    )
    assert query_15281["metrics"]["ndcg@10"]["delta"] == pytest.approx(
        -0.32299213481675826
    )


def test_reversing_comparison_reverses_metric_and_rank_directions(smoke_runs) -> None:
    baseline, candidate = smoke_runs
    forward = _compare(baseline, candidate)
    reverse = _compare(candidate, baseline)

    for metric in METRIC_NAMES:
        assert reverse["aggregate_metrics"][metric]["delta"] == pytest.approx(
            -forward["aggregate_metrics"][metric]["delta"]
        )
    forward_queries = {
        (query["locale"], query["query_id"]): query for query in forward["per_query"]
    }
    for reverse_query in reverse["per_query"]:
        query_key = (reverse_query["locale"], reverse_query["query_id"])
        forward_products = {
            (item["locale"], item["product_id"]): item
            for item in forward_queries[query_key]["ranking_diff"]
        }
        for reverse_item in reverse_query["ranking_diff"]:
            product_key = (reverse_item["locale"], reverse_item["product_id"])
            forward_item = forward_products[product_key]
            assert reverse_item["baseline_rank"] == forward_item["candidate_rank"]
            assert reverse_item["candidate_rank"] == forward_item["baseline_rank"]
            assert reverse_item["rank_delta"] == -forward_item["rank_delta"]


def test_comparison_revision_is_part_of_identity(smoke_runs) -> None:
    baseline, candidate = smoke_runs
    first = _compare(baseline, candidate, revision="d" * 40)
    second = _compare(baseline, candidate, revision="e" * 40)
    assert first["comparison_id"] != second["comparison_id"]


def test_comparison_requires_a_full_git_revision(smoke_runs) -> None:
    baseline, candidate = smoke_runs
    with pytest.raises(ValueError, match="full lowercase Git commit SHA"):
        _compare(baseline, candidate, revision="working-tree")


def test_same_run_is_a_zero_delta_sanity_check(smoke_runs) -> None:
    baseline, _candidate = smoke_runs
    comparison = _compare(baseline, baseline)
    assert all(
        values["delta"] == pytest.approx(0.0)
        for values in comparison["aggregate_metrics"].values()
    )
    assert all(
        counts == {"improved": 0, "regressed": 0, "tied": 20}
        for counts in comparison["outcome_counts"].values()
    )
    assert all(query["changed_rank_count"] == 0 for query in comparison["per_query"])


def test_code_revision_and_ranker_are_allowed_to_differ(smoke_runs) -> None:
    baseline, candidate = smoke_runs
    comparison = _compare(baseline, candidate)
    assert comparison["baseline"]["code_revision"] == BASELINE_REVISION
    assert comparison["candidate"]["code_revision"] == CANDIDATE_REVISION
    assert comparison["baseline"]["ranker_id"] != comparison["candidate"]["ranker_id"]


def test_same_ranking_from_a_different_code_revision_is_zero_delta(smoke_runs) -> None:
    baseline, _candidate = smoke_runs
    new_revision = copy.deepcopy(baseline)
    new_revision["code_revision"] = "d" * 40
    new_revision = _reseal(new_revision)

    comparison = _compare(baseline, new_revision)
    assert all(
        values["delta"] == pytest.approx(0.0)
        for values in comparison["aggregate_metrics"].values()
    )
    assert comparison["baseline"]["code_revision"] == BASELINE_REVISION
    assert comparison["candidate"]["code_revision"] == "d" * 40


def test_known_ranker_id_must_match_the_run_id_prefix(smoke_runs) -> None:
    baseline, candidate = smoke_runs
    mislabeled = copy.deepcopy(baseline)
    mislabeled["ranker"] = copy.deepcopy(candidate["ranker"])
    mislabeled = _reseal(mislabeled)

    with pytest.raises(ValueError, match="prefix does not match its Ranker ID"):
        _compare(mislabeled, candidate)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda run: run["dataset"].__setitem__("canonical_sha256", "0" * 64),
            "trusted Stage 1 Manifest",
        ),
        (
            lambda run: run["per_query"][0].__setitem__(
                "query_text", "different query text"
            ),
            "different text",
        ),
        (
            lambda run: run["per_query"][0]["ranking"][0].__setitem__(
                "product_id", "different-product-id"
            ),
            "different candidate evidence",
        ),
    ],
)
def test_comparison_rejects_incompatible_evidence(
    smoke_runs, mutation, message
) -> None:
    baseline, candidate = smoke_runs
    incompatible = copy.deepcopy(candidate)
    mutation(incompatible)
    incompatible = _reseal(incompatible)

    with pytest.raises(ValueError, match=message):
        _compare(baseline, incompatible)


def test_run_validation_rejects_tampering_and_incomplete_legacy_schema(
    smoke_runs,
) -> None:
    baseline, candidate = smoke_runs
    tampered = copy.deepcopy(candidate)
    tampered["code_revision"] = "f" * 40
    with pytest.raises(ValueError, match="Run content does not match its Run ID"):
        _compare(baseline, tampered)

    legacy = copy.deepcopy(candidate)
    del legacy["dataset"]["canonical_sha256"]
    legacy = _reseal(legacy)
    with pytest.raises(ValueError, match="dataset does not match the v1 schema"):
        _compare(baseline, legacy)


def test_run_validation_rejects_malformed_dataset_hash(smoke_runs) -> None:
    baseline, candidate = smoke_runs
    invalid = copy.deepcopy(candidate)
    invalid["dataset"]["file_sha256"] = "not-a-sha256"
    invalid = _reseal(invalid)

    with pytest.raises(ValueError, match="invalid dataset hash"):
        _compare(baseline, invalid)


def test_run_validation_rejects_official_test_and_locked_dev_profiles(
    smoke_runs,
) -> None:
    baseline, candidate = smoke_runs
    official_test = copy.deepcopy(candidate)
    official_test["dataset"]["origin_splits"] = ["test"]
    official_test = _reseal(official_test)
    with pytest.raises(ValueError, match="official-train-derived"):
        _compare(baseline, official_test)

    dev = copy.deepcopy(candidate)
    dev["dataset"]["profile"] = "dev"
    dev = _reseal(dev)
    with pytest.raises(RuntimeError, match="Owner data-boundary checkpoint"):
        _compare(baseline, dev)


def test_run_validation_recomputes_query_metrics_from_ranking(smoke_runs) -> None:
    baseline, candidate = smoke_runs
    invalid = copy.deepcopy(candidate)
    invalid["per_query"][0]["metrics"]["mrr@10"] = 0.0
    invalid = _reseal(invalid)
    with pytest.raises(ValueError, match="Query metric does not match its ranking"):
        _compare(baseline, invalid)


def test_markdown_and_logs_are_deterministic_and_logs_omit_query_text(
    smoke_runs,
) -> None:
    baseline, candidate = smoke_runs
    stream = io.StringIO()
    configure_logging(
        default_level="OFF",
        module_levels={"evaluation": "INFO"},
        stream=stream,
    )
    with logging_context(trace_id="comparison-trace"):
        comparison = _compare(baseline, candidate)
    report = render_comparison_markdown(comparison)

    assert "candidate minus baseline" in report
    assert "Aggregate metric deltas" in report
    assert comparison["comparison_id"] in report
    assert baseline["per_query"][0]["query_text"] not in stream.getvalue()
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [event["event"] for event in events] == [
        "run_comparison_started",
        "run_comparison_completed",
    ]
    assert all(event["trace_id"] == "comparison-trace" for event in events)


def test_load_run_supports_safe_pointer_and_rejects_unsafe_json(
    tmp_path: Path,
    smoke_runs,
) -> None:
    baseline, _candidate = smoke_runs
    run_path = tmp_path / f"{baseline['run_id']}.json"
    run_path.write_text(json.dumps(baseline), encoding="utf-8")
    pointer = tmp_path / "latest-smoke-random.txt"
    pointer.write_text(run_path.name + "\n", encoding="utf-8")
    assert load_run(pointer) == baseline

    pointer.write_text("../outside.json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid same-directory target"):
        load_run(pointer)

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"run_id":"one","run_id":"two"}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate object keys"):
        load_run(duplicate)

    non_finite = tmp_path / "non-finite.json"
    non_finite.write_text('{"metric":NaN}', encoding="utf-8")
    with pytest.raises(ValueError, match="non-finite numeric constant"):
        load_run(non_finite)


def test_trusted_store_loader_accepts_a_matching_run_and_pointer(
    tmp_path: Path,
    smoke_runs,
) -> None:
    baseline, _candidate = smoke_runs
    run_path = tmp_path / f"{baseline['run_id']}.json"
    run_path.write_text(json.dumps(baseline), encoding="utf-8")
    pointer = tmp_path / "latest-smoke-random.txt"
    pointer.write_text(run_path.name + "\n", encoding="utf-8")

    assert load_run_from_store(run_path, store_root=tmp_path) == baseline
    assert load_run_from_store(pointer, store_root=tmp_path) == baseline


def test_trusted_store_loader_rejects_outside_symlink_and_filename_mismatch(
    tmp_path: Path,
    smoke_runs,
) -> None:
    baseline, _candidate = smoke_runs
    store = tmp_path / "runs"
    store.mkdir()
    outside = tmp_path / f"{baseline['run_id']}.json"
    outside.write_text(json.dumps(baseline), encoding="utf-8")

    with pytest.raises(ValueError, match="direct file"):
        load_run_from_store(outside, store_root=store)
    with pytest.raises(ValueError, match="parent traversal"):
        load_run_from_store(store / ".." / outside.name, store_root=store)

    direct_symlink = store / outside.name
    direct_symlink.symlink_to(outside)
    with pytest.raises(ValueError, match="symbolic links"):
        load_run_from_store(direct_symlink, store_root=store)
    direct_symlink.unlink()

    target_symlink = store / outside.name
    target_symlink.symlink_to(outside)
    pointer = store / "latest-smoke-random.txt"
    pointer.write_text(target_symlink.name + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="symbolic link"):
        load_run_from_store(pointer, store_root=store)
    target_symlink.unlink()

    wrong_name = store / "random-000000000000.json"
    wrong_name.write_text(json.dumps(baseline), encoding="utf-8")
    with pytest.raises(ValueError, match="filename does not match"):
        load_run_from_store(wrong_name, store_root=store)

    correct_name = store / outside.name
    correct_name.write_text(json.dumps(baseline), encoding="utf-8")
    store_alias = tmp_path / "runs-alias"
    store_alias.symlink_to(store, target_is_directory=True)
    with pytest.raises(ValueError, match="store must not be a symbolic link"):
        load_run_from_store(store_alias / correct_name.name, store_root=store_alias)
