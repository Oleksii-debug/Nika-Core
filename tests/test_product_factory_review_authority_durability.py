from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from nika_core.qa.review_state import (
    CandidateReviewIdentity,
    CandidateReviewRecord,
    ReviewPipelineError,
)

SHA_A = "a" * 40


@dataclass(frozen=True)
class _Authority:
    candidate_sha: str = SHA_A
    reviewer_id: str = "qa-1"
    authority_ref: str = "authority:qa-assignment-1"
    independent_review_authorized: bool = True


@dataclass(frozen=True)
class _MalformedAuthority:
    candidate_sha: str = SHA_A
    reviewer_id: str = "qa-1"
    authority_ref: str = "authority:qa-assignment-1"
    independent_review_authorized: object = "false"


@dataclass(frozen=True)
class _Clearance:
    candidate_sha: str = SHA_A
    merge_clearance: bool = True


def _pending_review() -> CandidateReviewRecord:
    candidate = CandidateReviewRecord(
        CandidateReviewIdentity(
            work_id="work-1",
            candidate_sha=SHA_A,
            implementer_id="dev-1",
        )
    )
    return candidate.require_review().queue_qa(authority=_Authority())


def _passed_review() -> CandidateReviewRecord:
    return _pending_review().start_qa(reviewer_id="qa-1").record_verdict(
        candidate_sha=SHA_A,
        reviewer_id="qa-1",
        accepted=True,
        reason="independent exact-head review passed",
        evidence_refs=("review:1",),
    )


def test_snapshot_persists_explicit_independent_review_authorization() -> None:
    payload = _pending_review().snapshot()
    authority = json.loads(payload)["reviewer_authority"]

    assert authority["candidate_sha"] == SHA_A
    assert authority["reviewer_id"] == "qa-1"
    assert authority["authority_ref"] == "authority:qa-assignment-1"
    assert authority["independent_review_authorized"] is True


def test_queue_qa_rejects_truthy_non_boolean_reviewer_authority() -> None:
    candidate = CandidateReviewRecord(
        CandidateReviewIdentity(
            work_id="work-1",
            candidate_sha=SHA_A,
            implementer_id="dev-1",
        )
    ).require_review()

    with pytest.raises(
        ReviewPipelineError,
        match="trusted reviewer authority did not authorize independent review",
    ):
        candidate.queue_qa(authority=_MalformedAuthority())


def test_restore_rejects_tampered_independent_review_authorization() -> None:
    payload = _pending_review().snapshot().replace(
        '"independent_review_authorized":true',
        '"independent_review_authorized":false',
    )

    with pytest.raises(ReviewPipelineError, match="review snapshot is invalid"):
        CandidateReviewRecord.restore(payload)


def test_restore_rejects_snapshot_missing_authorization_decision() -> None:
    payload = _pending_review().snapshot().replace(
        ',"independent_review_authorized":true',
        "",
    )

    with pytest.raises(ReviewPipelineError, match="review snapshot is invalid"):
        CandidateReviewRecord.restore(payload)


def test_restore_rejects_forged_merge_ready_without_exact_head_clearance() -> None:
    raw = json.loads(_passed_review().snapshot())
    raw["state"] = "merge_ready"

    with pytest.raises(ReviewPipelineError, match="review snapshot is invalid"):
        CandidateReviewRecord.restore(json.dumps(raw))


def test_verified_merge_ready_is_restart_safe_with_same_head_clearance() -> None:
    merge_ready = _passed_review().mark_merge_ready(
        candidate_sha=SHA_A,
        verification=_Clearance(),
    )

    restored = CandidateReviewRecord.restore(
        merge_ready.snapshot(),
        verification=_Clearance(),
    )

    assert restored.state.value == "merge_ready"


def test_restore_rejects_truthy_non_boolean_verdict_acceptance() -> None:
    raw = json.loads(_passed_review().snapshot())
    raw["verdict"]["accepted"] = "false"

    with pytest.raises(ReviewPipelineError, match="review snapshot is invalid"):
        CandidateReviewRecord.restore(json.dumps(raw))


def test_durable_review_evidence_minimizes_credential_material_across_restart() -> None:
    raw_authority = "https://reviewer:password@example.invalid/authority"
    raw_reason = "Authorization: Bearer top-secret-review-token"
    raw_refs = (
        "https://example.invalid/callback?password=hunter2",
        "https://example.invalid/callback#access_token=fragment-secret",
        "Cookie: session=raw-cookie-value",
        "https%253A%252F%252Fexample.invalid%252Fcb%253Ftoken%253Dnested-secret",
    )
    candidate = CandidateReviewRecord(
        CandidateReviewIdentity("work-1", SHA_A, "dev-1")
    ).require_review()
    pending = candidate.queue_qa(
        authority=_Authority(authority_ref=raw_authority)
    ).start_qa(reviewer_id="qa-1")
    passed = pending.record_verdict(
        candidate_sha=SHA_A,
        reviewer_id="qa-1",
        accepted=True,
        reason=raw_reason,
        evidence_refs=raw_refs,
    )

    payload = passed.snapshot()
    for secret in (raw_authority, raw_reason, *raw_refs):
        assert secret not in payload
    assert payload.count("evidence-sha256:") == 6

    restored = CandidateReviewRecord.restore(payload)
    restarted_payload = restored.snapshot()
    assert restarted_payload == payload
    for secret in (raw_authority, raw_reason, *raw_refs):
        assert secret not in restarted_payload
