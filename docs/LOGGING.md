# Logging and independent diagnostics

The project separates immutable experiment evidence from runtime diagnostics:

- Stage manifests and Run JSON are the reproducible source of truth.
- Logs explain one execution and may contain timing or failure information.
- A random `trace_id` correlates one execution. For the Agent it also names the
  corresponding Trace snapshot; it never enters deterministic Run identity or
  metric calculations.

## Modules and defaults

All events use the `search_quality.<module>` logger namespace and are emitted to
stderr. Normal CLI results remain on stdout.

| Module | Covers | Production default |
|---|---|---|
| `api` | HTTP request boundary and public failure correlation | `INFO` in systemd |
| `backend` | Local/OpenSearch smoke lifecycle | `WARNING` |
| `catalog` | Full-catalog index build, readiness and search timing | `WARNING` |
| `data` | Stage 1 validation and build lifecycle | `WARNING` |
| `evaluation` | Baseline/Run comparison, validation and artifact storage | `WARNING` |
| `ranking` | Per-Query Ranker diagnostics | `OFF` |
| `agent_runtime` | State, policy, budget and terminal lifecycle | `WARNING` |
| `agent_model` | Planner decision boundary; currently the deterministic fixture | `WARNING` |
| `agent_tools` | Allowlisted tool call lifecycle and stable failures | `WARNING` |
| `agent_trace` | Trace artifact publication | `WARNING` |
| `agent_replay` | Offline Trace validation and Replay lifecycle | `WARNING` |
| `agent_optimization` | Strategy proposal, decision and catalog lifecycle | `WARNING` |
| `agent_eval` | Fixed Agent task suite, independent grading and artifact publication | `WARNING` |
| `query_constructor` | Source-bounded Query construction and immutable storage | `WARNING` |
| `bad_case` | Fixed 59-Query batch, evidence publication, rerun and failed attempts | `WARNING` |
| `retrieval` | Label-blind recall channels, fusion, stage retention and retrieval Runs | `WARNING` |
| `stage_diagnosis` | Stage evidence validation and bottleneck diagnosis | `WARNING` |
| `retrieval_analysis` | Bounded experiment orchestration and artifact publication | `WARNING` |

The library default is `WARNING`. Verbose ranking events are opt-in because one
event per Query becomes noisy on larger profiles.

## Enable, disable, and filter

Environment variables accept `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`,
or `OFF`:

```bash
export SEARCH_LOG_FORMAT=json
export SEARCH_LOG_LEVEL=WARNING
export SEARCH_LOG_LEVEL_EVALUATION=INFO
export SEARCH_LOG_LEVEL_RANKING=DEBUG
export SEARCH_LOG_LEVEL_CATALOG=INFO
export SEARCH_LOG_LEVEL_AGENT_RUNTIME=DEBUG
export SEARCH_LOG_LEVEL_AGENT_EVAL=INFO
export SEARCH_LOG_LEVEL_QUERY_CONSTRUCTOR=INFO
export SEARCH_LOG_LEVEL_BAD_CASE=INFO
export SEARCH_LOG_LEVEL_RETRIEVAL=DEBUG
export SEARCH_LOG_LEVEL_RETRIEVAL_ANALYSIS=INFO
export SEARCH_LOG_LEVEL_STAGE_DIAGNOSIS=INFO
```

CLI entry points also accept repeatable module overrides. These examples keep
JSON diagnostics separate from the normal result:

