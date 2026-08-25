# Amazon ESCI dataset

This directory references Amazon's official Shopping Queries ESCI dataset.

## Why the full files are not copied into this repository

The upstream dataset uses Git LFS. At the pinned revision:

- `shopping_queries_dataset_examples.parquet`: about 48.9 MB
- `shopping_queries_dataset_products.parquet`: about 1.03 GB
- `shopping_queries_dataset_sources.csv`: about 1.6 MB

Copying the same LFS objects into this repository would consume the owner's
GitHub LFS storage and transfer quota without improving reproducibility. The
official repository is instead pinned as `data/esci-data`, and
`scripts/download_esci.sh` retrieves the exact objects into ignored
`data/raw/esci/` without requiring a local Git LFS installation. The command
checks the byte size and SHA-256 recorded in `data/esci.lock.json`.

## Provenance

- Source: https://github.com/amazon-science/esci-data
- Commit: `7916cdf6ab75a462e77f20ab40428a10923998d5`
- License: Apache-2.0
- Languages: English, Spanish, Japanese
- Reduced ranking set: 48,300 queries and 1,118,011 query-product judgments
- Large classification set: 130,652 queries and 2,621,288 judgments

The dataset provides judged candidate products with Exact, Substitute,
Complement, and Irrelevant labels. The Stage 1 profile observes some Queries
with more than 40 judgments, so the pipeline preserves the official rows rather
than enforcing the README's informal “up to 40” description. It is primarily
suitable for candidate reranking. Retrieval experiments must document their
closed-corpus construction because the labels do not cover an entire live
product catalog.

## Download

Requirements:

- `curl`
- `shasum` or `sha256sum`
- About 1.2 GB of free disk space for the upstream dataset

From the project root:

```bash
bash scripts/download_esci.sh
```

Then run `make data-esci-validate` and `make data-esci-build`. Processed files
stay under `data/processed/`; aggregate evidence is committed under
`data/manifests/` and `docs/`.
