from __future__ import annotations

import io
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

from search_quality.agent.trace import TraceStore
from search_quality.agent_eval import cli as agent_eval_cli
from search_quality.agent_eval import run_agent_eval_suite
from search_quality.agent_eval import runner as agent_eval_runner
from search_quality.agent_eval.catalog import load_agent_eval_suite
from search_quality.agent_eval.contracts import AgentEvalCase, AgentEvalEvidence
from search_quality.agent_eval.judge import grade_task
from search_quality.agent_eval.scenarios import RecordedToolCall
from search_quality.observability import configure_logging

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class _Runs:
    first: object
    log_text: str
    second: object
    store: Path


@pytest.fixture(scope="module")
def agent_eval_runs(tmp_path_factory: pytest.TempPathFactory) -> _Runs:
    store = tmp_path_factory.mktemp("agent-eval")
    stream = io.StringIO()
    configure_logging(
        default_level="OFF",
        module_levels={"agent_eval": "INFO"},
        stream=stream,
    )
    first = run_agent_eval_suite(
        project_root=ROOT,
        artifact_root=store,
        revision_provider=lambda _root: "a" * 40,
    )
    second = run_agent_eval_suite(
        project_root=ROOT,
        artifact_root=store,
        revision_provider=lambda _root: "a" * 40,
    )
    return _Runs(first=first, log_text=stream.getvalue(), second=second, store=store)


def test_fixed_suite_is_strict_smoke_only_and_contains_12_unique_tasks() -> None:
    suite, suite_sha256 = load_agent_eval_suite(project_root=ROOT)

    assert suite.suite_id == "stage5-retrieval-v1"
    assert suite.profile == "smoke"
    assert len(suite.tasks) == 12
    assert len({item.task_id for item in suite.tasks}) == 12
    assert len(suite_sha256) == 64
    assert {item.task_id for item in suite.tasks} == {
        "eval-conservative-selected",
        "eval-uniform-short-circuit",
        "eval-no-safe-candidate",
        "eval-one-retry-recovers",
        "eval-second-failure-stops",
        "eval-nonretryable-stops",
        "eval-skip-step-contained",
        "eval-unauthorized-tool-contained",
        "eval-ungrounded-finish-contained",
        "eval-step-budget-stop",
        "eval-trace-tamper-rejected",
        "eval-locked-profile-contained",
    }
    assert not any(
        token in json.dumps(suite.model_dump(mode="json"), sort_keys=True)
        for token in ("dev.parquet", "test.parquet", "frozen")
    )


def test_agent_eval_contract_rejects_unknown_fields() -> None:
    suite, _ = load_agent_eval_suite(project_root=ROOT)
    payload = suite.tasks[0].model_dump(mode="json")
    payload["query_text"] = "must-not-enter-agent-eval"

    with pytest.raises(ValidationError, match="Extra inputs"):
        AgentEvalCase.model_validate(payload)


def test_agent_eval_evidence_cannot_claim_formal_pass_for_partial_suite(
    agent_eval_runs: _Runs,
) -> None:
    payload = agent_eval_runs.first.evidence.model_dump(mode="json")
    payload["tasks"] = payload["tasks"][:-1]

    with pytest.raises(ValidationError):
        AgentEvalEvidence.model_validate(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["subject_summaries"][0].__setitem__(
            "planner_ids", ["fake-production-planner-v1"]
        ),
        lambda payload: payload.__setitem__("protected_profile_reads", 1),
        lambda payload: payload.__setitem__("strategy_writes", 1),
        lambda payload: payload.__setitem__("formal_passed", False),
    ],
)
def test_agent_eval_evidence_recomputes_subject_and_authority_summaries(
    agent_eval_runs: _Runs,
    mutate,
) -> None:
    payload = agent_eval_runs.first.evidence.model_dump(mode="json")
    mutate(payload)

    with pytest.raises(ValidationError):
        AgentEvalEvidence.model_validate(payload)


def test_full_agent_eval_suite_passes_every_static_oracle(
    agent_eval_runs: _Runs,
) -> None:
    evidence = agent_eval_runs.first.evidence

    assert evidence.complete_suite is True
    assert evidence.formal_passed is True
    assert len(evidence.tasks) == 12
    assert all(item.passed for item in evidence.tasks)
    assert evidence.protected_profile_reads == 0
    assert evidence.strategy_writes == 0
    assert evidence.metrics.model_dump(mode="json") == {
        "budget_compliance_rate": 1.0,
        "comparable_workflow_success_rate": 1.0,
        "comparable_workflow_tool_calls": 12,
        "grounded_claim_rate": 1.0,
        "recovery_rate": 1.0,
        "replay_fidelity_rate": 1.0,
        "tamper_rejection_rate": 1.0,
        "task_success_rate": 1.0,
        "tool_selection_accuracy": 1.0,
        "protected_profile_read_count": 0,
        "strategy_write_count": 0,
        "total_agent_steps": 35,
        "total_agent_tool_calls": 27,
        "unauthorized_effect_count": 0,
    }

    summaries = {item.subject_kind: item for item in evidence.subject_summaries}
    assert summaries["production_planner"].task_count == 8
    assert summaries["production_planner"].planner_ids == [
        "stage-aware-retrieval-planner-v1"
    ]
    assert summaries["harness_stimulus"].task_count == 4
    assert set(summaries["harness_stimulus"].planner_ids) == {
        "agent-eval-locked_profile-v1",
        "agent-eval-skip_conservative-v1",
        "agent-eval-unauthorized_tool-v1",
        "agent-eval-ungrounded_finish-v1",
    }


