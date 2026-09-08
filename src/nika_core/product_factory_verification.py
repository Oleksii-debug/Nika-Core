from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class VerificationError(ValueError):
    """Raised when Product Factory verification evidence is structurally invalid."""


class CheckState(StrEnum):
    UNKNOWN = "unknown"
    RUNNING = "running"
    PASS = "pass"
    FAIL = "fail"


class VerificationState(StrEnum):
    UNKNOWN = "unknown"
    RUNNING = "running"
    PASS = "pass"
    FAIL = "fail"
    STALE = "stale"
    MISMATCH = "mismatch"


@dataclass(frozen=True, slots=True)
class ExactShaCheckEvidence:
    """One bounded verification observation tied to exactly one candidate SHA."""

    check_id: str
    candidate_sha: str
    state: CheckState
    evidence_ref: str
    required: bool = True

    def __post_init__(self) -> None:
        if not self.check_id.strip() or not self.evidence_ref.strip():
            raise VerificationError("verification evidence identity must not be empty")
        _validate_sha(self.candidate_sha)


@dataclass(frozen=True, slots=True)
class CandidateVerification:
    """Machine-readable verification classification for one exact candidate head."""

    candidate_sha: str
    state: VerificationState
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_sha(self.candidate_sha)
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise VerificationError("verification evidence refs must be unique")

    @property
    def merge_clearance(self) -> bool:
        """Verification can only clear its own exact head after all required checks pass."""

        return self.state is VerificationState.PASS


def classify_candidate_verification(
    candidate_sha: str,
    evidence: tuple[ExactShaCheckEvidence, ...],
) -> CandidateVerification:
    """Classify bounded CI/test evidence without transferring clearance across SHAs.

    Evidence exclusively for a different single SHA is STALE. Mixed evidence identities are
    MISMATCH, even if the current candidate has passing observations, because a machine consumer
    must not silently combine evidence from different candidate heads.
    """

    _validate_sha(candidate_sha)
    refs = tuple(item.evidence_ref for item in evidence)
    if len(refs) != len(set(refs)):
        raise VerificationError("verification evidence refs must be unique")
    if not evidence:
        return CandidateVerification(candidate_sha, VerificationState.UNKNOWN, ())

    observed_shas = {item.candidate_sha for item in evidence}
    if candidate_sha not in observed_shas:
        state = VerificationState.STALE if len(observed_shas) == 1 else VerificationState.MISMATCH
        return CandidateVerification(candidate_sha, state, refs)
    if observed_shas != {candidate_sha}:
        return CandidateVerification(candidate_sha, VerificationState.MISMATCH, refs)

    required = tuple(item for item in evidence if item.required)
    if not required:
        return CandidateVerification(candidate_sha, VerificationState.UNKNOWN, refs)
    if any(item.state is CheckState.FAIL for item in required):
        return CandidateVerification(candidate_sha, VerificationState.FAIL, refs)
    if any(item.state is CheckState.RUNNING for item in required):
        return CandidateVerification(candidate_sha, VerificationState.RUNNING, refs)
    if any(item.state is CheckState.UNKNOWN for item in required):
        return CandidateVerification(candidate_sha, VerificationState.UNKNOWN, refs)
    return CandidateVerification(candidate_sha, VerificationState.PASS, refs)


def _validate_sha(value: str) -> None:
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise VerificationError("candidate SHA must be a lowercase 40-character hex digest")
