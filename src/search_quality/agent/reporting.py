"""Deterministic terminal reports built only from observed evidence."""

from __future__ import annotations

from typing import Any

from .contracts import AgentTask, TerminalOutcome, ToolObservation


def build_terminal_report(
    *,
    task: AgentTask,
    outcome: TerminalOutcome,
    reason_code: str,
    observations: tuple[ToolObservation, ...],
    evidence_refs: list[str],
) -> dict[str, Any]:
    comparison = next(
        (
            item.payload
            for item in reversed(observations)
            if item.tool_name == "compare_runs" and item.status == "succeeded"
        ),
        None,
    )
    inspected_queries = [
        {
            "evidence_ref": item.evidence_ref,
            "query_id": item.payload.get("query_id"),
            "run_id": item.payload.get("run_id"),
        }
        for item in observations
        if item.tool_name == "inspect_query" and item.status == "succeeded"
    ]
    failed_tools = [
        {"error_code": item.error_code, "tool_name": item.tool_name}
        for item in observations
        if item.status == "failed"
    ]
    return {
        "boundary": {
            "full_catalog_recall_claimed": False,
            "profile": "smoke",
            "task": "judged-candidate-reranking",
        },
        "comparison": comparison,
        "decision": {
            "outcome": outcome.value,
            "reason_code": reason_code,
        },
        "evidence_refs": evidence_refs,
        "failed_tools": failed_tools,
        "inspected_queries": inspected_queries,
        "task": task.model_dump(mode="json"),
    }
