from __future__ import annotations

import math

import pytest

from search_quality.ranking import CandidateProduct, CandidateTitleBM25Ranker


@pytest.fixture
def ranker() -> CandidateTitleBM25Ranker:
    return CandidateTitleBM25Ranker(
        [
            CandidateProduct("us", "a", "Logitech M185 Wireless Mouse"),
            CandidateProduct("us", "b", "Razer Wireless Gaming Mouse"),
            CandidateProduct("us", "c", "Large Mouse Pad"),
            CandidateProduct("us", "d", "Phone Case"),
        ]
    )


def test_ranker_returns_every_candidate_once(
    ranker: CandidateTitleBM25Ranker,
) -> None:
    results = ranker.rank("wireless mouse")
    assert [result.product_id for result in results] == ["a", "b", "c", "d"]
    assert [result.rank for result in results] == [1, 2, 3, 4]


def test_ranker_uses_product_id_to_break_zero_score_ties(
    ranker: CandidateTitleBM25Ranker,
) -> None:
    results = ranker.rank("term-not-present")
    assert [result.product_id for result in results] == ["a", "b", "c", "d"]
    assert {result.score for result in results} == {0.0}


def test_rare_model_term_beats_product_id_tie_break() -> None:
    ranker = CandidateTitleBM25Ranker(
        [
            CandidateProduct("us", "a", "Generic Wireless Mouse"),
            CandidateProduct("us", "z", "Logitech M185 Mouse"),
        ]
    )
    results = ranker.rank("m185")
    assert [result.product_id for result in results] == ["z", "a"]
    assert results[0].score == pytest.approx(math.log(2.0))
    assert results[1].score == 0.0


def test_length_normalization_prefers_a_focused_title() -> None:
    ranker = CandidateTitleBM25Ranker(
        [
            CandidateProduct("us", "short", "Wireless Mouse"),
            CandidateProduct(
                "us", "long", "Premium Portable Office Wireless Optical Mouse Device"
            ),
        ]
    )
    results = ranker.rank("wireless")
    assert [result.product_id for result in results] == ["short", "long"]
    assert results[0].score > results[1].score


def test_ranker_rejects_duplicate_product_keys() -> None:
    product = CandidateProduct("us", "a", "Mouse")
    with pytest.raises(ValueError, match="unique"):
        CandidateTitleBM25Ranker([product, product])


def test_ranker_configuration_is_explicit(
    ranker: CandidateTitleBM25Ranker,
) -> None:
    assert ranker.config == {
        "analyzer_id": "ascii-alnum-lower-v1",
        "b": 0.75,
        "field": "product_title",
        "idf_scope": "per_query_judged_candidates",
        "k1": 1.5,
        "query_terms": "deduplicated",
        "ranker_id": "candidate-title-bm25-v1",
        "tie_break": "product_locale_product_id_ascending",
    }


def test_ranker_tie_break_includes_locale() -> None:
    ranker = CandidateTitleBM25Ranker(
        [
            CandidateProduct("us", "a", "Mouse"),
            CandidateProduct("uk", "a", "Mouse"),
        ]
    )
    assert [result.key for result in ranker.rank("missing")] == [
        ("uk", "a"),
        ("us", "a"),
    ]
