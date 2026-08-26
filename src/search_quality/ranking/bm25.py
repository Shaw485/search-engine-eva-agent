"""Title-only BM25 candidate ranker for the Stage 2 baseline."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from search_quality.text import tokenize

ProductKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class CandidateProduct:
    locale: str
    product_id: str
    title: str

    def __post_init__(self) -> None:
        if not self.locale.strip():
            raise ValueError("locale must not be empty")
        if not self.product_id.strip():
            raise ValueError("product_id must not be empty")
        if not self.title.strip():
            raise ValueError("title must not be empty")

    @property
    def key(self) -> ProductKey:
        return (self.locale, self.product_id)


@dataclass(frozen=True, slots=True)
class RankedProduct:
    locale: str
    product_id: str
    score: float
    rank: int

    @property
    def key(self) -> ProductKey:
        return (self.locale, self.product_id)


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
        if not products:
            raise ValueError("products must not be empty")
        if not math.isfinite(k1) or k1 <= 0.0:
            raise ValueError("k1 must be finite and greater than 0")
        if not math.isfinite(b) or not 0.0 <= b <= 1.0:
            raise ValueError("b must be finite and between 0 and 1")

        product_keys = [product.key for product in products]
        if len(product_keys) != len(set(product_keys)):
            raise ValueError("product keys must be unique")

        self.k1 = k1
        self.b = b
        self._products = {product.key: product for product in products}
        token_lists = {product.key: tokenize(product.title) for product in products}
        self._term_counts = {
            product_id: Counter(tokens) for product_id, tokens in token_lists.items()
        }
        self._document_lengths = {
            product_id: len(tokens) for product_id, tokens in token_lists.items()
        }
        self._document_frequency = Counter(
            token for tokens in token_lists.values() for token in set(tokens)
        )
        self._corpus_size = len(products)
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
