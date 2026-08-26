from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts import nika_lane_claim as claims

NOW = datetime(2026, 8, 26, 21, 0, tzinfo=UTC)


def _payload(
    *,
    lane_id: str = "W099",
    status: str = "active",
    scope: list[str] | None = None,
    created_at: str = "2026-08-26T20:00:00Z",
    expires_at: str = "2026-08-27T20:00:00Z",
) -> dict[str, object]:
    return {
        "schema": "nika-lane-claim/v1",
        "lane_id": lane_id,
        "owner": "chatgpt-worker",
        "status": status,
        "start_main": "1" * 40,
        "branch": f"work/{lane_id.lower()}/owned-slice",
        "scope": scope or ["src/nika_core/example/**"],
        "created_at": created_at,
        "expires_at": expires_at,
        "pr": None,
    }


def _write(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize(
    "candidate",
    (
        _payload(
            created_at="2026-08-26T19:00:00Z",
            expires_at="2026-08-26T20:30:00Z",
        ),
        _payload(status="released"),
    ),
)
def test_check_must_not_report_clear_for_inactive_candidate(
    candidate: dict[str, object],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A claim that is expired or released cannot authorize a shared-file write."""
    candidate_path = tmp_path / "candidate.json"
    peer_path = tmp_path / "peer.json"
    _write(candidate_path, candidate)
    _write(
        peer_path,
        _payload(
            lane_id="W100",
            scope=["docs/unrelated.md"],
            created_at="2026-08-26T20:01:00Z",
            expires_at="2026-08-27T20:01:00Z",
        ),
    )

    result = claims.main(
        [
            "check",
            str(candidate_path),
            "--against",
            str(peer_path),
            "--now",
            "2026-08-26T21:00:00Z",
        ]
    )
    output = capsys.readouterr()

    assert result != 0, (
        "check returned success for an inactive candidate. A caller that gates writes only on the "
        "documented exit code can proceed without an active lane claim."
    )
    assert "CLEAR W099" not in output.out


def test_check_must_not_reactivate_lane_closed_by_peer_history(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The check boundary must preserve the permanent-release invariant across input sources."""
    initial = _payload()
    released = _payload(
        status="released",
        created_at="2026-08-26T20:20:00Z",
        expires_at="2026-08-27T20:20:00Z",
    )
    candidate = _payload(
        created_at="2026-08-26T20:40:00Z",
        expires_at="2026-08-27T20:40:00Z",
    )

    combined = [
        claims.claim_from_mapping(initial),
        claims.claim_from_mapping(released),
        claims.claim_from_mapping(candidate),
    ]
    with pytest.raises(claims.LaneClaimError, match="cannot be reactivated"):
        claims.effective_claims(combined, NOW)

    candidate_path = tmp_path / "candidate.json"
    peer_path = tmp_path / "peer-history.json"
    _write(candidate_path, candidate)
    _write(peer_path, [initial, released])

    result = claims.main(
        [
            "check",
            str(candidate_path),
            "--against",
            str(peer_path),
            "--now",
            "2026-08-26T21:00:00Z",
        ]
    )
    output = capsys.readouterr()

    assert result != 0, (
        "check evaluated the candidate separately from the released same-lane history and returned "
        "success, bypassing the protocol rule that a released lane cannot be reactivated."
    )
    assert "CLEAR W099" not in output.out


def test_check_must_reject_distinct_lanes_sharing_one_branch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Different lanes cannot safely share a branch even when their file scopes are disjoint."""
    candidate = _payload(scope=["docs/lane-a.md"])
    peer = _payload(
        lane_id="W100",
        scope=["docs/lane-b.md"],
        created_at="2026-08-26T20:01:00Z",
        expires_at="2026-08-27T20:01:00Z",
    )
    peer["branch"] = candidate["branch"]

    candidate_path = tmp_path / "candidate.json"
    peer_path = tmp_path / "peer.json"
    _write(candidate_path, candidate)
    _write(peer_path, peer)

    result = claims.main(
        [
            "check",
            str(candidate_path),
            "--against",
            str(peer_path),
            "--now",
            "2026-08-26T21:00:00Z",
        ]
    )
    output = capsys.readouterr()

    assert result != 0, (
        "check returned success for two different lane IDs that name the exact same Git branch. "
        "The protocol requires one unique feature branch per lane so unrelated work cannot stack."
    )
    assert "CLEAR W099" not in output.out
