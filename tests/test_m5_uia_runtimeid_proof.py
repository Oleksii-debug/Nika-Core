from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROOF = ROOT / "scripts" / "m5_uia_proof.ps1"


def _runtime_id_helper() -> str:
    text = PROOF.read_text(encoding="utf-8")
    start = text.index("function Get-ElementRuntimeId(")
    end = text.index("\nfunction Test-SameAutomationElement(", start)
    return text[start:end]


def test_runtime_id_remains_mandatory_after_bounded_webview_provider_retry() -> None:
    helper = _runtime_id_helper()

    assert "[ValidateRange(1, 40)][int]$Attempts = 20" in helper
    assert "[ValidateRange(10, 500)][int]$DelayMilliseconds = 100" in helper
    assert "for ($attempt = 1; $attempt -le $Attempts; $attempt++)" in helper
    assert "$runtimeId = $Element.GetRuntimeId()" in helper
    assert "$runtimeId.Length -gt 0" in helper
    assert "return [int[]]$runtimeId" in helper
    assert "Start-Sleep -Milliseconds $DelayMilliseconds" in helper
    assert "did not expose a RuntimeId after bounded retry" in helper

    # The resilience change must never authorize a weaker identity source.
    assert "NativeWindowHandle" not in helper
    assert "AutomationId" not in helper
    assert "BoundingRectangle" not in helper
    assert "Click" not in helper
    assert "coordinates" in helper


def test_runtime_id_retry_does_not_swallow_stale_element_identity() -> None:
    helper = _runtime_id_helper()

    # A stale provider generation must still escape as ElementNotAvailable;
    # it is not converted into a name-only rebind.
    assert "catch [System.Windows.Automation.ElementNotAvailableException]" in helper
    assert "throw" in helper
