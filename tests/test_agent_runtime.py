from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from pydantic import BaseModel, Field, StrictInt, StrictStr

from search_quality.agent.contracts import (
    RUN_ID_PATTERN,
    AgentTask,
    FinishDecision,
    StrictModel,
    TerminalOutcome,
    TerminalResult,
    ToolAction,
)
from search_quality.agent.errors import AgentToolError
from search_quality.agent.planner import FakeBranchingPlanner
from search_quality.agent.registry import AgentToolRegistry, ToolSpec
from search_quality.agent.replay import TraceReplayer
from search_quality.agent.runtime import AgentRuntime, RuntimePolicy
from search_quality.agent.tools import (
    CompareRunsInput,
    CompareRunsOutput,
    InspectQueryInput,
    SearchEvaluationTools,
    TrustedRunRegistry,
)
from search_quality.agent.trace import (
    MAX_TRACE_BYTES,
    TraceStore,
    compute_event_hash,
    compute_terminal_hash,
)
from search_quality.evaluation.baseline import run_candidate_baseline
from search_quality.evaluation.datasets import EvaluationProfile
from search_quality.evaluation.relevance import RelevancePolicy
from search_quality.observability import configure_logging

ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "data/manifests/esci-stage1.json"
POLICY = ROOT / "configs/evaluation/esci-primary-v1.json"
METRIC_NAMES = ("ndcg@5", "ndcg@10", "mrr@10", "success@1", "success@5")


class _PermissiveToolOutput(StrictModel):
    """Test-only envelope that leaves Runtime payload limits observable."""

    evidence_ref: StrictStr
    payload: dict


class _SyntheticInspectPayload(StrictModel):
    query_id: StrictInt = Field(ge=1)
    run_id: StrictStr = Field(pattern=rf"^{RUN_ID_PATTERN}$")


class _SyntheticInspectOutput(StrictModel):
    evidence_ref: StrictStr
    payload: _SyntheticInspectPayload


@pytest.fixture(scope="module")
def smoke_runs() -> tuple[dict, dict]:
    profile = EvaluationProfile.from_stage1_manifest(
        profile_id="smoke", project_root=ROOT, manifest_path=MANIFEST
    )
    policy = RelevancePolicy.from_path(POLICY)
    return (
        run_candidate_baseline(
            profile,
            policy=policy,
            code_revision="a" * 40,
            ranker_name="random",
        ),
        run_candidate_baseline(
            profile,
            policy=policy,
            code_revision="b" * 40,
            ranker_name="title-bm25",
        ),
    )


def _real_runtime(
    tmp_path: Path, smoke_runs: tuple[dict, dict]
) -> tuple[AgentRuntime, AgentTask, TraceStore]:
    store = tmp_path / "runs"
    store.mkdir()
    for run in smoke_runs:
        (store / f"{run['run_id']}.json").write_text(json.dumps(run), encoding="utf-8")
    baseline_id, candidate_id = (run["run_id"] for run in smoke_runs)
    trusted_runs = TrustedRunRegistry(
        store_root=store,
        project_root=ROOT,
        manifest_path=MANIFEST,
        allowed_run_ids=(baseline_id, candidate_id),
    )
    tools = SearchEvaluationTools(
        project_root=ROOT,
        registry=trusted_runs,
        revision_provider=lambda _root: "c" * 40,
    ).build_registry()
    trace_store = TraceStore(tmp_path / "traces")
    runtime = AgentRuntime(
        planner=FakeBranchingPlanner(), tools=tools, trace_store=trace_store
    )
    task = AgentTask(
        task_id="compare-random-bm25",
        baseline_run_id=baseline_id,
        candidate_run_id=candidate_id,
    )
    return runtime, task, trace_store


def test_runtime_branches_on_regression_and_trace_replays_without_tools(
    tmp_path: Path, smoke_runs: tuple[dict, dict]
) -> None:
    runtime, task, trace_store = _real_runtime(tmp_path, smoke_runs)
    result = runtime.run(task)

    assert result.state == "completed"
    assert result.outcome == TerminalOutcome.INCONCLUSIVE
    assert result.reason_code == "mixed_metric_or_query_evidence"
    assert result.steps_used == 3
    assert result.tool_calls_used == 2
    assert result.report["inspected_queries"][0]["query_id"] == 15281
    assert any(ref.startswith("comparison:") for ref in result.evidence_refs)
    assert any(ref.endswith(":15281") for ref in result.evidence_refs)

    replayed = TraceReplayer(trace_store).replay(result.trace_id)
    assert replayed == result