```bash
.venv/bin/python -m search_quality.evaluation.cli \
  --profile smoke --ranker all \
  --log-module evaluation=INFO \
  --log-module ranking=DEBUG \
  2>evaluation-debug.jsonl

.venv/bin/python -m search_quality.evaluation.compare_cli \
  --profile smoke \
  --log-module evaluation=INFO \
  2>comparison-debug.jsonl

.venv/bin/python -m search_quality.data.cli --validate-only \
  --log-module data=INFO 2>data-validation.jsonl

.venv/bin/python -m search_quality.smoke \
  --backend local --log-module backend=INFO \
  2>backend-smoke.jsonl

.venv/bin/python -m search_quality.catalog.cli \
  --log-module catalog=DEBUG 2>catalog-build.jsonl

.venv/bin/python -m search_quality.agent.cli \
  --baseline-run-id "$BASELINE_RUN_ID" \
  --candidate-run-id "$CANDIDATE_RUN_ID" \
  --log-module agent_runtime=DEBUG \
  --log-module agent_tools=DEBUG \
  2>agent-debug.jsonl

.venv/bin/python -m search_quality.agent.replay_cli \
  "$TRACE_ID" \
  --log-module agent_replay=DEBUG \
  2>agent-replay.jsonl

.venv/bin/python -m search_quality.agent_eval.cli \
  --suite stage5-retrieval-v1 \
  --log-module agent_eval=INFO \
  2>agent-eval.jsonl

.venv/bin/python -m search_quality.query_constructor.cli \
  --log-module query_constructor=INFO \
  2>query-constructor.jsonl

.venv/bin/python -m search_quality.bad_cases.cli \
  --log-module bad_case=DEBUG \
  2>bad-case-debug.jsonl
```

To isolate one subsystem, set the global level to `OFF` and enable only that
module:

```bash
SEARCH_LOG_LEVEL=OFF SEARCH_LOG_LEVEL_EVALUATION=DEBUG \
  make eval-baseline 2>evaluation-only.jsonl
```

## Event fields

JSON events include:

- UTC timestamp, level, logger, module and stable event name;
- `trace_id` for one CLI execution or HTTP request;
- profile, Ranker, Query ID and candidate counts when relevant;
- Run ID and aggregate metrics after a completed evaluation;
- duration plus stable error code and error type at execution boundaries.

Run comparison emits `comparison_command_started`,
`run_comparison_started`, `run_comparison_completed`, and
`comparison_artifacts_stored` on success. These events contain validated Run
IDs, counts and the comparison ID, but never Query text, product titles, file
paths, ranked lists or raw exception messages. A failed command emits only
`comparison_command_failed` with a stable error code, exception type and
allowlisted failure stage. `run_comparison_completed` means the deterministic
result exists in memory; only `comparison_artifacts_stored` means its JSON,
Markdown and latest pointer were all persisted.

Per-Query debug events use numeric Query IDs and counts. They do not include raw
Query text, product titles, descriptions, vectors, request bodies or result
payloads.

The full-catalog builder emits command/build start, per-batch debug progress,
completion and failure events. Search emits index readiness plus request start
and completion. Safe fields include index ID, expected/processed/result counts,
token count, top K, duration and artifact size. These events do not contain the
source/index path, raw Query, product IDs, titles, brands, colors or response
body. `catalog_index_build_completed` means the artifact was verified, fsynced
and atomically installed at the operator-selected output path.

Comparison JSON and Markdown are evidence artifacts, not sanitized logs. They
intentionally contain raw Query text, product IDs, labels, scores and full
ranking differences. Local development uses Git-ignored `runs/`; production
strategy artifacts use the private `SEARCH_AGENT_ARTIFACT_ROOT` directory.
Review artifacts before sharing,
and never commit or upload a private/user-Query Run without a separate privacy
review. Content-addressed IDs detect content changes but are not signatures or
proof of which program produced a ranking.

Agent Trace JSON is also an evidence artifact rather than a sanitized log. It
contains typed actions and bounded observation snapshots so Replay can work
offline. Its event/terminal SHA-256 chain detects accidental corruption and
un-recomputed edits; it is not a keyed signature and cannot authenticate a
Trace against an attacker who can rewrite and rehash the file. Trace artifacts
remain under Git-ignored `runs/agent-traces/` and must be reviewed before
sharing.

Agent diagnostic events include safe task/Trace IDs, state, step, tool name,
duration, outcome and stable error codes. They never include action arguments,
raw Query text, product fields, observation/report payloads, filesystem paths,
provider prompts/responses or exception messages. `agent_runtime`,
`agent_model`, `agent_tools`, `agent_trace` and `agent_replay` can each be
enabled without enabling the others.

