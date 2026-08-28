from __future__ import annotations

import json
from pathlib import Path

import pytest

import search_quality.agent.tools as agent_tools_module
from search_quality.agent.errors import AgentToolError
from search_quality.agent.registry import AgentToolRegistry, ToolSpec
from search_quality.agent.tools import (
    CompareRunsInput,
    CompareRunsOutput,
    SearchEvaluationTools,
    TrustedRunRegistry,
)
from search_quality.evaluation.baseline import run_candidate_baseline
from search_quality.evaluation.datasets import EvaluationProfile
from search_quality.evaluation.relevance import RelevancePolicy

ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "data/manifests/esci-stage1.json"
POLICY = ROOT / "configs/evaluation/esci-primary-v1.json"


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


def _write_runs(store: Path, runs: tuple[dict, dict]) -> tuple[str, str]:
    store.mkdir()
    for run in runs:
        (store / f"{run['run_id']}.json").write_text(json.dumps(run), encoding="utf-8")
    return runs[0]["run_id"], runs[1]["run_id"]


def _tools(
    tmp_path: Path, runs: tuple[dict, dict]
) -> tuple[SearchEvaluationTools, TrustedRunRegistry, str, str]:
    store = tmp_path / "runs"
    baseline_id, candidate_id = _write_runs(store, runs)
    registry = TrustedRunRegistry(
        store_root=store,
        project_root=ROOT,
        manifest_path=MANIFEST,
        allowed_run_ids=(baseline_id, candidate_id),
    )
    tools = SearchEvaluationTools(
        project_root=ROOT,
        registry=registry,
        revision_provider=lambda _root: "c" * 40,
    )
    return tools, registry, baseline_id, candidate_id


def test_registry_requires_explicit_admission_and_detects_tampering(
    tmp_path: Path, smoke_runs: tuple[dict, dict]
) -> None:
    store = tmp_path / "runs"
    baseline_id, candidate_id = _write_runs(store, smoke_runs)
    registry = TrustedRunRegistry(
        store_root=store,
        project_root=ROOT,
        manifest_path=MANIFEST,
        allowed_run_ids=(baseline_id,),
    )
    assert registry.resolve(baseline_id).payload["run_id"] == baseline_id
    with pytest.raises(AgentToolError) as not_trusted:
        registry.resolve(candidate_id)
    assert not_trusted.value.code == "run_not_trusted"

    path = store / f"{baseline_id}.json"
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(AgentToolError) as tampered:
        registry.resolve(baseline_id)
    assert tampered.value.code == "run_integrity_failed"


def test_tool_schema_rejects_extra_fields_before_handler(
    tmp_path: Path, smoke_runs: tuple[dict, dict]
) -> None:
    tools, _registry, baseline_id, _candidate_id = _tools(tmp_path, smoke_runs)
    registry = tools.build_registry()
    with pytest.raises(AgentToolError) as captured:
        registry.execute(
            "evaluate_run",
            {"run_id": baseline_id, "path": "../../private"},
            allowed_capabilities=frozenset({"read_smoke_run"}),
        )
    assert captured.value.code == "invalid_argument"


def test_compare_tool_rejects_a_self_comparison_before_handler() -> None:
    called = False

    def handler(_request):
        nonlocal called
        called = True
        return {}

    registry = AgentToolRegistry(
        (
            ToolSpec(
                name="compare_runs",
                capability="compare_smoke_runs",
                input_model=CompareRunsInput,
                output_model=CompareRunsOutput,
                handler=handler,
            ),
        )
    )
    with pytest.raises(AgentToolError) as captured:
        registry.execute(
            "compare_runs",
            {
                "baseline_run_id": "random-aaaaaaaaaaaa",
                "candidate_run_id": "random-aaaaaaaaaaaa",
            },
            allowed_capabilities=frozenset({"compare_smoke_runs"}),
        )

    assert captured.value.code == "invalid_argument"
    assert called is False


@pytest.mark.parametrize("prefix", ["../", "/tmp/", "."])
def test_tool_schema_requires_the_entire_run_id_to_match(
    tmp_path: Path,
    smoke_runs: tuple[dict, dict],
    prefix: str,
) -> None:
    tools, _registry, baseline_id, _candidate_id = _tools(tmp_path, smoke_runs)
    with pytest.raises(AgentToolError) as captured:
        tools.build_registry().execute(
            "evaluate_run",
            {"run_id": f"{prefix}{baseline_id}"},
            allowed_capabilities=frozenset({"read_smoke_run"}),
        )
    assert captured.value.code == "invalid_argument"


@pytest.mark.parametrize(
    "malformed",
    [
        {
            "evidence_ref": "comparison:comparison-cccccccccccc",
            "payload": {"aggregate_metrics": {}, "regressions": []},
        },
        {
            "evidence_ref": "comparison:comparison-cccccccccccc",
            "payload": {
                "aggregate_metrics": {
                    "ndcg@10": {
                        "baseline": 0.5,
                        "candidate": 0.5,
                        "delta": float("nan"),
                    }
                },
                "regressions": [],
            },
        },
    ],
)
def test_tool_registry_rejects_malformed_outputs(malformed: dict) -> None:
    registry = AgentToolRegistry(
        (
            ToolSpec(
                name="compare_runs",
                capability="compare_smoke_runs",
                input_model=CompareRunsInput,
                output_model=CompareRunsOutput,
                handler=lambda _request: malformed,
            ),
        )
    )
    with pytest.raises(AgentToolError) as captured:
        registry.execute(
            "compare_runs",
            {
                "baseline_run_id": "random-aaaaaaaaaaaa",
                "candidate_run_id": "bm25-bbbbbbbbbbbb",
            },
            allowed_capabilities=frozenset({"compare_smoke_runs"}),
        )
    assert captured.value.code == "invalid_tool_result"


