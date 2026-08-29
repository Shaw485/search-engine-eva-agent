"""Append-only, owner-safe storage and state projection for Human Oracle work."""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import shutil
import stat
import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from search_quality.bad_cases.contracts import BadCaseDiagnosticArtifact
from search_quality.evaluation.artifacts import write_immutable_json
from search_quality.query_constructor.contracts import (
    QueryConstruction,
    QuerySetArtifact,
)

from .contracts import (
    EXPECTED_CANDIDATE_COUNT,
    EXPECTED_SYNTHETIC_CANDIDATE_COUNT,
    ORACLE_BATCH_ID_PATTERN,
    BehaviorAnnotation,
    BehaviorJudgment,
    BehaviorReason,
    BehaviorSubmission,
    HumanOracleArtifact,
    IntentAnnotation,
    IntentJudgment,
    IntentSubmission,
    JudgmentCounts,
    OracleActor,
    OracleBatchArtifact,
    OracleBatchProjection,
    OracleBatchStatus,
    OracleCandidate,
    OracleCaseReviewState,
    OracleReviewState,
    OracleReviewUnit,
    SealSubmission,
    human_oracle_id,
    oracle_behavior_id,
    oracle_intent_id,
)
from .policy import validate_oracle_batch

logger = logging.getLogger("search_quality.human_oracle")

MAX_ORACLE_STORE_BYTES = 64 * 1024 * 1024
MIN_FREE_SPACE_BYTES = 128 * 1024 * 1024
MAX_ARTIFACT_BYTES = 512 * 1024
_PROCESS_LOCK = threading.Lock()
_Model = TypeVar("_Model", bound=BaseModel)


class OracleStorageError(RuntimeError):
    """Base class for stable storage and projection failures."""


class OracleCompareAndSwapConflict(OracleStorageError):
    """A stale owner view attempted to replace a newer annotation."""


class OracleClientActionConflict(OracleStorageError):
    """One client action ID was reused with different content."""


class OracleBatchSealed(OracleStorageError):
    """A sealed batch cannot be modified."""


class OracleBatchIncomplete(OracleStorageError):
    """A batch cannot be sealed until every direct judgment is present."""


class OracleInvalidDecision(OracleStorageError):
    """A decision contradicts its candidate or active intent judgment."""


def _log_operation_failure(
    *,
    operation: str,
    error: Exception,
    oracle_batch_id: object = None,
    diagnostic_id: object = None,
    unit_id: object = None,
) -> None:
    error_codes = {
        OracleCompareAndSwapConflict: "compare_and_swap_conflict",
        OracleClientActionConflict: "client_action_conflict",
        OracleBatchSealed: "batch_sealed",
        OracleBatchIncomplete: "batch_incomplete",
        OracleInvalidDecision: "invalid_decision",
        OracleStorageError: "storage_error",
    }
    error_code = next(
        (
            code
            for error_type, code in error_codes.items()
            if isinstance(error, error_type)
        ),
        "validation_or_runtime_error",
    )
    extra: dict[str, object] = {
        "error_code": error_code,
        "error_type": type(error).__name__,
        "operation": operation,
    }
    safe_ids = {
        "oracle_batch_id": (oracle_batch_id, ORACLE_BATCH_ID_PATTERN),
        "diagnostic_id": (diagnostic_id, r"^bad-case-[0-9a-f]{12}$"),
        "unit_id": (unit_id, r"^oracle-unit-[0-9a-f]{12}$"),
    }
    for name, (value, pattern) in safe_ids.items():
        if isinstance(value, str) and re.fullmatch(pattern, value):
            extra[name] = value
    logger.warning("human_oracle_operation_failed", extra=extra)


