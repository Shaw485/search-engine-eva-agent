from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from search_quality.agent.contracts import (
    AgentState,
    RetrievalOptimizationTask,
    TerminalOutcome,
)
from search_quality.agent.llm_provider import (
    LLMDecisionRequest,
    LLMDecisionResult,
    LLMTokenUsage,
)
from search_quality.agent.llm_retrieval_planner import (
    LLMPlannerBudgetError,
    LLMPlannerConfigurationError,
    LLMRetrievalPlanner,
    build_retrieval_planner,
    load_retrieval_planner_configuration,
)
from search_quality.agent.planner import PlannerView
from search_quality.agent.registry import AgentToolRegistry, ToolSpec
from search_quality.agent.replay import TraceReplayer
from search_quality.agent.retrieval_runtime import _agent_run_summary
from search_quality.agent.retrieval_tools import (
    DIAGNOSE_BASELINE_TOOL,
    DIAGNOSE_RETRIEVAL_CAPABILITY,
    EXPERIMENT_RETRIEVAL_CAPABILITY,
    GATE_POLICY,
    RETRIEVAL_TOOL_CAPABILITIES,
    RUN_CANDIDATE_TOOL,
    ArtifactRefs,
    BaselineAggregateSummary,
    BaselineDiagnosisOutput,
    BaselineDiagnosisPayload,
    CandidateAggregateDeltaSummary,
    CandidateExperimentInput,
    CandidateExperimentOutput,
    CandidateExperimentPayload,
    CandidateGateSummary,
    CandidateRiskSummary,
    DiagnoseBaselineInput,
    FirstLossCounts,
    GateCheckSummary,
    StageDeltaSummary,
    StageMetricSummary,
)
from search_quality.agent.runtime import AgentRuntime, RuntimePolicy
from search_quality.agent.trace import ZERO_HASH, TraceStore, compute_event_hash


@dataclass
class _ScriptedProvider:
    decisions: list[str]
    requests: list[LLMDecisionRequest] = field(default_factory=list)

    def decide(self, request: LLMDecisionRequest) -> LLMDecisionResult:
        self.requests.append(request)
        option_id = self.decisions.pop(0)
        assert option_id in request.allowed_option_ids
        sequence = len(self.requests)
        return LLMDecisionResult(
            option_id=option_id,
            provider=request.provider,
            model=request.model,
            token_usage=LLMTokenUsage(
                input_tokens=40 + sequence,
                output_tokens=5,
                total_tokens=45 + sequence,
            ),
            response_id_sha256=f"{sequence:x}" * 64,
            duration_ms=float(sequence),
        )


def _baseline_output() -> BaselineDiagnosisOutput:
    payload = BaselineDiagnosisPayload(
        profile="smoke",
        query_count=20,
        judged_pair_count=416,
        run_id="retrieval-aaaaaaaaaaaa",
        diagnosis_id="stage-diagnosis-aaaaaaaaaaaa",
        pipeline_id="pipeline-aaaaaaaaaaaa",
        pipeline_variant="title-exact-v1",
        diagnosis_status="diagnosable",
        primary_category="recall",
        recommended_next_action="run_recall_experiment",
        findings=[],
        aggregate=BaselineAggregateSummary(
            recall_union_coverage=0.5,
            fusion=StageMetricSummary(
                judged_recall_at_10=0.4,
                mrr_at_10=0.3,
                ndcg_at_10=0.35,
            ),
            coarse_rank=StageMetricSummary(
                judged_recall_at_10=0.3,
                mrr_at_10=0.25,
                ndcg_at_10=0.28,
            ),
            first_loss_counts=FirstLossCounts(
                recall=5,
                fusion=4,
                coarse_rank=3,
                retained=8,
            ),
        ),
        artifacts=ArtifactRefs(
            retrieval_run_id="retrieval-aaaaaaaaaaaa",
            diagnosis_id="stage-diagnosis-aaaaaaaaaaaa",
        ),
    )
    return BaselineDiagnosisOutput(
        evidence_ref="run:retrieval-aaaaaaaaaaaa",
        payload=payload,
    )