Strategy proposal and bounded-search events use the independently controlled
`agent_optimization` module. Production enables it at `INFO` while keeping
ranking diagnostics off. The lifecycle includes `bad_case_diagnosed`,
`strategy_candidates_selected`, `strategy_comparison_scored` and
`strategy_winner_selected` between proposal start/completion. Events include
safe diagnosis/proposal/Run/comparison/evaluation IDs, profile ID, counts, gate
status and stable error codes. Raw Query text, product titles, labels, parameter
payloads and ranked lists stay in evidence artifacts under local `runs/` or the
configured production runtime directory, not in diagnostics.

Every approved strategy also writes a bounded public-safe version snapshot to
`search-strategies/catalog.json`. `/agent/strategy/catalog` exposes at most the
latest 100 snapshots as `strategy_history` and derives the matching
`strategy_activity_logs`. These records contain adoption time, strategy/config
identifiers, the three aggregate metrics (`Success@5`, `MRR@10`, `nDCG@10`),
explanation and approval evidence IDs. They deliberately omit per-Query
comparisons, bad cases, ranked results, product content and credentials. The
`strategy_catalog_loaded` diagnostic records only current ID and record counts,
never snapshot bodies or configuration values.

The API also emits `agent_strategy_proposal_cache_hit`,
`agent_strategy_proposal_cache_miss`,
`agent_strategy_proposal_parent_changed` and
`agent_strategy_proposal_cache_cleared`. These contain only the profile and
event state; active config bodies, Query text and evidence payloads are not
logged. `strategy_candidate_skipped` records only the allowlisted candidate ID
and a stable reason when a candidate exactly matches the active baseline.
`legacy_active_strategy_migrated` is emitted once when the optimizer recognizes
the exact fixed-shape v1 active strategy and, under the strategy decision lock,
atomically adds its missing config hash before reuse. It contains only the
schema version and allowlisted strategy ID; config values and evidence bodies
are deliberately omitted. A missing hash on any non-v1 shape, or a mismatched
hash on any newer artifact, remains a hard failure.
Decision replay after an interrupted write emits
`strategy_decision_recovery_detected` with only the proposal ID and decision;
the durable intent, strategy body and evidence remain private artifacts. An
explicit unsupported proposal request is a debug-level
`agent_strategy_proposal_rejected` event and HTTP 400. Artifact, I/O and
contract failures—including unexpected `ValueError`—emit the privacy-safe
ERROR event `agent_strategy_proposal_failed` and return a generic HTTP 503.

The stage-aware retrieval slice uses five independently controlled modules.
`agent_runtime` records the bounded task lifecycle and terminal outcome;
`agent_tools` records the two allowlisted retrieval-tool boundaries. Both use
only Trace/evidence IDs, tool names, profile, counts, stable reason/error codes
and pipeline variants. They never log tool arguments or observation payloads.
The retrieval Runtime completion/failure bridge includes `agent_trace_id` while
the standard `trace_id` remains the API request correlation ID, so concurrent
requests can be joined to the exact private Trace without logging its contents.
`retrieval` emits run/channel/fusion lifecycle events such as
`retrieval_run_started`, `retrieval_query_completed`,
`rrf_fusion_completed` and `retrieval_run_completed` with only profile,
pipeline/channel IDs, counts, ranks and durations. `stage_diagnosis` emits
`stage_diagnosis_started`, `stage_diagnosis_completed`,
and `stage_diagnosis_failed`; `retrieval_analysis` emits
`retrieval_analysis_artifacts_stored` after the bounded experiment set is
durably published. These use only content-addressed evidence IDs,
category/count summaries and stable outcomes. None logs raw Query text,
product identifiers, titles, labels,
ranked lists, filesystem paths or exception messages.

The corresponding JSON under `retrieval-runs/`, `stage-diagnoses/` and
`retrieval-comparisons/` is private evidence, not a sanitized log. It may
contain Query-level rankings or labels needed for reproducibility. Keep the
artifact root private and review exports. A stored analysis is evidence only:
it does not create a proposal decision, strategy-catalog entry or active search
configuration.

Agent Eval emits suite/task start and completion, artifact publication and
command failure events through `agent_eval`. Safe fields are fixed suite/task,
evidence/execution IDs, categories, counts, pass status, reason codes and
durations. Action arguments, observations, Query text, product content, paths
and exception messages never enter diagnostics. Deterministic evidence under
`agent-evals/evidence/` excludes timestamps, dynamic Trace IDs and durations;
those belong to a separate execution receipt. Repeated semantic runs can
therefore share an evidence identity while retaining per-execution diagnostics.
The runner refuses new work once the Agent Eval artifact tree exceeds 2 GiB;
this bounds diagnostic/evidence growth without silently deleting cited evidence.

