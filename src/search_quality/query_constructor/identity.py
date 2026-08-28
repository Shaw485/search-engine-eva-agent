"""Canonical identities shared by Query construction and artifact validation."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

from search_quality.data.contracts import canonical_json_sha256


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def query_case_id(payload_without_id: Mapping[str, Any]) -> str:
    return f"query-case-{canonical_json_sha256(payload_without_id)[:12]}"


def query_set_id(payload_without_id: Mapping[str, Any]) -> str:
    return f"query-set-{canonical_json_sha256(payload_without_id)[:12]}"


def query_keys_sha256(keys: Iterable[tuple[str, int]]) -> str:
    canonical = sorted([str(locale), int(query_id)] for locale, query_id in keys)
    return canonical_json_sha256(canonical)
