#!/usr/bin/env bash
set -euo pipefail

readonly ESCI_COMMIT="7916cdf6ab75a462e77f20ab40428a10923998d5"
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly DATASET_DIR="${PROJECT_DIR}/data/esci-data"

if ! command -v git >/dev/null 2>&1; then
  echo "git is required." >&2
  exit 1
fi

if ! command -v git-lfs >/dev/null 2>&1 && ! git lfs version >/dev/null 2>&1; then
  echo "Git LFS is required. Install it from https://git-lfs.com/ and retry." >&2
  exit 1
fi

if [[ ! -d "${DATASET_DIR}" ]]; then
  git -C "${PROJECT_DIR}" submodule update --init --depth 1 data/esci-data
fi

if [[ ! -e "${DATASET_DIR}/.git" ]]; then
  echo "The ESCI submodule is missing at ${DATASET_DIR}." >&2
  echo "Run: git submodule update --init --depth 1 data/esci-data" >&2
  exit 1
fi

git -C "${DATASET_DIR}" fetch --depth 1 origin "${ESCI_COMMIT}"
git -C "${DATASET_DIR}" checkout --detach "${ESCI_COMMIT}"
git -C "${DATASET_DIR}" lfs install --local
git -C "${DATASET_DIR}" lfs pull --include="shopping_queries_dataset/*"

echo "ESCI dataset ready at: ${DATASET_DIR}/shopping_queries_dataset"

