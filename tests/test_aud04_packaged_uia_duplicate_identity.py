from __future__ import annotations

import re
from pathlib import Path

SCRIPT = Path("scripts/m5_uia_proof.ps1")


def _function_body(source: str, function_name: str, end_marker: str) -> str:
    marker = f"function {function_name}"
    assert marker in source, f"missing packaged UIA helper: {function_name}"
    body = source.split(marker, 1)[1]
    assert end_marker in body, f"could not bound packaged UIA helper: {function_name}"
    return body.split(end_marker, 1)[0]


def test_packaged_uia_action_target_resolution_never_picks_first_duplicate() -> None:
    source = SCRIPT.read_text(encoding="utf-8-sig")
    resolver = _function_body(source, "Find-BoundDescendantName", "$window = $null")

    assert ".FindFirst(" not in resolver, (
        "packaged UIA proof resolves a named descendant with FindFirst; duplicate semantic "
        "labels can therefore be silently reduced to whichever element is returned first"
    )
    assert ".FindAll(" in resolver, (
        "packaged UIA proof must enumerate exact-name candidates before selecting an action target"
    )
    assert re.search(r"\.Count\s+-(?:gt|ge|ne)\s+1", resolver), (
        "packaged UIA proof must explicitly reject duplicate exact-name candidates"
    )


def test_packaged_uia_focus_verification_binds_element_identity_not_name_only() -> None:
    source = SCRIPT.read_text(encoding="utf-8-sig")
    focus_wait = _function_body(source, "Wait-FocusName", "# The DOM can be visible")

    assert (
        "RuntimeIdProperty" in focus_wait
        or "GetRuntimeId" in focus_wait
        or "CompareElements" in focus_wait
        or "Automation.Compare" in focus_wait
    ), (
        "packaged UIA proof accepts focus by accessible Name alone; duplicate labels can "
        "satisfy the assertion without proving focus stayed on the intended semantic element"
    )