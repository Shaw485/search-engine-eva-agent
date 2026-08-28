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


class CatalogBatchSearchFailed(RuntimeError):
    """A batch stopped after a safe number of completed Query calls."""

    def __init__(self, message: str, *, completed_query_count: int = 0) -> None:
        super().__init__(message)
        self.completed_query_count = completed_query_count


class CatalogSearchDeadlineExceeded(CatalogBatchSearchFailed):
    """A bounded batch search exceeded its interruptible SQL deadline."""


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
        tokens = validate_catalog_query(query, top_k=top_k)
        with self._connect() as connection:
            self._validate_connected_metadata(connection)
            return self._search_tokens(connection, tokens=tokens, top_k=top_k)

    def search_many(
        self,
        queries: list[str] | tuple[str, ...],
        *,
        top_k: int = 10,
        max_elapsed_ms: int = 120_000,
        max_query_elapsed_ms: int = 5_000,
    ) -> tuple[CatalogSearchResult, ...]:
        """Preflight then search one bounded batch on one immutable connection."""

        if not isinstance(queries, (list, tuple)) or not queries or len(queries) > 100:
            raise InvalidCatalogQuery("batch must contain between 1 and 100 Queries")
        if any(not isinstance(query, str) for query in queries):
            raise InvalidCatalogQuery("batch Queries must all be text")
        if (
            isinstance(max_elapsed_ms, bool)
            or not isinstance(max_elapsed_ms, int)
            or max_elapsed_ms < 1
            or isinstance(max_query_elapsed_ms, bool)
            or not isinstance(max_query_elapsed_ms, int)
            or max_query_elapsed_ms < 1
            or max_query_elapsed_ms > max_elapsed_ms
        ):
            raise InvalidCatalogQuery("batch search deadlines are invalid")
        # Complete the whole compatibility check before opening the index or
        # executing the first Query.
        validated = tuple(
            validate_catalog_query(query, top_k=top_k) for query in queries
        )
        batch_started = time.monotonic()
        overall_deadline = batch_started + (max_elapsed_ms / 1_000.0)
        results: list[CatalogSearchResult] = []
        logger.info(
            "catalog_batch_search_started",
            extra={
                "index_id": self.metadata.index_id,
                "query_count": len(validated),
                "top_k": top_k,
            },
        )
        with self._connect() as connection:
            try:
                self._validate_connected_metadata(connection)
            except (RuntimeError, ValueError) as exc:
                raise CatalogBatchSearchFailed(
                    "catalog index identity changed before batch search",
                    completed_query_count=0,
                ) from exc
            for tokens in validated:
                now = time.monotonic()
                if now >= overall_deadline:
                    raise CatalogSearchDeadlineExceeded(
                        "catalog batch deadline exceeded",
                        completed_query_count=len(results),
                    )
                query_deadline = min(
                    overall_deadline,
                    now + (max_query_elapsed_ms / 1_000.0),
                )
                interrupted = False

                def interrupt_when_expired(deadline: float = query_deadline) -> int:
                    nonlocal interrupted
                    interrupted = time.monotonic() >= deadline
                    return int(interrupted)

                connection.set_progress_handler(interrupt_when_expired, 1_000)
                try:
                    results.append(
                        self._search_tokens(connection, tokens=tokens, top_k=top_k)
                    )
                except CatalogBatchSearchFailed as exc:
                    raise type(exc)(
                        "catalog batch search failed",
                        completed_query_count=len(results),
                    ) from exc
                except sqlite3.OperationalError as exc:
                    if interrupted:
                        raise CatalogSearchDeadlineExceeded(
                            "catalog Query deadline exceeded",
                            completed_query_count=len(results),
                        ) from exc
                    raise CatalogBatchSearchFailed(
                        "catalog batch search failed",
                        completed_query_count=len(results),
                    ) from exc
                except (sqlite3.Error, RuntimeError) as exc:
                    raise CatalogBatchSearchFailed(
                        "catalog batch search failed",
                        completed_query_count=len(results),
                    ) from exc
                finally:
                    connection.set_progress_handler(None, 0)
        logger.info(
            "catalog_batch_search_completed",
            extra={
                "duration_ms": round((time.monotonic() - batch_started) * 1_000, 3),
                "index_id": self.metadata.index_id,
                "query_count": len(results),
                "top_k": top_k,
            },
        )
        return tuple(results)

    def _search_tokens(
        self,
        connection: sqlite3.Connection,
        *,
        tokens: tuple[str, ...],
        top_k: int,
    ) -> CatalogSearchResult:
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
                "returned_at_k": len(hits),
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

    def _validate_connected_metadata(self, connection: sqlite3.Connection) -> None:
        connected_metadata = CatalogIndexMetadata.from_connection(connection)
        if connected_metadata != self.metadata:
            raise RuntimeError("catalog index identity changed before search")


def validate_catalog_query(query: str, *, top_k: int = 10) -> tuple[str, ...]:
    """Validate one Query without opening the catalog index.

    Batch tools use this preflight to reject an incompatible Query set before
    the first search, so a completed diagnostic can never represent a partial
    batch.
    """

    tokens = _query_tokens(query)
    if isinstance(top_k, bool) or not isinstance(top_k, int):
        raise InvalidCatalogQuery("top_k must be an integer")
    if not 1 <= top_k <= MAX_CATALOG_TOP_K:
        raise InvalidCatalogQuery(f"top_k must be between 1 and {MAX_CATALOG_TOP_K}")
    return tokens


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
