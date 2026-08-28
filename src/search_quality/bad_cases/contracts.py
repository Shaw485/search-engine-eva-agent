"""Strict, label-blind contracts for source-bounded Bad Case diagnostics."""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, StrictInt, StrictStr, model_validator

from search_quality.agent.contracts import StrictModel
from search_quality.data.contracts import canonical_json_sha256
from search_quality.data.splits import normalize_query
from search_quality.query_constructor.contracts import QueryConstruction
from search_quality.query_constructor.identity import sha256_text

SHA256_PATTERN = r"^[0-9a-f]{64}$"
QUERY_CASE_ID_PATTERN = r"^query-case-[0-9a-f]{12}$"
QUERY_SET_ID_PATTERN = r"^query-set-[0-9a-f]{12}$"
INDEX_ID_PATTERN = r"^catalog-baseline-v1-[0-9a-f]{12}$"
PRODUCT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
TOP_K = 10
EXPECTED_QUERY_COUNT = 59
EXPECTED_IDENTITY_COUNT = 20
EXPECTED_TRANSPOSITION_COUNT = 20
EXPECTED_REVERSAL_COUNT = 19
RUNNER_ID = "source-bounded-bad-case-runner-v1"
Sha256 = Annotated[StrictStr, Field(pattern=SHA256_PATTERN)]


class BadCaseCategory(StrEnum):
    ZERO_RESULT = "zero_result"
    SPELLING_SENSITIVE = "spelling_sensitive"
    ORDER_SENSITIVE = "order_sensitive"
    RANKING_INSTABILITY_NEEDS_JUDGMENT = "ranking_instability_needs_judgment"


CATEGORY_ORDER = (
    BadCaseCategory.ZERO_RESULT,
    BadCaseCategory.SPELLING_SENSITIVE,
    BadCaseCategory.ORDER_SENSITIVE,
    BadCaseCategory.RANKING_INSTABILITY_NEEDS_JUDGMENT,
)
_CONSTRUCTION_ORDER = {
    QueryConstruction.IDENTITY: 0,
    QueryConstruction.ADJACENT_TRANSPOSITION: 1,
    QueryConstruction.TOKEN_ORDER_REVERSAL: 2,
}


class DiagnosticReason(StrEnum):
    IDENTITY_ZERO_RESULT = "identity_zero_result"
    VARIANT_ZERO_RESULT = "variant_zero_result"
    VARIANT_RESULT_SET_CHANGED = "variant_result_set_changed"
    VARIANT_RANKING_CHANGED = "variant_ranking_changed"
    TOKEN_ORDER_RESULT_CHANGED = "token_order_result_changed"


class BadCaseObservation(StrictModel):
    """One completed Top-10 search with product identities hashed at rest."""

    case_id: StrictStr = Field(pattern=QUERY_CASE_ID_PATTERN)
    source_case_id: StrictStr = Field(pattern=QUERY_CASE_ID_PATTERN)
    source_query_id: StrictInt = Field(ge=1)
    query_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    construction: QueryConstruction
    returned_at_k: StrictInt = Field(ge=0, le=TOP_K)
    ordered_product_key_sha256s: list[Sha256] = Field(max_length=TOP_K)
    ordered_display_hit_sha256s: list[Sha256] = Field(max_length=TOP_K)
    ordered_results_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    result_set_sha256: StrictStr = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        keys = self.ordered_product_key_sha256s
        if self.returned_at_k != len(keys):
            raise ValueError("returned_at_k does not match the retained result keys")
        if len(keys) != len(set(keys)):
            raise ValueError("catalog result product keys must be unique")
        if len(self.ordered_display_hit_sha256s) != self.returned_at_k:
            raise ValueError("display-hit hashes do not match returned_at_k")
        if self.ordered_results_sha256 != ordered_results_sha256(keys):
            raise ValueError("ordered result digest does not match result keys")
        if self.result_set_sha256 != result_set_sha256(keys):
            raise ValueError("result-set digest does not match result keys")
        is_identity = self.construction == QueryConstruction.IDENTITY
        if is_identity != (self.case_id == self.source_case_id):
            raise ValueError("source case linkage does not match construction")
        return self


