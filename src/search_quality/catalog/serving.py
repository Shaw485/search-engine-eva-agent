"""Fail-closed active-strategy resolution, activation, search and rollback."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import math
import os
import re
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from search_quality.catalog.index import CATALOG_SCHEMA_VERSION
from search_quality.catalog.index_v2 import CATALOG_V2_SCHEMA_VERSION
from search_quality.catalog.pipeline_v2 import (
    COARSE_TOP_K,
    PRODUCTION_PIPELINE_CONFIG_SHA256,
    PRODUCTION_PIPELINE_ID,
    PRODUCTION_STRATEGY_ID,
    CatalogV2SearchPipeline,
    validate_production_pipeline_config,
)
from search_quality.catalog.search import CatalogSearchService
from search_quality.evaluation.artifacts import atomic_write_text, write_immutable_json

ACTIVE_POINTER_SCHEMA_VERSION = "retrieval-serving-pointer-v1"
STRATEGY_REVISION_SCHEMA_VERSION = "retrieval-serving-revision-v1"
ACTIVATION_RECEIPT_SCHEMA_VERSION = "retrieval-serving-activation-receipt-v1"
ROLLBACK_RECEIPT_SCHEMA_VERSION = "retrieval-serving-rollback-receipt-v1"
BASELINE_STRATEGY_ID = "catalog-baseline-v1"
RETRIEVAL_STRATEGY_DIRECTORY = "retrieval-strategies"
MAX_SERVING_ARTIFACT_BYTES = 4 * 1024 * 1024
SERVING_LOCK_TIMEOUT_SECONDS = 10.0
SENTINEL_QUERY_LIMIT = 2
SENTINEL_QUERY_DEADLINE_MS = 5_000
SENTINEL_MAX_P95_MS = 5_000.0

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_CODE_REVISION_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_SAFE_ID_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,127}\Z")
_PROPOSAL_ID_PATTERN = re.compile(r"retrieval-proposal-[0-9a-f]{12}\Z")
_DECISION_ID_PATTERN = re.compile(r"retrieval-decision-[0-9a-f]{12}\Z")

logger = logging.getLogger("search_quality.catalog_serving")


class RetrievalServingError(ValueError):
    """Base class for stable, classifiable serving control failures."""

    error_code = "retrieval_serving_error"


class RetrievalServingConfigurationError(RetrievalServingError):
    """The active strategy or index is not compatible with production serving."""

    error_code = "retrieval_serving_configuration_invalid"


class RetrievalActivationRejected(RetrievalServingError):
    """Approval, CAS or validation did not authorize activation."""

    error_code = "retrieval_activation_rejected"


class RetrievalSentinelFailed(RetrievalServingError):
    """The candidate v2 service failed its bounded pre-activation sentinel."""

    error_code = "retrieval_activation_sentinel_failed"


class RetrievalRollbackRejected(RetrievalServingError):
    """No compatible rollback target was available."""

    error_code = "retrieval_rollback_rejected"


@dataclass(frozen=True, slots=True)
class RetrievalServingState:
    ready: bool
    mode: Literal["baseline", "v2"]
    strategy_id: str
    strategy_revision: str | None
    index_id: str
    index_schema_version: str
    pipeline_id: str | None
    channel_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_ids": list(self.channel_ids),
            "index_id": self.index_id,
            "index_schema_version": self.index_schema_version,
            "mode": self.mode,
            "pipeline_id": self.pipeline_id,
            "ready": self.ready,
            "strategy_id": self.strategy_id,
            "strategy_revision": self.strategy_revision,
        }


@dataclass(frozen=True, slots=True)
class ActiveCatalogSearchResult:
    mode: Literal["baseline", "v2"]
    strategy_id: str
    strategy_revision: str | None
    index_id: str
    index_schema_version: str
    pipeline_id: str | None
    product_count: int
    locale_counts: dict[str, int]
    channel_counts: dict[str, int]
    hits: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": "sqlite-fts5",
            "channel_counts": dict(self.channel_counts),
            "hits": list(self.hits),
            "index_id": self.index_id,
            "index_schema_version": self.index_schema_version,
            "locale_counts": dict(self.locale_counts),
            "mode": self.mode,
            "pipeline_id": self.pipeline_id,
            "product_count": self.product_count,
            "strategy_id": self.strategy_id,
            "strategy_revision": self.strategy_revision,
        }


class RetrievalStrategyResolver:
    """Resolve one pointer snapshot to one validated immutable strategy revision."""

    def __init__(
        self,
        *,
        baseline_index_path: str | Path,
        active_index_path: str | Path,
        artifact_root: str | Path,
    ) -> None:
        self.baseline_index_path = Path(baseline_index_path)
        self.active_index_path = Path(active_index_path)
        self.artifact_root = _resolve_artifact_root(artifact_root)

    def resolve(self) -> RetrievalServingState:
        revision = _load_active_revision_artifact(self.artifact_root)
        if revision is None:
            baseline = _load_baseline_service(self.baseline_index_path)
            return RetrievalServingState(
                ready=True,
                mode="baseline",
                strategy_id=BASELINE_STRATEGY_ID,
                strategy_revision=None,
                index_id=baseline.metadata.index_id,
                index_schema_version=baseline.metadata.schema_version,
                pipeline_id=None,
                channel_ids=("baseline-title-bm25-and-v1",),
            )
        return _state_from_revision(
            revision,
            baseline_index_path=self.baseline_index_path,
            active_index_path=self.active_index_path,
        )


class ActiveCatalogSearchService:
    """Resolve the active pointer for every request and report the actual strategy."""

    def __init__(
        self,
        *,
        baseline_index_path: str | Path,
        active_index_path: str | Path,
        artifact_root: str | Path,
    ) -> None:
        self.resolver = RetrievalStrategyResolver(
            baseline_index_path=baseline_index_path,
            active_index_path=active_index_path,
            artifact_root=artifact_root,
        )

    def readiness(self) -> dict[str, Any]:
        try:
            state = self.resolver.resolve()
        except Exception as exc:
            logger.error(
                "retrieval_serving_readiness_failed",
                extra={
                    "error_code": _serving_error_code(exc),
                    "error_type": type(exc).__name__,
                },
            )
            return {
                "ready": False,
                "error_code": _serving_error_code(exc),
            }
        logger.info(
            "retrieval_serving_ready",
            extra={
                "index_id": state.index_id,
                "mode": state.mode,
                "strategy_id": state.strategy_id,
                "strategy_revision": state.strategy_revision,
            },
        )
        return state.to_dict()

    def search(
        self, query: str, *, top_k: int = COARSE_TOP_K
    ) -> ActiveCatalogSearchResult:
        if (
            isinstance(top_k, bool)
            or not isinstance(top_k, int)
            or not 1 <= top_k <= COARSE_TOP_K
        ):
            raise ValueError(
                f"active catalog top_k must be between 1 and {COARSE_TOP_K}"
            )
        state = self.resolver.resolve()
        started = time.perf_counter()
        logger.debug(
            "active_catalog_search_started",
            extra={
                "index_id": state.index_id,
                "mode": state.mode,
                "strategy_id": state.strategy_id,
                "strategy_revision": state.strategy_revision,
                "top_k": top_k,
            },
        )
        if state.mode == "baseline":
            service = _load_baseline_service(self.resolver.baseline_index_path)
            if service.metadata.index_id != state.index_id:
                raise RetrievalServingConfigurationError(
                    "baseline index changed after strategy resolution"
                )
            result = service.search(query, top_k=top_k)
            payload = ActiveCatalogSearchResult(
                mode="baseline",
                strategy_id=state.strategy_id,
                strategy_revision=state.strategy_revision,
                index_id=result.index_id,
                index_schema_version=state.index_schema_version,
                pipeline_id=None,
                product_count=result.product_count,
                locale_counts=dict(result.locale_counts),
                channel_counts={"baseline": len(result.hits)},
                hits=tuple(hit.to_dict() for hit in result.hits),
            )
        else:
            pipeline = _load_v2_pipeline(self.resolver.active_index_path)
            if pipeline.metadata.index_id != state.index_id:
                raise RetrievalServingConfigurationError(
                    "catalog v2 index changed after strategy resolution"
                )
            result = pipeline.search(query, top_k=top_k)
            payload = ActiveCatalogSearchResult(
                mode="v2",
                strategy_id=state.strategy_id,
                strategy_revision=state.strategy_revision,
                index_id=result.index_id,
                index_schema_version=state.index_schema_version,
                pipeline_id=result.pipeline_id,
                product_count=result.product_count,
                locale_counts=dict(result.locale_counts),
                channel_counts=dict(result.channel_counts),
                hits=tuple(
                    {**hit.to_dict(), "strategy": state.strategy_id}
                    for hit in result.hits
                ),
            )
        logger.debug(
            "active_catalog_search_completed",
            extra={
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "index_id": payload.index_id,
                "mode": payload.mode,
                "returned_at_k": len(payload.hits),
                "strategy_id": payload.strategy_id,
                "strategy_revision": payload.strategy_revision,
                "top_k": top_k,
            },
        )
        return payload


# More concise alias for API integration.
CatalogServingService = ActiveCatalogSearchService


def load_active_retrieval_revision(artifact_root: str | Path) -> str | None:
    """Read and validate the active pointer target, returning its real revision."""

    root = _resolve_artifact_root(artifact_root)
    revision = _load_active_revision_artifact(root)
    return None if revision is None else str(revision["strategy_revision"])


def load_retrieval_serving_state(
    *,
    baseline_index_path: str | Path,
    active_index_path: str | Path,
    artifact_root: str | Path,
) -> RetrievalServingState:
    return RetrievalStrategyResolver(
        baseline_index_path=baseline_index_path,
        active_index_path=active_index_path,
        artifact_root=artifact_root,
    ).resolve()


def validate_and_activate_retrieval_strategy(
    proposal: dict[str, Any],
    baseline_index_path: str | Path,
    active_index_path: str | Path,
    artifact_root: str | Path,
    revision_provider: Callable[..., str],
) -> dict[str, Any]:
    """Validate approved evidence and atomically switch serving to catalog v2."""

    root = _resolve_artifact_root(artifact_root)
    started = time.perf_counter()
    logger.info("retrieval_strategy_activation_started")
    try:
        validated = _validate_activation_envelope(proposal)
        code_revision = _deployment_revision(revision_provider)
        if validated["proposal"]["code_revision"] != code_revision:
            raise RetrievalActivationRejected(
                "proposal code revision does not match the deployment"
            )
        selected = validated["selected_pipeline"]
        current_revision = load_active_retrieval_revision(root)
        parent_revision = validated["proposal"].get("parent_active_revision")
        if parent_revision != current_revision:
            raise RetrievalActivationRejected(
                "proposal is stale relative to active serving"
            )

        baseline = _load_baseline_service(baseline_index_path)
        pipeline = _load_v2_pipeline(active_index_path)
        sentinel = _run_activation_sentinel(pipeline)
        if sentinel["latency_ms"]["p95"] > SENTINEL_MAX_P95_MS:
            raise RetrievalSentinelFailed("catalog v2 sentinel latency gate failed")

        baseline_revision = _baseline_revision_payload(
            baseline=baseline,
            code_revision=code_revision,
        )
        rollback_revision = (
            current_revision
            if current_revision is not None
            else baseline_revision["strategy_revision"]
        )
        revision = _v2_revision_payload(
            proposal=validated["proposal"],
            selected=selected,
            pipeline=pipeline,
            code_revision=code_revision,
            parent_active_revision=current_revision,
            rollback_strategy_revision=rollback_revision,
        )
        receipt_body = {
            "active": True,
            "channel_counts": sentinel["channel_counts"],
            "code_revision": code_revision,
            "config_sha256": selected["config_sha256"],
            "index_id": pipeline.metadata.index_id,
            "index_schema_version": pipeline.metadata.schema_version,
            "latency_ms": sentinel["latency_ms"],
            "parent_active_revision": current_revision,
            "passed": True,
            "pipeline_id": selected["pipeline_id"],
            "previous_strategy_revision": current_revision,
            "proposal_id": validated["proposal"]["proposal_id"],
            "proposal_revision": validated["proposal"]["proposal_revision"],
            "rollback_strategy_revision": rollback_revision,
            "schema_version": ACTIVATION_RECEIPT_SCHEMA_VERSION,
            "sentinel_count": sentinel["sentinel_count"],
            "strategy_id": selected["strategy_id"],
            "strategy_revision": revision["strategy_revision"],
        }
        receipt = {
            **receipt_body,
            "receipt_id": _content_id("activation", receipt_body),
        }

        with _serving_lock(root):
            if load_active_retrieval_revision(root) != current_revision:
                raise RetrievalActivationRejected(
                    "active serving changed during validation"
                )
            if current_revision is None:
                _write_strategy_revision(root, baseline_revision)
            _write_strategy_revision(root, revision)
            write_immutable_json(
                _serving_subdirectory(root, "activation-receipts")
                / f"{receipt['receipt_id']}.json",
                receipt,
            )
            # The pointer is activation authority and deliberately the final
            # fallible write. A failed receipt/revision publication therefore
            # cannot change live serving state.
            _write_active_pointer(root, revision)
    except Exception as exc:
        logger.error(
            "retrieval_strategy_activation_failed",
            extra={
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "error_code": _serving_error_code(exc),
                "error_type": type(exc).__name__,
            },
        )
        raise
    logger.info(
        "retrieval_strategy_activation_completed",
        extra={
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "index_id": receipt["index_id"],
            "receipt_id": receipt["receipt_id"],
            "sentinel_count": receipt["sentinel_count"],
            "strategy_id": receipt["strategy_id"],
            "strategy_revision": receipt["strategy_revision"],
        },
    )
    return receipt


def rollback_retrieval_strategy(
    *,
    baseline_index_path: str | Path,
    active_index_path: str | Path,
    artifact_root: str | Path,
    expected_active_revision: str | None = None,
) -> dict[str, Any]:
    """Atomically point serving at the current revision's immutable rollback target."""

    root = _resolve_artifact_root(artifact_root)
    started = time.perf_counter()
    logger.info("retrieval_strategy_rollback_started")
    try:
        with _serving_lock(root):
            current = _load_active_revision_artifact(root)
            if current is None:
                raise RetrievalRollbackRejected("serving is already on legacy baseline")
            current_revision = str(current["strategy_revision"])
            if (
                expected_active_revision is not None
                and expected_active_revision != current_revision
            ):
                raise RetrievalRollbackRejected(
                    "active serving revision does not match rollback request"
                )
            target_revision = current.get("rollback_strategy_revision")
            if not isinstance(target_revision, str) or not _SHA256_PATTERN.fullmatch(
                target_revision
            ):
                raise RetrievalRollbackRejected(
                    "active revision has no rollback target"
                )
            target = _load_revision_by_id(root, target_revision)
            state = _state_from_revision(
                target,
                baseline_index_path=Path(baseline_index_path),
                active_index_path=Path(active_index_path),
            )
            receipt_body = {
                "from_strategy_revision": current_revision,
                "index_id": state.index_id,
                "index_schema_version": state.index_schema_version,
                "mode": state.mode,
                "schema_version": ROLLBACK_RECEIPT_SCHEMA_VERSION,
                "strategy_id": state.strategy_id,
                "strategy_revision": state.strategy_revision,
                "succeeded": True,
            }
            receipt = {
                **receipt_body,
                "receipt_id": _content_id("rollback", receipt_body),
            }
            write_immutable_json(
                _serving_subdirectory(root, "rollback-receipts")
                / f"{receipt['receipt_id']}.json",
                receipt,
            )
            # As with activation, publish the authority pointer last so any
            # raised precondition or receipt error leaves serving unchanged.
            _write_active_pointer(root, target)
    except Exception as exc:
        logger.error(
            "retrieval_strategy_rollback_failed",
            extra={
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "error_code": _serving_error_code(exc),
                "error_type": type(exc).__name__,
            },
        )
        raise
    logger.info(
        "retrieval_strategy_rollback_completed",
        extra={
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "index_id": receipt["index_id"],
            "receipt_id": receipt["receipt_id"],
            "strategy_id": receipt["strategy_id"],
            "strategy_revision": receipt["strategy_revision"],
        },
    )
    return receipt


