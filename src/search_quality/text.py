"""Deterministic text normalization used by Stage 0 backends."""

from __future__ import annotations

import re

TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


def tokenize(text: str) -> list[str]:
    """Return lowercase ASCII word/number tokens in input order."""

    return [match.group(0).lower() for match in TOKEN_RE.finditer(text)]