class BadCaseDiagnostic(StrictModel):
    """A behavioral candidate, never a relevance judgment."""

    case_id: StrictStr = Field(pattern=QUERY_CASE_ID_PATTERN)
    source_case_id: StrictStr = Field(pattern=QUERY_CASE_ID_PATTERN)
    source_query_id: StrictInt = Field(ge=1)
    construction: QueryConstruction
    categories: list[BadCaseCategory] = Field(min_length=1, max_length=4)
    reason_code: DiagnosticReason
    source_returned_at_k: StrictInt = Field(ge=0, le=TOP_K)
    variant_returned_at_k: StrictInt = Field(ge=0, le=TOP_K)
    overlap_at_k: StrictInt = Field(ge=0, le=TOP_K)

    @model_validator(mode="after")
    def validate_diagnostic(self) -> Self:
        if self.categories != [
            category for category in CATEGORY_ORDER if category in self.categories
        ] or len(self.categories) != len(set(self.categories)):
            raise ValueError("diagnostic categories must be unique and ordered")
        if self.overlap_at_k > min(
            self.source_returned_at_k,
            self.variant_returned_at_k,
        ):
            raise ValueError("result overlap exceeds one side of the comparison")
        return self


class BadCaseCategoryCounts(StrictModel):
    zero_result: StrictInt = Field(ge=0, le=EXPECTED_QUERY_COUNT)
    spelling_sensitive: StrictInt = Field(ge=0, le=EXPECTED_TRANSPOSITION_COUNT)
    order_sensitive: StrictInt = Field(ge=0, le=EXPECTED_REVERSAL_COUNT)
    ranking_instability_needs_judgment: StrictInt = Field(
        ge=0,
        le=EXPECTED_TRANSPOSITION_COUNT + EXPECTED_REVERSAL_COUNT,
    )


