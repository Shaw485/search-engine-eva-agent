# ADR 004: Query-scoped stage-aware retrieval Agent

- Status: Accepted for the smoke implementation
- Date: 2026-08-28
- Scope: closed, fully judged 20-Query smoke profile only
- Owner boundary: experiment execution is automatic; strategy activation remains
  subject to explicit Owner approval

## Context

The earlier optimizer could tune one title-ranking family, but it could not
separate a retrieval miss from a fusion or ranking loss. A useful optimization
Agent must be able to answer four different questions:

1. Did any recall channel retrieve the relevant product?
2. Did fusion retain it?
3. Did the coarse ranker keep it in the final Top 10?
4. Did the candidate improve aggregate quality without hiding Query-level
   regressions?

Amazon ESCI does not provide complete relevance labels for arbitrary Queries
over all 1,814,924 products. Measuring “full-catalog recall” from those labels
would therefore be invalid. The implementation needs a smaller explicit
retrieval boundary before it can make stage-level claims.

## Decision

### 1. Fix an explicit, query-scoped evaluation boundary

Stage-aware experiments use `query-scoped-retrieval-smoke-v0`: 20 smoke
Queries, 416 judged Query-product pairs and each Query's fully judged candidate
pool. Out-of-scope products are excluded rather than treated as irrelevant.
The allowed metrics are judged Recall@5/10, nDCG@10 and MRR@10.

This boundary supports controlled stage comparisons. It does not support claims
about Amazon production search, arbitrary full-catalog Recall or generalization
beyond smoke.

The 500-Query dev profile and the 8,956-Query frozen test remain locked. This
slice does not change their access policy.

### 2. Make recall channels explicit and label-blind

The baseline runs two channels:

- title BM25 recall;
- exact-title recall.

The candidate adds multi-field BM25 over title, brand, bullet point and description.
Each channel returns an independently inspectable ranked list. Labels are used
only after ranking by the evaluation Harness.

### 3. Fuse channels with versioned RRF

Reciprocal Rank Fusion combines channel ranks without comparing incomparable raw
scores. `rrf_k=60` is fixed in the versioned pipeline config. Three bounded
candidate experiments are run:

- uniform RRF;
- conservative weighted RRF: title `1.0`, exact title `1.0`, multi-field `0.1`;
- aggressive weighted RRF: title `1.0`, exact title `0.5`, multi-field `0.25`.

These weights are Codex-designed engineering candidates, not Owner-originated
product policy. The experiment records the exact configuration and content
identities required for revalidation.

### 4. Keep an independent coarse-ranking stage

RRF produces a fused Top 20. A cheap title-BM25 coarse ranker then reranks that
set and emits Top 10. Fine ranking and final reranking are explicit
`not_implemented` stages; the system must not present the coarse ranker as a
Cross-Encoder or business reranker.

The pipeline is therefore:

```text
query-scoped judged pool
        ↓
title BM25 | exact title | optional multi-field BM25
        ↓
RRF fusion Top 20
        ↓
title-BM25 coarse rank Top 10
        ↓
Search Evaluation Harness
```

### 5. Validate evidence before diagnosis or comparison

The validator rebuilds each label-blind document snapshot and canonical
allowlisted pipeline, then reconstructs channel output, RRF, coarse ranking,
per-Query metrics, aggregate metrics, stage lineage and channel contribution.
Content hashes alone detect conflicting bytes; they do not replace semantic
recomputation. Diagnosis and comparison reject Runs whose identity, policy,
boundary, pipeline configuration, rankings, lineage or metrics do not match the
reconstructed result.

### 6. Gate both retrieval gain and downstream safety

A candidate must pass all 12 `closed-retrieval-experiment-gates-v1` checks:

- multi-field contributes at least one uniquely recovered relevant item;
- recall-union coverage improves;
- fusion Recall@10, nDCG@10 and MRR@10 do not decrease;
- coarse Recall@10, nDCG@10 and MRR@10 do not decrease;
- worst fusion and coarse Query nDCG@10 deltas are each at least `-0.02`;
- fusion and coarse regression rates are each at most `0.10`.

The smoke thresholds are engineering defaults proposed and implemented by
Codex. They are not yet adopted as production policy and do not prove
statistical significance.

### 7. Separate experiment evidence from activation authority

The Agent can run the baseline and three bounded candidates, diagnose stage
losses, compare them, select a gate-passing experiment and return
`proposal_ready`. It persists immutable retrieval Runs, diagnoses and
comparisons. It does **not** create a strategy decision, update the strategy
catalog, mutate the active strategy, alter `/catalog/search` or deploy code.

The Owner decided that the product should autonomously diagnose, experiment and
run the Harness, then ask a human to approve or reject. Accordingly, approval
remains a separate authority boundary. The implementation's strategy family,
weights, pool definition and 12 gate thresholds were proposed and implemented
by Codex after the Owner said to execute that direction.

## Consequences

### Benefits

- Stage loss is observable instead of being collapsed into one final score.
- Multi-field recall can be credited for unique relevant coverage without
  allowing that gain to hide fusion or coarse-rank regressions.
- All evidence shown to the workbench is reproducible from the fixed pool.
- Failed candidates remain useful evidence and can guide the next bounded
  ablation.

### Costs and limitations

- Query-scoped pools are not a realistic global retrieval competition.
- Twenty Queries are enough for an engineering smoke check, not a release
  quality claim or confidence interval.
- The coarse ranker is lexical and title-only; fine ranking and final reranking
  are still absent.
- Candidate selection is deterministic orchestration, not yet a single
  Runtime/Trace execution with a model Planner.
- Semantic revalidation proves internal reproducibility, not who produced a
  Run. Source authenticity still depends on the pinned manifest, controlled Run
  store and trusted execution boundary; a bare SHA-256 ID is not a signature.
- A gate pass only means “eligible for Owner review inside this smoke policy.”
  It does not mean approved, active, deployed or production-safe.

## Rejected alternatives

- **Evaluate against the complete 1.8M catalog with ESCI labels:** rejected
  because missing labels would be misclassified as irrelevance.
- **Accept any union-coverage increase:** rejected because fusion or ranking may
  discard the recovered products or regress important Queries.
- **Let a model choose the winner from prose:** rejected because metrics and
  gates must be deterministic and reproducible.
- **Activate the selected candidate automatically:** rejected because smoke
  evidence is too weak and the Owner approval boundary is a product
  requirement.

## Evidence

- Fixed profile: `configs/evaluation/query-scoped-retrieval-smoke-v0.json`
- Smoke report: `docs/STAGE_AWARE_RETRIEVAL_REPORT.md`
- Pipeline: `src/search_quality/retrieval/pipeline.py`
- Revalidation: `src/search_quality/evaluation/retrieval_validation.py`
- Comparison and gates:
  `src/search_quality/evaluation/retrieval_comparison.py`
- Orchestration: `src/search_quality/agent/retrieval_analysis.py`
