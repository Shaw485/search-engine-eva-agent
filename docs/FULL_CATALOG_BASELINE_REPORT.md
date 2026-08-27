# Full-catalog baseline build and API report

**Date:** 2026-08-28  
**Status:** Local acceptance passed; website/server deployment pending  
**Purpose:** Give the Owner a real, intentionally unoptimized catalog search to
experience before choosing optimization work.

## Immutable identity

| Evidence | Value |
|---|---|
| Build code revision | `2866d8c2b3fed1f549d82dc04a38ff3dc2cedb98` |
| Index ID | `catalog-baseline-v1-f42e2120a938` |
| Index SHA-256 | `06134d90f4d12be96f52bc710636047a47102760ada2e707dd74061da085ab9c` |
| Index size | 527,306,752 bytes (503 MiB) |
| Source size | 1,108,857,465 bytes |
| Source SHA-256 | `25124442d064d64b26f74082d6fa09438d679efc0c183cf28d19064a2b65a265` |
| Schema | `catalog-sqlite-fts5-v1` |

The index is ignored by Git and remains at
`data/index/catalog-baseline-v1.sqlite3` on the development host. Deployment
must verify the recorded SHA-256 before installing the artifact.

## Completeness

The builder verified the pinned source identity, required fields, unique
`(locale, product_id)` keys, inserted row count and stored metadata before
atomic installation.

| Locale | Products |
|---|---:|
| US | 1,215,854 |
| ES | 260,011 |
| JP | 339,059 |
| **Total** | **1,814,924** |

Build duration was 47,951.587ms on the local development machine. This is an
offline one-time cost; the website does not read Parquet for each Query.

## Real API acceptance

The FastAPI service was started against the full index and tested through HTTP
POST, including its JSON serialization and request boundary.

| Check | Result |
|---|---|
| Health | `ready`, correct index ID and 1,814,924 products |
| English `wireless mouse` | 3 US results; HTTP 200 |
| Spanish `raton inalambrico` | 3 ES results; HTTP 200 |
| Japanese `ワイヤレス マウス` | 3 JP results; HTTP 200 |
| Exact ID `B088CYZHGX` | Exact product at rank 1; HTTP 200 |
| Unknown term | Empty hit list; HTTP 200 |

The first concurrent English request took 84.377ms while warming the process
and filesystem cache. A later 30-request sequential sample for
`wireless mouse`, top 10, produced:

| Samples | Min | p50 | p95 | Max |
|---:|---:|---:|---:|---:|
| 30 | 7.959ms | 12.550ms | 44.541ms | 46.757ms |

These numbers include local HTTP and JSON overhead. They characterize this
machine only and are not a production latency SLO.

## Verification

- Python formatting and lint: passed.
- Repository policy: passed.
- Full automated suite: **224 passed**.
- Website JavaScript syntax and local search-log store tests: passed.
- Catalog diagnostics were enabled independently during the build; no raw
  Query, product title or filesystem path was emitted.

## Known baseline weaknesses

- Query terms use `AND`, so verbose or misspelled Queries may return nothing.
- No typo correction, synonyms, semantic retrieval, popularity or behavioral
  signals are used.
- Japanese uses SQLite `unicode61`, not a language-specific analyzer.
- Search spans all locales without automatic locale selection.
- ESCI supplies no product images or prices, so the website does not invent
  them.
- Full-catalog availability is not full-catalog relevance evaluation. ESCI does
  not label every relevant product for arbitrary Queries, so this report makes
  no Recall or nDCG claim.

The next acceptance step is deployment to `shawspace.cn`, followed by Owner
experience and Bad Case collection. Those observations will guide the first
optimized strategy rather than retroactively changing this baseline.