def _validate_activation_envelope(envelope: Any) -> dict[str, Any]:
    if not isinstance(envelope, dict):
        raise RetrievalActivationRejected("activation evidence must be an object")
    proposal = envelope.get("proposal")
    decision = envelope.get("decision") or envelope.get("approval_decision")
    if not isinstance(proposal, dict) or not isinstance(decision, dict):
        raise RetrievalActivationRejected(
            "activation requires proposal and approval decision evidence"
        )
    if proposal.get("schema_version") != "retrieval-release-proposal-v1":
        raise RetrievalActivationRejected("unsupported retrieval proposal schema")
    proposal_fields = {
        "analysis_schema_version",
        "analysis_status",
        "approval_eligible",
        "code_revision",
        "evidence",
        "lifecycle",
        "parent_active_revision",
        "profile",
        "proposal_id",
        "proposal_revision",
        "release_gate",
        "schema_version",
        "selected_pipeline",
        "trace_terminal_reason_code",
    }
    if set(proposal) != proposal_fields:
        raise RetrievalActivationRejected("retrieval proposal contract does not match")
    proposal_id = proposal.get("proposal_id")
    proposal_revision = proposal.get("proposal_revision")
    if not isinstance(proposal_id, str) or not _PROPOSAL_ID_PATTERN.fullmatch(
        proposal_id
    ):
        raise RetrievalActivationRejected("retrieval proposal ID is invalid")
    if not isinstance(proposal_revision, str) or not _SHA256_PATTERN.fullmatch(
        proposal_revision
    ):
        raise RetrievalActivationRejected("retrieval proposal revision is invalid")
    proposal_body = {
        key: value
        for key, value in proposal.items()
        if key not in {"proposal_id", "proposal_revision"}
    }
    if (
        proposal_revision != _sha256_payload(proposal_body)
        or proposal_id != f"retrieval-proposal-{proposal_revision[:12]}"
    ):
        raise RetrievalActivationRejected(
            "retrieval proposal identity does not match content"
        )
    if proposal.get("approval_eligible") is not True:
        raise RetrievalActivationRejected("retrieval proposal is not approval eligible")
    if (
        proposal.get("lifecycle") != "pending_owner_review"
        or proposal.get("profile") != "smoke"
        or proposal.get("analysis_status") != "proposal_ready"
        or proposal.get("analysis_schema_version")
        != "retrieval-stage-analysis-response-v1"
    ):
        raise RetrievalActivationRejected("retrieval proposal is not reviewable")
    selected = proposal.get("selected_pipeline")
    if not isinstance(selected, dict):
        raise RetrievalActivationRejected("selected retrieval pipeline is missing")
    if selected.get("strategy_id") != PRODUCTION_STRATEGY_ID:
        raise RetrievalActivationRejected("retrieval strategy ID is unsupported")
    config = validate_production_pipeline_config(
        selected.get("config"),
        config_sha256=selected.get("config_sha256"),
        pipeline_id=selected.get("pipeline_id"),
    )
    if selected.get("config_sha256") != PRODUCTION_PIPELINE_CONFIG_SHA256:
        raise RetrievalActivationRejected("retrieval config hash is unsupported")

    decision_fields = {
        "activation_status",
        "actor_id",
        "client_action_id",
        "config_sha256",
        "decision",
        "decision_id",
        "lifecycle",
        "parent_active_revision",
        "pipeline_id",
        "proposal_id",
        "proposal_revision",
        "schema_version",
        "strategy_id",
        "validation_required",
    }
    if set(decision) != decision_fields:
        raise RetrievalActivationRejected(
            "retrieval approval decision contract does not match"
        )
    if (
        decision.get("schema_version") != "retrieval-release-decision-v1"
        or decision.get("decision") != "approve"
        or decision.get("lifecycle") != "approved_for_validation"
        or decision.get("activation_status") != "not_active"
        or decision.get("validation_required") is not True
    ):
        raise RetrievalActivationRejected(
            "retrieval decision is not approved for validation"
        )
    decision_id = decision.get("decision_id")
    decision_body = {
        key: value for key, value in decision.items() if key != "decision_id"
    }
    if (
        not isinstance(decision_id, str)
        or not _DECISION_ID_PATTERN.fullmatch(decision_id)
        or decision_id != _content_id("retrieval-decision", decision_body)
    ):
        raise RetrievalActivationRejected(
            "retrieval decision identity does not match content"
        )
    bindings = {
        "proposal_id": proposal_id,
        "proposal_revision": proposal_revision,
        "strategy_id": selected.get("strategy_id"),
        "pipeline_id": selected.get("pipeline_id"),
        "config_sha256": selected.get("config_sha256"),
    }
    for name, expected in bindings.items():
        if decision.get(name) != expected:
            raise RetrievalActivationRejected(
                f"retrieval decision {name} does not match proposal"
            )
    code_revision = proposal.get("code_revision")
    parent_revision = proposal.get("parent_active_revision")
    if not isinstance(code_revision, str) or not _CODE_REVISION_PATTERN.fullmatch(
        code_revision
    ):
        raise RetrievalActivationRejected("proposal code revision is invalid")
    if parent_revision is not None and (
        not isinstance(parent_revision, str)
        or not _SHA256_PATTERN.fullmatch(parent_revision)
    ):
        raise RetrievalActivationRejected("proposal parent revision is invalid")
    if decision.get("parent_active_revision") != parent_revision:
        raise RetrievalActivationRejected(
            "retrieval decision parent does not match proposal"
        )
    return {
        "decision": decision,
        "proposal": proposal,
        "selected_pipeline": {**selected, "config": config},
    }


