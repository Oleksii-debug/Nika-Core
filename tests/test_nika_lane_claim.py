from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from scripts.nika_lane_claim import (
    LaneClaimError,
    claim_from_mapping,
    claims_from_text,
    effective_claims,
    find_collisions,
)


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


def test_valid_claim_is_active() -> None:
    claim = claim_from_mapping(_payload())

    assert claim.lane_id == "W099"
    assert claim.is_active_at(NOW)


@pytest.mark.parametrize(
    "bad_scope",
    [
        "../src/file.py",
        "src/../file.py",
        "/src/file.py",
        r"src\file.py",
        "src/*/file.py",
        ".git/config",
    ],
)
def test_unsafe_scope_fails_closed(bad_scope: str) -> None:
    with pytest.raises(LaneClaimError):
        claim_from_mapping(_payload(scope=[bad_scope]))


def test_prefix_scope_collides_with_owned_descendant() -> None:
    candidate = claim_from_mapping(_payload(scope=["src/nika_core/research/**"]))
    peer = claim_from_mapping(
        _payload(
            lane_id="W100",
            scope=["src/nika_core/research/http.py"],
            created_at="2026-08-26T20:01:00Z",
            expires_at="2026-08-27T20:01:00Z",
        )
    )

    collisions = find_collisions(candidate, [peer], NOW)

    assert len(collisions) == 1
    assert collisions[0].right_lane == "W100"


def test_sibling_prefixes_do_not_collide() -> None:
    candidate = claim_from_mapping(_payload(scope=["src/nika_core/research/**"]))
    peer = claim_from_mapping(
        _payload(
            lane_id="W100",
            scope=["src/nika_core/media/**"],
            created_at="2026-08-26T20:01:00Z",
            expires_at="2026-08-27T20:01:00Z",
        )
    )

    assert find_collisions(candidate, [peer], NOW) == ()


def test_exact_scope_only_collides_with_same_exact_path() -> None:
    candidate = claim_from_mapping(_payload(scope=["docs/ROADMAP.md"]))
    other = claim_from_mapping(
        _payload(
            lane_id="W100",
            scope=["docs/ROADMAP.md"],
            created_at="2026-08-26T20:01:00Z",
            expires_at="2026-08-27T20:01:00Z",
        )
    )

    assert find_collisions(candidate, [other], NOW)


def test_latest_release_record_deactivates_lane() -> None:
    active = claim_from_mapping(_payload())
    released = claim_from_mapping(
        _payload(
            status="released",
            created_at="2026-08-26T20:30:00Z",
            expires_at="2026-08-27T20:30:00Z",
        )
    )

    assert effective_claims([active, released], NOW) == ()


def test_expired_claim_does_not_block_new_lane() -> None:
    candidate = claim_from_mapping(_payload())
    expired = claim_from_mapping(
        _payload(
            lane_id="W100",
            scope=["src/nika_core/example/file.py"],
            created_at="2026-08-25T19:00:00Z",
            expires_at="2026-08-26T19:00:00Z",
        )
    )

    assert find_collisions(candidate, [expired], NOW) == ()


def test_same_lane_conflicting_active_record_fails_as_collision() -> None:
    candidate = claim_from_mapping(_payload())
    peer_payload = _payload(scope=["docs/ROADMAP.md"])
    peer = claim_from_mapping(peer_payload)

    collisions = find_collisions(candidate, [peer], NOW)

    assert collisions
    assert collisions[0].left_scope == "<lane-id>"


def test_same_time_conflicting_records_fail_closed() -> None:
    first = claim_from_mapping(_payload())
    second = claim_from_mapping(_payload(scope=["docs/ROADMAP.md"]))

    with pytest.raises(LaneClaimError, match="conflicting same-time"):
        effective_claims([first, second], NOW)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("start_main", "ABC"),
        ("branch", "work/bad..branch"),
        ("lane_id", "w099"),
        ("status", "paused"),
        ("pr", True),
    ],
)
def test_invalid_identity_fields_fail_closed(field: str, value: object) -> None:
    payload = _payload()
    payload[field] = value

    with pytest.raises(LaneClaimError):
        claim_from_mapping(payload)


def test_lease_cannot_exceed_24_hours() -> None:
    with pytest.raises(LaneClaimError, match="24 hours"):
        claim_from_mapping(
            _payload(expires_at="2026-08-27T20:00:01Z")
        )


def test_unknown_fields_fail_closed() -> None:
    payload = _payload()
    payload["surprise"] = "value"

    with pytest.raises(LaneClaimError, match="unknown claim fields"):
        claim_from_mapping(payload)


def test_issue_comment_prefix_can_be_extracted() -> None:
    payload = _payload()
    text = (
        "human-readable heading\n"
        f"NIKA_LANE_CLAIM_V1={json.dumps(payload, separators=(',', ':'))}\n"
        "human-readable footer\n"
    )

    claims = claims_from_text(text)

    assert len(claims) == 1
    assert claims[0].lane_id == "W099"


def test_json_list_can_represent_ledger_export() -> None:
    text = json.dumps([_payload(), _payload(lane_id="W100", scope=["docs/**"])])

    claims = claims_from_text(text)

    assert [claim.lane_id for claim in claims] == ["W099", "W100"]


def test_non_object_prefixed_payload_fails_closed() -> None:
    with pytest.raises(LaneClaimError, match="must be a JSON object"):
        claims_from_text('NIKA_LANE_CLAIM_V1=["not", "an", "object"]')


def test_naive_now_is_rejected() -> None:
    claim = claim_from_mapping(_payload())

    with pytest.raises(LaneClaimError, match="timezone-aware"):
        effective_claims([claim], datetime(2026, 8, 26, 21, 0))


def test_scope_entries_must_be_unique() -> None:
    with pytest.raises(LaneClaimError, match="unique"):
        claim_from_mapping(
            _payload(scope=["docs/ROADMAP.md", "docs/ROADMAP.md"])
        )
