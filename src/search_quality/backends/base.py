"""Backend contract kept independent from OpenSearch and model providers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from search_quality.models import ProductDocument, SearchHit

MAX_TOP_K = 10_000


class SearchBackend(Protocol):
    name: str

    def replace_documents(self, documents: Sequence[ProductDocument]) -> None: ...

    def search_lexical(self, query: str, top_k: int = 5) -> list[SearchHit]: ...

    def search_vector(
        self, query_vector: Sequence[float], top_k: int = 5
    ) -> list[SearchHit]: ...
