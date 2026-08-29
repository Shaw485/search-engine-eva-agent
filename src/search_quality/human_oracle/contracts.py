"""Strict, content-addressed contracts for the Human Diagnostic Oracle.

The Oracle records an owner's diagnostic judgments.  It deliberately creates
neither ESCI product labels nor a formal search-quality conclusion.
"""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, StrictInt, StrictStr, model_validator

from search_quality.agent.contracts import StrictModel
from search_quality.bad_cases.contracts import (
    BadCaseCategory,
    BadCaseDisplayHit,
    DiagnosticReason,
)
from search_quality.data.contracts import canonical_json_sha256
from search_quality.query_constructor.contracts import QueryConstruction

SHA256_PATTERN = r"^[0-9a-f]{64}$"
QUERY_CASE_ID_PATTERN = r"^query-case-[0-9a-f]{12}$"
BAD_CASE_ID_PATTERN = r"^bad-case-[0-9a-f]{12}$"
QUERY_SET_ID_PATTERN = r"^query-set-[0-9a-f]{12}$"
INDEX_ID_PATTERN = r"^catalog-baseline-v1-[0-9a-f]{12}$"
ORACLE_BATCH_ID_PATTERN = r"^oracle-batch-[0-9a-f]{12}$"
ORACLE_UNIT_ID_PATTERN = r"^oracle-unit-[0-9a-f]{12}$"
ORACLE_INTENT_ID_PATTERN = r"^oracle-intent-[0-9a-f]{12}$"
ORACLE_BEHAVIOR_ID_PATTERN = r"^oracle-behavior-[0-9a-f]{12}$"
ORACLE_SEAL_ID_PATTERN = r"^human-oracle-[0-9a-f]{12}$"
CLIENT_ACTION_ID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
SAFE_KEY_ID_PATTERN = r"^[a-z][a-z0-9-]{0,63}$"

Sha256 = Annotated[StrictStr, Field(pattern=SHA256_PATTERN)]
CaseId = Annotated[StrictStr, Field(pattern=QUERY_CASE_ID_PATTERN)]
ClientActionId = Annotated[StrictStr, Field(pattern=CLIENT_ACTION_ID_PATTERN)]
IntentAnnotationId = Annotated[StrictStr, Field(pattern=ORACLE_INTENT_ID_PATTERN)]
BehaviorAnnotationId = Annotated[StrictStr, Field(pattern=ORACLE_BEHAVIOR_ID_PATTERN)]

EXPECTED_CANDIDATE_COUNT = 40
EXPECTED_CLUSTER_COUNT = 20
EXPECTED_SYNTHETIC_CANDIDATE_COUNT = 30
EXPECTED_SOURCE_ZERO_CLUSTERS = 10
EXPECTED_SOURCE_NONZERO_VARIANT_ZERO_CLUSTERS = 10
ORACLE_POLICY_ID = "source-cluster-census-up-to-24-v1"
ORACLE_CLUSTER_CAP = 24

CONSTRUCTION_ORDER = {
    QueryConstruction.IDENTITY: 0,
    QueryConstruction.ADJACENT_TRANSPOSITION: 1,
    QueryConstruction.TOKEN_ORDER_REVERSAL: 2,
}


class OracleStratum(StrEnum):
    SOURCE_ZERO_CLUSTER = "source_zero_cluster"
    SOURCE_NONZERO_VARIANT_ZERO = "source_nonzero_variant_zero"


class IntentJudgment(StrEnum):
    EQUIVALENT = "equivalent"
    NOT_EQUIVALENT = "not_equivalent"
    UNCERTAIN = "uncertain"


class IntentReason(StrEnum):
    SAME_PRODUCT_INTENT = "same_product_intent"
    OBVIOUS_TYPO_SAME_INTENT = "obvious_typo_same_intent"
    MEANING_CHANGED = "meaning_changed"
    QUERY_BECAME_UNINTERPRETABLE = "query_became_uninterpretable"
    AMBIGUOUS_INTENT = "ambiguous_intent"
    INSUFFICIENT_CONTEXT = "insufficient_context"


INTENT_REASONS_BY_JUDGMENT = {
    IntentJudgment.EQUIVALENT: frozenset(
        {
            IntentReason.SAME_PRODUCT_INTENT,
            IntentReason.OBVIOUS_TYPO_SAME_INTENT,
        }
    ),
    IntentJudgment.NOT_EQUIVALENT: frozenset(
        {
            IntentReason.MEANING_CHANGED,
            IntentReason.QUERY_BECAME_UNINTERPRETABLE,
        }
    ),
    IntentJudgment.UNCERTAIN: frozenset(
        {
            IntentReason.AMBIGUOUS_INTENT,
            IntentReason.INSUFFICIENT_CONTEXT,
        }
    ),
}


