# Stage 1 data dictionary

## ESCI benchmark rows

**Description:** English-US judged candidates from the Amazon Shopping Queries
ESCI Task 1 reduced set.

**Grain:** one row per `(product_locale, query_id, product_id)` judgment.

**Formal split:** `train`, `dev`, or frozen official `test`. `smoke.parquet` is a
20-Query profile inside `dev`; it is not a fourth formal split.

| Column | Type | Classification | Description | Notes |
|---|---|---|---|---|
| `example_id` | Int64 | Identifier | Upstream judgment identifier | Stable tie-break key |
| `query_id` | Int64 | Identifier | Upstream Query identifier | Formal split unit |
| `query_text` | String | Text | Original customer Query | Preserved for display and ranking |
| `query_key` | String | Structural | NFKC, case-folded, whitespace-normalized Query | Leakage checks only; not model input |
| `product_id` | String | Identifier | Product identifier | Unique only with locale |
| `product_locale` | String | Dimension | Product marketplace locale | Stage 1 is fixed to `us` |
| `product_title` | String | Text | Official product title | Required and non-empty |
| `product_description` | String | Text | Official product description | Roughly half are empty |
| `product_bullet_point` | String | Text | Official product bullet text | Retained instead of inventing category |
| `product_brand` | String | Dimension/Text | Official brand | May be empty |
| `product_color` | String | Dimension/Text | Official color | Roughly 30% are empty |
| `esci_label` | String | Dimension | `E`, `S`, `C`, or `I` judgment | Gain mapping is defined later per experiment |
| `source` | String | Dimension | Query source category | E.g. `other`, `negations`, `behavioral` |
| `origin_split` | String | Dimension | Official `train` or `test` | Official test is never resampled |
| `eval_split` | String | Dimension | Project `train`, `dev`, or `test` | Deterministic at Query level |
| `is_smoke` | Boolean | Boolean | Query belongs to the fixed smoke profile | Only true inside dev |

## Relationships and keys

- Examples join products on `(product_locale, product_id)`, never `product_id`
  alone.
- Examples join Query source on `query_id`.
- A `query_id` must map to exactly one Query text, locale and official split.
- Normalized copies of the same Query text cannot cross formal splits.
- A Query-product pair cannot have multiple ESCI labels.

## Known limits

- The official product table contains no category field. Stage 1 does not
  fabricate one.
- ESCI labels cover a judged candidate set, not the full Amazon catalog.
  Unjudged products are unknown rather than automatically Irrelevant.
- The main benchmark is candidate-set reranking. Full-catalog Recall is not
  claimed.
- Processed Parquet files remain local under `data/processed/`; Git stores only
  the configuration, manifest, dictionary and aggregate report.

## Provenance

- Source: `amazon-science/esci-data`
- Pinned commit: `7916cdf6ab75a462e77f20ab40428a10923998d5`
- License: Apache-2.0; upstream LICENSE and NOTICE remain in the pinned submodule.
