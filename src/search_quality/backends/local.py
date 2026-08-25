"""Deterministic local backend used as the Stage 0 fallback and CI backend."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence

from search_quality.backends.base import MAX_TOP_K
from search_quality.embedding import cosine_similarity
from search_quality.models import Product, ProductDocument, SearchHit
from search_quality.text import tokenize


class LocalSearchBackend:
    """In-memory BM25 and cosine search with no network or model dependency."""

    name = "local"

    def __init__(
        self,
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        if not math.isfinite(k1) or k1 <= 0.0:
            raise ValueError("k1 must be finite and greater than 0")
        if not math.isfinite(b) or not 0.0 <= b <= 1.0:
            raise ValueError("b must be finite and between 0 and 1")
        self.k1 = k1
        self.b = b
        self._products: list[Product] = []
        self._term_counts: list[Counter[str]] = []
        self._document_lengths: list[int] = []
        self._document_frequency: Counter[str] = Counter()
        self._vectors: list[list[float]] = []

    def replace_documents(self, documents: Sequence[ProductDocument]) -> None:
        if not documents:
            raise ValueError("documents must not be empty")
        products = [document.product for document in documents]
        product_ids = [product.product_id for product in products]
        if len(product_ids) != len(set(product_ids)):
            raise ValueError("product_id values must be unique")

        token_lists = [tokenize(product.searchable_text) for product in products]
        dimensions = {len(document.embedding) for document in documents}
        if len(dimensions) > 1:
            raise ValueError("all embeddings must have the same dimensions")

        # Assign only after every input check so a rejected replacement cannot
        # leave a partially updated index behind.
        self._products = list(products)
        self._term_counts = [Counter(tokens) for tokens in token_lists]
        self._document_lengths = [len(tokens) for tokens in token_lists]
        self._document_frequency = Counter(
            token for tokens in token_lists for token in set(tokens)
        )
        self._vectors = [list(document.embedding) for document in documents]

    def search_lexical(self, query: str, top_k: int = 5) -> list[SearchHit]:
        self._validate_search(query, top_k)
        query_terms = tokenize(query)
        corpus_size = len(self._products)
        average_length = max(sum(self._document_lengths) / corpus_size, 1.0)
        scored: list[tuple[Product, float]] = []

        for product, term_counts, document_length in zip(
            self._products,
            self._term_counts,
            self._document_lengths,
            strict=True,
        ):
            score = 0.0
            for term in query_terms:
                frequency = term_counts.get(term, 0)
                if frequency == 0:
                    continue
                document_frequency = self._document_frequency[term]
                inverse_document_frequency = math.log(
                    1.0
                    + (corpus_size - document_frequency + 0.5)
                    / (document_frequency + 0.5)
                )
                denominator = frequency + self.k1 * (
                    1.0 - self.b + self.b * document_length / average_length
                )
                score += inverse_document_frequency * (
                    frequency * (self.k1 + 1.0) / denominator
                )
            scored.append((product, score))

        matching = [(product, score) for product, score in scored if score > 0.0]
        return self._rank(matching, strategy="bm25", top_k=top_k)

    def search_vector(
        self, query_vector: Sequence[float], top_k: int = 5
    ) -> list[SearchHit]:
        self._validate_vector_search(query_vector, top_k)
        scored = [
            (product, cosine_similarity(query_vector, vector))
            for product, vector in zip(self._products, self._vectors, strict=True)
        ]
        return self._rank(scored, strategy="vector", top_k=top_k)

    def _validate_search(self, query: str, top_k: int) -> None:
        if not self._products:
            raise RuntimeError("replace_documents must be called before search")
        if not query.strip():
            raise ValueError("query must not be empty")
        if top_k < 1:
            raise ValueError("top_k must be at least 1")
        if top_k > MAX_TOP_K:
            raise ValueError(f"top_k must be at most {MAX_TOP_K}")

    def _validate_vector_search(
        self, query_vector: Sequence[float], top_k: int
    ) -> None:
        if not self._products:
            raise RuntimeError("replace_documents must be called before search")
        if top_k < 1:
            raise ValueError("top_k must be at least 1")
        if top_k > MAX_TOP_K:
            raise ValueError(f"top_k must be at most {MAX_TOP_K}")
        if not query_vector:
            raise ValueError("query_vector must not be empty")
        if not all(math.isfinite(value) for value in query_vector):
            raise ValueError("query vector values must be finite")
        if not any(value != 0.0 for value in query_vector):
            raise ValueError("query vector must not be a zero vector")
        expected_dimensions = len(self._vectors[0])
        if len(query_vector) != expected_dimensions:
            raise ValueError(
                f"query vector has {len(query_vector)} dimensions; "
                f"expected {expected_dimensions}"
            )

    @staticmethod
    def _rank(
        scored: list[tuple[Product, float]], *, strategy: str, top_k: int
    ) -> list[SearchHit]:
        ranked = sorted(scored, key=lambda item: (-item[1], item[0].product_id))[:top_k]
        return [
            SearchHit(product=product, score=score, rank=rank, strategy=strategy)
            for rank, (product, score) in enumerate(ranked, start=1)
        ]
