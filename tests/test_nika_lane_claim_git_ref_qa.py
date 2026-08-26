from __future__ import annotations

import subprocess

import pytest

from scripts.nika_lane_claim import LaneClaimError, claim_from_mapping


def _payload(branch: str) -> dict[str, object]:
    return {
        "schema": "nika-lane-claim/v1",
        "lane_id": "QAW099REF",
        "owner": "qa-worker",
        "status": "active",
        "start_main": "1" * 40,
        "branch": branch,
        "scope": ["tests/test_nika_lane_claim_git_ref_qa.py"],
        "created_at": "2026-08-26T20:39:00Z",
        "expires_at": "2026-08-27T20:39:00Z",
        "pr": None,
    }


@pytest.mark.parametrize(
    "branch",
    [
        "-leading-dash",
        "work/.hidden/topic",
    ],
)
def test_lane_claim_rejects_branch_names_rejected_by_git(branch: str) -> None:
    """A valid coordination claim must name a branch Git can actually create."""
    git_check = subprocess.run(
        ["git", "check-ref-format", "--branch", branch],
        capture_output=True,
        check=False,
        text=True,
    )
    assert git_check.returncode != 0, (
        f"QA fixture drift: Git unexpectedly accepts invalid branch {branch!r}"
    )

    with pytest.raises(LaneClaimError):
        claim_from_mapping(_payload(branch))
