#!/usr/bin/env python3
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable, Sequence
from typing import Any

UTC = dt.timezone.utc
MARKER_RE = re.compile(r"^\[(NIKA-LANE-CLAIM v1|NIKA-C10:[^\]]+)\]\s*$", re.MULTILINE)
FIELD_RE = re.compile(r"^([A-Z0-9_]+)\s*[:=]\s*(.*?)\s*$", re.MULTILINE)
SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9._:/+\-]{1,120}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ACTIVE_LEGACY_STATUSES = {
    "IN_PROGRESS",
    "BLOCKED",
    "WAITING",
    "WAITING_CI",
    "WAITING_AUDIT",
    "WAITING_EXACT_CI_AUDIT",
}


class ClaimError(RuntimeError):
    """Raised when the advisory GitHub claim channel cannot be used safely."""


@dataclasses.dataclass(frozen=True, slots=True)
class LaneClaim:
    comment_id: int
    actor: str
    run_id: str
    claim_key: str
    paths: tuple[str, ...]
    created_at: dt.datetime
    expires_at: dt.datetime
    source: str


@dataclasses.dataclass(frozen=True, slots=True)
class LaneRelease:
    comment_id: int
    actor: str
    run_id: str
    claim_key: str
    created_at: dt.datetime


def _utc(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _normalize_path(path: str) -> str:
    value = path.strip().replace("\\", "/").strip("/")
    parts = [part for part in value.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"unsafe ownership path: {path!r}")
    return "/".join(parts)


def _split_paths(value: str) -> tuple[str, ...]:
    if not value.strip():
        return ()
    normalized = (_normalize_path(item) for item in value.split(";") if item.strip())
    return tuple(dict.fromkeys(normalized))


def paths_overlap(left: Sequence[str], right: Sequence[str]) -> bool:
    for raw_left in left:
        first = _normalize_path(raw_left)
        for raw_right in right:
            second = _normalize_path(raw_right)
            if (
                first == second
                or first.startswith(f"{second}/")
                or second.startswith(f"{first}/")
            ):
                return True
    return False


def claims_conflict(left: LaneClaim, right: LaneClaim) -> bool:
    return left.claim_key == right.claim_key or paths_overlap(left.paths, right.paths)


def _fields(body: str) -> dict[str, str]:
    return {key.upper(): value.strip() for key, value in FIELD_RE.findall(body)}


def _actor(comment: dict[str, Any]) -> str:
    user = comment.get("user")
    return str(user.get("login") or "") if isinstance(user, dict) else ""


def parse_comment(
    comment: dict[str, Any],
    *,
    legacy_ttl_minutes: int,
) -> LaneClaim | LaneRelease | None:
    body = str(comment.get("body") or "")
    marker_match = MARKER_RE.search(body)
    if marker_match is None:
        return None

    try:
        comment_id = int(comment["id"])
        created_at = _utc(str(comment["created_at"]))
    except (KeyError, TypeError, ValueError):
        return None
    actor = _actor(comment)
    if not actor:
        return None

    marker = marker_match.group(1)
    fields = _fields(body)
    if marker == "NIKA-LANE-CLAIM v1":
        action = fields.get("ACTION", "").lower()
        run_id = fields.get("RUN_ID", "")
        claim_key = fields.get("CLAIM_KEY", "")
        if not SAFE_KEY_RE.fullmatch(run_id) or not SAFE_KEY_RE.fullmatch(claim_key):
            return None
        if action == "release":
            return LaneRelease(comment_id, actor, run_id, claim_key, created_at)
        if action != "claim":
            return None
        try:
            lease_minutes = int(fields.get("LEASE_MINUTES", "360"))
            paths = _split_paths(fields.get("OWNERSHIP_PATHS", ""))
        except ValueError:
            return None
        if not 5 <= lease_minutes <= 720 or not paths:
            return None
        return LaneClaim(
            comment_id=comment_id,
            actor=actor,
            run_id=run_id,
            claim_key=claim_key,
            paths=paths,
            created_at=created_at,
            expires_at=created_at + dt.timedelta(minutes=lease_minutes),
            source="v1",
        )

    status = fields.get("STATUS", "").upper()
    run_id = fields.get("RUN_ID", "")
    if status not in ACTIVE_LEGACY_STATUSES or not SAFE_KEY_RE.fullmatch(run_id):
        return None
    try:
        paths = _split_paths(fields.get("OWNERSHIP_PATHS", ""))
    except ValueError:
        return None
    if not paths:
        return None
    return LaneClaim(
        comment_id=comment_id,
        actor=actor,
        run_id=run_id,
        claim_key=marker.removeprefix("NIKA-C10:"),
        paths=paths,
        created_at=created_at,
        expires_at=created_at + dt.timedelta(minutes=legacy_ttl_minutes),
        source="legacy-c10",
    )


def resolve_winners(
    comments: Iterable[dict[str, Any]],
    *,
    now: dt.datetime,
    legacy_ttl_minutes: int = 720,
) -> tuple[LaneClaim, ...]:
    parsed = [
        item
        for comment in comments
        if (item := parse_comment(comment, legacy_ttl_minutes=legacy_ttl_minutes)) is not None
    ]
    releases = [item for item in parsed if isinstance(item, LaneRelease)]
    claims = [item for item in parsed if isinstance(item, LaneClaim)]
    eligible = []
    for claim in claims:
        released = any(
            release.actor == claim.actor
            and release.run_id == claim.run_id
            and release.claim_key == claim.claim_key
            and (release.created_at, release.comment_id) > (claim.created_at, claim.comment_id)
            for release in releases
        )
        if claim.expires_at > now and not released:
            eligible.append(claim)

    winners: list[LaneClaim] = []
    for claim in sorted(eligible, key=lambda item: (item.created_at, item.comment_id)):
        if any(claims_conflict(claim, winner) for winner in winners):
            continue
        winners.append(claim)
    return tuple(winners)


def _validate_repo(repo: str) -> str:
    if not REPO_RE.fullmatch(repo):
        raise ClaimError("repository must be in owner/name form")
    return repo


def _request_json(
    method: str,
    url: str,
    *,
    token: str | None,
    payload: dict[str, Any] | None = None,
) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "nika-core-lane-claim/1",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ClaimError(f"GitHub {method} failed with HTTP {exc.code}") from None
    except (OSError, ValueError) as exc:
        raise ClaimError(f"GitHub {method} failed: {type(exc).__name__}") from None


def fetch_issue_comments(repo: str, issue: int, *, token: str | None) -> list[dict[str, Any]]:
    repo = _validate_repo(repo)
    if issue < 1:
        raise ClaimError("issue number must be positive")
    comments: list[dict[str, Any]] = []
    page = 1
    while True:
        url = (
            f"https://api.github.com/repos/{repo}/issues/{issue}/comments"
            f"?per_page=100&page={page}"
        )
        batch = _request_json("GET", url, token=token)
        if not isinstance(batch, list):
            raise ClaimError("GitHub comments response was not a list")
        comments.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 100:
            return comments
        page += 1
        if page > 50:
            raise ClaimError("GitHub comments pagination exceeded safety bound")


def post_issue_comment(
    repo: str,
    issue: int,
    *,
    token: str,
    body: str,
) -> dict[str, Any]:
    repo = _validate_repo(repo)
    url = f"https://api.github.com/repos/{repo}/issues/{issue}/comments"
    result = _request_json("POST", url, token=token, payload={"body": body})
    if not isinstance(result, dict) or "id" not in result:
        raise ClaimError("GitHub comment creation returned an invalid response")
    return result


def _token() -> str | None:
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")


def _require_token() -> str:
    token = _token()
    if not token:
        raise ClaimError("GH_TOKEN or GITHUB_TOKEN is required for claim/release")
    return token


def render_claim(
    *,
    run_id: str,
    claim_key: str,
    worker_id: str,
    lease_minutes: int,
    paths: Sequence[str],
) -> str:
    if not SAFE_KEY_RE.fullmatch(run_id) or not SAFE_KEY_RE.fullmatch(claim_key):
        raise ClaimError("run-id and claim-key must use safe identifier characters")
    if not worker_id.strip() or "\n" in worker_id or "\r" in worker_id:
        raise ClaimError("worker-id must be a single non-empty line")
    if not 5 <= lease_minutes <= 720:
        raise ClaimError("lease-minutes must be between 5 and 720")
    normalized = tuple(dict.fromkeys(_normalize_path(path) for path in paths))
    if not normalized:
        raise ClaimError("at least one ownership path is required")
    return "\n".join(
        (
            "[NIKA-LANE-CLAIM v1]",
            "ACTION=claim",
            f"WORKER_ID={worker_id.strip()}",
            f"RUN_ID={run_id}",
            f"CLAIM_KEY={claim_key}",
            f"LEASE_MINUTES={lease_minutes}",
            f"OWNERSHIP_PATHS={';'.join(normalized)}",
            "AUTHORITY=advisory-coordination-only",
        )
    )


def render_release(*, run_id: str, claim_key: str) -> str:
    if not SAFE_KEY_RE.fullmatch(run_id) or not SAFE_KEY_RE.fullmatch(claim_key):
        raise ClaimError("run-id and claim-key must use safe identifier characters")
    return "\n".join(
        (
            "[NIKA-LANE-CLAIM v1]",
            "ACTION=release",
            f"RUN_ID={run_id}",
            f"CLAIM_KEY={claim_key}",
            "AUTHORITY=advisory-coordination-only",
        )
    )


def _claim_dict(claim: LaneClaim) -> dict[str, Any]:
    return {
        "comment_id": claim.comment_id,
        "actor": claim.actor,
        "run_id": claim.run_id,
        "claim_key": claim.claim_key,
        "paths": list(claim.paths),
        "created_at": claim.created_at.isoformat(),
        "expires_at": claim.expires_at.isoformat(),
        "source": claim.source,
    }


def _requested_paths(values: Sequence[str]) -> tuple[str, ...]:
    try:
        return tuple(dict.fromkeys(_normalize_path(value) for value in values))
    except ValueError as exc:
        raise ClaimError(str(exc)) from None


def _conflicts(
    winners: Sequence[LaneClaim],
    *,
    claim_key: str | None,
    paths: Sequence[str],
) -> tuple[LaneClaim, ...]:
    result = []
    for winner in winners:
        same_key = claim_key is not None and winner.claim_key == claim_key
        if same_key or paths_overlap(winner.paths, paths):
            result.append(winner)
    return tuple(result)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", default="Oleksii-debug/Nika-Core")
    parser.add_argument("--issue", type=int, default=1)
    parser.add_argument("--legacy-ttl-minutes", type=int, default=720)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail-closed advisory lane-claim preflight for Nika Core parallel workers."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check")
    _add_common(check)
    check.add_argument("--claim-key")
    check.add_argument("--path", action="append", required=True)

    claim = subparsers.add_parser("claim")
    _add_common(claim)
    claim.add_argument("--worker-id", required=True)
    claim.add_argument("--run-id", required=True)
    claim.add_argument("--claim-key", required=True)
    claim.add_argument("--lease-minutes", type=int, default=360)
    claim.add_argument("--path", action="append", required=True)

    release = subparsers.add_parser("release")
    _add_common(release)
    release.add_argument("--run-id", required=True)
    release.add_argument("--claim-key", required=True)
    return parser


