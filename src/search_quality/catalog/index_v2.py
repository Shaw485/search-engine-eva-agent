"""Build the full-field, immutable catalog v2 SQLite FTS5 index."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import resource
import sqlite3
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow.parquet as pq

CATALOG_V2_SCHEMA_VERSION = "catalog-sqlite-fts5-v2"
DEFAULT_CATALOG_V2_INDEX = Path("data/index/catalog-v2.sqlite3")
CATALOG_V2_PRODUCT_COLUMNS = (
    "product_id",
    "product_locale",
    "product_title",
    "product_brand",
    "product_bullet_point",
    "product_description",
    "product_color",
)
CATALOG_V2_INDEX_CONFIG: dict[str, Any] = {
    "backend": "sqlite-fts5",
    "content_storage": "external-content-table",
    "fields": [
        "product_id",
        "product_title",
        "product_brand",
        "product_bullet_point",
        "product_description",
        "product_color",
    ],
    "supported_pipeline_variants": ["title-exact-multifield-weighted-v1"],
    "tokenizer": "unicode61-remove-diacritics-2",
}

_REQUIRED_SOURCE_COLUMNS = frozenset({"product_id", "product_locale", "product_title"})
_OPTIONAL_SOURCE_COLUMNS = frozenset(CATALOG_V2_PRODUCT_COLUMNS) - (
    _REQUIRED_SOURCE_COLUMNS
)
_GIT_REVISION_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_INDEX_ID_PATTERN = re.compile(r"catalog-v2-[0-9a-f]{12}\Z")
logger = logging.getLogger("search_quality.catalog_index")


@dataclass(frozen=True, slots=True)
class CatalogV2IndexMetadata:
    """Verified identity and compatibility contract for a catalog v2 index."""

    index_id: str
    schema_version: str
    source_sha256: str
    source_size: int
    product_count: int
    locale_counts: dict[str, int]
    code_revision: str
    index_config: dict[str, Any]

    @classmethod
    def from_connection(cls, connection: sqlite3.Connection) -> CatalogV2IndexMetadata:
        try:
            rows = connection.execute(
                "SELECT key, value FROM catalog_metadata ORDER BY key"
            ).fetchall()
        except sqlite3.Error as exc:
            raise ValueError("catalog v2 index metadata is missing") from exc
        values = {str(key): str(value) for key, value in rows}
        required = {
            "code_revision",
            "index_config_json",
            "index_id",
            "locale_counts_json",
            "product_count",
            "schema_version",
            "source_sha256",
            "source_size",
        }
        if set(values) != required:
            raise ValueError("catalog v2 index metadata contract does not match")
        try:
            locale_counts = json.loads(values["locale_counts_json"])
            index_config = json.loads(values["index_config_json"])
            metadata = cls(
                index_id=values["index_id"],
                schema_version=values["schema_version"],
                source_sha256=values["source_sha256"],
                source_size=int(values["source_size"]),
                product_count=int(values["product_count"]),
                locale_counts={
                    str(locale): int(count) for locale, count in locale_counts.items()
                },
                code_revision=values["code_revision"],
                index_config=index_config,
            )
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(
                "catalog v2 index metadata contains invalid values"
            ) from exc
        metadata.validate()
        return metadata

    def validate(self) -> None:
        if self.schema_version != CATALOG_V2_SCHEMA_VERSION:
            raise ValueError("catalog v2 index uses an unsupported schema")
        if not _INDEX_ID_PATTERN.fullmatch(self.index_id):
            raise ValueError("catalog v2 index has an invalid index ID")
        if not _SHA256_PATTERN.fullmatch(self.source_sha256):
            raise ValueError("catalog v2 index has an invalid source hash")
        if not _GIT_REVISION_PATTERN.fullmatch(self.code_revision):
            raise ValueError("catalog v2 index has an invalid code revision")
        if self.source_size < 1 or self.product_count < 1:
            raise ValueError("catalog v2 index counts must be positive")
        if (
            not self.locale_counts
            or any(
                not locale or count < 1 for locale, count in self.locale_counts.items()
            )
            or sum(self.locale_counts.values()) != self.product_count
        ):
            raise ValueError("catalog v2 index locale counts are invalid")
        if self.index_config != CATALOG_V2_INDEX_CONFIG:
            raise ValueError("catalog v2 index configuration is unsupported")
        expected = _catalog_v2_index_id(
            source_sha256=self.source_sha256,
            source_size=self.source_size,
            product_count=self.product_count,
            locale_counts=self.locale_counts,
            code_revision=self.code_revision,
        )
        if self.index_id != expected:
            raise ValueError("catalog v2 index ID does not match its metadata identity")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code_revision": self.code_revision,
            "index_config": self.index_config,
            "index_id": self.index_id,
            "locale_counts": self.locale_counts,
            "product_count": self.product_count,
            "schema_version": self.schema_version,
            "source_sha256": self.source_sha256,
            "source_size": self.source_size,
        }


def build_catalog_index_v2(
    source_path: str | Path,
    output_path: str | Path,
    *,
    expected_source_size: int,
    expected_source_sha256: str,
    expected_product_count: int,
    code_revision: str,
    batch_size: int = 5_000,
) -> CatalogV2IndexMetadata:
    """Stream the product Parquet into an atomically published v2 index."""

    source = Path(source_path)
    output = Path(output_path)
    revision = code_revision.strip()
    _validate_build_inputs(
        source=source,
        revision=revision,
        batch_size=batch_size,
        expected_source_size=expected_source_size,
        expected_source_sha256=expected_source_sha256,
        expected_product_count=expected_product_count,
    )
    observed_size = source.stat().st_size
    if observed_size != expected_source_size:
        raise ValueError("catalog source size does not match the ESCI lock")
    observed_sha256 = _sha256_file(source)
    if observed_sha256 != expected_source_sha256:
        raise ValueError("catalog source hash does not match the ESCI lock")

    if output.is_symlink():
        raise ValueError("catalog v2 output must not be a symbolic link")
    parquet = _open_product_source(source)
    if parquet.metadata.num_rows != expected_product_count:
        parquet.close(force=True)
        raise ValueError("catalog Parquet row count does not match the contract")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    started = time.perf_counter()
    rows_indexed = 0
    locale_counts: Counter[str] = Counter()
    logger.info(
        "catalog_v2_index_build_started",
        extra={
            "batch_size": batch_size,
            "expected_product_count": expected_product_count,
            "source_size": observed_size,
        },
    )
    try:
        connection = sqlite3.connect(temporary)
        try:
            _prepare_database(connection)
            insert_sql = (
                "INSERT INTO catalog_product_records("
                "rowid, product_id, locale, title, brand, bullet_point, "
                "description, color"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            )
            fts_insert_sql = (
                "INSERT INTO catalog_products("
                "rowid, product_id, locale, title, brand, bullet_point, "
                "description, color"
                ") SELECT rowid, product_id, locale, title, brand, bullet_point, "
                "description, color FROM catalog_product_records "
                "WHERE rowid BETWEEN ? AND ? ORDER BY rowid"
            )
            next_progress_log = 100_000
            try:
                for record_batch in parquet.iter_batches(
                    batch_size=batch_size,
                    columns=_available_product_columns(parquet),
                    use_threads=False,
                    use_pandas_metadata=False,
                ):
                    batch = _normalize_product_batch(pl.from_arrow(record_batch))
                    _validate_product_batch(batch)
                    first_rowid = rows_indexed + 1
                    batch_count = batch.height
                    last_rowid = rows_indexed + batch_count
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        connection.executemany(
                            insert_sql,
                            (
                                (rowid, *row)
                                for rowid, row in enumerate(
                                    batch.iter_rows(), start=first_rowid
                                )
                            ),
                        )
                        connection.execute(fts_insert_sql, (first_rowid, last_rowid))
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    rows_indexed = last_rowid
                    locale_counts.update(batch.get_column("product_locale").to_list())
                    logger.debug(
                        "catalog_v2_index_batch_indexed",
                        extra={
                            "batch_count": batch_count,
                            "rows_indexed": rows_indexed,
                        },
                    )
                    if first_rowid == 1 or rows_indexed >= next_progress_log:
                        logger.info(
                            "catalog_v2_index_progress",
                            extra={
                                "batch_count": batch_count,
                                "peak_rss_bytes": _peak_rss_bytes(),
                                "rows_indexed": rows_indexed,
                            },
                        )
                        while next_progress_log <= rows_indexed:
                            next_progress_log += 100_000
            finally:
                parquet.close(force=True)
            if rows_indexed != expected_product_count:
                raise ValueError("catalog product count does not match the contract")
            metadata = _metadata_for_build(
                source_sha256=observed_sha256,
                source_size=observed_size,
                product_count=rows_indexed,
                locale_counts=dict(sorted(locale_counts.items())),
                code_revision=revision,
            )
            _store_metadata(connection, metadata)
            connection.commit()
            connection.execute(
                "INSERT INTO catalog_products(catalog_products, rank) "
                "VALUES ('integrity-check', 1)"
            )
            stored = connection.execute(
                "SELECT count(*) FROM catalog_product_records"
            ).fetchone()[0]
            indexed = connection.execute(
                "SELECT count(*) FROM catalog_products"
            ).fetchone()[0]
            if stored != rows_indexed or indexed != rows_indexed:
                raise RuntimeError("catalog v2 index row count verification failed")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity != ("ok",):
                raise RuntimeError("catalog v2 SQLite integrity check failed")
        finally:
            connection.close()
        with temporary.open("rb+") as handle:
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, output)
        _fsync_directory(output.parent)
    except Exception as exc:
        logger.error(
            "catalog_v2_index_build_failed",
            extra={
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "error_type": type(exc).__name__,
                "peak_rss_bytes": _peak_rss_bytes(),
                "rows_indexed": rows_indexed,
            },
        )
        raise
    finally:
        with contextlib.suppress(Exception):
            parquet.close(force=True)
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        for suffix in ("-journal", "-shm", "-wal"):
            with contextlib.suppress(FileNotFoundError):
                Path(f"{temporary}{suffix}").unlink()

    logger.info(
        "catalog_v2_index_build_completed",
        extra={
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "index_id": metadata.index_id,
            "peak_rss_bytes": _peak_rss_bytes(),
            "product_count": metadata.product_count,
            "size_bytes": output.stat().st_size,
        },
    )
    return metadata


def _validate_build_inputs(
    *,
    source: Path,
    revision: str,
    batch_size: int,
    expected_source_size: int,
    expected_source_sha256: str,
    expected_product_count: int,
) -> None:
    if not _GIT_REVISION_PATTERN.fullmatch(revision):
        raise ValueError("code_revision must be a full lowercase Git commit SHA")
    for name, value in (
        ("batch_size", batch_size),
        ("expected_source_size", expected_source_size),
        ("expected_product_count", expected_product_count),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be positive")
    if not _SHA256_PATTERN.fullmatch(expected_source_sha256):
        raise ValueError("expected_source_sha256 must be a lowercase SHA-256")
    if source.is_symlink() or not source.is_file():
        raise FileNotFoundError(source)


def _open_product_source(source: Path) -> pq.ParquetFile:
    parquet = pq.ParquetFile(source, memory_map=True, pre_buffer=False)
    missing = sorted(_REQUIRED_SOURCE_COLUMNS - set(parquet.schema_arrow.names))
    if missing:
        raise ValueError(f"catalog source is missing required columns: {missing}")
    return parquet


def _available_product_columns(parquet: pq.ParquetFile) -> list[str]:
    available = set(parquet.schema_arrow.names)
    return [column for column in CATALOG_V2_PRODUCT_COLUMNS if column in available]


def _normalize_product_batch(batch: pl.DataFrame) -> pl.DataFrame:
    available = set(batch.columns)
    expressions: list[pl.Expr] = []
    for column in CATALOG_V2_PRODUCT_COLUMNS:
        if column in available:
            expression = pl.col(column)
        elif column in _OPTIONAL_SOURCE_COLUMNS:
            expression = pl.lit("")
        else:  # pragma: no cover - guarded by the required-column check
            raise AssertionError(column)
        if column == "product_locale":
            expression = expression.cast(pl.String).str.strip_chars().str.to_lowercase()
        elif column in _OPTIONAL_SOURCE_COLUMNS:
            expression = expression.fill_null("").cast(pl.String).str.strip_chars()
        else:
            expression = expression.cast(pl.String).str.strip_chars()
        expressions.append(expression.alias(column))
    return batch.select(expressions)


def _validate_product_batch(batch: pl.DataFrame) -> None:
    for column in ("product_id", "product_locale", "product_title"):
        if batch.filter(pl.col(column).is_null() | (pl.col(column) == "")).height:
            raise ValueError(f"catalog source has empty {column} values")


def _prepare_database(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA page_size=4096")
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute("PRAGMA cache_size=-32768")
    connection.execute("PRAGMA locking_mode=EXCLUSIVE")
    connection.execute(
        "CREATE TABLE catalog_metadata ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL"
        ") WITHOUT ROWID"
    )
    connection.execute(
        "CREATE TABLE catalog_product_records ("
        "rowid INTEGER PRIMARY KEY, "
        "product_id TEXT NOT NULL, locale TEXT NOT NULL, title TEXT NOT NULL, "
        "brand TEXT NOT NULL, bullet_point TEXT NOT NULL, "
        "description TEXT NOT NULL, color TEXT NOT NULL, "
        "UNIQUE(locale, product_id)"
        ")"
    )
    connection.execute(
        "CREATE INDEX catalog_product_id_nocase "
        "ON catalog_product_records(product_id COLLATE NOCASE)"
    )
    connection.execute(
        "CREATE VIRTUAL TABLE catalog_products USING fts5("
        "product_id, locale UNINDEXED, title, brand, bullet_point, description, color, "
        "content='catalog_product_records', content_rowid='rowid', "
        "tokenize='unicode61 remove_diacritics 2'"
        ")"
    )


def _metadata_for_build(
    *,
    source_sha256: str,
    source_size: int,
    product_count: int,
    locale_counts: dict[str, int],
    code_revision: str,
) -> CatalogV2IndexMetadata:
    metadata = CatalogV2IndexMetadata(
        index_id=_catalog_v2_index_id(
            source_sha256=source_sha256,
            source_size=source_size,
            product_count=product_count,
            locale_counts=locale_counts,
            code_revision=code_revision,
        ),
        schema_version=CATALOG_V2_SCHEMA_VERSION,
        source_sha256=source_sha256,
        source_size=source_size,
        product_count=product_count,
        locale_counts=locale_counts,
        code_revision=code_revision,
        index_config=json.loads(json.dumps(CATALOG_V2_INDEX_CONFIG)),
    )
    metadata.validate()
    return metadata


def _catalog_v2_index_id(
    *,
    source_sha256: str,
    source_size: int,
    product_count: int,
    locale_counts: dict[str, int],
    code_revision: str,
) -> str:
    identity = {
        "code_revision": code_revision,
        "index_config": CATALOG_V2_INDEX_CONFIG,
        "locale_counts": locale_counts,
        "product_count": product_count,
        "schema_version": CATALOG_V2_SCHEMA_VERSION,
        "source_sha256": source_sha256,
        "source_size": source_size,
    }
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return f"catalog-v2-{hashlib.sha256(canonical).hexdigest()[:12]}"


def _store_metadata(
    connection: sqlite3.Connection,
    metadata: CatalogV2IndexMetadata,
) -> None:
    values = {
        "code_revision": metadata.code_revision,
        "index_config_json": json.dumps(
            metadata.index_config,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        "index_id": metadata.index_id,
        "locale_counts_json": json.dumps(
            metadata.locale_counts,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        "product_count": str(metadata.product_count),
        "schema_version": metadata.schema_version,
        "source_sha256": metadata.source_sha256,
        "source_size": str(metadata.source_size),
    }
    connection.executemany(
        "INSERT INTO catalog_metadata(key, value) VALUES (?, ?)",
        sorted(values.items()),
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _peak_rss_bytes() -> int:
    peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    # macOS reports bytes; Linux reports KiB.
    return peak if sys.platform == "darwin" else peak * 1024


# A short alias is convenient for callers while the explicit v2 name remains clear.
CatalogIndexV2Metadata = CatalogV2IndexMetadata
