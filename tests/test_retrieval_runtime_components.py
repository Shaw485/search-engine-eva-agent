from __future__ import annotations

import copy
import io
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

from search_quality.agent.contracts import (
    AgentState,
    FinishDecision,
    RetrievalOptimizationTask,
    TerminalOutcome,
    ToolObservation,
)
from search_quality.agent.errors import AgentToolError
from search_quality.agent.retrieval_planner import (
    ObservationDrivenRetrievalPlanner,
    expected_retrieval_decision,
    validate_retrieval_plan_semantics,
)
from search_quality.agent.retrieval_runtime import _summarize_action
from search_quality.agent.retrieval_tools import (
    RETRIEVAL_TOOL_CAPABILITIES,
    RUN_CANDIDATE_TOOL,
    BaselineDiagnosisPayload,
    StageRetrievalTools,
)
from search_quality.agent.runtime import AgentRuntime, RuntimePolicy
from search_quality.agent.trace import AgentTrace, TraceStore
from search_quality.observability import configure_logging, logging_context

ROOT = Path(__file__).resolve().parents[1]


def test_action_summary_preserves_a_retry_without_exposing_failed_payload() -> None:
    observation = _failed_observation(
        RUN_CANDIDATE_TOOL,
        retryable=True,
        suffix="f",
    )

    summary = _summarize_action(
        {
            "arguments": {
                "baseline_run_id": "retrieval-aaaaaaaaaaaa",
                "pipeline_variant": "title-exact-multifield-v1",
            },
            "reason_code": "test_uniform_multifield_fusion",
            "tool_name": RUN_CANDIDATE_TOOL,
        },
        observation.model_dump(mode="json"),
        2,
    )

    assert summary == {
        "evidence_ref": None,
        "failed_gates": [],
        "gate_passed": None,
        "pipeline_variant": "title-exact-multifield-v1",
        "reason_code": "test_uniform_multifield_fusion",
        "retryable": True,
        "sequence": 2,
        "status": "failed",
        "tool_name": RUN_CANDIDATE_TOOL,
    }


@dataclass(frozen=True)
class _RealPath:
    artifact_root: Path
    log_text: str
    result: object
    tools: StageRetrievalTools
    trace: AgentTrace


@pytest.fixture(scope="module")
def real_retrieval_path(tmp_path_factory: pytest.TempPathFactory) -> _RealPath:
    root = tmp_path_factory.mktemp("retrieval-runtime")
    artifact_root = root / "artifacts"
    trace_store = TraceStore(root / "traces")
    tools = StageRetrievalTools(
        project_root=ROOT,
        artifact_root=artifact_root,
        revision_provider=lambda _root: "a" * 40,
    )
    stream = io.StringIO()
    configure_logging(
        default_level="OFF",
        module_levels={"agent_tools": "INFO"},
        stream=stream,
    )
    runtime = AgentRuntime(
        planner=ObservationDrivenRetrievalPlanner(),
        tools=tools.build_registry(),
        trace_store=trace_store,
        policy=RuntimePolicy(
            max_steps=8,
            max_tool_calls=6,
            max_run_creations=4,
            max_failures=3,
            max_elapsed_ms=180_000,
            allowed_capabilities=RETRIEVAL_TOOL_CAPABILITIES,
        ),
    )
    with logging_context(trace_id="stage-retrieval-real-path"):
        result = runtime.run(
            RetrievalOptimizationTask(task_id="optimize-retrieval-smoke")
        )
    return _RealPath(
        artifact_root=artifact_root,
        log_text=stream.getvalue(),
        result=result,
        tools=tools,
        trace=trace_store.load(result.trace_id),
    )


def _observations(trace: AgentTrace) -> tuple[ToolObservation, ...]:
    return tuple(
        ToolObservation.model_validate(event.observation)
        for event in trace.events
        if event.event_type == "tool_observed" and event.observation is not None
    )


