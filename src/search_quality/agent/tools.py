"""Smoke-only search evaluation tools exposed to the Agent runtime."""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import polars as pl
from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from search_quality.evaluation.artifacts import (
    require_clean_code_revision,
    write_immutable_json,
)
from search_quality.evaluation.baseline import (
    DEFAULT_RANDOM_SEED,
    run_candidate_baseline,
)
from search_quality.evaluation.comparison import (
    COMPARISON_EPSILON,
    compare_runs,
    load_run_from_store,
    validate_trusted_run,
)
from search_quality.evaluation.datasets import EvaluationProfile, sha256_file
from search_quality.evaluation.relevance import RelevancePolicy

from .contracts import RUN_ID_PATTERN, StrictModel
from .errors import AgentToolError
from .registry import AgentToolRegistry, ToolSpec

RUN_ID_RE = re.compile(f"{RUN_ID_PATTERN}\\Z")
ALLOWED_RANKER_IDS = frozenset(
    {
        "candidate-random-v1",
        "candidate-title-bm25-exact-boost-v1",
        "candidate-title-keyword-overlap-v1",
        "candidate-title-bm25-v1",
    }
)
AGGREGATE_METRICS = ("ndcg@5", "ndcg@10", "mrr@10", "success@1", "success@5")
COMPARISON_ID_PATTERN = r"comparison-[0-9a-f]{12}"

FiniteFloat = Annotated[float, Field(strict=True, allow_inf_nan=False)]
UnitFloat = Annotated[
    float,
    Field(strict=True, allow_inf_nan=False, ge=0.0, le=1.0),
]
DeltaFloat = Annotated[
    float,
    Field(strict=True, allow_inf_nan=False, ge=-1.0, le=1.0),
]


class MetricValues(StrictModel):
    """The exact metric surface emitted by one evaluated Run or Query."""

    ndcg_at_5: UnitFloat = Field(alias="ndcg@5")
    ndcg_at_10: UnitFloat = Field(alias="ndcg@10")
    mrr_at_10: UnitFloat = Field(alias="mrr@10")
    success_at_1: UnitFloat = Field(alias="success@1")
    success_at_5: UnitFloat = Field(alias="success@5")


class EvaluationBoundaryOutput(StrictModel):
    full_catalog_recall_claimed: Literal[False]
    task: Literal["judged-candidate-reranking"]
    unjudged_products_are_irrelevant: Literal[False]


RankerId = Literal[
    "candidate-random-v1",
    "candidate-title-bm25-exact-boost-v1",
    "candidate-title-keyword-overlap-v1",
    "candidate-title-bm25-v1",
]


class RunSummaryPayload(StrictModel):
    evaluation_boundary: EvaluationBoundaryOutput
    metrics: MetricValues
    profile: Literal["smoke"]
    query_count: StrictInt = Field(ge=1)
    ranker_id: RankerId
    run_id: StrictStr = Field(pattern=rf"^{RUN_ID_PATTERN}$")


class RunRankerPayload(RunSummaryPayload):
    created: StrictBool


class QueryEvaluationPayload(StrictModel):
    candidate_count: StrictInt = Field(ge=1)
    locale: StrictStr = Field(pattern=r"^[a-z]{2,8}$")
    metrics: MetricValues
    query_id: StrictInt = Field(ge=1)
    ranker_id: RankerId
    run_id: StrictStr = Field(pattern=rf"^{RUN_ID_PATTERN}$")


class RankingCandidateOutput(StrictModel):
    example_id: StrictInt = Field(ge=1)
    gain: UnitFloat
    label: Literal["E", "S", "C", "I"]
    locale: StrictStr = Field(pattern=r"^[a-z]{2,8}$")
    product_id: StrictStr = Field(min_length=1, max_length=128)
    product_title: StrictStr = Field(min_length=1, max_length=4096)
    rank: StrictInt = Field(ge=1)
    score: FiniteFloat


class LabelCountsOutput(StrictModel):
    exact: StrictInt = Field(alias="E", ge=0)
    substitute: StrictInt = Field(alias="S", ge=0)
    complement: StrictInt = Field(alias="C", ge=0)
    irrelevant: StrictInt = Field(alias="I", ge=0)

    @property
    def total(self) -> int:
        return self.exact + self.substitute + self.complement + self.irrelevant