class BehaviorJudgment(StrEnum):
    CONFIRMED_ISSUE = "confirmed_issue"
    ACCEPTABLE = "acceptable"
    UNCERTAIN = "uncertain"


class BehaviorReason(StrEnum):
    OWNER_CATALOG_EXPECTATION = "owner_catalog_expectation"
    EQUIVALENT_INTENT_SHOULD_PRESERVE_BEHAVIOR = (
        "equivalent_intent_should_preserve_behavior"
    )
    INTENT_NOT_EQUIVALENT = "intent_not_equivalent"
    BEHAVIOR_IS_EXPECTED = "behavior_is_expected"
    CATALOG_COVERAGE_UNKNOWN = "catalog_coverage_unknown"
    INSUFFICIENT_RESULT_EVIDENCE = "insufficient_result_evidence"
    INSUFFICIENT_DOMAIN_KNOWLEDGE = "insufficient_domain_knowledge"


BEHAVIOR_REASONS_BY_JUDGMENT = {
    BehaviorJudgment.CONFIRMED_ISSUE: frozenset(
        {
            BehaviorReason.OWNER_CATALOG_EXPECTATION,
            BehaviorReason.EQUIVALENT_INTENT_SHOULD_PRESERVE_BEHAVIOR,
        }
    ),
    BehaviorJudgment.ACCEPTABLE: frozenset(
        {
            BehaviorReason.INTENT_NOT_EQUIVALENT,
            BehaviorReason.BEHAVIOR_IS_EXPECTED,
        }
    ),
    BehaviorJudgment.UNCERTAIN: frozenset(
        {
            BehaviorReason.CATALOG_COVERAGE_UNKNOWN,
            BehaviorReason.INSUFFICIENT_RESULT_EVIDENCE,
            BehaviorReason.INSUFFICIENT_DOMAIN_KNOWLEDGE,
        }
    ),
}


class OracleBatchStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    READY_TO_SEAL = "ready_to_seal"
    SEALED = "sealed"


class OracleActor(StrictModel):
    """Pseudonymous owner identity; a raw principal is never accepted."""

    principal_hmac_sha256: Sha256
    actor_key_id: StrictStr = Field(pattern=SAFE_KEY_ID_PATTERN)
    annotator_role: Literal["owner"] = "owner"
    decision_origin: Literal["human_owner"] = "human_owner"


class OracleCandidate(StrictModel):
    case_id: CaseId
    source_case_id: CaseId
    construction: QueryConstruction
    categories: list[BadCaseCategory] = Field(min_length=1, max_length=4)
    reason_code: DiagnosticReason
    source_returned_at_k: StrictInt = Field(ge=0, le=10)
    variant_returned_at_k: StrictInt = Field(ge=0, le=10)
    overlap_at_k: StrictInt = Field(ge=0, le=10)
    source_observation_sha256: Sha256
    variant_observation_sha256: Sha256
    source_query_sha256: Sha256
    variant_query_sha256: Sha256
    case_context_sha256: Sha256
    intent_context_sha256: Sha256 | None
    behavior_context_sha256: Sha256

    @model_validator(mode="after")
    def validate_candidate_context(self) -> Self:
        if self.overlap_at_k > min(
            self.source_returned_at_k,
            self.variant_returned_at_k,
        ):
            raise ValueError("Oracle candidate overlap exceeds observed results")
        is_identity = self.construction == QueryConstruction.IDENTITY
        if is_identity != (self.case_id == self.source_case_id):
            raise ValueError("Oracle candidate source linkage is invalid")
        if is_identity:
            if self.intent_context_sha256 is not None:
                raise ValueError("identity candidate cannot have an intent context")
            if self.source_query_sha256 != self.variant_query_sha256:
                raise ValueError("identity candidate Query hashes must match")
        elif self.intent_context_sha256 is None:
            raise ValueError("synthetic candidate requires an intent context")
        return self


