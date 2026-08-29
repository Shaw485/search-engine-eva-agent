"""Human Diagnostic Oracle: direct owner judgments over fixed Bad Case evidence."""

from .contracts import (
    BehaviorJudgment,
    BehaviorReason,
    BehaviorSubmission,
    HumanOracleArtifact,
    IntentJudgment,
    IntentReason,
    IntentSubmission,
    OracleActor,
    OracleBatchArtifact,
    OracleBatchProjection,
    OracleBatchStatus,
    OracleBehaviorView,
    OracleIntentView,
    OracleReviewState,
    SealSubmission,
)
from .policy import build_oracle_batch, validate_oracle_batch
from .storage import (
    HumanOracleRepository,
    OracleBatchIncomplete,
    OracleBatchSealed,
    OracleClientActionConflict,
    OracleCompareAndSwapConflict,
    OracleInvalidDecision,
    OracleStorageError,
)
from .views import (
    build_behavior_view,
    build_intent_view,
    collect_behavior_samples_for_unit,
)

__all__ = [
    "BehaviorJudgment",
    "BehaviorReason",
    "BehaviorSubmission",
    "HumanOracleArtifact",
    "HumanOracleRepository",
    "IntentJudgment",
    "IntentReason",
    "IntentSubmission",
    "OracleActor",
    "OracleBatchArtifact",
    "OracleBatchIncomplete",
    "OracleBatchProjection",
    "OracleBatchSealed",
    "OracleBatchStatus",
    "OracleBehaviorView",
    "OracleClientActionConflict",
    "OracleCompareAndSwapConflict",
    "OracleIntentView",
    "OracleInvalidDecision",
    "OracleReviewState",
    "OracleStorageError",
    "SealSubmission",
    "build_behavior_view",
    "build_intent_view",
    "build_oracle_batch",
    "collect_behavior_samples_for_unit",
    "validate_oracle_batch",
]
