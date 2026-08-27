"""Simple deterministic comparators for candidate-set evaluation."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence

from search_quality.ranking.base import (
    CandidateProduct,
    ProductKey,
    RankedProduct,
    validate_candidate_products,
)
from search_quality.text import tokenize

_OVERLAP_LOGGER = logging.getLogger(f"{__name__}.keyword_overlap")
_RANDOM_LOGGER = logging.getLogger(f"{__name__}.random")
_RANDOM_SCORE_BITS = 53
_RANDOM_SCORE_DENOMINATOR = (1 << _RANDOM_SCORE_BITS) - 1


class CandidateKeywordOverlapRanker:
    """Count unique exact tokens shared by the Query and product title."""

    ranker_id = "candidate-title-keyword-overlap-v1"
    analyzer_id = "ascii-alnum-lower-v1"

    def __init__(self, products: Sequence[CandidateProduct]) -> None:
        candidates = validate_candidate_products(products)
        self._title_terms = {
            product.key: frozenset(tokenize(product.title)) for product in candidates
        }

    @property
    def config(self) -> dict[str, str | float | int]:
        return {
            "analyzer_id": self.analyzer_id,
            "document_terms": "deduplicated",
            "field": "product_title",
            "query_terms": "deduplicated",
            "ranker_id": self.ranker_id,
            "score": "unique_query_title_token_intersection_count",
            "tie_break": "product_locale_product_id_ascending",
        }

    def rank(self, query: str) -> list[RankedProduct]:
        if not query.strip():
            _OVERLAP_LOGGER.debug(
                "rank_request_rejected",
                extra={"reason": "empty_query", "ranker_id": self.ranker_id},
            )
            raise ValueError("query must not be empty")

        query_terms = frozenset(tokenize(query))
        scored = [
            (product_key, float(len(query_terms & title_terms)))
            for product_key, title_terms in self._title_terms.items()
        ]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return _to_ranked_products(scored)


class CandidateDeterministicRandomRanker:
    """Produce a query-specific pseudorandom order that is stable across runs."""

    ranker_id = "candidate-random-v1"
    hash_algorithm_id = "sha256-canonical-json-first-53-bits-v1"

    def __init__(
        self,
        products: Sequence[CandidateProduct],
        *,
        seed: int = 17,
    ) -> None:
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise TypeError("seed must be an integer")
        if seed < 0:
            raise ValueError("seed must be non-negative")
        candidates = validate_candidate_products(products)
        self.seed = seed
        self._product_keys = tuple(product.key for product in candidates)

    @property
    def config(self) -> dict[str, str | float | int]:
        return {
            "hash_algorithm": self.hash_algorithm_id,
            "hash_bits": _RANDOM_SCORE_BITS,
            "query_identity": "stripped_query_text_utf8",
            "ranker_id": self.ranker_id,
            "seed": self.seed,
            "tie_break": "product_locale_product_id_ascending",
        }

    def rank(self, query: str) -> list[RankedProduct]:
        normalized_query = query.strip()
        if not normalized_query:
            _RANDOM_LOGGER.debug(
                "rank_request_rejected",
                extra={"reason": "empty_query", "ranker_id": self.ranker_id},
            )
            raise ValueError("query must not be empty")

        scored = [
            (product_key, self._score(normalized_query, product_key))
            for product_key in self._product_keys
        ]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return _to_ranked_products(scored)

    def _score(self, query: str, product_key: ProductKey) -> float:
        canonical = json.dumps(
            [self.hash_algorithm_id, self.seed, query, *product_key],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).digest()
        mantissa = int.from_bytes(digest[:8], byteorder="big") >> (
            64 - _RANDOM_SCORE_BITS
        )
        return mantissa / _RANDOM_SCORE_DENOMINATOR


def _to_ranked_products(
    scored: Sequence[tuple[ProductKey, float]],
) -> list[RankedProduct]:
    return [
        RankedProduct(
            locale=product_key[0],
            product_id=product_key[1],
            score=score,
            rank=rank,
        )
        for rank, (product_key, score) in enumerate(scored, start=1)
    ]