def test_replay_trace_returns_the_exact_single_loaded_snapshot(
    tmp_path: Path, smoke_runs: tuple[dict, dict]
) -> None:
    runtime, task, trace_store = _real_runtime(tmp_path, smoke_runs)
    result = runtime.run(task)

    class SingleLoadStore:
        def __init__(self, delegate: TraceStore) -> None:
            self.delegate = delegate
            self.load_count = 0

        def load(self, trace_id: str):
            self.load_count += 1
            if self.load_count > 1:
                raise AssertionError("Replay snapshot was loaded more than once")
            return self.delegate.load(trace_id)

    store = SingleLoadStore(trace_store)
    snapshot = TraceReplayer(store).replay_trace(result.trace_id)

    assert store.load_count == 1
    assert snapshot.terminal == result


def test_trace_store_rejects_root_replaced_by_symlink(
    tmp_path: Path, smoke_runs: tuple[dict, dict]
) -> None:
    runtime, task, trace_store = _real_runtime(tmp_path, smoke_runs)
    result = runtime.run(task)
    original_root = trace_store.root
    moved_root = tmp_path / "moved-traces"
    replacement_root = tmp_path / "replacement-traces"
    original_root.rename(moved_root)
    replacement_root.mkdir()
    original_root.symlink_to(replacement_root, target_is_directory=True)

    with pytest.raises(ValueError, match="root changed"):
        trace_store.load(result.trace_id)


def test_task_policy_can_skip_query_inspection(
    tmp_path: Path, smoke_runs: tuple[dict, dict]
) -> None:
    runtime, task, _trace_store = _real_runtime(tmp_path, smoke_runs)
    task = task.model_copy(update={"max_regressions_to_inspect": 0})
    result = runtime.run(task)
    assert result.state == "completed"
    assert result.outcome == TerminalOutcome.INCONCLUSIVE
    assert result.steps_used == 2
    assert result.tool_calls_used == 1
    assert result.report["inspected_queries"] == []


def _synthetic_runtime(
    tmp_path: Path,
    *,
    handler,
    planner=None,
    policy: RuntimePolicy | None = None,
    output_model: type[BaseModel] = CompareRunsOutput,
    additional_specs: tuple[ToolSpec, ...] = (),
) -> AgentRuntime:
    registry = AgentToolRegistry(
        (
            ToolSpec(
                name="compare_runs",
                capability="compare_smoke_runs",
                input_model=CompareRunsInput,
                output_model=output_model,
                handler=handler,
            ),
            *additional_specs,
        )
    )
    return AgentRuntime(
        planner=planner or FakeBranchingPlanner(),
        tools=registry,
        trace_store=TraceStore(tmp_path / "traces"),
        policy=policy,
    )


def _task() -> AgentTask:
    return AgentTask(
        task_id="synthetic-comparison",
        baseline_run_id="random-aaaaaaaaaaaa",
        candidate_run_id="bm25-bbbbbbbbbbbb",
    )


def _comparison_result(
    *,
    primary_delta: float = 0.1,
    ndcg_at_5_delta: float | None = None,
    mrr_delta: float = 0.1,
    success_delta: float = 0.1,
    success_at_5_delta: float | None = None,
    regressions: tuple[tuple[int, float], ...] = (),
) -> dict:
    if ndcg_at_5_delta is None:
        ndcg_at_5_delta = primary_delta
    if success_at_5_delta is None:
        success_at_5_delta = success_delta
    deltas = {
        "ndcg@5": ndcg_at_5_delta,
        "ndcg@10": primary_delta,
        "mrr@10": mrr_delta,
        "success@1": success_delta,
        "success@5": success_at_5_delta,
    }

    def metric(delta: float) -> dict[str, float]:
        baseline = 0.4
        return {
            "baseline": baseline,
            "candidate": baseline + delta,
            "delta": delta,
        }

    effective_regressions = list(regressions)
    if primary_delta < 0.0 and not effective_regressions:
        effective_regressions.append((901, primary_delta))
    query_count = max(3, len(effective_regressions) + 1)
    if primary_delta > 0.0:
        ndcg_improved = query_count - len(effective_regressions)
        ndcg_tied = 0
    elif primary_delta < 0.0:
        ndcg_improved = 0
        ndcg_tied = query_count - len(effective_regressions)
    else:
        ndcg_improved = int(bool(effective_regressions))
        ndcg_tied = query_count - len(effective_regressions) - ndcg_improved

    def outcomes(delta: float) -> dict[str, int]:
        return {
            "improved": query_count if delta > 0.0 else 0,
            "regressed": query_count if delta < 0.0 else 0,
            "tied": query_count if delta == 0.0 else 0,
        }

    outcome_counts = {name: outcomes(deltas[name]) for name in METRIC_NAMES}
    outcome_counts["ndcg@10"] = {
        "improved": ndcg_improved,
        "regressed": len(effective_regressions),
        "tied": ndcg_tied,
    }
    improvements = [
        {
            "changed_rank_count": 2,
            "ndcg@10_delta": 0.1,
            "query_id": 201 + index,
            "top_10_changed": True,
        }
        for index in range(ndcg_improved)
    ][:5]

    return {
        "evidence_ref": "comparison:comparison-cccccccccccc",
        "payload": {
            "aggregate_metrics": {name: metric(deltas[name]) for name in METRIC_NAMES},
            "baseline_run_id": "random-aaaaaaaaaaaa",
            "candidate_run_id": "bm25-bbbbbbbbbbbb",
            "comparison_id": "comparison-cccccccccccc",
            "comparison_epsilon": 1e-12,
            "improvements": improvements,
            "outcome_counts": outcome_counts,
            "query_count": query_count,
            "regressions": [
                {
                    "changed_rank_count": 2,
                    "ndcg@10_delta": delta,
                    "query_id": query_id,
                    "top_10_changed": True,
                }
                for query_id, delta in effective_regressions
            ],
        },
    }


