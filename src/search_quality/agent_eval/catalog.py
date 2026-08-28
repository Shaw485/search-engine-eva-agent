"""Allowlisted loading for the fixed Agent Eval task dataset."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .contracts import AgentEvalSuite

MAX_SUITE_BYTES = 256 * 1024
SUITE_PATHS = {
    "stage5-retrieval-v1": Path("configs/agent-eval/stage5-retrieval-v1.json")
}
SUITE_SHA256 = {
    "stage5-retrieval-v1": (
        "ffc3aa6c5f666d2dc35080ad07c0fd5f75624a326b8e33973ea397c2fc4c4529"
    )
}


def load_agent_eval_suite(
    *, project_root: str | Path, suite_id: str = "stage5-retrieval-v1"
) -> tuple[AgentEvalSuite, str]:
    """Load one internal Suite; arbitrary paths and protected profiles are impossible."""

    relative = SUITE_PATHS.get(suite_id)
    if relative is None:
        raise ValueError("unsupported Agent Eval suite")
    root = Path(project_root).resolve(strict=True)
    path = root / relative
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("Agent Eval suite path must not contain a symbolic link")
    if not path.is_file():
        raise ValueError("Agent Eval suite is unavailable")
    with path.open("rb") as source:
        encoded = source.read(MAX_SUITE_BYTES + 1)
    if len(encoded) > MAX_SUITE_BYTES:
        raise ValueError("Agent Eval suite exceeds the size limit")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Agent Eval suite contains duplicate JSON keys")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("Agent Eval suite contains a non-finite number")

    payload = json.loads(
        encoded.decode("utf-8"),
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_constant,
    )
    observed_sha256 = hashlib.sha256(encoded).hexdigest()
    if observed_sha256 != SUITE_SHA256[suite_id]:
        raise ValueError("Agent Eval suite hash does not match the reviewed fixture")
    suite = AgentEvalSuite.model_validate(payload)
    if suite.profile != "smoke":
        raise ValueError("Agent Eval suite is not smoke-only")
    return suite, observed_sha256
