from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

from search_quality.evaluation import compare_cli
from search_quality.evaluation.authorization import ensure_profile_authorized
from search_quality.evaluation.baseline import run_candidate_baseline
from search_quality.evaluation.cli import build_parser, ensure_profile_unlocked
from search_quality.evaluation.datasets import EvaluationProfile
from search_quality.evaluation.relevance import RelevancePolicy

ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "data/manifests/esci-stage1.json"
POLICY = ROOT / "configs/evaluation/esci-primary-v1.json"


@pytest.fixture(scope="module")
def smoke_run_pair() -> tuple[dict, dict]:
    profile = EvaluationProfile.from_stage1_manifest(
        profile_id="smoke",
        project_root=ROOT,
        manifest_path=MANIFEST,
    )
    policy = RelevancePolicy.from_path(POLICY)
    baseline = run_candidate_baseline(
        profile,
        policy=policy,
        code_revision="a" * 40,
        ranker_name="random",
    )
    candidate = run_candidate_baseline(
        profile,
        policy=policy,
        code_revision="b" * 40,
        ranker_name="title-bm25",
    )
    return baseline, candidate


def _write_run_pair(
    directory: Path,
    runs: tuple[dict, dict],
    *,
    candidate_name: str | None = None,
) -> tuple[Path, Path]:
    baseline, candidate = runs
    baseline_path = directory / f"{baseline['run_id']}.json"
    candidate_path = directory / (candidate_name or f"{candidate['run_id']}.json")
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    return baseline_path, candidate_path


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


def test_cli_defaults_to_all_smoke_comparators() -> None:
    args = build_parser().parse_args([])
    assert args.profile == "smoke"
    assert args.ranker == "all"


def test_cli_keeps_frozen_test_unreachable() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--profile", "test"])


def test_dev_profile_is_locked_until_owner_checkpoint_is_recorded() -> None:
    ensure_profile_unlocked("smoke")
    with pytest.raises(RuntimeError, match="Owner data-boundary checkpoint"):
        ensure_profile_unlocked("dev")


def test_shared_baseline_api_rejects_dev_before_reading_data(tmp_path) -> None:
    missing = tmp_path / "must-not-be-opened.parquet"
    profile = EvaluationProfile(
        profile_id="dev",
        path=missing,
        file_sha256="unused-file-hash",
        canonical_sha256="unused-canonical-hash",
        stage1_manifest_sha256="unused-manifest-hash",
        stage1_schema_version="esci-stage1-v1",
        source_commit="unused-source-commit",
        expected_rows=1,
        expected_queries=1,
        expected_products=1,
    )

    with pytest.raises(RuntimeError, match="Owner data-boundary checkpoint"):
        run_candidate_baseline(
            profile,
            policy=RelevancePolicy(
                policy_id="test",
                label_gains={"E": 1.0, "S": 0.1, "C": 0.01, "I": 0.0},
                relevant_labels=frozenset({"E", "S"}),
            ),
            code_revision="a" * 40,
            ranker_name="random",
        )
    assert not missing.exists()
    with pytest.raises(RuntimeError, match="Owner data-boundary checkpoint"):
        ensure_profile_authorized("dev")


def test_compare_cli_defaults_to_random_vs_bm25_smoke() -> None:
    args = compare_cli.build_parser().parse_args([])
    assert args.profile == "smoke"
    baseline, candidate = compare_cli._run_input_paths(args)
    assert baseline.name == "latest-smoke-random.txt"
    assert candidate.name == "latest-smoke-title-bm25.txt"

    dev_args = compare_cli.build_parser().parse_args(["--profile", "dev"])
    dev_baseline, dev_candidate = compare_cli._run_input_paths(dev_args)
    assert dev_baseline.name == "latest-dev-random.txt"
    assert dev_candidate.name == "latest-dev-title-bm25.txt"


