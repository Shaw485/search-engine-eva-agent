# Agent optimization workflow

> Status: product direction adopted from Owner feedback on 2026-08-28. This
> document defines the target loop. Some pieces are already implemented as
> smoke-only scaffolding; strategy proposal, approval panel and automatic
> strategy update are future implementation milestones.

## Target product behavior

The Agent should not wait for a human to provide two Run IDs. The final product
should work like this:

```text
1. Agent searches or samples current results.
2. Agent finds bad cases and classifies likely causes.
3. Agent proposes one bounded strategy change.
4. Harness runs the candidate strategy on the same evaluation data.
5. Harness compares baseline vs candidate.
6. Agent summarizes evidence in an approval panel.
7. Human clicks Update Strategy or Reject Strategy.
8. If approved, the system writes a new versioned strategy config and runs
   follow-up validation automatically.
```

The human should make product decisions, not manually run every diagnostic step.

## Who does what

| Actor | Responsibility |
|---|---|
| Search strategy | Produces ranked products for a Query. |
| Search Evaluation Harness | Computes metrics and compares Runs. |
| Agent | Finds bad cases, proposes experiments, calls Harness, explains evidence. |
| Approval panel | Presents the proposed strategy, examples, metrics, regressions and action buttons. |
| Owner | Accepts or rejects product direction and strategy updates. |
| System after approval | Applies the approved strategy as a versioned config and validates it. |

## Approval panel shape

The panel should show one proposal at a time:

| Panel section | Content |
|---|---|
| Proposed strategy | Example: add model-number exact-match boost or tune title/brand weights. |
| Why this strategy | The bad-case pattern the Agent found. |
| Example before/after | Query, top results before, top results after, labels if available. |
| Aggregate effect | nDCG@10, MRR@10, Success@1/5 and latency deltas. |
| Local risks | Worst regressions and Query groups harmed by the candidate. |
| Evidence | Run IDs, comparison ID, Query IDs and Trace ID. |
| Decision | Update Strategy, Reject Strategy, or Ask Agent for another experiment. |

## Strategy update boundary

The Agent may automatically create candidate experiments inside an allowed
parameter space. It must not silently update the active strategy. The update
rule is:

```text
Agent proposes -> Harness compares -> panel summarizes -> human approves ->
system writes versioned strategy config -> validation run starts automatically
```

This keeps the product usable while preserving accountability. The Agent does
the repetitive work; the Owner controls the product tradeoff.

## Allowed first strategy space

The first version should use simple, explainable strategies before expensive
models:

| Strategy | Bad case it targets | Example |
|---|---|---|
| Multi-field BM25 weights | Important terms appear in brand/category/description, not title only | brand match should help `apple charger` |
| Exact token boost | Product IDs, model numbers and sizes should not be blurred | `iphone 15 pro case`, `usb-c 65w` |
| Token normalization | Hyphen, plural, casing and punctuation mismatch | `usb c` vs `usb-c` |
| Synonym expansion | Same intent uses different words | `wireless` vs `bluetooth` when evidence supports it |
| Hybrid retrieval | Lexical match misses semantically related products | `running shoes` vs athletic sneakers |

Each strategy must have a config version and a bounded parameter range so the
Agent can experiment without arbitrary code execution.

## First implementation milestone

Before the web panel, implement the command-line version:

```text
search-quality-agent propose-strategy
  -> finds worst smoke bad cases
  -> chooses one allowed strategy candidate
  -> runs baseline and candidate
  -> compares Runs
  -> writes a StrategyProposal artifact
```

The artifact should contain:

- proposal ID and strategy config version;
- baseline Run ID, candidate Run ID and comparison ID;
- bad-case Query examples;
- aggregate metric deltas;
- worst regressions;
- approval status: `pending`, `approved`, `rejected`, or `applied`;
- safe apply command that works only after approval.

## Interview-safe explanation

You can say:

> I changed the product direction from a passive Run comparison tool into an
> approval-gated optimization Agent. The Agent is expected to find bad cases,
> propose bounded strategy changes, run the Harness, compare evidence, and show
> an approval panel. Humans decide whether the tradeoff is acceptable; once
> approved, the system can automatically write a versioned strategy update and
> rerun validation.