def _failed_observation(
    tool_name: str,
    *,
    retryable: bool,
    suffix: str,
) -> ToolObservation:
    return ToolObservation(
        tool_name=tool_name,
        status="failed",
        evidence_ref=None,
        payload={},
        error_code="artifact_store_unavailable",
        retryable=retryable,
        sha256=suffix * 64,
    )


def _candidate_observation(
    source: ToolObservation,
    *,
    variant: str,
    suffix: str,
) -> ToolObservation:
    payload = copy.deepcopy(source.payload)
    run_id = f"retrieval-{suffix * 12}"
    diagnosis_id = f"stage-diagnosis-{suffix * 12}"
    comparison_id = f"retrieval-comparison-{suffix * 12}"
    pipeline_id = f"pipeline-{suffix * 12}"
    payload.update(
        {
            "candidate_run_id": run_id,
            "comparison_id": comparison_id,
            "diagnosis_id": diagnosis_id,
            "pipeline_id": pipeline_id,
            "pipeline_variant": variant,
        }
    )
    payload["artifacts"] = {
        "comparison_id": comparison_id,
        "diagnosis_id": diagnosis_id,
        "retrieval_run_id": run_id,
    }
    return source.model_copy(
        update={
            "evidence_ref": f"comparison:{comparison_id}",
            "payload": payload,
            "sha256": suffix * 64,
        }
    )


def test_real_20_query_path_runs_all_bounded_candidates_and_builds_response(
    real_retrieval_path: _RealPath,
) -> None:
    result = real_retrieval_path.result
    assert result.state == "completed"
    assert result.outcome == TerminalOutcome.PROPOSAL_READY
    assert result.reason_code == "conservative_candidate_selected"
    assert result.tool_calls_used == 4
    assert result.steps_used == 5
    assert result.report["diagnosis"]["run_id"].startswith("retrieval-")
    assert [item["pipeline_variant"] for item in result.report["experiments"]] == [
        "title-exact-multifield-v1",
        "title-exact-multifield-weighted-v1",
        "title-exact-multifield-weighted-aggressive-v1",
    ]
    assert [item["gate_passed"] for item in result.report["experiments"]] == [
        False,
        True,
        False,
    ]

    response = real_retrieval_path.tools.build_analysis_response(result)
    assert response["schema_version"] == "retrieval-stage-analysis-response-v1"
    assert response["status"] == "proposal_ready"
    assert response["profile"] == "smoke"
    assert response["diagnosis"]["per_query"]
    assert len(response["experiments"]) == 3
    assert (
        response["candidate_run_id"]
        == result.report["experiments"][1]["candidate_run_id"]
    )
    assert (
        response["comparison_id"] == result.report["decision"]["selected_comparison_id"]
    )
    assert response["comparison"]["gate_result"]["passed"] is True


def test_real_path_persists_only_content_addressed_evidence(
    real_retrieval_path: _RealPath,
) -> None:
    root = real_retrieval_path.artifact_root
    assert len(list((root / "retrieval-runs").glob("retrieval-*.json"))) == 4
    assert len(list((root / "stage-diagnoses").glob("stage-diagnosis-*.json"))) == 4
    assert (
        len(list((root / "retrieval-comparisons").glob("retrieval-comparison-*.json")))
        == 3
    )
    assert not (root / "strategy-proposals").exists()
    assert not (root / "strategy-decisions").exists()
    assert not (root / "search-strategies").exists()
    assert not (root / "active-strategy.json").exists()


