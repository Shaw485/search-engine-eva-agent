# Search Engine EVA Agent

An evidence-driven evaluation and diagnosis agent for e-commerce search ranking.

The project uses the Amazon Shopping Queries ESCI dataset to compare BM25,
vector retrieval, hybrid ranking, and Cross-Encoder reranking. Every conclusion
must be traceable to a dataset version, run configuration, metric, and ranked
product list.

## Project status

The project is currently at **Stage 0: engineering skeleton and technical gate**.
See the full implementation guide in [ROADMAP.md](ROADMAP.md).

## Dataset

The official Amazon ESCI repository is pinned under `data/esci-data` as a Git
submodule. Its two Parquet files are stored by the upstream project with Git
LFS; the products file alone is about 1.03 GB. This repository therefore keeps
the source pinned instead of duplicating the large LFS objects into this
repository's quota.

Clone the project and download the complete dataset with:

```bash
git clone --recurse-submodules https://github.com/Shaw485/search-engine-eva-agent.git
cd search-engine-eva-agent
bash scripts/download_esci.sh
```

If the repository was cloned without submodules:

```bash
git submodule update --init --depth 1 data/esci-data
bash scripts/download_esci.sh
```

The downloaded files are available under:

```text
data/esci-data/shopping_queries_dataset/
├── shopping_queries_dataset_examples.parquet
├── shopping_queries_dataset_products.parquet
└── shopping_queries_dataset_sources.csv
```

Dataset source: [amazon-science/esci-data](https://github.com/amazon-science/esci-data)  
Pinned upstream commit: `7916cdf6ab75a462e77f20ab40428a10923998d5`  
Upstream license: [Apache-2.0](https://github.com/amazon-science/esci-data/blob/main/LICENSE)

## Planned workflow

```text
ESCI query + candidate products
              ↓
BM25 / Vector / Hybrid / Cross-Encoder
              ↓
nDCG / MRR / Recall + ranking diff
              ↓
Bad-case diagnosis agent
              ↓
Evidence-backed report + Trace + Replay
```

The first benchmark focuses on reranking the fully judged candidate set. A
separate closed-corpus track will be used for retrieval metrics so incomplete
relevance judgments are not presented as full-catalog recall.

