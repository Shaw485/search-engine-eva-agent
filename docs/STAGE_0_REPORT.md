# Stage 0 acceptance report

**Date:** 2026-08-25
**Decision:** Local technical gate accepted; optional OpenSearch integration pending
**Scope:** Engineering skeleton and search-backend feasibility only

## Outcome

The mandatory Stage 0 path is runnable without Docker, a model download, the
full ESCI dataset, or an LLM API. It indexes ten products, runs BM25 and exact
cosine search through a shared backend contract, replaces the index a second
time, and verifies that both ranked outputs remain deterministic.

OpenSearch remains an explicit optional adapter. Its configuration and HTTP
request contracts are covered offline, but the container has not been run on
this host because Docker is absent. No result in this report treats that live
integration as passed.

## Environment observed

| Component | Observed value | Stage 0 use |
|---|---|---|
| Host | macOS 26.5.2, Apple Silicon arm64 | Reference development host |
| Python | 3.13.15 | Local acceptance environment |
| Python CI targets | 3.11 and 3.13 | Supported range |
| Node / npm | 22.23.2 / 10.9.8 | Recorded; unused until Web stage |
| GNU Make | 3.81 | Project command entry point |
| Docker | Not installed | OpenSearch live gate deferred |
| Git LFS | Not installed | Full ESCI download deferred to Stage 1 |

Python and Node reference versions are recorded in `.python-version` and
`.nvmrc`. Python application and development packages are exactly pinned in
`requirements-dev.lock`.

## Delivered artifacts

| Area | Artifact |
|---|---|
| Backend boundary | `SearchBackend` protocol and normalized `SearchHit` |
| Required adapter | In-memory BM25 plus true cosine search |
| Optional adapter | OpenSearch 3.8.0 BM25 and Lucene HNSW k-NN |
| Embedding boundary | Separate provider with deterministic 64-d smoke vector |
| Fixture | Ten unique products in `data/samples/products.json` |
| Interfaces | CLI smoke command and FastAPI `/health` + `/smoke` |
| Reproducibility | Locked dependencies, Make targets, fixed runtime references |
| Quality gates | Ruff, repository policy, pytest, deterministic smoke, CI matrix |
| Decision record | `docs/adr/001-search-backend.md` |

## Verification evidence

| Command / check | Result |
|---|---|
| Clean `.venv` then `make setup` | Passed using the exact lock file |
| `make check` | Passed: format/lint, policy, tests, local smoke |
| `make data-sample` | Passed: 10 valid, unique products |
| Local smoke reindex and replay | Passed: stable BM25 and vector rankings |
| API process + `/health` request | Passed on `127.0.0.1:8000` |
| Repository diff hygiene | Passed; ESCI submodule remains clean |
| OpenSearch offline contract tests | Passed |
| OpenSearch Docker smoke | **Not run — Docker is not installed** |
| GitHub Actions | Workflow configured; live result is tracked in repository checks |

The smoke vector is a deterministic hashing mechanism, not a semantic model.
Its success proves plumbing and repeatability only; it does not establish
relevance quality.

## Acceptance gate

| Roadmap criterion | Decision | Evidence |
|---|---|---|
| New environment can start within 30 minutes | Pass | Clean locked install and README quick start |
| `make test` passes | Pass | Local and offline backend-contract tests |
| Same query returns the same result | Pass | Smoke repeats after a full index replacement |
| No large data, tokens, or model keys in Git | Pass | `.gitignore` plus repository policy gate |
| Search backend is feasible | Pass (local) | ADR-001 accepts the local reference adapter |

Stage 0 is therefore complete for its mandatory local path. OpenSearch is not a
blocker under the roadmap's fallback rule and remains a separately visible
integration check.

## OpenSearch safety boundary

The optional smoke adapter can reset an index, so it applies independent guards:

1. Only loopback URLs are accepted.
2. Index names must use the `search-quality-` prefix and safe characters.
3. Reset requires explicit `OPENSEARCH_ALLOW_INDEX_RESET=true` opt-in.
4. Distribution, cluster name, and exact version are checked before deletion.
5. Compose also enables `action.destructive_requires_name`.

The security plugin is disabled only in the loopback-bound local Compose
profile. The fixed-index rebuild is serialized within the API process and must
not be run concurrently from separate processes. A later production design
will build versioned indexes and atomically swap an alias.

## Deferred work

- Install Docker Desktop with at least 4 GB allocated and run the documented
  OpenSearch integration gate.
- Download Git LFS objects only when Stage 1 begins.
- Replace hashing vectors with a versioned semantic embedding in Stage 3.
- Build Harness, Trace, and Replay in Stage 6 after evaluation and Agent tool
  contracts exist.

## Learning checkpoint

Engineering acceptance does not complete the owner's Stage 0 learning check.
That checkpoint remains pending until the owner can explain why a precise model
query such as `iPhone 15 Pro case` should not rely on vector search alone, and
what BM25 contributes.
