"""Title-only BM25 candidate ranker for the Stage 2 baseline."""

from __future__ import annotations

import logging
import math
from collections import Counter
from collections.abc import Sequence

from search_quality.ranking.base import (
    CandidateProduct,
    ProductKey,
    RankedProduct,
    validate_candidate_products,
)
from search_quality.text import tokenize

_LOGGER = logging.getLogger(__name__)


class CandidateTitleBM25Ranker:
    """Rank a supplied candidate set using corpus-wide title statistics."""

    ranker_id = "candidate-title-bm25-v1"
    analyzer_id = "ascii-alnum-lower-v1"

    def __init__(
        self,
        products: Sequence[CandidateProduct],
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        if not math.isfinite(k1) or k1 <= 0.0:
            raise ValueError("k1 must be finite and greater than 0")
        if not math.isfinite(b) or not 0.0 <= b <= 1.0:
            raise ValueError("b must be finite and between 0 and 1")

        candidates = validate_candidate_products(products)

        self.k1 = k1
        self.b = b
        self._products = {product.key: product for product in candidates}
        token_lists = {product.key: tokenize(product.title) for product in candidates}
        self._term_counts = {
            product_id: Counter(tokens) for product_id, tokens in token_lists.items()
        }
        self._document_lengths = {
            product_id: len(tokens) for product_id, tokens in token_lists.items()
        }
        self._document_frequency = Counter(
            token for tokens in token_lists.values() for token in set(tokens)
        )
        self._corpus_size = len(candidates)
        self._average_length = max(
            sum(self._document_lengths.values()) / self._corpus_size, 1.0
        )

    @property
    def config(self) -> dict[str, str | float | int]:
        return {
            "analyzer_id": self.analyzer_id,
            "b": self.b,
            "field": "product_title",
            "idf_scope": "per_query_judged_candidates",
            "k1": self.k1,
            "query_terms": "deduplicated",
            "ranker_id": self.ranker_id,
            "tie_break": "product_locale_product_id_ascending",
        }

    def rank(self, query: str) -> list[RankedProduct]:
        if not query.strip():
            _LOGGER.debug(
                "rank_request_rejected",
                extra={"reason": "empty_query", "ranker_id": self.ranker_id},
            )
            raise ValueError("query must not be empty")

        query_terms = tuple(dict.fromkeys(tokenize(query)))
        scored = [
            (product_key, self._score(query_terms, product_key))
            for product_key in self._products
        ]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return [
            RankedProduct(
                locale=product_key[0],
                product_id=product_key[1],
                score=score,
                rank=rank,
            )
            for rank, (product_key, score) in enumerate(scored, start=1)
        ]

    def _score(self, query_terms: Sequence[str], product_key: ProductKey) -> float:
        term_counts = self._term_counts[product_key]
        document_length = self._document_lengths[product_key]
        score = 0.0
        for term in query_terms:
            frequency = term_counts.get(term, 0)
            if frequency == 0:
                continue
            document_frequency = self._document_frequency[term]
            inverse_document_frequency = math.log(
                1.0
                + (self._corpus_size - document_frequency + 0.5)
                / (document_frequency + 0.5)
            )
            length_normalization = (
                1.0 - self.b + self.b * document_length / self._average_length
            )
            score += inverse_document_frequency * (
                frequency
                * (self.k1 + 1.0)
                / (frequency + self.k1 * length_normalization)
            )
        return score


class CandidateTitleBM25ExactBoostRanker(CandidateTitleBM25Ranker):
    """BM25 plus small deterministic exact-token and phrase boosts."""

    ranker_id = "candidate-title-bm25-exact-boost-v1"

    def __init__(
        self,
        products: Sequence[CandidateProduct],
        *,
        coverage_boost: float = 0.8,
        numeric_boost: float = 1.0,
        phrase_boost: float = 1.2,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        for name, value in (
            ("coverage_boost", coverage_boost),
            ("numeric_boost", numeric_boost),
            ("phrase_boost", phrase_boost),
        ):
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and non-negative")
        super().__init__(products, k1=k1, b=b)
        self.coverage_boost = coverage_boost
        self.numeric_boost = numeric_boost
        self.phrase_boost = phrase_boost
        self._title_token_sets = {
            product_key: frozenset(term_counts)
            for product_key, term_counts in self._term_counts.items()
        }
        self._title_token_strings = {
            product_key: " ".join(tokenize(product.title))
            for product_key, product in self._products.items()
        }

    @property
    def config(self) -> dict[str, str | float | int]:
        payload = dict(super().config)
        payload.update(
            {
                "coverage_boost": self.coverage_boost,
                "numeric_boost": self.numeric_boost,
                "phrase_boost": self.phrase_boost,
                "ranker_id": self.ranker_id,
                "score": "title_bm25_plus_query_coverage_numeric_and_phrase_boosts",
            }
        )
        return payload

    def _score(self, query_terms: Sequence[str], product_key: ProductKey) -> float:
        score = super()._score(query_terms, product_key)
        title_terms = self._title_token_sets[product_key]
        query_set = frozenset(query_terms)
        if query_set:
            score += self.coverage_boost * (
                len(query_set & title_terms) / len(query_set)
            )
        numeric_terms = tuple(
            term for term in query_terms if any(char.isdigit() for char in term)
        )
        if numeric_terms:
            matched_numeric = sum(term in title_terms for term in numeric_terms)
            score += self.numeric_boost * (matched_numeric / len(numeric_terms))
        if (
            query_terms
            and " ".join(query_terms) in self._title_token_strings[product_key]
        ):
            score += self.phrase_boost
        return score
