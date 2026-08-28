"""Confined immutable storage for deterministic evidence and execution receipts."""

from __future__ import annotations

import logging
from pathlib import Path

from search_quality.evaluation.artifacts import (
    atomic_write_text,
    write_immutable_json,
)

from .contracts import AgentEvalEvidence, AgentEvalExecutionReceipt

logger = logging.getLogger("search_quality.agent_eval")


def store_agent_eval_artifacts(
    *,
    artifact_root: str | Path,
    evidence: AgentEvalEvidence,
    execution: AgentEvalExecutionReceipt,
) -> tuple[Path, Path]:
    root = _trusted_root(artifact_root)
    base = _trusted_child(root, "agent-evals")
    evidence_dir = _trusted_child(base, "evidence")
    execution_dir = _trusted_child(base, "executions")
    evidence_path = evidence_dir / f"{evidence.evidence_id}.json"
    execution_path = execution_dir / f"{execution.execution_id}.json"
    write_immutable_json(evidence_path, evidence.model_dump(mode="json"))
    write_immutable_json(execution_path, execution.model_dump(mode="json"))
    latest = base / "latest-stage5-retrieval-v1.txt"
    if latest.is_symlink():
        raise ValueError("Agent Eval latest pointer must not be a symbolic link")
    atomic_write_text(latest, f"{evidence.evidence_id}\n")
    logger.info(
        "agent_eval_artifacts_stored",
        extra={
            "evidence_id": evidence.evidence_id,
            "execution_id": execution.execution_id,
            "task_count": len(evidence.tasks),
        },
    )
    return evidence_path, execution_path


def trusted_agent_eval_root(artifact_root: str | Path) -> Path:
    return _trusted_child(_trusted_root(artifact_root), "agent-evals")


def _trusted_root(value: str | Path) -> Path:
    configured = Path(value)
    if not configured.is_absolute():
        raise ValueError("Agent Eval artifact root must be absolute")
    if configured.is_symlink():
        raise ValueError("Agent Eval artifact root must not be a symbolic link")
    configured.mkdir(parents=True, exist_ok=True)
    root = configured.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Agent Eval artifact root must be a directory")
    return root


def _trusted_child(parent: Path, name: str) -> Path:
    child = parent / name
    if child.is_symlink():
        raise ValueError("Agent Eval artifact directory must not be a symbolic link")
    child.mkdir(parents=True, exist_ok=True)
    resolved = child.resolve(strict=True)
    if resolved.parent != parent:
        raise ValueError("Agent Eval artifact directory escaped its configured root")
    return resolved
