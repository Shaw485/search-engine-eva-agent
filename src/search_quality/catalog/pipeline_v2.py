"""Production full-catalog retrieval, weighted RRF and coarse ranking."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from search_quality.catalog.index_v2 import CatalogV2IndexMetadata
from search_quality.catalog.search import InvalidCatalogQuery, validate_catalog_query
from search_quality.ranking import CandidateProduct, CandidateTitleBM25Ranker
from search_quality.retrieval.contracts import (
    ChannelResult,
    RetrievalHit,
    StageHit,
)
from search_quality.retrieval.rrf import reciprocal_rank_fuse

PRODUCTION_STRATEGY_ID = "multi-field-bm25-weighted-rrf-v1"
PRODUCTION_PIPELINE_VARIANT = "title-exact-multifield-weighted-v1"
TITLE_CHANNEL_ID = "title-bm25-recall-v1"
EXACT_CHANNEL_ID = "exact-title-recall-v1"
MULTI_FIELD_CHANNEL_ID = "multi-field-bm25-recall-v1"
CHANNEL_TOP_K = 50
FUSION_TOP_K = 20
COARSE_TOP_K = 10
RRF_K = 60
RRF_WEIGHTS = {
    EXACT_CHANNEL_ID: 1.0,
    MULTI_FIELD_CHANNEL_ID: 0.1,
    TITLE_CHANNEL_ID: 1.0,
}
PRODUCTION_PIPELINE_CONFIG: dict[str, Any] = {
    "analyzer_id": "ascii-alnum-lower-v1",
    "channel_top_k": CHANNEL_TOP_K,
    "channels": [
        {
            "analyzer_id": "ascii-alnum-lower-v1",
            "b": 0.75,
            "channel_id": TITLE_CHANNEL_ID,
            "idf_scope": "per_query_fully_judged_pool",
            "k1": 1.5,
            "match_operator": "or",
            "zero_score_products": "excluded",
        },
        {
            "analyzer_id": "ascii-alnum-lower-v1",
            "channel_id": EXACT_CHANNEL_ID,
            "identifier_match": "case_insensitive_exact_product_id",
            "match_operator": "all_query_tokens_or_exact_product_id",
            "phrase_use": "channel_ordering_only",
        },
        {
            "analyzer_id": "ascii-alnum-lower-v1",
            "b": 0.75,
            "channel_id": MULTI_FIELD_CHANNEL_ID,
            "field_weights": {
                "brand": 2.0,
                "bullet_point": 1.0,
                "description": 0.5,
                "title": 2.0,
            },
            "fields": ["brand", "bullet_point", "description", "title"],
            "k1": 1.2,
            "match_operator": "or",
            "score": "bm25f_style_weighted_field_tf_v1",
            "zero_score_products": "excluded",
        },
    ],
    "coarse_rank": {
        "ranker_id": "candidate-title-bm25-v1",
        "top_k": COARSE_TOP_K,
    },
    "fine_rank": {"status": "not_implemented"},
    "fusion": {
        "method": "reciprocal_rank_fusion",
        "rrf_k": RRF_K,
        "top_k": FUSION_TOP_K,
        "weights": RRF_WEIGHTS,
    },
    "rerank": {"status": "not_implemented"},
    "schema_version": "query-scoped-search-pipeline-config-v1",
    "variant": PRODUCTION_PIPELINE_VARIANT,
}

_CANONICAL_PIPELINE_CONFIG = json.dumps(
    PRODUCTION_PIPELINE_CONFIG,
    ensure_ascii=False,
    separators=(",", ":"),
    sort_keys=True,
    allow_nan=False,
)
PRODUCTION_PIPELINE_CONFIG_SHA256 = hashlib.sha256(
    _CANONICAL_PIPELINE_CONFIG.encode("utf-8")
).hexdigest()
PRODUCTION_PIPELINE_ID = f"pipeline-{PRODUCTION_PIPELINE_CONFIG_SHA256[:12]}"

logger = logging.getLogger("search_quality.catalog_pipeline")
_UNICODE_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


class ProductionPipelineUnavailable(RuntimeError):
    """The v2 index or its query execution failed closed."""


class ProductionPipelineDeadlineExceeded(ProductionPipelineUnavailable):
    """The bounded production pipeline exceeded its SQL deadline."""


@dataclass(frozen=True, slots=True)
class CatalogV2Product:
    product_id: str
    locale: str
    title: str
    brand: str
    bullet_point: str
    description: str
    color: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.locale, self.product_id)


@dataclass(frozen=True, slots=True)
class ProductionPipelineHit:
    product: CatalogV2Product
    rank: int
    score: float
    fused_rank: int
    fused_score: float
    channel_ranks: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_ranks": dict(self.channel_ranks),
            "fused_rank": self.fused_rank,
            "fused_score": round(self.fused_score, 12),
            "product": asdict(self.product),
            "rank": self.rank,
            "score": round(self.score, 8),
        }


@dataclass(frozen=True, slots=True)
class ProductionPipelineResult:
    pipeline_id: str
    index_id: str
    product_count: int
    locale_counts: dict[str, int]
    channel_counts: dict[str, int]
    hits: tuple[ProductionPipelineHit, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": "sqlite-fts5",
            "channel_counts": dict(self.channel_counts),
            "hits": [hit.to_dict() for hit in self.hits],
            "index_id": self.index_id,
            "locale_counts": dict(self.locale_counts),
            "pipeline_id": self.pipeline_id,
            "product_count": self.product_count,
        }


class CatalogV2SearchPipeline:
    """Execute the approved three-channel pipeline against one immutable index."""

    def __init__(self, index_path: str | Path) -> None:
        configured = Path(index_path)
        if configured.is_symlink():
            raise ValueError("catalog v2 index must be a regular non-symlink file")
        self.index_path = configured.resolve(strict=True)
        if not self.index_path.is_file():
            raise ValueError("catalog v2 index must be a regular non-symlink file")
        with self._connect() as connection:
            self.metadata = CatalogV2IndexMetadata.from_connection(connection)
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                )
            }
            required = {"catalog_products", "catalog_product_records"}
            if not required <= tables:
                raise ValueError("catalog v2 search tables are missing")
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(catalog_product_records)"
                )
            }
            if columns != {
                "rowid",
                "product_id",
                "locale",
                "title",
                "brand",
                "bullet_point",
                "description",
                "color",
            }:
                raise ValueError("catalog v2 product fields are incompatible")
        logger.info(
            "catalog_v2_pipeline_ready",
            extra={
                "index_id": self.metadata.index_id,
                "pipeline_id": PRODUCTION_PIPELINE_ID,
                "product_count": self.metadata.product_count,
            },
        )

    def search(
        self,
        query: str,
        *,
        top_k: int = COARSE_TOP_K,
        max_elapsed_ms: int = 5_000,
    ) -> ProductionPipelineResult:
        tokens = validate_catalog_query(query, top_k=top_k)
        if top_k > COARSE_TOP_K:
            raise InvalidCatalogQuery(
                f"v2 production top_k must be between 1 and {COARSE_TOP_K}"
            )
        if (
            isinstance(max_elapsed_ms, bool)
            or not isinstance(max_elapsed_ms, int)
            or max_elapsed_ms < 1
            or max_elapsed_ms > 30_000
        ):
            raise InvalidCatalogQuery("max_elapsed_ms must be between 1 and 30000")

        started = time.perf_counter()
        deadline = time.monotonic() + (max_elapsed_ms / 1000.0)
        interrupted = False

        def interrupt_when_expired() -> int:
            nonlocal interrupted
            interrupted = time.monotonic() >= deadline
            return int(interrupted)

        logger.debug(
            "catalog_v2_pipeline_search_started",
            extra={
                "index_id": self.metadata.index_id,
                "pipeline_id": PRODUCTION_PIPELINE_ID,
                "query_token_count": len(tokens),
                "top_k": top_k,
            },
        )
        try:
            with self._connect() as connection:
                self._validate_connected_metadata(connection)
                connection.set_progress_handler(interrupt_when_expired, 1_000)
                try:
                    result = self._search_connected(
                        connection,
                        query=query,
                        tokens=tokens,
                        top_k=top_k,
                    )
                finally:
                    connection.set_progress_handler(None, 0)
        except sqlite3.OperationalError as exc:
            if interrupted:
                logger.warning(
                    "catalog_v2_pipeline_deadline_exceeded",
                    extra={
                        "index_id": self.metadata.index_id,
                        "pipeline_id": PRODUCTION_PIPELINE_ID,
                    },
                )
                raise ProductionPipelineDeadlineExceeded(
                    "catalog v2 search deadline exceeded"
                ) from exc
            raise ProductionPipelineUnavailable(
                "catalog v2 search execution failed"
            ) from exc
        except sqlite3.Error as exc:
            raise ProductionPipelineUnavailable(
                "catalog v2 search execution failed"
            ) from exc
        logger.debug(
            "catalog_v2_pipeline_search_completed",
            extra={
                "channel_count": 3,
                "coarse_count": result.channel_counts["coarse"],
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "fused_count": result.channel_counts["fused"],
                "index_id": self.metadata.index_id,
                "pipeline_id": PRODUCTION_PIPELINE_ID,
                "query_token_count": len(tokens),
                "returned_at_k": len(result.hits),
            },
        )
        return result

    def sentinel_queries(self, *, limit: int = 2) -> tuple[str, ...]:
        """Derive bounded non-logged validation Queries from the index itself."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 4:
            raise ValueError("sentinel limit must be between 1 and 4")
        with self._connect() as connection:
            self._validate_connected_metadata(connection)
            rows = connection.execute(
                "SELECT product_id, title FROM catalog_product_records "
                "ORDER BY rowid ASC LIMIT ?",
                (limit,),
            ).fetchall()
        queries: list[str] = []
        for product_id, title in rows:
            title_tokens = _normalized_tokens(str(title))
            if title_tokens:
                queries.append(" ".join(title_tokens[: min(3, len(title_tokens))]))
            if len(queries) < limit:
                queries.append(str(product_id))
            if len(queries) >= limit:
                break
        if not queries:
            raise ProductionPipelineUnavailable("catalog v2 sentinel source is empty")
        return tuple(queries[:limit])

    def _search_connected(
        self,
        connection: sqlite3.Connection,
        *,
        query: str,
        tokens: tuple[str, ...],
        top_k: int,
    ) -> ProductionPipelineResult:
        title_hits, title_products = self._title_channel(connection, tokens)
        exact_hits, exact_products = self._exact_channel(
            connection,
            query=query,
            tokens=tokens,
        )
        multi_hits, multi_products = self._multi_field_channel(connection, tokens)
        products = {**title_products, **exact_products, **multi_products}
        channels = (
            ChannelResult(
                channel_id=TITLE_CHANNEL_ID,
                config=PRODUCTION_PIPELINE_CONFIG["channels"][0],
                hits=title_hits,
            ),
            ChannelResult(
                channel_id=EXACT_CHANNEL_ID,
                config=PRODUCTION_PIPELINE_CONFIG["channels"][1],
                hits=exact_hits,
            ),
            ChannelResult(
                channel_id=MULTI_FIELD_CHANNEL_ID,
                config=PRODUCTION_PIPELINE_CONFIG["channels"][2],
                hits=multi_hits,
            ),
        )
        union = {hit.key for channel in channels for hit in channel.hits}
        fused = reciprocal_rank_fuse(
            channels,
            rrf_k=RRF_K,
            top_k=FUSION_TOP_K,
            weights=RRF_WEIGHTS,
        )
        coarse = self._coarse_rank(query, fused=fused, products=products)
        fused_by_key = {hit.key: hit for hit in fused}
        hits = tuple(
            ProductionPipelineHit(
                product=products[item.key],
                rank=rank,
                score=item.score,
                fused_rank=fused_by_key[item.key].rank,
                fused_score=fused_by_key[item.key].score,
                channel_ranks={
                    contribution.channel_id: contribution.source_rank
                    for contribution in fused_by_key[item.key].contributions
                },
            )
            for rank, item in enumerate(coarse[:top_k], start=1)
        )
        return ProductionPipelineResult(
            pipeline_id=PRODUCTION_PIPELINE_ID,
            index_id=self.metadata.index_id,
            product_count=self.metadata.product_count,
            locale_counts=dict(self.metadata.locale_counts),
            channel_counts={
                "title": len(title_hits),
                "exact": len(exact_hits),
                "multi_field": len(multi_hits),
                "union": len(union),
                "fused": len(fused),
                "coarse": len(coarse),
            },
            hits=hits,
        )

    def _title_channel(
        self,
        connection: sqlite3.Connection,
        tokens: tuple[str, ...],
    ) -> tuple[tuple[RetrievalHit, ...], dict[tuple[str, str], CatalogV2Product]]:
        match_query = _column_or_query("title", tokens)
        rows = connection.execute(
            _channel_select(
                "bm25(catalog_products, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0)"
            ),
            (match_query, CHANNEL_TOP_K),
        ).fetchall()
        return _rows_to_channel(rows, channel_id=TITLE_CHANNEL_ID)

    def _exact_channel(
        self,
        connection: sqlite3.Connection,
        *,
        query: str,
        tokens: tuple[str, ...],
    ) -> tuple[tuple[RetrievalHit, ...], dict[tuple[str, str], CatalogV2Product]]:
        selected: dict[tuple[str, str], tuple[CatalogV2Product, float, float]] = {}
        identifier_rows = connection.execute(
            "SELECT product_id, locale, title, brand, bullet_point, description, "
            "color FROM catalog_product_records "
            "WHERE product_id = ? COLLATE NOCASE "
            "ORDER BY locale ASC, product_id ASC LIMIT ?",
            (query.strip(), CHANNEL_TOP_K),
        ).fetchall()
        for row in identifier_rows:
            product = _product_from_row(row)
            selected[product.key] = (product, 8.0, 0.0)

        # Phrase matches are collected separately so exact/phrase titles cannot be
        # displaced by many generic all-token matches before channel scoring.
        queries = (
            _column_phrase_query("title", tokens),
            _column_and_query("title", tokens),
        )
        for match_query in queries:
            rows = connection.execute(
                _channel_select(
                    "bm25(catalog_products, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0)"
                ),
                (match_query, CHANNEL_TOP_K),
            ).fetchall()
            for row in rows:
                product = _product_from_row(row)
                title_tokens = _normalized_tokens(product.title)
                query_phrase = tuple(tokens)
                if not frozenset(tokens) <= frozenset(title_tokens):
                    continue
                exact_title = title_tokens == query_phrase
                phrase = _contains_subsequence(title_tokens, query_phrase)
                identifier = product.product_id.casefold() == query.strip().casefold()
                score = (
                    8.0 * float(identifier)
                    + 4.0 * float(exact_title)
                    + 2.0 * float(phrase)
                    + 1.0
                )
                raw_bm25 = float(row[7])
                previous = selected.get(product.key)
                if previous is None or (score, -raw_bm25) > (
                    previous[1],
                    -previous[2],
                ):
                    selected[product.key] = (product, score, raw_bm25)

        ordered = sorted(
            selected.values(),
            key=lambda item: (-item[1], item[2], item[0].key),
        )[:CHANNEL_TOP_K]
        hits = tuple(
            RetrievalHit(
                channel_id=EXACT_CHANNEL_ID,
                locale=product.locale,
                product_id=product.product_id,
                rank=rank,
                score=score,
            )
            for rank, (product, score, _raw_bm25) in enumerate(ordered, start=1)
        )
        return hits, {product.key: product for product, _, _ in ordered}

    def _multi_field_channel(
        self,
        connection: sqlite3.Connection,
        tokens: tuple[str, ...],
    ) -> tuple[tuple[RetrievalHit, ...], dict[tuple[str, str], CatalogV2Product]]:
        match_query = _columns_or_query(
            ("title", "brand", "bullet_point", "description"),
            tokens,
        )
        rows = connection.execute(
            _channel_select(
                "bm25(catalog_products, 0.0, 0.0, 2.0, 2.0, 1.0, 0.5, 0.0)"
            ),
            (match_query, CHANNEL_TOP_K),
        ).fetchall()
        return _rows_to_channel(rows, channel_id=MULTI_FIELD_CHANNEL_ID)

    def _coarse_rank(
        self,
        query: str,
        *,
        fused,
        products: dict[tuple[str, str], CatalogV2Product],
    ) -> tuple[StageHit, ...]:
        if not fused:
            return ()
        candidates = [
            CandidateProduct(
                locale=products[hit.key].locale,
                product_id=products[hit.key].product_id,
                title=products[hit.key].title,
            )
            for hit in fused
        ]
        ranked = CandidateTitleBM25Ranker(candidates).rank(query)
        return tuple(
            StageHit(
                stage_id="coarse-title-bm25-v1",
                locale=item.locale,
                product_id=item.product_id,
                rank=rank,
                score=item.score,
            )
            for rank, item in enumerate(ranked[:COARSE_TOP_K], start=1)
        )

    def _connect(self) -> sqlite3.Connection:
        uri = f"{self.index_path.as_uri()}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, timeout=2.0)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA cache_size=-65536")
        connection.execute("PRAGMA mmap_size=268435456")
        return connection

    def _validate_connected_metadata(self, connection: sqlite3.Connection) -> None:
        connected = CatalogV2IndexMetadata.from_connection(connection)
        if connected != self.metadata:
            raise ProductionPipelineUnavailable(
                "catalog v2 index identity changed before search"
            )