def _run_activation_sentinel(pipeline: CatalogV2SearchPipeline) -> dict[str, Any]:
    latencies: list[float] = []
    channel_counts: Counter[str] = Counter()
    try:
        queries = pipeline.sentinel_queries(limit=SENTINEL_QUERY_LIMIT)
        for query in queries:
            started = time.perf_counter()
            result = pipeline.search(
                query,
                top_k=COARSE_TOP_K,
                max_elapsed_ms=SENTINEL_QUERY_DEADLINE_MS,
            )
            latencies.append((time.perf_counter() - started) * 1000)
            channel_counts.update(result.channel_counts)
            if result.index_id != pipeline.metadata.index_id:
                raise RetrievalSentinelFailed("sentinel index identity changed")
    except RetrievalServingError:
        raise
    except Exception as exc:
        raise RetrievalSentinelFailed("catalog v2 sentinel execution failed") from exc
    if not latencies:
        raise RetrievalSentinelFailed("catalog v2 sentinel produced no executions")
    required_counts = ("title", "exact", "multi_field", "union", "fused", "coarse")
    if any(channel_counts[name] < 1 for name in required_counts):
        raise RetrievalSentinelFailed(
            "catalog v2 sentinel did not exercise every production stage"
        )
    ordered = sorted(latencies)
    p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return {
        "channel_counts": {name: int(channel_counts[name]) for name in required_counts},
        "latency_ms": {
            "max": round(max(latencies), 3),
            "p95": round(ordered[p95_index], 3),
            "total": round(sum(latencies), 3),
        },
        "sentinel_count": len(latencies),
    }


