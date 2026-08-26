# ADR-001: Stage 0 search backend

**Status:** Accepted for Stage 0
**Date:** 2026-08-25
**Deciders:** Project owner and implementation collaborator
**Provenance:** The backend options and selected technical design were proposed
and implemented by Codex; installation and deployment continuation were
authorized by the owner. See D-009 in
[`docs/CONTRIBUTION_LOG.md`](../CONTRIBUTION_LOG.md) for the stricter attribution
record. The Git author is not evidence of who designed or wrote the change.

## Context

Stage 0 must prove a deterministic end-to-end path for indexing ten products,
running lexical and vector search, and returning normalized results. It must not
depend on Amazon ESCI, an LLM, or a model download.

The development host is Apple Silicon with Python 3.13.15. Docker, Git LFS, and
Python 3.11 are not currently installed. Making OpenSearch mandatory would block
the technical gate before any search contract could be tested.

Smoke success only proves that the pipeline is connected and repeatable. It does
not prove search relevance or predict later nDCG improvements.

## Decision

Use a shared `SearchBackend` contract with two explicit adapters:

1. `LocalSearchBackend` is the mandatory Stage 0 reference backend. It provides
   in-memory BM25 and exact cosine vector search and runs without Docker.
2. `OpenSearchBackend` is an optional integration backend using the same product
   documents, vectors, and normalized result type.

The embedding provider is a separate port. Stage 0 uses a clearly labelled
deterministic hashing vector only to validate dimensions and k-NN plumbing. It
is not treated as a semantic model; Stage 3 replaces it with a versioned model.

Backend choice is explicit (`local` or `opensearch`). Failure never silently
falls back to another backend because every experiment must record what ran.

## Options considered

### Option A: OpenSearch-only

| Dimension | Assessment |
|---|---|
| Complexity | Medium–high |
| Local cost | Docker Desktop and at least 4 GB allocated memory |
| Production similarity | High |
| Current host readiness | Blocked: Docker absent |

**Pros:** BM25 and k-NN run in the intended search engine.
**Cons:** Blocks CI and local progress on infrastructure installation.

### Option B: Local reference plus optional OpenSearch

| Dimension | Assessment |
|---|---|
| Complexity | Medium |
| Local cost | Low for the mandatory path |
| Testability | High and deterministic |
| Replaceability | High through the shared backend contract |

**Pros:** Keeps the core runnable offline and makes backend differences visible.
**Cons:** Local and OpenSearch scores are not numerically comparable.

### Option C: FAISS as the mandatory local backend

| Dimension | Assessment |
|---|---|
| Complexity | Medium |
| Current need | Low for ten products |
| Binary portability | Additional Python/Apple Silicon dependency risk |
| Scale | Better later, unnecessary now |

**Pros:** Efficient vector search at larger scale.
**Cons:** Adds native dependencies before the data scale requires them and does
not provide BM25.

## OpenSearch integration profile

- Image: `opensearchproject/opensearch:3.8.0`, pinned in `compose.yaml` to its
  multi-architecture image digest.
- Vector engine: Lucene HNSW with cosine similarity.
- Topology: one node, one shard, zero replicas.
- Network: port 9200 bound only to `127.0.0.1`.
- Security plugin: disabled only for this local smoke profile. This configuration
  must never be deployed to a public or shared host.
- Readiness: the Python adapter performs bounded polling of cluster health;
  Compose does not assume that `curl` exists in the image.
- Apple Silicon: use the native ARM64 image and do not force `linux/amd64`.
- Destructive reset: restricted to loopback, a `search-quality-` index prefix,
  explicit opt-in, and an exact distribution/cluster/version identity check.
- Cluster setting: `action.destructive_requires_name` is enabled as a second
  guard against wildcard deletion.
- Concurrency: the Stage 0 API serializes fixed-index smoke runs in-process.
  Separate CLI processes must not run the OpenSearch smoke concurrently; a
  future version uses versioned indexes and an atomic alias swap.

The current OpenSearch release and Docker setup are documented by the official
[OpenSearch download page](https://opensearch.org/downloads/) and
[Docker installation guide](https://docs.opensearch.org/latest/install-and-configure/install-opensearch/docker/).
The mapping and query follow the official
[k-NN vector field](https://docs.opensearch.org/latest/mappings/supported-field-types/knn-vector/)
and [k-NN query](https://docs.opensearch.org/latest/query-dsl/specialized/k-nn/)
documentation.

## Acceptance gates

The local reference backend is accepted when:

1. A clean Python environment installs successfully.
2. Unit and contract tests pass without Docker or network access.
3. Ten products can be replaced, searched lexically, and searched by vector.
4. Repeated searches produce the same ranked product IDs.
5. Invalid input fails explicitly.

OpenSearch becomes a verified backend only after Docker is installed and:

1. `docker compose up -d opensearch` reaches yellow or green health.
2. `make smoke-opensearch` passes twice without changing the contract.
3. The locally pulled image digest matches the pinned digest.
4. `docker compose down` cleans up the service normally.

Until those checks run, OpenSearch is **implemented but unverified on the
current host**.

## Consequences

- Core search and future evaluation code stays independent of OpenSearch and
  LLM libraries.
- CI can validate the mandatory backend on Python 3.11 and 3.13.
- Model versions and vectors can be traced separately from search storage.
- Local and OpenSearch share an interface, not identical analyzers, field
  weights, scores, or rankings. Later manifests record the backend configuration,
  and evaluation compares each ranking with relevance labels.
- Docker installation can happen separately without blocking Stage 0.

## Action items

- [x] Define the shared backend and embedding ports.
- [x] Implement the local reference backend.
- [x] Add the OpenSearch adapter, mapping, and Compose profile.
- [ ] Run the OpenSearch integration gate after Docker Desktop is available.
- [ ] Replace the Stage 0 hashing vectors with a versioned semantic model in
  Stage 3.
