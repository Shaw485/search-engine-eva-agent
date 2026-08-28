"""Deterministic title retrieval channels with different candidate rules."""

from __future__ import annotations

import logging
import math
from collections import Counter
from collections.abc import Sequence

from search_quality.text import tokenize

from .contracts import (
    RetrievalDocument,
    RetrievalHit,
    validate_retrieval_documents,
)

_BM25_LOGGER = logging.getLogger("search_quality.retrieval.title_bm25")
_EXACT_LOGGER = logging.getLogger("search_quality.retrieval.exact_title")
_MULTI_FIELD_LOGGER = logging.getLogger("search_quality.retrieval.multi_field_bm25")


def _validate_top_k(top_k: int) -> int:
    if isinstance(top_k, bool) or not isinstance(top_k, int):
        raise TypeError("top_k must be an integer")
    if top_k < 1:
        raise ValueError("top_k must be at least 1")
    return top_k


class TitleBM25Retriever:
    """OR-style BM25 channel that never pads results with zero-score products."""

    channel_id = "title-bm25-recall-v1"
    analyzer_id = "ascii-alnum-lower-v1"

    def __init__(
        self,
        documents: Sequence[RetrievalDocument],
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        if not math.isfinite(k1) or k1 <= 0.0:
            raise ValueError("k1 must be finite and positive")
        if not math.isfinite(b) or not 0.0 <= b <= 1.0:
            raise ValueError("b must be finite and between 0 and 1")
        snapshot = validate_retrieval_documents(documents)
        self.k1 = float(k1)
        self.b = float(b)
        self._documents = {document.key: document for document in snapshot}
        token_lists = {document.key: tokenize(document.title) for document in snapshot}
        self._term_counts = {
            key: Counter(tokens) for key, tokens in token_lists.items()
        }
        self._document_lengths = {
            key: len(tokens) for key, tokens in token_lists.items()
        }
        self._document_frequency = Counter(
            term for terms in token_lists.values() for term in set(terms)
        )
        self._corpus_size = len(snapshot)
        self._average_length = max(
            sum(self._document_lengths.values()) / self._corpus_size,
            1.0,
        )

    @property
    def config(self) -> dict[str, str | float]:
        return {
            "analyzer_id": self.analyzer_id,
            "b": self.b,
            "channel_id": self.channel_id,
            "idf_scope": "per_query_fully_judged_pool",
            "k1": self.k1,
            "match_operator": "or",
            "zero_score_products": "excluded",
        }

    def search(self, query: str, *, top_k: int) -> tuple[RetrievalHit, ...]:
        limit = _validate_top_k(top_k)
        query_terms = tuple(dict.fromkeys(tokenize(query)))
        if not query.strip() or not query_terms:
            _BM25_LOGGER.debug(
                "retrieval_request_rejected",
                extra={"channel_id": self.channel_id, "reason": "empty_query"},
            )
            raise ValueError("query must contain a searchable token")
        scored = [(key, self._score(query_terms, key)) for key in self._documents]
        scored = [item for item in scored if item[1] > 0.0]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return tuple(
            RetrievalHit(
                channel_id=self.channel_id,
                locale=key[0],
                product_id=key[1],
                rank=rank,
                score=score,
            )
            for rank, (key, score) in enumerate(scored[:limit], start=1)
        )

    def _score(self, query_terms: Sequence[str], key: tuple[str, str]) -> float:
        term_counts = self._term_counts[key]
        document_length = self._document_lengths[key]
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
            normalization = (
                1.0 - self.b + self.b * document_length / self._average_length
            )
            score += inverse_document_frequency * (
                frequency * (self.k1 + 1.0) / (frequency + self.k1 * normalization)
            )
        return score


class ExactTitleRetriever:
    """Strict all-token/title-phrase/identifier retrieval channel."""

    channel_id = "exact-title-recall-v1"
    analyzer_id = "ascii-alnum-lower-v1"

    def __init__(self, documents: Sequence[RetrievalDocument]) -> None:
        snapshot = validate_retrieval_documents(documents)
        self._documents = {document.key: document for document in snapshot}
        self._title_tokens = {
            document.key: tuple(tokenize(document.title)) for document in snapshot
        }
        self._title_token_sets = {
            key: frozenset(tokens) for key, tokens in self._title_tokens.items()
        }

    @property
    def config(self) -> dict[str, str]:
        return {
            "analyzer_id": self.analyzer_id,
            "channel_id": self.channel_id,
            "identifier_match": "case_insensitive_exact_product_id",
            "match_operator": "all_query_tokens_or_exact_product_id",
            "phrase_use": "channel_ordering_only",
        }

    def search(self, query: str, *, top_k: int) -> tuple[RetrievalHit, ...]:
        limit = _validate_top_k(top_k)
        normalized_query = query.strip().lower()
        query_terms = tuple(dict.fromkeys(tokenize(query)))
        if not normalized_query or not query_terms:
            _EXACT_LOGGER.debug(
                "retrieval_request_rejected",
                extra={"channel_id": self.channel_id, "reason": "empty_query"},
            )
            raise ValueError("query must contain a searchable token")
        query_set = frozenset(query_terms)
        query_phrase = " ".join(query_terms)
        scored: list[tuple[tuple[str, str], float]] = []
        for key, document in self._documents.items():
            identifier_exact = document.product_id.lower() == normalized_query
            all_tokens = query_set <= self._title_token_sets[key]
            if not identifier_exact and not all_tokens:
                continue
            normalized_title = " ".join(self._title_tokens[key])
            exact_title = normalized_title == query_phrase
            phrase = query_phrase in normalized_title
            score = (
                8.0 * float(identifier_exact)
                + 4.0 * float(exact_title)
                + 2.0 * float(phrase)
                + float(all_tokens)
            )
            scored.append((key, score))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return tuple(
            RetrievalHit(
                channel_id=self.channel_id,
                locale=key[0],
                product_id=key[1],
                rank=rank,
                score=score,
            )
            for rank, (key, score) in enumerate(scored[:limit], start=1)
        )


class MultiFieldBM25Retriever:
    """BM25F-style channel over title, brand, bullet point and description."""

    channel_id = "multi-field-bm25-recall-v1"
    analyzer_id = "ascii-alnum-lower-v1"
    field_weights = {
        "brand": 2.0,
        "bullet_point": 1.0,
        "description": 0.5,
        "title": 2.0,
    }

    def __init__(
        self,
        documents: Sequence[RetrievalDocument],
        *,
        k1: float = 1.2,
        b: float = 0.75,
    ) -> None:
        if not math.isfinite(k1) or k1 <= 0.0:
            raise ValueError("k1 must be finite and positive")
        if not math.isfinite(b) or not 0.0 <= b <= 1.0:
            raise ValueError("b must be finite and between 0 and 1")
        snapshot = validate_retrieval_documents(documents)
        self.k1 = float(k1)
        self.b = float(b)
        self._documents = {document.key: document for document in snapshot}
        self._field_counts: dict[str, dict[tuple[str, str], Counter[str]]] = {}
        self._field_lengths: dict[str, dict[tuple[str, str], int]] = {}
        self._average_field_lengths: dict[str, float] = {}
        for field in self.field_weights:
            counts = {
                document.key: Counter(tokenize(getattr(document, field)))
                for document in snapshot
            }
            lengths = {key: sum(terms.values()) for key, terms in counts.items()}
            self._field_counts[field] = counts
            self._field_lengths[field] = lengths
            self._average_field_lengths[field] = max(
                sum(lengths.values()) / len(snapshot),
                1.0,
            )
        self._document_frequency = Counter(
            term
            for document in snapshot
            for term in {
                token
                for field in self.field_weights
                for token in tokenize(getattr(document, field))
            }
        )
        self._corpus_size = len(snapshot)

    @property
    def config(self) -> dict[str, object]:
        return {
            "analyzer_id": self.analyzer_id,
            "b": self.b,
            "channel_id": self.channel_id,
            "field_weights": dict(self.field_weights),
            "fields": list(self.field_weights),
            "k1": self.k1,
            "match_operator": "or",
            "score": "bm25f_style_weighted_field_tf_v1",
            "zero_score_products": "excluded",
        }

    def search(self, query: str, *, top_k: int) -> tuple[RetrievalHit, ...]:
        limit = _validate_top_k(top_k)
        query_terms = tuple(dict.fromkeys(tokenize(query)))
        if not query.strip() or not query_terms:
            _MULTI_FIELD_LOGGER.debug(
                "retrieval_request_rejected",
                extra={"channel_id": self.channel_id, "reason": "empty_query"},
            )
            raise ValueError("query must contain a searchable token")
        scored = [(key, self._score(query_terms, key)) for key in self._documents]
        scored = [item for item in scored if item[1] > 0.0]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return tuple(
            RetrievalHit(
                channel_id=self.channel_id,
                locale=key[0],
                product_id=key[1],
                rank=rank,
                score=score,
            )
            for rank, (key, score) in enumerate(scored[:limit], start=1)
        )

    def _score(self, query_terms: Sequence[str], key: tuple[str, str]) -> float:
        score = 0.0
        for term in query_terms:
            weighted_tf = 0.0
            for field, weight in self.field_weights.items():
                frequency = self._field_counts[field][key].get(term, 0)
                if frequency == 0:
                    continue
                length = self._field_lengths[field][key]
                normalized_frequency = frequency / (
                    1.0 - self.b + self.b * length / self._average_field_lengths[field]
                )
                weighted_tf += weight * normalized_frequency
            if weighted_tf == 0.0:
                continue
            document_frequency = self._document_frequency[term]
            inverse_document_frequency = math.log(
                1.0
                + (self._corpus_size - document_frequency + 0.5)
                / (document_frequency + 0.5)
            )
            score += inverse_document_frequency * (
                weighted_tf * (self.k1 + 1.0) / (weighted_tf + self.k1)
            )
        return score
