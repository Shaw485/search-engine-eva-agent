from __future__ import annotations

import io
import json

import pytest
from pydantic import ValidationError

from search_quality.agent.strategy_search import (
    BadCaseInput,
    ExactBoostParameters,
    GatePolicy,
    diagnose_bad_case,
    score_strategy_comparison,
    select_exact_boost_candidates,
    select_winner,
)
from search_quality.agent.tools import CompareRunsPayload
from search_quality.observability import configure_logging


def _bad_case(
    *,
    query_id: str = "q-1",
    query: str = "usb c 65w charger",
    relevant_title: str = "USB C 65W Charger",
    top_title: str | None = "USB C Fast Cable",
    rank: int = 4,
    title_signal_used: bool = False,
) -> BadCaseInput:
    return BadCaseInput(
        query_id=query_id,
        query_text=query,
        relevant_title=relevant_title,
        baseline_top_title=top_title,
        relevant_rank=rank,
        title_signal_used=title_signal_used,
    )


def _metric(baseline: float, delta: float) -> dict[str, float]:
    return {
        "baseline": baseline,
        "candidate": baseline + delta,
        "delta": delta,
    }


def _comparison(
    *,
    comparison_suffix: str = "c",
    candidate_prefix: str = "exact-boost",
    ndcg_delta: float = 0.04,
    ndcg_5_delta: float = 0.03,
    mrr_delta: float = 0.02,
    success_1_delta: float = 0.01,
    success_5_delta: float = 0.01,
    query_count: int = 10,
    regression_deltas: tuple[float, ...] = (-0.04,),
) -> CompareRunsPayload:
    regression_count = len(regression_deltas)
    improved_count = 3
    tied_count = query_count - regression_count - improved_count
    if tied_count < 0:
        raise ValueError("test comparison counts exceed query_count")

    def counts(delta: float) -> dict[str, int]:
        if delta > 0:
            return {
                "improved": improved_count,
                "regressed": regression_count,
                "tied": tied_count,
            }
        if delta < 0:
            return {
                "improved": regression_count,
                "regressed": improved_count,
                "tied": tied_count,
            }
        return {"improved": 0, "regressed": 0, "tied": query_count}

    improvements = [
        {
            "changed_rank_count": 2,
            "ndcg@10_delta": value,
            "query_id": 100 + index,
            "top_10_changed": True,
        }
        for index, value in enumerate((0.20, 0.10, 0.05)[:improved_count])
    ]
    regressions = [
        {
            "changed_rank_count": 2,
            "ndcg@10_delta": value,
            "query_id": 200 + index,
            "top_10_changed": True,
        }
        for index, value in enumerate(sorted(regression_deltas))
    ]
    aggregate = {
        "ndcg@5": _metric(0.50, ndcg_5_delta),
        "ndcg@10": _metric(0.55, ndcg_delta),
        "mrr@10": _metric(0.45, mrr_delta),
        "success@1": _metric(0.40, success_1_delta),
        "success@5": _metric(0.70, success_5_delta),
    }
    return CompareRunsPayload.model_validate(
        {
            "aggregate_metrics": aggregate,
            "baseline_run_id": "bm25-aaaaaaaaaaaa",
            "candidate_run_id": f"{candidate_prefix}-bbbbbbbbbbbb",
            "comparison_id": f"comparison-{comparison_suffix * 12}",
            "comparison_epsilon": 1e-12,
            "improvements": improvements,
            "outcome_counts": {
                name: counts(values["delta"]) for name, values in aggregate.items()
            },
            "query_count": query_count,
            "regressions": regressions,
        }
    )


def test_diagnosis_finds_all_allowlisted_root_causes_without_raw_text() -> None:
    evidence = _bad_case()

    first = diagnose_bad_case(evidence)
    second = diagnose_bad_case(evidence)

    assert first == second
    assert [finding.cause for finding in first.findings] == [
        "numeric_token",
        "coverage_gap",
        "exact_phrase_displaced",
    ]
    assert first.signals.numeric_query_token_count == 1
    assert first.signals.numeric_missing_from_top_count == 1
    assert first.signals.relevant_query_coverage == 1.0
    assert first.signals.baseline_top_query_coverage == 0.5
    dumped = json.dumps(first.model_dump(mode="json"), allow_nan=False)
    assert evidence.query_text not in dumped
    assert evidence.relevant_title not in dumped
    assert evidence.baseline_top_title not in dumped