def _inspect_spec(handler) -> ToolSpec:
    return ToolSpec(
        name="inspect_query",
        capability="read_smoke_query_evidence",
        input_model=InspectQueryInput,
        output_model=_SyntheticInspectOutput,
        handler=handler,
    )


def test_runtime_can_accept_unmixed_positive_evidence(tmp_path: Path) -> None:
    def handler(_request):
        return _comparison_result()

    result = _synthetic_runtime(tmp_path, handler=handler).run(_task())
    assert result.state == "completed"
    assert result.outcome == TerminalOutcome.ACCEPT
    assert result.tool_calls_used == 1


def test_malformed_comparison_output_fails_closed_instead_of_accepting(
    tmp_path: Path,
) -> None:
    def handler(_request):
        return {
            "evidence_ref": "comparison:comparison-cccccccccccc",
            "payload": {
                "aggregate_metrics": {"ndcg@10": {"delta": 0.1}},
                "regressions": [],
            },
        }

    result = _synthetic_runtime(tmp_path, handler=handler).run(_task())
    assert result.state == "completed"
    assert result.outcome == TerminalOutcome.INCONCLUSIVE
    assert result.reason_code == "comparison_failed"
    assert result.report["failed_tools"] == [
        {"error_code": "invalid_tool_result", "tool_name": "compare_runs"}
    ]


def test_comparison_counts_cannot_hide_a_query_regression(tmp_path: Path) -> None:
    def handler(_request):
        result = _comparison_result()
        result["payload"]["outcome_counts"]["ndcg@10"] = {
            "improved": 2,
            "regressed": 1,
            "tied": 0,
        }
        result["payload"]["improvements"] = result["payload"]["improvements"][:2]
        return result

    result = _synthetic_runtime(tmp_path, handler=handler).run(_task())

    assert result.state == "completed"
    assert result.outcome == TerminalOutcome.INCONCLUSIVE
    assert result.reason_code == "comparison_failed"
    assert result.report["failed_tools"] == [
        {"error_code": "invalid_tool_result", "tool_name": "compare_runs"}
    ]


def test_positive_aggregate_requires_an_improved_query(tmp_path: Path) -> None:
    def handler(_request):
        result = _comparison_result()
        result["payload"]["outcome_counts"]["ndcg@10"] = {
            "improved": 0,
            "regressed": 0,
            "tied": 3,
        }
        result["payload"]["improvements"] = []
        return result

    result = _synthetic_runtime(tmp_path, handler=handler).run(_task())

    assert result.state == "completed"
    assert result.outcome == TerminalOutcome.INCONCLUSIVE
    assert result.reason_code == "comparison_failed"


def test_comparison_epsilon_is_bound_to_the_comparator_policy(tmp_path: Path) -> None:
    def handler(_request):
        result = _comparison_result(primary_delta=1e-14)
        result["payload"]["comparison_epsilon"] = 1e-15
        return result

    result = _synthetic_runtime(tmp_path, handler=handler).run(_task())

    assert result.state == "completed"
    assert result.outcome == TerminalOutcome.INCONCLUSIVE
    assert result.reason_code == "comparison_failed"


def test_complete_metric_tie_is_inconclusive(tmp_path: Path) -> None:
    result = _synthetic_runtime(
        tmp_path,
        handler=lambda _request: _comparison_result(
            primary_delta=0.0,
            mrr_delta=0.0,
            success_delta=0.0,
        ),
    ).run(_task())
    assert result.state == "completed"
    assert result.outcome == TerminalOutcome.INCONCLUSIVE
    assert result.reason_code == "primary_metric_tied"