def test_compare_cli_rejects_dev_before_loading_run_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    args = compare_cli.build_parser().parse_args(
        [
            "--profile",
            "dev",
            "--baseline",
            str(tmp_path / "must-not-open-baseline.json"),
            "--candidate",
            str(tmp_path / "must-not-open-candidate.json"),
        ]
    )
    called = False

    def fail_if_called(_path):
        nonlocal called
        called = True
        raise AssertionError("load_run must not be called")

    monkeypatch.setattr(compare_cli, "load_run_from_store", fail_if_called)
    with pytest.raises(RuntimeError, match="Owner data-boundary checkpoint"):
        compare_cli._execute(args)
    assert called is False


def test_compare_output_is_confined_to_a_non_symlink_store_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = tmp_path / "runs"
    store.mkdir()
    monkeypatch.setattr(compare_cli, "RUN_STORE_ROOT", store)

    with pytest.raises(ValueError, match="trusted Run store"):
        compare_cli._comparison_output_dir(tmp_path / "outside")
    with pytest.raises(ValueError, match="parent traversal"):
        compare_cli._comparison_output_dir(store / "other" / ".." / "comparisons")

    outside = tmp_path / "outside"
    outside.mkdir()
    comparison_dir = store / "comparisons"
    comparison_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        compare_cli._comparison_output_dir(comparison_dir)

    comparison_dir.unlink()
    store_alias = tmp_path / "runs-alias"
    store_alias.symlink_to(store, target_is_directory=True)
    monkeypatch.setattr(compare_cli, "RUN_STORE_ROOT", store_alias)
    with pytest.raises(ValueError, match="store must not be a symbolic link"):
        compare_cli._comparison_output_dir(store_alias / "comparisons")


def test_compare_cli_publishes_json_report_and_latest_pointer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    smoke_run_pair: tuple[dict, dict],
) -> None:
    baseline, candidate = smoke_run_pair
    baseline_path, candidate_path = _write_run_pair(tmp_path, smoke_run_pair)
    output_dir = tmp_path / "comparisons"
    args = compare_cli.build_parser().parse_args(
        [
            "--baseline",
            str(baseline_path),
            "--candidate",
            str(candidate_path),
            "--output-dir",
            str(output_dir),
        ]
    )
    monkeypatch.setattr(
        compare_cli,
        "require_clean_code_revision",
        lambda _root: "c" * 40,
    )
    monkeypatch.setattr(compare_cli, "RUN_STORE_ROOT", tmp_path)

    compare_cli._execute(args)

    pointer = output_dir / "latest-smoke-comparison.txt"
    comparison_path = output_dir / pointer.read_text(encoding="utf-8").strip()
    report_path = comparison_path.with_suffix(".md")
    comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
    assert comparison["baseline"]["run_id"] == baseline["run_id"]
    assert comparison["candidate"]["run_id"] == candidate["run_id"]
    assert comparison["aggregate_metrics"]["ndcg@10"]["delta"] == pytest.approx(
        0.17331174062776777
    )
    assert comparison["comparison_id"] in report_path.read_text(encoding="utf-8")
    assert comparison["comparison_id"] in capsys.readouterr().out
    assert list(output_dir.glob("*.tmp")) == []


def test_compare_cli_main_correlates_success_without_logging_evidence_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    smoke_run_pair: tuple[dict, dict],
) -> None:
    baseline, candidate = smoke_run_pair
    baseline_path, candidate_path = _write_run_pair(tmp_path, smoke_run_pair)
    output_dir = tmp_path / "comparisons"
    monkeypatch.setattr(
        compare_cli,
        "require_clean_code_revision",
        lambda _root: "c" * 40,
    )
    monkeypatch.setattr(compare_cli, "RUN_STORE_ROOT", tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compare-runs-test",
            "--baseline",
            str(baseline_path),
            "--candidate",
            str(candidate_path),
            "--output-dir",
            str(output_dir),
            "--log-module",
            "evaluation=INFO",
        ],
    )

    compare_cli.main()

    captured = capsys.readouterr()
    events = [json.loads(line) for line in captured.err.splitlines()]
    assert [event["event"] for event in events] == [
        "comparison_command_started",
        "run_comparison_started",
        "run_comparison_completed",
        "comparison_artifacts_stored",
    ]
    assert len({event["trace_id"] for event in events}) == 1
    assert all(event["profile_id"] == "smoke" for event in events)
    assert all(event["module"] == "evaluation" for event in events)
    assert events[2]["query_count"] == 20
    assert "duration_ms" in events[2]
    assert events[3]["comparison_id"] in captured.out
    assert events[3]["baseline_run_id"] == baseline["run_id"]
    assert events[3]["candidate_run_id"] == candidate["run_id"]
    for run in smoke_run_pair:
        for query in run["per_query"]:
            assert query["query_text"] not in captured.err
            for item in query["ranking"]:
                assert item["product_id"] not in captured.err
    assert str(baseline_path) not in captured.err
    assert str(candidate_path) not in captured.err
    assert str(output_dir) not in captured.err