class HumanOracleRepository:
    """Small append-only repository; immutable artifacts are the source of truth."""

    def __init__(self, artifact_root: str | Path):
        self.root = trusted_oracle_root(artifact_root)

    def create_batch(
        self,
        batch: OracleBatchArtifact,
        *,
        diagnostic: BadCaseDiagnosticArtifact,
        query_set: QuerySetArtifact,
    ) -> OracleBatchArtifact:
        try:
            return self._create_batch(
                batch,
                diagnostic=diagnostic,
                query_set=query_set,
            )
        except Exception as exc:
            _log_operation_failure(
                operation="create_batch",
                error=exc,
                oracle_batch_id=getattr(batch, "oracle_batch_id", None),
                diagnostic_id=getattr(diagnostic, "diagnostic_id", None),
            )
            raise

    def _create_batch(
        self,
        batch: OracleBatchArtifact,
        *,
        diagnostic: BadCaseDiagnosticArtifact,
        query_set: QuerySetArtifact,
    ) -> OracleBatchArtifact:
        """Store only a census rebuilt from the two trusted immutable inputs."""

        batch = validate_oracle_batch(
            batch=batch,
            diagnostic=diagnostic,
            query_set=query_set,
        )
        with self._locked():
            path = self._directory("batches") / f"{batch.oracle_batch_id}.json"
            if path.exists():
                existing = self._load_model(path, OracleBatchArtifact)
                if existing != batch:
                    raise OracleStorageError("Oracle batch content-ID collision")
                return existing
            ensure_oracle_capacity(self.root)
            _write_private_immutable(path, batch)
        logger.info(
            "human_oracle_batch_stored",
            extra={
                "diagnostic_id": batch.diagnostic_id,
                "oracle_batch_id": batch.oracle_batch_id,
                "selected_candidate_count": batch.selected_candidate_count,
                "selected_cluster_count": batch.selected_cluster_count,
            },
        )
        return batch

    def load_batch(self, oracle_batch_id: str) -> OracleBatchArtifact:
        try:
            return self._load_batch(oracle_batch_id)
        except Exception as exc:
            _log_operation_failure(
                operation="load_batch",
                error=exc,
                oracle_batch_id=(
                    oracle_batch_id
                    if isinstance(oracle_batch_id, str)
                    and re.fullmatch(ORACLE_BATCH_ID_PATTERN, oracle_batch_id)
                    else None
                ),
            )
            raise

    def _load_batch(self, oracle_batch_id: str) -> OracleBatchArtifact:
        return self._load_model(
            _trusted_artifact_path(
                self._directory("batches"),
                oracle_batch_id,
                pattern=ORACLE_BATCH_ID_PATTERN,
            ),
            OracleBatchArtifact,
        )

    def submit_intent(
        self,
        submission: IntentSubmission,
        *,
        now: datetime | None = None,
    ) -> IntentAnnotation:
        try:
            return self._submit_intent(submission, now=now)
        except Exception as exc:
            _log_operation_failure(
                operation="submit_intent",
                error=exc,
                oracle_batch_id=getattr(submission, "oracle_batch_id", None),
                unit_id=getattr(submission, "unit_id", None),
            )
            raise

    def _submit_intent(
        self,
        submission: IntentSubmission,
        *,
        now: datetime | None = None,
    ) -> IntentAnnotation:
        submission = IntentSubmission.model_validate(submission.model_dump(mode="json"))
        with self._locked():
            batch = self._load_batch(submission.oracle_batch_id)
            self._require_same_owner(batch.oracle_batch_id, submission.actor)
            replayed = self._idempotent_intent(submission, batch=batch)
            if replayed is not None:
                return replayed
            ensure_oracle_capacity(self.root)
            self._require_unsealed(batch.oracle_batch_id)
            _unit, candidate = _candidate_for_submission(
                batch,
                unit_id=submission.unit_id,
                case_id=submission.case_id,
            )
            if candidate.construction == QueryConstruction.IDENTITY:
                raise OracleInvalidDecision(
                    "identity cases do not accept intent labels"
                )
            if (
                submission.presentation_context_sha256
                != candidate.intent_context_sha256
            ):
                raise OracleInvalidDecision("intent presentation context is stale")
            current = self._latest_intents(batch.oracle_batch_id).get(candidate.case_id)
            current_id = current.intent_annotation_id if current is not None else None
            if submission.expected_previous_annotation_id != current_id:
                raise OracleCompareAndSwapConflict("intent annotation is stale")
            body = {
                "actor": submission.actor.model_dump(mode="json"),
                "case_context_sha256": candidate.case_context_sha256,
                "case_id": candidate.case_id,
                "client_action_id": submission.client_action_id,
                "construction": candidate.construction.value,
                "diagnostic_id": batch.diagnostic_id,
                "intent_annotation_id": "pending",
                "judgment": submission.judgment.value,
                "oracle_batch_id": batch.oracle_batch_id,
                "oracle_ui_withheld_result_evidence": True,
                "presentation_context_sha256": submission.presentation_context_sha256,
                "prior_external_exposure_uncontrolled": True,
                "product_relevance_labels_created": 0,
                "reason_code": submission.reason_code.value,
                "schema_version": "human-oracle-intent-v1",
                "source_case_id": candidate.source_case_id,
                "source_labels_inherited": False,
                "submitted_at_utc": _json_datetime(_utc_now(now)),
                "supersedes_annotation_id": current_id,
                "unit_id": submission.unit_id,
            }
            body_without_id = {
                key: value
                for key, value in body.items()
                if key != "intent_annotation_id"
            }
            annotation = IntentAnnotation.model_validate(
                {
                    **body_without_id,
                    "intent_annotation_id": oracle_intent_id(body_without_id),
                }
            )
            _write_private_immutable(
                self._directory("intent-annotations")
                / f"{annotation.intent_annotation_id}.json",
                annotation,
            )
        logger.info(
            "human_oracle_intent_stored",
            extra={
                "intent_annotation_id": annotation.intent_annotation_id,
                "oracle_batch_id": annotation.oracle_batch_id,
                "unit_id": annotation.unit_id,
            },
        )
        return annotation

    def submit_behavior(
        self,
        submission: BehaviorSubmission,
        *,
        now: datetime | None = None,
    ) -> BehaviorAnnotation:
        try:
            return self._submit_behavior(submission, now=now)
        except Exception as exc:
            _log_operation_failure(
                operation="submit_behavior",
                error=exc,
                oracle_batch_id=getattr(submission, "oracle_batch_id", None),
                unit_id=getattr(submission, "unit_id", None),
            )
            raise

    def _submit_behavior(
        self,
        submission: BehaviorSubmission,
        *,
        now: datetime | None = None,
    ) -> BehaviorAnnotation:
        submission = BehaviorSubmission.model_validate(
            submission.model_dump(mode="json")
        )
        with self._locked():
            batch = self._load_batch(submission.oracle_batch_id)
            self._require_same_owner(batch.oracle_batch_id, submission.actor)
            replayed = self._idempotent_behavior(submission, batch=batch)
            if replayed is not None:
                return replayed
            ensure_oracle_capacity(self.root)
            self._require_unsealed(batch.oracle_batch_id)
            _unit, candidate = _candidate_for_submission(
                batch,
                unit_id=submission.unit_id,
                case_id=submission.case_id,
            )
            if (
                submission.presentation_context_sha256
                != candidate.behavior_context_sha256
            ):
                raise OracleInvalidDecision("behavior presentation context is stale")
            active_intent = self._latest_intents(batch.oracle_batch_id).get(
                candidate.case_id
            )
            _validate_behavior_against_intent(
                candidate=candidate,
                submission=submission,
                active_intent=active_intent,
            )
            current = self._latest_behaviors(batch.oracle_batch_id).get(
                candidate.case_id
            )
            current_id = current.behavior_annotation_id if current is not None else None
            if submission.expected_previous_annotation_id != current_id:
                raise OracleCompareAndSwapConflict("behavior annotation is stale")
            body = {
                "actor": submission.actor.model_dump(mode="json"),
                "behavior_annotation_id": "pending",
                "case_context_sha256": candidate.case_context_sha256,
                "case_id": candidate.case_id,
                "client_action_id": submission.client_action_id,
                "construction": candidate.construction.value,
                "diagnostic_id": batch.diagnostic_id,
                "intent_annotation_id": submission.intent_annotation_id,
                "judgment": submission.judgment.value,
                "oracle_batch_id": batch.oracle_batch_id,
                "presentation_context_sha256": submission.presentation_context_sha256,
                "product_relevance_labels_created": 0,
                "reason_code": submission.reason_code.value,
                "root_cause_claimed": False,
                "schema_version": "human-oracle-behavior-v1",
                "source_case_id": candidate.source_case_id,
                "source_labels_inherited": False,
                "source_reference_scope": "identity_only_not_variant_label",
                "submitted_at_utc": _json_datetime(_utc_now(now)),
                "supersedes_annotation_id": current_id,
                "unit_id": submission.unit_id,
            }
            body_without_id = {
                key: value
                for key, value in body.items()
                if key != "behavior_annotation_id"
            }
            annotation = BehaviorAnnotation.model_validate(
                {
                    **body_without_id,
                    "behavior_annotation_id": oracle_behavior_id(body_without_id),
                }
            )
            _write_private_immutable(
                self._directory("behavior-annotations")
                / f"{annotation.behavior_annotation_id}.json",
                annotation,
            )
        logger.info(
            "human_oracle_behavior_stored",
            extra={
                "behavior_annotation_id": annotation.behavior_annotation_id,
                "oracle_batch_id": annotation.oracle_batch_id,
                "unit_id": annotation.unit_id,
            },
        )
        return annotation

    def seal(
        self,
        submission: SealSubmission,
        *,
        now: datetime | None = None,
    ) -> HumanOracleArtifact:
        try:
            return self._seal(submission, now=now)
        except Exception as exc:
            _log_operation_failure(
                operation="seal",
                error=exc,
                oracle_batch_id=getattr(submission, "oracle_batch_id", None),
            )
            raise

    def _seal(
        self,
        submission: SealSubmission,
        *,
        now: datetime | None = None,
    ) -> HumanOracleArtifact:
        submission = SealSubmission.model_validate(submission.model_dump(mode="json"))
        with self._locked():
            batch = self._load_batch(submission.oracle_batch_id)
            self._require_same_owner(batch.oracle_batch_id, submission.actor)
            replayed = self._idempotent_seal(submission, batch=batch)
            if replayed is not None:
                return replayed
            ensure_oracle_capacity(self.root)
            self._require_unsealed(batch.oracle_batch_id)
            active_intents = self._latest_intents(batch.oracle_batch_id)
            latest_behaviors = self._latest_behaviors(batch.oracle_batch_id)
            active_behaviors, _invalidated = _valid_behaviors(
                batch,
                intents=active_intents,
                behaviors=latest_behaviors,
            )
            ordered_candidates = [
                (unit, candidate)
                for unit in batch.units
                for candidate in unit.candidates
            ]
            synthetic_case_ids = {
                candidate.case_id
                for _unit, candidate in ordered_candidates
                if candidate.construction != QueryConstruction.IDENTITY
            }
            if set(active_intents) != synthetic_case_ids or set(active_behaviors) != {
                candidate.case_id for _unit, candidate in ordered_candidates
            }:
                raise OracleBatchIncomplete(
                    "all 30 intent and 40 behavior judgments are required"
                )
            intent_records = [
                active_intents[candidate.case_id]
                for _unit, candidate in ordered_candidates
                if candidate.construction != QueryConstruction.IDENTITY
            ]
            behavior_records = [
                active_behaviors[candidate.case_id]
                for _unit, candidate in ordered_candidates
            ]
            intent_counts = Counter(item.judgment.value for item in intent_records)
            behavior_counts = Counter(item.judgment.value for item in behavior_records)
            construction_counts = _group_behavior_counts(
                (
                    candidate.construction.value,
                    active_behaviors[candidate.case_id].judgment,
                )
                for _unit, candidate in ordered_candidates
            )
            stratum_counts = _group_behavior_counts(
                (
                    unit.stratum.value,
                    active_behaviors[candidate.case_id].judgment,
                )
                for unit, candidate in ordered_candidates
            )
            body = {
                "active_behavior_annotation_ids": [
                    item.behavior_annotation_id for item in behavior_records
                ],
                "active_intent_annotation_ids": [
                    item.intent_annotation_id for item in intent_records
                ],
                "all_selected_cases_independently_annotated": True,
                "behavior_annotation_count": EXPECTED_CANDIDATE_COUNT,
                "behavior_counts": _judgment_count_payload(behavior_counts),
                "client_action_id": submission.client_action_id,
                "counts_by_construction": construction_counts,
                "counts_by_stratum": stratum_counts,
                "diagnostic_id": batch.diagnostic_id,
                "formal_evaluation_allowed": False,
                "intent_counts": {
                    "equivalent": intent_counts["equivalent"],
                    "not_equivalent": intent_counts["not_equivalent"],
                    "uncertain": intent_counts["uncertain"],
                },
                "limitations": [
                    "single_owner_no_inter_annotator_agreement",
                    "selection_conditioned_development_set",
                    "synthetic_product_relevance_remains_unjudged",
                    "prior_exposure_not_controlled",
                    "diagnostic_judgment_is_not_root_cause",
                ],
                "mechanism_smoke_only": True,
                "oracle_batch_id": batch.oracle_batch_id,
                "product_relevance_labels_created": 0,
                "quality_conclusion_allowed": False,
                "root_cause_claimed": False,
                "schema_version": "human-diagnostic-oracle-v1",
                "sealed_at_utc": _json_datetime(_utc_now(now)),
                "sealed_by": submission.actor.model_dump(mode="json"),
                "strategy_write_count": 0,
                "synthetic_intent_annotation_count": (
                    EXPECTED_SYNTHETIC_CANDIDATE_COUNT
                ),
                "synthetic_label_inheritance_count": 0,
            }
            oracle = HumanOracleArtifact.model_validate(
                {**body, "oracle_id": human_oracle_id(body)}
            )
            _write_private_immutable(
                self._directory("seals") / f"{oracle.oracle_id}.json",
                oracle,
            )
        logger.info(
            "human_oracle_sealed",
            extra={
                "behavior_annotation_count": oracle.behavior_annotation_count,
                "oracle_batch_id": oracle.oracle_batch_id,
                "oracle_id": oracle.oracle_id,
                "synthetic_intent_annotation_count": (
                    oracle.synthetic_intent_annotation_count
                ),
            },
        )
        return oracle

    def project(self, oracle_batch_id: str) -> OracleBatchProjection:
        try:
            with self._locked():
                return self._project_unlocked(oracle_batch_id)
        except Exception as exc:
            _log_operation_failure(
                operation="project",
                error=exc,
                oracle_batch_id=oracle_batch_id,
            )
            raise

    def _project_unlocked(self, oracle_batch_id: str) -> OracleBatchProjection:
        batch = self._load_batch(oracle_batch_id)
        self._assert_single_owner_history(batch.oracle_batch_id)
        intents = self._latest_intents(batch.oracle_batch_id)
        behaviors = self._latest_behaviors(batch.oracle_batch_id)
        valid_behaviors, invalidated = _valid_behaviors(
            batch,
            intents=intents,
            behaviors=behaviors,
        )
        seal = self._seal_for_batch(batch.oracle_batch_id)
        if seal is not None:
            status = OracleBatchStatus.SEALED
        elif (
            len(intents) == EXPECTED_SYNTHETIC_CANDIDATE_COUNT
            and len(valid_behaviors) == EXPECTED_CANDIDATE_COUNT
        ):
            status = OracleBatchStatus.READY_TO_SEAL
        elif intents or behaviors:
            status = OracleBatchStatus.IN_PROGRESS
        else:
            status = OracleBatchStatus.OPEN
        return OracleBatchProjection(
            oracle_batch_id=batch.oracle_batch_id,
            status=status,
            active_intent_annotation_count=len(intents),
            active_behavior_annotation_count=len(valid_behaviors),
            invalidated_behavior_annotation_count=invalidated,
            sealed_oracle_id=seal.oracle_id if seal is not None else None,
        )

    def active_intent_for_case(
        self,
        oracle_batch_id: str,
        case_id: str,
    ) -> IntentAnnotation | None:
        try:
            with self._locked():
                self._load_batch(oracle_batch_id)
                self._assert_single_owner_history(oracle_batch_id)
                return self._latest_intents(oracle_batch_id).get(case_id)
        except Exception as exc:
            _log_operation_failure(
                operation="active_intent_for_case",
                error=exc,
                oracle_batch_id=oracle_batch_id,
            )
            raise

    def behavior_head_for_case(
        self,
        oracle_batch_id: str,
        case_id: str,
    ) -> BehaviorAnnotation | None:
        """Return the CAS head, even when a newer intent invalidated it."""

        try:
            with self._locked():
                self._load_batch(oracle_batch_id)
                self._assert_single_owner_history(oracle_batch_id)
                return self._latest_behaviors(oracle_batch_id).get(case_id)
        except Exception as exc:
            _log_operation_failure(
                operation="behavior_head_for_case",
                error=exc,
                oracle_batch_id=oracle_batch_id,
            )
            raise

    def active_behavior_for_case(
        self,
        oracle_batch_id: str,
        case_id: str,
    ) -> BehaviorAnnotation | None:
        try:
            with self._locked():
                batch = self._load_batch(oracle_batch_id)
                self._assert_single_owner_history(oracle_batch_id)
                behaviors, _invalidated = _valid_behaviors(
                    batch,
                    intents=self._latest_intents(oracle_batch_id),
                    behaviors=self._latest_behaviors(oracle_batch_id),
                )
                return behaviors.get(case_id)
        except Exception as exc:
            _log_operation_failure(
                operation="active_behavior_for_case",
                error=exc,
                oracle_batch_id=oracle_batch_id,
            )
            raise

    def review_state(self, oracle_batch_id: str) -> OracleReviewState:
        """Expose annotation IDs/judgments for UI progress and CAS, never raw text."""

        try:
            return self._review_state(oracle_batch_id)
        except Exception as exc:
            _log_operation_failure(
                operation="review_state",
                error=exc,
                oracle_batch_id=oracle_batch_id,
            )
            raise

    def _review_state(self, oracle_batch_id: str) -> OracleReviewState:

        with self._locked():
            batch = self._load_batch(oracle_batch_id)
            self._assert_single_owner_history(oracle_batch_id)
            intents = self._latest_intents(oracle_batch_id)
            behavior_heads = self._latest_behaviors(oracle_batch_id)
            active_behaviors, _invalidated = _valid_behaviors(
                batch,
                intents=intents,
                behaviors=behavior_heads,
            )
            cases = []
            for unit in batch.units:
                for candidate in unit.candidates:
                    intent = intents.get(candidate.case_id)
                    head = behavior_heads.get(candidate.case_id)
                    active = active_behaviors.get(candidate.case_id)
                    cases.append(
                        OracleCaseReviewState(
                            unit_id=unit.unit_id,
                            case_id=candidate.case_id,
                            construction=candidate.construction,
                            active_intent_annotation_id=(
                                intent.intent_annotation_id if intent else None
                            ),
                            active_intent_judgment=(
                                intent.judgment if intent else None
                            ),
                            expected_previous_intent_annotation_id=(
                                intent.intent_annotation_id if intent else None
                            ),
                            expected_previous_behavior_annotation_id=(
                                head.behavior_annotation_id if head else None
                            ),
                            active_behavior_annotation_id=(
                                active.behavior_annotation_id if active else None
                            ),
                            active_behavior_judgment=(
                                active.judgment if active else None
                            ),
                            behavior_invalidated_by_intent_change=(
                                head is not None and active is None
                            ),
                        )
                    )
            return OracleReviewState(
                oracle_batch_id=batch.oracle_batch_id,
                projection=self._project_unlocked(batch.oracle_batch_id),
                cases=cases,
            )

    def _require_same_owner(
        self,
        oracle_batch_id: str,
        actor: OracleActor,
    ) -> None:
        """Bind a batch to the first pseudonymous owner who annotates it."""

        observed = self._owners_for_batch(oracle_batch_id)
        if any(item != actor for item in observed):
            raise OracleInvalidDecision(
                "Human Oracle batch belongs to a different owner"
            )

    def _assert_single_owner_history(self, oracle_batch_id: str) -> None:
        if len(set(self._owners_for_batch(oracle_batch_id))) > 1:
            raise OracleStorageError("Human Oracle owner history is inconsistent")

    def _owners_for_batch(self, oracle_batch_id: str) -> list[OracleActor]:
        observed: list[OracleActor] = []
        for directory, model in (
            ("intent-annotations", IntentAnnotation),
            ("behavior-annotations", BehaviorAnnotation),
            ("seals", HumanOracleArtifact),
        ):
            for item in self._load_all(directory, model):
                if item.oracle_batch_id != oracle_batch_id:
                    continue
                observed.append(
                    item.sealed_by
                    if isinstance(item, HumanOracleArtifact)
                    else item.actor
                )
        return observed

    def _idempotent_intent(
        self,
        submission: IntentSubmission,
        *,
        batch: OracleBatchArtifact,
    ) -> IntentAnnotation | None:
        existing = self._find_client_action(submission.client_action_id)
        if existing is None:
            return None
        if not isinstance(existing, IntentAnnotation) or not _intent_matches_submission(
            existing,
            submission,
        ):
            raise OracleClientActionConflict("client action ID was reused")
        _validate_intent_context(existing, batch)
        return existing

    def _idempotent_behavior(
        self,
        submission: BehaviorSubmission,
        *,
        batch: OracleBatchArtifact,
    ) -> BehaviorAnnotation | None:
        existing = self._find_client_action(submission.client_action_id)
        if existing is None:
            return None
        if not isinstance(
            existing,
            BehaviorAnnotation,
        ) or not _behavior_matches_submission(existing, submission):
            raise OracleClientActionConflict("client action ID was reused")
        _validate_behavior_context(existing, batch)
        return existing

    def _idempotent_seal(
        self,
        submission: SealSubmission,
        *,
        batch: OracleBatchArtifact,
    ) -> HumanOracleArtifact | None:
        existing = self._find_client_action(submission.client_action_id)
        if existing is None:
            return None
        if not isinstance(existing, HumanOracleArtifact) or not (
            existing.oracle_batch_id == submission.oracle_batch_id
            and existing.sealed_by == submission.actor
            and existing.diagnostic_id == batch.diagnostic_id
        ):
            raise OracleClientActionConflict("client action ID was reused")
        self._validate_seal_state(batch, existing)
        return existing

    def _find_client_action(
        self,
        client_action_id: str,
    ) -> IntentAnnotation | BehaviorAnnotation | HumanOracleArtifact | None:
        matches: list[IntentAnnotation | BehaviorAnnotation | HumanOracleArtifact] = []
        for directory, model in (
            ("intent-annotations", IntentAnnotation),
            ("behavior-annotations", BehaviorAnnotation),
            ("seals", HumanOracleArtifact),
        ):
            for path in self._directory(directory).glob("*.json"):
                item = self._load_model(path, model)
                if item.client_action_id == client_action_id:
                    matches.append(item)
        if len(matches) > 1:
            raise OracleStorageError("client action ID appears more than once")
        return matches[0] if matches else None

    def _latest_intents(self, oracle_batch_id: str) -> dict[str, IntentAnnotation]:
        batch = self._load_batch(oracle_batch_id)
        records = [
            item
            for item in self._load_all("intent-annotations", IntentAnnotation)
            if item.oracle_batch_id == oracle_batch_id
        ]
        for item in records:
            _validate_intent_context(item, batch)
        return _latest_by_case(
            records,
            id_field="intent_annotation_id",
        )

    def _latest_behaviors(
        self,
        oracle_batch_id: str,
    ) -> dict[str, BehaviorAnnotation]:
        batch = self._load_batch(oracle_batch_id)
        records = [
            item
            for item in self._load_all("behavior-annotations", BehaviorAnnotation)
            if item.oracle_batch_id == oracle_batch_id
        ]
        for item in records:
            _validate_behavior_context(item, batch)
        return _latest_by_case(
            records,
            id_field="behavior_annotation_id",
        )

    def _seal_for_batch(self, oracle_batch_id: str) -> HumanOracleArtifact | None:
        batch = self._load_batch(oracle_batch_id)
        matches = [
            item
            for item in self._load_all("seals", HumanOracleArtifact)
            if item.oracle_batch_id == oracle_batch_id
        ]
        if len(matches) > 1:
            raise OracleStorageError("Oracle batch has more than one seal")
        if matches and matches[0].diagnostic_id != batch.diagnostic_id:
            raise OracleStorageError("Oracle seal contradicts its batch")
        if matches:
            self._validate_seal_state(batch, matches[0])
        return matches[0] if matches else None

    def _validate_seal_state(
        self,
        batch: OracleBatchArtifact,
        seal: HumanOracleArtifact,
    ) -> None:
        intents = self._latest_intents(batch.oracle_batch_id)
        behavior_heads = self._latest_behaviors(batch.oracle_batch_id)
        behaviors, invalidated = _valid_behaviors(
            batch,
            intents=intents,
            behaviors=behavior_heads,
        )
        ordered_candidates = [
            (unit, candidate) for unit in batch.units for candidate in unit.candidates
        ]
        expected_intent_ids = [
            intents[candidate.case_id].intent_annotation_id
            for _unit, candidate in ordered_candidates
            if candidate.construction != QueryConstruction.IDENTITY
            and candidate.case_id in intents
        ]
        expected_behavior_ids = [
            behaviors[candidate.case_id].behavior_annotation_id
            for _unit, candidate in ordered_candidates
            if candidate.case_id in behaviors
        ]
        if (
            invalidated != 0
            or any(item.actor != seal.sealed_by for item in intents.values())
            or any(item.actor != seal.sealed_by for item in behavior_heads.values())
            or seal.active_intent_annotation_ids != expected_intent_ids
            or seal.active_behavior_annotation_ids != expected_behavior_ids
            or len(expected_intent_ids) != EXPECTED_SYNTHETIC_CANDIDATE_COUNT
            or len(expected_behavior_ids) != EXPECTED_CANDIDATE_COUNT
        ):
            raise OracleStorageError("Oracle seal does not match active annotations")
        expected_intent_counts = Counter(
            intents[candidate.case_id].judgment.value
            for _unit, candidate in ordered_candidates
            if candidate.construction != QueryConstruction.IDENTITY
        )
        expected_behavior_counts = Counter(
            behaviors[candidate.case_id].judgment.value
            for _unit, candidate in ordered_candidates
        )
        expected_construction_counts = _group_behavior_counts(
            (
                candidate.construction.value,
                behaviors[candidate.case_id].judgment,
            )
            for _unit, candidate in ordered_candidates
        )
        expected_stratum_counts = _group_behavior_counts(
            (unit.stratum.value, behaviors[candidate.case_id].judgment)
            for unit, candidate in ordered_candidates
        )
        if (
            seal.intent_counts.model_dump(mode="json")
            != {
                "equivalent": expected_intent_counts["equivalent"],
                "not_equivalent": expected_intent_counts["not_equivalent"],
                "uncertain": expected_intent_counts["uncertain"],
            }
            or seal.behavior_counts.model_dump(mode="json")
            != _judgment_count_payload(expected_behavior_counts)
            or {
                key: value.model_dump(mode="json")
                for key, value in seal.counts_by_construction.items()
            }
            != expected_construction_counts
            or {
                key: value.model_dump(mode="json")
                for key, value in seal.counts_by_stratum.items()
            }
            != expected_stratum_counts
        ):
            raise OracleStorageError("Oracle seal counts contradict annotations")

    def _require_unsealed(self, oracle_batch_id: str) -> None:
        if self._seal_for_batch(oracle_batch_id) is not None:
            raise OracleBatchSealed("Human Oracle batch is sealed")

    def _load_all(self, directory: str, model: type[_Model]) -> list[_Model]:
        return [
            self._load_model(path, model)
            for path in sorted(self._directory(directory).glob("*.json"))
        ]

    def _load_model(self, path: Path, model: type[_Model]) -> _Model:
        if path.is_symlink():
            raise OracleStorageError("Oracle artifact cannot be a symbolic link")
        try:
            status = path.stat()
        except FileNotFoundError as exc:
            raise OracleStorageError("Oracle artifact was not found") from exc
        if not stat.S_ISREG(status.st_mode) or status.st_size > MAX_ARTIFACT_BYTES:
            raise OracleStorageError("Oracle artifact file is invalid")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            artifact = model.model_validate(payload)
            expected_stem = _artifact_stem(artifact)
            if expected_stem is not None and path.stem != expected_stem:
                raise ValueError("Oracle artifact filename does not match its ID")
            return artifact
        except (OSError, ValueError) as exc:
            raise OracleStorageError(
                "Oracle artifact integrity validation failed"
            ) from exc

    def _directory(self, name: str) -> Path:
        return _trusted_child(self.root, name)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with _PROCESS_LOCK:
            with oracle_write_lock(self.root):
                yield


