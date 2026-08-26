"""Versioned ESCI relevance policies used by evaluation runs."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from numbers import Real
from pathlib import Path
from types import MappingProxyType
from typing import Any

ESCI_LABELS = ("E", "S", "C", "I")
ESCI_LABEL_SET = frozenset(ESCI_LABELS)
POLICY_SCHEMA_VERSION = "relevance-policy-v1"


@dataclass(frozen=True, slots=True)
class RelevancePolicy:
    """Immutable mapping from ESCI labels to metric gains and binary relevance."""

    policy_id: str
    label_gains: Mapping[str, float]
    relevant_labels: frozenset[str]
    description: str = ""
    schema_version: str = POLICY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        policy_id = self.policy_id.strip()
        if not policy_id:
            raise ValueError("policy_id must not be empty")
        if self.schema_version != POLICY_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported relevance policy schema {self.schema_version!r}"
            )

        normalized_gains: dict[str, float] = {}
        for label, gain in self.label_gains.items():
            normalized_label = str(label).strip().upper()
            if normalized_label in normalized_gains:
                raise ValueError(f"duplicate normalized label {normalized_label}")
            if isinstance(gain, bool) or not isinstance(gain, Real):
                raise TypeError("label gains must be real numbers")
            value = float(gain)
            if not math.isfinite(value) or value < 0.0:
                raise ValueError("label gains must be finite and non-negative")
            normalized_gains[normalized_label] = value

        actual_labels = frozenset(normalized_gains)
        if actual_labels != ESCI_LABEL_SET:
            missing = sorted(ESCI_LABEL_SET - actual_labels)
            extra = sorted(actual_labels - ESCI_LABEL_SET)
            raise ValueError(
                f"label gains must define exactly E/S/C/I; missing={missing}, "
                f"extra={extra}"
            )
        if normalized_gains["I"] != 0.0:
            raise ValueError("Irrelevant label I must have zero gain")

        normalized_relevant = frozenset(
            str(label).strip().upper() for label in self.relevant_labels
        )
        if not normalized_relevant:
            raise ValueError("relevant_labels must not be empty")
        unknown_relevant = sorted(normalized_relevant - ESCI_LABEL_SET)
        if unknown_relevant:
            raise ValueError(f"unknown relevant labels: {unknown_relevant}")
        if "I" in normalized_relevant:
            raise ValueError("Irrelevant label I cannot count as relevant")

        object.__setattr__(self, "policy_id", policy_id)
        object.__setattr__(self, "label_gains", MappingProxyType(normalized_gains))
        object.__setattr__(self, "relevant_labels", normalized_relevant)
        object.__setattr__(self, "description", self.description.strip())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RelevancePolicy:
        label_gains = payload.get("label_gains")
        relevant_labels = payload.get("relevant_labels")
        if not isinstance(label_gains, Mapping):
            raise TypeError("label_gains must be an object")
        if not isinstance(relevant_labels, Sequence) or isinstance(
            relevant_labels, (str, bytes)
        ):
            raise TypeError("relevant_labels must be an array")
        return cls(
            policy_id=str(payload.get("policy_id", "")),
            label_gains=label_gains,
            relevant_labels=frozenset(relevant_labels),
            description=str(payload.get("description", "")),
            schema_version=str(payload.get("schema_version", "")),
        )

    @classmethod
    def from_path(cls, path: str | Path) -> RelevancePolicy:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("relevance policy file must contain an object")
        return cls.from_dict(payload)

    def gain(self, label: str) -> float:
        return self.label_gains[self._normalized_known_label(label)]

    def is_relevant(self, label: str) -> bool:
        return self._normalized_known_label(label) in self.relevant_labels

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "label_gains": {label: self.label_gains[label] for label in ESCI_LABELS},
            "policy_id": self.policy_id,
            "relevant_labels": [
                label for label in ESCI_LABELS if label in self.relevant_labels
            ],
            "schema_version": self.schema_version,
        }

    @staticmethod
    def _normalized_known_label(label: str) -> str:
        normalized = str(label).strip().upper()
        if normalized not in ESCI_LABEL_SET:
            raise ValueError(f"unknown ESCI label {label!r}")
        return normalized
