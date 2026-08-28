# ADR-003: Smoke-only deterministic Agent Runtime scaffold

**Status:** Accepted as a constrained Stage 3/4 foundation; not a completed
search-evaluation Agent

**Date:** 2026-08-28

**Product direction:** The Owner requires the final project to visibly behave as
an Agent and approved continuing with this milestone.

**Technical design:** Codex proposed and implemented the Runtime architecture,
tool boundary, policies, Trace/Replay contract and tests. See D-016 in
[`docs/CONTRIBUTION_LOG.md`](../CONTRIBUTION_LOG.md).

## Context

Stage 2 can create deterministic smoke Runs and compare their rankings. Calling
those functions from one fixed script would still be a workflow: the next action
would be known before any result was observed. The project needs a narrow first
slice that proves an observation can change the next action, while the 500-Query
dev profile remains locked and before any external model is trusted with tools.

The milestone must not add arbitrary code execution, filesystem paths, network
access or frozen-test access. It also must not turn fluent model text into
quality evidence.

## Decision

Implement a Python, domain-specific Runtime scaffold with these boundaries:

- `AgentTask` contains one ordered baseline/candidate smoke comparison.
- A static registry exposes only `run_ranker`, `evaluate_run`, `compare_runs`
  and `inspect_query`, with strict input and output models.
- Tools accept safe identifiers, not paths. A trusted registry revalidates and
  pins admitted Run artifacts before use.
- `FakeBranchingPlanner` is a deterministic fixture. It compares the requested
  Runs, inspects a bounded number of observed regressions, and then returns an
  evidence-referenced terminal decision.
- The Runtime enforces finite steps, calls, Run creations, failures, repeated
  actions, capabilities and decision/observation/Trace sizes.
- Every action, bounded observation and terminal result is recorded in a
  hash-chained Trace. Offline Replay validates the snapshot and rebuilds the
  report without invoking a Planner or tool.
- `accept` and `reject` require successful comparison evidence for the exact
  ordered Run pair in the task. The fixed comparator epsilon, aggregate metric
  directions and per-Query outcome counts are cross-validated; all cited
  comparisons must support the same terminal direction. No-change evidence is
  `inconclusive`, not an invented improvement.

The event hash chain detects accidental corruption and un-recomputed edits. It
is not a digital signature: an attacker who can rewrite a Trace can also
recompute an unkeyed SHA-256 chain. Strong producer authentication requires a
keyed signature or an external append-only evidence store and is deferred.

`max_elapsed_ms` is a cooperative budget checked between deterministic local
actions. It is not advertised as a killable timeout. Before a network model or
untrusted/possibly blocking tool is connected, Planner and tool calls must move
to a worker boundary that the parent can forcibly terminate at a monotonic
deadline.

## Why the first Planner is fake

The milestone validates Runtime mechanics independently of model variability:
state transitions, observation-driven branching, evidence grounding, policy
failure, Trace and Replay. `FakeBranchingPlanner` is explicitly not the final
LLM Agent and its templated conclusion is not presented as model intelligence.

A future provider adapter may use DeepSeek or another model, but it must produce
the same typed decisions and remain behind the same policy boundary. A provider
SDK or a general-purpose coding-agent harness is not adopted as the Runtime
core because this project needs search-specific evidence contracts and cannot
grant shell, arbitrary Python or unrestricted filesystem tools.

## Consequences

- The project can demonstrate the structural distinction between a workflow
  and an Agent: a comparison observation decides whether query inspection runs.
- All current execution stays on the fixed 20-Query smoke boundary; this work
  does not unlock dev or frozen test.
- A real model, ten-task Agent evaluation set, model/prompt versioning, token and
  cost budgets, killable deadlines, signed provenance and Web workbench remain
  future milestones.
- Replay checks the stored chain, task scope, terminal grounding, counts and
  regenerated report, but does not independently re-execute every live policy
  or tool schema. Stronger adversarial verification remains coupled to signed
  provenance rather than being implied by the local unkeyed hashes.
- Search quality is still decided by deterministic Run evidence, not by the
  Planner's wording.

## Diagnostics and verification

Runtime, model/planner, tool, Trace and Replay logs are independently
configurable. They emit safe identifiers, states, counts, duration and stable
error codes; raw Query text, product titles, tool payloads and paths remain out
of diagnostics. Tests cover schema rejection, task grounding, observation
branches, finite budgets, privacy, terminal/Trace consistency and offline
Replay. The current repository suite contains 277 passing tests; formatting,
lint and repository-policy checks also pass.