class InspectQueryPayload(QueryEvaluationPayload):
    candidates: list[RankingCandidateOutput] = Field(min_length=1)
    label_counts: LabelCountsOutput
    query_text: StrictStr = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def validate_candidates(self) -> Self:
        if len(self.candidates) != self.candidate_count:
            raise ValueError("candidate_count does not match candidates")
        if self.label_counts.total != self.candidate_count:
            raise ValueError("label_counts do not match candidate_count")
        if [item.rank for item in self.candidates] != list(
            range(1, self.candidate_count + 1)
        ):
            raise ValueError("candidate ranks must be contiguous and ordered")
        if len({item.product_id for item in self.candidates}) != self.candidate_count:
            raise ValueError("candidate product IDs must be unique")
        if any(item.locale != self.locale for item in self.candidates):
            raise ValueError("candidate locale does not match Query locale")
        return self


class MetricDeltaOutput(StrictModel):
    baseline: UnitFloat
    candidate: UnitFloat
    delta: DeltaFloat

    @model_validator(mode="after")
    def validate_delta(self) -> Self:
        if not math.isclose(
            self.delta,
            self.candidate - self.baseline,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("metric delta does not match candidate minus baseline")
        return self


class AggregateMetricDeltas(StrictModel):
    ndcg_at_5: MetricDeltaOutput = Field(alias="ndcg@5")
    ndcg_at_10: MetricDeltaOutput = Field(alias="ndcg@10")
    mrr_at_10: MetricDeltaOutput = Field(alias="mrr@10")
    success_at_1: MetricDeltaOutput = Field(alias="success@1")
    success_at_5: MetricDeltaOutput = Field(alias="success@5")

    def items(self) -> tuple[tuple[str, MetricDeltaOutput], ...]:
        return (
            ("ndcg@5", self.ndcg_at_5),
            ("ndcg@10", self.ndcg_at_10),
            ("mrr@10", self.mrr_at_10),
            ("success@1", self.success_at_1),
            ("success@5", self.success_at_5),
        )


class MetricOutcomeCounts(StrictModel):
    improved: StrictInt = Field(ge=0)
    regressed: StrictInt = Field(ge=0)
    tied: StrictInt = Field(ge=0)

    @property
    def total(self) -> int:
        return self.improved + self.regressed + self.tied


class OutcomeCountsOutput(StrictModel):
    ndcg_at_5: MetricOutcomeCounts = Field(alias="ndcg@5")
    ndcg_at_10: MetricOutcomeCounts = Field(alias="ndcg@10")
    mrr_at_10: MetricOutcomeCounts = Field(alias="mrr@10")
    success_at_1: MetricOutcomeCounts = Field(alias="success@1")
    success_at_5: MetricOutcomeCounts = Field(alias="success@5")

    def values(self) -> tuple[MetricOutcomeCounts, ...]:
        return (
            self.ndcg_at_5,
            self.ndcg_at_10,
            self.mrr_at_10,
            self.success_at_1,
            self.success_at_5,
        )

    def by_name(self, name: str) -> MetricOutcomeCounts:
        return dict(
            (
                ("ndcg@5", self.ndcg_at_5),
                ("ndcg@10", self.ndcg_at_10),
                ("mrr@10", self.mrr_at_10),
                ("success@1", self.success_at_1),
                ("success@5", self.success_at_5),
            )
        )[name]


class QueryDeltaOutput(StrictModel):
    changed_rank_count: StrictInt = Field(ge=0)
    ndcg_at_10_delta: DeltaFloat = Field(alias="ndcg@10_delta")
    query_id: StrictInt = Field(ge=1)
    top_10_changed: StrictBool


class CompareRunsPayload(StrictModel):
    aggregate_metrics: AggregateMetricDeltas
    baseline_run_id: StrictStr = Field(pattern=rf"^{RUN_ID_PATTERN}$")
    candidate_run_id: StrictStr = Field(pattern=rf"^{RUN_ID_PATTERN}$")
    comparison_id: StrictStr = Field(pattern=rf"^{COMPARISON_ID_PATTERN}$")
    comparison_epsilon: FiniteFloat = Field(gt=0.0, le=1e-6)
    improvements: list[QueryDeltaOutput] = Field(max_length=5)
    outcome_counts: OutcomeCountsOutput
    query_count: StrictInt = Field(ge=1)
    regressions: list[QueryDeltaOutput] = Field(max_length=5)

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        if self.baseline_run_id == self.candidate_run_id:
            raise ValueError("comparison Runs must differ")
        if self.comparison_epsilon != COMPARISON_EPSILON:
            raise ValueError("comparison epsilon does not match comparator policy")
        if any(
            counts.total != self.query_count for counts in self.outcome_counts.values()
        ):
            raise ValueError("outcome counts do not match query_count")
        for name, metric in self.aggregate_metrics.items():
            counts = self.outcome_counts.by_name(name)
            if metric.delta > COMPARISON_EPSILON and counts.improved == 0:
                raise ValueError("positive aggregate requires an improved Query")
            if metric.delta < -COMPARISON_EPSILON and counts.regressed == 0:
                raise ValueError("negative aggregate requires a regressed Query")
            if (
                counts.improved == 0
                and counts.regressed == 0
                and abs(metric.delta) > COMPARISON_EPSILON
            ):
                raise ValueError("all-tied Queries require a tied aggregate")
        regression_ids = [item.query_id for item in self.regressions]
        improvement_ids = [item.query_id for item in self.improvements]
        if len(regression_ids) != len(set(regression_ids)):
            raise ValueError("regression Query IDs must be unique")
        if len(improvement_ids) != len(set(improvement_ids)):
            raise ValueError("improvement Query IDs must be unique")
        if set(regression_ids) & set(improvement_ids):
            raise ValueError("Query cannot be both an improvement and regression")
        if any(
            item.ndcg_at_10_delta >= -COMPARISON_EPSILON for item in self.regressions
        ):
            raise ValueError("regressions must exceed the nDCG@10 tie threshold")
        if any(
            item.ndcg_at_10_delta <= COMPARISON_EPSILON for item in self.improvements
        ):
            raise ValueError("improvements must exceed the nDCG@10 tie threshold")
        ndcg_counts = self.outcome_counts.ndcg_at_10
        if len(self.regressions) != min(ndcg_counts.regressed, 5):
            raise ValueError("regression summary does not match nDCG@10 counts")
        if len(self.improvements) != min(ndcg_counts.improved, 5):
            raise ValueError("improvement summary does not match nDCG@10 counts")
        if self.regressions != sorted(
            self.regressions,
            key=lambda item: (item.ndcg_at_10_delta, item.query_id),
        ):
            raise ValueError("regressions must be ordered from worst to best")
        if self.improvements != sorted(
            self.improvements,
            key=lambda item: (-item.ndcg_at_10_delta, item.query_id),
        ):
            raise ValueError("improvements must be ordered from best to worst")
        return self


class RunRankerOutput(StrictModel):
    evidence_ref: StrictStr = Field(pattern=rf"^run:{RUN_ID_PATTERN}$")
    payload: RunRankerPayload

    @model_validator(mode="after")
    def validate_reference(self) -> Self:
        if self.evidence_ref != f"run:{self.payload.run_id}":
            raise ValueError("Run evidence reference does not match payload")
        return self


class EvaluateRunOutput(StrictModel):
    evidence_ref: StrictStr
    payload: RunSummaryPayload | QueryEvaluationPayload

    @model_validator(mode="after")
    def validate_reference(self) -> Self:
        if isinstance(self.payload, RunSummaryPayload):
            expected = f"run:{self.payload.run_id}"
        else:
            expected = f"query:{self.payload.run_id}:{self.payload.query_id}"
        if self.evidence_ref != expected:
            raise ValueError("evaluation evidence reference does not match payload")
        return self


class InspectQueryOutput(StrictModel):
    evidence_ref: StrictStr
    payload: InspectQueryPayload

    @model_validator(mode="after")
    def validate_reference(self) -> Self:
        expected = f"query:{self.payload.run_id}:{self.payload.query_id}"
        if self.evidence_ref != expected:
            raise ValueError("Query evidence reference does not match payload")
        return self


class CompareRunsOutput(StrictModel):
    evidence_ref: StrictStr = Field(pattern=rf"^comparison:{COMPARISON_ID_PATTERN}$")
    payload: CompareRunsPayload

    @model_validator(mode="after")
    def validate_reference(self) -> Self:
        if self.evidence_ref != f"comparison:{self.payload.comparison_id}":
            raise ValueError("comparison evidence reference does not match payload")
        return self


class RunRankerInput(StrictModel):
    ranker_name: Literal[
        "random", "keyword-overlap", "title-bm25", "title-bm25-exact-boost"
    ]


class EvaluateRunInput(StrictModel):
    run_id: StrictStr = Field(pattern=rf"^{RUN_ID_PATTERN}$")
    query_id: StrictInt | None = Field(default=None, ge=1)


class InspectQueryInput(StrictModel):
    run_id: StrictStr = Field(pattern=rf"^{RUN_ID_PATTERN}$")
    query_id: StrictInt = Field(ge=1)


class CompareRunsInput(StrictModel):
    baseline_run_id: StrictStr = Field(pattern=rf"^{RUN_ID_PATTERN}$")
    candidate_run_id: StrictStr = Field(pattern=rf"^{RUN_ID_PATTERN}$")

    @model_validator(mode="after")
    def validate_run_pair(self) -> Self:
        if self.baseline_run_id == self.candidate_run_id:
            raise ValueError("comparison Runs must differ")
        return self


@dataclass(frozen=True, slots=True)
class ValidatedRun:
    payload: dict[str, Any]
    queries: dict[tuple[str, int], dict[str, Any]]
    artifact_sha256: str


class TrustedRunRegistry:
    """Resolve only explicitly admitted smoke Runs from one trusted store."""

    def __init__(
        self,
        *,
        store_root: str | Path,
        project_root: str | Path,
        manifest_path: str | Path,
        allowed_run_ids: Iterable[str] = (),
    ) -> None:
        configured_root = Path(store_root)
        if configured_root.is_symlink():
            raise ValueError("trusted Run store must not be a symbolic link")
        configured_root.mkdir(parents=True, exist_ok=True)
        self.store_root = configured_root.resolve(strict=True)
        if not self.store_root.is_dir():
            raise ValueError("trusted Run store must be a directory")
        self.project_root = Path(project_root).resolve(strict=True)
        self.manifest_path = Path(manifest_path).resolve(strict=True)
        self._allowed: dict[str, str] = {}
        self._lock = threading.RLock()
        for run_id in allowed_run_ids:
            self.admit_existing(run_id)

    @property
    def allowed_run_ids(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._allowed)

    def admit_existing(self, run_id: str) -> ValidatedRun:
        validated = self._load_and_validate(run_id, require_admitted=False)
        with self._lock:
            pinned = self._allowed.get(run_id)
            if pinned is not None and pinned != validated.artifact_sha256:
                raise AgentToolError("run_integrity_failed")
            self._allowed[run_id] = validated.artifact_sha256
        return validated

    def register_generated(self, run: dict[str, Any]) -> tuple[ValidatedRun, bool]:
        run_id = run.get("run_id")
        if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
            raise AgentToolError("run_integrity_failed")
        output = self.store_root / f"{run_id}.json"
        created = not output.exists()
        try:
            write_immutable_json(output, run)
        except RuntimeError as exc:
            raise AgentToolError("artifact_collision") from exc
        except OSError as exc:
            raise AgentToolError("artifact_store_unavailable", retryable=True) from exc
        validated = self.admit_existing(run_id)
        return validated, created

    def resolve(self, run_id: str) -> ValidatedRun:
        return self._load_and_validate(run_id, require_admitted=True)

    def _load_and_validate(
        self, run_id: str, *, require_admitted: bool
    ) -> ValidatedRun:
        if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
            raise AgentToolError("invalid_argument")
        with self._lock:
            pinned_sha256 = self._allowed.get(run_id)
        if require_admitted and pinned_sha256 is None:
            raise AgentToolError("run_not_trusted")
        path = self.store_root / f"{run_id}.json"
        if path.is_symlink():
            raise AgentToolError("run_integrity_failed")
        if not path.is_file():
            raise AgentToolError("run_not_found")
        try:
            artifact_sha256_before = sha256_file(path)
            if pinned_sha256 is not None and artifact_sha256_before != pinned_sha256:
                raise AgentToolError("run_integrity_failed")
            payload = load_run_from_store(path, store_root=self.store_root)
            queries = validate_trusted_run(
                payload,
                expected_profile="smoke",
                project_root=self.project_root,
                manifest_path=self.manifest_path,
                role="Agent Run",
            )
            if path.is_symlink():
                raise AgentToolError("run_integrity_failed")
            artifact_sha256 = sha256_file(path)
            if artifact_sha256 != artifact_sha256_before or (
                pinned_sha256 is not None and artifact_sha256 != pinned_sha256
            ):
                raise AgentToolError("run_integrity_failed")
        except AgentToolError:
            raise
        except FileNotFoundError as exc:
            raise AgentToolError("run_not_found") from exc
        except OSError as exc:
            raise AgentToolError("artifact_store_unavailable", retryable=True) from exc
        except (TypeError, ValueError, RuntimeError) as exc:
            raise AgentToolError("run_integrity_failed") from exc
        ranker = payload.get("ranker")
        ranker_id = ranker.get("ranker_id") if isinstance(ranker, dict) else None
        if ranker_id not in ALLOWED_RANKER_IDS:
            raise AgentToolError("ranker_not_allowed")
        return ValidatedRun(
            payload=payload,
            queries=queries,
            artifact_sha256=artifact_sha256,
        )


class SearchEvaluationTools:
    """Thin deterministic adapters over the Stage 2 Search Harness."""

    def __init__(
        self,
        *,
        project_root: str | Path,
        registry: TrustedRunRegistry,
        revision_provider: Callable[[Path], str] = require_clean_code_revision,
    ) -> None:
        self.project_root = Path(project_root).resolve(strict=True)
        self.registry = registry
        self.manifest_path = (
            self.project_root / "data" / "manifests" / "esci-stage1.json"
        )
        self.policy_path = (
            self.project_root / "configs" / "evaluation" / "esci-primary-v1.json"
        )
        self.revision_provider = revision_provider

    def build_registry(self) -> AgentToolRegistry:
        return AgentToolRegistry(
            (
                ToolSpec(
                    name="run_ranker",
                    capability="create_smoke_run",
                    input_model=RunRankerInput,
                    output_model=RunRankerOutput,
                    handler=self.run_ranker,
                ),
                ToolSpec(
                    name="evaluate_run",
                    capability="read_smoke_run",
                    input_model=EvaluateRunInput,
                    output_model=EvaluateRunOutput,
                    handler=self.evaluate_run,
                ),
                ToolSpec(
                    name="inspect_query",
                    capability="read_smoke_query_evidence",
                    input_model=InspectQueryInput,
                    output_model=InspectQueryOutput,
                    handler=self.inspect_query,
                ),
                ToolSpec(
                    name="compare_runs",
                    capability="compare_smoke_runs",
                    input_model=CompareRunsInput,
                    output_model=CompareRunsOutput,
                    handler=self.compare_runs,
                ),
            )
        )

    def run_ranker(self, request: RunRankerInput) -> dict[str, Any]:
        try:
            revision = self.revision_provider(self.project_root)
        except RuntimeError as exc:
            raise AgentToolError("worktree_dirty") from exc
        profile = EvaluationProfile.from_stage1_manifest(
            profile_id="smoke",
            project_root=self.project_root,
            manifest_path=self.manifest_path,
        )
        policy = RelevancePolicy.from_path(self.policy_path)
        try:
            run = run_candidate_baseline(
                profile,
                policy=policy,
                code_revision=revision,
                ranker_name=request.ranker_name,
                random_seed=DEFAULT_RANDOM_SEED,
            )
            validated, created = self.registry.register_generated(run)
        except AgentToolError:
            raise
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            raise AgentToolError("run_integrity_failed") from exc
        return {
            "evidence_ref": f"run:{run['run_id']}",
            "payload": self._run_summary(validated.payload, created=created),
        }

    def evaluate_run(self, request: EvaluateRunInput) -> dict[str, Any]:
        run = self.registry.resolve(request.run_id)
        if request.query_id is None:
            return {
                "evidence_ref": f"run:{request.run_id}",
                "payload": self._run_summary(run.payload),
            }
        query = self._query(run, request.query_id)
        return {
            "evidence_ref": f"query:{request.run_id}:{request.query_id}",
            "payload": {
                "candidate_count": query["candidate_count"],
                "locale": query["locale"],
                "metrics": query["metrics"],
                "query_id": query["query_id"],
                "ranker_id": run.payload["ranker"]["ranker_id"],
                "run_id": request.run_id,
            },
        }

    def inspect_query(self, request: InspectQueryInput) -> dict[str, Any]:
        run = self.registry.resolve(request.run_id)
        query = self._query(run, request.query_id)
        profile = EvaluationProfile.from_stage1_manifest(
            profile_id="smoke",
            project_root=self.project_root,
            manifest_path=self.manifest_path,
        )
        if sha256_file(profile.path) != profile.file_sha256:
            raise AgentToolError("data_integrity_failed")
        frame = pl.read_parquet(profile.path).filter(
            (pl.col("query_id") == request.query_id)
            & (pl.col("product_locale") == query["locale"])
        )
        if frame.is_empty():
            raise AgentToolError("query_not_found")
        title_by_product = {
            str(row["product_id"]): str(row["product_title"])
            for row in frame.select("product_id", "product_title").iter_rows(named=True)
        }
        ranked_ids = {str(item["product_id"]) for item in query["ranking"]}
        if ranked_ids != set(title_by_product):
            raise AgentToolError("data_integrity_failed")
        candidates = [
            {
                **item,
                "product_title": title_by_product[str(item["product_id"])],
            }
            for item in query["ranking"]
        ]
        label_counts = {
            label: sum(item["label"] == label for item in candidates)
            for label in ("E", "S", "C", "I")
        }
        return {
            "evidence_ref": f"query:{request.run_id}:{request.query_id}",
            "payload": {
                "candidate_count": query["candidate_count"],
                "candidates": candidates,
                "label_counts": label_counts,
                "locale": query["locale"],
                "metrics": query["metrics"],
                "query_id": query["query_id"],
                "query_text": query["query_text"],
                "ranker_id": run.payload["ranker"]["ranker_id"],
                "run_id": request.run_id,
            },
        }

    def compare_runs(self, request: CompareRunsInput) -> dict[str, Any]:
        baseline = self.registry.resolve(request.baseline_run_id)
        candidate = self.registry.resolve(request.candidate_run_id)
        try:
            revision = self.revision_provider(self.project_root)
        except RuntimeError as exc:
            raise AgentToolError("worktree_dirty") from exc
        try:
            comparison = compare_runs(
                baseline.payload,
                candidate.payload,
                comparator_revision=revision,
                expected_profile="smoke",
                project_root=self.project_root,
                manifest_path=self.manifest_path,
            )
            comparison_dir = self.registry.store_root / "comparisons"
            if comparison_dir.is_symlink():
                raise AgentToolError("artifact_store_unavailable")
            write_immutable_json(
                comparison_dir / f"{comparison['comparison_id']}.json", comparison
            )
        except AgentToolError:
            raise
        except OSError as exc:
            raise AgentToolError("artifact_store_unavailable", retryable=True) from exc
        except (TypeError, ValueError, RuntimeError) as exc:
            raise AgentToolError("runs_incompatible") from exc
        comparison_epsilon = float(comparison["comparison_epsilon"])
        regressions = sorted(
            (
                item
                for item in comparison["per_query"]
                if item["metrics"]["ndcg@10"]["delta"] < -comparison_epsilon
            ),
            key=lambda item: (item["metrics"]["ndcg@10"]["delta"], item["query_id"]),
        )[:5]
        improvements = sorted(
            (
                item
                for item in comparison["per_query"]
                if item["metrics"]["ndcg@10"]["delta"] > comparison_epsilon
            ),
            key=lambda item: (
                -item["metrics"]["ndcg@10"]["delta"],
                item["query_id"],
            ),
        )[:5]
        return {
            "evidence_ref": f"comparison:{comparison['comparison_id']}",
            "payload": {
                "aggregate_metrics": comparison["aggregate_metrics"],
                "baseline_run_id": request.baseline_run_id,
                "candidate_run_id": request.candidate_run_id,
                "comparison_id": comparison["comparison_id"],
                "comparison_epsilon": comparison_epsilon,
                "improvements": [self._query_delta(item) for item in improvements],
                "outcome_counts": comparison["outcome_counts"],
                "query_count": comparison["compatibility"]["query_count"],
                "regressions": [self._query_delta(item) for item in regressions],
            },
        }

    @staticmethod
    def _query(run: ValidatedRun, query_id: int) -> dict[str, Any]:
        matches = [
            query
            for (locale, observed), query in run.queries.items()
            if observed == query_id
        ]
        if len(matches) != 1:
            raise AgentToolError("query_not_found")
        return matches[0]

    @staticmethod
    def _run_summary(
        run: dict[str, Any], *, created: bool | None = None
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "evaluation_boundary": run["evaluation_boundary"],
            "metrics": {name: run["metrics"][name] for name in AGGREGATE_METRICS},
            "profile": run["dataset"]["profile"],
            "query_count": run["dataset"]["queries"],
            "ranker_id": run["ranker"]["ranker_id"],
            "run_id": run["run_id"],
        }
        if created is not None:
            result["created"] = created
        return result

    @staticmethod
    def _query_delta(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "changed_rank_count": item["changed_rank_count"],
            "ndcg@10_delta": item["metrics"]["ndcg@10"]["delta"],
            "query_id": item["query_id"],
            "top_10_changed": item["top_10_changed"],
        }
