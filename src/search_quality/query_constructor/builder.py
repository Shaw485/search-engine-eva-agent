"""Build deterministic Query candidates without reading protected splits."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from search_quality.data.splits import normalize_query
from search_quality.evaluation.artifacts import (
    require_clean_code_revision,
    write_immutable_json,
)
from search_quality.evaluation.authorization import ensure_profile_authorized
from search_quality.evaluation.datasets import (
    EvaluationProfile,
    sha256_file,
    trusted_profile_data_path,
)

from .contracts import (
    CONSTRUCTOR_ID,
    DroppedQueryCase,
    QueryBucket,
    QueryCase,
    QueryConstruction,
    QueryDropReason,
    QuerySetArtifact,
    QuerySourceContract,
    QuerySourceRef,
)
from .identity import query_case_id, query_keys_sha256, query_set_id, sha256_text

logger = logging.getLogger("search_quality.query_constructor")
DEFAULT_SOURCE_CONTRACT = Path("configs/query-constructor/esci-stage1-smoke-v1.json")
MAX_SOURCE_CONTRACT_BYTES = 32 * 1024
_GIT_REVISION_RE = re.compile(r"[0-9a-f]{40}\Z")
_ALPHA_RE = re.compile(r"[A-Za-z]{4,}")
_NEGATIONS = frozenset({"no", "not", "without"})
_SOURCE_COLUMNS = (
    "query_id",
    "query_text",
    "source",
    "product_locale",
    "eval_split",
    "origin_split",
    "is_smoke",
)
_CONSTRUCTION_ORDER = {
    QueryConstruction.IDENTITY: 0,
    QueryConstruction.ADJACENT_TRANSPOSITION: 1,
    QueryConstruction.TOKEN_ORDER_REVERSAL: 2,
}


@dataclass(frozen=True, slots=True)
class _BuildProvenance:
    contract: QuerySourceContract
    source_contract_sha256: str
    code_revision: str


def build_smoke_query_set(
    *,
    project_root: str | Path,
    source_profile: str = "smoke",
    revision_provider: Callable[[Path], str] = require_clean_code_revision,
) -> QuerySetArtifact:
    """Construct original and exploratory variants from the committed smoke set.

    The profile and clean-revision gates intentionally run before manifest,
    source-contract, or Parquet I/O. Synthetic variants have no ESCI labels and
    cannot enter formal evaluation.
    """

    if source_profile != "smoke":
        raise ValueError("Query constructor is restricted to the smoke profile")
    ensure_profile_authorized(source_profile)
    root = Path(project_root).resolve(strict=True)
    code_revision = revision_provider(root).strip()
    if _GIT_REVISION_RE.fullmatch(code_revision) is None:
        raise ValueError("Query constructor requires a clean full Git revision")

    contract_path = _trusted_project_file(root, DEFAULT_SOURCE_CONTRACT)
    contract, source_contract_sha256 = _load_source_contract(contract_path)
    manifest_path = _trusted_project_file(
        root,
        Path(contract.stage1_manifest_relative_path),
    )
    observed_manifest_sha256 = sha256_file(manifest_path)
    if observed_manifest_sha256 != contract.stage1_manifest_sha256:
        raise ValueError("Stage 1 manifest does not match the Query source contract")

    profile = EvaluationProfile.from_stage1_manifest(
        profile_id="smoke",
        project_root=root,
        manifest_path=manifest_path,
    )
    _validate_profile_contract(profile, contract=contract)
    source_path = trusted_profile_data_path(profile, project_root=root)
    if source_path.relative_to(root).as_posix() != contract.source_relative_path:
        raise ValueError("smoke source path does not match the Query source contract")
    observed_source_sha256 = sha256_file(source_path)
    if observed_source_sha256 != contract.source_file_sha256:
        raise ValueError("committed smoke source does not match its independent pin")
    if observed_source_sha256 != profile.file_sha256:
        raise ValueError("committed smoke source does not match the Stage 1 manifest")

    logger.info(
        "query_construction_started",
        extra={
            "profile_id": "smoke",
            "source_contract_sha256": source_contract_sha256,
            "source_file_sha256": observed_source_sha256,
        },
    )
    frame = pl.read_parquet(source_path, columns=_SOURCE_COLUMNS).with_columns(
        pl.col("query_text").cast(pl.String),
        pl.col("source").cast(pl.String).str.to_lowercase(),
        pl.col("product_locale").cast(pl.String).str.to_lowercase(),
        pl.col("eval_split").cast(pl.String).str.to_lowercase(),
        pl.col("origin_split").cast(pl.String).str.to_lowercase(),
    )
    if sha256_file(source_path) != observed_source_sha256:
        raise ValueError("committed smoke source changed while it was being read")
    _validate_smoke_source(frame, contract=contract)
    source_queries = (
        frame.select("query_id", "query_text", "product_locale", "source")
        .unique()
        .sort("product_locale", "query_id")
    )
    observed_query_keys_sha256 = query_keys_sha256(
        (str(row["product_locale"]), int(row["query_id"]))
        for row in source_queries.iter_rows(named=True)
    )
    if observed_query_keys_sha256 != contract.query_keys_sha256:
        raise ValueError("smoke Query keys do not match their independent pin")

    provenance = _BuildProvenance(
        contract=contract,
        source_contract_sha256=source_contract_sha256,
        code_revision=code_revision,
    )
    cases, dropped_cases = _construct_cases(source_queries, provenance=provenance)
    originals = sum(item.construction == QueryConstruction.IDENTITY for item in cases)
    synthetic = len(cases) - originals
    artifact_body: dict[str, Any] = {
        "cases": [item.model_dump(mode="json") for item in cases],
        "code_revision": code_revision,
        "constructor_id": CONSTRUCTOR_ID,
        "cross_split_collision_status": "not_checked_without_reading_locked_splits",
        "deduplicated_count": len(dropped_cases),
        "dropped_cases": [item.model_dump(mode="json") for item in dropped_cases],
        "eval_split": contract.eval_split,
        "formal_evaluation_allowed": False,
        "is_smoke": contract.is_smoke,
        "locale": contract.locale,
        "locked_profiles_not_read": ["dev", "test"],
        "origin_split": contract.origin_split,
        "original_count": originals,
        "query_count": len(cases),
        "query_keys_sha256": observed_query_keys_sha256,
        "schema_version": "source-bounded-query-set-v1",
        "source_canonical_sha256": contract.source_canonical_sha256,
        "source_commit": contract.source_commit,
        "source_contract_sha256": source_contract_sha256,
        "source_file_sha256": observed_source_sha256,
        "source_id": contract.source_id,
        "source_profile": contract.profile,
        "source_query_count": source_queries.height,
        "stage1_manifest_sha256": observed_manifest_sha256,
        "stage1_schema_version": contract.stage1_schema_version,
        "synthetic_count": synthetic,
    }
    artifact = QuerySetArtifact.model_validate(
        {
            **artifact_body,
            "query_set_id": query_set_id(artifact_body),
        }
    )
    validate_query_set(artifact)
    logger.info(
        "query_construction_completed",
        extra={
            "deduplicated_count": artifact.deduplicated_count,
            "original_count": artifact.original_count,
            "query_count": artifact.query_count,
            "query_set_id": artifact.query_set_id,
            "synthetic_count": artifact.synthetic_count,
        },
    )
    return artifact


def validate_query_set(artifact: QuerySetArtifact) -> QuerySetArtifact:
    """Recompute the complete construction, provenance links, and content IDs."""

    validated = QuerySetArtifact.model_validate(artifact.model_dump(mode="json"))
    identities = {
        (item.source.locale, item.source.query_id): item
        for item in validated.cases
        if item.construction == QueryConstruction.IDENTITY
    }
    for item in validated.cases:
        if item.buckets != _buckets(item.query_text):
            raise ValueError("Query buckets do not match Query text")
        source = identities[(item.source.locale, item.source.query_id)]
        if item.construction == QueryConstruction.IDENTITY:
            if item.query_text != source.query_text:
                raise ValueError("identity Query text does not match its source")
            continue
        expected = _apply_construction(item.construction, source.query_text)
        if expected != item.query_text:
            raise ValueError("synthetic Query does not match its construction")

    cases_by_id = {item.case_id: item for item in validated.cases}
    for dropped in validated.dropped_cases:
        source = identities[(dropped.locale, dropped.source_query_id)]
        expected = _apply_construction(dropped.construction, source.query_text)
        if expected is None:
            raise ValueError("drop record references an inapplicable construction")
        if sha256_text(normalize_query(expected)) != dropped.normalized_query_sha256:
            raise ValueError("drop record does not match its construction")
        collided = cases_by_id[dropped.collides_with_case_id]
        if collided.normalized_query_sha256 != dropped.normalized_query_sha256:
            raise ValueError("drop record does not identify its retained collision")

    actual_synthetic = {
        (item.source.locale, item.source.query_id, item.construction): item
        for item in validated.cases
        if item.construction != QueryConstruction.IDENTITY
    }
    if len(actual_synthetic) != validated.synthetic_count:
        raise ValueError("synthetic construction keys must be unique")
    actual_drops = {
        (item.locale, item.source_query_id, item.construction): item
        for item in validated.dropped_cases
    }
    if len(actual_drops) != validated.deduplicated_count:
        raise ValueError("drop construction keys must be unique")

    retained_by_normalized: dict[str, QueryCase] = {}
    ordered_identities = sorted(
        identities.values(),
        key=lambda item: (item.source.locale, item.source.query_id),
    )
    for identity in ordered_identities:
        retained_by_normalized.setdefault(identity.normalized_query_sha256, identity)

    expected_synthetic: dict[tuple[str, int, QueryConstruction], QueryCase] = {}
    expected_drops: dict[tuple[str, int, QueryConstruction], DroppedQueryCase] = {}
    for source in ordered_identities:
        for construction in (
            QueryConstruction.ADJACENT_TRANSPOSITION,
            QueryConstruction.TOKEN_ORDER_REVERSAL,
        ):
            query_text = _apply_construction(construction, source.query_text)
            if query_text is None:
                continue
            key = (source.source.locale, source.source.query_id, construction)
            normalized_sha256 = sha256_text(normalize_query(query_text))
            collided = retained_by_normalized.get(normalized_sha256)
            if collided is not None:
                expected_drops[key] = DroppedQueryCase(
                    construction=construction,
                    source_query_id=source.source.query_id,
                    locale=source.source.locale,
                    normalized_query_sha256=normalized_sha256,
                    collides_with_case_id=collided.case_id,
                    reason=(
                        QueryDropReason.DUPLICATES_IDENTITY
                        if collided.construction == QueryConstruction.IDENTITY
                        else QueryDropReason.DUPLICATES_SYNTHETIC
                    ),
                )
                continue
            candidate = _make_case(construction, query_text, source.source)
            expected_synthetic[key] = candidate
            retained_by_normalized[normalized_sha256] = candidate

    if actual_synthetic != expected_synthetic:
        raise ValueError(
            "Query set does not contain the complete synthetic construction"
        )
    if actual_drops != expected_drops:
        raise ValueError("Query set does not contain the complete collision ledger")
    return validated


def store_query_set(
    artifact: QuerySetArtifact,
    *,
    artifact_root: str | Path,
) -> Path:
    """Persist one validated immutable Query set below an explicit local root."""

    artifact = validate_query_set(artifact)
    configured = Path(artifact_root)
    if not configured.is_absolute():
        raise ValueError("Query artifact root must be absolute")
    if configured.is_symlink():
        raise ValueError("Query artifact root must not be a symbolic link")
    configured.mkdir(parents=True, exist_ok=True)
    root = configured.resolve(strict=True)
    directory = root / "query-sets"
    if directory.is_symlink():
        raise ValueError("Query artifact directory must not be a symbolic link")
    directory.mkdir(parents=True, exist_ok=True)
    resolved = directory.resolve(strict=True)
    if resolved.parent != root:
        raise ValueError("Query artifact directory escaped its configured root")
    path = resolved / f"{artifact.query_set_id}.json"
    write_immutable_json(path, artifact.model_dump(mode="json"))
    logger.info(
        "query_set_stored",
        extra={
            "query_count": artifact.query_count,
            "query_set_id": artifact.query_set_id,
        },
    )
    return path


def _load_source_contract(path: Path) -> tuple[QuerySourceContract, str]:
    with path.open("rb") as source:
        encoded = source.read(MAX_SOURCE_CONTRACT_BYTES + 1)
    if len(encoded) > MAX_SOURCE_CONTRACT_BYTES:
        raise ValueError("Query source contract exceeds the size limit")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Query source contract contains duplicate JSON keys")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("Query source contract contains a non-finite number")

    payload = json.loads(
        encoded.decode("utf-8"),
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_constant,
    )
    return (
        QuerySourceContract.model_validate(payload),
        hashlib.sha256(encoded).hexdigest(),
    )


def _trusted_project_file(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Query constructor file escaped the project root")
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("Query constructor path must not contain a symbolic link")
    if not cursor.is_file():
        raise FileNotFoundError(cursor)
    resolved = cursor.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("Query constructor file escaped the project root") from exc
    return resolved


def _validate_profile_contract(
    profile: EvaluationProfile,
    *,
    contract: QuerySourceContract,
) -> None:
    observed = {
        "canonical_sha256": profile.canonical_sha256,
        "expected_queries": profile.expected_queries,
        "file_sha256": profile.file_sha256,
        "profile": profile.profile_id,
        "source_commit": profile.source_commit,
        "stage1_manifest_sha256": profile.stage1_manifest_sha256,
        "stage1_schema_version": profile.stage1_schema_version,
    }
    expected = {
        "canonical_sha256": contract.source_canonical_sha256,
        "expected_queries": contract.expected_query_count,
        "file_sha256": contract.source_file_sha256,
        "profile": contract.profile,
        "source_commit": contract.source_commit,
        "stage1_manifest_sha256": contract.stage1_manifest_sha256,
        "stage1_schema_version": contract.stage1_schema_version,
    }
    if observed != expected:
        raise ValueError("Stage 1 profile does not match the Query source contract")


def _validate_smoke_source(
    frame: pl.DataFrame,
    *,
    contract: QuerySourceContract,
) -> None:
    required = set(_SOURCE_COLUMNS)
    if set(frame.columns) != required:
        raise ValueError(
            "smoke Query source fields do not match the trusted projection"
        )
    if frame.is_empty():
        raise ValueError("smoke Query source must not be empty")
    if (
        frame.get_column("query_id").null_count()
        or not frame.schema["query_id"].is_integer()
    ):
        raise ValueError("smoke Query IDs are invalid")
    if (
        frame.get_column("query_text").null_count()
        or frame.filter(pl.col("query_text").str.strip_chars() == "").height
    ):
        raise ValueError("smoke Query text is invalid")
    if (
        frame.get_column("source").null_count()
        or frame.filter(pl.col("source") == "").height
    ):
        raise ValueError("smoke Query source bucket is invalid")
    allowed_sources = {
        "behavioral",
        "negations",
        "nlqec",
        "other",
        "parse_pattern",
        "unknown",
    }
    if not set(frame.get_column("source").unique().to_list()) <= allowed_sources:
        raise ValueError("smoke Query source bucket is not allowlisted")
    if set(frame.get_column("product_locale").unique().to_list()) != {contract.locale}:
        raise ValueError("Query constructor accepts only the pinned smoke locale")
    if set(frame.get_column("eval_split").unique().to_list()) != {contract.eval_split}:
        raise ValueError("smoke Query source must remain inside the dev view")
    if set(frame.get_column("origin_split").unique().to_list()) != {
        contract.origin_split
    }:
        raise ValueError("smoke Query source must be official-train-derived")
    if frame.get_column("is_smoke").dtype != pl.Boolean or not bool(
        frame.get_column("is_smoke").all()
    ):
        raise ValueError("Query constructor source contains non-smoke rows")
    identity_conflicts = (
        frame.group_by("product_locale", "query_id")
        .agg(
            pl.col("query_text").n_unique().alias("texts"),
            pl.col("source").n_unique().alias("sources"),
        )
        .filter((pl.col("texts") != 1) | (pl.col("sources") != 1))
    )
    if identity_conflicts.height:
        raise ValueError("a smoke Query ID maps to multiple texts")
    observed_queries = frame.select(
        pl.struct("product_locale", "query_id").n_unique()
    ).item()
    if observed_queries != contract.expected_query_count:
        raise ValueError("smoke Query count does not match its independent pin")


def _construct_cases(
    source_queries: pl.DataFrame,
    *,
    provenance: _BuildProvenance,
) -> tuple[list[QueryCase], list[DroppedQueryCase]]:
    sources: list[tuple[str, QuerySourceRef]] = []
    contract = provenance.contract
    for row in source_queries.sort("product_locale", "query_id").iter_rows(named=True):
        original = _display_query(str(row["query_text"]))
        sources.append(
            (
                original,
                QuerySourceRef(
                    source_id=contract.source_id,
                    locale=contract.locale,
                    query_id=int(row["query_id"]),
                    source_bucket=str(row["source"]),
                    eval_split=contract.eval_split,
                    origin_split=contract.origin_split,
                    is_smoke=contract.is_smoke,
                    source_file_sha256=contract.source_file_sha256,
                    source_canonical_sha256=contract.source_canonical_sha256,
                    stage1_manifest_sha256=contract.stage1_manifest_sha256,
                    stage1_schema_version=contract.stage1_schema_version,
                    source_commit=contract.source_commit,
                    source_contract_sha256=provenance.source_contract_sha256,
                    source_query_sha256=sha256_text(normalize_query(original)),
                ),
            )
        )

    cases: list[QueryCase] = []
    retained_by_normalized: dict[str, QueryCase] = {}
    for original, source in sources:
        identity = _make_case(QueryConstruction.IDENTITY, original, source)
        cases.append(identity)
        retained_by_normalized.setdefault(identity.normalized_query_sha256, identity)

    dropped: list[DroppedQueryCase] = []
    for original, source in sources:
        for construction in (
            QueryConstruction.ADJACENT_TRANSPOSITION,
            QueryConstruction.TOKEN_ORDER_REVERSAL,
        ):
            query_text = _apply_construction(construction, original)
            if query_text is None:
                continue
            normalized_sha256 = sha256_text(normalize_query(query_text))
            collided = retained_by_normalized.get(normalized_sha256)
            if collided is not None:
                dropped.append(
                    DroppedQueryCase(
                        construction=construction,
                        source_query_id=source.query_id,
                        normalized_query_sha256=normalized_sha256,
                        collides_with_case_id=collided.case_id,
                        reason=(
                            QueryDropReason.DUPLICATES_IDENTITY
                            if collided.construction == QueryConstruction.IDENTITY
                            else QueryDropReason.DUPLICATES_SYNTHETIC
                        ),
                    )
                )
                continue
            candidate = _make_case(construction, query_text, source)
            cases.append(candidate)
            retained_by_normalized[normalized_sha256] = candidate

    cases.sort(
        key=lambda item: (
            item.source.locale,
            item.source.query_id,
            _CONSTRUCTION_ORDER[item.construction],
            item.case_id,
        )
    )
    dropped.sort(
        key=lambda item: (
            item.locale,
            item.source_query_id,
            _CONSTRUCTION_ORDER[item.construction],
            item.collides_with_case_id,
        )
    )
    return cases, dropped


def _make_case(
    construction: QueryConstruction,
    query_text: str,
    source: QuerySourceRef,
) -> QueryCase:
    synthetic = construction != QueryConstruction.IDENTITY
    body = {
        "buckets": [item.value for item in _buckets(query_text)],
        "construction": construction.value,
        "constructor_id": CONSTRUCTOR_ID,
        "development_seen": True,
        "eligible_for_final_evaluation": False,
        "intended_use": (
            "exploratory_bad_case_discovery" if synthetic else "smoke_reference"
        ),
        "label_scope": "unjudged" if synthetic else "smoke_judged_candidates",
        "normalized_query_sha256": sha256_text(normalize_query(query_text)),
        "query_text": query_text,
        "source": source.model_dump(mode="json"),
        "synthetic_labels_inherited": False,
    }
    return QueryCase.model_validate(
        {
            **body,
            "case_id": query_case_id(body),
        }
    )


def _display_query(value: str) -> str:
    if value != value.strip() or not value or len(value) > 256:
        raise ValueError("Query text length is outside the constructor contract")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError("Query text contains a control character")
    return value


def _normalize_query(value: str) -> str:
    """Compatibility wrapper around the Stage 1 leakage identity function."""

    return normalize_query(value)


def _apply_construction(
    construction: QueryConstruction,
    value: str,
) -> str | None:
    if construction == QueryConstruction.IDENTITY:
        return _display_query(value)
    if construction == QueryConstruction.ADJACENT_TRANSPOSITION:
        return _adjacent_transposition(value)
    if construction == QueryConstruction.TOKEN_ORDER_REVERSAL:
        return _reverse_tokens(value)
    raise ValueError("unsupported Query construction")


def _adjacent_transposition(value: str) -> str | None:
    matches = list(_ALPHA_RE.finditer(value))
    if not matches:
        return None
    target = max(matches, key=lambda item: (len(item.group(0)), -item.start()))
    token = target.group(0)
    center = max(0, (len(token) // 2) - 1)
    swap_index = next(
        (
            index
            for offset in range(len(token) - 1)
            for index in (center + offset, center - offset)
            if 0 <= index < len(token) - 1 and token[index] != token[index + 1]
        ),
        None,
    )
    if swap_index is None:
        return None
    changed = (
        token[:swap_index]
        + token[swap_index + 1]
        + token[swap_index]
        + token[swap_index + 2 :]
    )
    return _display_query(value[: target.start()] + changed + value[target.end() :])


def _reverse_tokens(value: str) -> str | None:
    tokens = value.split()
    if len(tokens) < 2:
        return None
    reversed_query = " ".join(reversed(tokens))
    return (
        None
        if normalize_query(reversed_query) == normalize_query(value)
        else reversed_query
    )


def _buckets(value: str) -> list[QueryBucket]:
    normalized = normalize_query(value)
    tokens = normalized.split()
    if len(tokens) == 1:
        result = [QueryBucket.SINGLE_TOKEN]
    elif len(tokens) <= 3:
        result = [QueryBucket.SHORT_KEYWORD]
    else:
        result = [QueryBucket.LONG_TAIL]
    if any(character.isdigit() for character in normalized):
        result.append(QueryBucket.CONTAINS_NUMERIC)
    if "-" in normalized:
        result.append(QueryBucket.CONTAINS_HYPHEN)
    if _NEGATIONS & set(tokens):
        result.append(QueryBucket.CONTAINS_NEGATION)
    if any(ord(character) > 127 for character in normalized):
        result.append(QueryBucket.NON_ASCII)
    return result