class OracleReviewUnit(StrictModel):
    unit_id: StrictStr = Field(pattern=ORACLE_UNIT_ID_PATTERN)
    source_case_id: CaseId
    source_query_id: StrictInt = Field(ge=1)
    stratum: OracleStratum
    candidates: list[OracleCandidate] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def validate_unit(self) -> Self:
        if any(item.source_case_id != self.source_case_id for item in self.candidates):
            raise ValueError("Oracle unit candidates must share one source case")
        ordered = sorted(
            self.candidates,
            key=lambda item: (CONSTRUCTION_ORDER[item.construction], item.case_id),
        )
        if ordered != self.candidates:
            raise ValueError("Oracle unit candidates must use deterministic order")
        if len({item.case_id for item in self.candidates}) != len(self.candidates):
            raise ValueError("Oracle unit candidate IDs must be unique")
        if self.stratum == OracleStratum.SOURCE_ZERO_CLUSTER:
            expected = [
                QueryConstruction.IDENTITY,
                QueryConstruction.ADJACENT_TRANSPOSITION,
                QueryConstruction.TOKEN_ORDER_REVERSAL,
            ]
            if [item.construction for item in self.candidates] != expected:
                raise ValueError("source-zero unit must contain all three cases")
            for item in self.candidates:
                expected_reason = (
                    DiagnosticReason.IDENTITY_ZERO_RESULT
                    if item.construction == QueryConstruction.IDENTITY
                    else DiagnosticReason.VARIANT_ZERO_RESULT
                )
                if (
                    item.categories != [BadCaseCategory.ZERO_RESULT]
                    or item.reason_code != expected_reason
                    or item.source_returned_at_k != 0
                    or item.variant_returned_at_k != 0
                    or item.overlap_at_k != 0
                ):
                    raise ValueError("source-zero unit has an unexpected signal")
        elif self.stratum == OracleStratum.SOURCE_NONZERO_VARIANT_ZERO:
            if len(self.candidates) != 1:
                raise ValueError("source-nonzero unit must contain one case")
            item = self.candidates[0]
            if (
                item.construction != QueryConstruction.ADJACENT_TRANSPOSITION
                or item.case_id == item.source_case_id
                or item.categories
                != [
                    BadCaseCategory.ZERO_RESULT,
                    BadCaseCategory.SPELLING_SENSITIVE,
                ]
                or item.reason_code != DiagnosticReason.VARIANT_ZERO_RESULT
                or item.source_returned_at_k <= 0
                or item.variant_returned_at_k != 0
                or item.overlap_at_k != 0
            ):
                raise ValueError("source-nonzero unit has an unexpected signal")
        expected_id = oracle_unit_id(self.model_dump(mode="json", exclude={"unit_id"}))
        if self.unit_id != expected_id:
            raise ValueError("Oracle unit ID does not match its content")
        return self


