# Deterministic Agent Runtime scaffold

This is the first smoke-only vertical slice of the future search evaluation
Agent. It exercises control and evidence mechanics with a deterministic planner;
it does not yet connect an LLM and is not the completed Agent promised by the
roadmap.

## What runs

```text
ordered smoke comparison task
             │
             ▼
     FakeBranchingPlanner
             │ typed ToolAction / FinishDecision
             ▼
  policy-controlled Runtime ──────► hash-chained Trace
             │                              │
             ▼                              ▼
  static four-tool registry          offline Replay
             │
             ▼
 trusted Stage 2 Run evidence
```

The deterministic branch is:

1. compare the exact baseline and candidate from the task;
2. if the comparison contains per-Query regressions, inspect up to the task's
   configured limit;
3. accept only a positive primary-metric delta with no observed regression or
   negative aggregate metric, reject a primary-metric regression, and otherwise
   return inconclusive;
4. cite only successful evidence observed in the current Trace.

`inspect_query` currently explains the candidate ranking for each selected
regression. It gives the report concrete evidence to inspect, but it does not
yet infer a root cause or propose a ranking change. That diagnosis belongs to
the real Planner milestone.

## Trust and safety boundaries

- Profile is fixed to the 20-Query `smoke` view. `dev` and frozen test are not
  arguments and cannot be selected through these tools.
- Tool names and capabilities are a static allowlist. There is no shell,
  arbitrary Python, dynamic import, general filesystem or network tool.
- Run inputs are strict IDs resolved inside one configured store. Each admitted
  artifact is schema/content validated and pinned by digest.
- Comparison evidence is valid for a terminal quality decision only when its
  ordered baseline/candidate IDs exactly match the current task.
- The comparison tie threshold is bound to the comparator's fixed `1e-12`
  policy. Aggregate directions, per-Query outcome counts and bounded regression/
  improvement summaries must agree; conflicting cited comparisons fail closed.
- Tool outputs are validated before they become observations. Invalid,
  non-JSON or oversized results become bounded failures rather than Trace data.
- Planner decisions are capped at 64 KiB, individual observations at 1 MiB and
  stored Trace files at 8 MiB. These are containment limits, not token budgets.
- Trace files are immutable snapshots with event and terminal hashes. SHA-256
  detects corruption but is not authentication against an attacker who can
  rewrite and rehash the file.
- The elapsed budget is checked between local deterministic actions. It cannot
  kill a call that never returns, so real model/network adapters stay disabled
  until calls execute in forcibly terminable workers.

## Run and replay

First create or identify two trusted smoke Runs, then run:

```bash
BASELINE_RUN_ID=random-aaaaaaaaaaaa \
CANDIDATE_RUN_ID=title-bm25-bbbbbbbbbbbb \
make agent-smoke
```

The command prints a structured terminal result and stores the corresponding
Trace under ignored `runs/agent-traces/`. Replay never calls the Planner or a
tool:

```bash
.venv/bin/search-quality-agent-replay TRACE_ID
```

The IDs above are format examples, not guaranteed existing artifacts. Use Run
IDs produced by the local Stage 2 commands.

Replay validates the event/hash/state chain, ordered task scope, grounded
terminal decision, step/tool counts and regenerated report. It does not rerun
tool output schemas or independently reconstruct every live Runtime budget
(such as repeated actions, failures or byte ceilings); those remain enforced at
execution time and bound into the local Trace context. Because the hash chain
is unkeyed, this is reproducibility and corruption checking, not hostile-file
authentication.

## Debug one module at a time

```bash
SEARCH_LOG_LEVEL=OFF SEARCH_LOG_LEVEL_AGENT_RUNTIME=DEBUG \
  BASELINE_RUN_ID="$BASELINE_RUN_ID" \
  CANDIDATE_RUN_ID="$CANDIDATE_RUN_ID" \
  make agent-smoke 2>agent-runtime.jsonl

SEARCH_LOG_LEVEL=OFF SEARCH_LOG_LEVEL_AGENT_TOOLS=DEBUG \
  BASELINE_RUN_ID="$BASELINE_RUN_ID" \
  CANDIDATE_RUN_ID="$CANDIDATE_RUN_ID" \
  make agent-smoke 2>agent-tools.jsonl

SEARCH_LOG_LEVEL=OFF SEARCH_LOG_LEVEL_AGENT_REPLAY=DEBUG \
  .venv/bin/search-quality-agent-replay "$TRACE_ID" \
  2>agent-replay.jsonl
```

Available Agent modules are `agent_runtime`, `agent_model`, `agent_tools`,
`agent_trace` and `agent_replay`. Production defaults keep them at `WARNING`.
Diagnostics contain safe IDs, states, counts, durations and error codes; Query
text, product titles, full observations, paths and exception messages are not
logged. Trace artifacts intentionally contain evidence payloads and belong in
the Git-ignored `runs/` directory; review them before sharing.

## Acceptance boundary

Passing this milestone proves a finite, observable, policy-controlled Runtime
can branch on evidence and replay its own snapshot. It does not prove model
reasoning quality, Agent task success rate, search improvement, full-catalog
Recall, adversarial Trace authenticity or production-safe timeout isolation.
Those are explicit later gates rather than implied capabilities.
