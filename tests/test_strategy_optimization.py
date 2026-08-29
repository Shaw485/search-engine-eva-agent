from __future__ import annotations

import hashlib
import io
import json
import multiprocessing
import shutil
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from apps.api import main as api
from search_quality.agent import optimization
from search_quality.agent.optimization import (
    apply_strategy_decision as _apply_strategy_decision,
)
from search_quality.agent.optimization import (
    generate_strategy_proposal,
    load_strategy_catalog,
)
from search_quality.agent.strategy_search import CandidateSelection, GatePolicy
from search_quality.observability import configure_logging, logging_context

PROJECT_ROOT = Path(__file__).parents[1]
TEST_CODE_REVISION = "a" * 40


def apply_strategy_decision(**kwargs):
    kwargs.setdefault("revision_provider", lambda _root: TEST_CODE_REVISION)
    return _apply_strategy_decision(**kwargs)


def _apply_decision_worker(
    project_root: str,
    proposal_id: str,
    code_revision: str,
    start_event,
    result_queue,
) -> None:
    start_event.wait()
    try:
        result = apply_strategy_decision(
            project_root=project_root,
            proposal_id=proposal_id,
            decision="approve",
            revision_provider=lambda _root: code_revision,
        )
        result_queue.put(("applied", proposal_id, result["decision_id"]))
    except Exception as exc:  # pragma: no cover - asserted in the parent process
        result_queue.put(("error", proposal_id, str(exc)))


def _request(
    *,
    client: tuple[str, int] = ("127.0.0.1", 50000),
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/agent/strategy/decision",
            "headers": headers or [],
            "client": client,
        }
    )


def _copy_smoke_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "configs" / "evaluation").mkdir(parents=True)
    (project / "data" / "manifests").mkdir(parents=True)
    (project / "data" / "samples").mkdir(parents=True)
    shutil.copy2(
        PROJECT_ROOT / "configs" / "evaluation" / "esci-primary-v1.json",
        project / "configs" / "evaluation" / "esci-primary-v1.json",
    )
    shutil.copy2(
        PROJECT_ROOT / "data" / "manifests" / "esci-stage1.json",
        project / "data" / "manifests" / "esci-stage1.json",
    )
    shutil.copy2(
        PROJECT_ROOT / "data" / "samples" / "esci-stage1-smoke.parquet",
        project / "data" / "samples" / "esci-stage1-smoke.parquet",
    )
    return project


def _assert_no_strategy_activation(project: Path, proposal_id: str) -> None:
    run_store = project / "runs"
    assert not (run_store / "search-strategies" / "active.json").exists()
    assert not (run_store / "search-strategies" / "catalog.json").exists()
    assert not (
        run_store / "strategy-decisions" / "by-proposal" / f"{proposal_id}.json"
    ).exists()
    assert list((run_store / "strategy-decisions").glob("decision-*.json")) == []


def _legacy_active_strategy() -> dict:
    return {
        "applied_from_proposal_id": "proposal-aaaaaaaaaaaa",
        "baseline_run_id": "bm25-aaaaaaaaaaaa",
        "candidate_run_id": "exact-boost-bbbbbbbbbbbb",
        "comparison_id": "comparison-cccccccccccc",
        "schema_version": "search-strategy-config-v1",
        "strategy": {
            "catalog_entry": {
                "description": (
                    "Title BM25 plus deterministic query coverage, numeric-token "
                    "and exact-phrase boosts."
                ),
                "name": "Title BM25 Exact Boost",
                "stage": "多路召回 / 词法排序",
            },
            "config": {
                "analyzer_id": "ascii-alnum-lower-v1",
                "b": 0.75,
                "coverage_boost": 0.8,
                "field": "product_title",
                "idf_scope": "per_query_judged_candidates",
                "k1": 1.5,
                "numeric_boost": 1.0,
                "phrase_boost": 1.2,
                "query_terms": "deduplicated",
                "ranker_id": "candidate-title-bm25-exact-boost-v1",
                "score": ("title_bm25_plus_query_coverage_numeric_and_phrase_boosts"),
                "tie_break": "product_locale_product_id_ascending",
            },
            "is_new_strategy": True,
            "strategy_id": "candidate-title-bm25-exact-boost-v1",
        },
    }