def test_diagnosis_requires_displacement_and_supporting_evidence() -> None:
    diagnosis = diagnose_bad_case(
        _bad_case(
            query="wireless mouse",
            relevant_title="Wireless Mouse",
            top_title="Wireless Mouse",
            rank=1,
            title_signal_used=True,
        )
    )

    assert diagnosis.findings == []
    assert diagnosis.signals.coverage_gap == 0.0


def test_diagnosis_marks_missing_title_signal_as_unaddressable() -> None:
    diagnosis = diagnose_bad_case(
        _bad_case(
            query="quiet office mouse",
            relevant_title="Ergonomic Peripheral",
            top_title="Gaming Mouse",
            title_signal_used=False,
        )
    )

    assert [finding.cause for finding in diagnosis.findings] == ["missing_title_signal"]
    assert select_exact_boost_candidates([diagnosis]).candidates == []


def test_bad_case_and_parameter_contracts_are_strict() -> None:
    payload = _bad_case().model_dump(mode="json")
    payload["unknown"] = "not allowed"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        BadCaseInput.model_validate(payload)
    with pytest.raises(ValidationError):
        ExactBoostParameters(
            coverage_boost=float("nan"),
            numeric_boost=1.0,
            phrase_boost=1.0,
        )
    with pytest.raises(ValidationError):
        BadCaseInput(
            query_id="q-2",
            query_text="中文",
            relevant_title="Product",
            relevant_rank=2,
            title_signal_used=True,
        )


def test_candidate_selection_is_bounded_targeted_and_deterministic() -> None:
    diagnoses = [
        diagnose_bad_case(_bad_case(query_id="q-2")),
        diagnose_bad_case(
            _bad_case(
                query_id="q-1",
                query="quiet office mouse",
                relevant_title="Quiet Office Mouse",
                top_title="Gaming Mouse",
                title_signal_used=True,
            )
        ),
    ]

    first = select_exact_boost_candidates(diagnoses, max_candidates=3)
    second = select_exact_boost_candidates(diagnoses, max_candidates=3)

    assert first == second
    assert len(first.candidates) == 3
    assert [candidate.candidate_id for candidate in first.candidates] == [
        "exact-conservative-v1",
        "exact-coverage-v1",
        "exact-phrase-v1",
    ]
    assert first.candidates[0].supporting_query_ids == ["q-1", "q-2"]
    assert first.candidates[0].parameters.coverage_boost == 0.2
    assert first.candidates[1].parameters.coverage_boost == 1.4
    with pytest.raises(ValueError, match="duplicate IDs"):
        select_exact_boost_candidates([diagnoses[0], diagnoses[0]])
    with pytest.raises(ValueError, match="between 1 and 4"):
        select_exact_boost_candidates(diagnoses, max_candidates=5)


def test_comparison_selection_score_is_transparent_and_hard_gated() -> None:
    candidate = select_exact_boost_candidates(
        [diagnose_bad_case(_bad_case())],
        max_candidates=1,
    ).candidates[0]

    evaluation = score_strategy_comparison(candidate, _comparison())

    assert evaluation.eligible is True
    assert evaluation.metrics.success_at_5.model_dump() == pytest.approx(
        {"baseline": 0.70, "candidate": 0.71, "delta": 0.01}
    )
    assert evaluation.metrics.mrr_at_10.model_dump() == pytest.approx(
        {"baseline": 0.45, "candidate": 0.47, "delta": 0.02}
    )
    assert evaluation.metrics.ndcg_at_10.model_dump() == pytest.approx(
        {"baseline": 0.55, "candidate": 0.59, "delta": 0.04}
    )
    assert evaluation.success_at_5_delta == evaluation.metrics.success_at_5.delta
    assert evaluation.mrr_at_10_delta == evaluation.metrics.mrr_at_10.delta
    assert evaluation.ndcg_at_10_delta == evaluation.metrics.ndcg_at_10.delta
    assert evaluation.selection_score.total == pytest.approx(0.0155)
    assert {
        component.name: component.contribution
        for component in evaluation.selection_score.components
    } == pytest.approx(
        {
            "ndcg@10_delta": 0.02,
            "ndcg@5_delta": 0.0045,
            "mrr@10_delta": 0.003,
            "success@1_delta": 0.0015,
            "success@5_delta": 0.0005,
            "ndcg@10_regression_rate": -0.01,
            "worst_ndcg@10_regression_magnitude": -0.004,
        }
    )
    assert all(check.passed for check in evaluation.gates.checks)

    gated = score_strategy_comparison(
        candidate,
        _comparison(
            comparison_suffix="d",
            success_1_delta=-0.03,
            query_count=20,
            regression_deltas=(-0.25, -0.10),
        ),
    )
    assert gated.eligible is False
    assert [check.name for check in gated.gates.checks if not check.passed] == [
        "success@1_floor",
        "worst_ndcg@10_regression_ceiling",
    ]

    protected_metric_regression = score_strategy_comparison(
        candidate,
        _comparison(
            comparison_suffix="9",
            mrr_delta=-0.01,
            success_5_delta=-0.01,
        ),
    )
    assert protected_metric_regression.eligible is False
    assert [
        check.name
        for check in protected_metric_regression.gates.checks
        if not check.passed
    ] == ["mrr@10_floor", "success@5_floor"]


