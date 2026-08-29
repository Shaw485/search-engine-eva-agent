# Human Diagnostic Oracle

## Purpose

The 59-Query runner found 40 deterministic behavior candidates in 20 source
clusters. Those are observations, not 40 relevance defects: a zero result can
mean missing catalog coverage, a malformed synthetic Query, or a retrieval
failure. The Human Diagnostic Oracle records the Owner's direct diagnostic
judgments without inheriting Amazon ESCI labels to synthetic Queries.

The Oracle is a mechanism-smoke evidence source. It never creates product-level
relevance labels, computes nDCG/MRR, identifies a pipeline root cause, writes a
strategy, activates search configuration or unlocks the dev/frozen-test sets.

## Latest clean-revision construction

From clean revision `b7efb90ebf834533eec1c8fbdafc2ad48182df55`, the validated
pair `bad-case-3b5d1ff13a7c` + `query-set-33b9564cb660` reconstructs
`oracle-batch-4b2533c217d3`: 20 clusters, 40 behavior candidates and 30
synthetic intent tasks. The same evidence routes to
`diagnostic-experiment-plan-bae9602ea206`, targeting the 10 identity
zero-result cases with `zero-result-drop-one-token-backoff-v1`.

These IDs prove deterministic construction only. No Owner judgment was written,
the experiment strategy was not executed, and both
`quality_conclusion_allowed` and `activation_eligible` remain false.

## Fixed census

One batch is rebuilt from a content-addressed Bad Case diagnostic and its exact
Query-set artifact. It contains the complete current candidate population:

- 20 source clusters and 40 cases;
- 10 source-zero clusters, each containing identity, adjacent-transposition and
  token-order variants;
- 10 source-nonzero/variant-zero clusters, each containing the spelling
  variant that lost all results;
- 30 synthetic cases requiring an independent intent judgment;
- 40 cases requiring a behavior judgment.

The durable batch stores IDs, categories, counts and hashes. It stores no raw
Query, product ID, title or result list.

## Two ordered review phases

### 1. Intent review

The Owner sees only a source Query and its synthetic variant. Search results are
withheld. Each synthetic case is marked:

- `equivalent`: same product intent;
- `not_equivalent`: meaning changed or became uninterpretable;
- `uncertain`: intent cannot be determined safely.

This phase prevents the visible result difference from steering the semantic
judgment. The UI must finish all 30 intent decisions before requesting any
behavior view.

### 2. Behavior review

The server re-runs only the selected source cluster against the exact catalog
identity. It validates the full Top 10 against immutable observation hashes,
then returns at most Top 3 display evidence on each side. The browser cannot
upload or substitute results.

Each of the 40 cases is marked `confirmed_issue`, `acceptable`, or `uncertain`.
The allowed reason is constrained by construction and active intent. For
example, a synthetic case judged `not_equivalent` cannot be called a confirmed
pair issue, and an uncertain intent can produce only an uncertain behavior
judgment.

## State and integrity

- The server derives a pseudonymous `OracleActor` by HMAC from the authenticated
  Owner principal and compares it in constant time with a server-only Owner
  allowlist digest. Raw principals, HMAC keys and allowlist digests are never
  accepted from the browser or written to logs; another valid Basic Auth user
  cannot read or mutate Oracle state.
- Every annotation is immutable and content-addressed. A UUIDv4 client action
  ID provides idempotency; compare-and-swap rejects stale edits.
- A later intent annotation supersedes the old one and invalidates any behavior
  judgment tied to it.
- One batch can be sealed only when all 30 intent and 40 behavior decisions are
  active and mutually valid. A seal is immutable and still states
  `formal_evaluation_allowed=false`, `quality_conclusion_allowed=false`,
  `root_cause_claimed=false` and `strategy_write_count=0`.
- Capacity, ownership, private mode, regular-file, no-symlink, duplicate-key and
  content-ID checks fail closed.

Private artifacts live below the configured runtime root:

```text
human-oracle/
  batches/
  intent-annotations/
  behavior-annotations/
  seals/
```

Raw intent/behavior views are transient owner-only responses and must use
`Cache-Control: no-store`. Do not copy them into URLs, local storage, analytics,
access logs or exported diagnostics.

The current Tool 05 is intentionally sequential: it submits the next missing
decision and does not yet provide back-navigation for revising an earlier
annotation. The core and API already support CAS/supersession, but a correction
currently requires the owner-only API. Add review/edit navigation before using
the UI for a larger or multi-session labeling campaign.

## What this unlocks

A sealed Oracle can tell the Agent which observed behavior patterns the Owner
considers worth testing and can expose false-positive diagnostic rules. It does
not tell the Agent which products are relevant. Quality claims and activation
still require a separately judged, fixed product-relevance pool and the Search
Harness. This separation prevents the Agent from generating a hypothesis,
judging its own output and declaring itself improved.

## Independent debugging

Enable only the Oracle module:

```bash
SEARCH_LOG_LEVEL=OFF SEARCH_LOG_LEVEL_HUMAN_ORACLE=DEBUG \
  .venv/bin/python -m pytest -q tests/test_human_oracle.py
```

Filter by batch, unit, case or annotation ID. Events contain only safe IDs,
counts and stable error types; they exclude Query/product content, judgments,
reasons, principal/HMAC values, paths and exception text. See
`docs/LOGGING.md` for retention and export guidance.