def _baseline_revision_payload(
    *,
    baseline: CatalogSearchService,
    code_revision: str,
) -> dict[str, Any]:
    body = {
        "code_revision": code_revision,
        "config_sha256": None,
        "index_id": baseline.metadata.index_id,
        "index_schema_version": baseline.metadata.schema_version,
        "index_source_sha256": baseline.metadata.source_sha256,
        "mode": "baseline",
        "parent_active_revision": None,
        "pipeline_config": None,
        "pipeline_id": None,
        "previous_strategy_revision": None,
        "proposal_id": None,
        "proposal_revision": None,
        "rollback_strategy_revision": None,
        "schema_version": STRATEGY_REVISION_SCHEMA_VERSION,
        "strategy_id": BASELINE_STRATEGY_ID,
    }
    return {**body, "strategy_revision": _sha256_payload(body)}


def _v2_revision_payload(
    *,
    proposal: dict[str, Any],
    selected: dict[str, Any],
    pipeline: CatalogV2SearchPipeline,
    code_revision: str,
    parent_active_revision: str | None,
    rollback_strategy_revision: str,
) -> dict[str, Any]:
    body = {
        "code_revision": code_revision,
        "config_sha256": selected["config_sha256"],
        "index_id": pipeline.metadata.index_id,
        "index_schema_version": pipeline.metadata.schema_version,
        "index_source_sha256": pipeline.metadata.source_sha256,
        "mode": "v2",
        "parent_active_revision": parent_active_revision,
        "pipeline_config": selected["config"],
        "pipeline_id": selected["pipeline_id"],
        "previous_strategy_revision": parent_active_revision,
        "proposal_id": proposal["proposal_id"],
        "proposal_revision": proposal["proposal_revision"],
        "rollback_strategy_revision": rollback_strategy_revision,
        "schema_version": STRATEGY_REVISION_SCHEMA_VERSION,
        "strategy_id": selected["strategy_id"],
    }
    return {**body, "strategy_revision": _sha256_payload(body)}


