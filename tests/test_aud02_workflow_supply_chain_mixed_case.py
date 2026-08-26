from __future__ import annotations

import runpy
from collections.abc import Callable
from pathlib import Path
from typing import cast

_CHECKOUT_SHA = "11d5960a326750d5838078e36cf38b85af677262"
_POLICY_PATH = Path("tests/test_workflow_supply_chain_policy.py")


def _owner_checkout_findings(path: Path) -> list[str]:
    namespace = runpy.run_path(str(_POLICY_PATH))
    finder = cast(Callable[[Path], list[str]], namespace["_checkout_credential_findings"])
    return finder(path)


def _write_checkout(
    path: Path,
    *,
    action_repository: str,
    persist_credentials: str | None,
) -> None:
    lines = [
        "jobs:",
        "  verify:",
        "    steps:",
        "      - name: Checkout candidate",
        f"        uses: {action_repository}@{_CHECKOUT_SHA}",
        "        with:",
        "          ref: main",
    ]
    if persist_credentials is not None:
        lines.append(f"          persist-credentials: {persist_credentials}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_owner_checkout_policy_treats_repository_identity_case_insensitively(
    tmp_path: Path,
) -> None:
    lowercase_missing = tmp_path / "lowercase-missing.yml"
    mixed_safe = tmp_path / "mixed-safe.yml"
    mixed_missing = tmp_path / "mixed-missing.yml"
    mixed_true = tmp_path / "mixed-true.yml"

    _write_checkout(
        lowercase_missing,
        action_repository="actions/checkout",
        persist_credentials=None,
    )
    _write_checkout(
        mixed_safe,
        action_repository="Actions/Checkout",
        persist_credentials="false",
    )
    _write_checkout(
        mixed_missing,
        action_repository="Actions/Checkout",
        persist_credentials=None,
    )
    _write_checkout(
        mixed_true,
        action_repository="Actions/Checkout",
        persist_credentials="true",
    )

    assert len(_owner_checkout_findings(lowercase_missing)) == 1
    assert _owner_checkout_findings(mixed_safe) == []
    assert len(_owner_checkout_findings(mixed_missing)) == 1
    assert len(_owner_checkout_findings(mixed_true)) == 1
