from __future__ import annotations

import datetime as dt

import pytest

from scripts import nika_lane_claim as claims

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 26, 20, 0, tzinfo=UTC)


def _comment(
    comment_id: int,
    created_at: str,
    actor: str,
    body: str,
) -> dict[str, object]:
    return {
        "id": comment_id,
        "created_at": created_at,
        "user": {"login": actor},
        "body": body,
    }


def _claim(
    comment_id: int,
    *,
    actor: str = "worker-a",
    run_id: str = "run-a",
    key: str = "lane-a",
    path: str = "src/nika_core/a.py",
    created_at: str = "2026-08-26T19:00:00Z",
    lease_minutes: int = 360,
) -> dict[str, object]:
    return _comment(
        comment_id,
        created_at,
        actor,
        "\n".join(
            (
                "[NIKA-LANE-CLAIM v1]",
                "ACTION=claim",
                f"RUN_ID={run_id}",
                f"CLAIM_KEY={key}",
                f"LEASE_MINUTES={lease_minutes}",
                f"OWNERSHIP_PATHS={path}",
            )
        ),
    )


def test_parent_child_and_windows_paths_overlap() -> None:
    assert claims.paths_overlap(("src/nika_core",), ("src/nika_core/ui/app.py",))
    assert claims.paths_overlap((r"src\nika_core\ui",), ("src/nika_core/ui/app.py",))
    assert not claims.paths_overlap(("tests/a.py",), ("tests/b.py",))


def test_unsafe_parent_path_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsafe ownership path"):
        claims.paths_overlap(("../src",), ("src",))


def test_v1_claim_is_bounded_and_parsed() -> None:
    parsed = claims.parse_comment(_claim(10), legacy_ttl_minutes=720)
    assert isinstance(parsed, claims.LaneClaim)
    assert parsed.claim_key == "lane-a"
    assert parsed.paths == ("src/nika_core/a.py",)
    assert parsed.expires_at == dt.datetime(2026, 8, 27, 1, 0, tzinfo=UTC)


def test_legacy_c10_active_claim_is_reused() -> None:
    comment = _comment(
        11,
        "2026-08-26T19:30:00Z",
        "legacy-worker",
        "\n".join(
            (
                "[NIKA-C10:ENG02]",
                "RUN_ID=C10-ENG02-20260826T2301+0300",
                "STATUS=IN_PROGRESS",
                "OWNERSHIP_PATHS=src/nika_core/toolsmith;tests/test_toolsmith.py",
            )
        ),
    )
    parsed = claims.parse_comment(comment, legacy_ttl_minutes=720)
    assert isinstance(parsed, claims.LaneClaim)
    assert parsed.source == "legacy-c10"
    assert parsed.claim_key == "ENG02"


def test_terminal_legacy_marker_is_not_an_active_claim() -> None:
    comment = _comment(
        12,
        "2026-08-26T19:30:00Z",
        "legacy-worker",
        "[NIKA-C10:ENG02]\nRUN_ID=run-2\nSTATUS=DONE\nOWNERSHIP_PATHS=src/nika_core/a.py",
    )
    assert claims.parse_comment(comment, legacy_ttl_minutes=720) is None


def test_earliest_overlapping_claim_wins_deterministically() -> None:
    first = _claim(20, actor="a", run_id="a", key="one", path="src/nika_core")
    second = _claim(
        21,
        actor="b",
        run_id="b",
        key="two",
        path="src/nika_core/ui/app.py",
        created_at="2026-08-26T19:00:01Z",
    )
    winners = claims.resolve_winners((second, first), now=NOW)
    assert [winner.comment_id for winner in winners] == [20]


def test_same_claim_key_conflicts_even_when_paths_are_disjoint() -> None:
    first = _claim(30, actor="a", run_id="a", key="same", path="src/a.py")
    second = _claim(
        31,
        actor="b",
        run_id="b",
        key="same",
        path="tests/b.py",
        created_at="2026-08-26T19:00:01Z",
    )
    winners = claims.resolve_winners((first, second), now=NOW)
    assert [winner.comment_id for winner in winners] == [30]


def test_release_after_claim_removes_only_matching_actor_run_and_key() -> None:
    claim = _claim(40, actor="owner", run_id="r1", key="lane", path="src/a.py")
    unrelated = _claim(
        41,
        actor="other",
        run_id="r2",
        key="other",
        path="tests/b.py",
        created_at="2026-08-26T19:00:01Z",
    )
    release = _comment(
        42,
        "2026-08-26T19:10:00Z",
        "owner",
        "[NIKA-LANE-CLAIM v1]\nACTION=release\nRUN_ID=r1\nCLAIM_KEY=lane",
    )
    winners = claims.resolve_winners((claim, unrelated, release), now=NOW)
    assert [winner.comment_id for winner in winners] == [41]


def test_release_before_claim_does_not_cancel_later_claim() -> None:
    release = _comment(
        50,
        "2026-08-26T18:00:00Z",
        "owner",
        "[NIKA-LANE-CLAIM v1]\nACTION=release\nRUN_ID=r1\nCLAIM_KEY=lane",
    )
    claim = _claim(
        51,
        actor="owner",
        run_id="r1",
        key="lane",
        path="src/a.py",
        created_at="2026-08-26T19:00:00Z",
    )
    winners = claims.resolve_winners((release, claim), now=NOW)
    assert [winner.comment_id for winner in winners] == [51]


def test_expired_claim_does_not_block() -> None:
    expired = _claim(
        60,
        path="src/a.py",
        created_at="2026-08-26T18:00:00Z",
        lease_minutes=30,
    )
    assert claims.resolve_winners((expired,), now=NOW) == ()


def test_render_claim_normalizes_paths_and_declares_advisory_authority() -> None:
    body = claims.render_claim(
        run_id="run-1",
        claim_key="coordination/lease",
        worker_id="worker-1",
        lease_minutes=60,
        paths=(r"scripts\nika_lane_claim.py", "tests/test_nika_lane_claim.py"),
    )
    assert "OWNERSHIP_PATHS=scripts/nika_lane_claim.py;tests/test_nika_lane_claim.py" in body
    assert "AUTHORITY=advisory-coordination-only" in body


def test_render_claim_rejects_multiline_worker_id() -> None:
    with pytest.raises(claims.ClaimError, match="single non-empty line"):
        claims.render_claim(
            run_id="run-1",
            claim_key="lane",
            worker_id="worker\nforged",
            lease_minutes=60,
            paths=("scripts/a.py",),
        )