def _state_from_revision(
    revision: dict[str, Any],
    *,
    baseline_index_path: Path,
    active_index_path: Path,
) -> RetrievalServingState:
    mode = revision["mode"]
    if mode == "baseline":
        service = _load_baseline_service(baseline_index_path)
        metadata = service.metadata
        pipeline_id = None
        channel_ids = ("baseline-title-bm25-and-v1",)
    elif mode == "v2":
        service = _load_v2_pipeline(active_index_path)
        metadata = service.metadata
        pipeline_id = PRODUCTION_PIPELINE_ID
        channel_ids = (
            "title-bm25-recall-v1",
            "exact-title-recall-v1",
            "multi-field-bm25-recall-v1",
        )
    else:  # pragma: no cover - revision validation rejects this first
        raise RetrievalServingConfigurationError("unsupported retrieval serving mode")
    if (
        metadata.index_id != revision["index_id"]
        or metadata.schema_version != revision["index_schema_version"]
        or metadata.source_sha256 != revision["index_source_sha256"]
    ):
        raise RetrievalServingConfigurationError(
            "active strategy index identity is incompatible"
        )
    return RetrievalServingState(
        ready=True,
        mode=mode,
        strategy_id=str(revision["strategy_id"]),
        strategy_revision=str(revision["strategy_revision"]),
        index_id=metadata.index_id,
        index_schema_version=metadata.schema_version,
        pipeline_id=pipeline_id,
        channel_ids=channel_ids,
    )


