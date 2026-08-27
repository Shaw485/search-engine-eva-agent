# Search Engine EVA Agent

An evidence-driven evaluation and diagnosis agent for e-commerce search ranking.
The project uses Amazon Shopping Queries ESCI to compare BM25, vector retrieval,
hybrid ranking, and Cross-Encoder reranking. Every conclusion must be traceable
to a dataset version, run configuration, metric, and ranked product list.

## Project status

**Full-catalog website baseline plus Stage 2 Harness: in progress.** The Owner
has prioritized an experience milestone: all 1,814,924 official ESCI products
must first be searchable on the existing portfolio page, while the optimized
lane remains closed. This product-search track uses a persistent SQLite FTS5
index and does not unlock or weaken the relevance-evaluation test boundary.

The Stage 1 technical data
gate is complete, while its owner learning check remains pending. The repository
now includes hand-verified nDCG, MRR and Success metrics, the versioned
`esci-primary-v1` relevance policy, a shared label-blind Ranker Harness, and
deterministic random, keyword-overlap and title-BM25 comparators. Routine Stage
2 now also has a strict `compare_runs` path: it verifies both Runs against the
trusted Stage 1 manifest, recomputes their metrics, and emits aggregate plus
per-Query ranking differences. Execution remains smoke-only: the 500-Query dev
profile is code-locked until the Owner data-boundary checkpoint is recorded, and
the 8,956-Query frozen test remains unavailable to tuning runs.

The optional OpenSearch 3.8.0 adapter, mapping, and Apple Silicon-compatible
Compose profile are implemented. Live container verification remains pending
because Docker is not installed on the current development host. This is an
explicit pending integration check, not an implicit fallback or a claimed pass.

