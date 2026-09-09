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
        if not isinstance(self.check_id, str) or not isinstance(self.evidence_ref, str):
            raise VerificationError("verification evidence identity must be text")
        if not self.check_id.strip() or not self.evidence_ref.strip():
            raise VerificationError("verification evidence identity must not be empty")
        _validate_sha(self.candidate_sha)
        if not isinstance(self.state, CheckState):
            raise VerificationError("verification check state must be a CheckState")
        if not isinstance(self.required, bool):
            raise VerificationError("verification required flag must be a bool")


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
    required_check_ids: tuple[str, ...],
) -> CandidateVerification:
    """Classify bounded CI/test evidence without transferring clearance across SHAs.

    The caller supplies the authoritative required-check identity set. Missing required evidence
    remains UNKNOWN, so a partial observation set can never become merge clearance.
    """

    _validate_sha(candidate_sha)
    _validate_required_check_ids(required_check_ids)

    if any(not isinstance(item, ExactShaCheckEvidence) for item in evidence):
        raise VerificationError("verification evidence must be ExactShaCheckEvidence")

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

    evidence_by_check = {item.check_id: item for item in evidence}
    if len(evidence_by_check) != len(evidence):
        raise VerificationError("verification check ids must be unique")

    missing_required = set(required_check_ids) - evidence_by_check.keys()
    if missing_required:
        return CandidateVerification(candidate_sha, VerificationState.UNKNOWN, refs)

    unexpected_required = tuple(
        item.check_id
        for item in evidence
        if item.required and item.check_id not in required_check_ids
    )
    if unexpected_required:
        raise VerificationError("required verification check id is not authoritative")

    required = tuple(evidence_by_check[check_id] for check_id in required_check_ids)
    if any(item.state is CheckState.FAIL for item in required):
        return CandidateVerification(candidate_sha, VerificationState.FAIL, refs)
    if any(item.state is CheckState.RUNNING for item in required):
        return CandidateVerification(candidate_sha, VerificationState.RUNNING, refs)
    if any(item.state is CheckState.UNKNOWN for item in required):
        return CandidateVerification(candidate_sha, VerificationState.UNKNOWN, refs)
    return CandidateVerification(candidate_sha, VerificationState.PASS, refs)


def _validate_required_check_ids(required_check_ids: tuple[str, ...]) -> None:
    if not required_check_ids:
        raise VerificationError("required check ids must not be empty")
    if any(not check_id.strip() for check_id in required_check_ids):
        raise VerificationError("required check ids must not be empty")
    if len(required_check_ids) != len(set(required_check_ids)):
        raise VerificationError("required check ids must be unique")


def _validate_sha(value: str) -> None:
    if not isinstance(value, str):
        raise VerificationError("candidate SHA must be a lowercase 40-character hex digest")
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise VerificationError("candidate SHA must be a lowercase 40-character hex digest")
