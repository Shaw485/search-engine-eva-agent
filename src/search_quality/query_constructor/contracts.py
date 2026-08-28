"""Strict contracts for source-bounded Query construction artifacts."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, StrictInt, StrictStr, model_validator

from search_quality.agent.contracts import StrictModel
from search_quality.data.splits import normalize_query

from .identity import query_case_id, query_keys_sha256, query_set_id, sha256_text

SHA256_PATTERN = r"^[0-9a-f]{64}$"
GIT_REVISION_PATTERN = r"^[0-9a-f]{40}$"
QUERY_CASE_ID_PATTERN = r"^query-case-[0-9a-f]{12}$"
QUERY_SET_ID_PATTERN = r"^query-set-[0-9a-f]{12}$"
CONSTRUCTOR_ID = "source-bounded-query-constructor-v1"


class QueryConstruction(StrEnum):
    IDENTITY = "identity"
    ADJACENT_TRANSPOSITION = "adjacent_transposition"
    TOKEN_ORDER_REVERSAL = "token_order_reversal"


class QueryBucket(StrEnum):
    SINGLE_TOKEN = "single_token"
    SHORT_KEYWORD = "short_keyword"
    LONG_TAIL = "long_tail"
    CONTAINS_NUMERIC = "contains_numeric"
    CONTAINS_HYPHEN = "contains_hyphen"
    CONTAINS_NEGATION = "contains_negation"
    NON_ASCII = "non_ascii"


class QueryDropReason(StrEnum):
    DUPLICATES_IDENTITY = "synthetic_duplicates_identity"
    DUPLICATES_SYNTHETIC = "synthetic_duplicates_synthetic"


class QuerySourceContract(StrictModel):
    """Independent, committed pins for the only allowed Query source."""

    schema_version: Literal["query-constructor-source-v1"]
    constructor_id: Literal["source-bounded-query-constructor-v1"]
    source_id: Literal["esci-stage1-smoke-v1"]
    profile: Literal["smoke"]
    source_relative_path: Literal["data/samples/esci-stage1-smoke.parquet"]
    stage1_manifest_relative_path: Literal["data/manifests/esci-stage1.json"]
    source_file_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    source_canonical_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    stage1_manifest_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    stage1_schema_version: Literal["esci-stage1-v1"]
    source_commit: StrictStr = Field(pattern=GIT_REVISION_PATTERN)
    expected_query_count: StrictInt = Field(ge=1, le=100)
    query_keys_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    locale: Literal["us"]
    eval_split: Literal["dev"]
    origin_split: Literal["train"]
    is_smoke: Literal[True]


class QuerySourceRef(StrictModel):
    source_type: Literal["committed_smoke"] = "committed_smoke"
    source_id: Literal["esci-stage1-smoke-v1"]
    profile: Literal["smoke"] = "smoke"
    locale: Literal["us"] = "us"
    query_id: StrictInt = Field(ge=1)
    source_bucket: Literal[
        "behavioral", "negations", "nlqec", "other", "parse_pattern", "unknown"
    ]
    eval_split: Literal["dev"]
    origin_split: Literal["train"]
    is_smoke: Literal[True]
    source_file_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    source_canonical_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    stage1_manifest_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    stage1_schema_version: Literal["esci-stage1-v1"]
    source_commit: StrictStr = Field(pattern=GIT_REVISION_PATTERN)
    source_contract_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    source_query_sha256: StrictStr = Field(pattern=SHA256_PATTERN)


class QueryCase(StrictModel):
    case_id: StrictStr = Field(pattern=QUERY_CASE_ID_PATTERN)
    constructor_id: Literal["source-bounded-query-constructor-v1"] = CONSTRUCTOR_ID
    query_text: StrictStr = Field(min_length=1, max_length=256)
    normalized_query_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    construction: QueryConstruction
    buckets: list[QueryBucket] = Field(min_length=1, max_length=7)
    label_scope: Literal["smoke_judged_candidates", "unjudged"]
    intended_use: Literal["smoke_reference", "exploratory_bad_case_discovery"]
    development_seen: Literal[True] = True
    eligible_for_final_evaluation: Literal[False] = False
    synthetic_labels_inherited: Literal[False] = False
    source: QuerySourceRef

    @model_validator(mode="after")
    def validate_usage_boundary_and_identity(self) -> Self:
        if len(self.buckets) != len(set(self.buckets)):
            raise ValueError("Query buckets must be unique")
        expected_normalized = sha256_text(normalize_query(self.query_text))
        if self.normalized_query_sha256 != expected_normalized:
            raise ValueError("normalized Query hash does not match Query text")
        if self.construction == QueryConstruction.IDENTITY:
            if (
                self.label_scope != "smoke_judged_candidates"
                or self.intended_use != "smoke_reference"
            ):
                raise ValueError("identity Query must retain the smoke label boundary")
            if self.source.source_query_sha256 != self.normalized_query_sha256:
                raise ValueError("identity Query does not match its source Query")
        elif (
            self.label_scope != "unjudged"
            or self.intended_use != "exploratory_bad_case_discovery"
        ):
            raise ValueError("synthetic Query must remain unjudged and exploratory")
        if (
            not self.development_seen
            or self.eligible_for_final_evaluation
            or self.synthetic_labels_inherited
        ):
            raise ValueError("constructed Query violates its contamination boundary")
        expected_case_id = query_case_id(
            self.model_dump(mode="json", exclude={"case_id"})
        )
        if self.case_id != expected_case_id:
            raise ValueError("Query case ID does not match its content")
        return self


class DroppedQueryCase(StrictModel):
    construction: Literal[
        QueryConstruction.ADJACENT_TRANSPOSITION,
        QueryConstruction.TOKEN_ORDER_REVERSAL,
    ]
    source_query_id: StrictInt = Field(ge=1)
    locale: Literal["us"] = "us"
    normalized_query_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    collides_with_case_id: StrictStr = Field(pattern=QUERY_CASE_ID_PATTERN)
    reason: QueryDropReason


class QuerySetArtifact(StrictModel):
    schema_version: Literal["source-bounded-query-set-v1"] = (
        "source-bounded-query-set-v1"
    )
    query_set_id: StrictStr = Field(pattern=QUERY_SET_ID_PATTERN)
    constructor_id: Literal["source-bounded-query-constructor-v1"] = CONSTRUCTOR_ID
    code_revision: StrictStr = Field(pattern=GIT_REVISION_PATTERN)
    source_id: Literal["esci-stage1-smoke-v1"]
    source_profile: Literal["smoke"] = "smoke"
    source_file_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    source_canonical_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    stage1_manifest_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    stage1_schema_version: Literal["esci-stage1-v1"]
    source_commit: StrictStr = Field(pattern=GIT_REVISION_PATTERN)
    source_contract_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    query_keys_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    locale: Literal["us"] = "us"
    eval_split: Literal["dev"] = "dev"
    origin_split: Literal["train"] = "train"
    is_smoke: Literal[True] = True
    source_query_count: StrictInt = Field(ge=1, le=100)
    query_count: StrictInt = Field(ge=1, le=500)
    original_count: StrictInt = Field(ge=1, le=100)
    synthetic_count: StrictInt = Field(ge=0, le=400)
    deduplicated_count: StrictInt = Field(ge=0, le=500)
    dropped_cases: list[DroppedQueryCase] = Field(max_length=500)
    locked_profiles_not_read: tuple[Literal["dev"], Literal["test"]] = (
        "dev",
        "test",
    )
    cross_split_collision_status: Literal[
        "not_checked_without_reading_locked_splits"
    ] = "not_checked_without_reading_locked_splits"
    formal_evaluation_allowed: Literal[False] = False
    cases: list[QueryCase] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_counts_provenance_and_identity(self) -> Self:
        if self.query_count != len(self.cases):
            raise ValueError("query_count does not match Query cases")
        originals = [
            item
            for item in self.cases
            if item.construction == QueryConstruction.IDENTITY
        ]
        synthetic = [
            item
            for item in self.cases
            if item.construction != QueryConstruction.IDENTITY
        ]
        if self.original_count != len(originals):
            raise ValueError("original_count does not match Query cases")
        if self.original_count != self.source_query_count:
            raise ValueError("every source Query must retain one identity case")
        if self.synthetic_count != len(synthetic):
            raise ValueError("synthetic_count does not match Query cases")
        if self.deduplicated_count != len(self.dropped_cases):
            raise ValueError("deduplicated_count does not match drop records")
        if len({item.case_id for item in self.cases}) != len(self.cases):
            raise ValueError("Query case IDs must be unique")

        identity_by_key = {
            (item.source.locale, item.source.query_id): item for item in originals
        }
        if len(identity_by_key) != len(originals):
            raise ValueError("source Query identities must be unique")
        if query_keys_sha256(identity_by_key) != self.query_keys_sha256:
            raise ValueError("source Query keys do not match their pinned hash")

        identity_hashes = {item.normalized_query_sha256 for item in originals}
        synthetic_hashes = [item.normalized_query_sha256 for item in synthetic]
        if identity_hashes & set(synthetic_hashes):
            raise ValueError("synthetic Query duplicates a source identity")
        if len(synthetic_hashes) != len(set(synthetic_hashes)):
            raise ValueError("synthetic Query texts must be deduplicated")

        top_level_provenance = {
            "eval_split": self.eval_split,
            "is_smoke": self.is_smoke,
            "locale": self.locale,
            "origin_split": self.origin_split,
            "profile": self.source_profile,
            "source_canonical_sha256": self.source_canonical_sha256,
            "source_commit": self.source_commit,
            "source_contract_sha256": self.source_contract_sha256,
            "source_file_sha256": self.source_file_sha256,
            "source_id": self.source_id,
            "stage1_manifest_sha256": self.stage1_manifest_sha256,
            "stage1_schema_version": self.stage1_schema_version,
        }
        for item in self.cases:
            observed = item.source.model_dump(
                mode="json",
                include=set(top_level_provenance),
            )
            if observed != top_level_provenance:
                raise ValueError("Query case provenance does not match its Query set")
            if item.constructor_id != self.constructor_id:
                raise ValueError("Query case constructor does not match its Query set")
            identity = identity_by_key.get((item.source.locale, item.source.query_id))
            if identity is None or item.source != identity.source:
                raise ValueError("synthetic Query has no exact source identity")

        cases_by_id = {item.case_id: item for item in self.cases}
        for dropped in self.dropped_cases:
            collided = cases_by_id.get(dropped.collides_with_case_id)
            if collided is None:
                raise ValueError("drop record references an unknown retained Query")
            if dropped.normalized_query_sha256 != collided.normalized_query_sha256:
                raise ValueError("drop record hash does not match retained Query")
            if (dropped.locale, dropped.source_query_id) not in identity_by_key:
                raise ValueError("drop record references an unknown source Query")
            expected_reason = (
                QueryDropReason.DUPLICATES_IDENTITY
                if collided.construction == QueryConstruction.IDENTITY
                else QueryDropReason.DUPLICATES_SYNTHETIC
            )
            if dropped.reason != expected_reason:
                raise ValueError("drop record reason does not match its collision")

        expected_set_id = query_set_id(
            self.model_dump(mode="json", exclude={"query_set_id"})
        )
        if self.query_set_id != expected_set_id:
            raise ValueError("Query set ID does not match its content")
        return self