def test_compare_cli_real_validation_failure_is_safe_and_has_no_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    smoke_run_pair: tuple[dict, dict],
) -> None:
    baseline, candidate = smoke_run_pair
    incompatible = copy.deepcopy(candidate)
    secret = "Bearer-validation-private-secret-query"
    incompatible["per_query"][0]["query_text"] = secret
    incompatible = _reseal(incompatible)
    baseline_path, candidate_path = _write_run_pair(
        tmp_path,
        (baseline, incompatible),
    )
    output_dir = tmp_path / "comparisons"
    monkeypatch.setattr(
        compare_cli,
        "require_clean_code_revision",
        lambda _root: "c" * 40,
    )
    monkeypatch.setattr(compare_cli, "RUN_STORE_ROOT", tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compare-runs-test",
            "--baseline",
            str(baseline_path),
            "--candidate",
            str(candidate_path),
            "--output-dir",
            str(output_dir),
            "--log-module",
            "evaluation=INFO",
        ],
    )

    with pytest.raises(SystemExit) as captured_exit:
        compare_cli.main()

    captured = capsys.readouterr()
    events = [json.loads(line) for line in captured.err.splitlines()]
    assert captured_exit.value.code == 1
    assert [event["event"] for event in events] == [
        "comparison_command_started",
        "comparison_command_failed",
    ]
    assert events[-1]["failure_stage"] == "compare"
    assert events[-1]["error_code"] == "invalid_input"
    assert secret not in captured.err
    assert str(candidate_path) not in captured.err
    assert not output_dir.exists()


def test_compare_cli_store_failure_is_safe_and_never_claims_storage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    smoke_run_pair: tuple[dict, dict],
) -> None:
    baseline_path, candidate_path = _write_run_pair(tmp_path, smoke_run_pair)
    output_dir = tmp_path / "comparisons"
    secret = "Bearer-store-private-secret-path"
    monkeypatch.setattr(
        compare_cli,
        "require_clean_code_revision",
        lambda _root: "c" * 40,
    )
    monkeypatch.setattr(compare_cli, "RUN_STORE_ROOT", tmp_path)

    def fail_store(_path, _payload) -> None:
        raise OSError(secret)

    monkeypatch.setattr(compare_cli, "write_immutable_json", fail_store)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compare-runs-test",
            "--baseline",
            str(baseline_path),
            "--candidate",
            str(candidate_path),
            "--output-dir",
            str(output_dir),
            "--log-module",
            "evaluation=INFO",
        ],
    )

    with pytest.raises(SystemExit) as captured_exit:
        compare_cli.main()

    captured = capsys.readouterr()
    events = [json.loads(line) for line in captured.err.splitlines()]
    assert captured_exit.value.code == 1
    assert [event["event"] for event in events] == [
        "comparison_command_started",
        "run_comparison_started",
        "run_comparison_completed",
        "comparison_command_failed",
    ]
    assert events[-1]["failure_stage"] == "store"
    assert events[-1]["error_code"] == "io_failure"
    assert "comparison_artifacts_stored" not in captured.err
    assert secret not in captured.err
    assert str(output_dir) not in captured.err
    assert not output_dir.exists()
