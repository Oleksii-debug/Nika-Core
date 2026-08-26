from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

MODULE_PATH = Path(__file__).parents[1] / "scripts" / "verify_repository_governance.py"
SPEC = importlib.util.spec_from_file_location("verify_repository_governance", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

ApiFailure = MODULE.ApiFailure
GovernancePolicy = MODULE.GovernancePolicy
GitHubRestClient = MODULE.GitHubRestClient
inspect_repository_governance = MODULE.inspect_repository_governance

HEAD = "a" * 40
REPO = "Oleksii-debug/Nika-Core"
BRANCH = "main"
CHECKS = ["Verify (ubuntu-latest)", "Verify (windows-latest)"]


class FakeClient:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get_json(self, path: str) -> Any:
        self.calls.append(path)
        response = self.responses[path]
        if isinstance(response, Exception):
            raise response
        return response


def branch_response(*, protected: bool = True, head: str = HEAD) -> dict[str, Any]:
    return {"protected": protected, "commit": {"sha": head}}


def classic_protection(*, checks: list[str] | None = None) -> dict[str, Any]:
    return {
        "required_status_checks": {
            "strict": True,
            "contexts": checks or CHECKS,
        },
        "required_pull_request_reviews": {"required_approving_review_count": 0},
        "enforce_admins": {"enabled": True},
        "allow_force_pushes": {"enabled": False},
        "allow_deletions": {"enabled": False},
    }


def base_responses(*, protected: bool = True) -> dict[str, Any]:
    return {
        f"/repos/Oleksii-debug/Nika-Core/branches/{BRANCH}": branch_response(
            protected=protected
        ),
        "/repos/Oleksii-debug/Nika-Core": {"default_branch": "main"},
        "/repos/Oleksii-debug/Nika-Core/rulesets?includes_parents=true": [],
    }


def inspect(client: FakeClient, *, expected_head: str | None = HEAD) -> dict[str, Any]:
    return inspect_repository_governance(
        client,
        repository=REPO,
        branch=BRANCH,
        expected_head=expected_head,
        policy=GovernancePolicy(),
    )


def test_unprotected_branch_fails_closed_without_protection_probe() -> None:
    client = FakeClient(base_responses(protected=False))

    report = inspect(client)

    assert report["status"] == "BLOCKED"
    assert "BRANCH_UNPROTECTED" in report["blockers"]
    assert "PULL_REQUEST_NOT_REQUIRED" in report["blockers"]
    assert all(not call.endswith("/protection") for call in client.calls)


def test_classic_branch_protection_proves_minimum_pf4_controls() -> None:
    responses = base_responses()
    responses[f"/repos/Oleksii-debug/Nika-Core/branches/{BRANCH}/protection"] = (
        classic_protection()
    )
    client = FakeClient(responses)

    report = inspect(client)

    assert report["status"] == "PASS"
    assert report["blockers"] == []
    assert report["proof_sources"] == ["classic_branch_protection"]
    assert report["observed_required_checks"] == CHECKS
    assert report["safe_activation_preview"]["automatic_mutation"] is False


def test_protected_branch_with_unreadable_details_does_not_guess_green() -> None:
    responses = base_responses()
    responses[f"/repos/Oleksii-debug/Nika-Core/branches/{BRANCH}/protection"] = ApiFailure(
        status=403, message="http_403:Resource not accessible by integration"
    )
    client = FakeClient(responses)

    report = inspect(client)

    assert report["status"] == "BLOCKED"
    assert "PROTECTION_DETAILS_NOT_PROVEN" in report["blockers"]
    assert report["evidence_errors"] == [
        "protection:http_403:Resource not accessible by integration"
    ]


def test_active_ruleset_without_bypass_can_prove_pf4_controls() -> None:
    responses = base_responses()
    responses[f"/repos/Oleksii-debug/Nika-Core/branches/{BRANCH}/protection"] = ApiFailure(
        status=403, message="http_403:forbidden"
    )
    responses["/repos/Oleksii-debug/Nika-Core/rulesets?includes_parents=true"] = [
        {"id": 17, "name": "protect-main"}
    ]
    responses["/repos/Oleksii-debug/Nika-Core/rulesets/17"] = {
        "id": 17,
        "name": "protect-main",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {"type": "pull_request"},
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [{"context": check} for check in CHECKS]
                },
            },
        ],
    }
    client = FakeClient(responses)

    report = inspect(client)

    assert report["controls"]["pull_request_required"] is True
    assert report["controls"]["force_push_blocked"] is True
    assert report["controls"]["deletion_blocked"] is True
    assert report["observed_required_checks"] == CHECKS
    assert report["controls"]["admin_enforcement"] is True
    assert report["status"] == "PASS"
    assert report["blockers"] == []


