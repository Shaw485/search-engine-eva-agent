# ADR 005: Run stage-aware retrieval optimization inside the Agent Runtime

- Status: Accepted for the smoke-only Runtime slice
- Date: 2026-08-28
- Decision source: Codex-proposed implementation of the Owner-requested autonomous
  diagnosis and evidence workflow

## Context

The stage-aware retrieval analysis can already produce a baseline, diagnose
recall/fusion/coarse-rank losses, test three multi-field BM25 + RRF candidates and
apply 12 deterministic gates. It currently runs as one orchestration function,
however, so the workbench cannot prove that an observation changed the Agent's
next decision. The existing Agent Runtime has policy, budgets, Trace and Replay,
but only supports a preselected candidate-set Run comparison task.

The current 20-Query/416-judgment pool is a smoke boundary. It does not authorize
500-Query dev, frozen test, browser approval, strategy activation or a production
search change.

## Decision

Add a second Runtime task, `optimize_retrieval_stages`, with a fixed profile and
an immutable three-variant candidate space. Expose only two domain tools:

1. `diagnose_baseline_retrieval` creates the fixed title/exact baseline and its
   stage diagnosis.
2. `run_retrieval_candidate` runs one allowlisted pipeline variant, diagnoses it,
   compares it with the pinned baseline and returns a privacy-safe gate summary.

The deterministic Planner must inspect each Tool Observation before selecting
the next action. In the current evidence path, uniform RRF fails, conservative
RRF passes, and the Agent then probes the bounded aggressive candidate; because
that candidate fails, the conservative comparison becomes the reviewable result.
Different gate or tool-failure observations may stop, retry once, or choose a
different terminal outcome.

```text
Runtime task
  -> baseline Tool -> diagnosis Observation
  -> uniform Tool  -> failed-gate Observation
  -> conservative Tool -> passing Observation
  -> aggressive Tool -> failed-gate Observation
  -> proposal_ready (Owner review only)
  -> immutable Trace -> offline Replay
```

The Runtime tool registry has no decision, catalog, activation, deployment,
shell or arbitrary-code capability. `proposal_ready` means that the selected
experiment may be reviewed; it does not create a StrategyProposal or update
`active.json`.

## Contracts and storage

- Full Retrieval Runs, Stage Diagnoses and Comparisons remain content-addressed
  under their existing artifact directories.
- Tool Observations contain bounded ID/metric/gate summaries, not Query text,
  product titles or complete artifacts.
- The Trace binds task, Planner ID, policy, tool names, ordered decisions,
  observations and the terminal report through the existing hash chain.
- Replay performs no Tool, model, search or write operation. It revalidates the
  schema, hash/state chain, action scope, evidence grounding, candidate order,
  gate-derived branch and terminal selection.
- The API returns a small cross-linked Trace summary for the workbench while the
  full Trace remains a server-side evidence artifact.

Content addressing detects inconsistent mutation; it does not authenticate the
producer. Cross-machine evidence import remains outside this trust boundary.

## Reliability and diagnostics

- The Runtime enforces explicit step, Tool-call, Run-creation, failure,
  repeated-action, elapsed-time and payload-size budgets.
- The complete Agent Run has one global retry allowance: the first retryable
  Tool error may repeat the exact action once; a later retryable failure or any
  non-retryable failure stops inconclusively. This matches the five-Run budget
  for the normal four-Run path.
- `agent_runtime`, `agent_tools`, `agent_trace`, `agent_replay`, `retrieval` and
  `stage_diagnosis` remain independently filterable log areas.
- Logs may contain Trace/Run/Diagnosis/Comparison IDs, counts and stable error
  codes. They must not contain Query/product content, credentials, response
  bodies or provider secrets.

## Trade-offs

- Two compound domain tools keep the Trace small and avoid embedding the full
  Run, but one Tool call covers execution, diagnosis and comparison internally.
  The persisted evidence objects retain the lower-level audit trail.
- A deterministic Planner is less flexible than an LLM Planner, but makes the
  first Runtime integration replayable and keeps metric/activation authority out
  of model output.
- The fixed smoke candidate order demonstrates observation-driven branching but
  does not establish production-optimal parameters or statistical significance.

## Revisit when the system grows

Revisit this decision before enabling larger data, a model Planner or browser
approval. Required additions include force-terminable workers, signed/trusted
evidence manifests, Agent Eval tasks, authenticated Owner sessions with CSRF and
Origin protection, post-approval validation, a serving-compatible full-catalog
pipeline and versioned rollback.