class OracleBatchArtifact(StrictModel):
    schema_version: Literal["human-oracle-batch-v1"] = "human-oracle-batch-v1"
    oracle_batch_id: StrictStr = Field(pattern=ORACLE_BATCH_ID_PATTERN)
    sampling_policy_id: Literal["source-cluster-census-up-to-24-v1"] = ORACLE_POLICY_ID
    diagnostic_id: StrictStr = Field(pattern=BAD_CASE_ID_PATTERN)
    query_set_id: StrictStr = Field(pattern=QUERY_SET_ID_PATTERN)
    index_id: StrictStr = Field(pattern=INDEX_ID_PATTERN)
    search_strategy_id: Literal["sqlite-fts5-bm25"] = "sqlite-fts5-bm25"
    top_k: Literal[10] = 10
    population_candidate_count: Literal[40] = EXPECTED_CANDIDATE_COUNT
    population_cluster_count: Literal[20] = EXPECTED_CLUSTER_COUNT
    selected_candidate_count: Literal[40] = EXPECTED_CANDIDATE_COUNT
    selected_cluster_count: Literal[20] = EXPECTED_CLUSTER_COUNT
    synthetic_intent_candidate_count: Literal[30] = EXPECTED_SYNTHETIC_CANDIDATE_COUNT
    stratum_counts: dict[
        Literal["source_zero_cluster", "source_nonzero_variant_zero"],
        StrictInt,
    ]
    selection_mode: Literal["cluster_census"] = "cluster_census"
    cluster_cap: Literal[24] = ORACLE_CLUSTER_CAP
    sampling_fraction: Literal[1.0] = 1.0
    units: list[OracleReviewUnit] = Field(
        min_length=EXPECTED_CLUSTER_COUNT,
        max_length=EXPECTED_CLUSTER_COUNT,
    )
    raw_query_text_stored: Literal[False] = False
    raw_product_content_stored: Literal[False] = False
    source_labels_inherited: Literal[False] = False
    product_relevance_labels_created: Literal[0] = 0
    formal_evaluation_allowed: Literal[False] = False
    quality_conclusion_allowed: Literal[False] = False
    mechanism_smoke_only: Literal[True] = True
    root_cause_claimed: Literal[False] = False
    strategy_write_count: Literal[0] = 0

    @model_validator(mode="after")
    def validate_batch(self) -> Self:
        if self.stratum_counts != {
            "source_zero_cluster": EXPECTED_SOURCE_ZERO_CLUSTERS,
            "source_nonzero_variant_zero": (
                EXPECTED_SOURCE_NONZERO_VARIANT_ZERO_CLUSTERS
            ),
        }:
            raise ValueError("Oracle batch strata do not match the fixed evidence")
        ordered = sorted(
            self.units,
            key=lambda item: (item.source_query_id, item.source_case_id),
        )
        if self.units != ordered:
            raise ValueError("Oracle units must use deterministic source order")
        if len({item.unit_id for item in self.units}) != EXPECTED_CLUSTER_COUNT:
            raise ValueError("Oracle unit IDs must be unique")
        cases = [
            candidate.case_id for unit in self.units for candidate in unit.candidates
        ]
        if len(cases) != EXPECTED_CANDIDATE_COUNT or len(set(cases)) != len(cases):
            raise ValueError("Oracle batch must cover every candidate exactly once")
        synthetic = sum(
            candidate.construction != QueryConstruction.IDENTITY
            for unit in self.units
            for candidate in unit.candidates
        )
        if synthetic != EXPECTED_SYNTHETIC_CANDIDATE_COUNT:
            raise ValueError("Oracle batch synthetic intent count is invalid")
        if Counter(item.stratum.value for item in self.units) != Counter(
            self.stratum_counts
        ):
            raise ValueError("Oracle unit strata do not match their counts")
        expected_id = oracle_batch_id(
            self.model_dump(mode="json", exclude={"oracle_batch_id"})
        )
        if self.oracle_batch_id != expected_id:
            raise ValueError("Oracle batch ID does not match its content")
        return self


class IntentSubmission(StrictModel):
    oracle_batch_id: StrictStr = Field(pattern=ORACLE_BATCH_ID_PATTERN)
    unit_id: StrictStr = Field(pattern=ORACLE_UNIT_ID_PATTERN)
    case_id: CaseId
    presentation_context_sha256: Sha256
    judgment: IntentJudgment
    reason_code: IntentReason
    actor: OracleActor
    client_action_id: ClientActionId
    expected_previous_annotation_id: StrictStr | None = Field(
        default=None,
        pattern=ORACLE_INTENT_ID_PATTERN,
    )

    @model_validator(mode="after")
    def validate_reason(self) -> Self:
        if self.reason_code not in INTENT_REASONS_BY_JUDGMENT[self.judgment]:
            raise ValueError("intent reason does not match its judgment")
        return self


class IntentAnnotation(StrictModel):
    schema_version: Literal["human-oracle-intent-v1"] = "human-oracle-intent-v1"
    intent_annotation_id: StrictStr = Field(pattern=ORACLE_INTENT_ID_PATTERN)
    oracle_batch_id: StrictStr = Field(pattern=ORACLE_BATCH_ID_PATTERN)
    diagnostic_id: StrictStr = Field(pattern=BAD_CASE_ID_PATTERN)
    unit_id: StrictStr = Field(pattern=ORACLE_UNIT_ID_PATTERN)
    case_id: CaseId
    source_case_id: CaseId
    construction: Literal[
        QueryConstruction.ADJACENT_TRANSPOSITION,
        QueryConstruction.TOKEN_ORDER_REVERSAL,
    ]
    case_context_sha256: Sha256
    presentation_context_sha256: Sha256
    judgment: IntentJudgment
    reason_code: IntentReason
    oracle_ui_withheld_result_evidence: Literal[True] = True
    prior_external_exposure_uncontrolled: Literal[True] = True
    source_labels_inherited: Literal[False] = False
    product_relevance_labels_created: Literal[0] = 0
    actor: OracleActor
    client_action_id: ClientActionId
    supersedes_annotation_id: StrictStr | None = Field(
        default=None,
        pattern=ORACLE_INTENT_ID_PATTERN,
    )
    submitted_at_utc: AwareDatetime

    @model_validator(mode="after")
    def validate_annotation(self) -> Self:
        if self.reason_code not in INTENT_REASONS_BY_JUDGMENT[self.judgment]:
            raise ValueError("intent reason does not match its judgment")
        expected_id = oracle_intent_id(
            self.model_dump(mode="json", exclude={"intent_annotation_id"})
        )
        if self.intent_annotation_id != expected_id:
            raise ValueError("intent annotation ID does not match its content")
        return self


