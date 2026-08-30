# Search evaluation Agent Runtime

The retrieval workbench supports a deterministic control and explicitly
selected LLM providers over the same policy-controlled Runtime:

- `deterministic` is the reproducible control and remains the default;
- `llm` with `openai` uses the fixed OpenAI provider adapter;
- `llm` with `volcengine_agent_plan` uses the fixed Volcengine Agent Plan
  adapter.

An LLM Planner makes a fresh model decision after every validated Observation
and can change experiment order or stop early. Provider selection changes only
the model-call adapter; it does not change the option set, tools, evidence,
budgets, Harness gates or human approval boundary. The legacy
`SEARCH_AGENT_PLANNER=openai` spelling remains a compatibility alias for the
OpenAI provider, not a generic endpoint mode.

The provider decision and rejected alternatives are recorded in
[ADR-010](adr/010-volcengine-agent-plan-provider.md).

No mode or provider can approve, activate or deploy a strategy. The Human Owner
keeps that authority.

## Control loop

```text
Task + bounded Memory (current Trace only)
                 │
                 ▼
 server derives allowed option IDs
                 │ aggregate-only Observation
                 ▼
     deterministic Planner or LLM worker
                 │ one selected option ID
                 ▼
 server maps option to canonical ToolAction / FinishDecision
                 │
                 ▼
 Runtime scope + capability + budget checks
                 │
                 ▼
 allowlisted retrieval Tool ──► Harness evidence
                 │                    │
                 └──── Observation ◄──┘
                              │
                              └── repeat or finish
```

The LLM does not receive Query text, product IDs/titles, ranked lists,
`changed_query_examples`, recovered-product examples, artifact paths, evidence
references, tool arguments or credentials. It sees only strict aggregate stage
deltas, gate results and bounded risk rates. It may select one of six
server-defined IDs:

- diagnose the baseline;
- run the uniform, conservative or aggressive candidate;
- finish with the server-computed best passing candidate;
- finish with no safe improvement after every candidate failed.

The server—not the model—constructs Run IDs and evidence references, selects
the best passing candidate, verifies terminal facts, and hashes the decision
receipt into the Trace. This is an LLM decision loop, but it is not arbitrary
code generation or unrestricted tool use.

## Model isolation and budgets

Each model decision runs in a fresh child process using one fixed, reviewed
provider endpoint. OpenAI and Volcengine Agent Plan have separate provider
adapters and Key namespaces. Volcengine Agent Plan is fixed to
`https://ark.cn-beijing.volces.com/api/plan/v3` and the resulting
`/api/plan/v3/responses` resource; there is no configurable `base_url`.

The API process never imports the provider SDK and the Planner object never
stores the Key. The worker has:

- a fixed command and minimal environment;
- one strict function call with parallel calls disabled;
- Volcengine deep thinking explicitly disabled for this bounded option-selection
  step; the macOS launcher pins the Agent Plan model ID that was verified in
  the subscription console (`doubao-seed-2.1-turbo` as of 2026-08-30);
- SDK retries disabled;
- 32 KiB input and 8 KiB output ceilings;
- a hard parent deadline followed by terminate/kill;
- stable error codes with third-party messages discarded.

The configured model remains explicit. OpenAI responses must echo that model
exactly. Agent Plan may report the actual resolved model name/version instead of
the package alias: the adapter accepts only an exact normalized alias or a
version suffix inside the same requested family. A different family or tier is
rejected. Response status, model, output, usage and ID failures have distinct
safe error codes so compatibility can be debugged without logging provider
content.

The LLM path allows at most six planning steps, four tool calls, four Run
creations, one failure, one attempt per canonical action and 120 seconds. It
also caps per-call output and cumulative model input/output Tokens. Exceeding a
model or Runtime budget fails the run before another Tool is dispatched.

## Verified real-provider smoke

