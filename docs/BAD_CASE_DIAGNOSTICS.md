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

## Verified clean-revision evidence

On 2026-08-29, implementation revision
`c923174e651f6b1f4f06fc06e7571d0fea0f8463` ran the fixed batch twice against
the 1,814,924-product index `catalog-baseline-v1-f42e2120a938`:

- Query set: `query-set-a48442f35d30`;
- deterministic evidence: `bad-case-b2cbe225fea3`;
- executions: `bad-case-execution-74143e4c33224adabf2456a514f8907e` and
  `bad-case-execution-070f3524e27c43108c28821415bb0c8c`;
- both executions completed 59/59 search calls with zero operational failure,
  protected-profile dispatch or strategy write;
- 40 unique behavioral candidates were flagged: `zero_result=40`,
  `spelling_sensitive=10`, `order_sensitive=0`, and
  `ranking_instability_needs_judgment=0`.

The identical diagnostic ID and distinct execution IDs confirm deterministic
evidence with separate per-run receipts. The category counts overlap: ten
zero-result cases are also spelling-sensitive. No relevance label or quality
metric was used, so this is not evidence of 40 confirmed search defects. Local
durations are intentionally not recorded as a performance benchmark because
they depend on filesystem and SQLite cache state.

## Completion and evidence

Before the first search, all 59 Query strings must satisfy the catalog contract
of at most 200 characters and 16 searchable tokens. `search_many` then uses one
read-only immutable SQLite connection, exactly 59 Query search calls, a
120-second child batch budget, a 5-second per-Query budget, and a SQLite progress
handler that can interrupt active SQL. One error aborts the whole batch. The
parent additionally enforces a 125-second monotonic process-group deadline; it
sends `SIGTERM`, waits one second, escalates to `SIGKILL`, waits one more second
and reaps the child before returning a terminal outcome.

A completed artifact is published only when all 59 expected case IDs appear
exactly once, all 39 variants link to their identity Query, the catalog index
identity and file snapshot stay unchanged, protected profile dispatches remain
zero, and strategy-authority paths are unchanged. An operational failure writes
only a small safe attempt receipt with the failure stage and completed Query
count; capacity or free-space rejection writes no receipt.

Two IDs have different purposes:

- `diagnostic_id` is a deterministic content ID for the evidence;
- `execution_id` is random for one click/run and correlates its receipt and logs.

A successful supervised run has a third content-addressed ID,
`bad-case-supervisor-execution-*`. Its private immutable receipt binds the
parent execution to the verified child execution receipt, diagnostic ID,
deadline policy, TERM/KILL grace values, trace and one allowlisted completion
observation. The API loads and revalidates this receipt before claiming that the
hard worker boundary was enforced. It does not change deterministic diagnostic
identity.

For that reason, the reusable `bad-case-diagnostic-v1` artifact retains its
legacy `no_hard_worker_deadline_enforcement` limitation: the artifact alone
cannot prove which parent executed it. The API v2 claim is execution-scoped and
is valid only when the separate supervisor receipt has been loaded and linked.

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

## Concurrency, worker isolation and storage

The API rejects concurrent work in-process. The parent supervisor and child
artifact runner use separate non-blocking cross-process `flock` locks. The
child runs as a fixed isolated Python module in a new POSIX process group, reads
a strict allowlisted environment and returns one bounded framed envelope over
an anonymous pipe; stdout is discarded and credentials are not forwarded.
Immutable publication prevents a different payload from overwriting an
existing content ID. The private store has a 256 MiB watermark, 2 GiB
free-space preflight, per-file size limits and private supervisor-receipt
permissions.

The hard deadline is an execution-isolation guarantee, not a hostile-code
sandbox or a search-quality claim. POSIX uninterruptible I/O can still delay
kernel-level termination; an unreaped process is therefore reported as a
distinct critical failure and never as success. A proxy timeout alone is never
treated as worker termination.

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
runs/bad-case-diagnostics/supervisor-executions/
runs/bad-case-diagnostics/attempts/
```

Filter `bad_case` logs by `execution_id`, `diagnostic_id` or `failure_stage`;
filter `bad_case_supervisor` by execution/receipt ID and
`bad_case_worker` by execution ID. Raw Query text remains in the separate
private Query-set artifact and may also appear in the transient owner-only API
response. Limited product display content exists only in that response.
Neither is persisted in the diagnostic or supervisor artifacts or copied into
logs.
