from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import StrEnum

_MAX_EVIDENCE_REFS = 16
_MAX_EVIDENCE_REF_CHARS = 256
_MAX_REASON_CHARS = 2048


class ReviewPipelineError(ValueError):
    """Raised when Product Factory review/QA invariants are violated."""


class StaleCandidateReviewError(ReviewPipelineError):
    """Raised when review evidence targets a different candidate SHA."""


class ReviewState(StrEnum):
    IMPLEMENTED = "implemented"
    REVIEW_REQUIRED = "review_required"
    QA_PENDING = "qa_pending"
    QA_RUNNING = "qa_running"
    PASS = "pass"
    FAIL = "fail"
    FIX_REQUIRED = "fix_required"
    MERGE_READY = "merge_ready"


@dataclass(frozen=True, slots=True)
class CandidateReviewIdentity:
    work_id: str
    candidate_sha: str
    implementer_id: str

    def __post_init__(self) -> None:
        if not self.work_id.strip() or not self.implementer_id.strip():
            raise ReviewPipelineError("candidate work and implementer identity must not be empty")
        _validate_sha(self.candidate_sha)


@dataclass(frozen=True, slots=True)
class ReviewVerdict:
    candidate_sha: str
    reviewer_id: str
    accepted: bool
    reason: str
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_sha(self.candidate_sha)
        if not self.reviewer_id.strip() or not self.reason.strip():
            raise ReviewPipelineError("reviewer identity and verdict reason must not be empty")
        if len(self.reason) > _MAX_REASON_CHARS:
            raise ReviewPipelineError("review verdict reason exceeds bounded evidence limit")
        _validate_evidence_refs(self.evidence_refs)


