# Stage 2 BM25 smoke baseline

## Status

This is the first guarded Search Evaluation Harness run. It verifies the metric,
relevance-policy, ranking-completeness and data-boundary contracts on the fixed
20-Query smoke profile. It is not yet the formal 500-Query dev baseline.

## Run identity

- Run ID: `bm25-0d2d105b5e78`
- Code revision: `96123ab3ce18f34958bc758e6df6e2a06e042bc8`
- Data schema: `esci-stage1-v1`
- Data: 20 dev Queries, 416 judgments, 416 unique US products
- Relevance policy: `esci-primary-v1`
- Ranker: `candidate-title-bm25-v1`
- Field: `product_title`
- IDF scope: each Query's complete judged candidate set
- BM25: `k1=1.5`, `b=0.75`
- Tie break: `(product_locale, product_id)` ascending

Running `make eval-baseline` twice at the same clean revision produced the same
Run ID and metrics.

## Aggregate metrics

| Metric | Result |
|---|---:|
| nDCG@5 | 0.659606 |
| nDCG@10 | 0.719098 |
| MRR@10 | 0.851667 |
| Success@1 | 0.750000 |
| Success@5 | 1.000000 |

Success@5 is saturated on this small candidate-set profile, so it cannot by
itself distinguish ranking quality. nDCG remains the primary quality signal
because it rewards the order of E/S/C/I labels throughout the result list.

## Lowest nDCG@10 examples

| Query ID | Query | nDCG@10 |
|---:|---|---:|
| 561 | `07 nissan pathfinder window regulator without motor` | 0.316986 |
| 15281 | `bey blades` | 0.387501 |
| 9367 | `ant kind` | 0.447517 |

These are diagnosis candidates, not yet conclusions about the cause. The future
Agent must inspect the products and ranking evidence before assigning a Bad Case
category.

## Guardrails verified

- nDCG ideal ranking uses the complete judged candidate set, not returned Top-K.
- Every candidate is returned exactly once, including zero-score products.
- Product identity is `(product_locale, product_id)`.
- Input order does not change rankings or metrics.
- Query metrics are macro-averaged; a Query with no relevant item still counts.
- Only Manifest-pinned smoke/dev files derived from official train are accepted.
- Relabelling an official-test row as dev is rejected through `origin_split`.
- Formal Runs require a clean Git worktree and create immutable Run-ID files.

## Boundary

This baseline reranks each Query's judged candidates and computes BM25 statistics
inside that candidate set. It is deliberately named `candidate-title-bm25-v1`.
It is separate from the exploratory full-corpus search demo and must not be
described as Amazon's engine or as full-catalog retrieval quality.

## Next gate

Complete the Owner checkpoint on Query-level leakage, then run the 500-Query dev
profile and add random plus keyword-overlap comparators before declaring a
formal baseline.