def _candidate_output(variant: str, *, passed: bool) -> CandidateExperimentOutput:
    suffix_by_variant = {
        "title-exact-multifield-v1": "b",
        "title-exact-multifield-weighted-v1": "c",
        "title-exact-multifield-weighted-aggressive-v1": "d",
    }
    suffix = suffix_by_variant[variant]
    values = {
        "unique_relevant_contribution": 1.0 if passed else 0.0,
        "union_coverage_improvement": 0.1 if passed else 0.0,
        "fusion_recall_at_10_floor": 0.02,
        "fusion_ndcg_at_10_floor": 0.01,
        "fusion_mrr_at_10_floor": 0.01,
        "coarse_recall_at_10_floor": 0.02,
        "coarse_ndcg_at_10_floor": 0.01,
        "coarse_mrr_at_10_floor": 0.01,
        "worst_query_coarse_ndcg_delta_floor": -0.01,
        "regressed_query_rate_ceiling": 0.05,
        "worst_query_fusion_ndcg_delta_floor": -0.01,
        "fusion_regressed_query_rate_ceiling": 0.05,
    }
    checks = [
        GateCheckSummary(
            name=name,
            comparator=comparator,
            threshold=threshold,
            observed=values[name],
            passed=(
                values[name] > threshold
                if comparator == ">"
                else values[name] >= threshold
                if comparator == ">="
                else values[name] <= threshold
            ),
        )
        for name, comparator, threshold in GATE_POLICY
    ]
    failed_gates = [item.name for item in checks if not item.passed]
    candidate_run_id = f"retrieval-{suffix * 12}"
    diagnosis_id = f"stage-diagnosis-{suffix * 12}"
    comparison_id = f"retrieval-comparison-{suffix * 12}"
    payload = CandidateExperimentPayload(
        profile="smoke",
        baseline_run_id="retrieval-aaaaaaaaaaaa",
        candidate_run_id=candidate_run_id,
        diagnosis_id=diagnosis_id,
        comparison_id=comparison_id,
        pipeline_id=f"pipeline-{suffix * 12}",
        pipeline_variant=variant,
        diagnosis_status="diagnosable",
        aggregate_deltas=CandidateAggregateDeltaSummary(
            recall_union_coverage=values["union_coverage_improvement"],
            fusion=StageDeltaSummary(
                judged_recall_at_10=values["fusion_recall_at_10_floor"],
                mrr_at_10=values["fusion_mrr_at_10_floor"],
                ndcg_at_10=values["fusion_ndcg_at_10_floor"],
            ),
            coarse_rank=StageDeltaSummary(
                judged_recall_at_10=values["coarse_recall_at_10_floor"],
                mrr_at_10=values["coarse_mrr_at_10_floor"],
                ndcg_at_10=values["coarse_ndcg_at_10_floor"],
            ),
        ),
        risk=CandidateRiskSummary(
            unique_relevant_contribution=int(values["unique_relevant_contribution"]),
            worst_coarse_query_ndcg_at_10_delta=values[
                "worst_query_coarse_ndcg_delta_floor"
            ],
            coarse_regressed_query_rate=values["regressed_query_rate_ceiling"],
            worst_fusion_query_ndcg_at_10_delta=values[
                "worst_query_fusion_ndcg_delta_floor"
            ],
            fusion_regressed_query_rate=values["fusion_regressed_query_rate_ceiling"],
        ),
        gate=CandidateGateSummary(
            passed=not failed_gates,
            checks=checks,
            failed_gates=failed_gates,
        ),
        recommendation="review_candidate" if passed else "reject_candidate",
        next_action=("request_owner_review" if passed else "replace_recall_candidate"),
        artifacts=ArtifactRefs(
            retrieval_run_id=candidate_run_id,
            diagnosis_id=diagnosis_id,
            comparison_id=comparison_id,
        ),
    )
    return CandidateExperimentOutput(
        evidence_ref=f"comparison:{comparison_id}",
        payload=payload,
    )