class BadCaseDiagnosticArtifact(StrictModel):
    """Complete deterministic evidence for exactly the 59 development cases."""

    schema_version: Literal["bad-case-diagnostic-v1"] = "bad-case-diagnostic-v1"
    diagnostic_id: StrictStr = Field(pattern=r"^bad-case-[0-9a-f]{12}$")
    runner_id: Literal["source-bounded-bad-case-runner-v1"] = RUNNER_ID
    completed: Literal[True] = True
    executor_revision: StrictStr = Field(pattern=r"^[0-9a-f]{40}$")
    query_set_id: StrictStr = Field(pattern=QUERY_SET_ID_PATTERN)
    query_set_revision: StrictStr = Field(pattern=r"^[0-9a-f]{40}$")
    query_source_contract_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    index_id: StrictStr = Field(pattern=INDEX_ID_PATTERN)
    index_build_revision: StrictStr = Field(pattern=r"^[0-9a-f]{40}$")
    index_config_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    index_source_sha256: StrictStr = Field(pattern=SHA256_PATTERN)
    catalog_product_count: StrictInt = Field(ge=1)
    search_strategy_id: Literal["sqlite-fts5-bm25"] = "sqlite-fts5-bm25"
    top_k: Literal[10] = TOP_K
    search_call_count: Literal[59] = EXPECTED_QUERY_COUNT
    operational_failure_count: Literal[0] = 0
    query_count: Literal[59] = EXPECTED_QUERY_COUNT
    original_count: Literal[20] = EXPECTED_IDENTITY_COUNT
    synthetic_count: Literal[39] = (
        EXPECTED_TRANSPOSITION_COUNT + EXPECTED_REVERSAL_COUNT
    )
    construction_counts: dict[
        Literal["identity", "adjacent_transposition", "token_order_reversal"],
        StrictInt,
    ]
    diagnostic_candidate_count: StrictInt = Field(ge=0, le=EXPECTED_QUERY_COUNT)
    category_counts: BadCaseCategoryCounts
    observations: list[BadCaseObservation] = Field(
        min_length=EXPECTED_QUERY_COUNT,
        max_length=EXPECTED_QUERY_COUNT,
    )
    diagnostics: list[BadCaseDiagnostic] = Field(max_length=EXPECTED_QUERY_COUNT)
    relevance_labels_used: Literal[False] = False
    relevance_metrics_computed: Literal[False] = False
    quality_metrics_computed: Literal[False] = False
    formal_evaluation_allowed: Literal[False] = False
    stage_drop_diagnostics_computed: Literal[False] = False
    locked_profiles_not_read: tuple[Literal["dev"], Literal["test"]] = (
        "dev",
        "test",
    )
    protected_profile_dispatch_count: Literal[0] = 0
    strategy_write_count: Literal[0] = 0
    raw_query_text_stored: Literal[False] = False
    raw_product_content_stored: Literal[False] = False
    limitations: tuple[
        Literal["synthetic_queries_are_unjudged"],
        Literal["diagnostics_do_not_claim_relevance_improvement"],
        Literal["development_smoke_is_not_final_evaluation"],
        Literal["single_stage_catalog_cannot_diagnose_stage_drop"],
        Literal["no_hard_worker_deadline_enforcement"],
    ]

    @model_validator(mode="after")
    def validate_complete_evidence(self) -> Self:
        expected_counts = {
            "identity": EXPECTED_IDENTITY_COUNT,
            "adjacent_transposition": EXPECTED_TRANSPOSITION_COUNT,
            "token_order_reversal": EXPECTED_REVERSAL_COUNT,
        }
        if self.construction_counts != expected_counts:
            raise ValueError("construction counts do not match the fixed Query set")
        observed_counts = Counter(item.construction.value for item in self.observations)
        if dict(observed_counts) != expected_counts:
            raise ValueError("observations do not cover the fixed Query constructions")
        ordered_observations = sorted(
            self.observations,
            key=lambda item: (
                item.source_query_id,
                _CONSTRUCTION_ORDER[item.construction],
                item.case_id,
            ),
        )
        if self.observations != ordered_observations:
            raise ValueError("observations must use deterministic Query order")
        by_case = {item.case_id: item for item in self.observations}
        if len(by_case) != EXPECTED_QUERY_COUNT:
            raise ValueError("every Query case must be observed exactly once")
        identities = {
            item.source_query_id: item
            for item in self.observations
            if item.construction == QueryConstruction.IDENTITY
        }
        if len(identities) != EXPECTED_IDENTITY_COUNT:
            raise ValueError("identity observations must be unique by source Query")
        for item in self.observations:
            source = identities.get(item.source_query_id)
            if source is None or item.source_case_id != source.case_id:
                raise ValueError("synthetic observation lacks its identity source")

        expected_diagnostics = []
        for item in self.observations:
            candidate = derive_diagnostic(item, identities[item.source_query_id])
            if candidate is not None:
                expected_diagnostics.append(candidate)
        if self.diagnostics != expected_diagnostics:
            raise ValueError("diagnostics do not match the completed observations")
        if self.diagnostic_candidate_count != len(expected_diagnostics):
            raise ValueError("diagnostic candidate count does not match diagnostics")
        expected_category_counts = Counter(
            category.value
            for diagnostic in expected_diagnostics
            for category in diagnostic.categories
        )
        if self.category_counts.model_dump(mode="json") != {
            category.value: expected_category_counts[category.value]
            for category in CATEGORY_ORDER
        }:
            raise ValueError("category counts do not match diagnostic candidates")
        expected_id = diagnostic_id(
            self.model_dump(mode="json", exclude={"diagnostic_id"})
        )
        if self.diagnostic_id != expected_id:
            raise ValueError("diagnostic ID does not match its evidence")
        return self


