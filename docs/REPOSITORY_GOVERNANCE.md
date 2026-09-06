# Nika Core repository governance proof

Status: PF4 acceptance support. This document and its verifier are read-only; they do not enable, weaken, or delete GitHub protection settings.

## Why this exists

`docs/AUTONOMOUS_PRODUCT_FACTORY_ACCEPTANCE.md` requires PF4 to prove that production/main is protected from direct worker mutation. A green coordinator or green test suite does not satisfy that repository-level control by itself.

The canonical verifier is:

```text
python scripts/verify_repository_governance.py --expected-head <EXACT_MAIN_SHA>
```

Exit codes are deterministic:

- `0`: the minimum PF4 repository-governance controls are proven for the exact requested head;
- `2`: governance is absent, incomplete, stale, or cannot be proven;
- `3`: the verifier itself could not obtain/parse required GitHub evidence.

The JSON report is suitable for logs and screen readers. It never prints the token value.

## Minimum fail-closed policy

The verifier requires all of the following before returning `PASS`:

1. the selected branch is protected;
2. changes require a pull request;
3. classic branch protection applies to administrators and has no `bypass_pull_request_allowances` for users, teams, or apps when it is used as evidence;
4. force pushes are blocked;
5. branch deletion is blocked;
6. the stable Core CI contexts `Verify (ubuntu-latest)` and `Verify (windows-latest)` are required;
7. active rulesets used as evidence have no bypass actors;
8. when `--expected-head` is supplied, the observed branch head is exactly that SHA.

Path-conditional or routinely skipped workflows are deliberately not made universal required checks. M11/M12 remain release/acceptance evidence and can be added to repository protection only after their exact stable check surfaces are proven suitable for every protected-branch change.

## Authentication and least privilege

The public branch summary may show whether a branch is protected, but complete classic branch-protection evidence can require authenticated GitHub API access. GitHub's current REST documentation specifies repository `Administration: read` permission for the Get branch protection endpoint.

Pass a token only through an environment variable, never a command-line value or repository file. The default is `GITHUB_TOKEN`; use `--token-env NAME` to select another environment variable. The verifier pins requests to `https://api.github.com`; there is no command-line API-host override that could redirect a token. Do not commit `.env`, tokens, browser credentials, or GitHub sessions.

If the token cannot read detailed protection state, the verifier reports `BLOCKED` rather than guessing that the branch is safe.

## Safe activation boundary

This repository tool intentionally does not mutate branch protection or rulesets. Enabling or changing repository governance is a high-impact administrator action because an incorrect required context or bypass policy can deadlock automated integration.

Before activation, retain these invariants:

- PR-based changes to `main`;
- no classic PR-bypass allowances for worker users, teams, or apps;
- no force push or deletion;
- exact stable Core CI contexts only;
- no silent ruleset worker/app bypass;
- a documented repository-owner break-glass recovery path;
- after any break-glass change, rerun the exact-head Core, M11, M12 and this governance verifier before restoring release credit.

A protection change is not PF4 acceptance by itself. Acceptance requires rerunning this verifier against the resulting live repository state and preserving the exact GitHub evidence in the integration/release handoff.

## REUSE -> ADAPT -> CUSTOM (thin)

- **REUSE:** GitHub's branch/ruleset/status-check control plane and the existing Core CI job identities.
- **ADAPT:** normalize those live controls into one deterministic PF4 evidence report.
- **CUSTOM (thin):** Nika-specific fail-closed acceptance policy, exact-head binding, and accessible JSON output.

No second CI engine, policy framework, or repository mutation service is introduced.