class BehaviorSubmission(StrictModel):
    oracle_batch_id: StrictStr = Field(pattern=ORACLE_BATCH_ID_PATTERN)
    unit_id: StrictStr = Field(pattern=ORACLE_UNIT_ID_PATTERN)
    case_id: CaseId
    presentation_context_sha256: Sha256
    judgment: BehaviorJudgment
    reason_code: BehaviorReason
    intent_annotation_id: StrictStr | None = Field(
        default=None,
        pattern=ORACLE_INTENT_ID_PATTERN,
    )
    actor: OracleActor
    client_action_id: ClientActionId
    expected_previous_annotation_id: StrictStr | None = Field(
        default=None,
        pattern=ORACLE_BEHAVIOR_ID_PATTERN,
    )

    @model_validator(mode="after")
    def validate_reason(self) -> Self:
        if self.reason_code not in BEHAVIOR_REASONS_BY_JUDGMENT[self.judgment]:
            raise ValueError("behavior reason does not match its judgment")
        return self


class BehaviorAnnotation(StrictModel):
    schema_version: Literal["human-oracle-behavior-v1"] = "human-oracle-behavior-v1"
    behavior_annotation_id: StrictStr = Field(pattern=ORACLE_BEHAVIOR_ID_PATTERN)
    oracle_batch_id: StrictStr = Field(pattern=ORACLE_BATCH_ID_PATTERN)
    diagnostic_id: StrictStr = Field(pattern=BAD_CASE_ID_PATTERN)
    unit_id: StrictStr = Field(pattern=ORACLE_UNIT_ID_PATTERN)
    case_id: CaseId
    source_case_id: CaseId
    construction: QueryConstruction
    case_context_sha256: Sha256
    presentation_context_sha256: Sha256
    intent_annotation_id: StrictStr | None = Field(
        default=None,
        pattern=ORACLE_INTENT_ID_PATTERN,
    )
    judgment: BehaviorJudgment
    reason_code: BehaviorReason
    source_reference_scope: Literal["identity_only_not_variant_label"] = (
        "identity_only_not_variant_label"
    )
    source_labels_inherited: Literal[False] = False
    product_relevance_labels_created: Literal[0] = 0
    root_cause_claimed: Literal[False] = False
    actor: OracleActor
    client_action_id: ClientActionId
    supersedes_annotation_id: StrictStr | None = Field(
        default=None,
        pattern=ORACLE_BEHAVIOR_ID_PATTERN,
    )
    submitted_at_utc: AwareDatetime

    @model_validator(mode="after")
    def validate_annotation(self) -> Self:
        if self.reason_code not in BEHAVIOR_REASONS_BY_JUDGMENT[self.judgment]:
            raise ValueError("behavior reason does not match its judgment")
        is_identity = self.construction == QueryConstruction.IDENTITY
        if is_identity != (self.intent_annotation_id is None):
            raise ValueError("behavior intent linkage does not match construction")
        expected_id = oracle_behavior_id(
            self.model_dump(mode="json", exclude={"behavior_annotation_id"})
        )
        if self.behavior_annotation_id != expected_id:
            raise ValueError("behavior annotation ID does not match its content")
        return self


class JudgmentCounts(StrictModel):
    confirmed_issue: StrictInt = Field(ge=0, le=EXPECTED_CANDIDATE_COUNT)
    acceptable: StrictInt = Field(ge=0, le=EXPECTED_CANDIDATE_COUNT)
    uncertain: StrictInt = Field(ge=0, le=EXPECTED_CANDIDATE_COUNT)


class IntentCounts(StrictModel):
    equivalent: StrictInt = Field(ge=0, le=EXPECTED_SYNTHETIC_CANDIDATE_COUNT)
    not_equivalent: StrictInt = Field(
        ge=0,
        le=EXPECTED_SYNTHETIC_CANDIDATE_COUNT,
    )
    uncertain: StrictInt = Field(ge=0, le=EXPECTED_SYNTHETIC_CANDIDATE_COUNT)


