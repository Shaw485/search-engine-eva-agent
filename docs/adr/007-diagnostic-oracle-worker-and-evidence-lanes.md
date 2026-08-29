# ADR 007: Diagnostic Oracle, killable worker, and two evidence lanes

- Status: Accepted; local implementation complete, deployment and Owner review pending
- Date: 2026-08-29

## Context

The fixed full-catalog diagnostic run produced 40 behavioral candidates from 20
source Query clusters. Those candidates are useful for choosing what to
investigate, but they are not 40 independent relevance judgments. The ESCI
labels attached to an identity Query cannot be inherited by a spelling or word
order variant, and the 12 samples returned to the workbench are only a display
preview.

The current API also runs the diagnostic in its request process. SQLite can
interrupt an active statement, but that does not terminate a stuck Python,
filesystem, Git, native-library, or future model call. A proxy timeout only
disconnects the client; it does not stop the work.

## Decision

### 1. Human Diagnostic Oracle

The project will create a deterministic batch containing the complete 40-case,
20-cluster diagnostic census. The durable batch contains hashes and references,
not raw Query or product content.

Review has two ordered phases:

1. **Intent review** shows the source and synthetic Query but hides search
   results. The Owner records `equivalent`, `not_equivalent`, or `uncertain`.
2. **Behavior review** then reveals bounded result evidence. The Owner records
   `confirmed_issue`, `acceptable`, or `uncertain` under constraints derived
   from the locked intent judgment.

Every decision is append-only, content-addressed, bound to a principal HMAC and
uses compare-and-swap plus a client action ID. A later intent revision
invalidates the older behavior decision. Sealing requires every selected unit
to have one active valid decision; a sealed batch is immutable.

The Oracle is diagnostic evidence only. It does not create ESCI product labels,
claim a root cause, compute search metrics, activate a strategy, or unlock the
development/test profiles.

### 2. Killable diagnostic worker

API and CLI callers will supervise a fixed Python module in a new POSIX process
group. The parent owns the execution ID and a supervisor lock; the child keeps
the existing run lock. The parent uses a monotonic wall deadline and terminates
the complete process group with `SIGTERM`, followed by `SIGKILL` after a short
grace period, then reaps the child.

IPC is a bounded anonymous pipe with a strict allowlisted envelope. Worker
stdout is discarded, its environment is allowlisted, and model/cloud secrets
are never forwarded. A timeout with unknown progress records `null/unknown`,
not a fabricated zero. One execution ID can have exactly one durable terminal
receipt. If a complete execution receipt won the deadline race, the supervisor
may recover that completed state; orphan evidence alone is not completion.

The worker is availability isolation, not a hostile-code sandbox. POSIX
uninterruptible I/O remains a platform limitation, so an unreaped process is a
distinct critical terminal state rather than a false success.

### 3. Diagnostic-guided experiments use two evidence lanes

The Agent maps trusted diagnostic facts to a strict, allowlisted `StrategySpec`.
Its first candidate is `zero-result-drop-one-token-backoff-v1`:

- run the existing all-token AND query first;
- only when it returns zero, issue deterministic subqueries that each drop one
  non-protected token;
- never drop numeric, model, or product-ID-like tokens;
- combine subquery ranks with RRF rather than comparing raw BM25 scores from
  different queries;
- leave every originally non-zero Query unchanged.

The experiment has two independent lanes:

- **Behavior lane:** full catalog and the fixed diagnostic Query set. It may
  report result recovery, result changes, failures, query fan-out, and latency.
- **Quality lane:** a fixed pool with legitimate labels and the deterministic
  Search Harness. The existing 20-Query smoke pool is development-only evidence;
  an independent held-out Oracle is required for a quality conclusion.

Until independent product relevance labels exist,
`quality_conclusion_allowed=false` and `activation_eligible=false`. Restoring a
zero-result Query is not treated as nDCG, MRR, or relevance improvement.

## Security and operations

- Oracle mutation routes are exact, owner-authenticated, same-origin POSTs with
  JSON-only bodies, `Cache-Control: no-store`, no access log, and an Nginx-set
  trusted principal header. The application never receives Basic Auth.
- Raw Query/result content is transient response data and is never written to
  Oracle artifacts or structured logs.
- `human_oracle`, `bad_case_supervisor`, `bad_case_worker`, and
  `diagnostic_experiments` are independently configurable log modules.
- Production uses journald retention. The worker and its descendants remain in
  the service control group and are killed when the unit stops.

## Consequences

The Agent can safely say “this pattern warrants a bounded experiment” before it
can say “search quality improved.” Completing the Oracle is additional Owner
work, but it removes circular self-judging and turns representative screenshots
into a complete, auditable decision set. Hard worker termination also makes the
Runtime budget enforceable rather than merely cooperative.

This decision does not approve a strategy, change `/catalog/search`, deploy the
website, expose a model key, or unlock the 500-Query development/frozen test
sets.