def trusted_oracle_root(artifact_root: str | Path) -> Path:
    configured = Path(artifact_root)
    if not configured.is_absolute():
        raise ValueError("Human Oracle artifact root must be absolute")
    _reject_existing_symlink_components(configured)
    if configured.is_symlink():
        raise ValueError("Human Oracle artifact root must not be a symbolic link")
    configured.mkdir(parents=True, exist_ok=True)
    root = configured.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Human Oracle artifact root must be a directory")
    return _trusted_child(root, "human-oracle")


@contextmanager
def oracle_write_lock(root: Path) -> Iterator[None]:
    lock_path = root / ".write.lock"
    if lock_path.is_symlink():
        raise ValueError("Human Oracle lock cannot be a symbolic link")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Human Oracle lock must be a regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def ensure_oracle_capacity(root: Path) -> None:
    observed = sum(
        path.stat().st_size
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    if observed >= MAX_ORACLE_STORE_BYTES:
        raise OracleStorageError("Human Oracle store exceeds its size limit")
    if shutil.disk_usage(root).free < MIN_FREE_SPACE_BYTES:
        raise OracleStorageError("Human Oracle store has insufficient free space")


def _trusted_child(parent: Path, name: str) -> Path:
    child = parent / name
    if child.is_symlink():
        raise ValueError("Human Oracle directory cannot be a symbolic link")
    child.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved = child.resolve(strict=True)
    if resolved.parent != parent:
        raise ValueError("Human Oracle directory escaped its configured root")
    resolved.chmod(0o700)
    return resolved


def _trusted_artifact_path(directory: Path, artifact_id: str, *, pattern: str) -> Path:
    if not isinstance(artifact_id, str) or re.fullmatch(pattern, artifact_id) is None:
        raise OracleStorageError("Oracle artifact ID is invalid")
    path = directory / f"{artifact_id}.json"
    if path.parent != directory:
        raise OracleStorageError("Oracle artifact path escaped its directory")
    return path


def _reject_existing_symlink_components(path: Path) -> None:
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor /= part
        if (cursor.exists() or cursor.is_symlink()) and cursor.is_symlink():
            raise ValueError("Human Oracle path cannot contain a symbolic link")


def _write_private_immutable(path: Path, artifact: BaseModel) -> None:
    payload = artifact.model_dump(mode="json")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise OracleStorageError("Human Oracle artifact exceeds its size limit")
    write_immutable_json(path, payload)
    path.chmod(0o600)


def _candidate_for_submission(
    batch: OracleBatchArtifact,
    *,
    unit_id: str,
    case_id: str,
) -> tuple[OracleReviewUnit, OracleCandidate]:
    unit = next((item for item in batch.units if item.unit_id == unit_id), None)
    if unit is None:
        raise OracleInvalidDecision("Oracle unit does not belong to the batch")
    candidate = next(
        (item for item in unit.candidates if item.case_id == case_id), None
    )
    if candidate is None:
        raise OracleInvalidDecision("Oracle case does not belong to the unit")
    return unit, candidate


def _validate_intent_context(
    annotation: IntentAnnotation,
    batch: OracleBatchArtifact,
) -> None:
    unit, candidate = _candidate_for_submission(
        batch,
        unit_id=annotation.unit_id,
        case_id=annotation.case_id,
    )
    if (
        annotation.oracle_batch_id != batch.oracle_batch_id
        or annotation.diagnostic_id != batch.diagnostic_id
        or annotation.source_case_id != unit.source_case_id
        or annotation.construction != candidate.construction
        or annotation.case_context_sha256 != candidate.case_context_sha256
        or annotation.presentation_context_sha256 != candidate.intent_context_sha256
        or candidate.construction == QueryConstruction.IDENTITY
    ):
        raise OracleStorageError("intent annotation contradicts its Oracle batch")


def _validate_behavior_context(
    annotation: BehaviorAnnotation,
    batch: OracleBatchArtifact,
) -> None:
    unit, candidate = _candidate_for_submission(
        batch,
        unit_id=annotation.unit_id,
        case_id=annotation.case_id,
    )
    if (
        annotation.oracle_batch_id != batch.oracle_batch_id
        or annotation.diagnostic_id != batch.diagnostic_id
        or annotation.source_case_id != unit.source_case_id
        or annotation.construction != candidate.construction
        or annotation.case_context_sha256 != candidate.case_context_sha256
        or annotation.presentation_context_sha256 != candidate.behavior_context_sha256
    ):
        raise OracleStorageError("behavior annotation contradicts its Oracle batch")


def _validate_behavior_against_intent(
    *,
    candidate: OracleCandidate,
    submission: BehaviorSubmission,
    active_intent: IntentAnnotation | None,
) -> None:
    if candidate.construction == QueryConstruction.IDENTITY:
        if submission.intent_annotation_id is not None:
            raise OracleInvalidDecision("identity behavior cannot reference intent")
        intent_judgment = None
    elif active_intent is None or submission.intent_annotation_id != (
        active_intent.intent_annotation_id
    ):
        raise OracleInvalidDecision("synthetic behavior requires active intent")
    else:
        intent_judgment = active_intent.judgment
    error = _behavior_matrix_error(
        construction=candidate.construction,
        intent_judgment=intent_judgment,
        judgment=submission.judgment,
        reason=submission.reason_code,
    )
    if error is not None:
        raise OracleInvalidDecision(error)


def _latest_by_case(records: list[_Model], *, id_field: str) -> dict[str, _Model]:
    by_id = {getattr(item, id_field): item for item in records}
    if len(by_id) != len(records):
        raise OracleStorageError("duplicate Oracle annotation ID")
    by_case: dict[str, list[_Model]] = {}
    for item in records:
        by_case.setdefault(item.case_id, []).append(item)
    leaves: dict[str, _Model] = {}
    for case_id, case_records in by_case.items():
        case_ids = {getattr(item, id_field) for item in case_records}
        superseded: set[str] = set()
        for item in case_records:
            previous = item.supersedes_annotation_id
            if previous is None:
                continue
            parent = by_id.get(previous)
            if (
                parent is None
                or parent.case_id != item.case_id
                or parent.oracle_batch_id != item.oracle_batch_id
            ):
                raise OracleStorageError("Oracle supersession link is invalid")
            superseded.add(previous)
        case_leaves = [
            item for item in case_records if getattr(item, id_field) not in superseded
        ]
        if len(case_leaves) != 1:
            raise OracleStorageError("Oracle annotation history must have one leaf")
        leaf = case_leaves[0]
        visited: set[str] = set()
        cursor = leaf
        while True:
            cursor_id = getattr(cursor, id_field)
            if cursor_id in visited:
                raise OracleStorageError("Oracle annotation history contains a cycle")
            visited.add(cursor_id)
            previous = cursor.supersedes_annotation_id
            if previous is None:
                break
            cursor = by_id[previous]
        if visited != case_ids:
            raise OracleStorageError("Oracle annotation history contains a branch")
        leaves[case_id] = leaf
    return leaves


def _valid_behaviors(
    batch: OracleBatchArtifact,
    *,
    intents: dict[str, IntentAnnotation],
    behaviors: dict[str, BehaviorAnnotation],
) -> tuple[dict[str, BehaviorAnnotation], int]:
    candidates = {
        item.case_id: item for unit in batch.units for item in unit.candidates
    }
    valid: dict[str, BehaviorAnnotation] = {}
    invalidated = 0
    for case_id, behavior in behaviors.items():
        candidate = candidates.get(case_id)
        if candidate is None:
            raise OracleStorageError("behavior references an unknown Oracle case")
        if candidate.construction == QueryConstruction.IDENTITY:
            if behavior.intent_annotation_id is not None:
                raise OracleStorageError("identity behavior has an intent reference")
            _validate_stored_behavior_against_intent(
                candidate=candidate,
                behavior=behavior,
                intent=None,
            )
            valid[case_id] = behavior
            continue
        intent = intents.get(case_id)
        if (
            intent is None
            or behavior.intent_annotation_id != intent.intent_annotation_id
        ):
            invalidated += 1
        else:
            _validate_stored_behavior_against_intent(
                candidate=candidate,
                behavior=behavior,
                intent=intent,
            )
            valid[case_id] = behavior
    return valid, invalidated


def _validate_stored_behavior_against_intent(
    *,
    candidate: OracleCandidate,
    behavior: BehaviorAnnotation,
    intent: IntentAnnotation | None,
) -> None:
    if candidate.construction == QueryConstruction.IDENTITY:
        intent_judgment = None
    elif intent is None or behavior.intent_annotation_id != intent.intent_annotation_id:
        raise OracleStorageError("stored synthetic behavior lacks its active intent")
    else:
        intent_judgment = intent.judgment
    error = _behavior_matrix_error(
        construction=candidate.construction,
        intent_judgment=intent_judgment,
        judgment=behavior.judgment,
        reason=behavior.reason_code,
    )
    if error is not None:
        raise OracleStorageError(f"stored {error}")


def _behavior_matrix_error(
    *,
    construction: QueryConstruction,
    intent_judgment: IntentJudgment | None,
    judgment: BehaviorJudgment,
    reason: BehaviorReason,
) -> str | None:
    """Return why a complete construction×intent×behavior tuple is invalid."""

    uncertain_reasons = {
        BehaviorReason.CATALOG_COVERAGE_UNKNOWN,
        BehaviorReason.INSUFFICIENT_RESULT_EVIDENCE,
        BehaviorReason.INSUFFICIENT_DOMAIN_KNOWLEDGE,
    }
    if construction == QueryConstruction.IDENTITY:
        allowed = {
            BehaviorJudgment.CONFIRMED_ISSUE: {
                BehaviorReason.OWNER_CATALOG_EXPECTATION
            },
            BehaviorJudgment.ACCEPTABLE: {BehaviorReason.BEHAVIOR_IS_EXPECTED},
            BehaviorJudgment.UNCERTAIN: uncertain_reasons,
        }
        if intent_judgment is not None:
            return "identity behavior cannot have an intent judgment"
    elif intent_judgment == IntentJudgment.EQUIVALENT:
        allowed = {
            BehaviorJudgment.CONFIRMED_ISSUE: {
                BehaviorReason.EQUIVALENT_INTENT_SHOULD_PRESERVE_BEHAVIOR
            },
            BehaviorJudgment.ACCEPTABLE: {BehaviorReason.BEHAVIOR_IS_EXPECTED},
            BehaviorJudgment.UNCERTAIN: uncertain_reasons,
        }
    elif intent_judgment == IntentJudgment.NOT_EQUIVALENT:
        if judgment == BehaviorJudgment.CONFIRMED_ISSUE:
            return "non-equivalent intent cannot confirm a pair issue"
        allowed = {
            BehaviorJudgment.CONFIRMED_ISSUE: set(),
            BehaviorJudgment.ACCEPTABLE: {BehaviorReason.INTENT_NOT_EQUIVALENT},
            BehaviorJudgment.UNCERTAIN: uncertain_reasons,
        }
    elif intent_judgment == IntentJudgment.UNCERTAIN:
        if judgment != BehaviorJudgment.UNCERTAIN:
            return "uncertain intent requires uncertain behavior"
        allowed = {
            BehaviorJudgment.CONFIRMED_ISSUE: set(),
            BehaviorJudgment.ACCEPTABLE: set(),
            BehaviorJudgment.UNCERTAIN: uncertain_reasons,
        }
    else:
        return "synthetic behavior requires an intent judgment"
    if reason not in allowed[judgment]:
        return "behavior judgment and reason contradict construction/intent"
    return None


def _group_behavior_counts(items) -> dict[str, dict[str, int]]:
    grouped: dict[str, Counter[str]] = {}
    for group, judgment in items:
        grouped.setdefault(group, Counter())[judgment.value] += 1
    return {
        group: _judgment_count_payload(counts)
        for group, counts in sorted(grouped.items())
    }


def _judgment_count_payload(counts: Counter[str]) -> dict[str, int]:
    return JudgmentCounts(
        confirmed_issue=counts["confirmed_issue"],
        acceptable=counts["acceptable"],
        uncertain=counts["uncertain"],
    ).model_dump(mode="json")


def _intent_matches_submission(
    annotation: IntentAnnotation,
    submission: IntentSubmission,
) -> bool:
    return (
        annotation.oracle_batch_id == submission.oracle_batch_id
        and annotation.unit_id == submission.unit_id
        and annotation.case_id == submission.case_id
        and annotation.presentation_context_sha256
        == submission.presentation_context_sha256
        and annotation.judgment == submission.judgment
        and annotation.reason_code == submission.reason_code
        and annotation.actor == submission.actor
        and annotation.supersedes_annotation_id
        == submission.expected_previous_annotation_id
    )


def _behavior_matches_submission(
    annotation: BehaviorAnnotation,
    submission: BehaviorSubmission,
) -> bool:
    return (
        annotation.oracle_batch_id == submission.oracle_batch_id
        and annotation.unit_id == submission.unit_id
        and annotation.case_id == submission.case_id
        and annotation.presentation_context_sha256
        == submission.presentation_context_sha256
        and annotation.judgment == submission.judgment
        and annotation.reason_code == submission.reason_code
        and annotation.intent_annotation_id == submission.intent_annotation_id
        and annotation.actor == submission.actor
        and annotation.supersedes_annotation_id
        == submission.expected_previous_annotation_id
    )


def _utc_now(value: datetime | None) -> datetime:
    observed = value or datetime.now(UTC)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("Human Oracle timestamps must be timezone-aware")
    return observed.astimezone(UTC)


def _json_datetime(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _artifact_stem(artifact: BaseModel) -> str | None:
    for name in (
        "oracle_id",
        "behavior_annotation_id",
        "intent_annotation_id",
        "oracle_batch_id",
    ):
        value = getattr(artifact, name, None)
        if isinstance(value, str):
            return value
    return None
