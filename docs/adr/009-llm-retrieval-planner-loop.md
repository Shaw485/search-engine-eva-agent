# ADR-009: Bounded LLM retrieval Planner loop

- Status: Adopted
- Date: 2026-08-29
- Deciders: Owner (product direction and human approval boundary), Codex
  (technical architecture, security policy and implementation)

## Context

The stage-aware Agent already had a real Runtime loop, two allowlisted tools,
strict Observations, Harness gates, Trace and Replay. Its Planner still followed
one fixed candidate sequence. The Owner explicitly asked to make it an Agent
based on an LLM that decides while executing, and offered to provide a model
Key.

Directly allowing model-generated tool names, arguments, evidence IDs or code
would let untrusted output cross execution and evaluation boundaries. Calling a
network model inside the API process would also violate ADR-003's deadline
requirement: the Runtime elapsed check cannot terminate a blocked SDK call.
Sending raw Query/product evidence to a provider would expand the current data
boundary without an Owner privacy decision.

## Decision

Add an optional `openai` Planner mode while retaining `deterministic` as the
explicit control and default.

The server derives the complete legal option set from the validated Runtime
state. The model receives only aggregate stage deltas, gate outcomes and risk
rates, then calls one strict function with one `option_id`. The server maps that
ID to a canonical `ToolAction` or `FinishDecision`. It remains responsible for
Run/evidence IDs, tool arguments, best-candidate selection and terminal
grounding.

Each decision executes in a fresh killable subprocess. The worker uses the
official provider endpoint, one strict function, disabled parallel calls,
disabled SDK retries, bounded input/output, a hard deadline and a minimal
environment. The API Key exists only in the server environment and disposable
worker; it is never accepted from the browser/API request or stored in Planner
objects, logs, Trace or Git.

The LLM path has separate model-call, Token, output, elapsed, step, tool, Run
creation, failure and repeated-action budgets. Provider/configuration failures
fail closed; there is no hidden deterministic fallback. A single-flight API
lock prevents concurrent retrieval analyses from competing for local evidence
and model budget.

Trace stores only safe model provenance and usage plus a hash of the provider
response ID. Replay recomputes the finite option set at every recorded state and
rejects an option/action mismatch. The workbench displays Planner readiness,
selected option IDs, per-call model identity/Token/latency and aggregate usage,
but never Prompt, response body or chain-of-thought.

Strategy approval, activation and deployment remain outside both Planners and
require the Owner-controlled workflow.

## Options considered and tradeoffs

### Keep only the fixed Planner

- Positive: deterministic, cheapest and simplest to reproduce.
- Negative: cannot satisfy the Owner's request for evidence-dependent LLM
  decisions or demonstrate model/runtime interaction.

### Let the LLM emit arbitrary tool calls or code

- Positive: maximum apparent flexibility and faster prototyping of novel
  strategies.
- Negative: unbounded capability, prompt-injection and argument-forgery risk;
  no trustworthy Replay; model could cross approval and data boundaries.
- Rejected.

### Call the provider SDK directly in the API process

- Positive: fewer files and lower process startup overhead.
- Negative: a blocked network call cannot be forcibly ended by the Runtime and
  imports/provider state remain in the long-lived service.
- Rejected.

### Model selects a finite option ID in a killable worker

- Positive: genuine observe-decide-act looping while preserving deterministic
  permissions, evidence construction, Harness judgment and Replay.
- Negative: limited to prebuilt experiments; one subprocess per turn adds
  latency; model outputs remain stochastic and incur provider cost.
- Adopted.

## Consequences

### Positive

- The Agent can change candidate order and stop based on actual Harness
  evidence rather than imitate a fixed sequence.
- Every model decision is attributable, bounded and replay-validated.
- Missing credentials, timeout, malformed output and secret leakage have
  isolated tests and actionable module-specific diagnostics.
- The deterministic control remains available for reproducibility and A/B
  evaluation of the Planner itself.

### Negative

- The first LLM Agent cannot invent a vector recall channel, train a ranker,
  write code or tune arbitrary parameters.
- A real provider run requires an explicit model, server-side Key, dependency
  installation and clean committed revision.
- Model quality is not proven by Runtime correctness; it needs a separate
  Planner evaluation set and repeated-run cost/quality analysis.

### Risks

- Aggregates may be insufficient for difficult strategy choices.
- Provider/model version changes may alter decisions despite the same state.
- Token counts and latency create new operating costs.
- A compromised host user can still inspect process environments; production
  should prefer host-native secret injection over a general environment file.

## Follow-up actions

1. Run the first real-key smoke only after the code is committed and record the
   exact model/Prompt versions and cost without storing provider content.
2. Add an independent Fake/recorded Planner Harness covering legal alternative
   orders, stopping decisions, malformed outputs and budget failures.
3. Measure deterministic versus LLM Planner task success, repeated-run
   variance, Tokens and latency before choosing a production default.
4. Design strict strategy-construction tools for new recall/ranking components;
   do not grant shell or arbitrary code execution.
5. Move long-running analysis to a queued job before larger profiles or
   concurrent production use.
