"""Source-bounded behavioral diagnostics over the full-catalog baseline."""

from .contracts import (
    BadCaseCategory,
    BadCaseDiagnosticArtifact,
    BadCaseRun,
    BadCaseSample,
)
from .runner import (
    rerun_bad_case_diagnostic,
    run_bad_case_diagnostics,
    validate_bad_case_diagnostic,
)

__all__ = [
    "BadCaseCategory",
    "BadCaseDiagnosticArtifact",
    "BadCaseRun",
    "BadCaseSample",
    "rerun_bad_case_diagnostic",
    "run_bad_case_diagnostics",
    "validate_bad_case_diagnostic",
]