@pytest.mark.parametrize(
    "overrides",
    [
        {"ndcg_at_5_delta": -0.1},
        {"success_at_5_delta": -0.1},
    ],
)
def test_any_aggregate_metric_regression_prevents_accept(
    tmp_path: Path,
    overrides: dict[str, float],
) -> None:
    result = _synthetic_runtime(
        tmp_path,
        handler=lambda _request: _comparison_result(**overrides),
    ).run(_task())

    assert result.state == "completed"
    assert result.outcome == TerminalOutcome.INCONCLUSIVE
    assert result.reason_code == "mixed_metric_or_query_evidence"


@pytest.mark.parametrize("inspection_limit", [2, 3])
def test_planner_inspects_distinct_regressions_up_to_task_limit(
    tmp_path: Path,
    inspection_limit: int,
) -> None:
    attempted: list[int] = []

    def inspect_handler(request):
        attempted.append(request.query_id)
        return {
            "evidence_ref": f"query:{request.run_id}:{request.query_id}",
            "payload": {
                "query_id": request.query_id,
                "run_id": request.run_id,
            },
        }

    runtime = _synthetic_runtime(
        tmp_path,
        handler=lambda _request: _comparison_result(
            regressions=((101, -0.3), (102, -0.2), (103, -0.1))
        ),
        additional_specs=(_inspect_spec(inspect_handler),),
    )
    task = _task().model_copy(update={"max_regressions_to_inspect": inspection_limit})
    result = runtime.run(task)

    assert result.state == "completed"
    assert result.outcome == TerminalOutcome.INCONCLUSIVE
    assert result.reason_code == "mixed_metric_or_query_evidence"
    assert attempted == [101, 102, 103][:inspection_limit]
    assert result.tool_calls_used == 1 + inspection_limit
    assert [
        item["query_id"] for item in result.report["inspected_queries"]
    ] == attempted


def test_failed_inspection_moves_to_next_regression_without_retrying(
    tmp_path: Path,
) -> None:
    attempted: list[int] = []

    def inspect_handler(request):
        attempted.append(request.query_id)
        if request.query_id == 101:
            raise AgentToolError("query_not_found")
        return {
            "evidence_ref": f"query:{request.run_id}:{request.query_id}",
            "payload": {
                "query_id": request.query_id,
                "run_id": request.run_id,
            },
        }

    runtime = _synthetic_runtime(
        tmp_path,
        handler=lambda _request: _comparison_result(
            regressions=((101, -0.3), (102, -0.2), (103, -0.1))
        ),
        additional_specs=(_inspect_spec(inspect_handler),),
    )
    task = _task().model_copy(update={"max_regressions_to_inspect": 2})
    result = runtime.run(task)

    assert result.state == "completed"
    assert result.outcome == TerminalOutcome.INCONCLUSIVE
    assert result.reason_code == "regression_diagnosis_incomplete"
    assert attempted == [101, 102]
    assert result.report["failed_tools"] == [
        {"error_code": "query_not_found", "tool_name": "inspect_query"}
    ]
    assert [item["query_id"] for item in result.report["inspected_queries"]] == [102]


def test_tool_failure_becomes_an_inconclusive_grounded_terminal(tmp_path: Path) -> None:
    def handler(_request):
        raise AgentToolError("artifact_store_unavailable", retryable=True)

    result = _synthetic_runtime(tmp_path, handler=handler).run(_task())
    assert result.state == "completed"
    assert result.outcome == TerminalOutcome.INCONCLUSIVE
    assert result.reason_code == "comparison_failed"
    assert result.evidence_refs == []
    assert result.report["failed_tools"] == [
        {"error_code": "artifact_store_unavailable", "tool_name": "compare_runs"}
    ]


def test_unknown_tool_is_a_terminal_policy_violation(tmp_path: Path) -> None:
    class UnknownToolPlanner:
        planner_id = "unknown-tool-planner-v1"

        def decide(self, _view):
            return ToolAction(
                tool_name="shell",
                arguments={"command": "private"},
                reason_code="try_unknown_tool",
            )

    runtime = _synthetic_runtime(
        tmp_path,
        handler=lambda _request: {},
        planner=UnknownToolPlanner(),
    )
    result = runtime.run(_task())
    assert result.state == "failed"
    assert result.reason_code == "policy_violation"
    assert result.tool_calls_used == 1


def test_untrusted_unknown_tool_name_is_not_copied_into_logs(tmp_path: Path) -> None:
    class UnknownToolPlanner:
        planner_id = "unknown-tool-log-planner-v1"

        def decide(self, _view):
            return ToolAction(
                tool_name="sk_live_secret_token_123",
                arguments={},
                reason_code="try_unknown_tool",
            )

    stream = io.StringIO()
    configure_logging(
        default_level="OFF",
        module_levels={"agent_tools": "DEBUG"},
        stream=stream,
    )
    result = _synthetic_runtime(
        tmp_path,
        handler=lambda _request: {},
        planner=UnknownToolPlanner(),
    ).run(_task())

    assert result.state == "failed"
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [event["event"] for event in events] == [
        "agent_tool_started",
        "agent_tool_failed",
    ]
    assert all(event["tool_name"] == "unrecognized" for event in events)
    assert "sk_live_secret_token_123" not in stream.getvalue()


