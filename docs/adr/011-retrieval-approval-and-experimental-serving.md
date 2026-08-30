# ADR-011: One-click approval with a separate experimental serving lane

- Status: Adopted for implementation; no strategy is approved by this ADR
- Date: 2026-08-30
- Deciders: Owner (product workflow and execution authorization), Codex
  (control-plane, serving and rollback design)

## Context

The stage-aware Retrieval Agent can diagnose the fixed smoke profile, run
three bounded multi-recall/RRF candidates and return a gate-passing
recommendation. That recommendation is not currently a durable Proposal. The
older exact-boost approval controller accepts a different Proposal schema and
writes a strategy catalog, while the full-catalog `/catalog/search` endpoint
continues to execute one hard-coded SQLite FTS5 baseline.

Consequently, three statements that look similar in the UI currently mean
different things:

1. a candidate passed the 20-Query smoke gates;
2. a human approved a versioned strategy artifact;
3. a search request actually executed that strategy.

The Owner requires one human product decision—update or reject—followed by
automatic validation and real search execution. The baseline search must remain
available for comparison and recovery. The fixed smoke set is not evidence of
full-catalog relevance, and the locked 500-Query dev and frozen test sets remain
unavailable.

## Decision

Introduce a dedicated Retrieval release control plane and a separate
full-catalog experimental serving lane.

The control plane persists a content-addressed Proposal that binds the selected
pipeline config, Run/Comparison/Diagnosis/Trace evidence, code revision, parent
active revision and config hash. The browser exposes the candidate publicly but
requires an authenticated, same-origin Owner session for either `approve` or
`reject`. Approval means `approved_for_validation`; it does not itself mean
`active`.

An approved Proposal is automatically checked against the full-catalog serving
index and the supported production pipeline. Only a successful validation may
atomically advance the Retrieval serving pointer. Search resolves that pointer
for each active-lane request and returns the actual strategy revision and index
identity it executed. A failed validation leaves the previous pointer and all
baseline behavior unchanged.

The first release uses two public data-plane endpoints:

- `/catalog/search` remains the immutable baseline lane;
- `/catalog/search/active` executes the approved Retrieval strategy when a
  compatible active revision exists.

This is an explicit, zero-default-traffic canary: only the right-hand
“optimized” search box calls the active lane. It does not silently replace the
baseline for other callers.

The lifecycle is:

```text
rejected_by_gate
pending_owner_review
  |-- rejected
  `-- approved_for_validation
        -> validating
        |-- validation_failed
        `-- staged -> canary -> active -> rolled_back
```

Lifecycle artifacts and the active pointer are separate. Strategy-platform
copy must say “approved, not live” until the data plane reports the matching
active revision.

## Options considered

### Option A: Add a browser button to the old decision endpoint

| Dimension | Assessment |
|---|---|
| Complexity | Low UI effort, incompatible backend objects |
| Safety | Poor; can approve the wrong strategy family |
| Verifiability | Catalog write is easily mistaken for serving activation |
| Recovery | No full-catalog execution rollback |

**Decision:** rejected. The Retrieval recommendation has no legacy
`proposal_id`, and the old active artifact is not consumed by catalog search.

### Option B: Replace the baseline immediately after smoke approval

| Dimension | Assessment |
|---|---|
| Complexity | Medium |
| Safety | Poor; 20 judged Queries do not represent full-catalog behavior |
| User value | Immediate but hard to compare or recover |
| Recovery | Requires restoring both code/config and index |

**Decision:** rejected. It turns an evaluation slice into an unsupported
production-quality claim and removes the stable control.

### Option C: Versioned control plane plus explicit active lane

| Dimension | Assessment |
|---|---|
| Complexity | High; Proposal, serving adapter, validation and UI state |
| Safety | Strong; baseline remains available and failed gates fail closed |
| Verifiability | Every response reports the executed revision/index |
| Recovery | Atomic pointer rollback to the previous immutable revision |

**Decision:** adopted.

## Consequences

### Positive

- One Owner click can safely trigger the rest of the bounded release workflow.
- Strategy approval, release validation and request-time execution become
  independently observable facts.
- The search demo can compare baseline and active behavior on the same Query.
- The previous immutable revision remains available for atomic rollback.
- Public Agent analysis still cannot spend model quota or mutate active state.

### Negative

- A second full-field index consumes additional disk and build time.
- The active lane performs three retrieval queries plus fusion and coarse rank,
  so its latency is higher than the single baseline query.
- Smoke evidence still cannot establish full-catalog relevance because most
  returned products are unjudged for a given Query.
- The first canary has explicit user traffic only; it is not an online A/B test.

### Risks and controls

- **False activation:** the API and strategy platform show `active` only when
  the serving pointer, Proposal revision and compatible index all agree.
- **Stale approval:** Proposal decisions use expected parent/revision CAS and
  idempotent client action IDs.
- **CSRF or replay:** Owner write routes require Basic authentication at Nginx,
  exact same-origin checks and a short-lived, single-use approval token.
- **Index incompatibility:** unsupported schema, missing fields or config hash
  mismatch fails validation without moving the active pointer.
- **Builder memory boundary:** the first full build exposed that downstream
  batched SQLite writes did not bound an upstream Parquet decoder. The builder
  now uses single-threaded PyArrow record batches, per-batch records/FTS commits,
  a 32 MiB SQLite cache and no final full-index rebuild/optimize; formal release
  still requires measured peak-RSS evidence on the production host.
- **Cold-query I/O boundary:** every FTS channel materializes ranked row IDs and
  scores before joining the external content table. This prevents broad cold
  queries from hydrating long descriptions for the full match set before the
  Top-50 cutoff; full-pipeline cold and warm latency remain release evidence.
- **Secret/privacy leakage:** logs contain IDs, counts, stages and durations,
  never credentials, raw Query text, product fields or response bodies.
- **Rollback drift:** rollback uses an expected-active revision and changes one
  atomic pointer; the next search response proves the restored revision.

## Action items

1. [x] Persist and validate formal Retrieval Proposals.
2. [x] Add authenticated Owner decision/session endpoints and audit artifacts.
3. [x] Build the streaming full-field catalog index and active serving pipeline.
4. [x] Add atomic activation/rollback and executed-revision response metadata.
5. [x] Add approval, lifecycle, strategy-platform and optimized-search UI.
6. [ ] Verify focused tests, full regression, local browser behavior and public
       deployment without opening dev or frozen-test data.
