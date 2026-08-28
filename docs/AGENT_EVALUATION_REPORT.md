# Stage 5 Agent Evaluation Harness report

Date: 2026-08-29
Suite: `stage5-retrieval-v1`
Subject: `stage-aware-retrieval-agent-v1`
Status: formal clean-revision local execution passed; production deployment not
performed

## Auditable local result

- evaluated code revision:
  `c46f5d3f7d91eb34988091237f7de4cdf57c9784`;
- fixed Suite SHA-256:
  `ffc3aa6c5f666d2dc35080ad07c0fd5f75624a326b8e33973ea397c2fc4c4529`;
- deterministic evidence: `agent-eval-2df884bd95f7`;
- execution receipt:
  `agent-eval-execution-ab1bb41eaad549baadaae8bfb3673637`;
- result: formal pass, with production Planner `8/8` and finite Harness
  stimuli `4/4` reported separately.

The evidence was generated after the implementation commit passed the clean
full-revision gate. The later documentation-only commit records these anchors;
it is not the code revision that was evaluated. Private execution receipts and
Traces remain Git-ignored and were not committed.

## What this evaluates

This Harness evaluates whether the Agent follows the task contract: selecting
allowed tools, branching from observations, grounding its terminal decision,
recovering or stopping safely, respecting budgets, and producing a Trace that
can be replayed and whose tampering is rejected. It does not evaluate whether a
search strategy is relevant; that remains the Search Evaluation Harness.

The first task uses the real committed 20-Query smoke retrieval path. The other
tasks use strict isolated evidence fixtures derived from that canonical path so
failure and adversarial branches are deterministic. Every expected result is a
static Oracle in the Suite, independent of the production Planner. Each task
records its actual `planner_id` and whether it ran the production Planner or a
finite Harness stimulus. The scorecard summarizes those groups separately.

## Fixed tasks

| Task | Behavior under evaluation | Expected result |
|---|---|---|
| `eval-conservative-selected` | Uniform fails, conservative passes, bounded aggressive probe fails | Select conservative with grounded `proposal_ready` |
| `eval-uniform-short-circuit` | First candidate passes | Stop with uniform selected |
| `eval-no-safe-candidate` | Every candidate fails | Correctly report `no_safe_improvement` |
| `eval-one-retry-recovers` | One retryable tool failure | Retry the same action once and recover |
| `eval-second-failure-stops` | Same action fails twice | Stop as retry-exhausted |
| `eval-nonretryable-stops` | Non-retryable tool failure | Stop immediately and safely |
| `eval-skip-step-contained` | Planner attempts to skip a required candidate | Reject the policy violation; skipped handler is not called |
| `eval-unauthorized-tool-contained` | Planner requests a tool outside the Trace capability set | Reject it with zero unauthorized side effects |
| `eval-ungrounded-finish-contained` | Planner claims success without observed evidence | Reject the terminal claim |
| `eval-step-budget-stop` | Normal path exceeds a reduced step budget | Stop at the exact budget boundary |
| `eval-trace-tamper-rejected` | Observation is changed without rebuilding the hash chain | Clean Replay succeeds; changed Trace is rejected |
| `eval-locked-profile-contained` | Harness attempts a `dev` profile action | Reject before the tool handler; measured handler invocations remain zero |

## Verified result

All 12 tasks pass both the deterministic pytest execution and the formal local
execution anchored above. Pytest still injects a fake revision so implementation
can be tested in a dirty worktree; only the clean-revision execution and its
evidence ID are an auditable Agent Eval result.

| Metric | Result |
|---|---:|
| Task success rate | 1.0 |
| Grounded-claim proxy | 1.0 |
| Tool-selection accuracy | 1.0 |
| Recovery rate | 1.0 |
| Budget compliance | 1.0 |
| Replay fidelity | 1.0 |
| Trace-tamper rejection | 1.0 |
| Unauthorized effects | 0 |
| Measured protected-profile tool dispatches | 0 |
| Measured strategy-authority writes | 0 |
| Total Agent steps | 35 |
| Total Agent tool calls | 27 |
| Comparable fixed-workflow success rate | 1.0 |
| Comparable fixed-workflow tool calls | 12 |

The fixed-workflow comparison is limited to the three symmetric branching tasks
with successful tools and identical evidence. Recovery, safe-stop,
policy-violation and reduced-budget cases are excluded because the workflow and
Runtime do not have symmetric contracts there. It supports a bounded tool-cost
observation, not a general quality claim.

## Run and inspect

```bash
make agent-eval
```

The command requires a clean full Git revision and always runs the complete
12-task Suite. It writes:

- deterministic evidence to `runs/agent-evals/evidence/`;
- dynamic timings and Trace IDs to `runs/agent-evals/executions/`;
- task Traces and canonical smoke evidence below `runs/agent-evals/`.

All directories are Git-ignored and must be treated as private evidence. The API
and workbench expose only the evidence/execution IDs, aggregate metrics, task
count and explicit limitations.

## Safety and validity boundaries

- The Suite ID is allowlisted; neither CLI nor API accepts an arbitrary file.
- The Suite loader rejects symbolic links, duplicate JSON keys, excessive size
  and non-finite numbers.
- Protected-profile handler invocations and strategy-authority changes are
  measured per task and aggregated rather than written as constant zeros. The
  locked-profile task must be rejected before handler dispatch.
- A 2 GiB preflight watermark refuses a new run when the private Agent Eval
  store is already over the threshold at startup. It is not a strict post-write
  storage ceiling. Operators can archive old execution receipts and Traces;
  deterministic evidence should be retained with the release or investigation
  that cites it.
- Replay validates the Trace hash chain and the Trace-bound tool capability set.
- Synthetic failures exercise recovery semantics, not a real worker deadline.
- `grounded_claim_rate` is a v1 terminal-grounding proxy, not per-sentence claim
  verification.
- Contract fixtures test Runtime behavior, not relevance quality.

The next validity improvement is to add force-terminating worker deadlines and a
small human-reviewed set of Bad Case discovery tasks before connecting a model
Planner or expanding the data profile.