class BadCaseDisplayHit(StrictModel):
    product_id: StrictStr = Field(pattern=PRODUCT_ID_PATTERN)
    locale: StrictStr = Field(pattern=r"^[a-z][a-z0-9-]{1,15}$")
    title: StrictStr = Field(min_length=1, max_length=256)
    rank: StrictInt = Field(ge=1, le=3)


class BadCaseSample(StrictModel):
    """Strictly limited, owner-only display content kept out of artifacts/logs."""

    case_id: StrictStr = Field(pattern=QUERY_CASE_ID_PATTERN)
    source_case_id: StrictStr = Field(pattern=QUERY_CASE_ID_PATTERN)
    construction: QueryConstruction
    categories: list[BadCaseCategory] = Field(min_length=1, max_length=4)
    reason_code: DiagnosticReason
    query_text: StrictStr = Field(min_length=1, max_length=200)
    source_query_text: StrictStr = Field(min_length=1, max_length=200)
    source_returned_at_k: StrictInt = Field(ge=0, le=TOP_K)
    variant_returned_at_k: StrictInt = Field(ge=0, le=TOP_K)
    overlap_at_k: StrictInt = Field(ge=0, le=TOP_K)
    source_top_hits: list[BadCaseDisplayHit] = Field(max_length=3)
    variant_top_hits: list[BadCaseDisplayHit] = Field(max_length=3)

    @model_validator(mode="after")
    def validate_sample(self) -> Self:
        if self.categories != [
            category for category in CATEGORY_ORDER if category in self.categories
        ] or len(self.categories) != len(set(self.categories)):
            raise ValueError("sample categories must be unique and ordered")
        if self.overlap_at_k > min(
            self.source_returned_at_k,
            self.variant_returned_at_k,
        ):
            raise ValueError("sample overlap exceeds returned results")
        if self.construction == QueryConstruction.IDENTITY:
            if (
                self.case_id != self.source_case_id
                or self.query_text != self.source_query_text
            ):
                raise ValueError("identity sample must match its source")
        elif self.case_id == self.source_case_id:
            raise ValueError("synthetic sample must reference a different source case")
        if BadCaseCategory.ZERO_RESULT in self.categories and (
            self.variant_returned_at_k != 0
        ):
            raise ValueError("zero-result sample must have no variant results")
        if BadCaseCategory.SPELLING_SENSITIVE in self.categories and (
            self.construction != QueryConstruction.ADJACENT_TRANSPOSITION
        ):
            raise ValueError("spelling sensitivity requires a transposition")
        if BadCaseCategory.ORDER_SENSITIVE in self.categories and (
            self.construction != QueryConstruction.TOKEN_ORDER_REVERSAL
        ):
            raise ValueError("order sensitivity requires token reversal")
        if BadCaseCategory.RANKING_INSTABILITY_NEEDS_JUDGMENT in self.categories and (
            self.source_returned_at_k == 0 or self.variant_returned_at_k == 0
        ):
            raise ValueError("ranking instability requires results on both sides")
        _validate_display_hits(self.source_top_hits, self.source_returned_at_k)
        _validate_display_hits(self.variant_top_hits, self.variant_returned_at_k)
        return self


