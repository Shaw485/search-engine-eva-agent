# Agent optimization strategy

> Status: adopted product direction; the first bounded, deterministic optimizer
> is implemented on the smoke profile. A separate stage-aware retrieval slice
> now evaluates multi-field recall, RRF fusion and coarse ranking. Neither slice
> is yet one Runtime Trace. This document separates what is executable now from
> the target system so the project does not overclaim.

## Outcome

The optimization Agent is an evidence-driven experiment loop:

```text
Query set and buckets
        ↓
Baseline Run → Bad Case mining → root-cause hypotheses
        ↓                         ↓
        └──────── bounded strategy candidates
                                  ↓
                    Search Evaluation Harness
                                  ↓
                    pairwise metrics and ranking Diff
                                  ↓
                       deterministic release gates
                                  ↓
                       pending human-reviewed proposal
                                  ↓
             current: server-only decision → next optimizer baseline
                                  ↓
       TARGET / NOT IMPLEMENTED: larger validation → serving activation / rollback
```

The model may propose hypotheses and explain evidence. It is never the source
of metric truth, approval authority, or production write access.

## Responsibilities

| Component | Owns | Must not own |
|---|---|---|
| Query constructor | Query provenance, split, buckets and label eligibility | Inventing trusted relevance labels |
| Search strategy | Producing ranked products from a versioned config | Reading ESCI labels while ranking |
| Search Evaluation Harness | Runs, metrics, pairwise Diff and reproducibility checks | Choosing a product trade-off |
| Optimization Agent | Bad Case selection, diagnosis, candidate selection and experiment sequencing | Bypassing policy or approving itself |
| Optional model provider | Bounded hypothesis generation and proposal wording | Computing metrics, code execution or activation |
| Owner | Accepting or rejecting the product trade-off | Manually running every experiment |

## Implemented stage-aware retrieval slice

The current retrieval analysis runs inside a deliberately closed boundary:
each of 20 smoke Queries is searched only against its fully judged ESCI product
pool. It must not be described as full-catalog Recall or production quality.

```text
title BM25 recall ─────┐
exact-title recall ────┼─> RRF Top 20 -> title-BM25 coarse Top 10 -> Harness
multi-field BM25 ──────┘
```

The baseline omits multi-field recall. The Agent then runs uniform,
conservative (`1.0 / 1.0 / 0.1`) and aggressive (`1.0 / 0.5 / 0.25`) RRF
experiments. The conservative candidate increased mean judged recall-union
coverage from `0.8114716964` to `0.8487412085`, preserved fusion/coarse metric
floors and passed all 12 retrieval gates. Uniform and aggressive variants are
retained as failed evidence, not hidden after selection.

This slice stops at `request_owner_review`. It persists only Runs, stage
diagnoses and comparisons; it does not write proposal decisions, add an active
catalog strategy, change `/catalog/search` or deploy. See
[the smoke report](STAGE_AWARE_RETRIEVAL_REPORT.md) and
[ADR 004](adr/004-stage-aware-retrieval-agent.md).

## Optimization loop

### 1. Construct and classify Query cases

Every Query records its source and whether it can be used for formal metrics:

| Source | Purpose | May enter nDCG/MRR? |
|---|---|---|
| ESCI or human-judged | Formal comparison | Yes, with the matching labels |
| Privacy-reviewed search log | Problem discovery and traffic weighting | Only after labels are attached |
| Catalog-derived | Coverage exploration | No; the source product is only a weak label |
| Rule/model synthetic | Robustness exploration | No; changing the Query invalidates old labels |

Initial buckets include brand/category, numeric or model number, multi-token
attribute, phrase, spelling/normalization, long-tail, semantic wording and
zero-result exploration.

### 2. Run the current baseline

The Run fixes dataset hash, Query set, relevance policy, code revision, strategy
config, random seed and evaluation boundary. Dynamic latency stays in
diagnostics rather than the content-addressed quality Run.

The first round uses title BM25. After an approval, the optimizer reads the
versioned active exact-boost config and uses that Run as the next round's
baseline. A candidate identical to the active config is skipped. This makes the
loop compare `current active → next candidate` instead of repeatedly comparing
every proposal with the original BM25.

### 3. Mine representative Bad Cases

The implemented v2 scans every fixed smoke Query with deterministic diagnosis
rules and chooses the three lowest-baseline-nDCG cases for the summary. It does
not yet use traffic weighting, bucket quotas or the following target severity
formula.

The planned first transparent severity formula is:

```text
badness =
  0.45 × (1 - nDCG@10)
+ 0.25 × (1 - MRR@10)
+ 0.20 × [Success@1 = 0]
+ 0.10 × [zero results]
```

