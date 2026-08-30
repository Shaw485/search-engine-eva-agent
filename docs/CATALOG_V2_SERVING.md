# Catalog v2 active serving

This module turns the approved weighted multi-field retrieval candidate into a
real, explicit canary search lane. It does not replace the immutable baseline
lane and it does not treat Owner approval alone as activation.

## Contracts

Catalog v2 indexes the following source fields with SQLite FTS5:

- `product_id`
- `product_title`
- `product_brand`
- `product_bullet_point`
- `product_description`
- `product_color`

`build_catalog_index_v2` keeps the deployed v1 reader and builder unchanged.
It validates the locked source size/hash and Parquet row count, reads only the
seven required/optional fields through PyArrow bounded record batches, rejects
duplicate `(locale, product_id)` keys, and incrementally writes both the content
table and external-content FTS index in one transaction per batch. It builds
into a temporary database, runs FTS external-content and SQLite integrity
checks, verifies row counts and metadata, fsyncs it and atomically replaces the
requested output. The official 1,814,924-product artifact is intentionally not
committed to Git.

The only supported v2 production pipeline is:

```text
title BM25 OR Top 50 -------------------- 1.0 --\
all-title-token / exact-ID Top 50 ------- 1.0 --- RRF(k=60) Top 20
multi-field BM25 OR Top 50 -------------- 0.1 --/       |
                                                        v
                                           title BM25 coarse Top 10
```

Each FTS channel first materializes only ranked `(rowid, score)` Top 50, then
hydrates those 50 rows from the external content table. Joining the full product
records before the cutoff made a cold common-term query read long descriptions
for a much larger match set: production validation measured about 10.46 seconds
for `wireless mouse`. The two-phase plan measured about 1.50 seconds for the
cold title channel and keeps the same retrieval cutoff and BM25 score.

Every active-lane response reports the strategy ID, immutable 64-character
strategy revision, index ID/schema, pipeline ID and actual per-stage counts.
The baseline mode reports its real baseline identity rather than claiming the
v2 strategy.

## Build without loading the full catalog into RAM

The production CLI is:

```bash
.venv/bin/python -m search_quality.catalog.v2_cli \
  --source data/raw/esci/shopping_queries_dataset_products.parquet \
  --lock data/esci.lock.json \
  --output data/index/catalog-v2.sqlite3 \
  --batch-size 5000 \
  --log-module catalog_index=INFO \
  2>catalog-v2-build.jsonl
```

The CLI requires a clean Git revision because that revision enters the index
identity. On the current 1.9 GiB server, begin with 5,000 rows per batch, ensure
the index and SQLite temporary files are on the filesystem with at least 30 GiB
free, and observe peak RSS and disk usage during the first formal build:

```bash
/usr/bin/time -v .venv/bin/python -m search_quality.catalog.v2_cli ...
df -h data/index
```

Do not copy the active pointer until the completed build event and independent
metadata/sentinel validation succeed. The source Parquet download is an
operator step and must still match `data/esci.lock.json`.

The first production attempt on 2026-08-30 is retained as negative operational
evidence: the former Polars `collect_batches` reader was killed before its first
SQLite batch at about 1.77 GiB peak RSS on the 1.9 GiB host. Writing to SQLite
in batches did not prove that the upstream Parquet decoder was memory-bounded.
The current implementation therefore uses `ParquetFile.iter_batches` with
memory mapping, pre-buffering and threaded decoding disabled; it also removes
the former unbounded final FTS `rebuild`/`optimize` phase. A new full build must
still prove the fix with `/usr/bin/time -v`; unit tests cannot measure Arrow's
C++ allocations.

## Approval, activation and rollback

`load_retrieval_activation_envelope` supplies an immutable Proposal plus an
Owner `approved_for_validation` decision. Pass that envelope to:

```python
receipt = validate_and_activate_retrieval_strategy(
    envelope,
    baseline_index_path,
    catalog_v2_index_path,
    artifact_root,
    revision_provider,
)
```

Activation verifies the content-addressed Proposal and decision, exact
`1/1/0.1` pipeline config/hash, parent serving revision, deployment Git
revision, v1/v2 index identities and a bounded two-query v2 sentinel. Any
failure raises a stable serving exception and leaves the active pointer
unchanged.

Successful validation writes content-addressed revision and receipt artifacts,
then atomically replaces only:

```text
<artifact_root>/retrieval-strategies/active.json
```

The pointer has exactly `schema_version`, `strategy_id` and
`strategy_revision`. Its target is always the immutable
`revisions/<strategy_revision>.json`; it never accepts a filesystem path.
Readers snapshot the pointer once and then validate the target hash, strategy,
pipeline and configured index identity. A concurrent switch therefore yields
either the complete old revision or the complete new revision, never a mixed
configuration.

`rollback_retrieval_strategy` requires the expected current revision and
atomically points at the recorded immutable rollback target. The initial v2
activation creates an immutable baseline target, so rollback also uses pointer
replacement rather than deleting state.

## API integration points

`apps/api/main.py` integrates the serving and release-control surfaces:

- `ActiveCatalogSearchService(...).readiness()` for an active-lane health route;
- `ActiveCatalogSearchService(...).search(query, top_k=10)` for
  `/catalog/search/active`;
- `load_active_retrieval_revision(artifact_root)` for Proposal parent revision
  and approval CAS;
- `validate_and_activate_retrieval_strategy(...)` after Owner approval;
- `rollback_retrieval_strategy(...)` for an Owner-only recovery route.

The API maps invalid Query input to 400, incompatible active state/index
to a generic 503 with request trace ID, stale activation/rollback CAS to 409,
and must never fall back to baseline while reporting the v2 strategy. The
existing `/catalog/search` remains the explicit baseline lane.

## Independent diagnostics

The three runtime areas have separate logger namespaces and switches:

- `catalog_index`: first-batch/100k-row build progress, peak RSS, integrity
  verification and atomic publication;
- `catalog_pipeline`: channel/fusion/coarse counts and bounded latency;
- `catalog_serving`: pointer resolution, activation, rollback and active-lane
  request boundaries.

Use `SEARCH_LOG_LEVEL=OFF` plus exactly one module override to isolate a layer.
Logs contain only IDs, revisions, counts, duration and stable error types/codes.
They never contain source/index paths, Query text, product IDs/content,
Proposal bodies, credentials or exception messages. CLI stderr files remain
operator-owned; production rotation and retention belong to journald as
documented in `docs/LOGGING.md`.