def test_runtime_rejects_ungrounded_acceptance(tmp_path: Path) -> None:
    class UngroundedPlanner:
        planner_id = "ungrounded-planner-v1"

        def decide(self, _view):
            return FinishDecision(
                outcome=TerminalOutcome.ACCEPT,
                evidence_refs=["comparison:comparison-cccccccccccc"],
                reason_code="claim_without_observation",
            )

    runtime = _synthetic_runtime(
        tmp_path,
        handler=lambda _request: {},
        planner=UngroundedPlanner(),
    )
    result = runtime.run(_task())
    assert result.state == "failed"
    assert result.reason_code == "ungrounded_finish_rejected"
    assert result.tool_calls_used == 0


def test_runtime_blocks_a_reversed_comparison_before_handler(
    tmp_path: Path,
) -> None:
    class ReversedComparisonPlanner:
        planner_id = "reversed-comparison-planner-v1"

        def decide(self, _view):
            return ToolAction(
                tool_name="compare_runs",
                arguments={
                    "baseline_run_id": "bm25-bbbbbbbbbbbb",
                    "candidate_run_id": "random-aaaaaaaaaaaa",
                },
                reason_code="reverse_requested_pair",
            )

    called = False

    def handler(_request):
        nonlocal called
        called = True
        return _comparison_result()

    runtime = _synthetic_runtime(
        tmp_path,
        handler=handler,
        planner=ReversedComparisonPlanner(),
    )
    result = runtime.run(_task())

    assert called is False
    assert result.state == "failed"
    assert result.reason_code == "policy_violation"
    assert result.report["failed_tools"] == [
        {
            "error_code": "comparison_outside_task_scope",
            "tool_name": "compare_runs",
        }
    ]
    assert (
        TraceReplayer(TraceStore(tmp_path / "traces")).replay(result.trace_id) == result
    )


def test_runtime_rejects_comparison_evidence_for_another_pair(
    tmp_path: Path,
) -> None:
    class EvidenceBlindPlanner:
        planner_id = "evidence-blind-planner-v1"

        def decide(self, view):
            if not view.observations:
                return ToolAction(
                    tool_name="compare_runs",
                    arguments={
                        "baseline_run_id": view.task.baseline_run_id,
                        "candidate_run_id": view.task.candidate_run_id,
                    },
                    reason_code="compare_requested_runs",
                )
            return FinishDecision(
                outcome=TerminalOutcome.ACCEPT,
                evidence_refs=[view.observations[0].evidence_ref],
                reason_code="accept_wrong_pair",
            )

    def handler(_request):
        result = _comparison_result()
        result["payload"]["baseline_run_id"] = "bm25-bbbbbbbbbbbb"
        result["payload"]["candidate_run_id"] = "random-aaaaaaaaaaaa"
        return result

    result = _synthetic_runtime(
        tmp_path,
        handler=handler,
        planner=EvidenceBlindPlanner(),
    ).run(_task())

    assert result.state == "failed"
    assert result.reason_code == "ungrounded_finish_rejected"


def test_runtime_rejects_accept_when_primary_metric_regressed(
    tmp_path: Path,
) -> None:
    class DirectionBlindPlanner:
        planner_id = "direction-blind-planner-v1"

        def decide(self, view):
            if not view.observations:
                return ToolAction(
                    tool_name="compare_runs",
                    arguments={
                        "baseline_run_id": view.task.baseline_run_id,
                        "candidate_run_id": view.task.candidate_run_id,
                    },
                    reason_code="compare_requested_runs",
                )
            return FinishDecision(
                outcome=TerminalOutcome.ACCEPT,
                evidence_refs=[view.observations[0].evidence_ref],
                reason_code="accept_negative_delta",
            )

    result = _synthetic_runtime(
        tmp_path,
        handler=lambda _request: _comparison_result(primary_delta=-0.1),
        planner=DirectionBlindPlanner(),
    ).run(_task())

    assert result.state == "failed"
    assert result.reason_code == "ungrounded_finish_rejected"