def _registry(executed_variants: list[str]) -> AgentToolRegistry:
    baseline = _baseline_output()

    def diagnose(_request: DiagnoseBaselineInput) -> BaselineDiagnosisOutput:
        return baseline

    def run_candidate(request: CandidateExperimentInput) -> CandidateExperimentOutput:
        executed_variants.append(request.pipeline_variant)
        return _candidate_output(
            request.pipeline_variant,
            passed=request.pipeline_variant == "title-exact-multifield-weighted-v1",
        )

    return AgentToolRegistry(
        (
            ToolSpec(
                name=DIAGNOSE_BASELINE_TOOL,
                capability=DIAGNOSE_RETRIEVAL_CAPABILITY,
                input_model=DiagnoseBaselineInput,
                output_model=BaselineDiagnosisOutput,
                handler=diagnose,
            ),
            ToolSpec(
                name=RUN_CANDIDATE_TOOL,
                capability=EXPERIMENT_RETRIEVAL_CAPABILITY,
                input_model=CandidateExperimentInput,
                output_model=CandidateExperimentOutput,
                handler=run_candidate,
            ),
        )
    )


@pytest.mark.parametrize("provider_id", ["openai", "volcengine_agent_plan"])
def test_llm_loop_selects_an_experiment_then_stops_on_harness_evidence(
    tmp_path: Path,
    provider_id: str,
) -> None:
    provider = _ScriptedProvider(
        [
            "diagnose_baseline",
            "run_conservative_candidate",
            "finish_best_passing_candidate",
        ]
    )
    planner = LLMRetrievalPlanner(
        model_id="gpt-test",
        provider=provider,
        provider_id=provider_id,
    )
    trace_store = TraceStore(tmp_path / "traces")
    executed_variants: list[str] = []
    runtime = AgentRuntime(
        planner=planner,
        tools=_registry(executed_variants),
        trace_store=trace_store,
        policy=RuntimePolicy(
            max_steps=6,
            max_tool_calls=4,
            max_run_creations=4,
            max_failures=1,
            max_same_action_attempts=1,
            max_elapsed_ms=120_000,
            allowed_capabilities=RETRIEVAL_TOOL_CAPABILITIES,
        ),
    )
    result = runtime.run(
        RetrievalOptimizationTask(
            task_id="llm-retrieval-test",
            decision_policy="adaptive_llm_v1",
        )
    )

    assert result.state == "completed"
    assert result.outcome == TerminalOutcome.PROPOSAL_READY
    assert result.reason_code == "llm_conservative_candidate_selected"
    assert result.steps_used == 3
    assert result.tool_calls_used == 2
    assert executed_variants == ["title-exact-multifield-weighted-v1"]
    assert len(provider.requests) == 3
    assert provider.requests[1].allowed_option_ids == [
        "run_uniform_candidate",
        "run_conservative_candidate",
        "run_aggressive_candidate",
    ]
    assert provider.requests[2].allowed_option_ids == [
        "run_uniform_candidate",
        "run_aggressive_candidate",
        "finish_best_passing_candidate",
    ]
    request_text = provider.requests[2].model_dump_json()
    for private_value in (
        "retrieval-aaaaaaaaaaaa",
        "stage-diagnosis-aaaaaaaaaaaa",
        "wireless mouse",
        "product_title",
        "evidence_ref",
    ):
        assert private_value not in request_text

    trace = TraceReplayer(trace_store).replay_trace(result.trace_id)
    assert trace.terminal == result
    summary = _agent_run_summary(trace)
    assert summary["planner_mode"] == "llm"
    assert summary["llm_usage"]["model_calls"] == 3
    assert summary["llm_usage"]["provider_id"] == provider_id
    assert summary["llm_usage"]["terminal_option_id"] == (
        "finish_best_passing_candidate"
    )
    assert [item["selected_option_id"] for item in summary["actions"]] == [
        "diagnose_baseline",
        "run_conservative_candidate",
    ]