class SealSubmission(StrictModel):
    oracle_batch_id: StrictStr = Field(pattern=ORACLE_BATCH_ID_PATTERN)
    actor: OracleActor
    client_action_id: ClientActionId


class HumanOracleArtifact(StrictModel):
    schema_version: Literal["human-diagnostic-oracle-v1"] = "human-diagnostic-oracle-v1"
    oracle_id: StrictStr = Field(pattern=ORACLE_SEAL_ID_PATTERN)
    oracle_batch_id: StrictStr = Field(pattern=ORACLE_BATCH_ID_PATTERN)
    diagnostic_id: StrictStr = Field(pattern=BAD_CASE_ID_PATTERN)
    active_intent_annotation_ids: list[IntentAnnotationId] = Field(
        min_length=EXPECTED_SYNTHETIC_CANDIDATE_COUNT,
        max_length=EXPECTED_SYNTHETIC_CANDIDATE_COUNT,
    )
    active_behavior_annotation_ids: list[BehaviorAnnotationId] = Field(
        min_length=EXPECTED_CANDIDATE_COUNT,
        max_length=EXPECTED_CANDIDATE_COUNT,
    )
    synthetic_intent_annotation_count: Literal[30] = EXPECTED_SYNTHETIC_CANDIDATE_COUNT
    behavior_annotation_count: Literal[40] = EXPECTED_CANDIDATE_COUNT
    intent_counts: IntentCounts
    behavior_counts: JudgmentCounts
    counts_by_construction: dict[
        Literal["identity", "adjacent_transposition", "token_order_reversal"],
        JudgmentCounts,
    ]
    counts_by_stratum: dict[
        Literal["source_zero_cluster", "source_nonzero_variant_zero"],
        JudgmentCounts,
    ]
    all_selected_cases_independently_annotated: Literal[True] = True
    synthetic_label_inheritance_count: Literal[0] = 0
    product_relevance_labels_created: Literal[0] = 0
    formal_evaluation_allowed: Literal[False] = False
    quality_conclusion_allowed: Literal[False] = False
    mechanism_smoke_only: Literal[True] = True
    root_cause_claimed: Literal[False] = False
    strategy_write_count: Literal[0] = 0
    sealed_by: OracleActor
    client_action_id: ClientActionId
    sealed_at_utc: AwareDatetime
    limitations: tuple[
        Literal["single_owner_no_inter_annotator_agreement"],
        Literal["selection_conditioned_development_set"],
        Literal["synthetic_product_relevance_remains_unjudged"],
        Literal["prior_exposure_not_controlled"],
        Literal["diagnostic_judgment_is_not_root_cause"],
    ] = (
        "single_owner_no_inter_annotator_agreement",
        "selection_conditioned_development_set",
        "synthetic_product_relevance_remains_unjudged",
        "prior_exposure_not_controlled",
        "diagnostic_judgment_is_not_root_cause",
    )

    @model_validator(mode="after")
    def validate_seal(self) -> Self:
        if len(set(self.active_intent_annotation_ids)) != len(
            self.active_intent_annotation_ids
        ):
            raise ValueError("active intent annotation IDs must be unique")
        if len(set(self.active_behavior_annotation_ids)) != len(
            self.active_behavior_annotation_ids
        ):
            raise ValueError("active behavior annotation IDs must be unique")
        if sum(self.intent_counts.model_dump().values()) != (
            EXPECTED_SYNTHETIC_CANDIDATE_COUNT
        ):
            raise ValueError("sealed intent counts are incomplete")
        if sum(self.behavior_counts.model_dump().values()) != EXPECTED_CANDIDATE_COUNT:
            raise ValueError("sealed behavior counts are incomplete")
        for expected_keys, observed in (
            (
                {"identity", "adjacent_transposition", "token_order_reversal"},
                self.counts_by_construction,
            ),
            (
                {"source_zero_cluster", "source_nonzero_variant_zero"},
                self.counts_by_stratum,
            ),
        ):
            if set(observed) != expected_keys:
                raise ValueError("sealed grouped counts have invalid keys")
            total = sum(sum(item.model_dump().values()) for item in observed.values())
            if total != EXPECTED_CANDIDATE_COUNT:
                raise ValueError("sealed grouped counts are incomplete")
        expected_construction_totals = {
            "identity": 10,
            "adjacent_transposition": 20,
            "token_order_reversal": 10,
        }
        for key, expected in expected_construction_totals.items():
            if sum(self.counts_by_construction[key].model_dump().values()) != expected:
                raise ValueError("sealed construction count is invalid")
        expected_stratum_totals = {
            "source_zero_cluster": 30,
            "source_nonzero_variant_zero": 10,
        }
        for key, expected in expected_stratum_totals.items():
            if sum(self.counts_by_stratum[key].model_dump().values()) != expected:
                raise ValueError("sealed stratum count is invalid")
        expected_id = human_oracle_id(
            self.model_dump(mode="json", exclude={"oracle_id"})
        )
        if self.oracle_id != expected_id:
            raise ValueError("Human Oracle ID does not match its content")
        return self