The Query constructor emits construction start/completion, storage and command
failure events through `query_constructor`. It logs only profile, source hash,
Query-set ID and aggregate counts. Raw original and synthetic Query text exists
only in the private immutable `query-sets/` artifact. Production enables these
two modules at `INFO` in systemd while keeping per-Query ranking diagnostics
off; journald provides the same centralized retention and rotation described
below.

## Privacy and public errors

The formatter normalizes case, punctuation and camelCase before recursively
redacting credential, password, token, secret, authorization, cookie, API key,
private key, generic error text, query text, title, description, vector, payload
or body fields. Free-form log messages are rejected: `event` must be a stable
snake_case identifier. This is a last line of defence; callers still emit only
safe codes and counts, never generic exception text.

The portfolio API returns a generic error code plus `trace_id`. Internal
exception text is not sent to the browser. The preferred search request is POST
with a JSON body, while Uvicorn and Nginx also disable default access logs. The
deprecated GET compatibility endpoint puts Query text in the URL and must not
be used for sensitive searches. The API emits a safe allowlisted route event;
unknown path content is recorded only as `unmatched`.

## Server viewing, filtering, and export

The systemd unit writes JSON events to journald:

```bash
sudo journalctl -u search-engine-eva-agent -n 100 -o cat
sudo journalctl -u search-engine-eva-agent --since '15 minutes ago' -o cat \
  | jq -R 'fromjson? | select(.module == "api")'
sudo journalctl -u search-engine-eva-agent -o cat \
  | jq -R 'fromjson? | select(.trace_id == "TRACE_ID_FROM_ERROR")'
```

Export only the time range and module needed for diagnosis. Review exported
files before sharing even though structured fields are redacted by default.

The application does not create unbounded log files. CLI capture files are
operator-owned and should be deleted after diagnosis. Production retention,
rotation and size limits are controlled centrally by journald settings such as
`SystemMaxUse` and `MaxRetentionSec`; inspect the host policy with:

```bash
systemd-analyze cat-config systemd/journald.conf
```

## Reproduce by subsystem

1. `data`: rerun `--validate-only` with only `data=DEBUG`.
2. `evaluation`: use the fixed smoke profile with `evaluation=DEBUG` and keep
   `ranking=OFF` unless a specific Query needs inspection. For a Run comparison,
   use `compare_cli` with `evaluation=INFO` and filter by `comparison_id` or the
   two validated Run IDs. Filter by `operation == "stage2_compare_runs"` to
   isolate one command type. A validation failure occurs before
   `run_comparison_started`, so only command start/failure events are expected.
3. `ranking`: enable `ranking=DEBUG`; filter by `query_id`. Raw Query text is in
   the immutable Run evidence rather than duplicated into logs.
4. `backend`: run the ten-product local smoke with `backend=DEBUG`; OpenSearch
   remains an explicitly separate optional integration.
5. `api`: reproduce the request and filter journald using the response
   `X-Request-ID` or error `trace_id`.
6. `catalog`: run the builder with only `catalog=DEBUG`, or reproduce a POST
   search and correlate the `catalog_search_completed` event using the API
   trace ID. Health status distinguishes a ready index from a missing/corrupt
   one without exposing its filesystem path.
7. `agent_runtime`: run `make agent-smoke` with only
   `agent_runtime=DEBUG`; filter by `trace_id`, `state` and `step`.
8. `agent_model`: enable only `agent_model=DEBUG` to see decision boundaries
   without tool payloads. The current source is the deterministic fixture.
9. `agent_tools`: enable only `agent_tools=DEBUG`; filter by `tool_name` and
   `error_code`. Inspect the Trace, not logs, when evidence payloads are needed.
10. `agent_trace`: enable only `agent_trace=INFO` to confirm immutable Trace
    publication. Enable only `agent_replay` when isolating schema, hash, state
    or report-validation failures during offline Replay.
