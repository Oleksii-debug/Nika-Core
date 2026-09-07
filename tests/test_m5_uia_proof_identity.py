from __future__ import annotations

from pathlib import Path


PROOF = Path(__file__).parents[1] / "scripts" / "m5_uia_proof.ps1"


def _source() -> str:
    return PROOF.read_text(encoding="utf-8")


def test_named_control_resolution_collects_all_bound_root_candidates_and_rejects_duplicates() -> None:
    source = _source()
    start = source.index("function Find-BoundDescendantName")
    end = source.index("function New-BoundControlIdentity", start)
    resolver = source[start:end]

    assert ".FindFirst(" not in resolver
    assert ".FindAll(" in resolver
    assert "Add-AddressableBoundCandidate" in resolver
    assert "ControlTypeProperty" in resolver
    assert "[System.Windows.Automation.AndCondition]::new" in resolver
    assert "$candidates.Count -gt 1" in resolver
    assert "Multiple distinct UI Automation descendants matched exact semantic locator" in resolver