On 2026-08-30, the Owner used the native hidden-input launcher to configure the
dedicated Agent Plan Key, authorized the aggregate-only outbound call and ran
the first successful real-provider Smoke. The requested alias resolved to
`doubao-seed-2-1-turbo-260628`. The bounded loop made four model decisions,
executed three allowlisted Tool calls over four steps, consumed 4,192 total
Tokens and ended `proposal_ready` (Trace
`b767e4c84bda412080939e9275454233`). After the uniform candidate failed seven
Harness gates, the model selected the conservative candidate, which passed all
configured smoke gates, then stopped with the server-computed best passing
candidate. Offline Replay independently validated the ten-event Trace and
reconstructed terminal report without invoking the provider.

Codex implemented the adapter, Runtime and validation boundary; the Owner
supplied the runtime credential and executed and authorized the external
validation. This single fixed 20-Query Smoke validates connectivity,
response-contract compatibility and one observe-decide-act path—not
repeatability, broader search quality or production readiness. It did not
approve, activate or deploy a strategy.

## Configure locally without exposing the Key

> **Agent Plan API mode:** current package docs and console evidence indicate
> Agent Plan can call through its dedicated API endpoint when the caller uses
> the dedicated Agent Plan key and plan-compatible model list. For this
> integration we keep strict isolation: `/api/plan/v3/responses` for
> `volcengine_agent_plan`, and `/api/v3/responses` must only be used by the
> separate normal Ark provider with its own cost path. Do not mix keys or base
> URLs across these providers.

Install the project dependencies. On macOS, use the native hidden-input
launcher rather than sending the Key to Codex or entering it in a browser.

```bash
cd /Users/bytedance/Documents/ChatGPT/game/search-engine-eva-agent
make agent-api-volcengine-macos
```

The launcher compiles a short local AppKit helper, opens an
`NSSecureTextField`, and the native child directly replaces itself with the
fixed loopback Uvicorn process. The Key appears only in the provider-specific child
environment; it never crosses Shell/AppleScript output, command arguments,
the clipboard API, browser storage, a file or a diagnostic event. The child
environment is built from an allowlist, so proxy variables, Python injection
variables and unrelated provider credentials are not inherited. Core dumps
are disabled, the evidence directory is mode `0700`, and access logging stays
off. The temporary compiled helper contains no Key. Its Key-free wrapper waits
only so the helper and private build directory can be removed after the native
child/backend exits; it never receives the Key.

This action starts only the local backend. It does not call the provider and
does not POST an Agent Smoke. Check the safe status endpoint first; a real
Smoke remains a separately authorized action. The launcher also deliberately
does not set `SEARCH_CODE_REVISION`, so the existing clean-worktree evidence
gate remains authoritative.

The terminal hidden prompt remains a manual fallback. Do not send a Key in
chat, put it in JavaScript, commit it, or add it to a URL/request body.

```bash
cd /Users/bytedance/Documents/ChatGPT/game/search-engine-eva-agent
.venv/bin/pip install -e '.[dev]'

read -rsp 'Volcengine Agent Plan API key: ' SEARCH_VOLCENGINE_AGENT_PLAN_API_KEY; echo
export SEARCH_VOLCENGINE_AGENT_PLAN_API_KEY
export SEARCH_AGENT_PLANNER=llm
export SEARCH_LLM_PROVIDER=volcengine_agent_plan
export SEARCH_LLM_MODEL='<explicit reviewed Agent Plan model ID>'
export SEARCH_LLM_TIMEOUT_MS=30000
export SEARCH_LLM_MAX_OUTPUT_TOKENS=128
```

For OpenAI instead, select `SEARCH_LLM_PROVIDER=openai` and inject
`SEARCH_OPENAI_API_KEY` through the same hidden-prompt/server-secret process.
Never reuse one provider's Key variable as another provider's credential. The
model is always explicit; no provider supplies a model default.

The provider receives a 30-second SDK timeout. Its disposable worker has a
separate 40-second hard wall-clock deadline, reserving 10 seconds for process
startup, SDK import and a final structured envelope before terminate/kill. The
128-Token output ceiling remains unchanged; it covers both reasoning and the
strict function-call output. The Volcengine adapter sends
`thinking.type=disabled` because this Planner step is classification over a
finite server allowlist, not an open-ended reasoning task.

