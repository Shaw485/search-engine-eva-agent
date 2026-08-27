"""Learning-boundary authorization shared by every formal evaluation entry point."""

from __future__ import annotations

DEV_PROFILE_UNLOCKED = False


def ensure_profile_authorized(profile_id: str) -> None:
    """Reject protected profiles before their data file can be opened."""

    if profile_id == "dev" and not DEV_PROFILE_UNLOCKED:
        raise RuntimeError(
            "the 500-Query dev profile is locked until the Stage 1 Owner data-"
            "boundary checkpoint is completed and recorded"
        )