- Full execution guide: [ROADMAP.md](ROADMAP.md)
- Stage 0 evidence: [docs/STAGE_0_REPORT.md](docs/STAGE_0_REPORT.md)
- Stage 1 evidence: [docs/STAGE_1_REPORT.md](docs/STAGE_1_REPORT.md)
- Stage 2 smoke evidence: [docs/STAGE_2_SMOKE_REPORT.md](docs/STAGE_2_SMOKE_REPORT.md)
- Full-catalog build/API evidence: [docs/FULL_CATALOG_BASELINE_REPORT.md](docs/FULL_CATALOG_BASELINE_REPORT.md)
- Data dictionary: [docs/DATA_DICTIONARY.md](docs/DATA_DICTIONARY.md)
- Backend decision: [docs/adr/001-search-backend.md](docs/adr/001-search-backend.md)
- Full-catalog baseline decision: [docs/adr/002-full-catalog-baseline.md](docs/adr/002-full-catalog-baseline.md)
- Required learning: [docs/LEARNING_CHECKPOINTS.md](docs/LEARNING_CHECKPOINTS.md)
- Decision and contribution provenance: [docs/CONTRIBUTION_LOG.md](docs/CONTRIBUTION_LOG.md)
- Logging and independent diagnostics: [docs/LOGGING.md](docs/LOGGING.md)
- Portfolio deployment: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)
- Live Stage 0 experience: [shawspace.cn/search-eval.html](https://shawspace.cn/search-eval.html)

## Quick start

Prerequisites: Python 3.11–3.13 and GNU Make. The reference development versions
are recorded in `.python-version` and `.nvmrc`; Node is not needed until the Web
stage.

```bash
git clone https://github.com/Shaw485/search-engine-eva-agent.git
cd search-engine-eva-agent
make setup
make check
```

`make check` runs formatting/lint checks, repository policy checks, all tests,
and the deterministic local smoke path. Individual commands are also available:

```bash
make test
make data-sample
make smoke
make eval-baseline
make compare-runs
make catalog-index
EVAL_RANKER=title-bm25 make eval-baseline
QUERY="iphone 15 pro case" make smoke
```

`make eval-baseline` runs all three deterministic comparators on the fixed
20-Query smoke profile. The smoke results validate the Harness; they are not a
formal quality decision. `make compare-runs` then compares the latest random
baseline with title BM25 and stores immutable JSON plus a Markdown diagnostic
report under ignored `runs/comparisons/`.

The Stage 2 CLI trusts the local operator and only loads artifacts directly from
the project's ignored `runs/` store. Run and comparison IDs are content hashes:
they detect accidental or conflicting content changes, but they are not digital
signatures and do not prove that a ranking was produced by the declared code.
The Stage 3 Agent therefore will accept validated Run IDs from a controlled
registry, never arbitrary filesystem paths.

The Stage 1 data path is separate so CI never downloads the 1.16 GB source:

```bash
make data-download
make data-esci-validate
make data-esci-build
```

After the pinned source is present, `make catalog-index` builds the ignored
full-catalog artifact at `data/index/catalog-baseline-v1.sqlite3`. The command
requires a clean Git revision so the index identity can record the exact code.

Start the Stage 0 API with:

```bash
make api
curl http://127.0.0.1:8000/health
curl --request POST 'http://127.0.0.1:8000/smoke' \
  --header 'Content-Type: application/json' \
  --data '{"query":"wireless mouse","top_k":3,"backend":"local"}'

curl --request POST 'http://127.0.0.1:8000/catalog/search' \
  --header 'Content-Type: application/json' \
  --data '{"query":"wireless mouse","top_k":10}'
```

## What the Stage 0 vector result means

Stage 0 uses `deterministic-hash-v1`, a 64-dimensional hashing vector with no
downloaded model. It verifies vector dimensions, indexing, cosine ranking,
determinism, and backend interchangeability. It is **not a semantic embedding**
and its rankings are not evidence that vector search improves relevance. A
versioned semantic model is introduced in Stage 6 and evaluated only after the
Stage 2 metrics are trusted.

The local and OpenSearch adapters share a method and normalized-result contract,
not identical scores or rankings. Their analyzers and BM25 field weights differ.
Later experiment manifests must record the backend and configuration; quality is
compared with relevance metrics rather than raw cross-backend scores.

## Architecture

```text
10-product JSON fixture
          │
          ├── deterministic embedding provider ──┐
          │                                      │
          └──────────── ProductDocument ─────────┘
                             │
                    SearchBackend contract
                       ┌─────┴─────┐
                       │           │
                Local backend   OpenSearch adapter
                BM25 + cosine   BM25 + Lucene k-NN
                 (required)       (optional)
                       └─────┬─────┘
                             │
                  normalized hits + smoke JSON
```

The embedding provider is intentionally separate from storage. Search and
future evaluation code do not depend on an LLM or an external model API.

## Optional OpenSearch smoke

OpenSearch is not required for the accepted local Stage 0 path. To run the
optional integration after installing Docker Desktop:

```bash
make opensearch-up
make smoke-opensearch
make opensearch-down
```

Allocate at least 4 GB to Docker Desktop. The image is pinned by version and
multi-architecture digest, so Apple Silicon runs the native ARM64 image without
forcing `linux/amd64`.

This Compose profile disables the OpenSearch security plugin and binds port
9200 only to `127.0.0.1`. It is for a private local smoke test only and must
never be exposed or reused as a shared/production configuration. Index reset is
protected by localhost, project-prefix, explicit opt-in, and cluster identity
checks. Do not run multiple OpenSearch smoke commands concurrently; Stage 0
rebuilds a fixed disposable index.

Official references: [OpenSearch downloads](https://opensearch.org/downloads/),
[Docker installation](https://docs.opensearch.org/latest/install-and-configure/install-opensearch/docker/),
and [k-NN vector fields](https://docs.opensearch.org/latest/mappings/supported-field-types/knn-vector/).

## Dataset

The official Amazon ESCI repository is pinned under `data/esci-data` as a Git
submodule. Its two Parquet files are stored upstream with Git LFS; the products
file alone is about 1.03 GB. The download command retrieves the objects at the
pinned commit directly into ignored `data/raw/esci/` and verifies their exact
sizes and SHA-256 hashes. Git LFS is not required locally.

The full dataset is not downloaded by `make setup` or CI:

```bash
make data-download
make data-esci-build
```

Raw files stay under `data/raw/esci/`; generated Parquet stays under
`data/processed/esci-stage1-v1/`. Neither directory is committed.
The repository does commit the 175 KB real ESCI smoke profile at
`data/samples/esci-stage1-smoke.parquet` so the schema and labels can be
inspected without the full download.

- Dataset source: [amazon-science/esci-data](https://github.com/amazon-science/esci-data)
- Pinned upstream commit: `7916cdf6ab75a462e77f20ab40428a10923998d5`
- Upstream license: [Apache-2.0](https://github.com/amazon-science/esci-data/blob/main/LICENSE)

ESCI labels cover judged candidates for each query, not every Amazon product.
The primary benchmark therefore reranks fully judged candidate sets. A separate
closed-corpus track is used for retrieval metrics so incomplete judgments are
not presented as full-catalog recall.

## Next step

Build, benchmark and deploy the full-catalog baseline so the Owner can record
real search failures on the website. That experience does not count as a
relevance evaluation. In parallel, the Owner still needs to complete the Stage
1 data-boundary and Stage 2 metric exercises before the 500-Query dev profile is
unlocked. Frozen test stays closed until a milestone release gate.