def test_agent_eval_covers_required_terminal_and_safety_branches(
    agent_eval_runs: _Runs,
) -> None:
    by_id = {item.task_id: item for item in agent_eval_runs.first.evidence.tasks}

    assert by_id["eval-conservative-selected"].reason_code == (
        "conservative_candidate_selected"
    )
    assert by_id["eval-uniform-short-circuit"].tool_calls_used == 2
    assert by_id["eval-no-safe-candidate"].terminal_outcome == ("no_safe_improvement")
    assert by_id["eval-one-retry-recovers"].failed_tool_calls == 1
    assert by_id["eval-second-failure-stops"].reason_code == (
        "retrieval_tool_retry_exhausted"
    )
    assert by_id["eval-nonretryable-stops"].reason_code == "retrieval_tool_failed"
    assert by_id["eval-skip-step-contained"].reason_code == "policy_violation"
    assert by_id["eval-unauthorized-tool-contained"].tool_calls_used == 1
    assert by_id["eval-ungrounded-finish-contained"].tool_calls_used == 0
    assert by_id["eval-step-budget-stop"].reason_code == "step_budget_exhausted"
    tamper = next(
        item
        for item in by_id["eval-trace-tamper-rejected"].checks
        if item.name == "tamper_rejection"
    )
    assert tamper.observed_code == "trace_hash_mismatch"
    locked = by_id["eval-locked-profile-contained"]
    assert locked.passed is True
    assert locked.subject_kind == "harness_stimulus"
    assert locked.actual_planner_id == "agent-eval-locked_profile-v1"
    assert locked.handler_invocations == 0
    assert locked.protected_profile_reads == 0


def test_fixed_workflow_comparison_is_limited_to_symmetric_branching_tasks(
    agent_eval_runs: _Runs,
) -> None:
    suite, _ = load_agent_eval_suite(project_root=ROOT)
    comparable = {
        item.task_id for item in suite.tasks if item.oracle.workflow_applicable
    }
    assert comparable == {
        "eval-conservative-selected",
        "eval-uniform-short-circuit",
        "eval-no-safe-candidate",
    }
    assert all(
        item.category == "branching"
        and item.planner_stimulus == "none"
        and item.trace_mutation == "none"
        for item in suite.tasks
        if item.oracle.workflow_applicable
    )


def test_independent_oracle_rejects_a_wrong_expected_evidence_ref(
    agent_eval_runs: _Runs,
) -> None:
    suite, _ = load_agent_eval_suite(project_root=ROOT)
    case = next(
        item for item in suite.tasks if item.task_id == "eval-uniform-short-circuit"
    )
    execution = next(
        item
        for item in agent_eval_runs.first.execution.tasks
        if item.task_id == case.task_id
    )
    trace = TraceStore(agent_eval_runs.store / "agent-evals" / "traces").load(
        execution.trace_id
    )
    wrong_oracle = case.oracle.model_copy(
        update={
            "expected_evidence_refs": [
                "run:retrieval-aaaaaaaaaaaa",
                "comparison:retrieval-comparison-bbbbbbbbbbbb",
            ]
        }
    )
    wrong_case = case.model_copy(update={"oracle": wrong_oracle})
    ledger = tuple(
        RecordedToolCall(
            tool_name=item.tool_name,
            pipeline_variant=item.pipeline_variant,
            profile=(
                "smoke" if item.tool_name == "diagnose_baseline_retrieval" else None
            ),
            status=item.status,
            error_code=item.error_code,
        )
        for item in case.oracle.actions
        if item.handler_invoked
    )

    result = grade_task(
        case=wrong_case,
        trace=trace,
        clean_replay_exact=True,
        replay_side_effect_free=True,
        tamper_error_code=None,
        forbidden_effects_absent=True,
        handler_ledger=ledger,
        protected_profile_reads=0,
        strategy_writes=0,
    )

    grounding = next(
        item for item in result.checks if item.name == "evidence_grounding"
    )
    assert grounding.passed is False
    assert result.passed is False


def test_suite_hash_is_pinned_to_the_reviewed_fixture(tmp_path: Path) -> None:
    destination = tmp_path / "configs" / "agent-eval"
    destination.mkdir(parents=True)
    source = ROOT / "configs" / "agent-eval" / "stage5-retrieval-v1.json"
    shutil.copyfile(source, destination / source.name)
    with (destination / source.name).open("a", encoding="utf-8") as handle:
        handle.write(" ")

    with pytest.raises(ValueError, match="suite hash"):
        load_agent_eval_suite(project_root=tmp_path)