class BadCaseExecutionReceipt(StrictModel):
    schema_version: Literal["bad-case-execution-v1"] = "bad-case-execution-v1"
    execution_id: StrictStr = Field(pattern=r"^bad-case-execution-[0-9a-f]{32}$")
    diagnostic_id: StrictStr = Field(pattern=r"^bad-case-[0-9a-f]{12}$")
    query_set_id: StrictStr = Field(pattern=QUERY_SET_ID_PATTERN)
    index_id: StrictStr = Field(pattern=INDEX_ID_PATTERN)
    completed_query_count: Literal[59] = EXPECTED_QUERY_COUNT
    started_at_utc: AwareDatetime
    completed_at_utc: AwareDatetime
    duration_ms: float = Field(strict=True, ge=0.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_time_order(self) -> Self:
        if self.completed_at_utc < self.started_at_utc:
            raise ValueError("Bad Case execution completed before it started")
        return self


class BadCaseFailedAttempt(StrictModel):
    schema_version: Literal["bad-case-failed-attempt-v1"] = "bad-case-failed-attempt-v1"
    execution_id: StrictStr = Field(pattern=r"^bad-case-execution-[0-9a-f]{32}$")
    status: Literal["failed"] = "failed"
    failure_stage: Literal[
        "source_preflight",
        "query_construction",
        "catalog_search",
        "evidence_validation",
        "authority_check",
        "artifact_storage",
    ]
    completed_query_count: StrictInt = Field(ge=0, le=EXPECTED_QUERY_COUNT)
    error_code: StrictStr = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    started_at_utc: AwareDatetime
    completed_at_utc: AwareDatetime
    duration_ms: float = Field(strict=True, ge=0.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_time_order(self) -> Self:
        if self.completed_at_utc < self.started_at_utc:
            raise ValueError("Bad Case attempt completed before it started")
        return self


class BadCaseRun(StrictModel):
    artifact: BadCaseDiagnosticArtifact
    execution: BadCaseExecutionReceipt
    samples: list[BadCaseSample] = Field(max_length=12)
    artifact_path: StrictStr
    execution_path: StrictStr

    @model_validator(mode="after")
    def validate_samples_against_evidence(self) -> Self:
        diagnostics = {item.case_id: item for item in self.artifact.diagnostics}
        observations = {item.case_id: item for item in self.artifact.observations}
        if self.execution.diagnostic_id != self.artifact.diagnostic_id:
            raise ValueError("execution does not reference its diagnostic evidence")
        if self.execution.query_set_id != self.artifact.query_set_id:
            raise ValueError("execution does not reference its Query set")
        if self.execution.index_id != self.artifact.index_id:
            raise ValueError("execution does not reference its catalog index")
        if len({item.case_id for item in self.samples}) != len(self.samples):
            raise ValueError("display samples must be unique")
        for sample in self.samples:
            diagnostic = diagnostics.get(sample.case_id)
            observation = observations.get(sample.case_id)
            source = observations.get(sample.source_case_id)
            if diagnostic is None or observation is None or source is None:
                raise ValueError("display sample lacks diagnostic evidence")
            compared = {
                "case_id": sample.case_id,
                "source_case_id": sample.source_case_id,
                "construction": sample.construction,
                "categories": sample.categories,
                "reason_code": sample.reason_code,
                "source_returned_at_k": sample.source_returned_at_k,
                "variant_returned_at_k": sample.variant_returned_at_k,
                "overlap_at_k": sample.overlap_at_k,
            }
            if compared != diagnostic.model_dump(
                include=set(compared),
            ):
                raise ValueError("display sample contradicts its diagnostic")
            if (
                sha256_text(normalize_query(sample.query_text))
                != observation.query_sha256
            ):
                raise ValueError("display Query does not match diagnostic evidence")
            if (
                sha256_text(normalize_query(sample.source_query_text))
                != source.query_sha256
            ):
                raise ValueError("display source Query does not match evidence")
            _validate_sample_hits_against_observation(
                sample.source_top_hits,
                source,
            )
            _validate_sample_hits_against_observation(
                sample.variant_top_hits,
                observation,
            )
        return self


def ordered_results_sha256(keys: list[str]) -> str:
    return canonical_json_sha256(keys)


def result_set_sha256(keys: list[str]) -> str:
    return canonical_json_sha256(sorted(keys))


def product_key_sha256(*, locale: str, product_id: str) -> str:
    return canonical_json_sha256([locale, product_id])


def display_hit_sha256(
    *,
    locale: str,
    product_id: str,
    title: str,
    rank: int,
) -> str:
    return canonical_json_sha256(
        {
            "locale": locale,
            "product_id": product_id,
            "rank": rank,
            "title": title,
        }
    )


def diagnostic_id(payload_without_id: dict[str, object]) -> str:
    return f"bad-case-{canonical_json_sha256(payload_without_id)[:12]}"


def derive_diagnostic(
    observation: BadCaseObservation,
    source: BadCaseObservation,
) -> BadCaseDiagnostic | None:
    """Derive only observable behavior; never infer relevance or root cause."""

    if source.construction != QueryConstruction.IDENTITY:
        raise ValueError("diagnostic source must be an identity observation")
    current_keys = observation.ordered_product_key_sha256s
    source_keys = source.ordered_product_key_sha256s
    changed = current_keys != source_keys
    categories: list[BadCaseCategory] = []
    if observation.returned_at_k == 0:
        categories.append(BadCaseCategory.ZERO_RESULT)
    if observation.construction == QueryConstruction.ADJACENT_TRANSPOSITION and changed:
        categories.append(BadCaseCategory.SPELLING_SENSITIVE)
    if observation.construction == QueryConstruction.TOKEN_ORDER_REVERSAL and changed:
        categories.append(BadCaseCategory.ORDER_SENSITIVE)
    if (
        observation.construction != QueryConstruction.IDENTITY
        and source.returned_at_k > 0
        and observation.returned_at_k > 0
        and changed
    ):
        categories.append(BadCaseCategory.RANKING_INSTABILITY_NEEDS_JUDGMENT)
    categories = [category for category in CATEGORY_ORDER if category in categories]
    if not categories:
        return None

    if observation.construction == QueryConstruction.IDENTITY:
        reason = DiagnosticReason.IDENTITY_ZERO_RESULT
    elif observation.construction == QueryConstruction.TOKEN_ORDER_REVERSAL and changed:
        reason = DiagnosticReason.TOKEN_ORDER_RESULT_CHANGED
    elif observation.returned_at_k == 0:
        reason = DiagnosticReason.VARIANT_ZERO_RESULT
    elif set(current_keys) != set(source_keys):
        reason = DiagnosticReason.VARIANT_RESULT_SET_CHANGED
    else:
        reason = DiagnosticReason.VARIANT_RANKING_CHANGED
    return BadCaseDiagnostic(
        case_id=observation.case_id,
        source_case_id=source.case_id,
        source_query_id=observation.source_query_id,
        construction=observation.construction,
        categories=categories,
        reason_code=reason,
        source_returned_at_k=source.returned_at_k,
        variant_returned_at_k=observation.returned_at_k,
        overlap_at_k=len(set(source_keys) & set(current_keys)),
    )


def _validate_display_hits(hits: list[BadCaseDisplayHit], returned_at_k: int) -> None:
    if len(hits) > returned_at_k:
        raise ValueError("display hits exceed returned_at_k")
    if [item.rank for item in hits] != list(range(1, len(hits) + 1)):
        raise ValueError("display hit ranks must be contiguous and ordered")
    if len({(item.locale, item.product_id) for item in hits}) != len(hits):
        raise ValueError("display hit product keys must be unique")


def _validate_sample_hits_against_observation(
    hits: list[BadCaseDisplayHit],
    observation: BadCaseObservation,
) -> None:
    observed_product_keys = [
        product_key_sha256(locale=item.locale, product_id=item.product_id)
        for item in hits
    ]
    if observed_product_keys != observation.ordered_product_key_sha256s[: len(hits)]:
        raise ValueError("display product keys do not match diagnostic evidence")
    observed = [
        display_hit_sha256(
            locale=item.locale,
            product_id=item.product_id,
            title=item.title,
            rank=item.rank,
        )
        for item in hits
    ]
    if observed != observation.ordered_display_hit_sha256s[: len(hits)]:
        raise ValueError("display hits do not match diagnostic evidence")
