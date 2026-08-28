# Source-bounded Query constructor

Date: 2026-08-29
Contract: `source-bounded-query-set-v1`
Status: local smoke-only tool; not a formal evaluation dataset

## Purpose

The Query constructor gives the Agent additional development cases for Bad Case
discovery without opening the 500-Query dev profile or frozen test. It reads only
the committed smoke view and creates deterministic variants that expose spelling
and token-order sensitivity.

```text
20 independently pinned, committed smoke Queries
          ├── identity
          ├── one deterministic adjacent-letter transposition
          └── token-order reversal when there is more than one token
                         ↓
        retain every identity, then NFKC-normalize
                 and de-duplicate synthetic cases
                         ↓
         59 cases = 20 original + 39 synthetic
```

## Data boundary

The tool projects only:

- `query_id`, `query_text` and the allowlisted source bucket;
- `product_locale`, `eval_split`, `origin_split` and `is_smoke` provenance.

It does not read ESCI labels, candidate products or product content. A source
other than `smoke` is rejected before project or data I/O. Before the source
contract, Stage 1 manifest or Parquet can be opened, construction also requires
a clean full Git revision. The code-owned source contract independently pins the
committed Parquet hash, Stage 1 manifest hash, canonical source hash, upstream
source commit, locale, split origin, Query identities/count and schema. Every
source path component is checked without following symbolic links before the
file can be hashed or read.

Every output case says:

- `development_seen=true`;
- `eligible_for_final_evaluation=false`;
- `synthetic_labels_inherited=false`.

Identity cases retain only their existing smoke-candidate label scope. Synthetic
cases are explicitly `unjudged` and intended only for exploratory Bad Case
discovery. It would be invalid to copy the original Query's ESCI labels to a
misspelling or reordered Query and compute formal nDCG/MRR.

All source identity cases are retained before generating any synthetic case. If
a transformation normalizes to an identity or an earlier synthetic case, the
candidate is omitted and the artifact records the construction, source Query,
retained collision and reason. Normalization uses Unicode NFKC, case-folding and
whitespace collapsing, matching the Stage 1 split identity function.

## Determinism and storage

```bash
make query-set-smoke
```

The same trusted source and code revision produce the same content-derived case
and Query-set identities. Each artifact carries its code revision, independent
source-contract hash, manifest/file/canonical hashes, source commit/schema,
pinned Query-key hash and per-case source link. `validate_query_set()` recomputes
case/set IDs, provenance links, buckets, transformations, collisions and counts;
storage calls it before touching the artifact directory. The artifact is then
stored immutably under `runs/query-sets/`; the configured root and subdirectory
cannot be symbolic links or escape their trusted parent. Logs expose only hashes,
IDs and counts, never raw Query text.

The workbench endpoint is fixed to `{ "source": "smoke" }` and returns only:

- Query-set ID and total/original/synthetic/de-duplicated counts;
- counts by construction method;
- the `formal_evaluation_allowed=false` boundary;
- confirmation that locked `dev` and `test` profiles were not read.

## What comes next

The next tool should execute these cases against the baseline, group failures as
zero-result, spelling-sensitive, order-sensitive or stage-drop, and let an
independent Oracle assess whether the Agent's diagnosis follows the evidence.
Synthetic cases still remain development inputs; any relevance claim needs new
human judgments or an explicitly separate weak-label policy.
