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
| `catalog_index` | Streamed catalog v2 build, verification and atomic publication | `WARNING` |
| `catalog_pipeline` | Catalog v2 recall channels, fusion and coarse-rank timing | `WARNING` |
| `catalog_serving` | Active pointer resolution, validation, activation and rollback | `WARNING` |
| `data` | Stage 1 validation and build lifecycle | `WARNING` |
| `evaluation` | Baseline/Run comparison, validation and artifact storage | `WARNING` |
| `ranking` | Per-Query Ranker diagnostics | `OFF` |
| `agent_runtime` | State, policy, budget and terminal lifecycle | `WARNING` |
| `agent_model` | Planner option selection, model-call budgets and decision boundary | `WARNING` |
| `agent_provider` | Isolated provider-worker lifecycle, safe usage counts and stable failures | `WARNING` |
| `agent_tools` | Allowlisted tool call lifecycle and stable failures | `WARNING` |
| `agent_trace` | Trace artifact publication | `WARNING` |
| `agent_replay` | Offline Trace validation and Replay lifecycle | `WARNING` |
| `agent_optimization` | Strategy proposal, decision and catalog lifecycle | `WARNING` |
| `agent_eval` | Fixed Agent task suite, independent grading and artifact publication | `WARNING` |
| `query_constructor` | Source-bounded Query construction and immutable storage | `WARNING` |
| `bad_case` | Fixed 59-Query batch, evidence publication, rerun and failed attempts | `WARNING` |
| `bad_case_supervisor` | Parent process-group deadline, termination and immutable supervisor receipt | `WARNING` |
| `bad_case_worker` | Isolated child startup, fixed diagnostic execution and terminal envelope | `WARNING` |
| `diagnostic_experiments` | Trusted diagnostic loading, evidence routing and bounded experiment planning | `WARNING` |
| `human_oracle` | Owner-only diagnostic batch, blind intent review, behavior replay, CAS and seal | `WARNING` |
| `launcher_dialog` | Native macOS hidden-input lifecycle and stable validation outcomes | `INFO` locally |
| `launcher_backend` | Native launcher preflight and fixed loopback backend handoff | `INFO` locally |
| `retrieval` | Label-blind recall channels, fusion, stage retention and retrieval Runs | `WARNING` |
| `stage_diagnosis` | Stage evidence validation and bottleneck diagnosis | `WARNING` |
| `retrieval_analysis` | Bounded experiment orchestration and artifact publication | `WARNING` |
| `retrieval_release` | Immutable Retrieval proposal, Owner decision, validation outcome and rollback projection | `WARNING` |

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
export SEARCH_LOG_LEVEL_CATALOG_INDEX=INFO
export SEARCH_LOG_LEVEL_CATALOG_PIPELINE=DEBUG
export SEARCH_LOG_LEVEL_CATALOG_SERVING=INFO
export SEARCH_LOG_LEVEL_AGENT_RUNTIME=DEBUG
export SEARCH_LOG_LEVEL_AGENT_MODEL=INFO
export SEARCH_LOG_LEVEL_AGENT_PROVIDER=INFO
export SEARCH_LOG_LEVEL_AGENT_EVAL=INFO
export SEARCH_LOG_LEVEL_QUERY_CONSTRUCTOR=INFO
export SEARCH_LOG_LEVEL_BAD_CASE=INFO
export SEARCH_LOG_LEVEL_BAD_CASE_SUPERVISOR=INFO
export SEARCH_LOG_LEVEL_BAD_CASE_WORKER=INFO
export SEARCH_LOG_LEVEL_DIAGNOSTIC_EXPERIMENTS=INFO
export SEARCH_LOG_LEVEL_HUMAN_ORACLE=INFO
export SEARCH_LOG_LEVEL_LAUNCHER_DIALOG=INFO
export SEARCH_LOG_LEVEL_LAUNCHER_BACKEND=INFO
export SEARCH_LOG_LEVEL_RETRIEVAL=DEBUG
export SEARCH_LOG_LEVEL_RETRIEVAL_ANALYSIS=INFO
export SEARCH_LOG_LEVEL_RETRIEVAL_RELEASE=INFO
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

SEARCH_LOG_LEVEL=OFF \
SEARCH_LOG_LEVEL_BAD_CASE_SUPERVISOR=DEBUG \
SEARCH_LOG_LEVEL_BAD_CASE_WORKER=INFO \
.venv/bin/python -m search_quality.bad_cases.cli \
  2>bad-case-worker-debug.jsonl
