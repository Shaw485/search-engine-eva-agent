from __future__ import annotations

import io
import json

import pytest

from search_quality.observability import configure_logging
from search_quality.ranking import (
    CandidateDeterministicRandomRanker,
    CandidateKeywordOverlapRanker,
    CandidateProduct,
    CandidateRanker,
)


@pytest.fixture
def products() -> list[CandidateProduct]:
    return [
        CandidateProduct("us", "a", "Wireless Mouse"),
        CandidateProduct("us", "b", "Wireless Wireless Gaming Mouse"),
        CandidateProduct("us", "c", "Large Mouse Pad"),
        CandidateProduct("us", "d", "Phone Case"),
    ]


def test_keyword_overlap_counts_unique_exact_tokens(
    products: list[CandidateProduct],
) -> None:
    ranker = CandidateKeywordOverlapRanker(products)

    results = ranker.rank("WIRELESS, wireless mouse!")

    assert [result.product_id for result in results] == ["a", "b", "c", "d"]
    assert [result.score for result in results] == [2.0, 2.0, 1.0, 0.0]
    assert [result.rank for result in results] == [1, 2, 3, 4]


def test_keyword_overlap_uses_locale_and_product_id_for_ties() -> None:
    ranker = CandidateKeywordOverlapRanker(
        [
            CandidateProduct("us", "a", "Mouse"),
            CandidateProduct("uk", "a", "Mouse"),
            CandidateProduct("us", "b", "Mouse"),
        ]
    )

    assert [result.key for result in ranker.rank("missing")] == [
        ("uk", "a"),
        ("us", "a"),
        ("us", "b"),
    ]


def test_keyword_overlap_configuration_is_explicit(
    products: list[CandidateProduct],
) -> None:
    ranker = CandidateKeywordOverlapRanker(products)

    assert ranker.config == {
        "analyzer_id": "ascii-alnum-lower-v1",
        "document_terms": "deduplicated",
        "field": "product_title",
        "query_terms": "deduplicated",
        "ranker_id": "candidate-title-keyword-overlap-v1",
        "score": "unique_query_title_token_intersection_count",
        "tie_break": "product_locale_product_id_ascending",
    }
    assert isinstance(ranker, CandidateRanker)


def test_deterministic_random_has_a_stable_golden_order(
    products: list[CandidateProduct],
) -> None:
    ranker = CandidateDeterministicRandomRanker(products, seed=17)

    first = ranker.rank("wireless mouse")
    second = ranker.rank("wireless mouse")

    assert first == second
    assert [result.product_id for result in first] == ["d", "a", "b", "c"]
    assert [result.rank for result in first] == [1, 2, 3, 4]
    assert all(0.0 <= result.score <= 1.0 for result in first)


def test_deterministic_random_ignores_input_order_and_titles(
    products: list[CandidateProduct],
) -> None:
    expected = CandidateDeterministicRandomRanker(products, seed=17).rank(
        "wireless mouse"
    )
    changed = [
        CandidateProduct(product.locale, product.product_id, f"Changed {index}")
        for index, product in enumerate(reversed(products))
    ]

    actual = CandidateDeterministicRandomRanker(changed, seed=17).rank("wireless mouse")

    assert actual == expected


def test_deterministic_random_seed_changes_the_order() -> None:
    products = [
        CandidateProduct("us", f"p{index:02d}", f"Product {index}")
        for index in range(12)
    ]

    first = CandidateDeterministicRandomRanker(products, seed=17).rank("query")
    second = CandidateDeterministicRandomRanker(products, seed=18).rank("query")

    assert [result.key for result in first] != [result.key for result in second]


def test_deterministic_random_configuration_records_seed(
    products: list[CandidateProduct],
) -> None:
    ranker = CandidateDeterministicRandomRanker(products, seed=23)

    assert ranker.config == {
        "hash_algorithm": "sha256-canonical-json-first-53-bits-v1",
        "hash_bits": 53,
        "query_identity": "stripped_query_text_utf8",
        "ranker_id": "candidate-random-v1",
        "seed": 23,
        "tie_break": "product_locale_product_id_ascending",
    }
    assert isinstance(ranker, CandidateRanker)


@pytest.mark.parametrize(
    ("seed", "error", "message"),
    [
        (True, TypeError, "integer"),
        (1.5, TypeError, "integer"),
        (-1, ValueError, "non-negative"),
    ],
)
def test_deterministic_random_rejects_invalid_seeds(
    products: list[CandidateProduct],
    seed: object,
    error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error, match=message):
        CandidateDeterministicRandomRanker(products, seed=seed)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "ranker_type",
    [CandidateKeywordOverlapRanker, CandidateDeterministicRandomRanker],
)
def test_simple_rankers_reject_duplicate_products(
    ranker_type: type[CandidateKeywordOverlapRanker]
    | type[CandidateDeterministicRandomRanker],
) -> None:
    product = CandidateProduct("us", "a", "Mouse")
    with pytest.raises(ValueError, match="unique"):
        ranker_type([product, product])


@pytest.mark.parametrize(
    "ranker_type",
    [CandidateKeywordOverlapRanker, CandidateDeterministicRandomRanker],
)
def test_simple_rankers_log_rejected_empty_queries_for_independent_debugging(
    products: list[CandidateProduct],
    ranker_type: type[CandidateKeywordOverlapRanker]
    | type[CandidateDeterministicRandomRanker],
) -> None:
    logger_name = (
        "search_quality.ranking.baselines.keyword_overlap"
        if ranker_type is CandidateKeywordOverlapRanker
        else "search_quality.ranking.baselines.random"
    )
    stream = io.StringIO()
    configure_logging(
        default_level="WARNING",
        module_levels={"ranking": "DEBUG"},
        stream=stream,
    )
    ranker = ranker_type(products)

    with pytest.raises(ValueError, match="query must not be empty"):
        ranker.rank("  ")

    rejected = [
        event
        for event in map(json.loads, stream.getvalue().splitlines())
        if event["logger"] == logger_name and event.get("reason") == "empty_query"
    ]
    assert len(rejected) == 1
