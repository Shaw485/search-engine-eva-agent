"""Build an atomic SQLite FTS5 index over every official ESCI product."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

CATALOG_SCHEMA_VERSION = "catalog-sqlite-fts5-v1"
DEFAULT_CATALOG_INDEX = Path("data/index/catalog-baseline-v1.sqlite3")
EXPECTED_PRODUCT_COUNT = 1_814_924
PRODUCT_COLUMNS = (
    "product_id",
    "product_locale",
    "product_title",
    "product_brand",
    "product_color",
)
INDEX_CONFIG: dict[str, Any] = {
    "backend": "sqlite-fts5",
    "fields": [
        "product_id",
        "product_title",
        "product_brand",
        "product_color",
    ],
    "field_weights": {
        "product_id": 8.0,
        "product_title": 4.0,
        "product_brand": 2.0,
        "product_color": 1.0,
    },
    "query_operator": "AND",
    "tokenizer": "unicode61-remove-diacritics-2",
}
_GIT_REVISION_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_INDEX_ID_PATTERN = re.compile(r"catalog-baseline-v1-[0-9a-f]{12}\Z")
logger = logging.getLogger("search_quality.catalog")


@dataclass(frozen=True, slots=True)
class CatalogIndexMetadata:
    """Small verified identity read without opening the source Parquet file."""

    index_id: str
    schema_version: str
    source_sha256: str
    source_size: int
    product_count: int
    locale_counts: dict[str, int]
    code_revision: str
    index_config: dict[str, Any]

    @classmethod
    def from_connection(cls, connection: sqlite3.Connection) -> CatalogIndexMetadata:
        try:
            rows = connection.execute(
                "SELECT key, value FROM catalog_metadata ORDER BY key"
            ).fetchall()
        except sqlite3.Error as exc:
            raise ValueError("catalog index metadata is missing") from exc
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
            raise ValueError("catalog index metadata contract does not match")
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
            raise ValueError("catalog index metadata contains invalid values") from exc
        metadata.validate()
        return metadata

    def validate(self) -> None:
        if self.schema_version != CATALOG_SCHEMA_VERSION:
            raise ValueError("catalog index uses an unsupported schema")
        if not _INDEX_ID_PATTERN.fullmatch(self.index_id):
            raise ValueError("catalog index has an invalid index ID")
        if not _SHA256_PATTERN.fullmatch(self.source_sha256):
            raise ValueError("catalog index has an invalid source hash")
        if not _GIT_REVISION_PATTERN.fullmatch(self.code_revision):
            raise ValueError("catalog index has an invalid code revision")
        if self.source_size < 1 or self.product_count < 1:
            raise ValueError("catalog index counts must be positive")
        if (
            not self.locale_counts
            or any(
                not locale or count < 1 for locale, count in self.locale_counts.items()
            )
            or sum(self.locale_counts.values()) != self.product_count
        ):
            raise ValueError("catalog index locale counts are invalid")
        if self.index_config != INDEX_CONFIG:
            raise ValueError("catalog index configuration is unsupported")

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


def load_product_lock(lock_path: str | Path) -> tuple[int, str]:
    """Return the pinned product file size/hash from the official ESCI lock."""

    payload = json.loads(Path(lock_path).read_text(encoding="utf-8"))
    matches = [
        item
        for item in payload.get("files", [])
        if item.get("path")
        == "shopping_queries_dataset/shopping_queries_dataset_products.parquet"
    ]
    if len(matches) != 1:
        raise ValueError("ESCI lock must define exactly one product file")
    item = matches[0]
    size = item.get("size")
    sha256 = item.get("sha256")
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or size < 1
        or not isinstance(sha256, str)
        or not _SHA256_PATTERN.fullmatch(sha256)
    ):
        raise ValueError("ESCI product lock identity is invalid")
    return size, sha256


def build_catalog_index(
    source_path: str | Path,
    output_path: str | Path,
    *,
    expected_source_size: int,
    expected_source_sha256: str,
    expected_product_count: int,
    code_revision: str,
    batch_size: int = 10_000,
) -> CatalogIndexMetadata:
    """Build into a temporary database and atomically replace the live index."""

    source = Path(source_path)
    output = Path(output_path)
    revision = code_revision.strip()
    if not _GIT_REVISION_PATTERN.fullmatch(revision):
        raise ValueError("code_revision must be a full lowercase Git commit SHA")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if expected_product_count < 1:
        raise ValueError("expected_product_count must be positive")
    if not _SHA256_PATTERN.fullmatch(expected_source_sha256):
        raise ValueError("expected_source_sha256 must be a lowercase SHA-256")
    if not source.is_file():
        raise FileNotFoundError(source)
    observed_size = source.stat().st_size
    if observed_size != expected_source_size:
        raise ValueError("catalog source size does not match the ESCI lock")
    observed_sha256 = _sha256_file(source)
    if observed_sha256 != expected_source_sha256:
        raise ValueError("catalog source hash does not match the ESCI lock")

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
    logger.info(
        "catalog_index_build_started",
        extra={
            "batch_size": batch_size,
            "expected_product_count": expected_product_count,
            "source_size": observed_size,
        },
    )
    try:
        frame = _load_products(source)
        product_count = frame.height
        if product_count != expected_product_count:
            raise ValueError("catalog product count does not match the contract")
        locale_counts = {
            str(row["product_locale"]): int(row["len"])
            for row in frame.group_by("product_locale")
            .len()
            .sort("product_locale")
            .iter_rows(named=True)
        }
        metadata = _metadata_for_build(
            source_sha256=observed_sha256,
            source_size=observed_size,
            product_count=product_count,
            locale_counts=locale_counts,
            code_revision=revision,
        )
        connection = sqlite3.connect(temporary)
        try:
            _prepare_database(connection)
            connection.execute("BEGIN")
            insert_sql = (
                "INSERT INTO catalog_products"
                "(product_id, locale, title, brand, color) VALUES (?, ?, ?, ?, ?)"
            )
            for batch in frame.iter_slices(n_rows=batch_size):
                connection.executemany(insert_sql, batch.iter_rows())
                rows_indexed += batch.height
                logger.debug(
                    "catalog_index_batch_indexed",
                    extra={
                        "batch_count": batch.height,
                        "rows_indexed": rows_indexed,
                    },
                )
            _store_metadata(connection, metadata)
            connection.commit()
            connection.execute(
                "INSERT INTO catalog_products(catalog_products) VALUES ('optimize')"
            )
            connection.commit()
            stored = connection.execute(
                "SELECT count(*) FROM catalog_products"
            ).fetchone()[0]
            if stored != product_count:
                raise RuntimeError("catalog index row count verification failed")
        finally:
            connection.close()
        with temporary.open("rb+") as handle:
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, output)
        _fsync_directory(output.parent)
    except Exception:
        logger.error(
            "catalog_index_build_failed",
            extra={
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "rows_indexed": rows_indexed,
            },
        )
        raise
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        with contextlib.suppress(FileNotFoundError):
            Path(f"{temporary}-journal").unlink()

    logger.info(
        "catalog_index_build_completed",
        extra={
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "index_id": metadata.index_id,
            "product_count": metadata.product_count,
            "size_bytes": output.stat().st_size,
        },
    )
    return metadata


def _load_products(source: Path) -> pl.DataFrame:
    frame = pl.read_parquet(
        source,
        columns=list(PRODUCT_COLUMNS),
        low_memory=True,
        memory_map=True,
    ).with_columns(
        pl.col("product_id").cast(pl.String).str.strip_chars(),
        pl.col("product_locale").cast(pl.String).str.strip_chars().str.to_lowercase(),
        pl.col("product_title").cast(pl.String).str.strip_chars(),
        pl.col("product_brand").fill_null("").cast(pl.String).str.strip_chars(),
        pl.col("product_color").fill_null("").cast(pl.String).str.strip_chars(),
    )
    if frame.is_empty():
        raise ValueError("catalog source must not be empty")
    for column in ("product_id", "product_locale", "product_title"):
        if frame.filter(pl.col(column).is_null() | (pl.col(column) == "")).height:
            raise ValueError(f"catalog source has empty {column} values")
    unique_products = frame.select(
        pl.struct("product_locale", "product_id").n_unique()
    ).item()
    if unique_products != frame.height:
        raise ValueError("catalog source has duplicate locale/product keys")
    return frame


def _prepare_database(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA page_size=4096")
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute("PRAGMA cache_size=-131072")
    connection.execute("PRAGMA locking_mode=EXCLUSIVE")
    connection.execute(
        "CREATE TABLE catalog_metadata ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL"
        ") WITHOUT ROWID"
    )
    connection.execute(
        "CREATE VIRTUAL TABLE catalog_products USING fts5("
        "product_id, locale UNINDEXED, title, brand, color, "
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
) -> CatalogIndexMetadata:
    identity = {
        "code_revision": code_revision,
        "index_config": INDEX_CONFIG,
        "locale_counts": locale_counts,
        "product_count": product_count,
        "schema_version": CATALOG_SCHEMA_VERSION,
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
    metadata = CatalogIndexMetadata(
        index_id=f"catalog-baseline-v1-{hashlib.sha256(canonical).hexdigest()[:12]}",
        schema_version=CATALOG_SCHEMA_VERSION,
        source_sha256=source_sha256,
        source_size=source_size,
        product_count=product_count,
        locale_counts=locale_counts,
        code_revision=code_revision,
        index_config=json.loads(json.dumps(INDEX_CONFIG)),
    )
    metadata.validate()
    return metadata


def _store_metadata(
    connection: sqlite3.Connection,
    metadata: CatalogIndexMetadata,
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
