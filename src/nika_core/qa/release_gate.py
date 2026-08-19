from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReleaseGateEvidence:
    core_ci_green: bool = False
    windows_package_built: bool = False
    package_smoke_passed: bool = False
    manifest_verified: bool = False
    third_party_notices_verified: bool = False
    recovery_drill_passed: bool = False
    packaged_uia_passed: bool = False
    human_tested: bool = False
    nvda_verified: bool = False


@dataclass(frozen=True, slots=True)
class ReleaseGateResult:
    stage: str
    release_candidate_ready: bool
    production_release_ready: bool
    blockers: tuple[str, ...]


_AUTOMATED_REQUIREMENTS = (
    ("core_ci_green", "Core CI is not green on the exact candidate"),
    ("windows_package_built", "Windows release package has not been built"),
    ("package_smoke_passed", "Packaged application smoke proof is missing"),
    ("manifest_verified", "Release manifest integrity verification is missing"),
    ("third_party_notices_verified", "Third-party release notices/license evidence is missing"),
    ("recovery_drill_passed", "Full-system recovery drill is missing"),
    ("packaged_uia_passed", "Packaged UI Automation proof is missing"),
)


def evaluate_release_gate(evidence: ReleaseGateEvidence) -> ReleaseGateResult:
    automated_blockers = tuple(
        message for field, message in _AUTOMATED_REQUIREMENTS if not getattr(evidence, field)
    )
    release_candidate_ready = not automated_blockers

    blockers = list(automated_blockers)
    if not evidence.human_tested:
        blockers.append("Human accessibility/functional acceptance is missing")
    if not evidence.nvda_verified:
        blockers.append("NVDA verification by a human tester is missing")

    if evidence.nvda_verified and not evidence.human_tested:
        blockers.append("NVDA_VERIFIED cannot precede HUMAN_TESTED")

    production_release_ready = release_candidate_ready and evidence.human_tested and evidence.nvda_verified

    if production_release_ready:
        stage = "NVDA_VERIFIED"
    elif evidence.human_tested:
        stage = "HUMAN_TESTED"
    elif evidence.windows_package_built:
        stage = "PACKAGED"
    elif evidence.core_ci_green:
        stage = "INTEGRATED"
    else:
        stage = "IMPLEMENTED"

    return ReleaseGateResult(
        stage=stage,
        release_candidate_ready=release_candidate_ready,
        production_release_ready=production_release_ready,
        blockers=tuple(dict.fromkeys(blockers)),
    )
