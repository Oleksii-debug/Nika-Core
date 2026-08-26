from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable, Mapping, Sequence

CLAIM_PREFIX = "NIKA_LANE_CLAIM_V1="
SCHEMA = "nika-lane-claim/v1"
MAX_LEASE = timedelta(hours=24)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_LANE_RE = re.compile(r"^[A-Z][A-Z0-9_-]{1,31}$")
_ALLOWED_KEYS = {
    "schema",
    "lane_id",
    "owner",
    "status",
    "start_main",
    "branch",
    "scope",
    "created_at",
    "expires_at",
    "pr",
}
_STATUSES = {"active", "released"}


class LaneClaimError(ValueError):
    """Raised when a lane-claim payload is malformed or ambiguous."""


@dataclass(frozen=True, slots=True)
class Scope:
    value: str
    is_prefix: bool

    @property
    def root(self) -> str:
        if self.is_prefix:
            return self.value[:-3]
        return self.value


@dataclass(frozen=True, slots=True)
class LaneClaim:
    lane_id: str
    owner: str
    status: str
    start_main: str
    branch: str
    scope: tuple[Scope, ...]
    created_at: datetime
    expires_at: datetime
    pr: int | None = None

    @property
    def is_active_status(self) -> bool:
        return self.status == "active"

    def is_active_at(self, now: datetime) -> bool:
        return self.is_active_status and now < self.expires_at


@dataclass(frozen=True, slots=True)
class Collision:
    left_lane: str
    right_lane: str
    left_scope: str
    right_scope: str