def _load_winners(args: argparse.Namespace, *, token: str | None) -> tuple[LaneClaim, ...]:
    if not 5 <= args.legacy_ttl_minutes <= 1440:
        raise ClaimError("legacy-ttl-minutes must be between 5 and 1440")
    comments = fetch_issue_comments(args.repo, args.issue, token=token)
    return resolve_winners(
        comments,
        now=dt.datetime.now(UTC),
        legacy_ttl_minutes=args.legacy_ttl_minutes,
    )


def run(args: argparse.Namespace) -> int:
    if args.command == "check":
        paths = _requested_paths(args.path)
        winners = _load_winners(args, token=_token())
        conflicts = _conflicts(winners, claim_key=args.claim_key, paths=paths)
        print(json.dumps({"conflicts": [_claim_dict(item) for item in conflicts]}, indent=2))
        return 2 if conflicts else 0

    if args.command == "claim":
        token = _require_token()
        paths = _requested_paths(args.path)
        winners = _load_winners(args, token=token)
        conflicts = _conflicts(winners, claim_key=args.claim_key, paths=paths)
        if conflicts:
            print(json.dumps({"status": "conflict", "claims": [_claim_dict(c) for c in conflicts]}))
            return 2

        body = render_claim(
            run_id=args.run_id,
            claim_key=args.claim_key,
            worker_id=args.worker_id,
            lease_minutes=args.lease_minutes,
            paths=paths,
        )
        posted = post_issue_comment(args.repo, args.issue, token=token, body=body)
        posted_id = int(posted["id"])
        winners = _load_winners(args, token=token)
        winner_ids = {claim.comment_id for claim in winners}
        if posted_id not in winner_ids:
            print(json.dumps({"status": "lost-race", "comment_id": posted_id}))
            return 2
        print(json.dumps({"status": "acquired", "comment_id": posted_id}))
        return 0

    if args.command == "release":
        token = _require_token()
        body = render_release(run_id=args.run_id, claim_key=args.claim_key)
        posted = post_issue_comment(args.repo, args.issue, token=token, body=body)
        print(json.dumps({"status": "released", "comment_id": int(posted["id"])}))
        return 0

    raise ClaimError(f"unsupported command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except ClaimError as exc:
        print(f"claim unavailable: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
