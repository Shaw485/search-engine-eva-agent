from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from search_quality.agent.retrieval_analysis import generate_retrieval_analysis
from search_quality.observability import configure_logging, logging_context

ROOT = Path(__file__).resolve().parents[1]


def test_retrieval_analysis_persists_only_content_addressed_evidence(
    tmp_path: Path,
) -> None:
    first = generate_retrieval_analysis(
        project_root=ROOT,
        artifact_root=tmp_path,
        revision_provider=lambda _root: "c" * 40,
    )
    second = generate_retrieval_analysis(
        project_root=ROOT,
        artifact_root=tmp_path,
        revision_provider=lambda _root: "c" * 40,
    )

    assert first == second
    run_path = tmp_path / "retrieval-runs" / f"{first['retrieval_run_id']}.json"
    diagnosis_path = tmp_path / "stage-diagnoses" / f"{first['diagnosis_id']}.json"
    candidate_path = tmp_path / "retrieval-runs" / f"{first['candidate_run_id']}.json"
    comparison_path = (
        tmp_path / "retrieval-comparisons" / f"{first['comparison_id']}.json"
    )
    assert json.loads(run_path.read_text())["run_id"] == first["retrieval_run_id"]
    assert json.loads(candidate_path.read_text())["run_id"] == first["candidate_run_id"]
    assert (
        json.loads(diagnosis_path.read_text())["diagnosis_id"] == first["diagnosis_id"]
    )
    assert (
        json.loads(comparison_path.read_text())["comparison_id"]
        == first["comparison_id"]
    )
    assert first["status"] == "proposal_ready"
    changed_examples = first["changed_query_examples"]
    assert {item["outcome"] for item in changed_examples} == {
        "improvement",
        "regression",
    }
    assert all(abs(item["coarse_ndcg@10_delta"]) > 1e-12 for item in changed_examples)
    assert all(len(item["baseline_top_results"]) == 10 for item in changed_examples)
    assert all(len(item["candidate_top_results"]) == 10 for item in changed_examples)
    assert any(item["is_selected_comparison"] is True for item in changed_examples)
    assert any(item["is_selected_comparison"] is False for item in changed_examples)
    assert first["proposal"] == {
        "candidate_strategy_id": "multi-field-bm25-weighted-rrf-v1",
        "decision": "request_owner_review",
        "next_action": "request_owner_review",
        "reason": "A bounded RRF weight ablation preserved final quality while expanding closed-pool coverage.",
    }
    assert (
        first["comparison"]["candidate_strategy"]["unique_relevant_contribution"] == 14
    )
    assert first["comparison"]["aggregate_deltas"]["recall_union"][
        "judged_relevant_coverage"
    ]["delta"] == pytest.approx(0.03726951209709828)
    assert len(first["experiments"]) == 3
    assert [item["gate_passed"] for item in first["experiments"]] == [
        False,
        True,
        False,
    ]
    for experiment in first["experiments"]:
        assert (
            tmp_path / "retrieval-runs" / f"{experiment['candidate_run_id']}.json"
        ).is_file()
        assert (
            tmp_path / "retrieval-comparisons" / f"{experiment['comparison_id']}.json"
        ).is_file()
    assert not (tmp_path / "strategy-proposals").exists()
    assert not (tmp_path / "strategy-decisions").exists()
    assert not (tmp_path / "search-strategies").exists()


def test_retrieval_analysis_logging_is_independent_and_private(tmp_path: Path) -> None:
    stream = io.StringIO()
    configure_logging(
        default_level="OFF",
        module_levels={"retrieval_analysis": "INFO"},
        stream=stream,
    )

    with logging_context(trace_id="retrieval-analysis-safe"):
        result = generate_retrieval_analysis(
            project_root=ROOT,
            artifact_root=tmp_path,
            revision_provider=lambda _root: "d" * 40,
        )

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert events
    assert {event["module"] for event in events} == {"retrieval_analysis"}
    assert all(event["trace_id"] == "retrieval-analysis-safe" for event in events)
    serialized = stream.getvalue().lower()
    assert result["comparison"]["per_query"][0]["query_text"].lower() not in serialized
    assert (
        result["comparison"]["per_query"][0]["baseline_top_results"][0][
            "product_id"
        ].lower()
        not in serialized
    )
    regression = next(
        item
        for item in result["changed_query_examples"]
        if item["outcome"] == "regression"
    )
    assert regression["query_text"].lower() not in serialized
    for result_row in (
        regression["baseline_top_results"] + regression["candidate_top_results"]
    ):
        assert result_row["product_id"].lower() not in serialized
        assert result_row["product_title"].lower() not in serialized
    completed = next(
        event
        for event in events
        if event["event"] == "retrieval_analysis_artifacts_stored"
    )
    assert completed["changed_query_example_count"] == 2
    assert completed["improvement_example_count"] == 1
    assert completed["regression_example_count"] == 1