When privacy-reviewed frequency exists, priority may multiply badness by
`log(1 + frequency)`. Without traffic data, frequency is exactly `1`; the report
must not imply production traffic impact. Selection uses per-bucket quotas so
one repeated failure type cannot occupy the whole review set.

### 4. Diagnose a falsifiable cause

The first deterministic rules inspect evidence rather than free-form text:

- numeric/model token appears in a relevant title but not in the top result;
- a relevant result covers more Query tokens than the top result;
- an exact Query phrase exists in a relevant result but is displaced;
- a relevant result has no title-token overlap, suggesting the current title
  strategy cannot solve the case;
- candidate-set ranking improved or regressed after one isolated operator.

Each cause includes Query/Run evidence and the experiment that could falsify
it. A model-generated cause without valid evidence references is rejected.

### 5. Generate bounded candidates

The Agent selects from implemented, allowlisted strategy operators. The first
executable search space is title BM25 with bounded coverage, numeric-token and
phrase boosts. Candidate values are validated by the Harness and arbitrary
code or unknown parameters are rejected.

Later allowlisted families may include token normalization, spelling
correction, multi-field weights, lexical/vector recall, RRF fusion, coarse
ranking, learned reranking and final business rules. A requested family that
has no implementation becomes `requires_engineering`; it cannot be activated
from natural language.

The MVP uses a small deterministic grid. As the space grows, successive
halving or Bayesian optimization may reduce experiment cost, but neither
replaces the Harness.

The query-scoped stage slice has now implemented the first bounded multi-field
recall/RRF/coarse-rank family. Its three weight sets are Codex-proposed
engineering candidates. The Owner decided that the product should autonomously
diagnose, experiment and request approval, but has not independently selected
these weights as product policy.

### 6. Compare every candidate with the same baseline

Every comparison uses the same Query set, labels, product snapshot and policy.
The panel reports aggregate nDCG/MRR/Success, per-Query changes, the worst
regressions, representative improvements and the exact strategy config.

For larger labeled sets, paired bootstrap confidence intervals will be added.
The current 20-Query smoke evidence is directional and cannot establish a
production-quality improvement.

### 7. Apply deterministic gates

An average improvement alone is insufficient. A candidate must be checked for:

- minimum aggregate nDCG@10 improvement;
- nDCG@5 not falling below its allowed floor;
- MRR@10, Success@1 and Success@5 not decreasing;
- maximum regression count/rate;
- maximum single-Query regression;
- protected Query-bucket regressions;
- repeatability, latency and cost limits when those measurements exist.

The gate policy is versioned evidence. Approval ignores thresholds supplied by
the proposal and recomputes the selected evaluation with the service's trusted
`smoke-release-gates-v2` policy. Thresholds shown by the implementation are
engineering defaults until the Owner explicitly adopts them as product policy.
A candidate that fails a hard gate can still be shown as an experiment, but it
cannot be presented as safe to activate.

Among candidates that pass all seven smoke gates, a transparent relative
`selection_score` orders experiments using metric gains, regression rate and
worst-regression magnitude. Its zero point has no accept/reject meaning; only
the trusted gates determine eligibility.

Stage-aware retrieval uses a separate 12-check policy,
`closed-retrieval-experiment-gates-v1`: unique multi-field contribution and
union-coverage gain; non-decreasing fusion/coarse Recall@10, nDCG@10 and MRR@10;
worst-Query nDCG floors of `-0.02`; and fusion/coarse regression-rate ceilings
of `0.10`. These are also Codex engineering defaults, not Owner-approved
production thresholds.

### 8. Produce one reviewable proposal

The proposal contains diagnosis counts, candidate table, selection rule,
winning config, metric Diff, before/after examples, local risks, all evidence
IDs, evaluation boundary and model-usage metadata. The Owner reviews the exact
`strategy_id + config hash + comparison_id`, not only a prose description.

### 9. Approve, validate, activate or roll back

The target lifecycle is:

```text
draft → evaluated → gate_passed → approved_pending_validation
      → validated → active → superseded / rolled_back
```

Approval must revalidate the immutable proposal and Run/Comparison evidence,
check that the active parent strategy has not changed, run follow-up validation
and atomically activate only on success. New algorithm code still requires the
normal code-review and deployment path.

The implemented smoke slice reloads the stored Runs, rebuilds the Comparison,
recomputes the selected evaluation with the trusted gate policy, compares a
SHA-256 revision of the complete active entry, and requires the baseline Run,
candidate Run and comparator to match the currently deployed Git revision. It
also checks the candidate's complete canonical Ranker config rather than only
its three boost values. Decisions are serialized with a cross-process file lock
before updating the versioned strategy catalog. The next optimization round
consumes that active config as its baseline.