@dataclass(frozen=True, slots=True)
class CandidateReviewRecord:
    identity: CandidateReviewIdentity
    state: ReviewState = ReviewState.IMPLEMENTED
    reviewer_id: str | None = None
    verdict: ReviewVerdict | None = None

    def snapshot(self) -> str:
        """Return a deterministic restart-safe representation of the review state."""
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def restore(cls, payload: str) -> CandidateReviewRecord:
        try:
            raw = json.loads(payload)
            identity = CandidateReviewIdentity(**raw["identity"])
            verdict_raw = raw.get("verdict")
            verdict = None
            if verdict_raw is not None:
                verdict_raw["evidence_refs"] = tuple(verdict_raw["evidence_refs"])
                verdict = ReviewVerdict(**verdict_raw)
            record = cls(
                identity=identity,
                state=ReviewState(raw["state"]),
                reviewer_id=raw.get("reviewer_id"),
                verdict=verdict,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReviewPipelineError("review snapshot is invalid") from exc
        record._validate()
        return record

    def require_review(self) -> CandidateReviewRecord:
        self._require_state(ReviewState.IMPLEMENTED)
        return CandidateReviewRecord(self.identity, ReviewState.REVIEW_REQUIRED)

    def queue_qa(self, *, reviewer_id: str) -> CandidateReviewRecord:
        self._require_state(ReviewState.REVIEW_REQUIRED)
        self._validate_reviewer(reviewer_id)
        return CandidateReviewRecord(self.identity, ReviewState.QA_PENDING, reviewer_id=reviewer_id)

    def start_qa(self, *, reviewer_id: str) -> CandidateReviewRecord:
        self._require_state(ReviewState.QA_PENDING)
        self._require_assigned_reviewer(reviewer_id)
        return CandidateReviewRecord(self.identity, ReviewState.QA_RUNNING, reviewer_id=reviewer_id)

    def record_verdict(
        self,
        *,
        candidate_sha: str,
        reviewer_id: str,
        accepted: bool,
        reason: str,
        evidence_refs: tuple[str, ...],
    ) -> CandidateReviewRecord:
        self._require_state(ReviewState.QA_RUNNING)
        self._require_candidate(candidate_sha)
        self._require_assigned_reviewer(reviewer_id)
        verdict = ReviewVerdict(candidate_sha, reviewer_id, accepted, reason, evidence_refs)
        return CandidateReviewRecord(
            self.identity,
            ReviewState.PASS if accepted else ReviewState.FAIL,
            reviewer_id=reviewer_id,
            verdict=verdict,
        )

    def mark_merge_ready(self, *, candidate_sha: str) -> CandidateReviewRecord:
        self._require_state(ReviewState.PASS)
        self._require_candidate(candidate_sha)
        return CandidateReviewRecord(
            self.identity,
            ReviewState.MERGE_READY,
            reviewer_id=self.reviewer_id,
            verdict=self.verdict,
        )

    def require_fix(self, *, candidate_sha: str) -> CandidateReviewRecord:
        self._require_state(ReviewState.FAIL)
        self._require_candidate(candidate_sha)
        return CandidateReviewRecord(
            self.identity,
            ReviewState.FIX_REQUIRED,
            reviewer_id=self.reviewer_id,
            verdict=self.verdict,
        )

    def successor(self, *, candidate_sha: str, implementer_id: str) -> CandidateReviewRecord:
        """Start a fresh review lifecycle; verdicts never transfer to a successor head."""
        _validate_sha(candidate_sha)
        if candidate_sha == self.identity.candidate_sha:
            raise ReviewPipelineError("successor candidate SHA must change")
        return CandidateReviewRecord(
            CandidateReviewIdentity(self.identity.work_id, candidate_sha, implementer_id)
        )

    def _validate(self) -> None:
        if self.reviewer_id is not None:
            self._validate_reviewer(self.reviewer_id)
        pre_verdict = {
            ReviewState.IMPLEMENTED,
            ReviewState.REVIEW_REQUIRED,
            ReviewState.QA_PENDING,
            ReviewState.QA_RUNNING,
        }
        if self.state in pre_verdict and self.verdict is not None:
            raise ReviewPipelineError("pre-verdict review state cannot contain a verdict")
        if self.state in {ReviewState.IMPLEMENTED, ReviewState.REVIEW_REQUIRED}:
            if self.reviewer_id is not None:
                raise ReviewPipelineError("reviewer cannot be bound before QA is queued")
        elif self.reviewer_id is None:
            raise ReviewPipelineError("QA state requires an independent reviewer")
        terminal = {
            ReviewState.PASS,
            ReviewState.FAIL,
            ReviewState.FIX_REQUIRED,
            ReviewState.MERGE_READY,
        }
        if self.state in terminal:
            if self.verdict is None:
                raise ReviewPipelineError("terminal review state requires exact verdict evidence")
            self._require_candidate(self.verdict.candidate_sha)
            self._require_assigned_reviewer(self.verdict.reviewer_id)
            if self.state in {ReviewState.PASS, ReviewState.MERGE_READY} and not self.verdict.accepted:
                raise ReviewPipelineError("passing review state requires accepted verdict")
            if self.state in {ReviewState.FAIL, ReviewState.FIX_REQUIRED} and self.verdict.accepted:
                raise ReviewPipelineError("failing review state requires rejected verdict")

    def _require_state(self, expected: ReviewState) -> None:
        self._validate()
        if self.state is not expected:
            raise ReviewPipelineError(
                f"review transition requires {expected.value}, got {self.state.value}"
            )

    def _require_candidate(self, candidate_sha: str) -> None:
        _validate_sha(candidate_sha)
        if candidate_sha != self.identity.candidate_sha:
            raise StaleCandidateReviewError(
                "review evidence does not match exact current candidate SHA"
            )

    def _validate_reviewer(self, reviewer_id: str) -> None:
        if not reviewer_id.strip():
            raise ReviewPipelineError("reviewer identity must not be empty")
        if reviewer_id == self.identity.implementer_id:
            raise ReviewPipelineError("candidate implementer cannot independently review own work")

    def _require_assigned_reviewer(self, reviewer_id: str) -> None:
        self._validate_reviewer(reviewer_id)
        if reviewer_id != self.reviewer_id:
            raise ReviewPipelineError("reviewer identity does not match queued independent reviewer")


def _validate_sha(value: str) -> None:
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value.casefold()):
        raise ReviewPipelineError("candidate SHA must be a 40-character hexadecimal SHA")


def _validate_evidence_refs(values: tuple[str, ...]) -> None:
    if not values or len(values) > _MAX_EVIDENCE_REFS:
        raise ReviewPipelineError("review verdict requires bounded evidence references")
    if any(not value.strip() or len(value) > _MAX_EVIDENCE_REF_CHARS for value in values):
        raise ReviewPipelineError("review evidence reference exceeds bounded evidence limit")
