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


class AgentReplayError(ValueError):
    """Stable, privacy-safe failure raised by offline Trace validation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