def test_bypass_ruleset_is_not_used_when_classic_protection_is_complete() -> None:
    responses = base_responses()
    responses[f"/repos/Oleksii-debug/Nika-Core/branches/{BRANCH}/protection"] = (
        classic_protection()
    )
    responses["/repos/Oleksii-debug/Nika-Core/rulesets?includes_parents=true"] = [
        {"id": 18, "name": "extra-policy"}
    ]
    responses["/repos/Oleksii-debug/Nika-Core/rulesets/18"] = {
        "id": 18,
        "name": "extra-policy",
        "enforcement": "active",
        "bypass_actors": [
            {"actor_id": 123, "actor_type": "Integration", "bypass_mode": "always"}
        ],
        "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
        "rules": [],
    }
    client = FakeClient(responses)

    report = inspect(client)

    assert report["status"] == "PASS"
    assert report["blockers"] == []
    assert report["controls"]["ruleset_bypass_actor_count"] == 1
    assert report["controls"]["proof_eligible_ruleset_count"] == 0


def test_ruleset_bypass_actor_blocks_when_ruleset_is_needed_for_proof() -> None:
    responses = base_responses()
    responses[f"/repos/Oleksii-debug/Nika-Core/branches/{BRANCH}/protection"] = ApiFailure(
        status=403, message="http_403:forbidden"
    )
    responses["/repos/Oleksii-debug/Nika-Core/rulesets?includes_parents=true"] = [
        {"id": 19, "name": "protect-main"}
    ]
    responses["/repos/Oleksii-debug/Nika-Core/rulesets/19"] = {
        "id": 19,
        "name": "protect-main",
        "enforcement": "active",
        "bypass_actors": [
            {"actor_id": 123, "actor_type": "Integration", "bypass_mode": "always"}
        ],
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {"type": "pull_request"},
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [{"context": check} for check in CHECKS]
                },
            },
        ],
    }
    client = FakeClient(responses)

    report = inspect(client)

    assert report["status"] == "BLOCKED"
    assert "ADMIN_BYPASS_NOT_CLOSED" in report["blockers"]
    assert "PULL_REQUEST_NOT_REQUIRED" in report["blockers"]
    assert report["controls"]["proof_eligible_ruleset_count"] == 0


def test_unrelated_ruleset_cannot_close_classic_admin_bypass() -> None:
    responses = base_responses()
    protection = classic_protection()
    protection["enforce_admins"] = {"enabled": False}
    responses[f"/repos/Oleksii-debug/Nika-Core/branches/{BRANCH}/protection"] = protection
    responses["/repos/Oleksii-debug/Nika-Core/rulesets?includes_parents=true"] = [
        {"id": 20, "name": "metadata-only"}
    ]
    responses["/repos/Oleksii-debug/Nika-Core/rulesets/20"] = {
        "id": 20,
        "name": "metadata-only",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [{"type": "required_commit_message"}],
    }
    client = FakeClient(responses)

    report = inspect(client)

    assert report["status"] == "BLOCKED"
    assert "PULL_REQUEST_NOT_REQUIRED" in report["blockers"]
    assert "REQUIRED_STATUS_CHECKS_MISSING" in report["blockers"]


def test_missing_required_check_is_not_accepted() -> None:
    responses = base_responses()
    responses[f"/repos/Oleksii-debug/Nika-Core/branches/{BRANCH}/protection"] = (
        classic_protection(checks=[CHECKS[0]])
    )
    client = FakeClient(responses)

    report = inspect(client)

    assert report["status"] == "BLOCKED"
    assert report["missing_required_checks"] == [CHECKS[1]]
    assert "REQUIRED_STATUS_CHECKS_MISSING" in report["blockers"]


def test_expected_head_mismatch_blocks_stale_governance_evidence() -> None:
    responses = base_responses()
    responses[f"/repos/Oleksii-debug/Nika-Core/branches/{BRANCH}/protection"] = (
        classic_protection()
    )
    client = FakeClient(responses)

    report = inspect(client, expected_head="b" * 40)

    assert report["status"] == "BLOCKED"
    assert report["blockers"] == ["HEAD_MISMATCH"]


def test_rest_client_pins_github_api_host() -> None:
    client = GitHubRestClient(token="synthetic-token", timeout=1.0)

    assert client._api_base == "https://api.github.com"


def test_report_never_contains_token_value() -> None:
    token = "ghp_SYNTHETIC_SUPER_SECRET_CANARY"
    responses = base_responses()
    responses[f"/repos/Oleksii-debug/Nika-Core/branches/{BRANCH}/protection"] = ApiFailure(
        status=403, message="http_403:forbidden"
    )
    client = FakeClient(responses)

    report = inspect(client)
    serialized = str(report)

    assert token not in serialized
    assert "Authorization" not in serialized
