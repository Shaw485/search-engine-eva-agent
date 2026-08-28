"""Stable, non-sensitive errors exposed at the Agent tool boundary."""

from __future__ import annotations


class AgentToolError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class AgentPolicyError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
