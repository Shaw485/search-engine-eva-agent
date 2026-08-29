from __future__ import annotations

import copy

from search_quality.agent.retrieval_examples import select_changed_query_examples


def _query(query_id: int, delta: float) -> dict[str, object]:
    return {
        "baseline_top_results": [],
        "candidate_top_results": [],
        "coarse_ndcg@10_delta": delta,
        "fusion_ndcg@10_delta": delta,
        "locale": "us",
        "query_id": query_id,
        "query_text": f"query {query_id}",
        "recovered_relevant": [],
        "union_coverage_delta": 0.0,
    }


def _experiment(
    variant: str,
    suffix: str,
    queries: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "candidate": {
            "run_id": f"retrieval-{suffix * 12}",
            "pipeline": {"variant": variant},
        },
        "comparison": {
            "comparison_id": f"retrieval-comparison-{suffix * 12}",
            "gate_result": {"passed": suffix == "b"},
            "per_query": queries,
        },
    }


def test_changed_examples_exclude_ties_and_keep_improvement_and_regression() -> None:
    selected_run_id = "retrieval-bbbbbbbbbbbb"
    experiments = [
        _experiment(
            "title-exact-multifield-weighted-v1",
            "b",
            [_query(1, 0.02), _query(2, 0.0)],
        ),
        _experiment(
            "title-exact-multifield-v1",
            "c",
            [
                _query(1, 0.05),
                _query(3, -0.01),
                _query(4, 1e-13),
                _query(5, 1e-12),
                _query(6, -1e-12),
            ],
        ),
    ]

    examples = select_changed_query_examples(
        experiments,
        selected_candidate_run_id=selected_run_id,
    )

    assert {item["outcome"] for item in examples} == {
        "improvement",
        "regression",
    }
    assert {item["query_id"] for item in examples} == {1, 3}
    improvement = next(item for item in examples if item["outcome"] == "improvement")
    assert improvement["candidate_run_id"] == selected_run_id
    assert improvement["coarse_ndcg@10_delta"] == 0.02
    assert improvement["is_selected_comparison"] is True
    assert improvement["gate_passed"] is True
    regression = next(item for item in examples if item["outcome"] == "regression")
    assert regression["pipeline_variant"] == "title-exact-multifield-v1"
    assert regression["is_selected_comparison"] is False
    assert regression["gate_passed"] is False
    assert all(abs(float(item["coarse_ndcg@10_delta"])) > 1e-12 for item in examples)


def test_changed_examples_reserve_the_missing_direction_inside_the_limit() -> None:
    queries = [_query(query_id, 0.1 - query_id / 1000) for query_id in range(1, 13)]
    queries.append(_query(99, -0.001))
    experiments = [
        _experiment("title-exact-multifield-v1", "d", queries),
    ]

    examples = select_changed_query_examples(
        experiments,
        selected_candidate_run_id="retrieval-dddddddddddd",
        limit=10,
    )

    assert len(examples) == 10
    assert {item["outcome"] for item in examples} == {
        "improvement",
        "regression",
    }
    assert any(item["query_id"] == 99 for item in examples)


def test_changed_example_selection_is_independent_of_input_order() -> None:
    experiments = [
        _experiment(
            "title-exact-multifield-weighted-v1",
            "b",
            [_query(1, 0.02), _query(2, 0.0)],
        ),
        _experiment(
            "title-exact-multifield-v1",
            "c",
            [_query(1, 0.05), _query(3, -0.01)],
        ),
    ]
    shuffled = copy.deepcopy(list(reversed(experiments)))
    for experiment in shuffled:
        experiment["comparison"]["per_query"].reverse()

    first = select_changed_query_examples(
        experiments,
        selected_candidate_run_id="retrieval-bbbbbbbbbbbb",
    )
    second = select_changed_query_examples(
        shuffled,
        selected_candidate_run_id="retrieval-bbbbbbbbbbbb",
    )

    assert first == second