def test_tool_observations_and_logs_are_private_summaries(
    real_retrieval_path: _RealPath,
) -> None:
    response = real_retrieval_path.tools.build_analysis_response(
        real_retrieval_path.result
    )
    raw_query = response["comparison"]["per_query"][0]["query_text"]
    raw_product = response["comparison"]["per_query"][0]["baseline_top_results"][0][
        "product_id"
    ]
    observation_text = json.dumps(
        [
            item.model_dump(mode="json")
            for item in _observations(real_retrieval_path.trace)
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    for private_key in (
        '"query_text"',
        '"product_id"',
        '"product_title"',
        '"per_query"',
        '"judgments"',
        '"rankings"',
    ):
        assert private_key not in observation_text
    assert raw_query not in observation_text
    assert raw_product not in observation_text
    assert raw_query not in real_retrieval_path.log_text
    assert raw_product not in real_retrieval_path.log_text
    events = [
        json.loads(line) for line in real_retrieval_path.log_text.splitlines() if line
    ]
    assert events
    assert {event["module"] for event in events} == {"agent_tools"}
    assert all(
        event["trace_id"] == real_retrieval_path.result.trace_id for event in events
    )


def test_registry_exposes_exactly_two_strict_tool_schemas(
    real_retrieval_path: _RealPath,
) -> None:
    registry = real_retrieval_path.tools.build_registry()
    assert registry.names == {
        "diagnose_baseline_retrieval",
        "run_retrieval_candidate",
    }
    with pytest.raises(AgentToolError, match="invalid_argument"):
        registry.execute(
            "diagnose_baseline_retrieval",
            {"profile": "smoke", "query_text": "must-not-enter-tool"},
            allowed_capabilities=RETRIEVAL_TOOL_CAPABILITIES,
        )
    baseline_run_id = real_retrieval_path.result.report["diagnosis"]["run_id"]
    with pytest.raises(AgentToolError, match="invalid_argument"):
        registry.execute(
            "run_retrieval_candidate",
            {
                "baseline_run_id": baseline_run_id,
                "pipeline_variant": "semantic-model-unbounded-v1",
            },
            allowed_capabilities=RETRIEVAL_TOOL_CAPABILITIES,
        )
    baseline_payload = next(
        item.payload
        for item in _observations(real_retrieval_path.trace)
        if item.tool_name == "diagnose_baseline_retrieval"
    )
    with pytest.raises(ValidationError, match="Extra inputs"):
        BaselineDiagnosisPayload.model_validate(
            {**baseline_payload, "query_text": "must-not-enter-output"}
        )


def test_pure_semantics_accept_real_path_and_reject_reorder_skip_and_tampering(
    real_retrieval_path: _RealPath,
) -> None:
    task = RetrievalOptimizationTask(task_id="semantic-validation")
    observations = _observations(real_retrieval_path.trace)
    terminal = FinishDecision(
        outcome=real_retrieval_path.result.outcome,
        evidence_refs=real_retrieval_path.result.evidence_refs,
        reason_code=real_retrieval_path.result.reason_code,
    )
    validate_retrieval_plan_semantics(task, observations, terminal)

    with pytest.raises(ValueError, match="reordered or skipped"):
        validate_retrieval_plan_semantics(
            task,
            (observations[0], observations[2], observations[1], observations[3]),
            terminal,
        )
    with pytest.raises(ValueError, match="reordered or skipped"):
        expected_retrieval_decision(task, (observations[0], observations[2]))

    tampered_payload = copy.deepcopy(observations[1].payload)
    tampered_payload["gate"]["checks"][0]["passed"] = not tampered_payload["gate"][
        "checks"
    ][0]["passed"]
    tampered = observations[1].model_copy(update={"payload": tampered_payload})
    with pytest.raises(ValidationError, match="gate result"):
        expected_retrieval_decision(
            task,
            (observations[0], tampered),
        )

    wrong_selection = terminal.model_copy(
        update={"reason_code": "aggressive_candidate_selected"}
    )
    with pytest.raises(ValueError, match="does not match observations"):
        validate_retrieval_plan_semantics(task, observations, wrong_selection)


def test_planner_stops_on_uniform_pass_and_retries_only_once(
    real_retrieval_path: _RealPath,
) -> None:
    task = RetrievalOptimizationTask(task_id="branch-validation")
    observations = _observations(real_retrieval_path.trace)
    baseline = observations[0]
    conservative = copy.deepcopy(observations[2].payload)
    conservative["pipeline_variant"] = "title-exact-multifield-v1"
    uniform_pass = observations[2].model_copy(update={"payload": conservative})
    decision = expected_retrieval_decision(task, (baseline, uniform_pass))
    assert isinstance(decision, FinishDecision)
    assert decision.outcome == TerminalOutcome.PROPOSAL_READY
    assert decision.reason_code == "uniform_candidate_passed"

    first_failure = _failed_observation(
        "diagnose_baseline_retrieval",
        retryable=True,
        suffix="a",
    )
    retry = expected_retrieval_decision(task, (first_failure,))
    assert retry.kind == "tool"
    assert retry.tool_name == "diagnose_baseline_retrieval"
    second_failure = _failed_observation(
        "diagnose_baseline_retrieval",
        retryable=True,
        suffix="b",
    )
    exhausted = expected_retrieval_decision(task, (first_failure, second_failure))
    assert isinstance(exhausted, FinishDecision)
    assert exhausted.outcome == TerminalOutcome.INCONCLUSIVE
    assert exhausted.reason_code == "retrieval_tool_retry_exhausted"

    later_failure = _failed_observation(
        RUN_CANDIDATE_TOOL,
        retryable=True,
        suffix="c",
    )
    global_retry_exhausted = expected_retrieval_decision(
        task,
        (first_failure, baseline, later_failure),
    )
    assert isinstance(global_retry_exhausted, FinishDecision)
    assert global_retry_exhausted.outcome == TerminalOutcome.INCONCLUSIVE
    assert global_retry_exhausted.reason_code == "retrieval_tool_retry_exhausted"


def test_planner_finishes_no_safe_improvement_or_selects_aggressive_from_gates(
    real_retrieval_path: _RealPath,
) -> None:
    task = RetrievalOptimizationTask(task_id="terminal-branches")
    observations = _observations(real_retrieval_path.trace)
    baseline, uniform_failed, conservative_passed, aggressive_failed = observations
    conservative_failed = _candidate_observation(
        uniform_failed,
        variant="title-exact-multifield-weighted-v1",
        suffix="b",
    )
    no_safe = expected_retrieval_decision(
        task,
        (baseline, uniform_failed, conservative_failed, aggressive_failed),
    )
    assert isinstance(no_safe, FinishDecision)
    assert no_safe.outcome == TerminalOutcome.NO_SAFE_IMPROVEMENT
    assert no_safe.reason_code == "no_safe_candidate"

    aggressive_passed = _candidate_observation(
        conservative_passed,
        variant="title-exact-multifield-weighted-aggressive-v1",
        suffix="d",
    )
    aggressive_selected = expected_retrieval_decision(
        task,
        (baseline, uniform_failed, conservative_failed, aggressive_passed),
    )
    assert isinstance(aggressive_selected, FinishDecision)
    assert aggressive_selected.outcome == TerminalOutcome.PROPOSAL_READY
    assert aggressive_selected.reason_code == "aggressive_candidate_selected"


def test_planner_view_is_observation_driven(real_retrieval_path: _RealPath) -> None:
    from search_quality.agent.planner import PlannerView

    task = RetrievalOptimizationTask(task_id="planner-view")
    planner = ObservationDrivenRetrievalPlanner()
    initial = planner.decide(
        PlannerView(
            task=task,
            state=AgentState.PLANNING,
            observations=(),
            steps_used=0,
            tool_calls_used=0,
        )
    )
    assert initial.tool_name == "diagnose_baseline_retrieval"
    after_baseline = planner.decide(
        PlannerView(
            task=task,
            state=AgentState.DECIDING,
            observations=(_observations(real_retrieval_path.trace)[0],),
            steps_used=1,
            tool_calls_used=1,
        )
    )
    assert after_baseline.arguments["pipeline_variant"] == ("title-exact-multifield-v1")
