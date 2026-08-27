# ADR-002: Website full-catalog lexical baseline

**Status:** Accepted for the current experience milestone  
**Date:** 2026-08-28  
**Product decision:** The Owner decided to make every official ESCI product
searchable on the existing portfolio website before optimizing relevance.  
**Technical design:** Codex proposed and implemented the SQLite FTS5 index,
field contract, API boundary, diagnostics and deployment plan. See D-015 in
[`docs/CONTRIBUTION_LOG.md`](../CONTRIBUTION_LOG.md).

## Context

The Owner needs to experience an intentionally simple baseline over the full
official ESCI product catalog, then use observed failures to guide later
optimization. The existing ten-product smoke fixture cannot provide that
experience. Reading the 1.1 GB Parquet file for every request would also make
interactive search impractical.

This product-search corpus has 1,814,924 unique `(locale, product_id)` rows:
1,215,854 US, 260,011 ES and 339,059 JP products. ESCI does not provide product
images, prices or complete relevance judgments for arbitrary full-catalog
queries.

## Requirements

- Keep the experience on `shawspace.cn/search-eval.html`.
- Search all official products across US, ES and JP locales.
- Preserve an intentionally limited lexical baseline with obvious room to
  improve.
- Return at most 20 products through a same-origin JSON API.
- Keep raw Query text out of URLs, access logs and server diagnostic logs.
- Build an immutable, verified index without committing large artifacts to Git.
- Keep full-catalog browsing separate from the frozen relevance-evaluation
  boundary.

## Decision

Build a versioned SQLite FTS5 index containing these fields:

| Field | Indexed | BM25 weight | Purpose |
|---|---:|---:|---|
| `product_id` | Yes | 8.0 | Exact identifier/model-like lookup |
| `product_title` | Yes | 4.0 | Primary lexical relevance |
| `product_brand` | Yes | 2.0 | Brand intent |
| `product_color` | Yes | 1.0 | Simple attribute intent |
| `product_locale` | No | — | Result provenance and stable tie-break |

The Query is Unicode-tokenized, duplicate tokens are removed, every token is
quoted, and tokens are combined with `AND`. There is no spelling correction,
synonym expansion, semantic recall, behavioral signal, personalization or
learned reranking. Those omissions are part of the baseline, not hidden
fallbacks.

```text
shawspace.cn/search-eval.html
            │ POST /search-eval-api/catalog/search
            ▼
Nginx (same origin, access log off)
            ▼
FastAPI on 127.0.0.1:8010
            ▼
read-only SQLite FTS5 index
            ▼
BM25 top K + title/brand/color/locale/product ID
```

The build validates the pinned source size and SHA-256, row count, required
fields and key uniqueness. It writes a temporary database, verifies the stored
row count, fsyncs it, then atomically replaces the live artifact. Runtime opens
the file in read-only immutable mode and verifies schema/config/source metadata.

## Options considered

| Option | Assessment |
|---|---|
| Scan Parquet per request | Simplest code, but repeatedly scans 1.1 GB and cannot provide interactive latency |
| SQLite FTS5 | No new server process, persistent inverted index, deployable on the current small host |
| OpenSearch | Better growth path and richer analyzers, but adds JVM/service memory and operations before the baseline needs them |

SQLite FTS5 is selected for this milestone. Revisit OpenSearch when the product
needs multi-field analyzers, typo tolerance, online index updates, replicas,
facets or multiple concurrent ranking stages.

## Consequences and boundaries

- Full-catalog lookup becomes interactive without loading Parquet per request.
- The index artifact is large and must be built/deployed separately from Git.
- `unicode61` is a weak baseline for Japanese tokenization and compound terms.
- `AND` improves precision but can produce zero results for verbose or misspelled
  queries. This gives later optimization a measurable target.
- Full-catalog searchability does **not** imply full-catalog Recall can be
  measured. Unjudged products remain unknown.
- Product images and prices are not invented; the site renders a neutral visual
  and only fields present in ESCI.

## Diagnostics and verification

The `catalog` log module is independently configurable. Build/search events
contain index ID, counts, duration and stable errors, never Query text, titles,
response bodies or paths. API events carry a generated request/trace ID. Unit
tests cover deterministic ranking, source/index validation, atomic failure,
syntax injection, multilingual lookup, safe errors and log privacy.