def test_standalone_replay_rejects_mixed_llm_provenance_and_option_count(
    tmp_path: Path,
) -> None:
    provider = _ScriptedProvider(
        [
            "diagnose_baseline",
            "run_conservative_candidate",
            "finish_best_passing_candidate",
        ]
    )
    trace_store = TraceStore(tmp_path / "traces")
    runtime = AgentRuntime(
        planner=LLMRetrievalPlanner(model_id="gpt-test", provider=provider),
        tools=_registry([]),
        trace_store=trace_store,
        policy=RuntimePolicy(
            max_steps=6,
            max_tool_calls=4,
            max_run_creations=4,
            max_failures=1,
            max_same_action_attempts=1,
            max_elapsed_ms=120_000,
            allowed_capabilities=RETRIEVAL_TOOL_CAPABILITIES,
        ),
    )
    result = runtime.run(
        RetrievalOptimizationTask(
            task_id="llm-replay-binding-test",
            decision_policy="adaptive_llm_v1",
        )
    )
    path = trace_store.root / f"trace-{result.trace_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    audited_events = [
        event for event in payload["events"] if event["planner_audit"] is not None
    ]
    assert len(audited_events) == 3
    audited_events[1]["planner_audit"]["model_id"] = "different-model"
    audited_events[1]["planner_audit"]["planner_config_sha256"] = "b" * 64

    previous_hash = ZERO_HASH
    for event in payload["events"]:
        event["previous_hash"] = previous_hash
        event_payload = {
            key: value for key, value in event.items() if key != "event_hash"
        }
        event["event_hash"] = compute_event_hash(event_payload)
        previous_hash = event["event_hash"]
    path.chmod(0o600)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="Planner configuration changes between decisions",
    ):
        TraceReplayer(trace_store).replay_trace(result.trace_id)

    audited_events[1]["planner_audit"]["model_id"] = "gpt-test"
    audited_events[1]["planner_audit"]["planner_config_sha256"] = audited_events[0][
        "planner_audit"
    ]["planner_config_sha256"]
    audited_events[1]["planner_audit"]["option_count"] += 1
    previous_hash = ZERO_HASH
    for event in payload["events"]:
        event["previous_hash"] = previous_hash
        event_payload = {
            key: value for key, value in event.items() if key != "event_hash"
        }
        event["event_hash"] = compute_event_hash(event_payload)
        previous_hash = event["event_hash"]
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="option count does not replay"):
        TraceReplayer(trace_store).replay_trace(result.trace_id)


def test_planner_enforces_total_token_budget_before_returning_an_action() -> None:
    provider = _ScriptedProvider(["diagnose_baseline"])
    planner = LLMRetrievalPlanner(
        model_id="gpt-test",
        provider=provider,
        max_total_input_tokens=1,
    )
    view = PlannerView(
        task=RetrievalOptimizationTask(
            task_id="llm-budget-test",
            decision_policy="adaptive_llm_v1",
        ),
        state=AgentState.PLANNING,
        observations=(),
        steps_used=0,
        tool_calls_used=0,
        remaining_ms=50_000,
    )

    with pytest.raises(LLMPlannerBudgetError, match="llm_token_budget_exhausted"):
        planner.decide(view)
    assert planner.take_last_audit() is None