def test_runtime_rejects_accept_when_query_evidence_is_mixed(
    tmp_path: Path,
) -> None:
    class RegressionBlindPlanner:
        planner_id = "regression-blind-planner-v1"

        def decide(self, view):
            if not view.observations:
                return ToolAction(
                    tool_name="compare_runs",
                    arguments={
                        "baseline_run_id": view.task.baseline_run_id,
                        "candidate_run_id": view.task.candidate_run_id,
                    },
                    reason_code="compare_requested_runs",
                )
            return FinishDecision(
                outcome=TerminalOutcome.ACCEPT,
                evidence_refs=[view.observations[0].evidence_ref],
                reason_code="ignore_query_regression",
            )

    result = _synthetic_runtime(
        tmp_path,
        handler=lambda _request: _comparison_result(regressions=((101, -0.2),)),
        planner=RegressionBlindPlanner(),
    ).run(_task())

    assert result.state == "failed"
    assert result.reason_code == "ungrounded_finish_rejected"


def test_runtime_rejects_accept_when_cited_comparisons_conflict(
    tmp_path: Path,
) -> None:
    class ConflictingEvidencePlanner:
        planner_id = "conflicting-evidence-planner-v1"

        def decide(self, view):
            if len(view.observations) < 2:
                return ToolAction(
                    tool_name="compare_runs",
                    arguments={
                        "baseline_run_id": view.task.baseline_run_id,
                        "candidate_run_id": view.task.candidate_run_id,
                    },
                    reason_code="repeat_comparison",
                )
            return FinishDecision(
                outcome=TerminalOutcome.ACCEPT,
                evidence_refs=[
                    item.evidence_ref
                    for item in view.observations
                    if item.evidence_ref is not None
                ],
                reason_code="ignore_conflicting_comparison",
            )

    call_count = 0

    def handler(_request):
        nonlocal call_count
        call_count += 1
        result = _comparison_result(primary_delta=0.1 if call_count == 1 else -0.1)
        if call_count == 2:
            result["evidence_ref"] = "comparison:comparison-dddddddddddd"
            result["payload"]["comparison_id"] = "comparison-dddddddddddd"
        return result

    runtime = _synthetic_runtime(
        tmp_path,
        handler=handler,
        planner=ConflictingEvidencePlanner(),
    )
    result = runtime.run(_task())

    assert result.state == "failed"
    assert result.reason_code == "ungrounded_finish_rejected"
    assert (
        TraceReplayer(TraceStore(tmp_path / "traces")).replay(result.trace_id) == result
    )


def test_invalid_planner_output_becomes_a_replayable_failure(tmp_path: Path) -> None:
    class InvalidOutputPlanner:
        planner_id = "invalid-output-planner-v1"

        def decide(self, _view):
            return None

    result = _synthetic_runtime(
        tmp_path,
        handler=lambda _request: _comparison_result(),
        planner=InvalidOutputPlanner(),
    ).run(_task())

    assert result.state == "failed"
    assert result.reason_code == "planner_invalid_output"
    assert result.steps_used == 0
    assert (
        TraceReplayer(TraceStore(tmp_path / "traces")).replay(result.trace_id) == result
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"opaque": object()},
        {"padding": "x" * (65 * 1024)},
    ],
)
def test_non_json_or_oversized_planner_action_is_a_bounded_failure(
    tmp_path: Path,
    arguments: dict,
) -> None:
    class InvalidActionPlanner:
        planner_id = "invalid-action-planner-v1"

        def decide(self, _view):
            return ToolAction(
                tool_name="compare_runs",
                arguments=arguments,
                reason_code="invalid_action_payload",
            )

    trace_store = TraceStore(tmp_path / "traces")
    runtime = _synthetic_runtime(
        tmp_path,
        handler=lambda _request: _comparison_result(),
        planner=InvalidActionPlanner(),
    )
    result = runtime.run(_task())

    assert result.state == "failed"
    assert result.reason_code == "planner_invalid_output"
    path = trace_store.root / f"trace-{result.trace_id}.json"
    assert path.stat().st_size < 16 * 1024
    assert TraceReplayer(trace_store).replay(result.trace_id) == result


def test_replay_rejects_a_tampered_observation(
    tmp_path: Path, smoke_runs: tuple[dict, dict]
) -> None:
    runtime, task, trace_store = _real_runtime(tmp_path, smoke_runs)
    result = runtime.run(task)
    path = trace_store.root / f"trace-{result.trace_id}.json"
    trace = json.loads(path.read_text(encoding="utf-8"))
    observed = next(
        event for event in trace["events"] if event["event_type"] == "tool_observed"
    )
    observed["observation"]["payload"]["query_count"] = 999
    path.write_text(json.dumps(trace), encoding="utf-8")

    with pytest.raises(ValueError, match="hash"):
        TraceReplayer(trace_store).replay(result.trace_id)