class OracleBatchProjection(StrictModel):
    oracle_batch_id: StrictStr = Field(pattern=ORACLE_BATCH_ID_PATTERN)
    status: OracleBatchStatus
    active_intent_annotation_count: StrictInt = Field(
        ge=0,
        le=EXPECTED_SYNTHETIC_CANDIDATE_COUNT,
    )
    active_behavior_annotation_count: StrictInt = Field(
        ge=0,
        le=EXPECTED_CANDIDATE_COUNT,
    )
    invalidated_behavior_annotation_count: StrictInt = Field(ge=0)
    sealed_oracle_id: StrictStr | None = Field(
        default=None,
        pattern=ORACLE_SEAL_ID_PATTERN,
    )


class OracleCaseReviewState(StrictModel):
    unit_id: StrictStr = Field(pattern=ORACLE_UNIT_ID_PATTERN)
    case_id: CaseId
    construction: QueryConstruction
    active_intent_annotation_id: StrictStr | None = Field(
        default=None,
        pattern=ORACLE_INTENT_ID_PATTERN,
    )
    active_intent_judgment: IntentJudgment | None = None
    expected_previous_intent_annotation_id: StrictStr | None = Field(
        default=None,
        pattern=ORACLE_INTENT_ID_PATTERN,
    )
    expected_previous_behavior_annotation_id: StrictStr | None = Field(
        default=None,
        pattern=ORACLE_BEHAVIOR_ID_PATTERN,
    )
    active_behavior_annotation_id: StrictStr | None = Field(
        default=None,
        pattern=ORACLE_BEHAVIOR_ID_PATTERN,
    )
    active_behavior_judgment: BehaviorJudgment | None = None
    behavior_invalidated_by_intent_change: bool

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if (self.active_intent_annotation_id is None) != (
            self.active_intent_judgment is None
        ):
            raise ValueError("intent review-state ID and judgment must align")
        if (
            self.expected_previous_intent_annotation_id
            != self.active_intent_annotation_id
        ):
            raise ValueError("intent review-state CAS head is inconsistent")
        if (self.active_behavior_annotation_id is None) != (
            self.active_behavior_judgment is None
        ):
            raise ValueError("behavior review-state ID and judgment must align")
        if self.behavior_invalidated_by_intent_change and (
            self.expected_previous_behavior_annotation_id is None
            or self.active_behavior_annotation_id is not None
        ):
            raise ValueError("invalidated behavior state is inconsistent")
        return self


class OracleReviewState(StrictModel):
    schema_version: Literal["human-oracle-review-state-v1"] = (
        "human-oracle-review-state-v1"
    )
    oracle_batch_id: StrictStr = Field(pattern=ORACLE_BATCH_ID_PATTERN)
    projection: OracleBatchProjection
    cases: list[OracleCaseReviewState] = Field(
        min_length=EXPECTED_CANDIDATE_COUNT,
        max_length=EXPECTED_CANDIDATE_COUNT,
    )

    @model_validator(mode="after")
    def validate_review_state(self) -> Self:
        if self.projection.oracle_batch_id != self.oracle_batch_id:
            raise ValueError("review-state projection belongs to another batch")
        if len({item.case_id for item in self.cases}) != EXPECTED_CANDIDATE_COUNT:
            raise ValueError("review-state cases must be unique")
        if any(
            item.construction == QueryConstruction.IDENTITY
            and item.active_intent_annotation_id is not None
            for item in self.cases
        ):
            raise ValueError("identity review state cannot have an intent annotation")
        return self


class OracleIntentViewCandidate(StrictModel):
    """Transient raw Query text for one owner-only intent review."""

    case_id: CaseId
    construction: QueryConstruction
    query_text: StrictStr = Field(min_length=1, max_length=256)
    requires_intent_annotation: bool
    intent_context_sha256: Sha256 | None

    @model_validator(mode="after")
    def validate_intent_requirement(self) -> Self:
        requires = self.construction != QueryConstruction.IDENTITY
        if self.requires_intent_annotation != requires:
            raise ValueError("intent-view requirement contradicts construction")
        if requires != (self.intent_context_sha256 is not None):
            raise ValueError("intent-view context contradicts construction")
        return self


