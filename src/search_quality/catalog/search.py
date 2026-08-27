"""Read-only full-catalog BM25 search over an immutable SQLite FTS5 index."""

from __future__ import annotations

import logging
import math
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from search_quality.catalog.index import CatalogIndexMetadata

MAX_CATALOG_TOP_K = 20
MAX_QUERY_CHARACTERS = 200
MAX_QUERY_TOKENS = 16
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
logger = logging.getLogger("search_quality.catalog")


class InvalidCatalogQuery(ValueError):
    """The caller supplied a Query outside the public search contract."""


@dataclass(frozen=True, slots=True)
class CatalogProduct:
    product_id: str
    locale: str
    title: str
    brand: str
    color: str


@dataclass(frozen=True, slots=True)
class CatalogSearchHit:
    product: CatalogProduct
    score: float
    rank: int
    strategy: str = "sqlite-fts5-bm25"

    def to_dict(self) -> dict[str, Any]:
        return {
            "product": asdict(self.product),
            "rank": self.rank,
            "score": round(self.score, 8),
            "strategy": self.strategy,
        }


@dataclass(frozen=True, slots=True)
class CatalogSearchResult:
    index_id: str
    product_count: int
    locale_counts: dict[str, int]
    hits: tuple[CatalogSearchHit, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": "sqlite-fts5",
            "hits": [hit.to_dict() for hit in self.hits],
            "index_id": self.index_id,
            "locale_counts": self.locale_counts,
            "product_count": self.product_count,
        }


class CatalogSearchService:
    """Open one immutable index per Query without sharing SQLite connections."""

    def __init__(self, index_path: str | Path) -> None:
        configured_path = Path(index_path)
        if configured_path.is_symlink():
            raise ValueError("catalog index must be a regular non-symlink file")
        self.index_path = configured_path.resolve(strict=True)
        if not self.index_path.is_file():
            raise ValueError("catalog index must be a regular non-symlink file")
        with self._connect() as connection:
            self.metadata = CatalogIndexMetadata.from_connection(connection)
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                )
            }
            if "catalog_products" not in tables:
                raise ValueError("catalog search table is missing")
        logger.info(
            "catalog_index_ready",
            extra={
                "index_id": self.metadata.index_id,
                "product_count": self.metadata.product_count,
            },
        )

    def search(self, query: str, *, top_k: int = 10) -> CatalogSearchResult:
        tokens = _query_tokens(query)
        if isinstance(top_k, bool) or not isinstance(top_k, int):
            raise InvalidCatalogQuery("top_k must be an integer")
        if not 1 <= top_k <= MAX_CATALOG_TOP_K:
            raise InvalidCatalogQuery(
                f"top_k must be between 1 and {MAX_CATALOG_TOP_K}"
            )
        match_query = " AND ".join(_quote_fts_token(token) for token in tokens)
        started = time.perf_counter()
        logger.debug(
            "catalog_search_started",
            extra={
                "index_id": self.metadata.index_id,
                "query_token_count": len(tokens),
                "top_k": top_k,
            },
        )
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT product_id, locale, title, brand, color, score "
                "FROM ("
                "SELECT product_id, locale, title, brand, color, "
                "bm25(catalog_products, 8.0, 0.0, 4.0, 2.0, 1.0) AS score "
                "FROM catalog_products WHERE catalog_products MATCH ?"
                ") ORDER BY score ASC, locale ASC, product_id ASC LIMIT ?",
                (match_query, top_k),
            ).fetchall()
        hits = tuple(
            CatalogSearchHit(
                product=CatalogProduct(
                    product_id=str(row[0]),
                    locale=str(row[1]),
                    title=str(row[2]),
                    brand=str(row[3]),
                    color=str(row[4]),
                ),
                score=_public_score(row[5]),
                rank=rank,
            )
            for rank, row in enumerate(rows, start=1)
        )
        logger.debug(
            "catalog_search_completed",
            extra={
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "index_id": self.metadata.index_id,
                "query_token_count": len(tokens),
                "result_count": len(hits),
                "top_k": top_k,
            },
        )
        return CatalogSearchResult(
            index_id=self.metadata.index_id,
            product_count=self.metadata.product_count,
            locale_counts=dict(self.metadata.locale_counts),
            hits=hits,
        )

    def _connect(self) -> sqlite3.Connection:
        uri = f"{self.index_path.as_uri()}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, timeout=2.0)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA cache_size=-32768")
        connection.execute("PRAGMA mmap_size=268435456")
        return connection


def _query_tokens(query: str) -> tuple[str, ...]:
    if not isinstance(query, str):
        raise InvalidCatalogQuery("query must be text")
    normalized = query.strip().casefold()
    if not normalized:
        raise InvalidCatalogQuery("query must not be empty")
    if len(normalized) > MAX_QUERY_CHARACTERS:
        raise InvalidCatalogQuery(
            f"query must contain at most {MAX_QUERY_CHARACTERS} characters"
        )
    tokens = tuple(dict.fromkeys(_TOKEN_RE.findall(normalized)))
    if not tokens:
        raise InvalidCatalogQuery("query must contain searchable text")
    if len(tokens) > MAX_QUERY_TOKENS:
        raise InvalidCatalogQuery(
            f"query must contain at most {MAX_QUERY_TOKENS} tokens"
        )
    return tokens


def _quote_fts_token(token: str) -> str:
    return f'"{token.replace(chr(34), chr(34) * 2)}"'


def _public_score(raw_score: Any) -> float:
    if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
        raise RuntimeError("catalog search returned an invalid score")
    score = -float(raw_score)
    if not math.isfinite(score) or score < 0.0:
        raise RuntimeError("catalog search returned an invalid score")
    return score