def test_replay_rejects_a_tampered_terminal(
    tmp_path: Path, smoke_runs: tuple[dict, dict]
) -> None:
    runtime, task, trace_store = _real_runtime(tmp_path, smoke_runs)
    result = runtime.run(task)
    path = trace_store.root / f"trace-{result.trace_id}.json"
    trace = json.loads(path.read_text(encoding="utf-8"))
    trace["terminal"]["reason_code"] = "tampered_reason"
    path.write_text(json.dumps(trace), encoding="utf-8")

    with pytest.raises(ValueError, match="terminal hash"):
        TraceReplayer(trace_store).replay(result.trace_id)


def test_replay_rejects_tampered_top_level_context(
    tmp_path: Path, smoke_runs: tuple[dict, dict]
) -> None:
    runtime, task, trace_store = _real_runtime(tmp_path, smoke_runs)
    result = runtime.run(task)
    path = trace_store.root / f"trace-{result.trace_id}.json"
    trace = json.loads(path.read_text(encoding="utf-8"))
    trace["planner_id"] = "tampered-planner-v1"
    path.write_text(json.dumps(trace), encoding="utf-8")

    with pytest.raises(ValueError, match="context hash"):
        TraceReplayer(trace_store).replay(result.trace_id)


def test_replay_cross_checks_completed_decision_against_terminal(
    tmp_path: Path,
) -> None:
    def handler(_request):
        return _comparison_result()

    runtime = _synthetic_runtime(tmp_path, handler=handler)
    result = runtime.run(_task())
    trace_store = TraceStore(tmp_path / "traces")
    path = trace_store.root / f"trace-{result.trace_id}.json"
    trace = json.loads(path.read_text(encoding="utf-8"))
    trace["terminal"]["reason_code"] = "tampered_reason"
    terminal = TerminalResult.model_validate(trace["terminal"])
    final_event = trace["events"][-1]
    final_event["terminal_sha256"] = compute_terminal_hash(terminal)
    final_event["event_hash"] = compute_event_hash(
        {key: value for key, value in final_event.items() if key != "event_hash"}
    )
    path.write_text(json.dumps(trace), encoding="utf-8")

    with pytest.raises(ValueError, match="decision does not match"):
        TraceReplayer(trace_store).replay(result.trace_id)


def test_replay_validates_failed_terminal_semantics(tmp_path: Path) -> None:
    class UnknownToolPlanner:
        planner_id = "unknown-tool-planner-v1"

        def decide(self, _view):
            return ToolAction(
                tool_name="shell",
                arguments={"command": "private"},
                reason_code="try_unknown_tool",
            )

    runtime = _synthetic_runtime(
        tmp_path,
        handler=lambda _request: {},
        planner=UnknownToolPlanner(),
    )
    result = runtime.run(_task())
    trace_store = TraceStore(tmp_path / "traces")
    assert TraceReplayer(trace_store).replay(result.trace_id) == result

    path = trace_store.root / f"trace-{result.trace_id}.json"
    trace = json.loads(path.read_text(encoding="utf-8"))
    trace["terminal"]["outcome"] = "accept"
    terminal = TerminalResult.model_validate(trace["terminal"])
    final_event = trace["events"][-1]
    final_event["terminal_sha256"] = compute_terminal_hash(terminal)
    final_event["event_hash"] = compute_event_hash(
        {key: value for key, value in final_event.items() if key != "event_hash"}
    )
    path.write_text(json.dumps(trace), encoding="utf-8")

    with pytest.raises(ValueError, match="failure terminal"):
        TraceReplayer(trace_store).replay(result.trace_id)


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        ({"blob": "x" * 512}, "observation_too_large"),
        ({"not_json": object()}, "invalid_tool_result"),
    ],
)
def test_invalid_or_oversized_payload_is_not_written_to_trace(
    tmp_path: Path, payload: dict, expected_error: str
) -> None:
    def handler(_request):
        return {
            "evidence_ref": "comparison:comparison-cccccccccccc",
            "payload": payload,
        }

    runtime = _synthetic_runtime(
        tmp_path,
        handler=handler,
        policy=RuntimePolicy(max_observation_bytes=128),
        output_model=_PermissiveToolOutput,
    )
    result = runtime.run(_task())
    trace_store = TraceStore(tmp_path / "traces")
    trace = trace_store.load(result.trace_id)
    observation = next(
        event.observation
        for event in trace.events
        if event.event_type == "tool_observed"
    )

    assert observation is not None
    assert observation["status"] == "failed"
    assert observation["error_code"] == expected_error
    assert observation["evidence_ref"] is None
    assert observation["payload"] == {}
    assert TraceReplayer(trace_store).replay(result.trace_id) == result


