from __future__ import annotations

import math

import pytest

from search_quality.evaluation.metrics import (
    dcg_at_k,
    mean_ndcg_at_k,
    mean_reciprocal_rank_at_k,
    mean_success_at_k,
    ndcg_at_k,
    reciprocal_rank_at_k,
    success_at_k,
)


def test_dcg_matches_a_hand_calculation() -> None:
    gains = [3.0, 2.0, 1.0, 0.0]
    expected = 3.0 + 2.0 / math.log2(3) + 1.0 / math.log2(4)
    assert dcg_at_k(gains, k=4) == pytest.approx(expected)


def test_ndcg_is_one_for_the_ideal_order() -> None:
    gains = [3.0, 2.0, 1.0, 0.0]
    assert ndcg_at_k(gains, candidate_gains=gains, k=4) == pytest.approx(1.0)


def test_ndcg_penalizes_a_relevant_result_at_a_lower_rank() -> None:
    actual = dcg_at_k([2.0, 0.0, 3.0, 1.0], k=4)
    ideal = dcg_at_k([3.0, 2.0, 1.0, 0.0], k=4)
    assert ndcg_at_k(
        [2.0, 0.0, 3.0, 1.0],
        candidate_gains=[3.0, 2.0, 1.0, 0.0],
        k=4,
    ) == pytest.approx(actual / ideal)
    assert actual / ideal < 1.0


def test_ndcg_is_zero_when_no_result_has_gain() -> None:
    assert ndcg_at_k([0.0, 0.0], candidate_gains=[0.0, 0.0], k=10) == 0.0


def test_ndcg_uses_unreturned_candidates_in_the_ideal_ranking() -> None:
    expected = 3.0 / (3.0 + 2.0 / math.log2(3))
    assert ndcg_at_k([3.0], candidate_gains=[3.0, 2.0, 0.0], k=2) == pytest.approx(
        expected
    )


def test_ndcg_is_zero_for_an_empty_result_with_relevant_candidates() -> None:
    assert ndcg_at_k([], candidate_gains=[3.0, 2.0], k=2) == 0.0


def test_ndcg_rejects_results_that_exceed_the_candidate_ideal() -> None:
    with pytest.raises(ValueError, match="exceed"):
        ndcg_at_k([3.0, 3.0], candidate_gains=[3.0, 0.0], k=2)


def test_mean_ndcg_averages_query_scores() -> None:
    rankings = [[3.0, 0.0], [0.0, 3.0]]
    expected = (1.0 + 1.0 / math.log2(3)) / 2.0
    assert mean_ndcg_at_k(
        rankings,
        candidate_gains_by_query=[[3.0, 0.0], [3.0, 0.0]],
        k=2,
    ) == pytest.approx(expected)


def test_mean_ndcg_rejects_mismatched_query_sets() -> None:
    with pytest.raises(ValueError, match="same queries"):
        mean_ndcg_at_k([[3.0]], candidate_gains_by_query=[[3.0], [2.0]], k=10)


def test_reciprocal_rank_uses_the_first_relevant_result() -> None:
    assert reciprocal_rank_at_k([False, False, True, True], k=4) == pytest.approx(
        1.0 / 3.0
    )


def test_reciprocal_rank_respects_the_cutoff() -> None:
    assert reciprocal_rank_at_k([False, False, True], k=2) == 0.0


def test_mrr_includes_queries_with_no_relevant_result() -> None:
    rankings = [[True], [False, True], [False, False]]
    assert mean_reciprocal_rank_at_k(rankings, k=10) == pytest.approx(0.5)


def test_success_only_checks_whether_top_k_contains_a_relevant_result() -> None:
    relevant = [False, False, True]
    assert success_at_k(relevant, k=2) == 0.0
    assert success_at_k(relevant, k=3) == 1.0


def test_mean_success_averages_all_queries() -> None:
    rankings = [[True], [False, True], [False, False]]
    assert mean_success_at_k(rankings, k=2) == pytest.approx(2.0 / 3.0)


@pytest.mark.parametrize(
    "metric",
    [mean_reciprocal_rank_at_k, mean_success_at_k],
)
def test_mean_metrics_reject_an_empty_query_set(metric) -> None:
    with pytest.raises(ValueError, match="at least one query"):
        metric([], k=10)


def test_mean_ndcg_rejects_an_empty_query_set() -> None:
    with pytest.raises(ValueError, match="at least one query"):
        mean_ndcg_at_k([], candidate_gains_by_query=[], k=10)


@pytest.mark.parametrize("k", [0, -1])
def test_metrics_reject_non_positive_cutoffs(k: int) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        dcg_at_k([1.0], k=k)


@pytest.mark.parametrize("k", [1.5, True])
def test_metrics_reject_non_integer_cutoffs(k) -> None:
    with pytest.raises(TypeError, match="integer"):
        dcg_at_k([1.0], k=k)


@pytest.mark.parametrize("gain", [-1.0, float("nan"), float("inf")])
def test_dcg_rejects_invalid_gains(gain: float) -> None:
    with pytest.raises(ValueError):
        dcg_at_k([gain], k=1)


def test_binary_metrics_require_explicit_boolean_relevance() -> None:
    with pytest.raises(TypeError, match="booleans"):
        reciprocal_rank_at_k([0, 1], k=2)