11. `agent_optimization`: enable only `agent_optimization=INFO` while calling
    `/agent/strategy/propose`, `/agent/strategy/decision` or
    `/agent/strategy/catalog`; locally inspect `runs/strategy-proposals/`,
    `runs/strategy-decisions/` and `runs/search-strategies/`. In production,
    inspect the corresponding subdirectories below
    `/var/lib/search-engine-eva-agent/runtime/` as an authorized operator.
12. `retrieval`: enable only `retrieval=DEBUG` while calling
    `/agent/retrieval/analyze`; filter by `pipeline_run_id`, `pipeline_id` or
    `channel_id`. Inspect the private Run only when stage rankings are required.
13. `stage_diagnosis`: enable only `stage_diagnosis=INFO`; filter by
    `diagnosis_id` or `pipeline_run_id` to isolate diagnosis from the per-Query
    retrieval work.
14. `retrieval_analysis`: enable only `retrieval_analysis=INFO`; filter by
    `comparison_id`, `candidate_run_id` or `pipeline_run_id` to inspect bounded
    experiment publication without enabling either retrieval or diagnosis logs.
    The current `/agent/retrieval/analyze` entry is orchestrated by
    `agent_runtime`; enable `agent_runtime=INFO,agent_tools=INFO` to isolate the
    ordered Agent/tool boundaries, then enable `retrieval` or `stage_diagnosis`
    only when the lower-level search stage is the suspected fault.
15. `agent_eval`: run `make agent-eval` with only `agent_eval=INFO`; filter by
    `suite_id`, `task_id` or `evidence_id`. Enable `agent_runtime` or
    `agent_replay` separately only when that boundary is under investigation.
    Detailed Trace evidence remains private.
16. `query_constructor`: run `make query-set-smoke` with only
    `query_constructor=INFO`; filter by `query_set_id`. Inspect the private
    artifact for case text; logs deliberately expose only counts and hashes.
17. `bad_case`: run `make bad-cases-smoke` with only `bad_case=DEBUG`; filter by
    `execution_id`, `diagnostic_id`, `failure_stage` and
    `completed_query_count`. Search text, product IDs/titles and exception
    messages never enter logs. Enable `catalog=INFO` separately to inspect the
    59-call boundary and interruptible SQL timing.

`tests/test_observability.py` and `tests/test_catalog_search.py` verify JSON structure, module isolation,
redaction variants, error classification, stable events, low-noise defaults and
handler de-duplication. Comparison CLI tests verify success correlation, real
validation and storage failures, failure stages, and that all Query/product
evidence plus paths stay out of stderr. API tests verify that successful,
backend-failed and unhandled requests remain traceable without logging Query
strings or exposing internal error causes. Deployment contract tests keep
Uvicorn access logging disabled in both local and systemd entry points.
Agent Runtime tests additionally verify independent module control, safe
success/failure correlation, payload redaction, Trace validation and that
offline Replay invokes neither Planner nor tools. Strategy optimization tests
verify proposal/decision/catalog behavior and that proposal diagnostics do not
log raw Query text.
Retrieval and stage-diagnosis tests additionally verify independent module
control, label-blind inputs, dev-before-read locking, privacy-safe success and
failure events, immutable artifact storage and that analysis cannot activate a
strategy. Retrieval Runtime component and API tests also verify minimal
capabilities, bounded retry, semantic action order, Trace/Replay consistency and
that the browser summary contains evidence IDs and gate names rather than raw
Query or product content.
Agent Eval tests verify all 12 static Oracles, deterministic evidence identity,
dynamic execution receipts, Replay/tamper behavior, zero strategy writes and
module-specific privacy. Query-constructor tests verify smoke-only authorization
before reads, projected-column minimization, global de-duplication, no label
inheritance, confined immutable storage and log privacy. Deployment tests keep
all owner-only exact API locations behind Basic Auth and strip the Authorization header.
Bad Case tests additionally verify whole-batch preflight, exact 59-call
completion, mid-batch failure counts, SQLite deadline interruption,
cross-process locking, source/index/authority binding, offline tamper rejection,
raw-content exclusion and owner-only sample limits. Production keeps verbose
`bad_case` logging off unless that subsystem is being diagnosed.