def test_trace_store_is_immutable_and_rejects_oversized_artifacts(
    tmp_path: Path, smoke_runs: tuple[dict, dict]
) -> None:
    runtime, task, trace_store = _real_runtime(tmp_path, smoke_runs)
    result = runtime.run(task)
    trace = trace_store.load(result.trace_id)

    assert trace_store.store(trace).is_file()
    collision = trace.model_copy(update={"planner_id": "different-planner-v1"})
    with pytest.raises(RuntimeError, match="immutable artifact collision"):
        trace_store.store(collision)

    oversized_id = "f" * 32
    oversized_terminal = trace.terminal.model_copy(
        update={
            "trace_id": oversized_id,
            "report": {"blob": "x" * MAX_TRACE_BYTES},
        }
    )
    oversized_trace = trace.model_copy(
        update={"trace_id": oversized_id, "terminal": oversized_terminal}
    )
    oversized_path = trace_store.root / f"trace-{oversized_id}.json"
    with pytest.raises(ValueError, match="size limit"):
        trace_store.store(oversized_trace)
    assert not oversized_path.exists()


def test_invalid_replay_id_is_not_copied_into_logs(tmp_path: Path) -> None:
    stream = io.StringIO()
    configure_logging(
        default_level="OFF",
        module_levels={"agent_replay": "ERROR"},
        stream=stream,
    )

    with pytest.raises(ValueError, match="invalid Trace ID"):
        TraceReplayer(TraceStore(tmp_path / "traces")).replay("secret-token-password")

    event = json.loads(stream.getvalue())
    assert event["trace_id"] == "invalid"
    assert "secret-token-password" not in stream.getvalue()


def test_trace_uses_compact_encoding_for_large_valid_observation(
    tmp_path: Path,
) -> None:
    def handler(_request):
        result = _comparison_result()
        result["payload"]["padding"] = [0] * 400_000
        return result

    trace_store = TraceStore(tmp_path / "traces")
    result = _synthetic_runtime(
        tmp_path,
        handler=handler,
        output_model=_PermissiveToolOutput,
    ).run(_task())

    assert result.state == "completed"
    path = trace_store.root / f"trace-{result.trace_id}.json"
    assert path.stat().st_size < MAX_TRACE_BYTES
    assert TraceReplayer(trace_store).replay(result.trace_id) == result


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_steps": 1.5},
        {"max_tool_calls": True},
        {"allowed_capabilities": {"compare_smoke_runs"}},
        {"allowed_capabilities": frozenset({"unsafe capability"})},
    ],
)
def test_runtime_policy_rejects_values_that_cannot_replay(overrides: dict) -> None:
    with pytest.raises(ValueError):
        RuntimePolicy(**overrides)


def test_step_budget_prevents_an_infinite_planner_loop(tmp_path: Path) -> None:
    class LoopPlanner:
        planner_id = "loop-planner-v1"

        def decide(self, _view):
            return ToolAction(
                tool_name="compare_runs",
                arguments={
                    "baseline_run_id": "random-aaaaaaaaaaaa",
                    "candidate_run_id": "bm25-bbbbbbbbbbbb",
                },
                reason_code="repeat_forever",
            )

    def handler(_request):
        return _comparison_result()

    runtime = _synthetic_runtime(
        tmp_path,
        handler=handler,
        planner=LoopPlanner(),
        policy=RuntimePolicy(max_steps=3, max_same_action_attempts=2),
    )
    result = runtime.run(_task())
    assert result.state == "failed"
    assert result.reason_code == "repeated_action_rejected"
    assert result.tool_calls_used == 2


def test_agent_runtime_and_tools_logs_are_independently_filterable_and_private(
    tmp_path: Path,
) -> None:
    stream = io.StringIO()
    configure_logging(
        default_level="OFF",
        module_levels={"agent_runtime": "INFO", "agent_tools": "OFF"},
        stream=stream,
    )

    def handler(_request):
        result = _comparison_result()
        result["payload"]["product_title"] = "private product title"
        result["payload"]["query_text"] = "private Query text"
        return result

    result = _synthetic_runtime(
        tmp_path,
        handler=handler,
        output_model=_PermissiveToolOutput,
    ).run(_task())
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [event["event"] for event in events] == [
        "agent_run_started",
        "agent_run_completed",
    ]
    assert all(event["module"] == "agent_runtime" for event in events)
    assert all(event["trace_id"] == result.trace_id for event in events)
    assert "private product title" not in stream.getvalue()
    assert "private Query text" not in stream.getvalue()

    stream = io.StringIO()
    configure_logging(
        default_level="OFF",
        module_levels={"agent_runtime": "OFF", "agent_tools": "DEBUG"},
        stream=stream,
    )
    _synthetic_runtime(
        tmp_path / "second",
        handler=handler,
        output_model=_PermissiveToolOutput,
    ).run(_task())
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [event["event"] for event in events] == [
        "agent_tool_started",
        "agent_tool_completed",
    ]
    assert all(event["module"] == "agent_tools" for event in events)
    assert "private" not in stream.getvalue()