This is not yet the complete target lifecycle: there is no larger-set
post-approval validation or automatic rollback, and the full-catalog
`/catalog/search` endpoint does not consume the active strategy. Public browser
approval also remains intentionally disabled until authenticated Owner session,
CSRF protection and audit identity are implemented.

`active.json` is the activation authority. `catalog.json` is written first, so
a process crash between those two atomic writes may leave a catalog entry that
is listed but not active; it does not activate an unverified strategy. Retrying
the same serialized decision repairs the catalog/active pair. A future
immutable strategy revision plus one atomic active pointer will remove this
recovery case. Decision-lock waiting is capped at five seconds.

## Model-provider boundary

Codex/ChatGPT session quota and platform credentials cannot be transferred to
the repository. A deployable model must use an Owner-controlled, limited
server-side credential or a local model:

```text
Agent worker → server-side model adapter → provider
```

The credential never enters Git, browser JavaScript, prompts, Trace artifacts
or logs. Recommended initial limits, still requiring Owner approval, are three
model calls per Agent Run, concurrency one, a 60-second hard Run deadline, a
1,500-token output cap, a per-Run cost cap and a provider-side daily spend cap.

Provider output is a strict strategy DSL containing only allowlisted family,
bounded parameters, target buckets, hypothesis and evidence references. The
deterministic fallback remains available when the provider is missing, times
out, exceeds budget or returns invalid output.

Before any model provider is connected, the trusted service must reconstruct
the diagnosis-derived candidate selection, rerun every candidate evaluation and
recompute `select_winner()` from Harness evidence. The current deterministic
generator is inside the trusted service and approval revalidates the selected
winner; that narrower boundary must not be reused for untrusted model output.

## Tool inventory

| Tool | Status | Purpose |
|---|---|---|
| `run_ranker` | Implemented, smoke-only | Create a trusted quality Run |
| `evaluate_run` | Implemented, smoke-only | Read aggregate or Query metrics |
| `inspect_query` | Implemented, smoke-only | Inspect ranked evidence |
| `compare_runs` | Implemented, smoke-only | Produce pairwise metric and ranking Diff |
| Query constructor and bucketizer | Next | Build labeled selections and unlabeled exploration sets without mixing them |
| Bad Case miner and diagnosis | Partial deterministic slice | Scan every smoke Query and attach title-signal causes; severity scoring, traffic weights and bucket quotas are next |
| Strategy registry/search | First exact-boost slice implemented | Instantiate bounded candidates and select an experiment |
| Release-gate evaluator | First smoke slice implemented | Apply versioned aggregate and regression rules |
| Stage diagnosis | Implemented, query-scoped smoke | Attribute judged relevant loss to recall, RRF fusion or coarse ranking after strict Run revalidation |
| Multi-route retrieval + RRF ablation | Implemented, query-scoped smoke | Run title/exact/multi-field channels and compare uniform, conservative and aggressive fusion weights |
| Retrieval comparison gates | Implemented, query-scoped smoke | Recompute evidence and apply 12 coverage, quality and Query-regression checks |
| Model adapter | Designed, not connected | Optional untrusted hypothesis planner with cost controls |
| Approval/validation/rollback controller | Partial smoke slice | Server-only evidence revalidation, trusted gates, parent revision check and serialized catalog activation are implemented; larger-set validation and rollback are next |
| Research watcher | Planned, read-only | Turn new search research into experiment hypotheses, never into direct updates |

## Initial budgets and terminal states

The target runtime stops on one of these explicit outcomes:

- `proposal_ready`: at least one candidate passed every active gate;
- `no_safe_improvement`: candidates ran, but none passed;
- `insufficient_evidence`: labels or sample size cannot support the claim;
- `requires_engineering`: the hypothesis needs a new algorithm implementation;
- `requires_owner_decision`: a data or product boundary must be changed;
- `budget_exhausted`: experiment/tool/model budget ended;
- `tool_failure`: bounded retries were exhausted.

The first full Runtime target allows at most three optimization rounds, four
candidates per round and 30 tool calls. Repeating an identical strategy/config
hash is forbidden. These are technical safeguards, not a promise that smoke
evidence is sufficient for launch.

The current stage-aware orchestrator always runs exactly three allowlisted RRF
candidates. It is deterministic orchestration outside the Runtime budget/Trace
loop; moving those actions into the Runtime is the immediate integration step.

## What proves the Agent

The completed Agent is demonstrated when evidence changes its next action: a
numeric failure selects numeric-boost experiments, an unaddressable title gap
requests another strategy family, a regression causes inspection or rejection,
and a failed tool causes a bounded fallback. A fixed script that always runs the
same candidate is an optimizer scaffold, not the completed Agent.
