from __future__ import annotations

from pathlib import Path

import pytest

from search_quality.evaluation.relevance import RelevancePolicy

POLICY_PATH = (
    Path(__file__).parents[1] / "configs" / "evaluation" / "esci-primary-v1.json"
)


def test_primary_policy_records_the_owner_approved_decision() -> None:
    policy = RelevancePolicy.from_path(POLICY_PATH)
    assert policy.policy_id == "esci-primary-v1"
    assert policy.label_gains == {
        "E": 1.0,
        "S": 0.1,
        "C": 0.01,
        "I": 0.0,
    }
    assert policy.relevant_labels == frozenset({"E", "S"})


def test_policy_mapping_is_immutable() -> None:
    policy = RelevancePolicy.from_path(POLICY_PATH)
    with pytest.raises(TypeError):
        policy.label_gains["E"] = 0.0


def test_policy_converts_labels_to_gains_and_binary_relevance() -> None:
    policy = RelevancePolicy.from_path(POLICY_PATH)
    assert [policy.gain(label) for label in "ESCI"] == [1.0, 0.1, 0.01, 0.0]
    assert [policy.is_relevant(label) for label in "ESCI"] == [
        True,
        True,
        False,
        False,
    ]


@pytest.mark.parametrize(
    "label_gains",
    [
        {"E": 1.0, "S": 0.1, "C": 0.01},
        {"E": 1.0, "S": 0.1, "C": 0.01, "I": 0.0, "X": 0.5},
    ],
)
def test_policy_requires_exactly_the_esci_labels(label_gains) -> None:
    with pytest.raises(ValueError, match="exactly E/S/C/I"):
        RelevancePolicy(
            policy_id="invalid",
            label_gains=label_gains,
            relevant_labels=frozenset({"E"}),
        )


@pytest.mark.parametrize("gain", [-1.0, float("nan"), float("inf")])
def test_policy_rejects_invalid_gains(gain: float) -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        RelevancePolicy(
            policy_id="invalid",
            label_gains={"E": gain, "S": 0.1, "C": 0.01, "I": 0.0},
            relevant_labels=frozenset({"E"}),
        )


def test_policy_requires_zero_gain_for_irrelevant() -> None:
    with pytest.raises(ValueError, match="zero gain"):
        RelevancePolicy(
            policy_id="invalid",
            label_gains={"E": 1.0, "S": 0.1, "C": 0.01, "I": 0.01},
            relevant_labels=frozenset({"E"}),
        )


@pytest.mark.parametrize("relevant_labels", [frozenset(), frozenset({"I"})])
def test_policy_rejects_invalid_relevant_labels(relevant_labels) -> None:
    with pytest.raises(ValueError):
        RelevancePolicy(
            policy_id="invalid",
            label_gains={"E": 1.0, "S": 0.1, "C": 0.01, "I": 0.0},
            relevant_labels=relevant_labels,
        )


def test_policy_rejects_unknown_labels_at_evaluation_time() -> None:
    policy = RelevancePolicy.from_path(POLICY_PATH)
    with pytest.raises(ValueError, match="unknown ESCI"):
        policy.gain("X")


def test_policy_round_trip_is_stable() -> None:
    policy = RelevancePolicy.from_path(POLICY_PATH)
    assert RelevancePolicy.from_dict(policy.to_dict()) == policy