def _load_baseline_service(path: str | Path) -> CatalogSearchService:
    try:
        service = CatalogSearchService(path)
    except Exception as exc:
        raise RetrievalServingConfigurationError(
            "baseline catalog index is unavailable"
        ) from exc
    if service.metadata.schema_version != CATALOG_SCHEMA_VERSION:
        raise RetrievalServingConfigurationError(
            "baseline catalog index is incompatible"
        )
    return service


def _load_v2_pipeline(path: str | Path) -> CatalogV2SearchPipeline:
    try:
        pipeline = CatalogV2SearchPipeline(path)
    except Exception as exc:
        raise RetrievalServingConfigurationError(
            "catalog v2 index is unavailable"
        ) from exc
    if pipeline.metadata.schema_version != CATALOG_V2_SCHEMA_VERSION:
        raise RetrievalServingConfigurationError("catalog v2 index is incompatible")
    return pipeline


def _write_strategy_revision(root: Path, revision: dict[str, Any]) -> None:
    path = (
        _serving_subdirectory(root, "revisions")
        / f"{revision['strategy_revision']}.json"
    )
    write_immutable_json(path, revision)


def _write_active_pointer(root: Path, revision: dict[str, Any]) -> None:
    pointer = {
        "schema_version": ACTIVE_POINTER_SCHEMA_VERSION,
        "strategy_id": revision["strategy_id"],
        "strategy_revision": revision["strategy_revision"],
    }
    path = _strategy_directory(root) / "active.json"
    if path.is_symlink():
        raise RetrievalServingConfigurationError(
            "active retrieval pointer must not be a symbolic link"
        )
    atomic_write_text(
        path,
        json.dumps(
            pointer,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
    )


def _load_active_revision_artifact(root: Path) -> dict[str, Any] | None:
    active_path = _strategy_directory(root) / "active.json"
    if not active_path.exists() and not active_path.is_symlink():
        return None
    pointer = _load_bounded_json_object(active_path)
    if set(pointer) != {"schema_version", "strategy_id", "strategy_revision"}:
        raise RetrievalServingConfigurationError(
            "active retrieval pointer contract does not match"
        )
    if pointer.get("schema_version") != ACTIVE_POINTER_SCHEMA_VERSION:
        raise RetrievalServingConfigurationError(
            "active retrieval pointer schema is unsupported"
        )
    strategy_id = pointer.get("strategy_id")
    strategy_revision = pointer.get("strategy_revision")
    if not isinstance(strategy_id, str) or not _SAFE_ID_PATTERN.fullmatch(strategy_id):
        raise RetrievalServingConfigurationError(
            "active retrieval strategy ID is invalid"
        )
    if not isinstance(strategy_revision, str) or not _SHA256_PATTERN.fullmatch(
        strategy_revision
    ):
        raise RetrievalServingConfigurationError("active retrieval revision is invalid")
    revision = _load_revision_by_id(root, strategy_revision)
    if revision["strategy_id"] != strategy_id:
        raise RetrievalServingConfigurationError(
            "active retrieval pointer strategy does not match revision"
        )
    return revision


def _load_revision_by_id(root: Path, strategy_revision: str) -> dict[str, Any]:
    if not _SHA256_PATTERN.fullmatch(strategy_revision):
        raise RetrievalServingConfigurationError("retrieval revision ID is invalid")
    path = _serving_subdirectory(root, "revisions") / f"{strategy_revision}.json"
    revision = _load_bounded_json_object(path)
    _validate_strategy_revision(revision, expected_revision=strategy_revision)
    return revision


def _validate_strategy_revision(
    revision: dict[str, Any],
    *,
    expected_revision: str,
) -> None:
    required = {
        "code_revision",
        "config_sha256",
        "index_id",
        "index_schema_version",
        "index_source_sha256",
        "mode",
        "parent_active_revision",
        "pipeline_config",
        "pipeline_id",
        "previous_strategy_revision",
        "proposal_id",
        "proposal_revision",
        "rollback_strategy_revision",
        "schema_version",
        "strategy_id",
        "strategy_revision",
    }
    if set(revision) != required:
        raise RetrievalServingConfigurationError(
            "retrieval strategy revision contract does not match"
        )
    if revision.get("schema_version") != STRATEGY_REVISION_SCHEMA_VERSION:
        raise RetrievalServingConfigurationError(
            "retrieval strategy revision schema is unsupported"
        )
    observed_revision = revision.get("strategy_revision")
    body = {key: value for key, value in revision.items() if key != "strategy_revision"}
    if observed_revision != expected_revision or observed_revision != _sha256_payload(
        body
    ):
        raise RetrievalServingConfigurationError(
            "retrieval strategy revision does not match content"
        )
    strategy_id = revision.get("strategy_id")
    code_revision = revision.get("code_revision")
    index_id = revision.get("index_id")
    source_sha256 = revision.get("index_source_sha256")
    if not isinstance(strategy_id, str) or not _SAFE_ID_PATTERN.fullmatch(strategy_id):
        raise RetrievalServingConfigurationError("retrieval strategy ID is invalid")
    if not isinstance(code_revision, str) or not _CODE_REVISION_PATTERN.fullmatch(
        code_revision
    ):
        raise RetrievalServingConfigurationError("retrieval code revision is invalid")
    if not isinstance(index_id, str) or not _SAFE_ID_PATTERN.fullmatch(index_id):
        raise RetrievalServingConfigurationError("retrieval index ID is invalid")
    if not isinstance(source_sha256, str) or not _SHA256_PATTERN.fullmatch(
        source_sha256
    ):
        raise RetrievalServingConfigurationError(
            "retrieval index source hash is invalid"
        )
    for field in (
        "parent_active_revision",
        "previous_strategy_revision",
        "rollback_strategy_revision",
    ):
        value = revision.get(field)
        if value is not None and (
            not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value)
        ):
            raise RetrievalServingConfigurationError(f"retrieval {field} is invalid")
    if revision.get("previous_strategy_revision") != revision.get(
        "parent_active_revision"
    ):
        raise RetrievalServingConfigurationError(
            "retrieval previous revision does not match parent"
        )
    mode = revision.get("mode")
    if mode == "baseline":
        if (
            strategy_id != BASELINE_STRATEGY_ID
            or revision.get("index_schema_version") != CATALOG_SCHEMA_VERSION
            or any(
                revision.get(field) is not None
                for field in (
                    "config_sha256",
                    "pipeline_config",
                    "pipeline_id",
                    "proposal_id",
                    "proposal_revision",
                    "parent_active_revision",
                    "previous_strategy_revision",
                    "rollback_strategy_revision",
                )
            )
        ):
            raise RetrievalServingConfigurationError(
                "baseline retrieval revision is incompatible"
            )
    elif mode == "v2":
        if (
            strategy_id != PRODUCTION_STRATEGY_ID
            or revision.get("index_schema_version") != CATALOG_V2_SCHEMA_VERSION
            or revision.get("pipeline_id") != PRODUCTION_PIPELINE_ID
            or revision.get("config_sha256") != PRODUCTION_PIPELINE_CONFIG_SHA256
        ):
            raise RetrievalServingConfigurationError(
                "catalog v2 retrieval revision is incompatible"
            )
        validate_production_pipeline_config(
            revision.get("pipeline_config"),
            config_sha256=revision.get("config_sha256"),
            pipeline_id=revision.get("pipeline_id"),
        )
        if not isinstance(revision.get("proposal_id"), str) or not isinstance(
            revision.get("proposal_revision"), str
        ):
            raise RetrievalServingConfigurationError(
                "catalog v2 proposal identity is invalid"
            )
        if revision.get("rollback_strategy_revision") is None:
            raise RetrievalServingConfigurationError(
                "catalog v2 revision has no rollback target"
            )
    else:
        raise RetrievalServingConfigurationError(
            "retrieval serving mode is unsupported"
        )