class OracleIntentView(StrictModel):
    schema_version: Literal["human-oracle-intent-view-v1"] = (
        "human-oracle-intent-view-v1"
    )
    oracle_batch_id: StrictStr = Field(pattern=ORACLE_BATCH_ID_PATTERN)
    unit_id: StrictStr = Field(pattern=ORACLE_UNIT_ID_PATTERN)
    source_case_id: CaseId
    source_query_text: StrictStr = Field(min_length=1, max_length=256)
    candidates: list[OracleIntentViewCandidate] = Field(min_length=1, max_length=3)
    result_evidence_included: Literal[False] = False
    source_product_labels_included: Literal[False] = False
    cache_allowed: Literal[False] = False


class OracleBehaviorViewCandidate(StrictModel):
    """Transient evidence verified against one immutable observation pair."""

    case_id: CaseId
    construction: QueryConstruction
    query_text: StrictStr = Field(min_length=1, max_length=256)
    source_query_text: StrictStr = Field(min_length=1, max_length=256)
    categories: list[BadCaseCategory] = Field(min_length=1, max_length=4)
    reason_code: DiagnosticReason
    source_returned_at_k: StrictInt = Field(ge=0, le=10)
    variant_returned_at_k: StrictInt = Field(ge=0, le=10)
    overlap_at_k: StrictInt = Field(ge=0, le=10)
    source_top_hits: list[BadCaseDisplayHit] = Field(max_length=3)
    variant_top_hits: list[BadCaseDisplayHit] = Field(max_length=3)
    behavior_context_sha256: Sha256
    intent_annotation_id: StrictStr | None = Field(
        default=None,
        pattern=ORACLE_INTENT_ID_PATTERN,
    )

    @model_validator(mode="after")
    def validate_top_hit_evidence(self) -> Self:
        for hits, returned_at_k, label in (
            (self.source_top_hits, self.source_returned_at_k, "source"),
            (self.variant_top_hits, self.variant_returned_at_k, "variant"),
        ):
            expected_count = min(3, returned_at_k)
            if len(hits) != expected_count:
                raise ValueError(
                    f"behavior {label} hits must contain the complete Top-3"
                )
            if [item.rank for item in hits] != list(range(1, len(hits) + 1)):
                raise ValueError(f"behavior {label} hit ranks must be contiguous")
            if len({(item.locale, item.product_id) for item in hits}) != len(hits):
                raise ValueError(f"behavior {label} product keys must be unique")
        return self


class OracleBehaviorView(StrictModel):
    schema_version: Literal["human-oracle-behavior-view-v1"] = (
        "human-oracle-behavior-view-v1"
    )
    oracle_batch_id: StrictStr = Field(pattern=ORACLE_BATCH_ID_PATTERN)
    diagnostic_id: StrictStr = Field(pattern=BAD_CASE_ID_PATTERN)
    unit_id: StrictStr = Field(pattern=ORACLE_UNIT_ID_PATTERN)
    source_case_id: CaseId
    candidates: list[OracleBehaviorViewCandidate] = Field(min_length=1, max_length=3)
    source_reference_scope: Literal["identity_only_not_variant_label"] = (
        "identity_only_not_variant_label"
    )
    synthetic_product_relevance_labels_included: Literal[False] = False
    cache_allowed: Literal[False] = False


def oracle_unit_id(payload_without_id: dict[str, object]) -> str:
    return f"oracle-unit-{canonical_json_sha256(payload_without_id)[:12]}"


def oracle_batch_id(payload_without_id: dict[str, object]) -> str:
    return f"oracle-batch-{canonical_json_sha256(payload_without_id)[:12]}"


def oracle_intent_id(payload_without_id: dict[str, object]) -> str:
    return f"oracle-intent-{canonical_json_sha256(payload_without_id)[:12]}"


def oracle_behavior_id(payload_without_id: dict[str, object]) -> str:
    return f"oracle-behavior-{canonical_json_sha256(payload_without_id)[:12]}"


def human_oracle_id(payload_without_id: dict[str, object]) -> str:
    return f"human-oracle-{canonical_json_sha256(payload_without_id)[:12]}"