When using the manual fallback, start the loopback API in that same shell. The
Key is inherited only by the server and its disposable provider worker:

```bash
export SEARCH_BACKEND=local
export SEARCH_CATALOG_INDEX="$PWD/data/index/catalog-baseline-v1.sqlite3"
export SEARCH_AGENT_ARTIFACT_ROOT="$(mktemp -d /private/tmp/search-agent-llm.XXXXXX)"

.venv/bin/python -m uvicorn apps.api.main:app \
  --host 127.0.0.1 --port 8000 --no-access-log
```

An analysis run requires a clean, committed worktree so its evidence can bind
to the real Git revision. Do not bypass that gate by labeling uncommitted code
with the current `HEAD`; use the status endpoint and Fake-provider tests until
the implementation has been committed.

Check configuration without making a model or search call:

```bash
curl http://127.0.0.1:8000/agent/retrieval/status
```

`state=ready` means the selected LLM provider has a syntactically valid,
explicit model and its own Key configuration. `state=not_configured` fails
closed before the baseline Tool. The status response may disclose provider and
model IDs, but never Key contents. There is no cross-provider Key fallback and
no silent fallback to the deterministic Planner after a provider error. A
configured Volcengine process exposes these safe status fields:

```text
planner_mode=llm
provider_id=volcengine_agent_plan
model_id=<explicit reviewed Agent Plan model ID>
state=ready
```

These are the expected safe status fields for a configured Volcengine process;
the actual endpoint returns JSON rather than this explanatory notation. To use
the control explicitly, set:

```bash
export SEARCH_AGENT_PLANNER=deterministic
```

When the API stops, remove the Key from the current shell:

```bash
unset SEARCH_VOLCENGINE_AGENT_PLAN_API_KEY SEARCH_OPENAI_API_KEY SEARCH_LLM_API_KEY
```

For production, inject the same variable names through the root-owned
`/etc/search-engine-eva-agent.env` (`0600`) or a stronger host secret manager.
Never put a real value in the unit file, repository, deployment output or
documentation.

## Trace, Replay and observability

Every LLM decision records only safe provenance: provider, model ID, prompt and
schema versions, selected option ID, option count, Token counts, duration and a
SHA-256 digest of the provider response ID. Prompt text, response bodies,
free-form reasoning and the Key are not stored. Replay validates that every
recorded option was legal in its exact Observation state and mapped to the
canonical action/finish decision.

The authenticated local analysis API separately returns at most ten
`changed_query_examples` for the Owner's workbench. Those examples can contain
Query text, product IDs/titles and before/after results, so they remain local
protected evidence and are never copied into the LLM request, Trace or
diagnostic logs. "Returned by the local API" must not be read as "sent to the
model provider."

Debug one boundary at a time:

```bash
SEARCH_LOG_LEVEL=OFF SEARCH_LOG_LEVEL_AGENT_MODEL=DEBUG \
  .venv/bin/python -m uvicorn apps.api.main:app \
  --host 127.0.0.1 --port 8000 --no-access-log 2>planner.jsonl

SEARCH_LOG_LEVEL=OFF SEARCH_LOG_LEVEL_AGENT_PROVIDER=DEBUG \
  .venv/bin/python -m uvicorn apps.api.main:app \
  --host 127.0.0.1 --port 8000 --no-access-log 2>provider.jsonl
```

Use `agent_model` for option decisions, `agent_provider` for worker/provider
lifecycles, `agent_runtime` for state/budgets, `agent_tools` for the two search
tools, `agent_trace` for publication, and `agent_replay` for offline
validation. Production defaults keep verbose events disabled. Logs contain
safe IDs, counts, durations and stable codes only.

## Current capability boundary

The Agent can now genuinely choose and execute among the three existing
retrieval candidates and stop based on Harness evidence. It cannot yet invent
a new vector channel, train a coarse/fine ranker, tune arbitrary parameters or
author code. Those capabilities require new strict strategy-construction tools
and independent evaluation gates; giving the model shell access is not an
acceptable shortcut.
