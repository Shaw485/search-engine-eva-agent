"""Static tool registry with schema validation and no dynamic imports."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticSerializationError

from .errors import AgentPolicyError, AgentToolError


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    capability: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: Callable[[BaseModel], Any]


class AgentToolRegistry:
    """One immutable mapping of allowlisted domain tools."""

    def __init__(self, specs: Iterable[ToolSpec]) -> None:
        items = tuple(specs)
        by_name = {item.name: item for item in items}
        if not items or len(by_name) != len(items):
            raise ValueError("tool registry must contain unique tools")
        self._specs = by_name

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._specs)

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        allowed_capabilities: frozenset[str],
    ) -> dict[str, Any]:
        spec = self._specs.get(tool_name)
        if spec is None:
            raise AgentPolicyError("tool_not_allowed")
        if spec.capability not in allowed_capabilities:
            raise AgentPolicyError("capability_not_allowed")
        try:
            validated = spec.input_model.model_validate(arguments)
        except (TypeError, ValueError, ValidationError) as exc:
            raise AgentToolError("invalid_argument") from exc
        result = spec.handler(validated)
        try:
            output = spec.output_model.model_validate(result)
            serialized = output.model_dump(mode="json", by_alias=True)
        except (
            TypeError,
            ValueError,
            ValidationError,
            PydanticSerializationError,
        ) as exc:
            raise AgentToolError("invalid_tool_result") from exc
        return serialized

    def schemas(self) -> dict[str, dict[str, Any]]:
        return {
            name: spec.input_model.model_json_schema()
            for name, spec in sorted(self._specs.items())
        }