def _write_active_strategy(project: Path, active: dict) -> Path:
    active_path = project / "runs" / "search-strategies" / "active.json"
    active_path.parent.mkdir(parents=True)
    active_path.write_text(
        json.dumps(active, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return active_path


def test_legacy_catalog_is_exposed_as_history_without_sensitive_evidence(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    strategy_dir = project / "runs" / "search-strategies"
    strategy_dir.mkdir(parents=True)
    (strategy_dir / "catalog.json").write_text(
        json.dumps(
            {
                "schema_version": "search-strategy-catalog-v1",
                "strategies": [
                    {
                        "comparison_id": "comparison-aaaaaaaaaaaa",
                        "description": "Legacy approved strategy",
                        "name": "Legacy strategy",
                        "proposal_id": "proposal-aaaaaaaaaaaa",
                        "stage": "多路召回",
                        "strategy_id": "legacy-v1",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    catalog = load_strategy_catalog(project_root=project)

    assert catalog["strategy_history"][0]["strategy_id"] == "legacy-v1"
    assert catalog["strategy_history"][0]["config"] == {}
    assert catalog["strategy_activity_logs"][0]["event_id"] == (
        "activity-proposal-aaaaaaaaaaaa"
    )


def test_legacy_active_strategy_is_strictly_migrated_before_reuse(
    tmp_path: Path,
) -> None:
    project = _copy_smoke_project(tmp_path)
    legacy = _legacy_active_strategy()
    active_path = _write_active_strategy(project, legacy)
    stream = io.StringIO()
    configure_logging(
        default_level="WARNING",
        module_levels={"agent_optimization": "INFO"},
        stream=stream,
    )

    catalog = load_strategy_catalog(project_root=project)

    migrated = json.loads(active_path.read_text(encoding="utf-8"))
    expected_hash = hashlib.sha256(
        json.dumps(
            legacy["strategy"]["config"],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    assert migrated["strategy"]["config_sha256"] == expected_hash
    assert {
        key: value
        for key, value in migrated["strategy"].items()
        if key != "config_sha256"
    } == legacy["strategy"]
    assert catalog["active_strategy_id"] == ("candidate-title-bm25-exact-boost-v1")
    assert (
        catalog["active_revision"]
        == hashlib.sha256(
            json.dumps(
                migrated,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
    )
    migration_event = next(
        event
        for event in (json.loads(line) for line in stream.getvalue().splitlines())
        if event["event"] == "legacy_active_strategy_migrated"
    )
    assert migration_event["strategy_id"] == ("candidate-title-bm25-exact-boost-v1")
    assert "config" not in migration_event

    proposal = generate_strategy_proposal(
        project_root=project,
        revision_provider=lambda _root: "a" * 40,
    )
    baseline = json.loads(
        (project / "runs" / f"{proposal['baseline_run_id']}.json").read_text(
            encoding="utf-8"
        )
    )
    assert proposal["parent_active_strategy_id"] == (
        "candidate-title-bm25-exact-boost-v1"
    )
    assert baseline["ranker"] == legacy["strategy"]["config"]


def test_legacy_active_strategy_migration_rejects_modified_config(
    tmp_path: Path,
) -> None:
    project = _copy_smoke_project(tmp_path)
    active = _legacy_active_strategy()
    active["strategy"]["config"]["coverage_boost"] = 0.9
    active_path = _write_active_strategy(project, active)

    with pytest.raises(ValueError, match="not a supported legacy v1 strategy"):
        load_strategy_catalog(project_root=project)

    assert json.loads(active_path.read_text(encoding="utf-8")) == active


@pytest.mark.parametrize(
    ("config_sha256", "error"),
    [
        (None, "config hash is missing"),
        ("0" * 64, "config hash is invalid"),
    ],
)
def test_new_active_strategy_never_bypasses_config_hash_validation(
    tmp_path: Path,
    config_sha256: str | None,
    error: str,
) -> None:
    project = _copy_smoke_project(tmp_path)
    active = _legacy_active_strategy()
    strategy = active["strategy"]
    strategy["strategy_id"] = "exact-conservative-v1"
    strategy["catalog_entry"]["name"] = "保守精确匹配加权"
    strategy["config"]["coverage_boost"] = 0.2
    strategy["config"]["numeric_boost"] = 0.3
    strategy["config"]["phrase_boost"] = 0.3
    strategy["explanation"] = {"target_problem": "词面匹配不足"}
    if config_sha256 is not None:
        strategy["config_sha256"] = config_sha256
    active_path = _write_active_strategy(project, active)

    with pytest.raises(ValueError, match=error):
        generate_strategy_proposal(
            project_root=project,
            revision_provider=lambda _root: "a" * 40,
        )

    assert json.loads(active_path.read_text(encoding="utf-8")) == active


def test_agent_generates_real_strategy_proposal_artifacts(tmp_path: Path) -> None:
    project = _copy_smoke_project(tmp_path)

    proposal = generate_strategy_proposal(
        project_root=project,
        revision_provider=lambda _root: "a" * 40,
    )

    assert proposal["schema_version"] == "strategy-proposal-v2"
    assert proposal["status"] == "pending"
    assert proposal["agent_summary"]["recommendation"] == "update_strategy"
    assert proposal["strategy"]["strategy_id"] == "exact-conservative-v1"
    assert proposal["strategy"]["catalog_entry"]["name"] == "保守精确匹配加权"
    assert "最终得分" in proposal["strategy"]["explanation"]["scoring_formula"]
    assert [
        item["label"] for item in proposal["strategy"]["explanation"]["parameters"]
    ] == ["查询词覆盖加权", "型号与数字词加权", "完整短语加权"]
    assert proposal["strategy"]["config"]["ranker_id"] == (
        "candidate-title-bm25-exact-boost-v1"
    )
    assert proposal["release_gate"]["passed"] is True
    assert proposal["experiment"]["winner_selection"]["winner_candidate_id"] == (
        "exact-conservative-v1"
    )
    assert len(proposal["experiment"]["evaluations"]) >= 2
    for evaluation in proposal["experiment"]["evaluations"]:
        assert set(evaluation["metrics"]) == {"success@5", "mrr@10", "ndcg@10"}
        assert evaluation["catalog_entry"]["name"]
        assert evaluation["explanation"]["target_problem"]
        assert evaluation["explanation"]["expected_benefit"]
        assert evaluation["explanation"]["risk"]
        for values in evaluation["metrics"].values():
            assert 0.0 <= values["baseline"] <= 1.0
            assert 0.0 <= values["candidate"] <= 1.0
            assert values["delta"] == pytest.approx(
                values["candidate"] - values["baseline"]
            )
    assert proposal["analysis"]["root_cause_counts"]["coverage_gap"] >= 1
    assert proposal["model_usage"] == {
        "calls": 0,
        "estimated_cost_usd": 0.0,
        "mode": "deterministic",
        "provider_id": None,
    }
    assert proposal["evidence"]["aggregate_metrics"]["ndcg@10"]["delta"] > 0
    assert proposal["evidence"]["bad_cases"]
    query_comparisons = proposal["evidence"]["query_comparisons"]
    assert 0 < len(query_comparisons) <= 10
    assert {item["outcome"] for item in query_comparisons} <= {
        "improvement",
        "regression",
    }
    ndcg_outcomes = proposal["evidence"]["outcome_counts"]["ndcg@10"]
    assert len(query_comparisons) == min(
        10,
        ndcg_outcomes["improved"] + ndcg_outcomes["regressed"],
    )
    if ndcg_outcomes["improved"] and ndcg_outcomes["regressed"]:
        assert {item["outcome"] for item in query_comparisons} == {
            "improvement",
            "regression",
        }
    assert all(
        set(item["metrics"]) == {"success@5", "mrr@10", "ndcg@10"}
        for item in query_comparisons
    )
    for comparison in query_comparisons:
        for values in comparison["metrics"].values():
            assert values["delta"] == pytest.approx(
                values["candidate"] - values["baseline"]
            )
    assert [abs(item["ndcg@10_delta"]) for item in query_comparisons] == sorted(
        (abs(item["ndcg@10_delta"]) for item in query_comparisons),
        reverse=True,
    )
    assert all(len(item["top_baseline"]) == 10 for item in query_comparisons)
    assert all(len(item["top_candidate"]) == 10 for item in query_comparisons)
    assert all(
        result["title"]
        for item in query_comparisons
        for result in item["top_baseline"] + item["top_candidate"]
    )
    assert (
        project / "runs" / "strategy-proposals" / f"{proposal['proposal_id']}.json"
    ).is_file()


def test_unaddressable_diagnosis_returns_engineering_terminal_instead_of_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _copy_smoke_project(tmp_path)
    monkeypatch.setattr(
        "search_quality.agent.optimization.select_exact_boost_candidates",
        lambda _diagnoses, *, max_candidates: CandidateSelection(
            selection_id="candidate-selection-aaaaaaaaaaaa",
            diagnosis_count=20,
            candidates=[],
        ),
    )

    proposal = generate_strategy_proposal(
        project_root=project,
        revision_provider=lambda _root: "a" * 40,
    )

    assert proposal["status"] == "terminal"
    assert proposal["terminal_status"] == "requires_engineering"
    assert proposal["agent_summary"]["recommendation"] == "requires_engineering"
    assert proposal["candidate_run_id"] is None
    assert proposal["comparison_id"] is None
    assert proposal["experiment"]["evaluations"] == []
    assert proposal["release_gate"]["passed"] is False
    with pytest.raises(ValueError, match="only pending proposals"):
        apply_strategy_decision(
            project_root=project,
            proposal_id=proposal["proposal_id"],
            decision="approve",
        )


def test_no_findings_terminal_does_not_invent_a_root_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _copy_smoke_project(tmp_path)
    monkeypatch.setattr(
        optimization, "_diagnose_baseline", lambda *_args, **_kwargs: []
    )

    proposal = generate_strategy_proposal(
        project_root=project,
        revision_provider=lambda _root: TEST_CODE_REVISION,
    )

    assert proposal["status"] == "terminal"
    assert proposal["analysis"]["diagnosis_count"] == 0
    assert not any(proposal["analysis"]["root_cause_counts"].values())
    assert proposal["strategy"]["target_root_cause"] is None
    assert (
        "不能凭空选择策略方向" in proposal["strategy"]["explanation"]["target_problem"]
    )


def test_agent_artifacts_can_live_outside_read_only_project(tmp_path: Path) -> None:
    project = _copy_smoke_project(tmp_path)
    artifact_root = tmp_path / "runtime"
    artifact_root.mkdir()

    proposal = generate_strategy_proposal(
        project_root=project,
        artifact_root=artifact_root,
        revision_provider=lambda _root: "a" * 40,
    )
    decision = apply_strategy_decision(
        project_root=project,
        artifact_root=artifact_root,
        proposal_id=proposal["proposal_id"],
        decision="approve",
    )
    catalog = load_strategy_catalog(
        project_root=project,
        artifact_root=artifact_root,
    )

    assert not (project / "runs").exists()
    assert (
        artifact_root / "strategy-proposals" / f"{proposal['proposal_id']}.json"
    ).is_file()
    assert (artifact_root / "search-strategies" / "active.json").is_file()
    assert decision["active_strategy_path"] == "runs/search-strategies/active.json"
    assert catalog["active_strategy_id"] == "exact-conservative-v1"


def test_strategy_decision_approve_updates_catalog_and_is_idempotent(
    tmp_path: Path,
) -> None:
    project = _copy_smoke_project(tmp_path)
    proposal = generate_strategy_proposal(
        project_root=project,
        revision_provider=lambda _root: "a" * 40,
    )

    decision = apply_strategy_decision(
        project_root=project,
        proposal_id=proposal["proposal_id"],
        decision="approve",
    )

    def fail_if_revision_is_reloaded(_root: Path) -> str:
        raise AssertionError(
            "an existing decision must be returned before revision load"
        )

    second = _apply_strategy_decision(
        project_root=project,
        proposal_id=proposal["proposal_id"],
        decision="approve",
        revision_provider=fail_if_revision_is_reloaded,
    )

    catalog = load_strategy_catalog(project_root=project)
    assert second == decision
    assert decision["applied"] is True
    assert decision["active_strategy_path"] == "runs/search-strategies/active.json"
    assert catalog["active_strategy_id"] == "exact-conservative-v1"
    assert [item["strategy_id"] for item in catalog["strategies"]] == [
        "exact-conservative-v1"
    ]
    assert len(catalog["strategy_history"]) == 1
    history = catalog["strategy_history"][0]
    assert history["strategy_id"] == "exact-conservative-v1"
    assert history["proposal_id"] == proposal["proposal_id"]
    assert history["decision_id"] == decision["decision_id"]
    assert history["config"] == proposal["strategy"]["config"]
    assert set(history["metrics"]) == {"success@5", "mrr@10", "ndcg@10"}
    assert history["adopted_at"].endswith("Z")
    assert catalog["strategy_activity_logs"] == [
        {
            "decision_id": decision["decision_id"],
            "event_id": f"activity-{decision['decision_id']}",
            "event_type": "strategy_approved_and_activated",
            "message": "站长批准策略，配置已写入运行目录并成为当时的生效版本。",
            "occurred_at": history["adopted_at"],
            "proposal_id": proposal["proposal_id"],
            "strategy_id": "exact-conservative-v1",
            "strategy_name": proposal["strategy"]["catalog_entry"]["name"],
        }
    ]
    serialized_public_catalog = json.dumps(catalog, ensure_ascii=False).lower()
    assert "query_comparisons" not in serialized_public_catalog
    assert "bad_cases" not in serialized_public_catalog
    assert "query_text" not in serialized_public_catalog
    assert decision["strategy_config_sha256"] == proposal["strategy"]["config_sha256"]
    with pytest.raises(ValueError, match="different decision"):
        apply_strategy_decision(
            project_root=project,
            proposal_id=proposal["proposal_id"],
            decision="reject",
        )


def test_strategy_decision_recovers_after_pointer_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _copy_smoke_project(tmp_path)
    proposal = generate_strategy_proposal(
        project_root=project,
        revision_provider=lambda _root: "a" * 40,
    )
    original_atomic_write = optimization.atomic_write_text
    pointer_failed = False

    def fail_first_pointer_write(path: Path, contents: str) -> None:
        nonlocal pointer_failed
        if "by-proposal" in path.parts and not pointer_failed:
            pointer_failed = True
            raise OSError("simulated decision pointer failure")
        original_atomic_write(path, contents)

    monkeypatch.setattr(optimization, "atomic_write_text", fail_first_pointer_write)

    with pytest.raises(OSError, match="pointer failure"):
        apply_strategy_decision(
            project_root=project,
            proposal_id=proposal["proposal_id"],
            decision="approve",
        )

    run_store = project / "runs"
    pointer_path = (
        run_store
        / "strategy-decisions"
        / "by-proposal"
        / f"{proposal['proposal_id']}.json"
    )
    intent_path = (
        run_store / "strategy-decisions" / "intents" / f"{proposal['proposal_id']}.json"
    )
    assert (run_store / "search-strategies" / "active.json").is_file()
    assert intent_path.is_file()
    assert not pointer_path.exists()

    recovered = apply_strategy_decision(
        project_root=project,
        proposal_id=proposal["proposal_id"],
        decision="approve",
    )
    repeated = apply_strategy_decision(
        project_root=project,
        proposal_id=proposal["proposal_id"],
        decision="approve",
    )

    assert pointer_failed is True
    assert recovered == repeated
    assert json.loads(pointer_path.read_text(encoding="utf-8")) == recovered
    assert len(list((run_store / "strategy-decisions").glob("decision-*.json"))) == 1
    catalog = load_strategy_catalog(project_root=project)
    assert catalog["active"]["applied_from_proposal_id"] == proposal["proposal_id"]
    assert len(catalog["strategies"]) == 1


def test_strategy_decision_intent_prevents_conflict_and_recovers_before_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _copy_smoke_project(tmp_path)
    proposal = generate_strategy_proposal(
        project_root=project,
        revision_provider=lambda _root: "a" * 40,
    )
    original_write_catalog = optimization._write_strategy_catalog
    activation_failed = False

    def fail_first_activation(
        run_store: Path, payload: dict, decision_payload: dict
    ) -> str:
        nonlocal activation_failed
        if not activation_failed:
            activation_failed = True
            raise OSError("simulated activation failure")
        return original_write_catalog(run_store, payload, decision_payload)

    monkeypatch.setattr(
        optimization,
        "_write_strategy_catalog",
        fail_first_activation,
    )

    with pytest.raises(OSError, match="activation failure"):
        apply_strategy_decision(
            project_root=project,
            proposal_id=proposal["proposal_id"],
            decision="approve",
        )
    with pytest.raises(ValueError, match="different decision"):
        apply_strategy_decision(
            project_root=project,
            proposal_id=proposal["proposal_id"],
            decision="reject",
        )

    recovered = apply_strategy_decision(
        project_root=project,
        proposal_id=proposal["proposal_id"],
        decision="approve",
    )

    assert activation_failed is True
    assert recovered["applied"] is True
    assert load_strategy_catalog(project_root=project)["active_strategy_id"] == (
        "exact-conservative-v1"
    )


def test_next_optimizer_run_uses_the_approved_strategy_as_its_baseline(
    tmp_path: Path,
) -> None:
    project = _copy_smoke_project(tmp_path)
    first = generate_strategy_proposal(
        project_root=project,
        revision_provider=lambda _root: "a" * 40,
    )
    apply_strategy_decision(
        project_root=project,
        proposal_id=first["proposal_id"],
        decision="approve",
    )
    catalog = load_strategy_catalog(project_root=project)

    second = generate_strategy_proposal(
        project_root=project,
        revision_provider=lambda _root: "a" * 40,
    )

    assert second["parent_active_strategy_id"] == "exact-conservative-v1"
    assert second["parent_active_strategy_revision"] == catalog["active_revision"]
    assert second["baseline_run_id"] == first["candidate_run_id"]
    assert second["candidate_run_id"] != second["baseline_run_id"]
    evaluated_ids = {
        item["candidate"]["candidate_id"]
        for item in second["experiment"]["evaluations"]
    }
    assert "exact-conservative-v1" not in evaluated_ids


def test_cross_process_decision_lock_allows_only_one_same_parent_approval(
    tmp_path: Path,
) -> None:
    project = _copy_smoke_project(tmp_path)
    proposals = [
        generate_strategy_proposal(
            project_root=project,
            revision_provider=lambda _root, revision=revision: revision,
        )
        for revision in ("a" * 40, "b" * 40)
    ]
    assert proposals[0]["proposal_id"] != proposals[1]["proposal_id"]
    assert all(item["parent_active_strategy_revision"] is None for item in proposals)

    context = multiprocessing.get_context("fork")
    start_event = context.Event()
    result_queue = context.Queue()
    revisions = ("a" * 40, "b" * 40)
    processes = [
        context.Process(
            target=_apply_decision_worker,
            args=(
                str(project),
                proposal["proposal_id"],
                revision,
                start_event,
                result_queue,
            ),
        )
        for proposal, revision in zip(proposals, revisions, strict=True)
    ]
    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    results = [result_queue.get(timeout=5) for _ in processes]
    applied = [item for item in results if item[0] == "applied"]
    rejected = [item for item in results if item[0] == "error"]
    assert len(applied) == 1
    assert len(rejected) == 1
    assert "stale relative to the active strategy" in rejected[0][2]

    winning_proposal_id = applied[0][1]
    catalog = load_strategy_catalog(project_root=project)
    assert catalog["active"]["applied_from_proposal_id"] == winning_proposal_id
    assert catalog["strategies"] == [
        {
            **proposals[0]["strategy"]["catalog_entry"],
            "comparison_id": next(
                item["comparison_id"]
                for item in proposals
                if item["proposal_id"] == winning_proposal_id
            ),
            "proposal_id": winning_proposal_id,
            "strategy_id": "exact-conservative-v1",
        }
    ]
    decision_pointers = list(
        (project / "runs" / "strategy-decisions" / "by-proposal").glob("*.json")
    )
    assert [path.stem for path in decision_pointers] == [winning_proposal_id]


def test_strategy_decision_reject_records_without_updating_catalog(
    tmp_path: Path,
) -> None:
    project = _copy_smoke_project(tmp_path)
    proposal = generate_strategy_proposal(
        project_root=project,
        revision_provider=lambda _root: "a" * 40,
    )

    decision = apply_strategy_decision(
        project_root=project,
        proposal_id=proposal["proposal_id"],
        decision="reject",
    )

    assert decision["applied"] is False
    assert load_strategy_catalog(project_root=project)["strategies"] == []


def test_strategy_approval_rejects_failed_release_gates(tmp_path: Path) -> None:
    project = _copy_smoke_project(tmp_path)
    proposal = generate_strategy_proposal(
        project_root=project,
        gate_policy=GatePolicy(min_ndcg_at_10_delta=0.5),
        revision_provider=lambda _root: "a" * 40,
    )

    assert proposal["agent_summary"]["recommendation"] == "continue_experiment"
    assert proposal["release_gate"]["passed"] is False
    with pytest.raises(ValueError, match="gate-passing"):
        apply_strategy_decision(
            project_root=project,
            proposal_id=proposal["proposal_id"],
            decision="approve",
        )


def test_strategy_approval_recomputes_with_trusted_gate_policy(tmp_path: Path) -> None:
    project = _copy_smoke_project(tmp_path)
    proposal = generate_strategy_proposal(
        project_root=project,
        gate_policy=GatePolicy(
            min_ndcg_at_10_delta=-1.0,
            min_ndcg_at_5_delta=-1.0,
            min_mrr_at_10_delta=-1.0,
            min_success_at_1_delta=-1.0,
            min_success_at_5_delta=-1.0,
            max_ndcg_at_10_regression_rate=1.0,
            max_worst_ndcg_at_10_regression_magnitude=1.0,
        ),
        revision_provider=lambda _root: "a" * 40,
    )

    assert proposal["release_gate"]["passed"] is True
    with pytest.raises(ValueError, match="trusted smoke gate policy"):
        apply_strategy_decision(
            project_root=project,
            proposal_id=proposal["proposal_id"],
            decision="approve",
        )
    _assert_no_strategy_activation(project, proposal["proposal_id"])


def test_strategy_approval_rejects_tampered_proposal(tmp_path: Path) -> None:
    project = _copy_smoke_project(tmp_path)
    proposal = generate_strategy_proposal(
        project_root=project,
        revision_provider=lambda _root: "a" * 40,
    )
    proposal_path = (
        project / "runs" / "strategy-proposals" / f"{proposal['proposal_id']}.json"
    )
    tampered = json.loads(proposal_path.read_text(encoding="utf-8"))
    tampered["strategy"]["config"]["coverage_boost"] = 2.9
    proposal_path.write_text(
        json.dumps(tampered, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="content does not match"):
        apply_strategy_decision(
            project_root=project,
            proposal_id=proposal["proposal_id"],
            decision="approve",
        )


def test_strategy_approval_rejects_evidence_from_an_old_code_revision(
    tmp_path: Path,
) -> None:
    project = _copy_smoke_project(tmp_path)
    proposal = generate_strategy_proposal(
        project_root=project,
        revision_provider=lambda _root: TEST_CODE_REVISION,
    )

    with pytest.raises(ValueError, match="current deployment"):
        _apply_strategy_decision(
            project_root=project,
            proposal_id=proposal["proposal_id"],
            decision="approve",
            revision_provider=lambda _root: "b" * 40,
        )

    _assert_no_strategy_activation(project, proposal["proposal_id"])


def test_strategy_approval_requires_the_complete_canonical_ranker_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _copy_smoke_project(tmp_path)
    proposal = generate_strategy_proposal(
        project_root=project,
        revision_provider=lambda _root: TEST_CODE_REVISION,
    )
    monkeypatch.setattr(
        optimization,
        "_canonical_candidate_ranker_config",
        lambda _candidate: {"ranker_id": "candidate-title-bm25-exact-boost-v1"},
    )

    with pytest.raises(ValueError, match="selected strategy config"):
        apply_strategy_decision(
            project_root=project,
            proposal_id=proposal["proposal_id"],
            decision="approve",
        )

    _assert_no_strategy_activation(project, proposal["proposal_id"])


@pytest.mark.parametrize(
    ("run_id_field", "config_key", "tampered_value", "role"),
    [
        ("baseline_run_id", "k1", 1.6, "baseline"),
        ("candidate_run_id", "coverage_boost", 2.9, "candidate"),
    ],
)
def test_strategy_approval_rejects_tampered_run_without_partial_activation(
    tmp_path: Path,
    run_id_field: str,
    config_key: str,
    tampered_value: float,
    role: str,
) -> None:
    project = _copy_smoke_project(tmp_path)
    proposal = generate_strategy_proposal(
        project_root=project,
        revision_provider=lambda _root: "a" * 40,
    )
    run_path = project / "runs" / f"{proposal[run_id_field]}.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["ranker"][config_key] = tampered_value
    run_path.write_text(
        json.dumps(run, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError, match=rf"{role} Run content does not match its Run ID"
    ):
        apply_strategy_decision(
            project_root=project,
            proposal_id=proposal["proposal_id"],
            decision="approve",
        )
    _assert_no_strategy_activation(project, proposal["proposal_id"])


def test_strategy_approval_rejects_tampered_comparison_without_partial_activation(
    tmp_path: Path,
) -> None:
    project = _copy_smoke_project(tmp_path)
    proposal = generate_strategy_proposal(
        project_root=project,
        revision_provider=lambda _root: "a" * 40,
    )
    comparison_path = (
        project / "runs" / "comparisons" / f"{proposal['comparison_id']}.json"
    )
    comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
    comparison["aggregate_metrics"]["ndcg@10"]["delta"] = 0.99
    comparison_path.write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError, match="stored comparison does not match trusted Run evidence"
    ):
        apply_strategy_decision(
            project_root=project,
            proposal_id=proposal["proposal_id"],
            decision="approve",
        )
    _assert_no_strategy_activation(project, proposal["proposal_id"])


def test_strategy_approval_rejects_stale_active_parent(tmp_path: Path) -> None:
    project = _copy_smoke_project(tmp_path)
    proposal = generate_strategy_proposal(
        project_root=project,
        revision_provider=lambda _root: "a" * 40,
    )
    active_path = project / "runs" / "search-strategies" / "active.json"
    active_path.parent.mkdir(parents=True)
    active_path.write_text(
        json.dumps(
            {"strategy": {"strategy_id": "another-approved-strategy"}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="stale"):
        apply_strategy_decision(
            project_root=project,
            proposal_id=proposal["proposal_id"],
            decision="approve",
        )


def test_strategy_approval_rejects_same_id_with_a_different_parent_revision(
    tmp_path: Path,
) -> None:
    project = _copy_smoke_project(tmp_path)
    first = generate_strategy_proposal(
        project_root=project,
        revision_provider=lambda _root: "a" * 40,
    )
    apply_strategy_decision(
        project_root=project,
        proposal_id=first["proposal_id"],
        decision="approve",
    )
    second = generate_strategy_proposal(
        project_root=project,
        revision_provider=lambda _root: "a" * 40,
    )
    active_path = project / "runs" / "search-strategies" / "active.json"
    active = json.loads(active_path.read_text(encoding="utf-8"))
    assert active["strategy"]["strategy_id"] == "exact-conservative-v1"
    active["comparison_id"] = "comparison-ffffffffffff"
    active_path.write_text(
        json.dumps(active, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="stale"):
        apply_strategy_decision(
            project_root=project,
            proposal_id=second["proposal_id"],
            decision="approve",
        )
    assert not (
        project
        / "runs"
        / "strategy-decisions"
        / "by-proposal"
        / f"{second['proposal_id']}.json"
    ).exists()


def test_api_strategy_routes_return_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEARCH_AGENT_ARTIFACT_ROOT", raising=False)
    monkeypatch.delenv("SEARCH_CODE_REVISION", raising=False)
    monkeypatch.setattr(
        api,
        "generate_strategy_proposal",
        lambda **_kwargs: {"proposal_id": "proposal-aaaaaaaaaaaa"},
    )
    decision_calls: list[dict] = []

    def decide(**kwargs):
        decision_calls.append(kwargs)
        return {"decision_id": "decision-bbbbbbbbbbbb"}

    monkeypatch.setattr(api, "apply_strategy_decision", decide)
    monkeypatch.setattr(
        api,
        "load_strategy_catalog",
        lambda **_kwargs: {"schema_version": "search-strategy-catalog-v1"},
    )

    assert api.agent_strategy_propose(api.StrategyProposalRequest()) == {
        "proposal_id": "proposal-aaaaaaaaaaaa"
    }
    assert api.agent_strategy_decision(
        _request(),
        api.StrategyDecisionRequest(
            proposal_id="proposal-aaaaaaaaaaaa",
            decision="approve",
        ),
    ) == {"decision_id": "decision-bbbbbbbbbbbb"}
    assert decision_calls[0]["revision_provider"] is api._api_code_revision
    assert api.agent_strategy_catalog() == {
        "schema_version": "search-strategy-catalog-v1"
    }


def test_api_uses_external_artifacts_and_deployment_revision_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_root = tmp_path / "runtime"
    artifact_root.mkdir()
    revision = "b" * 40
    calls: list[dict] = []
    monkeypatch.setenv("SEARCH_AGENT_ARTIFACT_ROOT", str(artifact_root))
    monkeypatch.setenv("SEARCH_CODE_REVISION", revision)
    api._AGENT_PROPOSAL_CACHE.clear()

    def proposal(**kwargs):
        calls.append(kwargs)
        return {"proposal_id": "proposal-bbbbbbbbbbbb"}

    monkeypatch.setattr(api, "generate_strategy_proposal", proposal)

    first = api.agent_strategy_propose(api.StrategyProposalRequest())
    second = api.agent_strategy_propose(api.StrategyProposalRequest())

    assert first == second == {"proposal_id": "proposal-bbbbbbbbbbbb"}
    assert len(calls) == 1
    assert calls[0]["artifact_root"] == artifact_root.resolve()
    assert calls[0]["revision_provider"](PROJECT_ROOT) == revision


def test_api_proposal_cache_tracks_active_parent_and_clears_after_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_root = tmp_path / "runtime"
    artifact_root.mkdir()
    revision = "c" * 40
    active_strategy_id: str | None = None
    active_revision: str | None = None
    proposal_calls: list[str | None] = []
    monkeypatch.setenv("SEARCH_AGENT_ARTIFACT_ROOT", str(artifact_root))
    monkeypatch.setenv("SEARCH_CODE_REVISION", revision)
    api._AGENT_PROPOSAL_CACHE.clear()

    def catalog(**_kwargs):
        return {
            "active_revision": active_revision,
            "active_strategy_id": active_strategy_id,
        }

    def proposal(**_kwargs):
        proposal_calls.append(active_revision)
        return {
            "parent_active_strategy_revision": active_revision,
            "proposal_id": f"proposal-{len(proposal_calls):012x}",
        }

    def decision(**_kwargs):
        nonlocal active_revision
        active_revision = "e" * 64
        return {"decision_id": "decision-cccccccccccc"}

    monkeypatch.setattr(api, "load_strategy_catalog", catalog)
    monkeypatch.setattr(api, "generate_strategy_proposal", proposal)
    monkeypatch.setattr(
        api,
        "apply_strategy_decision",
        decision,
    )

    first = api.agent_strategy_propose(api.StrategyProposalRequest())
    assert api.agent_strategy_propose(api.StrategyProposalRequest()) == first
    assert proposal_calls == [None]

    active_strategy_id = "exact-conservative-v1"
    active_revision = "d" * 64
    second = api.agent_strategy_propose(api.StrategyProposalRequest())
    assert second != first
    assert proposal_calls == [None, "d" * 64]

    api.agent_strategy_decision(
        _request(),
        api.StrategyDecisionRequest(
            proposal_id=second["proposal_id"],
            decision="approve",
        ),
    )
    assert api._AGENT_PROPOSAL_CACHE == {}
    third = api.agent_strategy_propose(api.StrategyProposalRequest())
    assert third != second
    assert third["parent_active_strategy_revision"] == "e" * 64
    assert proposal_calls == [None, "d" * 64, "e" * 64]
    assert api.agent_strategy_propose(api.StrategyProposalRequest()) == third


def test_api_retries_when_active_parent_changes_during_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_root = tmp_path / "runtime"
    artifact_root.mkdir()
    active_revision = "a" * 64
    proposal_calls: list[str] = []
    monkeypatch.setenv("SEARCH_AGENT_ARTIFACT_ROOT", str(artifact_root))
    monkeypatch.setenv("SEARCH_CODE_REVISION", "f" * 40)
    api._AGENT_PROPOSAL_CACHE.clear()

    def catalog(**_kwargs):
        return {"active_revision": active_revision}

    def proposal(**_kwargs):
        nonlocal active_revision
        proposal_calls.append(active_revision)
        if len(proposal_calls) == 1:
            active_revision = "b" * 64
            raise api.ActiveStrategyChangedError("simulated active change")
        return {
            "parent_active_strategy_revision": active_revision,
            "proposal_id": f"proposal-{len(proposal_calls):012x}",
        }

    monkeypatch.setattr(api, "load_strategy_catalog", catalog)
    monkeypatch.setattr(api, "generate_strategy_proposal", proposal)

    result = api.agent_strategy_propose(api.StrategyProposalRequest())

    assert proposal_calls == ["a" * 64, "b" * 64]
    assert result["parent_active_strategy_revision"] == "b" * 64
    assert len(api._AGENT_PROPOSAL_CACHE) == 1
    assert next(iter(api._AGENT_PROPOSAL_CACHE))[3] == "b" * 64


def test_api_retries_active_change_without_a_deployment_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_root = tmp_path / "runtime"
    artifact_root.mkdir()
    calls = 0
    monkeypatch.setenv("SEARCH_AGENT_ARTIFACT_ROOT", str(artifact_root))
    monkeypatch.delenv("SEARCH_CODE_REVISION", raising=False)

    def proposal(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise api.ActiveStrategyChangedError("simulated active change")
        return {"proposal_id": "proposal-aaaaaaaaaaaa"}

    monkeypatch.setattr(api, "generate_strategy_proposal", proposal)

    assert api.agent_strategy_propose(api.StrategyProposalRequest()) == {
        "proposal_id": "proposal-aaaaaaaaaaaa"
    }
    assert calls == 2


def test_api_rejects_public_strategy_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def decision(**_kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(api, "apply_strategy_decision", decision)
    public_request = _request(
        headers=[(b"x-forwarded-for", b"203.0.113.10")],
    )
    with pytest.raises(HTTPException) as captured:
        api.agent_strategy_decision(
            public_request,
            api.StrategyDecisionRequest(
                proposal_id="proposal-aaaaaaaaaaaa",
                decision="approve",
            ),
        )

    assert captured.value.status_code == 404
    assert called is False


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("private strategy runtime detail"),
        ValueError("private strategy contract detail"),
    ],
    ids=["runtime", "contract-value-error"],
)
def test_api_strategy_proposal_failure_is_safe(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    stream = io.StringIO()
    configure_logging(
        default_level="WARNING",
        module_levels={"api": "INFO"},
        stream=stream,
    )

    def fail_proposal(**_kwargs):
        raise failure

    monkeypatch.setattr(api, "generate_strategy_proposal", fail_proposal)
    with logging_context(trace_id="strategy-safe-1"):
        with pytest.raises(HTTPException) as captured:
            api.agent_strategy_propose(api.StrategyProposalRequest())

    assert captured.value.status_code == 503
    assert captured.value.detail == {
        "code": "strategy_proposal_unavailable",
        "message": "Strategy proposal workflow unavailable",
        "trace_id": "strategy-safe-1",
    }
    assert str(failure) not in stream.getvalue()
    event = json.loads(stream.getvalue())
    assert event["event"] == "agent_strategy_proposal_failed"
    assert event["error_type"] == type(failure).__name__
    assert event["level"] == "ERROR"


def test_api_expected_strategy_proposal_rejection_remains_a_safe_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = io.StringIO()
    configure_logging(
        default_level="WARNING",
        module_levels={"api": "DEBUG"},
        stream=stream,
    )

    def reject_proposal(**_kwargs):
        raise api.StrategyProposalRejectedError("private rejected request detail")

    monkeypatch.setattr(api, "generate_strategy_proposal", reject_proposal)
    with logging_context(trace_id="strategy-rejected-1"):
        with pytest.raises(HTTPException) as captured:
            api.agent_strategy_propose(api.StrategyProposalRequest())

    assert captured.value.status_code == 400
    assert captured.value.detail == {
        "code": "invalid_strategy_proposal_request",
        "message": "Strategy proposal request is invalid",
        "trace_id": "strategy-rejected-1",
    }
    assert "private rejected request detail" not in stream.getvalue()
    event = json.loads(stream.getvalue())
    assert event["event"] == "agent_strategy_proposal_rejected"
    assert event["level"] == "DEBUG"


def test_strategy_optimizer_logs_without_query_text(tmp_path: Path) -> None:
    project = _copy_smoke_project(tmp_path)
    stream = io.StringIO()
    configure_logging(
        default_level="WARNING",
        module_levels={"agent_optimization": "INFO"},
        stream=stream,
    )

    with logging_context(trace_id="strategy-trace"):
        proposal = generate_strategy_proposal(
            project_root=project,
            revision_provider=lambda _root: "a" * 40,
        )

    assert proposal["evidence"]["bad_cases"]
    assert "07 nissan" not in stream.getvalue()
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    event_names = [event["event"] for event in events]
    assert event_names[0] == "strategy_proposal_started"
    assert event_names[-1] == "strategy_proposal_completed"
    assert event_names.count("bad_case_diagnosed") == 20
    assert event_names.count("strategy_candidates_selected") == 1
    assert event_names.count("strategy_comparison_scored") >= 2
    assert event_names.count("strategy_winner_selected") == 1
    assert all(event["trace_id"] == "strategy-trace" for event in events)
    assert events[-1]["query_comparison_count"] == len(
        proposal["evidence"]["query_comparisons"]
    )
