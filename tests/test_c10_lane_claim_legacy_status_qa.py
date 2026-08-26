from __future__ import annotations

import datetime as dt

import pytest

from scripts import nika_lane_claim as claims

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 26, 20, 45, tzinfo=UTC)


def _comment(
    comment_id: int,
    *,
    created_at: str,
    status: str,
    path: str,
) -> dict[str, object]:
    return {
        "id": comment_id,
        "created_at": created_at,
        "user": {"login": "Oleksii-debug"},
        "body": "\n".join(
            (
                "[NIKA-C10:LIVE-LANE]",
                f"RUN_ID=live-run-{comment_id}",
                f"STATUS={status}",
                f"OWNERSHIP_PATHS={path}",
            )
        ),
    }


@pytest.mark.parametrize(
    "status",
    (
        "SOURCE_COMPLETE_WAITING_CI_AND_BRIDGE_DEPENDENCY",
        "BLOCKED_WAITING_EXACT_QA_AND_REPAIRS",
    ),
)
def test_current_live_nonterminal_c10_statuses_remain_active_claims(status: str) -> None:
    """Current Issue #1 nonterminal vocabulary must not silently become an unlocked lane."""
    parsed = claims.parse_comment(
        _comment(
            100,
            created_at="2026-08-26T20:00:00Z",
            status=status,
            path="src/nika_core/ui/web/app.js",
        ),
        legacy_ttl_minutes=720,
    )

    assert isinstance(parsed, claims.LaneClaim), (
        "A live nonterminal NIKA-C10 marker was ignored. A check using only the lane helper "
        "can therefore report no conflict while Issue #1 still records active ownership."
    )


def test_current_live_markdown_heading_c10_marker_remains_active_claim() -> None:
    """Live ENG08 uses '# [NIKA-C10:...]'; Markdown decoration must not unlock that lane."""
    comment = {
        "id": 150,
        "created_at": "2026-08-26T20:10:00Z",
        "user": {"login": "Oleksii-debug"},
        "body": "\n".join(
            (
                "# [NIKA-C10:ENG08]",
                "WORKER_ID: ENG08",
                "RUN_ID: C10-ENG08-S100-CONSOLIDATE-20260826T1815+0300",
                "STATUS: IN_PROGRESS",
                "OWNERSHIP_PATHS: src/nika_core/product_factory_release.py",
            )
        ),
    }

    parsed = claims.parse_comment(comment, legacy_ttl_minutes=720)

    assert isinstance(parsed, claims.LaneClaim), (
        "A current live Markdown-heading NIKA-C10 marker was ignored even though its status is "
        "IN_PROGRESS. The legacy compatibility parser must not unlock an occupied lane because "
        "the marker is rendered as a Markdown heading."
    )


def test_ignored_live_nonterminal_status_cannot_lose_to_new_overlapping_claim() -> None:
    """An older live legacy owner must win over a later overlapping v1 claimant."""
    legacy = _comment(
        200,
        created_at="2026-08-26T20:00:00Z",
        status="SOURCE_COMPLETE_WAITING_CI_AND_BRIDGE_DEPENDENCY",
        path="src/nika_core/ui/web/app.js",
    )
    contender = {
        "id": 201,
        "created_at": "2026-08-26T20:01:00Z",
        "user": {"login": "Oleksii-debug"},
        "body": "\n".join(
            (
                "[NIKA-LANE-CLAIM v1]",
                "ACTION=claim",
                "RUN_ID=contender-run",
                "CLAIM_KEY=other-lane",
                "LEASE_MINUTES=360",
                "OWNERSHIP_PATHS=src/nika_core/ui/web/app.js",
            )
        ),
    }

    winners = claims.resolve_winners((legacy, contender), now=NOW, legacy_ttl_minutes=720)

    assert [winner.comment_id for winner in winners] == [200], (
        "The later v1 contender was allowed to win because the older live legacy owner status "
        "was not recognized as active."
    )