```

To isolate one subsystem, set the global level to `OFF` and enable only that
module:

```bash
SEARCH_LOG_LEVEL=OFF SEARCH_LOG_LEVEL_EVALUATION=DEBUG \
  make eval-baseline 2>evaluation-only.jsonl
```

The macOS Agent Plan launcher has two independently filtered, structured
stderr modules. For example, use
`SEARCH_LOG_LEVEL_LAUNCHER_DIALOG=OFF` to suppress dialog lifecycle events or
`SEARCH_LOG_LEVEL_LAUNCHER_BACKEND=DEBUG` to isolate backend handoff metadata.
It emits only timestamps, trace IDs, attempts, fixed provider/model/host/port
and stable error codes. It never emits the Key, its prefix/length/digest,
parent environment, command environment, AppKit field contents or provider
responses. These local events remain in the launching Terminal; the launcher
does not create a persistent log. Uvicorn and Agent modules retain their own
independent levels after the native child directly hands off to the backend;
the Key-free wrapper waits only to clean up the temporary helper at process
exit.

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
`agent_model`, `agent_provider`, `agent_tools`, `agent_trace` and `agent_replay` can each be
enabled without enabling the others.

The optional LLM Planner records only the allowlisted option ID, model ID,
provider ID (`openai` or `volcengine_agent_plan`), call/Token counts, duration
and stable failure code. The disposable provider worker never logs either
provider's API Key, Prompt, provider response, function arguments,
Authorization header or third-party exception message. Trace stores the same
bounded provenance plus a digest of the provider response ID; it does not store
free-form reasoning or chain-of-thought. Authentication debugging must use the
stable provider error code and configured provider ID; never add a Key prefix,
length, digest, response body or provider exception text to logs.

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

The stage-aware retrieval slice uses seven independently controlled modules.
`agent_runtime` records the bounded task lifecycle and terminal outcome;
`agent_model` records the selected finite option and model-call budget, while
`agent_provider` records the isolated worker/provider lifecycle.
`agent_tools` records the two allowlisted retrieval-tool boundaries. They use
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
category/count summaries and stable outcomes. Completion events also report
only the number of changed Query examples split into improvement and regression;
the public response may display bounded examples from the committed public ESCI
smoke fixture, but those examples never enter diagnostics. The API module emits
`public_retrieval_analysis_cache_miss`,
`public_retrieval_analysis_cache_hit`,
`public_retrieval_analysis_cache_hit_after_lock`,
`public_retrieval_analysis_cached` and
`public_retrieval_analysis_rejected_busy` with only the fixed profile. A public
failure emits `public_retrieval_analysis_failed` with stable error metadata and
the request trace. The public route is permanently deterministic and returns a
strict v1 projection, so these events never identify configured provider/model
or Token usage.

The separate Owner-only full-v2 route does not use the public cache. Its API
boundary emits `owner_retrieval_analysis_rejected_busy`,
`owner_retrieval_planner_unavailable` or
`owner_retrieval_analysis_failed`; a successful run is correlated through the
existing `agent_runtime`, `agent_model`, `agent_provider`, `agent_tools` and
`retrieval_analysis` events. `agent_retrieval_configuration_invalid` isolates
an invalid Owner status configuration. None logs raw Query text, product
identifiers, titles, labels, ranked lists, filesystem paths, Prompt, provider
response, Key or exception message. An Owner run must never emit
`public_retrieval_analysis_cache_hit*` or
`public_retrieval_analysis_cached`.

At the proxy boundary, rejected public analysis requests are independently
recorded in
`/var/log/nginx/search-agent-public-analysis-rejection.log`. The JSON schema is
limited to timestamp, request ID, source IP, status, method, exact URI and
duration. Request bodies, arguments, credentials, cookies, Owner identity and
response content are excluded. Successful calls are not written to that file.
Production rotation reuses the host `/etc/logrotate.d/nginx` wildcard; verify
coverage with `logrotate --debug` rather than adding a duplicate stanza.

The public browser and model-provider data planes must remain distinct when
diagnosing an LLM run. `POST /agent/retrieval/analyze` always constructs
`ObservationDrivenRetrievalPlanner`; it never resolves
`SEARCH_AGENT_PLANNER`, provider/model configuration or a Key. Only Owner-only
`POST /agent/retrieval/analyze-owner` may enter `agent_provider`. Apart from
fixed instructions/schema and call metadata, the OpenAI or Volcengine model
evidence input receives only aggregate Observation counts, deltas, gates and
risk rates; the Key is used only for authentication. The public projected v1
API may return bounded `changed_query_examples` with committed
Query/product/result details, but the application must never log them or copy
them into a provider model input. Filter `agent_provider` by provider ID and
model to inspect the Owner network boundary; inspect the private local evidence
artifact, not provider logs, when full comparison evidence is needed.

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

The killable Bad Case boundary is split across `bad_case_supervisor` and
`bad_case_worker`. The parent records dispatch, deadline/termination outcome and
publication of the immutable supervisor receipt. The child records only its
fixed startup and terminal state. Safe fields are execution/diagnostic/receipt
IDs, policy, bounded counts, durations, signal names and stable error codes.
Neither side logs environment values, filesystem paths, Query/product content,
IPC payloads, exception messages or credentials. Enable the two modules
independently to distinguish parent supervision faults from search execution
faults.

`diagnostic_experiments` records trusted artifact loading, evidence routing and
query-route generation using only content IDs, allowlisted strategy IDs,
aggregate counts and stable failures. It never logs Query routes, protected
tokens, raw evidence bodies or product content. A generated plan is behavior
evidence only: the module does not compute relevance metrics, write a strategy
or activate search configuration.

`human_oracle` records batch/view/submission/replay/seal lifecycle using only
batch, unit, case and annotation IDs plus bounded counts and stable error types.
It never logs raw principal values, principal HMACs, the Owner allowlist digest,
reason/judgment values, Query text, product IDs/titles, result lists, request
bodies, paths or exception messages. Raw intent and Top-3 behavior views are transient owner-only HTTP
responses with `no-store`; immutable Oracle artifacts contain hashes and IDs,
not raw display content.

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
   For v2, enable `catalog_index`, `catalog_pipeline` and `catalog_serving`
   independently. `catalog_index` isolates streamed batch/build failures;
   `catalog_pipeline` isolates title/exact/multi-field, RRF and coarse counts;
   `catalog_serving` isolates pointer compatibility, activation CAS, sentinel
   and rollback. See `docs/CATALOG_V2_SERVING.md` for commands and artifact
   semantics.
7. `agent_runtime`: run `make agent-smoke` with only
   `agent_runtime=DEBUG`; filter by `trace_id`, `state` and `step`.
8. `agent_model`: enable only `agent_model=DEBUG` to see deterministic or LLM
   decision boundaries, selected option IDs and budget state without prompts or
   tool payloads.
9. `agent_provider`: enable only `agent_provider=DEBUG`; filter by `model`,
   `provider`, `option_id` and stable `error_code` to distinguish worker
   startup, provider rejection and hard timeout. Never add Prompt/response
   logging while debugging authentication; verify the selected provider uses
   its own Key variable, then rotate the Key if needed. A Volcengine
   authentication failure must not be retried with the OpenAI Key or endpoint.
10. `agent_tools`: enable only `agent_tools=DEBUG`; filter by `tool_name` and
   `error_code`. Inspect the Trace, not logs, when evidence payloads are needed.
11. `agent_trace`: enable only `agent_trace=INFO` to confirm immutable Trace
    publication. Enable only `agent_replay` when isolating schema, hash, state
    or report-validation failures during offline Replay.
12. `agent_optimization`: enable only `agent_optimization=INFO` while calling
    `/agent/strategy/propose`, `/agent/strategy/decision` or
    `/agent/strategy/catalog`; locally inspect `runs/strategy-proposals/`,
    `runs/strategy-decisions/` and `runs/search-strategies/`. In production,
    inspect the corresponding subdirectories below
    `/var/lib/search-engine-eva-agent/runtime/` as an authorized operator.
13. `retrieval`: enable only `retrieval=DEBUG` while calling
    public `/agent/retrieval/analyze` or Owner
    `/agent/retrieval/analyze-owner`; filter by `pipeline_run_id`, `pipeline_id`
    or `channel_id`. Inspect the private Run only when stage rankings are
    required.
14. `stage_diagnosis`: enable only `stage_diagnosis=INFO`; filter by
    `diagnosis_id` or `pipeline_run_id` to isolate diagnosis from the per-Query
    retrieval work.
15. `retrieval_analysis`: enable only `retrieval_analysis=INFO`; filter by
    `comparison_id`, `candidate_run_id` or `pipeline_run_id` to inspect bounded
    experiment publication without enabling either retrieval or diagnosis logs.
    Both analysis entries are orchestrated by `agent_runtime`; enable
    `agent_runtime=INFO,agent_tools=INFO` to isolate the ordered Agent/tool
    boundaries, then enable `retrieval` or `stage_diagnosis` only when the
    lower-level search stage is the suspected fault. For the public route,
    verify `public_retrieval_analysis_cache_*` events and absence of
    `agent_provider`. For the Owner route, verify the absence of public-cache
    events and enable `agent_model`/`agent_provider` only if its configured
    Planner is LLM.
16. `retrieval_release`: enable only `retrieval_release=INFO`; filter by
    `proposal_id`, `decision_id`, `outcome_id`, `rollback_id`, `lifecycle` or stable
    `error_code`. Inspect private `retrieval-release-proposals/`,
    `retrieval-release-decisions/`, `retrieval-release-outcomes/` and
    `retrieval-release-rollbacks/` artifacts when the full config, validation
    receipt or rollback receipt is required. Logs deliberately
    omit Query/product evidence, actor identity, client action IDs, receipt
    bodies, credentials and filesystem paths. Reproduce proposal creation,
    Owner decision, serving-outcome recording and rollback projection
    independently; `approved_for_validation` is not an active-strategy event,
    and the authoritative serving pointer prevents a stale outcome from being
    projected as active before its rollback record is published.
17. `agent_eval`: run `make agent-eval` with only `agent_eval=INFO`; filter by
    `suite_id`, `task_id` or `evidence_id`. Enable `agent_runtime` or
    `agent_replay` separately only when that boundary is under investigation.
    Detailed Trace evidence remains private.
18. `query_constructor`: run `make query-set-smoke` with only
    `query_constructor=INFO`; filter by `query_set_id`. Inspect the private
    artifact for case text; logs deliberately expose only counts and hashes.
19. `bad_case`: run `make bad-cases-smoke` with only `bad_case=DEBUG`; filter by
    `execution_id`, `diagnostic_id`, `failure_stage` and
    `completed_query_count`. Search text, product IDs/titles and exception
    messages never enter logs. Enable `catalog=INFO` separately to inspect the
    59-call boundary and interruptible SQL timing.
20. `bad_case_supervisor`: enable only this module and filter by `execution_id`
    or supervisor `receipt_id` to distinguish dispatch, hard deadline,
    TERM/KILL escalation and durable completion. Enable `bad_case_worker`
    separately only when child startup or envelope production is suspect.
21. `diagnostic_experiments`: enable only this module while requesting a plan;
    filter by `diagnostic_id`, `query_set_id`, `experiment_plan_id` or the
    allowlisted `strategy_id`. Inspect the private evidence artifacts when the
    plan details are required; they are intentionally absent from logs.
22. `human_oracle`: enable only this module while creating/reviewing one batch;
    filter by `oracle_batch_id`, `unit_id` or annotation ID. Inspect the private
    Oracle directories for immutable state. Reproduce intent-view, behavior
    replay, CAS conflict and seal independently; never export transient raw
    views or the principal-HMAC key with diagnostics.

`tests/test_observability.py`, `tests/test_catalog_search.py` and
`tests/test_catalog_v2_serving.py` verify JSON structure, module isolation,
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
Retrieval-release tests independently verify content-addressed proposal
publication, complete pipeline/config binding, semantic Run/Comparison/
Diagnosis/Trace revalidation, proposal/parent CAS, client-action idempotency,
the `pending_owner_review -> approved_for_validation/rejected` boundary,
validation outcome recording and privacy-safe module isolation. Approval tests
also assert that no active serving pointer is written; rollback tests verify
receipt/from-revision binding, target-pointer CAS, exact idempotency and the
fail-closed `rolled_back` catalog projection.
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
Worker tests additionally launch a real isolated child, enforce bounded IPC,
exercise TERM/KILL and deadline-boundary recovery, and verify a private
content-addressed supervisor receipt. Diagnostic-experiment tests verify fixed
ID loading, allowlisted strategies, token protection and locked quality/write
lanes. Human Oracle tests verify blind-view separation, complete cluster
census, append-only idempotency/CAS, intent invalidation, server-side Top-10
replay, seal completeness, module-isolated failure logs and absence of raw or
sensitive fields.
