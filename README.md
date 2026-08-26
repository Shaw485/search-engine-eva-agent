# Search Engine EVA Agent

An evidence-driven evaluation and diagnosis agent for e-commerce search ranking.
The project uses Amazon Shopping Queries ESCI to compare BM25, vector retrieval,
hybrid ranking, and Cross-Encoder reranking. Every conclusion must be traceable
to a dataset version, run configuration, metric, and ranked product list.

## Project status

**Stage 2 Search Evaluation Harness: in progress.** The Stage 1 technical data
gate is complete, while its owner learning check remains pending. The repository
now includes hand-verified nDCG, MRR and Success metrics, the versioned
`esci-primary-v1` relevance policy, and a deterministic title-BM25 candidate
reranking smoke run. Routine Stage 2 commands accept only smoke/dev data; the
8,956-Query frozen test remains unavailable to tuning runs.

The optional OpenSearch 3.8.0 adapter, mapping, and Apple Silicon-compatible
Compose profile are implemented. Live container verification remains pending
because Docker is not installed on the current development host. This is an
explicit pending integration check, not an implicit fallback or a claimed pass.

- Full execution guide: [ROADMAP.md](ROADMAP.md)
- Stage 0 evidence: [docs/STAGE_0_REPORT.md](docs/STAGE_0_REPORT.md)
- Stage 1 evidence: [docs/STAGE_1_REPORT.md](docs/STAGE_1_REPORT.md)
- Stage 2 smoke evidence: [docs/STAGE_2_SMOKE_REPORT.md](docs/STAGE_2_SMOKE_REPORT.md)
- Data dictionary: [docs/DATA_DICTIONARY.md](docs/DATA_DICTIONARY.md)
- Backend decision: [docs/adr/001-search-backend.md](docs/adr/001-search-backend.md)
- Required learning: [docs/LEARNING_CHECKPOINTS.md](docs/LEARNING_CHECKPOINTS.md)
- Decision and contribution provenance: [docs/CONTRIBUTION_LOG.md](docs/CONTRIBUTION_LOG.md)
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
QUERY="iphone 15 pro case" make smoke
```

The Stage 1 data path is separate so CI never downloads the 1.16 GB source:

```bash
make data-download
make data-esci-validate
make data-esci-build
```

Start the Stage 0 API with:

```bash
make api
curl http://127.0.0.1:8000/health
curl 'http://127.0.0.1:8000/smoke?query=wireless%20mouse&top_k=3'
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

Complete the Stage 1 owner checkpoint on Query-level leakage, then promote the
title-BM25 smoke run to the full 500-Query dev profile and add deterministic
random/keyword-overlap comparators. Frozen test stays closed until a milestone
release gate.