def _resolve_artifact_root(artifact_root: str | Path) -> Path:
    requested = Path(artifact_root)
    if requested.is_symlink():
        raise RetrievalServingConfigurationError(
            "retrieval artifact root must not be a symbolic link"
        )
    try:
        resolved = requested.resolve(strict=True)
    except FileNotFoundError as exc:
        raise RetrievalServingConfigurationError(
            "retrieval artifact root does not exist"
        ) from exc
    if not resolved.is_dir():
        raise RetrievalServingConfigurationError(
            "retrieval artifact root must be a directory"
        )
    return resolved


def _strategy_directory(root: Path) -> Path:
    directory = root / RETRIEVAL_STRATEGY_DIRECTORY
    if directory.is_symlink():
        raise RetrievalServingConfigurationError(
            "retrieval strategy directory must not be a symbolic link"
        )
    return directory


def _serving_subdirectory(root: Path, name: str) -> Path:
    if name not in {"activation-receipts", "revisions", "rollback-receipts"}:
        raise ValueError("unsupported retrieval serving subdirectory")
    directory = _strategy_directory(root) / name
    if directory.is_symlink():
        raise RetrievalServingConfigurationError(
            "retrieval serving subdirectory must not be a symbolic link"
        )
    return directory


def _load_bounded_json_object(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except (FileNotFoundError, OSError) as exc:
        raise RetrievalServingConfigurationError(
            "retrieval serving artifact is unavailable"
        ) from exc
    try:
        with os.fdopen(descriptor, "rb") as handle:
            contents = handle.read(MAX_SERVING_ARTIFACT_BYTES + 1)
    finally:
        # fdopen owns and closes the descriptor on the normal and error paths.
        pass
    if len(contents) > MAX_SERVING_ARTIFACT_BYTES:
        raise RetrievalServingConfigurationError(
            "retrieval serving artifact exceeds the size limit"
        )

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key, value in pairs:
            if key in payload:
                raise RetrievalServingConfigurationError(
                    "retrieval serving artifact contains duplicate keys"
                )
            payload[key] = value
        return payload

    try:
        payload = json.loads(
            contents.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                RetrievalServingConfigurationError(
                    "retrieval serving artifact contains a non-finite number"
                )
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetrievalServingConfigurationError(
            "retrieval serving artifact is invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise RetrievalServingConfigurationError(
            "retrieval serving artifact must be an object"
        )
    return payload


@contextmanager
def _serving_lock(root: Path) -> Iterator[None]:
    directory = _strategy_directory(root)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / ".serving.lock"
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    locked = False
    try:
        deadline = time.monotonic() + SERVING_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError("retrieval serving lock timed out") from exc
                time.sleep(0.05)
        yield
    finally:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _deployment_revision(revision_provider: Callable[..., str]) -> str:
    project_root = Path(__file__).resolve().parents[3]
    try:
        revision = revision_provider(project_root)
    except TypeError:
        revision = revision_provider()
    if not isinstance(revision, str) or not _CODE_REVISION_PATTERN.fullmatch(
        revision.strip()
    ):
        raise RetrievalActivationRejected(
            "deployment revision provider returned an invalid revision"
        )
    return revision.strip()


def _sha256_payload(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _content_id(prefix: str, payload: dict[str, Any]) -> str:
    return f"{prefix}-{_sha256_payload(payload)[:12]}"


def _serving_error_code(exc: BaseException) -> str:
    if isinstance(exc, RetrievalServingError):
        return exc.error_code
    if isinstance(exc, TimeoutError):
        return "retrieval_serving_lock_timeout"
    if isinstance(exc, (ValueError, TypeError)):
        return "retrieval_serving_contract_invalid"
    if isinstance(exc, OSError):
        return "retrieval_serving_io_failure"
    return "retrieval_serving_internal_error"