def validate_production_pipeline_config(
    config: Any,
    *,
    config_sha256: str | None = None,
    pipeline_id: str | None = None,
) -> dict[str, Any]:
    """Require the exact approved weighted-conservative production pipeline."""

    if not isinstance(config, dict):
        raise ValueError("retrieval pipeline config must be an object")
    try:
        canonical = json.dumps(
            config,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("retrieval pipeline config is not canonical JSON") from exc
    observed_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if canonical != _CANONICAL_PIPELINE_CONFIG:
        raise ValueError("retrieval pipeline is not the supported production config")
    if config_sha256 is not None and config_sha256 != observed_sha256:
        raise ValueError("retrieval pipeline config hash does not match")
    if pipeline_id is not None and pipeline_id != PRODUCTION_PIPELINE_ID:
        raise ValueError("retrieval pipeline ID does not match its config")
    return json.loads(canonical)


def _channel_select(score_expression: str) -> str:
    return (
        "SELECT p.product_id, p.locale, p.title, p.brand, p.bullet_point, "
        "p.description, p.color, "
        f"{score_expression} AS score "
        "FROM catalog_products "
        "JOIN catalog_product_records AS p ON p.rowid = catalog_products.rowid "
        "WHERE catalog_products MATCH ? "
        "ORDER BY score ASC, p.locale ASC, p.product_id ASC LIMIT ?"
    )


def _rows_to_channel(
    rows: list[tuple[Any, ...]],
    *,
    channel_id: str,
) -> tuple[tuple[RetrievalHit, ...], dict[tuple[str, str], CatalogV2Product]]:
    products = [_product_from_row(row) for row in rows]
    hits = tuple(
        RetrievalHit(
            channel_id=channel_id,
            locale=product.locale,
            product_id=product.product_id,
            rank=rank,
            score=_public_fts_score(rows[rank - 1][7]),
        )
        for rank, product in enumerate(products, start=1)
    )
    return hits, {product.key: product for product in products}


def _product_from_row(row: tuple[Any, ...]) -> CatalogV2Product:
    return CatalogV2Product(
        product_id=str(row[0]),
        locale=str(row[1]),
        title=str(row[2]),
        brand=str(row[3]),
        bullet_point=str(row[4]),
        description=str(row[5]),
        color=str(row[6]),
    )


def _public_fts_score(raw_score: Any) -> float:
    if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
        raise ProductionPipelineUnavailable("catalog v2 returned an invalid score")
    score = -float(raw_score)
    if not math.isfinite(score) or score < 0.0:
        raise ProductionPipelineUnavailable("catalog v2 returned an invalid score")
    return score


def _quote_fts_token(token: str) -> str:
    return f'"{token.replace(chr(34), chr(34) * 2)}"'


def _column_or_query(column: str, tokens: tuple[str, ...]) -> str:
    return (
        f"{column} : (" + " OR ".join(_quote_fts_token(token) for token in tokens) + ")"
    )


def _columns_or_query(columns: tuple[str, ...], tokens: tuple[str, ...]) -> str:
    return (
        "{"
        + " ".join(columns)
        + "} : ("
        + " OR ".join(_quote_fts_token(token) for token in tokens)
        + ")"
    )


def _column_and_query(column: str, tokens: tuple[str, ...]) -> str:
    return " AND ".join(f"{column} : {_quote_fts_token(token)}" for token in tokens)


def _column_phrase_query(column: str, tokens: tuple[str, ...]) -> str:
    phrase = " ".join(tokens).replace('"', '""')
    return f'{column} : "{phrase}"'


def _normalized_tokens(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_UNICODE_TOKEN_RE.findall(value.strip().casefold())))


def _contains_subsequence(
    document: tuple[str, ...],
    query: tuple[str, ...],
) -> bool:
    if not query or len(query) > len(document):
        return False
    return any(
        document[offset : offset + len(query)] == query
        for offset in range(len(document) - len(query) + 1)
    )