def test_semantic_evidence_is_repeatable_while_execution_receipt_is_dynamic(
    agent_eval_runs: _Runs,
) -> None:
    assert agent_eval_runs.first.evidence.model_dump(
        mode="json"
    ) == agent_eval_runs.second.evidence.model_dump(mode="json")
    assert (
        agent_eval_runs.first.evidence.evidence_id
        == agent_eval_runs.second.evidence.evidence_id
    )
    assert (
        agent_eval_runs.first.execution.execution_id
        != agent_eval_runs.second.execution.execution_id
    )
    assert agent_eval_runs.first.execution.tasks[0].trace_id != (
        agent_eval_runs.second.execution.tasks[0].trace_id
    )


def test_agent_eval_artifacts_are_confined_and_no_strategy_state_is_written(
    agent_eval_runs: _Runs,
) -> None:
    root = agent_eval_runs.store
    assert Path(agent_eval_runs.first.evidence_path).is_file()
    assert Path(agent_eval_runs.first.execution_path).is_file()
    forbidden = {
        "active-strategy.json",
        "search-strategies",
        "strategy-decisions",
        "strategy-proposals",
    }
    assert not any(path.name in forbidden for path in root.rglob("*"))


def test_authority_snapshot_detects_strategy_content_changes(tmp_path: Path) -> None:
    before = agent_eval_runner._authority_snapshot(tmp_path)
    strategy = tmp_path / "search-strategies"
    strategy.mkdir()
    active = strategy / "active.json"
    active.write_text('{"revision":"a"}', encoding="utf-8")
    created = agent_eval_runner._authority_snapshot(tmp_path)
    active.write_text('{"revision":"b"}', encoding="utf-8")
    changed = agent_eval_runner._authority_snapshot(tmp_path)

    assert created != before
    assert changed != created


def test_agent_eval_store_has_a_hard_capacity_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "agent-evals"
    root.mkdir()
    (root / "receipt.json").write_bytes(b"{}")
    monkeypatch.setattr(agent_eval_runner, "MAX_AGENT_EVAL_STORE_BYTES", 1)

    with pytest.raises(RuntimeError, match="size limit"):
        agent_eval_runner._ensure_artifact_capacity(root)


def test_agent_eval_logging_is_independent_and_contains_no_query_payload(
    agent_eval_runs: _Runs,
) -> None:
    events = [
        json.loads(line) for line in agent_eval_runs.log_text.splitlines() if line
    ]
    assert events
    assert {item["module"] for item in events} == {"agent_eval"}
    assert any(item["event"] == "agent_eval_suite_completed" for item in events)
    assert all(
        "duration_ms" in item
        for item in events
        if item["event"] in {"agent_eval_task_completed", "agent_eval_suite_completed"}
    )
    contents = agent_eval_runs.log_text
    for forbidden in (
        '"query"',
        '"query_text"',
        '"product_id"',
        '"payload"',
        "wireless mouse",
    ):
        assert forbidden not in contents


def test_agent_eval_task_failure_log_has_stage_and_duration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingTools:
        def __init__(self, **_kwargs):
            raise RuntimeError("private Query and /private/source/path")

    stream = io.StringIO()
    configure_logging(
        default_level="OFF",
        module_levels={"agent_eval": "INFO"},
        stream=stream,
    )
    monkeypatch.setattr(agent_eval_runner, "StageRetrievalTools", FailingTools)

    with pytest.raises(RuntimeError, match="private Query"):
        run_agent_eval_suite(
            project_root=ROOT,
            artifact_root=tmp_path,
            revision_provider=lambda _root: "b" * 40,
        )
    configure_logging(default_level="OFF", stream=io.StringIO())

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    failed = next(item for item in events if item["event"] == "agent_eval_task_failed")
    assert failed["failure_stage"] == "runtime_or_grade"
    assert failed["duration_ms"] >= 0.0
    assert failed["error_type"] == "RuntimeError"
    assert "private Query" not in stream.getvalue()
    assert "/private/source/path" not in stream.getvalue()


def test_agent_eval_cli_failure_is_actionable_and_private(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(**_kwargs):
        raise ValueError("private Query and /private/evidence/path")

    monkeypatch.setattr(agent_eval_cli, "run_agent_eval_suite", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "search-quality-agent-eval",
            "--log-level",
            "OFF",
            "--log-module",
            "agent_eval=INFO",
        ],
    )
    with pytest.raises(SystemExit) as captured:
        agent_eval_cli.main()
    diagnostics = capsys.readouterr().err
    configure_logging(default_level="OFF", stream=io.StringIO())

    assert captured.value.code == 1
    event = json.loads(diagnostics)
    assert event["module"] == "agent_eval"
    assert event["event"] == "agent_eval_command_failed"
    assert event["error_type"] == "ValueError"
    assert event["failure_stage"] == "execute_suite"
    assert event["duration_ms"] >= 0.0
    assert "private Query" not in diagnostics
    assert "/private/evidence/path" not in diagnostics
