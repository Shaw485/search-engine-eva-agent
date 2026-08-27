# Stage 2 comparator and Run-Diff smoke evidence

## Status

This guarded Search Evaluation Harness run verifies the shared Ranker contract,
metric policy, ranking completeness, determinism and data boundary on the fixed
20-Query smoke profile. It is not the formal 500-Query dev comparison, and the
results are not a production Ranker decision. The strict `compare_runs` path now
adds compatible-Run validation, metric recomputation, aggregate deltas and a
complete per-Query/per-product ranking Diff.

## Shared identity

- Code revision: `8df54b882c68df5955298be46d0bb27a36cec051`
- Data schema: `esci-stage1-v1`
- Data: 20 dev-derived smoke Queries, 416 judgments, 416 unique US products
- Evaluation task: complete judged-candidate reranking
- Relevance policy: `esci-primary-v1`
- Tie break: `(product_locale, product_id)` ascending

| Comparator | Run ID | Defining configuration |
|---|---|---|
| Deterministic random | `random-4aefec7cb33d` | seed `17`; SHA-256 query/product score |
| Title keyword overlap | `overlap-229d3cbb4f5c` | count unique exact Query/title token overlap |
| Title BM25 | `bm25-b7f0f10680e7` | `k1=1.5`, `b=0.75`; IDF within each judged candidate set |

Running `make eval-baseline` at this clean revision produced these three Run
IDs; repeated-run determinism is covered by the automated Harness tests.

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

## Run comparisons

Both comparisons were produced at the same clean code revision. Delta always
means candidate minus baseline.

| Comparison | Baseline → Candidate | nDCG@5 | nDCG@10 | MRR@10 | Success@1 | Success@5 |
|---|---|---:|---:|---:|---:|---:|
| `comparison-5c59968c1cd7` | random → BM25 | +0.211858 | +0.173312 | +0.047500 | +0.050000 | 0.000000 |
| `comparison-dc727a4e03ca` | keyword overlap → BM25 | -0.013605 | +0.008561 | -0.025000 | -0.050000 | 0.000000 |

For random → BM25, nDCG@10 improved on 15 Queries and regressed on 5. Query
`15281` (`bey blades`) is the largest regression: nDCG@10 moved from `0.710493`
to `0.387501`, a delta of `-0.322992`, despite the positive aggregate result.
This is the concrete reason the Harness preserves every Query and product move
instead of returning only one average.

The overlap → BM25 comparison demonstrates a metric trade-off rather than a
single winner: BM25 is slightly higher on nDCG@10, while overlap is higher on
nDCG@5, MRR@10 and Success@1. Success@5 is tied at `1.0` for every comparator,
so it supplies no discrimination on this small profile.

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
- Run comparison accepts only direct artifacts/pointers in the operator-trusted
  local `runs/` store, rejects traversal/symlinks/name mismatches, verifies the
  trusted Stage 1 data identity and recomputes all Query/aggregate metrics.
- Trace IDs, timestamps and durations exist only in structured diagnostics;
  they cannot change deterministic Run identity.

## Boundary

These Runs compare deliberately simple quality references, not Amazon's search
engine and not the portfolio's Stage 0 full-product demo. They do not establish
full-catalog Recall because ESCI does not judge every Amazon product for each
Query.

Run IDs are unkeyed content hashes. They detect content changes and collisions,
but are not signatures proving that a declared Ranker generated a ranking. This
Stage 2 CLI assumes a trusted local operator; the Stage 3 Agent must accept only
validated Run IDs from a controlled registry, never arbitrary paths.

## Next gate

Before the 500-Query dev comparison, the Owner must answer the Stage 1 data
boundary checkpoint in their own words and complete the Stage 2 metric exercise.
The next implementation step after those answers is to unlock the fixed dev
profile, repeat the same three Runs and Run comparisons, and inspect selected
Bad Cases before entering the minimal Agent. Frozen test remains closed.
