#!/usr/bin/env python3
"""Read-only proof for Nika Core repository governance.

The verifier never changes GitHub settings. It reports whether the selected branch is
provably protected against direct mutation under the minimum PF4 policy.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

DEFAULT_REPOSITORY = "Oleksii-debug/Nika-Core"
DEFAULT_BRANCH = "main"
DEFAULT_REQUIRED_CHECKS = (
    "Verify (ubuntu-latest)",
    "Verify (windows-latest)",
)
API_VERSION = "2026-03-10"
USER_AGENT = "Nika-Core-PF4-Governance-Proof/1"


class ApiFailure(RuntimeError):
    """A sanitized GitHub API failure."""

    def __init__(self, *, status: int | None, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class JsonClient(Protocol):
    def get_json(self, path: str) -> Any: ...


@dataclass(frozen=True)
class GovernancePolicy:
    required_checks: tuple[str, ...] = DEFAULT_REQUIRED_CHECKS
    require_pull_request: bool = True
    require_admin_enforcement: bool = True
    forbid_force_pushes: bool = True
    forbid_deletions: bool = True
    forbid_ruleset_bypass: bool = True


class GitHubRestClient:
    """Minimal read-only GitHub REST client with secret-safe failures."""

    def __init__(self, *, api_base: str, token: str | None, timeout: float) -> None:
        self._api_base = api_base.rstrip("/")
        self._token = token
        self._timeout = timeout

    def get_json(self, path: str) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = Request(f"{self._api_base}{path}", headers=headers, method="GET")
        try:
            with urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                payload = response.read().decode("utf-8")
        except HTTPError as exc:
            message = _safe_http_error_message(exc)
            raise ApiFailure(status=exc.code, message=message) from None
        except (URLError, TimeoutError, OSError) as exc:
            raise ApiFailure(status=None, message=f"network_error:{type(exc).__name__}") from None
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            raise ApiFailure(status=None, message="invalid_json_response") from None


def _safe_http_error_message(exc: HTTPError) -> str:
    """Return only non-secret API error metadata."""

    try:
        raw = exc.read().decode("utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    api_message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(api_message, str) or not api_message.strip():
        api_message = "github_api_error"
    return f"http_{exc.code}:{api_message.strip()}"


def _bool_enabled(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        enabled = value.get("enabled")
        if isinstance(enabled, bool):
            return enabled
    return default


def _classic_required_checks(protection: dict[str, Any]) -> set[str]:
    status_checks = protection.get("required_status_checks")
    if not isinstance(status_checks, dict):
        return set()
    names: set[str] = set()
    contexts = status_checks.get("contexts", [])
    if isinstance(contexts, list):
        names.update(item for item in contexts if isinstance(item, str))
    checks = status_checks.get("checks", [])
    if isinstance(checks, list):
        for item in checks:
            if isinstance(item, dict) and isinstance(item.get("context"), str):
                names.add(item["context"])
    return names


def _ruleset_targets_branch(
    ruleset: dict[str, Any], *, branch: str, default_branch: str
) -> bool:
    conditions = ruleset.get("conditions")
    if not isinstance(conditions, dict):
        return False
    ref_name = conditions.get("ref_name")
    if not isinstance(ref_name, dict):
        return False
    includes = ref_name.get("include", [])
    excludes = ref_name.get("exclude", [])
    if not isinstance(includes, list) or not isinstance(excludes, list):
        return False

    ref = f"refs/heads/{branch}"

    def matches(pattern: Any) -> bool:
        if not isinstance(pattern, str):
            return False
        if pattern == "~ALL":
            return True
        if pattern == "~DEFAULT_BRANCH":
            return branch == default_branch
        return fnmatchcase(ref, pattern)

    if any(matches(pattern) for pattern in excludes):
        return False
    return any(matches(pattern) for pattern in includes)


def _ruleset_controls(ruleset: dict[str, Any]) -> dict[str, Any]:
    rule_types: set[str] = set()
    required_checks: set[str] = set()
    rules = ruleset.get("rules", [])
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            rule_type = rule.get("type")
            if not isinstance(rule_type, str):
                continue
            rule_types.add(rule_type)
            if rule_type != "required_status_checks":
                continue
            parameters = rule.get("parameters")
            if not isinstance(parameters, dict):
                continue
            checks = parameters.get("required_status_checks", [])
            if not isinstance(checks, list):
                continue
            for check in checks:
                if isinstance(check, dict) and isinstance(check.get("context"), str):
                    required_checks.add(check["context"])
    bypass_actors = ruleset.get("bypass_actors", [])
    if not isinstance(bypass_actors, list):
        bypass_actors = []
    return {
        "pull_request": "pull_request" in rule_types,
        "no_force_push": "non_fast_forward" in rule_types,
        "no_delete": "deletion" in rule_types,
        "required_checks": required_checks,
        "bypass_actor_count": len(bypass_actors),
    }


def _fetch_ruleset_details(
    client: JsonClient, *, owner: str, repo: str, summaries: Any
) -> tuple[list[dict[str, Any]], list[str]]:
    details: list[dict[str, Any]] = []
    errors: list[str] = []
    if not isinstance(summaries, list):
        return details, ["rulesets_response_not_list"]
    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        ruleset_id = summary.get("id")
        if not isinstance(ruleset_id, int):
            continue
        try:
            detail = client.get_json(f"/repos/{owner}/{repo}/rulesets/{ruleset_id}")
        except ApiFailure as exc:
            errors.append(f"ruleset_{ruleset_id}:{exc.message}")
            continue
        if isinstance(detail, dict):
            details.append(detail)
        else:
            errors.append(f"ruleset_{ruleset_id}:response_not_object")
    return details, errors


def inspect_repository_governance(
    client: JsonClient,
    *,
    repository: str,
    branch: str,
    expected_head: str | None,
    policy: GovernancePolicy,
) -> dict[str, Any]:
    try:
        owner, repo = repository.split("/", 1)
    except ValueError as exc:
        raise ValueError("repository must use owner/name form") from exc
    if not owner or not repo:
        raise ValueError("repository must use owner/name form")

    encoded_branch = quote(branch, safe="")
    branch_path = f"/repos/{owner}/{repo}/branches/{encoded_branch}"
    branch_data = client.get_json(branch_path)
    if not isinstance(branch_data, dict):
        raise ApiFailure(status=None, message="branch_response_not_object")

    commit = branch_data.get("commit")
    observed_head = commit.get("sha") if isinstance(commit, dict) else None
    protected = branch_data.get("protected") is True

    repo_data = client.get_json(f"/repos/{owner}/{repo}")
    default_branch = (
        repo_data.get("default_branch") if isinstance(repo_data, dict) else None
    )
    if not isinstance(default_branch, str):
        default_branch = DEFAULT_BRANCH

    evidence_errors: list[str] = []
    protection: dict[str, Any] | None = None
    if protected:
        try:
            candidate = client.get_json(f"{branch_path}/protection")
            if isinstance(candidate, dict):
                protection = candidate
            else:
                evidence_errors.append("protection:response_not_object")
        except ApiFailure as exc:
            evidence_errors.append(f"protection:{exc.message}")

    try:
        ruleset_summaries = client.get_json(f"/repos/{owner}/{repo}/rulesets?includes_parents=true")
    except ApiFailure as exc:
        ruleset_summaries = []
        evidence_errors.append(f"rulesets:{exc.message}")
    rulesets, ruleset_errors = _fetch_ruleset_details(
        client, owner=owner, repo=repo, summaries=ruleset_summaries
    )
    evidence_errors.extend(ruleset_errors)

    classic = {
        "available": protection is not None,
        "pull_request": False,
        "admin_enforcement": False,
        "no_force_push": False,
        "no_delete": False,
        "required_checks": set(),
    }
    if protection is not None:
        classic["pull_request"] = isinstance(
            protection.get("required_pull_request_reviews"), dict
        )
        classic["admin_enforcement"] = _bool_enabled(protection.get("enforce_admins"))
        classic["no_force_push"] = not _bool_enabled(
            protection.get("allow_force_pushes"), default=True
        )
        classic["no_delete"] = not _bool_enabled(
            protection.get("allow_deletions"), default=True
        )
        classic["required_checks"] = _classic_required_checks(protection)

    active_rulesets: list[dict[str, Any]] = []
    for ruleset in rulesets:
        if ruleset.get("enforcement") != "active":
            continue
        if not _ruleset_targets_branch(ruleset, branch=branch, default_branch=default_branch):
            continue
        controls = _ruleset_controls(ruleset)
        active_rulesets.append(
            {
                "id": ruleset.get("id"),
                "name": ruleset.get("name"),
                **controls,
            }
        )

    observed_ruleset_bypass_actor_count = sum(
        item["bypass_actor_count"] for item in active_rulesets
    )
    proof_rulesets = [item for item in active_rulesets if item["bypass_actor_count"] == 0]
    ruleset_combined = {
        "pull_request": any(item["pull_request"] for item in proof_rulesets),
        "no_force_push": any(item["no_force_push"] for item in proof_rulesets),
        "no_delete": any(item["no_delete"] for item in proof_rulesets),
        "required_checks": (
            set().union(*(item["required_checks"] for item in proof_rulesets))
            if proof_rulesets
            else set()
        ),
    }

    classic_unrestricted = bool(protection is not None and classic["admin_enforcement"])
    classic_checks = set(classic["required_checks"]) if classic_unrestricted else set()
    combined_checks = classic_checks | set(ruleset_combined["required_checks"])
    controls = {
        "branch_protected": protected,
        "pull_request_required": bool(
            (classic_unrestricted and classic["pull_request"])
            or ruleset_combined["pull_request"]
        ),
        "admin_enforcement": bool(classic_unrestricted or proof_rulesets),
        "force_push_blocked": bool(
            (classic_unrestricted and classic["no_force_push"])
            or ruleset_combined["no_force_push"]
        ),
        "deletion_blocked": bool(
            (classic_unrestricted and classic["no_delete"])
            or ruleset_combined["no_delete"]
        ),
        "ruleset_bypass_actor_count": observed_ruleset_bypass_actor_count,
        "proof_eligible_ruleset_count": len(proof_rulesets),
    }

    blockers: list[str] = []
    if expected_head and observed_head != expected_head:
        blockers.append("HEAD_MISMATCH")
    if not protected:
        blockers.append("BRANCH_UNPROTECTED")
    if policy.require_pull_request and not controls["pull_request_required"]:
        blockers.append("PULL_REQUEST_NOT_REQUIRED")
    if policy.require_admin_enforcement and not controls["admin_enforcement"]:
        blockers.append("ADMIN_BYPASS_NOT_CLOSED")
    if policy.forbid_force_pushes and not controls["force_push_blocked"]:
        blockers.append("FORCE_PUSH_NOT_PROVEN_BLOCKED")
    if policy.forbid_deletions and not controls["deletion_blocked"]:
        blockers.append("DELETION_NOT_PROVEN_BLOCKED")
    missing_checks = sorted(set(policy.required_checks) - combined_checks)
    if missing_checks:
        blockers.append("REQUIRED_STATUS_CHECKS_MISSING")

    if protected and protection is None and not proof_rulesets:
        blockers.append("PROTECTION_DETAILS_NOT_PROVEN")

    status = "PASS" if not blockers else "BLOCKED"
    proof_sources: list[str] = []
    if protection is not None:
        proof_sources.append("classic_branch_protection")
    if proof_rulesets:
        proof_sources.append("active_ruleset")

    return {
        "schema_version": 1,
        "status": status,
        "repository": repository,
        "branch": branch,
        "observed_head": observed_head,
        "expected_head": expected_head,
        "default_branch": default_branch,
        "proof_sources": proof_sources,
        "controls": controls,
        "required_checks": list(policy.required_checks),
        "observed_required_checks": sorted(combined_checks),
        "missing_required_checks": missing_checks,
        "active_rulesets": [
            {
                **item,
                "required_checks": sorted(item["required_checks"]),
            }
            for item in active_rulesets
        ],
        "blockers": blockers,
        "evidence_errors": evidence_errors,
        "safe_activation_preview": {
            "automatic_mutation": False,
            "require_pull_request": True,
            "require_admin_enforcement": True,
            "block_force_pushes": True,
            "block_deletions": True,
            "required_status_checks": list(policy.required_checks),
            "break_glass": (
                "Repository owner may deliberately change governance only as an audited recovery "
                "action, then must rerun exact-head Core/M11/M12 and this verifier."
            ),
        },
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read GitHub governance state and fail closed unless PF4 main-branch "
            "protection is proven."
        )
    )
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY, help="GitHub owner/name")
    parser.add_argument("--branch", default=DEFAULT_BRANCH)
    parser.add_argument("--expected-head", default=None)
    parser.add_argument(
        "--required-check",
        action="append",
        dest="required_checks",
        help="Required status-check context; repeat to override defaults.",
    )
    parser.add_argument("--api-base", default="https://api.github.com")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument(
        "--token-env",
        default="GITHUB_TOKEN",
        help="Environment variable containing a GitHub token. Its value is never printed.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    required_checks = tuple(args.required_checks or DEFAULT_REQUIRED_CHECKS)
    token = os.environ.get(args.token_env) if args.token_env else None
    client = GitHubRestClient(api_base=args.api_base, token=token, timeout=args.timeout)
    try:
        report = inspect_repository_governance(
            client,
            repository=args.repository,
            branch=args.branch,
            expected_head=args.expected_head,
            policy=GovernancePolicy(required_checks=required_checks),
        )
    except (ApiFailure, ValueError) as exc:
        if isinstance(exc, ApiFailure):
            error = exc.message
            status = exc.status
        else:
            error = str(exc)
            status = None
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "ERROR",
                    "error": error,
                    "http_status": status,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 3

    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
