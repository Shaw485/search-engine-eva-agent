# Stage 1 ESCI data report

- Schema: `esci-stage1-v1`
- Source commit: `7916cdf6ab75a462e77f20ab40428a10923998d5`
- Slice: English-US, Task 1 reduced set
- Grain: one row per judged Query-product pair

## Split overview

| Asset | Formal split | Queries | Rows | Products |
|---|---|---:|---:|---:|
| train | train | 20388 | 409592 | 344452 |
| dev | dev | 500 | 10061 | 9992 |
| test | test | 8956 | 181701 | 164900 |
| smoke | dev profile | 20 | 416 | 416 |

## Data quality

- Duplicate judgments removed: 0
- Empty-title judgments quarantined: 0
- Queries missing a source category: 0
- Repeated build matched previous logical and file hashes: True.
- Formal split leakage: checked by Query ID and normalized Query text.
- Product join key: `(product_locale, product_id)`.

## Label distribution

| Split | E | S | C | I |
|---|---:|---:|---:|---:|
| train | 177291 | 144240 | 18637 | 69424 |
| dev | 4528 | 3388 | 453 | 1692 |
| test | 79708 | 63563 | 8099 | 30331 |

## Shape and completeness

| Split | Candidates p50 / p95 / max | Query tokens p50 / p95 | Empty description | Empty bullet | Empty brand | Empty color |
|---|---:|---:|---:|---:|---:|---:|
| train | 16 / 40 / 188 | 4 / 7 | 49.9% | 11.6% | 4.9% | 30.8% |
| dev | 16 / 40 / 69 | 4 / 7 | 48.6% | 10.3% | 4.4% | 30.5% |
| test | 16 / 40 / 95 | 4 / 7 | 50.0% | 11.8% | 5.1% | 30.7% |

### Observed quality notes

- Product descriptions are sparse at roughly 49–50% empty; ranking templates must not depend on descriptions alone.
- Product color is empty for roughly 30% of judgments; color-aware analysis must report this coverage limit.
- Queries with more than 40 judged candidates were observed: train 462, dev 13, test 198. The pipeline preserves these official rows instead of enforcing the README's informal 'up to 40' description.

## Evaluation boundary

ESCI labels cover judged candidates for each Query, not the entire Amazon catalog. Unjudged products are **unknown**, not automatically Irrelevant. The primary benchmark therefore evaluates candidate-set reranking. This report does not claim full-catalog Recall.

The official dataset has no category field. Stage 1 preserves the official bullet point, brand and color fields and does not fabricate a category.