def test_evaluate_and_inspect_return_grounded_smoke_evidence(
    tmp_path: Path, smoke_runs: tuple[dict, dict]
) -> None:
    tools, _registry, _baseline_id, candidate_id = _tools(tmp_path, smoke_runs)
    registry = tools.build_registry()
    aggregate = registry.execute(
        "evaluate_run",
        {"run_id": candidate_id},
        allowed_capabilities=frozenset({"read_smoke_run"}),
    )
    assert aggregate["evidence_ref"] == f"run:{candidate_id}"
    assert aggregate["payload"]["query_count"] == 20
    assert aggregate["payload"]["metrics"]["ndcg@10"] == pytest.approx(
        0.7190978854121438
    )

    query_id = 15281
    query = registry.execute(
        "evaluate_run",
        {"run_id": candidate_id, "query_id": query_id},
        allowed_capabilities=frozenset({"read_smoke_run"}),
    )
    detail = registry.execute(
        "inspect_query",
        {"run_id": candidate_id, "query_id": query_id},
        allowed_capabilities=frozenset({"read_smoke_query_evidence"}),
    )
    assert query["evidence_ref"] == f"query:{candidate_id}:{query_id}"
    assert detail["evidence_ref"] == query["evidence_ref"]
    assert detail["payload"]["candidate_count"] == len(detail["payload"]["candidates"])
    assert (
        sum(detail["payload"]["label_counts"].values())
        == detail["payload"]["candidate_count"]
    )
    assert all(item["product_title"] for item in detail["payload"]["candidates"])


def test_compare_tool_returns_bounded_summary_and_persists_full_evidence(
    tmp_path: Path, smoke_runs: tuple[dict, dict]
) -> None:
    tools, registry, baseline_id, candidate_id = _tools(tmp_path, smoke_runs)
    result = tools.build_registry().execute(
        "compare_runs",
        {
            "baseline_run_id": baseline_id,
            "candidate_run_id": candidate_id,
        },
        allowed_capabilities=frozenset({"compare_smoke_runs"}),
    )
    payload = result["payload"]
    assert result["evidence_ref"] == f"comparison:{payload['comparison_id']}"
    assert payload["aggregate_metrics"]["ndcg@10"]["delta"] == pytest.approx(
        0.17331174062776777
    )
    assert len(payload["regressions"]) == 5
    assert payload["regressions"][0]["query_id"] == 15281
    assert "query_text" not in json.dumps(payload)
    assert (
        registry.store_root / "comparisons" / f"{payload['comparison_id']}.json"
    ).is_file()


def test_run_ranker_is_idempotent_and_registers_only_its_generated_run(
    tmp_path: Path,
) -> None:
    store = tmp_path / "runs"
    registry = TrustedRunRegistry(
        store_root=store,
        project_root=ROOT,
        manifest_path=MANIFEST,
    )
    tools = SearchEvaluationTools(
        project_root=ROOT,
        registry=registry,
        revision_provider=lambda _root: "d" * 40,
    )
    tool_registry = tools.build_registry()
    first = tool_registry.execute(
        "run_ranker",
        {"ranker_name": "keyword-overlap"},
        allowed_capabilities=frozenset({"create_smoke_run"}),
    )
    second = tool_registry.execute(
        "run_ranker",
        {"ranker_name": "keyword-overlap"},
        allowed_capabilities=frozenset({"create_smoke_run"}),
    )

    assert first["evidence_ref"] == second["evidence_ref"]
    assert first["payload"]["created"] is True
    assert second["payload"]["created"] is False
    run_id = first["payload"]["run_id"]
    assert registry.allowed_run_ids == {run_id}
    assert registry.resolve(run_id).payload["ranker"]["ranker_id"] == (
        "candidate-title-keyword-overlap-v1"
    )


def test_registry_rejects_a_run_changed_during_validation(
    tmp_path: Path,
    smoke_runs: tuple[dict, dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = tmp_path / "runs"
    baseline_id, _candidate_id = _write_runs(store, smoke_runs)
    registry = TrustedRunRegistry(
        store_root=store,
        project_root=ROOT,
        manifest_path=MANIFEST,
        allowed_run_ids=(baseline_id,),
    )
    original_sha256_file = agent_tools_module.sha256_file
    calls = 0

    def changing_sha256(path: Path) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_sha256_file(path)
        return "0" * 64

    monkeypatch.setattr(agent_tools_module, "sha256_file", changing_sha256)
    with pytest.raises(AgentToolError) as captured:
        registry.resolve(baseline_id)
    assert captured.value.code == "run_integrity_failed"