def test_llm_configuration_is_explicit_and_never_falls_back() -> None:
    deterministic = load_retrieval_planner_configuration({})
    assert deterministic.state == "deterministic"
    assert build_retrieval_planner(deterministic).planner_id == (
        "stage-aware-retrieval-planner-v1"
    )

    missing_key = load_retrieval_planner_configuration(
        {
            "SEARCH_AGENT_PLANNER": "openai",
            "SEARCH_LLM_MODEL": "gpt-5.2",
        }
    )
    assert missing_key.state == "not_configured"
    assert missing_key.model_id == "gpt-5.2"
    with pytest.raises(
        LLMPlannerConfigurationError,
        match="llm_planner_not_configured",
    ):
        build_retrieval_planner(missing_key)

    ready = load_retrieval_planner_configuration(
        {
            "SEARCH_AGENT_PLANNER": "openai",
            "SEARCH_LLM_API_KEY": "secret-sentinel",
            "SEARCH_LLM_MODEL": "gpt-5.2",
        }
    )
    assert ready.state == "ready"
    assert ready.key_configured is True
    assert ready.provider_timeout_ms == 30_000
    assert ready.worker_timeout_ms == 40_000
    assert ready.max_output_tokens == 128
    assert "secret-sentinel" not in repr(ready)

    explicit_openai_ready = load_retrieval_planner_configuration(
        {
            "SEARCH_AGENT_PLANNER": "llm",
            "SEARCH_LLM_PROVIDER": "openai",
            "SEARCH_OPENAI_API_KEY": "secret-sentinel",
            "SEARCH_LLM_MODEL": "gpt-5.2",
        }
    )
    assert explicit_openai_ready.state == "ready"
    assert explicit_openai_ready.provider_id == "openai"

    volcengine_ready = load_retrieval_planner_configuration(
        {
            "SEARCH_AGENT_PLANNER": "llm",
            "SEARCH_LLM_PROVIDER": "volcengine_agent_plan",
            "SEARCH_VOLCENGINE_AGENT_PLAN_API_KEY": "secret-sentinel",
            "SEARCH_LLM_MODEL": "ep-agent-plan-test",
        }
    )
    assert volcengine_ready.state == "ready"
    assert volcengine_ready.provider_id == "volcengine_agent_plan"
    assert volcengine_ready.model_id == "ep-agent-plan-test"
    volcengine_planner = build_retrieval_planner(volcengine_ready)
    assert volcengine_planner.provider_id == "volcengine_agent_plan"
    assert type(volcengine_planner.provider).__name__ == (
        "VolcengineAgentPlanDecisionProvider"
    )
    assert "secret-sentinel" not in repr(volcengine_ready)

    volcengine_with_only_openai_key = load_retrieval_planner_configuration(
        {
            "SEARCH_AGENT_PLANNER": "llm",
            "SEARCH_LLM_PROVIDER": "volcengine_agent_plan",
            "SEARCH_LLM_API_KEY": "wrong-provider-secret",
            "SEARCH_LLM_MODEL": "ep-agent-plan-test",
        }
    )
    assert volcengine_with_only_openai_key.state == "not_configured"
    assert volcengine_with_only_openai_key.key_configured is False

    missing_explicit_provider = {
        "SEARCH_AGENT_PLANNER": "llm",
        "SEARCH_LLM_MODEL": "ep-agent-plan-test",
    }
    with pytest.raises(
        LLMPlannerConfigurationError,
        match="unsupported_llm_provider",
    ):
        load_retrieval_planner_configuration(missing_explicit_provider)

    with pytest.raises(
        LLMPlannerConfigurationError,
        match="conflicting_llm_provider",
    ):
        load_retrieval_planner_configuration(
            {
                "SEARCH_AGENT_PLANNER": "openai",
                "SEARCH_LLM_PROVIDER": "volcengine_agent_plan",
            }
        )

    configured_timeout = load_retrieval_planner_configuration(
        {
            "SEARCH_AGENT_PLANNER": "openai",
            "SEARCH_LLM_API_KEY": "secret-sentinel",
            "SEARCH_LLM_MODEL": "gpt-5.2",
            "SEARCH_LLM_TIMEOUT_MS": "60000",
        }
    )
    assert configured_timeout.provider_timeout_ms == 60_000
    assert configured_timeout.worker_timeout_ms == 70_000

    with pytest.raises(LLMPlannerConfigurationError, match="invalid_llm_timeout"):
        load_retrieval_planner_configuration(
            {
                "SEARCH_AGENT_PLANNER": "openai",
                "SEARCH_LLM_TIMEOUT_MS": "not-an-int",
            }
        )