def _require_exact_str(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise LaneClaimError(f"{field} must be a string")
    if not value or value != value.strip():
        raise LaneClaimError(f"{field} must be non-empty canonical text")
    return value


def _parse_timestamp(value: object, field: str) -> datetime:
    text = _require_exact_str(value, field)
    if not text.endswith("Z"):
        raise LaneClaimError(f"{field} must use canonical UTC Z form")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise LaneClaimError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise LaneClaimError(f"{field} must be UTC")
    return parsed.astimezone(UTC)


def _parse_now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    return _parse_timestamp(value, "--now")


def _validate_branch(value: object) -> str:
    branch = _require_exact_str(value, "branch")
    if len(branch) > 200:
        raise LaneClaimError("branch is too long")
    if branch.startswith(("/", ".")) or branch.endswith(("/", ".")):
        raise LaneClaimError("branch must not start or end with '/' or '.'")
    if "//" in branch or ".." in branch or "@{" in branch:
        raise LaneClaimError("branch contains a forbidden ref sequence")
    if any(char in branch for char in "\\ ~^:?*["):
        raise LaneClaimError("branch contains a forbidden ref character")
    if any(ord(char) < 32 or ord(char) == 127 for char in branch):
        raise LaneClaimError("branch contains a control character")
    if any(part.endswith(".lock") or part in {"", ".", ".."} for part in branch.split("/")):
        raise LaneClaimError("branch contains an invalid ref component")
    return branch


def _validate_scope(value: object) -> Scope:
    text = _require_exact_str(value, "scope item")
    is_prefix = text.endswith("/**")
    raw = text[:-3] if is_prefix else text

    if "*" in raw:
        raise LaneClaimError("scope wildcard is allowed only as trailing '/**'")
    if "\\" in raw:
        raise LaneClaimError("scope must use repository POSIX separators")
    if raw.startswith("/") or raw.endswith("/") or "//" in raw:
        raise LaneClaimError("scope must be a canonical repository-relative path")
    parts = raw.split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise LaneClaimError("scope must not contain empty, '.' or '..' components")
    if parts[0] == ".git":
        raise LaneClaimError("scope may not claim Git metadata")
    return Scope(value=text, is_prefix=is_prefix)


def claim_from_mapping(payload: Mapping[str, object]) -> LaneClaim:
    unknown = set(payload) - _ALLOWED_KEYS
    missing = _ALLOWED_KEYS - {"pr"} - set(payload)
    if unknown:
        raise LaneClaimError(f"unknown claim fields: {', '.join(sorted(unknown))}")
    if missing:
        raise LaneClaimError(f"missing claim fields: {', '.join(sorted(missing))}")

    schema = _require_exact_str(payload["schema"], "schema")
    if schema != SCHEMA:
        raise LaneClaimError(f"unsupported schema: {schema}")

    lane_id = _require_exact_str(payload["lane_id"], "lane_id")
    if not _LANE_RE.fullmatch(lane_id):
        raise LaneClaimError("lane_id must match [A-Z][A-Z0-9_-]{1,31}")

    owner = _require_exact_str(payload["owner"], "owner")
    if len(owner) > 128:
        raise LaneClaimError("owner is too long")

    status = _require_exact_str(payload["status"], "status")
    if status not in _STATUSES:
        raise LaneClaimError("status must be 'active' or 'released'")

    start_main = _require_exact_str(payload["start_main"], "start_main")
    if not _SHA_RE.fullmatch(start_main):
        raise LaneClaimError("start_main must be a lowercase 40-hex commit SHA")

    branch = _validate_branch(payload["branch"])

    raw_scope = payload["scope"]
    if not isinstance(raw_scope, list):
        raise LaneClaimError("scope must be a JSON array")
    if not raw_scope:
        raise LaneClaimError("scope must not be empty")
    scopes = tuple(_validate_scope(item) for item in raw_scope)
    scope_names = [item.value for item in scopes]
    if len(scope_names) != len(set(scope_names)):
        raise LaneClaimError("scope entries must be unique")

    created_at = _parse_timestamp(payload["created_at"], "created_at")
    expires_at = _parse_timestamp(payload["expires_at"], "expires_at")
    if expires_at <= created_at:
        raise LaneClaimError("expires_at must be later than created_at")
    if expires_at - created_at > MAX_LEASE:
        raise LaneClaimError("lease duration must not exceed 24 hours")

    raw_pr = payload.get("pr")
    if raw_pr is None:
        pr = None
    elif type(raw_pr) is not int or raw_pr < 1:
        raise LaneClaimError("pr must be a positive integer or null")
    else:
        pr = raw_pr

    return LaneClaim(
        lane_id=lane_id,
        owner=owner,
        status=status,
        start_main=start_main,
        branch=branch,
        scope=scopes,
        created_at=created_at,
        expires_at=expires_at,
        pr=pr,
    )


def _payloads_from_text(text: str) -> list[Mapping[str, object]]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        if not all(isinstance(item, dict) for item in parsed):
            raise LaneClaimError("JSON claim lists may contain objects only")
        return parsed

    payloads: list[Mapping[str, object]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(CLAIM_PREFIX):
            continue
        encoded = stripped[len(CLAIM_PREFIX) :]
        try:
            item = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise LaneClaimError("invalid JSON after NIKA_LANE_CLAIM_V1=") from exc
        if not isinstance(item, dict):
            raise LaneClaimError("prefixed claim payload must be a JSON object")
        payloads.append(item)
    if not payloads:
        raise LaneClaimError("no lane claim payload found")
    return payloads


def claims_from_text(text: str) -> tuple[LaneClaim, ...]:
    return tuple(claim_from_mapping(item) for item in _payloads_from_text(text))


def load_claims(path: Path) -> tuple[LaneClaim, ...]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LaneClaimError(f"cannot read {path}: {exc}") from exc
    return claims_from_text(text)


def _scope_overlaps(left: Scope, right: Scope) -> bool:
    if not left.is_prefix and not right.is_prefix:
        return left.root == right.root
    if left.is_prefix and right.is_prefix:
        return (
            left.root == right.root
            or left.root.startswith(right.root + "/")
            or right.root.startswith(left.root + "/")
        )
    prefix, exact = (left, right) if left.is_prefix else (right, left)
    return exact.root == prefix.root or exact.root.startswith(prefix.root + "/")


def effective_claims(claims: Iterable[LaneClaim], now: datetime) -> tuple[LaneClaim, ...]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise LaneClaimError("now must be timezone-aware")
    latest: dict[str, LaneClaim] = {}
    for claim in claims:
        current = latest.get(claim.lane_id)
        if current is None or claim.created_at > current.created_at:
            latest[claim.lane_id] = claim
            continue
        if claim.created_at == current.created_at and claim != current:
            raise LaneClaimError(
                f"conflicting same-time claim records for lane {claim.lane_id}"
            )
    return tuple(
        claim
        for claim in sorted(latest.values(), key=lambda item: item.lane_id)
        if claim.is_active_at(now)
    )


def find_collisions(
    candidate: LaneClaim,
    peers: Iterable[LaneClaim],
    now: datetime,
) -> tuple[Collision, ...]:
    if not candidate.is_active_at(now):
        return ()

    active_peers = effective_claims(peers, now)
    collisions: list[Collision] = []
    for peer in active_peers:
        if peer.lane_id == candidate.lane_id:
            if peer != candidate:
                collisions.append(
                    Collision(
                        candidate.lane_id,
                        peer.lane_id,
                        "<lane-id>",
                        "<lane-id>",
                    )
                )
            continue
        for left_scope in candidate.scope:
            for right_scope in peer.scope:
                if _scope_overlaps(left_scope, right_scope):
                    collisions.append(
                        Collision(
                            candidate.lane_id,
                            peer.lane_id,
                            left_scope.value,
                            right_scope.value,
                        )
                    )
    return tuple(collisions)


def _cmd_validate(args: argparse.Namespace) -> int:
    now = _parse_now(args.now)
    claims = load_claims(Path(args.path))
    active = effective_claims(claims, now)
    active_ids = {claim.lane_id for claim in active}
    for claim in claims:
        state = "ACTIVE" if claim.lane_id in active_ids else "INACTIVE"
        print(f"VALID {claim.lane_id} {state}")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    now = _parse_now(args.now)
    candidates = load_claims(Path(args.path))
    if len(candidates) != 1:
        raise LaneClaimError("check requires exactly one candidate claim")
    candidate = candidates[0]

    peers: list[LaneClaim] = []
    for peer_path in args.against:
        peers.extend(load_claims(Path(peer_path)))

    collisions = find_collisions(candidate, peers, now)
    if not collisions:
        print(f"CLEAR {candidate.lane_id}")
        return 0

    for collision in collisions:
        print(
            "COLLISION "
            f"{collision.left_lane} {collision.right_lane} "
            f"{collision.left_scope} {collision.right_scope}"
        )
    return 3


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate Nika parallel development lane claims."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate one claim source")
    validate.add_argument("path")
    validate.add_argument("--now", help="canonical UTC timestamp ending in Z")
    validate.set_defaults(func=_cmd_validate)

    check = subparsers.add_parser("check", help="check one claim against peer claims")
    check.add_argument("path")
    check.add_argument("--against", action="append", default=[], required=True)
    check.add_argument("--now", help="canonical UTC timestamp ending in Z")
    check.set_defaults(func=_cmd_check)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except LaneClaimError as exc:
        print(f"INVALID {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