def test_custom_gate_policy_can_be_stric_without_changing_selection_score() -> None:
    candidate = select_exact_boost_candidates(
        [diagnose_bad_case(_bad_case())],
        max_candidates=1,
    ).candidates[0]
    comparison = _comparison()
    default = score_strategy_comparison(candidate, comparison)
    strict = score_strategy_comparison(
        candidate,
        comparison,
        gate_policy=GatePolicy(min_ndcg_at_10_delta=0.05),
    )

    assert default.selection_score == strict.selection_score
    assert default.eligible is True
    assert strict.eligible is False


def test_winner_uses_selection_score_then_deterministic_tie_breaks() -> None:
    candidates = select_exact_boost_candidates(
        [diagnose_bad_case(_bad_case())],
        max_candidates=4,
    ).candidates
    evaluations = [
        score_strategy_comparison(candidates[0], _comparison(comparison_suffix="e")),
        score_strategy_comparison(
            candidates[1],
            _comparison(
                comparison_suffix="f",
                candidate_prefix="exact-boost-two",
                ndcg_delta=0.06,
                ndcg_5_delta=0.05,
            ),
        ),
    ]

    selection = select_winner(list(reversed(evaluations)))

    assert selection.status == "winner_selected"
    assert selection.winner_candidate_id == candidates[1].candidate_id
    assert selection.ranked_candidate_ids == [
        candidates[1].candidate_id,
        candidates[0].candidate_id,
    ]
    assert select_winner(list(reversed(evaluations))) == selection


def test_winner_reports_no_candidate_when_every_gate_fails() -> None:
    candidate = select_exact_boost_candidates(
        [diagnose_bad_case(_bad_case())],
        max_candidates=1,
    ).candidates[0]
    failed = score_strategy_comparison(
        candidate,
        _comparison(comparison_suffix="1"),
        gate_policy=GatePolicy(min_ndcg_at_10_delta=0.05),
    )

    selection = select_winner([failed])

    assert selection.status == "no_passing_candidate"
    assert selection.winner_candidate_id is None
    assert selection.ranked_candidate_ids == []


def test_strategy_search_logs_ids_and_counts_without_query_or_titles() -> None:
    stream = io.StringIO()
    configure_logging(
        default_level="WARNING",
        module_levels={"agent_optimization": "INFO"},
        stream=stream,
    )
    evidence = _bad_case(
        query="private 65w query",
        relevant_title="private 65w relevant title",
        top_title="private top title",
    )

    diagnosis = diagnose_bad_case(evidence)
    candidate = select_exact_boost_candidates([diagnosis]).candidates[0]
    evaluation = score_strategy_comparison(candidate, _comparison())
    select_winner([evaluation])

    logs = stream.getvalue()
    assert evidence.query_text not in logs
    assert evidence.relevant_title not in logs
    assert evidence.baseline_top_title not in logs
    events = [json.loads(line)["event"] for line in logs.splitlines()]
    assert events == [
        "bad_case_diagnosed",
        "strategy_candidates_selected",
        "strategy_comparison_scored",
        "strategy_winner_selected",
    ]
