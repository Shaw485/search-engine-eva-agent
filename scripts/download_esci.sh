#!/usr/bin/env bash
set -euo pipefail

readonly ESCI_COMMIT="7916cdf6ab75a462e77f20ab40428a10923998d5"
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly OUTPUT_DIR="${ESCI_DATA_DIR:-${PROJECT_DIR}/data/raw/esci}"
readonly BASE_URL="https://media.githubusercontent.com/media/amazon-science/esci-data/${ESCI_COMMIT}/shopping_queries_dataset"
readonly RAW_BASE_URL="https://raw.githubusercontent.com/amazon-science/esci-data/${ESCI_COMMIT}/shopping_queries_dataset"

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required." >&2
  exit 1
fi

sha256_file() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    echo "shasum or sha256sum is required." >&2
    return 1
  fi
}

verify_file() {
  local path="$1"
  local expected_sha="$2"
  local expected_size="$3"
  [[ -f "${path}" ]] || return 1

  local actual_size
  actual_size="$(wc -c < "${path}" | tr -d ' ')"
  [[ "${actual_size}" == "${expected_size}" ]] || return 1
  [[ "$(sha256_file "${path}")" == "${expected_sha}" ]]
}

download_file() {
  local filename="$1"
  local expected_sha="$2"
  local expected_size="$3"
  local source_url="$4"
  local target="${OUTPUT_DIR}/${filename}"
  local partial="${target}.partial"

  if verify_file "${target}" "${expected_sha}" "${expected_size}"; then
    echo "Verified existing ${filename}"
    return
  fi

  echo "Downloading ${filename} (${expected_size} bytes)"
  curl --fail --location --retry 5 --retry-all-errors --continue-at - \
    --output "${partial}" "${source_url}"

  if ! verify_file "${partial}" "${expected_sha}" "${expected_size}"; then
    echo "Integrity check failed for ${partial}." >&2
    echo "Expected size=${expected_size} sha256=${expected_sha}" >&2
    exit 1
  fi
  mv -f "${partial}" "${target}"
  echo "Verified ${filename}"
}

mkdir -p "${OUTPUT_DIR}"
download_file \
  "shopping_queries_dataset_examples.parquet" \
  "4a735b693b4a424a6fc67f5be6e4c811495c488bbf66d02a602d308b2744263a" \
  "51286808" \
  "${BASE_URL}/shopping_queries_dataset_examples.parquet"
download_file \
  "shopping_queries_dataset_products.parquet" \
  "25124442d064d64b26f74082d6fa09438d679efc0c183cf28d19064a2b65a265" \
  "1108857465" \
  "${BASE_URL}/shopping_queries_dataset_products.parquet"
download_file \
  "shopping_queries_dataset_sources.csv" \
  "a5fed8ecc016443de40bf3c63098f0e3f23bbe4daa4236f1c38b8c3184778c50" \
  "1682802" \
  "${RAW_BASE_URL}/shopping_queries_dataset_sources.csv"

echo "ESCI dataset ready at: ${OUTPUT_DIR}"
