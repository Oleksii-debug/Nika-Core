from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

MAX_EVIDENCE_REF_LENGTH = 512
PRODUCT_FACTORY_REQUIRED_CHECK_IDS = ("core", "factory")


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
        if type(self.check_id) is not str or type(self.evidence_ref) is not str:
            raise VerificationError("verification evidence identity must be text")
        if not self.check_id.strip() or not self.evidence_ref.strip():
            raise VerificationError("verification evidence identity must not be empty")
        _validate_utf8(self.check_id, "verification check id")
        _validate_utf8(self.evidence_ref, "verification evidence ref")
        _validate_evidence_ref(self.evidence_ref)
        _validate_sha(self.candidate_sha)
        if type(self.state) is not CheckState:
            raise VerificationError("verification check state must be a CheckState")
        if type(self.required) is not bool:
            raise VerificationError("verification required flag must be a bool")


@dataclass(frozen=True, slots=True, init=False)
class CandidateVerification:
    """Machine-readable verification classification for one exact candidate head."""

    candidate_sha: str
    state: VerificationState
    evidence_refs: tuple[str, ...]

    def __init__(
        self,
        candidate_sha: str,
        state: VerificationState,
        evidence_refs: tuple[str, ...],
    ) -> None:
        _validate_candidate_verification(candidate_sha, state, evidence_refs)
        if state is VerificationState.PASS:
            raise VerificationError("PASS verification is classifier-owned")
        object.__setattr__(self, "candidate_sha", candidate_sha)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "evidence_refs", evidence_refs)

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

    Product Factory clearance is bound to the canonical required-check profile. Callers may
    provide that profile explicitly for adapter compatibility, but may not omit or substitute
    gates. Missing required evidence remains UNKNOWN, so partial observations never clear merge.
    """

    _validate_sha(candidate_sha)
    _validate_required_check_ids(required_check_ids)
    _validate_authoritative_required_profile(required_check_ids)
    if type(evidence) is not tuple:
        raise VerificationError("verification evidence must be a tuple")

    canonical_evidence = tuple(_snapshot_evidence(item) for item in evidence)
    refs = tuple(item.evidence_ref for item in canonical_evidence)
    if len(refs) != len(set(refs)):
        raise VerificationError("verification evidence refs must be unique")
    if not canonical_evidence:
        return CandidateVerification(candidate_sha, VerificationState.UNKNOWN, ())

    observed_shas = {item.candidate_sha for item in canonical_evidence}
    if candidate_sha not in observed_shas:
        state = VerificationState.STALE if len(observed_shas) == 1 else VerificationState.MISMATCH
        return CandidateVerification(candidate_sha, state, refs)
    if observed_shas != {candidate_sha}:
        return CandidateVerification(candidate_sha, VerificationState.MISMATCH, refs)

    evidence_by_check = {item.check_id: item for item in canonical_evidence}
    if len(evidence_by_check) != len(canonical_evidence):
        raise VerificationError("verification check ids must be unique")

    missing_required = set(required_check_ids) - evidence_by_check.keys()
    if missing_required:
        return CandidateVerification(candidate_sha, VerificationState.UNKNOWN, refs)

    unexpected_required = tuple(
        item.check_id
        for item in canonical_evidence
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

    _validate_candidate_verification(candidate_sha, VerificationState.PASS, refs)
    result = object.__new__(CandidateVerification)
    object.__setattr__(result, "candidate_sha", candidate_sha)
    object.__setattr__(result, "state", VerificationState.PASS)
    object.__setattr__(result, "evidence_refs", refs)
    return result


def _snapshot_evidence(item: object) -> ExactShaCheckEvidence:
    if type(item) is not ExactShaCheckEvidence:
        raise VerificationError("verification evidence must be ExactShaCheckEvidence")
    try:
        check_id = item.check_id
        candidate_sha = item.candidate_sha
        state = item.state
        evidence_ref = item.evidence_ref
        required = item.required
    except AttributeError as exc:
        raise VerificationError("verification evidence is incomplete") from exc
    return ExactShaCheckEvidence(
        check_id=check_id,
        candidate_sha=candidate_sha,
        state=state,
        evidence_ref=evidence_ref,
        required=required,
    )


def _validate_candidate_verification(
    candidate_sha: str,
    state: VerificationState,
    evidence_refs: tuple[str, ...],
) -> None:
    _validate_sha(candidate_sha)
    if type(state) is not VerificationState:
        raise VerificationError("verification state must be a VerificationState")
    if type(evidence_refs) is not tuple:
        raise VerificationError("verification evidence refs must be a tuple")
    if any(type(ref) is not str or not ref.strip() for ref in evidence_refs):
        raise VerificationError("verification evidence refs must be non-empty text")
    for ref in evidence_refs:
        _validate_utf8(ref, "verification evidence ref")
        _validate_evidence_ref(ref)
    if len(evidence_refs) != len(set(evidence_refs)):
        raise VerificationError("verification evidence refs must be unique")


def _validate_required_check_ids(required_check_ids: tuple[str, ...]) -> None:
    if type(required_check_ids) is not tuple:
        raise VerificationError("required check ids must be a tuple")
    if not required_check_ids:
        raise VerificationError("required check ids must not be empty")
    if any(type(check_id) is not str or not check_id.strip() for check_id in required_check_ids):
        raise VerificationError("required check ids must be non-empty text")
    for check_id in required_check_ids:
        _validate_utf8(check_id, "required check id")
    if len(required_check_ids) != len(set(required_check_ids)):
        raise VerificationError("required check ids must be unique")


def _validate_authoritative_required_profile(required_check_ids: tuple[str, ...]) -> None:
    if required_check_ids != PRODUCT_FACTORY_REQUIRED_CHECK_IDS:
        raise VerificationError(
            "required check ids must match authoritative Product Factory profile"
        )


def _validate_evidence_ref(value: str) -> None:
    if len(value) > MAX_EVIDENCE_REF_LENGTH:
        raise VerificationError("verification evidence ref exceeds maximum length")


def _validate_utf8(value: str, field: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise VerificationError(f"{field} must be valid UTF-8 text") from exc


def _validate_sha(value: str) -> None:
    if type(value) is not str:
        raise VerificationError("candidate SHA must be a lowercase 40-character hex digest")
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise VerificationError("candidate SHA must be a lowercase 40-character hex digest")
