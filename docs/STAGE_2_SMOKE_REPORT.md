# Stage 2 three-comparator smoke evidence

## Status

This guarded Search Evaluation Harness run verifies the shared Ranker contract,
metric policy, ranking completeness, determinism and data boundary on the fixed
20-Query smoke profile. It is not the formal 500-Query dev comparison, and the
three Run files are not yet a `compare_runs` report.

## Shared identity

- Code revision: `22877b08fc433a0b7f083fb60d4e2e1ba4054793`
- Data schema: `esci-stage1-v1`
- Data: 20 dev-derived smoke Queries, 416 judgments, 416 unique US products
- Evaluation task: complete judged-candidate reranking
- Relevance policy: `esci-primary-v1`
- Tie break: `(product_locale, product_id)` ascending

| Comparator | Run ID | Defining configuration |
|---|---|---|
| Deterministic random | `random-862694f9c87e` | seed `17`; SHA-256 query/product score |
| Title keyword overlap | `overlap-7ced66a13013` | count unique exact Query/title token overlap |
| Title BM25 | `bm25-9dceb197b199` | `k1=1.5`, `b=0.75`; IDF within each judged candidate set |

Running `make eval-baseline` twice at this clean revision produced the same
three Run IDs and metrics.

## Aggregate metrics

| Comparator | nDCG@5 | nDCG@10 | MRR@10 | Success@1 | Success@5 |
|---|---:|---:|---:|---:|---:|
| Deterministic random | 0.447749 | 0.545786 | 0.804167 | 0.700000 | 1.000000 |
| Title keyword overlap | 0.673211 | 0.710537 | **0.876667** | **0.800000** | 1.000000 |
| Title BM25 | 0.659606 | **0.719098** | 0.851667 | 0.750000 | 1.000000 |

BM25 leads nDCG@10 by 0.008561 over keyword overlap, while keyword overlap
leads nDCG@5, MRR@10 and Success@1. Success@5 is saturated for all three and
cannot distinguish them on this profile.

This is not enough evidence to choose the better production strategy. The
profile has only 20 Queries, the evaluation reranks judged candidates rather
than retrieving from the full catalog, and different metrics reward different
parts of the order. A decision requires the locked 500-Query dev set plus
per-Query ranking differences and Bad Case inspection.

## Guardrails verified

- Every Ranker sees only Query text and label-free candidate products; ESCI
  labels remain inside the Harness for scoring.
- Every candidate must be returned exactly once with contiguous ranks and
  finite scores; malformed Ranker output is rejected.
- nDCG ideal ranking uses the complete judged candidate set, not returned Top-K.
- Product identity is `(product_locale, product_id)` and input row order does
  not change results.
- Query metrics are macro-averaged; a Query with no relevant item still counts.
- Shared formal evaluation code rejects 500-Query dev before opening its file
  until the Owner data-boundary checkpoint is recorded.
- Frozen official-test rows remain outside routine profiles and relabelling one
  as dev is rejected through `origin_split`.
- Formal Runs require a clean Git worktree, write immutable Run-ID files
  atomically and keep a separate latest pointer per profile and Ranker.
- Trace IDs, timestamps and durations exist only in structured diagnostics;
  they cannot change deterministic Run identity.

## Boundary

These Runs compare deliberately simple quality references, not Amazon's search
engine and not the portfolio's Stage 0 full-product demo. They do not establish
full-catalog Recall because ESCI does not judge every Amazon product for each
Query.

## Next gate

Before the 500-Query dev comparison, the Owner must answer the Stage 1 data
boundary checkpoint in their own words and complete the Stage 2 metric exercise.
The next Harness increment is `compare_runs`: compatible-Run checks, aggregate
deltas and per-Query ranking Diff. Frozen test remains closed.
