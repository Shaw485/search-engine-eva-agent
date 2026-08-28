# Stage-aware retrieval smoke report

> Date: 2026-08-28
> Status: local smoke evidence; eligible for Owner review, not approved or
> deployed
> Boundary: 20 Queries / 416 fully judged Query-product pairs in
> `query-scoped-retrieval-smoke-v0`

## Question

Can a third, multi-field lexical recall channel recover relevant products that
the title-only channels miss, while RRF and the coarse ranker preserve final
quality?

This report does not evaluate full-catalog Recall. It compares pipelines inside
each Query's fully judged ESCI candidate pool. The 500-Query dev profile and
frozen test were not opened.

## Pipelines

The baseline uses title BM25 plus exact-title recall, uniform RRF and a
title-BM25 coarse ranker. Each candidate adds multi-field BM25 over title,
brand, bullet point and description. All candidates use the same data, relevance
policy and stage cutoffs.

| Experiment | RRF weights: title / exact / multi-field | Gate result |
|---|---:|---|
| Baseline | `1.0 / 1.0 / —` | Reference |
| Uniform candidate | `1.0 / 1.0 / 1.0` | Failed |
| Conservative candidate | `1.0 / 1.0 / 0.1` | **Passed all 12 smoke gates** |
| Aggressive candidate | `1.0 / 0.5 / 0.25` | Failed |

## Selected experiment

The conservative candidate expanded mean judged recall-union coverage from
`0.8114716964` to `0.8487412085`, an absolute increase of `0.0372695121`
(`+3.73` percentage points). Multi-field recall contributed relevant items that
were absent from the baseline union.

| Stage / metric | Baseline | Conservative | Delta |
|---|---:|---:|---:|
| Recall union coverage | 0.8114716964 | 0.8487412085 | +0.0372695121 |
| Fusion judged Recall@10 | 0.5279689273 | 0.5296930653 | +0.0017241380 |
| Fusion nDCG@10 | 0.6711220109 | 0.6743785643 | +0.0032565534 |
| Fusion MRR@10 | 0.8250000000 | 0.8250000000 | 0.0000000000 |
| Coarse judged Recall@10 | 0.5296930653 | 0.5296930653 | 0.0000000000 |
| Coarse nDCG@10 | 0.6838156484 | 0.6841070029 | +0.0002913545 |
| Coarse MRR@10 | 0.8500000000 | 0.8500000000 | 0.0000000000 |

The worst fusion Query nDCG@10 delta was `-0.004531`, within the engineering
floor of `-0.02`. All 12 checks passed. This is a deliberately conservative
trade-off: it captures more judged relevant products while giving the new
channel only a small influence over fusion.

## Why the other candidates failed

The uniform candidate achieved the same higher recall-union coverage, but the
new channel was strong enough to displace useful baseline results during fusion
and coarse ranking. It failed seven of the 12 downstream quality and
Query-regression gates.

The aggressive weighted candidate improved some aggregate ranking measurements
more than the conservative candidate, but fusion MRR declined and 6 of 20
Queries regressed at fusion. It failed the fusion MRR floor and the fusion
regression-rate ceiling. Higher average nDCG was therefore not enough to make it
eligible.

## Interpretation

The experiment supports a narrow, falsifiable conclusion:

> Inside the fixed, fully judged 20-Query pool, multi-field BM25 finds additional
> relevant products. Conservative RRF weighting preserves the measured fusion
> and coarse-rank floors better than uniform or aggressive weighting.

It does **not** prove that this weighting is optimal, statistically significant,
better over all Amazon products or ready for production. The selected result is
`proposal_ready`: it may enter Owner review, but the analysis endpoint does not
write a decision, strategy catalog entry or active search configuration.

## Gate policy

`closed-retrieval-experiment-gates-v1` checks unique contribution, union
coverage, non-decreasing fusion/coarse Recall@10, nDCG@10 and MRR@10, worst
Query nDCG floors and regression-rate ceilings. The thresholds are Codex
engineering defaults for this smoke slice. The Owner has not independently
selected or adopted them as production policy.

## Reproducibility and attribution

Run, diagnosis and comparison artifacts are content-addressed and semantically
revalidated before comparison. Exact IDs should be cited from the generated
analysis response for the clean code revision that is under review; this report
does not invent IDs from a dirty development tree.

### Clean-revision evidence

The analysis was rerun from clean revision
`9be9eb31409317d719b018ac12520928e27a61f2` with status `proposal_ready`.

| Evidence | Content-addressed ID |
|---|---|
| Baseline pipeline | `pipeline-82f7693ed3c3` |
| Baseline retrieval Run | `retrieval-7df0aba74533` |
| Baseline diagnosis | `stage-diagnosis-a4e05a99335c` |
| Selected candidate pipeline | `pipeline-95a246096d53` |
| Selected candidate Run | `retrieval-eea85ed08937` |
| Selected candidate diagnosis | `stage-diagnosis-21e1ced991bf` |
| Baseline/candidate Comparison | `retrieval-comparison-93fb997f6d5c` |

The selected experiment is
`title-exact-multifield-weighted-v1`, using RRF channel weights
`1.0 / 1.0 / 0.1` for title BM25, exact title and multi-field BM25. These IDs
bind this report to reproducible artifacts for that revision; content addressing
detects mutation but is not an authentication or source-trust mechanism.

The Owner defined the required product behavior: one button should let the
Agent diagnose stages, run bounded experiments through the Harness and present
evidence for an approval decision, and then said “执行”. Codex proposed and implemented this
specific query-scoped boundary, channel set, RRF variants, coarse ranker,
candidate selection and 12 engineering gates.

## Next evidence needed

1. Owner reviews the strategy trade-off; a smoke gate pass is not approval.
2. Stage actions are integrated into the Runtime so one Trace records diagnosis,
   experiment selection, gate failure and fallback.
3. After the learning/data gate is explicitly satisfied, rerun on a larger
   locked labeled profile without touching frozen test.
4. Add fine ranking and final reranking as separate allowlisted stages, then
   evaluate their incremental effect with the same evidence discipline.
