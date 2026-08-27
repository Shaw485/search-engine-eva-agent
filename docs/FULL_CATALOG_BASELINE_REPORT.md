# Full-catalog baseline build and API report

**Date:** 2026-08-28  
**Status:** Production deployed and technically accepted; Owner experience and
Bad Case collection pending
**Purpose:** Give the Owner a real, intentionally unoptimized catalog search to
experience before choosing optimization work.

## Immutable identity

| Evidence | Value |
|---|---|
| Build code revision | `868a86175fd1693512224fd68b96005055f4f786` |
| Index ID | `catalog-baseline-v1-de52d3f2b913` |
| Index SHA-256 | `1d5005c9a7ed16a8a569f1ee0bf4ea2269a30de18e2c5f8b66fcc30ee1176bc1` |
| Index size | 527,306,752 bytes (503 MiB) |
| Source size | 1,108,857,465 bytes |
| Source SHA-256 | `25124442d064d64b26f74082d6fa09438d679efc0c183cf28d19064a2b65a265` |
| Schema | `catalog-sqlite-fts5-v1` |

The production index was rebuilt from the pinned source on the deployment host,
verified as a candidate, copied to a same-filesystem `.new` path and atomically
renamed to `/var/lib/search-engine-eva-agent/catalog-baseline-v1.sqlite3`.
It is owned by `root:www-data` with mode `0640`; the service opens it read-only.

The earlier development reference build remains valid historical evidence:
revision `2866d8c2b3fed1f549d82dc04a38ff3dc2cedb98`, index ID
`catalog-baseline-v1-f42e2120a938` and SHA-256
`06134d90f4d12be96f52bc710636047a47102760ada2e707dd74061da085ab9c`.
Index identity includes the full code revision, so the production rebuild has a
different ID even though the intervening committed change only recorded the
local acceptance evidence.

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

The production build took 32,912.655ms, peaked at about 1.0 GiB of memory and
used no swap. The earlier development build took 47,951.587ms. These are
offline one-time costs; the website does not read Parquet for each Query.

## Production API and website acceptance

The FastAPI service was started against the full index and tested through HTTP
POST, including its JSON serialization and request boundary.

| Check | Result |
|---|---|
| Health | Nested catalog status `ready`, production index ID and 1,814,924 products |
| English `wireless mouse` | 3 US results; HTTP 200 |
| Spanish `ratón inalámbrico` | 3 ES results; HTTP 200 |
| Japanese `ワイヤレス マウス` | 3 JP results; HTTP 200 |
| Exact ID `B088CYZHGX` | Exact product at rank 1; HTTP 200 |
| Unknown term | Empty hit list; HTTP 200 |
| Public reverse proxy | `/search-eval-api/catalog/search`; HTTP 200 with production index ID |
| Browser UI | Results rendered on `shawspace.cn`; optimized lane remained disabled |

The first concurrent English request took 84.377ms while warming the process
and filesystem cache. A later 30-request sequential sample for
`wireless mouse`, top 10, produced:

| Samples | Min | p50 | p95 | Max |
|---:|---:|---:|---:|---:|
| 30 | 7.959ms | 12.550ms | 44.541ms | 46.757ms |

These numbers include local HTTP and JSON overhead. They characterize the
development machine only and are not a production latency SLO.

The six fixed production API requests recorded server-side durations from
1.282ms to 7.523ms after startup. Browser-observed end-to-end times were 216ms
for `wireless mouse`, 222ms for `ratón inalámbrico` and 203ms for the exact
product ID. The browser numbers include public network, TLS, API, JSON and UI
rendering time and likewise are not an SLO.

## Verification

- Python formatting and lint: passed.
- Repository policy: passed.
- Full automated suite: **224 passed**.
- Website JavaScript syntax and local search-log store tests: passed.
- Catalog diagnostics were enabled independently during the build; no raw
  Query, product title or filesystem path was emitted.
- Server dependency integrity (`pip check`) and `www-data` runtime imports:
  passed.
- Production service: systemd `active`; nested catalog health contract passed.
- Nginx configuration test and public HTTPS reverse-proxy request: passed.
- Production request logs contain module, route, method, status, duration and
  Trace ID but no Query or product fields; browser debug output is disabled and
  no warning/error was observed.

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

The next acceptance step is Owner firsthand experience and Bad Case collection
on [the live comparison page](https://shawspace.cn/search-eval.html). Those
observations will guide the first optimized strategy rather than retroactively
changing this baseline.
