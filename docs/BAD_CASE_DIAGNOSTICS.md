# Source-bounded Bad Case diagnostics

## What this tool does

`make bad-cases-smoke` executes exactly the 59 development Query cases produced
by the committed smoke-only Query constructor against the immutable
full-catalog SQLite FTS5 baseline:

- 20 identity Queries;
- 20 adjacent-letter transpositions;
- 19 token-order reversals.

It is a behavioral/metamorphic diagnostic tool. It does not read relevance
labels, calculate nDCG/MRR/accuracy, open the locked 500-Query dev profile or
frozen test, modify a strategy, approve a proposal, activate configuration, or
deploy code.

## Deterministic classifications

Every comparison uses ordered Top 10 product keys. No BM25 scores are compared
across Queries and no arbitrary overlap threshold is used.

| Category | Exact predicate | What it permits us to say |
|---|---|---|
| `zero_result` | the current case returns zero hits at K=10 | this input produced no result |
| `spelling_sensitive` | an adjacent-transposition variant has a different ordered Top 10 from its identity Query | the output changed after the spelling perturbation |
| `order_sensitive` | a token-order reversal has a different ordered Top 10 from its identity Query | the output changed after token reordering |
| `ranking_instability_needs_judgment` | both identity and variant return hits and their ordered Top 10 differs | a human or independently labelled Harness must judge the change |

Flags overlap. `diagnostic_candidate_count` counts unique cases and therefore
must not be calculated by summing category counts. `overlap_at_k` is only an
observable set intersection; it is not a relevance score. A changed result is
never called an improvement, regression, relevance failure, or root cause.

## Completion and evidence

Before the first search, all 59 Query strings must satisfy the catalog contract
of at most 200 characters and 16 searchable tokens. `search_many` then uses one
read-only immutable SQLite connection, exactly 59 Query search calls, a
120-second batch budget, a 5-second per-Query budget, and a SQLite progress handler that can
interrupt active SQL. One error aborts the whole batch.

A completed artifact is published only when all 59 expected case IDs appear
exactly once, all 39 variants link to their identity Query, the catalog index
identity and file snapshot stay unchanged, protected profile dispatches remain
zero, and strategy-authority paths are unchanged. An operational failure writes
only a small safe attempt receipt with the failure stage and completed Query
count; capacity or free-space rejection writes no receipt.

Two IDs have different purposes:

- `diagnostic_id` is a deterministic content ID for the evidence;
- `execution_id` is random for one click/run and correlates its receipt and logs.

The evidence artifact stores Query hashes, hashed ordered product keys and
aggregate diagnostics. It stores no raw Query, product ID, title, label or
score. The owner-only API may return at most 12 understandable samples and at
most three hits on each side. Those samples are cross-checked against the
artifact hashes, excluded from logs and served with `Cache-Control: no-store`
by the production Nginx location.

`validate_bad_case_diagnostic` performs offline schema, content-ID and trusted
Query-set linkage validation without searching. `rerun_bad_case_diagnostic`
performs all 59 searches again for reproducibility; it is deliberately not
called offline Replay.

## Concurrency, storage and remaining limitation

The API rejects concurrent work in-process and the artifact root uses a
non-blocking cross-process `flock`. Immutable hard-link publication prevents a
different payload from overwriting an existing content ID. The private store
has a 256 MiB watermark, 2 GiB free-space preflight, and per-file size limits.

SQLite SQL is interruptible, but this synchronous process still has no
force-terminable worker deadline. A stuck Python/native operation outside the
SQL progress handler cannot be killed independently. This limitation is fixed
in every response as `no_hard_worker_deadline_enforcement`; the next stage must
move execution to a killable worker before larger data or model-driven loops.

The single-stage full-catalog baseline also cannot diagnose recall/fusion/
coarse-rank stage drop. That requires a stage-aware executor and remains a
separate next step.

## Run and debug

```bash
make bad-cases-smoke

SEARCH_LOG_LEVEL=OFF SEARCH_LOG_LEVEL_BAD_CASE=DEBUG \
  .venv/bin/python -m search_quality.bad_cases.cli \
  2>bad-case-debug.jsonl
```

Artifacts are stored below ignored `runs/` by default:

```text
runs/query-sets/
runs/bad-case-diagnostics/evidence/
runs/bad-case-diagnostics/executions/
runs/bad-case-diagnostics/attempts/
```

Filter logs by `execution_id`, `diagnostic_id`, `failure_stage` or the API
request `trace_id`. Raw Query text remains in the separate private Query-set
artifact and may also appear in the transient owner-only API response. Limited
product display content exists only in that response. Neither is persisted in
the diagnostic artifact or copied into logs.
