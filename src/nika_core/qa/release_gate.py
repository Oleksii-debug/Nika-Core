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

_EVIDENCE_FIELDS = tuple(field for field, _ in _AUTOMATED_REQUIREMENTS) + (
    "human_tested",
    "nvda_verified",
)


def evaluate_release_gate(evidence: ReleaseGateEvidence) -> ReleaseGateResult:
    # Dataclass annotations do not enforce runtime types. Never let truthy strings,
    # integers or corrupted/missing attributes certify an acceptance gate.
    values = {field: getattr(evidence, field, None) for field in _EVIDENCE_FIELDS}
    invalid_fields = tuple(field for field, value in values.items() if type(value) is not bool)

    def proven(field: str) -> bool:
        return values[field] is True and type(values[field]) is bool

    automated_blockers = tuple(
        message for field, message in _AUTOMATED_REQUIREMENTS if not proven(field)
    )
    release_candidate_ready = not automated_blockers and not invalid_fields

    blockers = [f"Invalid release evidence type: {field}" for field in invalid_fields]
    blockers.extend(automated_blockers)
    human_tested = proven("human_tested")
    nvda_verified = proven("nvda_verified")
    if not human_tested:
        blockers.append("Human accessibility/functional acceptance is missing")
    if not nvda_verified:
        blockers.append("NVDA verification by a human tester is missing")

    if nvda_verified and not human_tested:
        blockers.append("NVDA_VERIFIED cannot precede HUMAN_TESTED")

    production_release_ready = release_candidate_ready and human_tested and nvda_verified

    if production_release_ready:
        stage = "NVDA_VERIFIED"
    elif human_tested:
        stage = "HUMAN_TESTED"
    elif proven("windows_package_built"):
        stage = "PACKAGED"
    elif proven("core_ci_green"):
        stage = "INTEGRATED"
    else:
        stage = "IMPLEMENTED"

    return ReleaseGateResult(
        stage=stage,
        release_candidate_ready=release_candidate_ready,
        production_release_ready=production_release_ready,
        blockers=tuple(dict.fromkeys(blockers)),
    )
