"""Release-quality gates and accessibility checks."""

from .release_gate import ReleaseGateEvidence, ReleaseGateResult, evaluate_release_gate

__all__ = ["ReleaseGateEvidence", "ReleaseGateResult", "evaluate_release_gate"]
