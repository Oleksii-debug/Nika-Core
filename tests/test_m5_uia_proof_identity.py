from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "m5_uia_proof.ps1"


def _source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_m5_uia_proof_binds_top_level_window_to_launched_process() -> None:
    source = _source()

    assert "ProcessIdProperty" in source
    assert "[System.Windows.Automation.AndCondition]::new" in source
    assert "Find-ExactWindow" in source
    assert "Multiple Nika Core top-level UI Automation windows matched" in source
    assert "$root.FindFirst([System.Windows.Automation.TreeScope]::Children" not in source


def test_m5_uia_proof_forces_renderer_accessibility_only_for_child_process() -> None:
    source = _source()

    env_name = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"
    force_arg = "--force-renderer-accessibility"
    disable_arg = "--disable-renderer-accessibility"

    assert env_name in source
    assert force_arg in source
    assert disable_arg in source
    assert "previousWebView2BrowserArgs" in source
    assert f"Remove-Item Env:{env_name}" in source
    assert source.index(force_arg) < source.index("Start-Process -FilePath $ExePath -PassThru")
    assert "cannot run while WebView2 renderer accessibility is explicitly disabled" in source


def test_m5_uia_proof_activates_uia_providers_only_below_exact_bound_window() -> None:
    source = _source()

    assert "GetDescendantWindows" in source
    assert "EnumChildWindows" in source
    assert "NativeWindowHandle" in source
    assert "AutomationElement]::FromHandle" in source
    assert "Get-BoundSearchRoots" in source
    assert "Get-BoundDescendantNames" in source
    assert "Find-BoundDescendantName" in source
    assert source.index("Find-ExactWindow") < source.index("Get-BoundSearchRoots")
    assert "without coordinates, another process, or a relaunch" in source
    assert source.count("Start-Process -FilePath $ExePath -PassThru") == 1


def test_m5_uia_proof_uses_one_bounded_cold_start_deadline() -> None:
    source = _source()

    assert "[ValidateRange(30, 120)][int]$StartupTimeoutSeconds = 90" in source
    assert "[System.Diagnostics.Stopwatch]::StartNew()" in source
    assert "[System.TimeSpan]::FromSeconds($StartupTimeoutSeconds)" in source
    assert source.count("$startupWatch.Elapsed -lt $startupTimeout") == 2
    assert "Required packaged WebView2 UIA semantics became discoverable after" in source
    assert "within the bounded $StartupTimeoutSeconds-second startup deadline" in source
    assert "$startupWatch.Elapsed.TotalSeconds" in source
    assert "for ($attempt = 0; $attempt -lt 40 -and $null -eq $window" not in source
    assert "for ($attempt = 0; $attempt -lt 40 -and $missing.Count -gt 0" not in source


def test_m5_uia_proof_re_resolves_window_without_weakening_semantic_gate() -> None:
    source = _source()

    assert source.count("Find-ExactWindow") >= 4
    assert "ElementNotAvailableException" in source
    assert "Start-Process -FilePath $ExePath -PassThru" in source
    assert "Stop-Process -Id $process.Id -Force" in source
    for required_name in (
        "Nika Core",
        "Що має зробити Nika?",
        "Створити завдання",
        "Клавіатура",
        "Nika Core готова до роботи.",
        "Завдання",
    ):
        assert required_name in source

    assert "SendKeys]::SendWait('%1')" in source
    assert "SendKeys]::SendWait('^+p')" in source
    assert "Start-Sleep -Milliseconds 500" in source
    assert "Start-Sleep -Milliseconds 250" in source


def test_named_control_resolution_collects_all_bound_root_candidates_and_rejects_duplicates() -> None:
    source = _source()
    start = source.index("function Find-BoundDescendantName")
    end = source.index("function New-BoundControlIdentity", start)
    resolver = source[start:end]

    assert ".FindFirst(" not in resolver
    assert ".FindAll(" in resolver
    assert "Add-UniqueAutomationElement" in resolver
    assert "ControlTypeProperty" in resolver
    assert "[System.Windows.Automation.AndCondition]::new" in resolver
    assert "$candidates.Count -gt 1" in resolver
    assert "Multiple distinct UI Automation descendants matched exact semantic locator" in resolver


def test_packaged_hotkey_targets_use_exact_name_and_control_type_locators() -> None:
    source = _source()

    assert (
        "$startControl = Wait-DescendantName 'Створити завдання' "
        "([System.Windows.Automation.ControlType]::Button)"
    ) in source
    assert (
        "$tasksControl = Wait-DescendantName 'Завдання' "
        "([System.Windows.Automation.ControlType]::Text)"
    ) in source
    assert (
        "$commandControl = Wait-DescendantName 'Що має зробити Nika?' "
        "([System.Windows.Automation.ControlType]::Edit)"
    ) in source


def test_focus_verification_uses_captured_runtime_id_generation_and_live_target() -> None:
    source = _source()
    start = source.index("function Wait-FocusName")
    end = source.index("function Set-BoundControlFocus", start)
    verifier = source[start:end]

    assert "GetRuntimeId" in verifier
    assert "ExpectedControl.RuntimeId" in verifier
    assert "ExpectedControl.ElementGeneration" in verifier
    assert "Automation]::Compare($target, $focused)" in verifier
    assert "Current.Name -eq" not in verifier


def test_control_identity_is_bound_to_process_window_and_live_element_generation() -> None:
    source = _source()
    start = source.index("function Resolve-BoundControlIdentity")
    end = source.index("$window = $null", start)
    resolver = source[start:end]

    for token in (
        "ProcessStartTicks",
        "ExecutablePath",
        "WindowHandle",
        "WindowRuntimeId",
        "RuntimeId",
        "ElementGeneration",
        "ExpectedControlType",
        "Resolve-BoundControlIdentity",
    ):
        assert token in source
    assert "RuntimeId reuse cannot restore control authority" in resolver
    assert "Get-ElementRuntimeId $Identity.Element" in resolver
    assert "Test-ElementMatchesSemanticLocator $Identity.Element" in resolver
    assert "Automation]::Compare($Identity.Element, $resolved)" in resolver
    assert "re-resolved to a different live element generation" in resolver
    assert resolver.index("Get-ElementRuntimeId $Identity.Element") < resolver.index(
        "$resolved = Find-BoundDescendantName"
    )
    assert "Discard the incomplete snapshot and retry all bound roots" in source
