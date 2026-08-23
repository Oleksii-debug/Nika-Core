param(
    [Parameter(Mandatory=$true)][string]$ExePath,
    [string]$WindowTitle = 'Nika Core M5 Proof',
    [ValidateRange(30, 120)][int]$StartupTimeoutSeconds = 90
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public static class NikaUiaNative
{
    private delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern bool EnumChildWindows(IntPtr hWndParent, EnumWindowsProc lpEnumFunc, IntPtr lParam);

    public static IntPtr[] GetDescendantWindows(IntPtr parent)
    {
        var handles = new List<IntPtr>();
        EnumChildWindows(parent, delegate (IntPtr hWnd, IntPtr lParam) {
            handles.Add(hWnd);
            return true;
        }, IntPtr.Zero);
        return handles.ToArray();
    }
}
'@

$requiredNames = @('Nika Core', 'Що має зробити Nika?', 'Створити завдання', 'Клавіатура')

# WebView2 enables renderer accessibility on demand when assistive technology such
# as a screen reader is detected. GitHub-hosted Windows runners do not run a
# screen reader, so make the automated UIA proof deterministic by forcing the
# renderer accessibility mode for this child process only. This does not set
# HUMAN_TESTED/NVDA_VERIFIED and does not alter the shipped application config.
$previousWebView2BrowserArgs = $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS
$forceRendererAccessibilityArg = '--force-renderer-accessibility'
if ($previousWebView2BrowserArgs -match '(?i)(?:^|\s)--disable-renderer-accessibility(?:\s|$)') {
    throw 'Automated UIA proof cannot run while WebView2 renderer accessibility is explicitly disabled.'
}
if ([string]::IsNullOrWhiteSpace($previousWebView2BrowserArgs)) {
    $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = $forceRendererAccessibilityArg
} elseif ($previousWebView2BrowserArgs -notmatch '(?i)(?:^|\s)--force-renderer-accessibility(?:\s|$)') {
    $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = "$previousWebView2BrowserArgs $forceRendererAccessibilityArg"
}

$process = $null
$startupWatch = $null
$expectedProcessId = $null
$expectedProcessStartTicks = $null
$expectedExecutablePath = $null
$boundWindowHandle = $null
$boundWindowRuntimeId = $null

function Get-ElementRuntimeId([System.Windows.Automation.AutomationElement]$Element) {
    $runtimeId = $Element.GetRuntimeId()
    if ($null -eq $runtimeId -or $runtimeId.Length -eq 0) {
        throw 'UI Automation element did not expose a RuntimeId.'
    }
    return [int[]]$runtimeId
}

function Test-SameAutomationElement(
    [System.Windows.Automation.AutomationElement]$Left,
    [System.Windows.Automation.AutomationElement]$Right
) {
    return [System.Windows.Automation.Automation]::Compare($Left, $Right)
}

function Add-UniqueAutomationElement($Elements, [System.Windows.Automation.AutomationElement]$Candidate) {
    foreach ($existing in $Elements) {
        try {
            if (Test-SameAutomationElement $existing $Candidate) { return }
        } catch [System.Windows.Automation.ElementNotAvailableException] {
            throw
        }
    }
    [void]$Elements.Add($Candidate)
}

try {
    $ExePath = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $ExePath).Path)
    $expectedExecutablePath = $ExePath
    $process = Start-Process -FilePath $ExePath -PassThru
    $expectedProcessId = $process.Id
    $expectedProcessStartTicks = $process.StartTime.ToUniversalTime().Ticks
    $startedExecutablePath = [System.IO.Path]::GetFullPath($process.MainModule.FileName)
    if (-not [string]::Equals(
        $startedExecutablePath,
        $expectedExecutablePath,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Started executable '$startedExecutablePath' does not match requested executable '$expectedExecutablePath'."
    }

    function Assert-BoundProcessGeneration {
        if ($null -eq $process) { throw 'Packaged UIA proof process is not bound.' }
        $process.Refresh()
        if ($process.HasExited) {
            throw "Nika Core process generation $expectedProcessId has exited."
        }
        if ($process.Id -ne $expectedProcessId) {
            throw "Nika Core process identity changed from PID $expectedProcessId to PID $($process.Id)."
        }
        $currentStartTicks = $process.StartTime.ToUniversalTime().Ticks
        if ($currentStartTicks -ne $expectedProcessStartTicks) {
            throw "Nika Core PID $expectedProcessId was rebound to a different process generation."
        }
        $currentExecutablePath = [System.IO.Path]::GetFullPath($process.MainModule.FileName)
        if (-not [string]::Equals(
            $currentExecutablePath,
            $expectedExecutablePath,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            throw "Nika Core process executable changed from '$expectedExecutablePath' to '$currentExecutablePath'."
        }
    }

    $startupWatch = [System.Diagnostics.Stopwatch]::StartNew()
    $startupTimeout = [System.TimeSpan]::FromSeconds($StartupTimeoutSeconds)
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $nameCondition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty,
        $WindowTitle
    )
    $processCondition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ProcessIdProperty,
        $expectedProcessId
    )
    $windowCondition = [System.Windows.Automation.AndCondition]::new(
        [System.Windows.Automation.Condition[]]@($nameCondition, $processCondition)
    )

    function Find-ExactWindow {
        Assert-BoundProcessGeneration
        $matches = $root.FindAll(
            [System.Windows.Automation.TreeScope]::Children,
            $windowCondition
        )
        if ($matches.Count -gt 1) {
            throw "Multiple Nika Core top-level UI Automation windows matched title '$WindowTitle' and process $expectedProcessId."
        }
        if ($matches.Count -eq 0) { return $null }

        $candidate = $matches.Item(0)
        if ($null -ne $boundWindowHandle) {
            $candidateHandle = $candidate.Current.NativeWindowHandle
            $candidateRuntimeId = Get-ElementRuntimeId $candidate
            if ($candidateHandle -ne $boundWindowHandle) {
                throw "Nika Core top-level HWND generation changed from $boundWindowHandle to $candidateHandle."
            }
            if (-not [System.Windows.Automation.Automation]::Compare(
                [int[]]$boundWindowRuntimeId,
                [int[]]$candidateRuntimeId
            )) {
                throw 'Nika Core top-level UI Automation RuntimeId generation changed after binding.'
            }
        }
        return $candidate
    }

    # Chromium/WebView2 accessibility providers can appear below native child HWNDs
    # even when the exact host window's initial UIA descendant query is empty. Walk
    # only HWNDs that are descendants of the already PID+title+generation-bound host
    # window, convert them back into UIA elements, and query semantics there. Search
    # roots can overlap, so semantic candidates are later deduplicated only with
    # Automation.Compare (same UIA element identity), never by accessible Name.
    # The proof stays within this exact bound generation without coordinates, another process, or a relaunch.
    function Get-BoundSearchRoots([System.Windows.Automation.AutomationElement]$ExactWindow) {
        $searchRoots = New-Object 'System.Collections.Generic.List[System.Windows.Automation.AutomationElement]'
        Add-UniqueAutomationElement $searchRoots $ExactWindow
        $nativeHandleValue = $ExactWindow.Current.NativeWindowHandle
        if ($nativeHandleValue -eq 0) { return $searchRoots.ToArray() }
        if ($null -ne $boundWindowHandle -and $nativeHandleValue -ne $boundWindowHandle) {
            throw "Bound UI Automation window HWND changed from $boundWindowHandle to $nativeHandleValue."
        }

        $nativeHandle = [System.IntPtr]::new($nativeHandleValue)
        foreach ($childHandle in [NikaUiaNative]::GetDescendantWindows($nativeHandle)) {
            try {
                $childElement = [System.Windows.Automation.AutomationElement]::FromHandle($childHandle)
                if ($null -ne $childElement) {
                    Add-UniqueAutomationElement $searchRoots $childElement
                }
            } catch [System.Windows.Automation.ElementNotAvailableException] {
                throw
            }
        }
        return $searchRoots.ToArray()
    }

    function Get-BoundDescendantNames([System.Windows.Automation.AutomationElement]$ExactWindow) {
        $collected = New-Object 'System.Collections.Generic.List[string]'
        foreach ($searchRoot in (Get-BoundSearchRoots $ExactWindow)) {
            try {
                if ($searchRoot.Current.Name) { $collected.Add($searchRoot.Current.Name) }
                $descendants = $searchRoot.FindAll(
                    [System.Windows.Automation.TreeScope]::Descendants,
                    [System.Windows.Automation.Condition]::TrueCondition
                )
                foreach ($element in $descendants) {
                    try {
                        if ($element.Current.Name) { $collected.Add($element.Current.Name) }
                    } catch [System.Windows.Automation.ElementNotAvailableException] {
                        throw
                    }
                }
            } catch [System.Windows.Automation.ElementNotAvailableException] {
                throw
            }
        }
        return @($collected)
    }

    function Find-BoundDescendantName(
        [System.Windows.Automation.AutomationElement]$ExactWindow,
        [string]$Expected
    ) {
        $condition = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::NameProperty,
            $Expected
        )
        $candidates = New-Object 'System.Collections.Generic.List[System.Windows.Automation.AutomationElement]'
        foreach ($searchRoot in (Get-BoundSearchRoots $ExactWindow)) {
            try {
                if ($searchRoot.Current.Name -eq $Expected) {
                    Add-UniqueAutomationElement $candidates $searchRoot
                }
                $matches = $searchRoot.FindAll(
                    [System.Windows.Automation.TreeScope]::Descendants,
                    $condition
                )
                for ($index = 0; $index -lt $matches.Count; $index++) {
                    Add-UniqueAutomationElement $candidates $matches.Item($index)
                }
            } catch [System.Windows.Automation.ElementNotAvailableException] {
                # Never choose from a partially enumerated candidate set. The caller
                # may retry the entire resolution from fresh bound roots.
                throw
            }
        }

        if ($candidates.Count -gt 1) {
            $runtimeIds = @(
                foreach ($candidate in $candidates) {
                    try { (Get-ElementRuntimeId $candidate) -join '.' }
                    catch [System.Windows.Automation.ElementNotAvailableException] { '<stale>' }
                }
            ) -join ' | '
            throw "Multiple distinct UI Automation descendants matched exact accessible name '$Expected'. RuntimeIds: $runtimeIds"
        }
        if ($candidates.Count -eq 1) { return $candidates.Item(0) }
        return $null
    }

    function New-BoundControlIdentity(
        [System.Windows.Automation.AutomationElement]$Element,
        [string]$ExpectedName
    ) {
        $runtimeId = Get-ElementRuntimeId $Element
        return [pscustomobject]@{
            ExpectedName = $ExpectedName
            RuntimeId = [int[]]$runtimeId
            ProcessId = $expectedProcessId
            ProcessStartTicks = $expectedProcessStartTicks
            ExecutablePath = $expectedExecutablePath
            WindowHandle = $boundWindowHandle
            WindowRuntimeId = [int[]]$boundWindowRuntimeId
            Element = $Element
        }
    }

    function Resolve-BoundControlIdentity($Identity) {
        Assert-BoundProcessGeneration
        if ($Identity.ProcessId -ne $expectedProcessId -or
            $Identity.ProcessStartTicks -ne $expectedProcessStartTicks -or
            -not [string]::Equals(
                $Identity.ExecutablePath,
                $expectedExecutablePath,
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
            throw "Control '$($Identity.ExpectedName)' belongs to a different process generation."
        }
        if ($Identity.WindowHandle -ne $boundWindowHandle -or
            -not [System.Windows.Automation.Automation]::Compare(
                [int[]]$Identity.WindowRuntimeId,
                [int[]]$boundWindowRuntimeId
            )) {
            throw "Control '$($Identity.ExpectedName)' belongs to a different top-level window generation."
        }

        $currentWindow = Find-ExactWindow
        if ($null -eq $currentWindow) {
            throw "Bound top-level window disappeared while resolving '$($Identity.ExpectedName)'."
        }
        $resolved = Find-BoundDescendantName $currentWindow $Identity.ExpectedName
        if ($null -eq $resolved) {
            throw "Bound UI Automation control '$($Identity.ExpectedName)' disappeared."
        }
        $resolvedRuntimeId = Get-ElementRuntimeId $resolved
        if (-not [System.Windows.Automation.Automation]::Compare(
            [int[]]$Identity.RuntimeId,
            [int[]]$resolvedRuntimeId
        )) {
            throw "UI Automation control '$($Identity.ExpectedName)' was re-resolved to a different RuntimeId after becoming stale or being replaced."
        }
        $Identity.Element = $resolved
        return $resolved
    }

    $window = $null
    while ($startupWatch.Elapsed -lt $startupTimeout -and $null -eq $window) {
        Assert-BoundProcessGeneration
        $window = Find-ExactWindow
        if ($null -eq $window) { Start-Sleep -Milliseconds 250 }
    }
    if ($null -eq $window) {
        $elapsed = [Math]::Round($startupWatch.Elapsed.TotalSeconds, 1)
        throw "Nika Core top-level window '$WindowTitle' for process $expectedProcessId was not found within the bounded $StartupTimeoutSeconds-second startup deadline (elapsed ${elapsed}s)."
    }

    $boundWindowHandle = $window.Current.NativeWindowHandle
    if ($boundWindowHandle -eq 0) {
        throw "Nika Core top-level UI Automation window '$WindowTitle' did not expose a native HWND."
    }
    $boundWindowRuntimeId = Get-ElementRuntimeId $window
    # Re-resolve immediately through the strict path so the captured PID/start/exe/HWND/
    # RuntimeId tuple is proven before any descendant identity is accepted.
    $window = Find-ExactWindow

    $names = @()
    $missing = $requiredNames
    while ($startupWatch.Elapsed -lt $startupTimeout -and $missing.Count -gt 0) {
        Assert-BoundProcessGeneration
        $window = Find-ExactWindow
        if ($null -ne $window) {
            try {
                $names = Get-BoundDescendantNames $window
                $missing = @($requiredNames | Where-Object { $names -notcontains $_ })
            } catch [System.Windows.Automation.ElementNotAvailableException] {
                # Discard the incomplete snapshot and retry all bound roots.
                $names = @()
                $missing = $requiredNames
            }
        }
        if ($missing.Count -gt 0) { Start-Sleep -Milliseconds 500 }
    }

    if ($missing.Count -gt 0) {
        $elapsed = [Math]::Round($startupWatch.Elapsed.TotalSeconds, 1)
        $preview = ($names | Select-Object -Unique | Select-Object -First 80) -join ' | '
        throw "WebView2 UIA descendants were not discoverable within the bounded $StartupTimeoutSeconds-second startup deadline (elapsed ${elapsed}s). Missing: $($missing -join ', '). Seen: $preview"
    }

    $semanticElapsed = [Math]::Round($startupWatch.Elapsed.TotalSeconds, 1)
    Write-Host "Required packaged WebView2 UIA semantics became discoverable after ${semanticElapsed}s."

    function Wait-DescendantName([string]$Expected, [int]$Attempts = 80) {
        for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
            Start-Sleep -Milliseconds 250
            Assert-BoundProcessGeneration
            $currentWindow = Find-ExactWindow
            if ($null -eq $currentWindow) { continue }
            try {
                $element = Find-BoundDescendantName $currentWindow $Expected
                if ($null -ne $element) {
                    return New-BoundControlIdentity $element $Expected
                }
            } catch [System.Windows.Automation.ElementNotAvailableException] {
                # An incomplete enumeration is never accepted. Retry from fresh roots.
                continue
            }
        }
        throw "Expected unique UI Automation descendant '$Expected' did not appear."
    }

    function Wait-FocusName($ExpectedControl) {
        for ($attempt = 0; $attempt -lt 20; $attempt++) {
            Start-Sleep -Milliseconds 250
            try {
                $target = Resolve-BoundControlIdentity $ExpectedControl
                $focused = [System.Windows.Automation.AutomationElement]::FocusedElement
                if ($null -eq $focused) { continue }
                $focusedRuntimeId = [int[]]$focused.GetRuntimeId()
                if ($null -eq $focusedRuntimeId -or $focusedRuntimeId.Length -eq 0) { continue }
                $sameRuntimeId = [System.Windows.Automation.Automation]::Compare(
                    [int[]]$ExpectedControl.RuntimeId,
                    [int[]]$focusedRuntimeId
                )
                $sameElement = [System.Windows.Automation.Automation]::Compare($target, $focused)
                if ($sameRuntimeId -and $sameElement) { return }
            } catch [System.Windows.Automation.ElementNotAvailableException] {
                # A provider transition can stale either the stored element reference,
                # a search root, or FocusedElement. Retry only after resolving the same
                # captured RuntimeId/process/window generation from a fresh full search.
                continue
            }
        }
        $actual = [System.Windows.Automation.AutomationElement]::FocusedElement
        $actualName = '<none>'
        $actualRuntimeId = '<none>'
        if ($null -ne $actual) {
            try {
                $actualName = $actual.Current.Name
                $actualRuntimeId = (Get-ElementRuntimeId $actual) -join '.'
            } catch [System.Windows.Automation.ElementNotAvailableException] {
                $actualName = '<stale>'
                $actualRuntimeId = '<stale>'
            }
        }
        throw "Expected keyboard focus on exact '$($ExpectedControl.ExpectedName)' RuntimeId '$($ExpectedControl.RuntimeId -join '.')', got '$actualName' RuntimeId '$actualRuntimeId'."
    }

    function Set-BoundControlFocus($Control) {
        for ($attempt = 0; $attempt -lt 2; $attempt++) {
            try {
                $target = Resolve-BoundControlIdentity $Control
                $target.SetFocus()
                Wait-FocusName $Control
                return
            } catch [System.Windows.Automation.ElementNotAvailableException] {
                if ($attempt -eq 1) { throw }
            }
        }
    }

    # The DOM can be visible in UIA before the asynchronous pywebview JS API call
    # has returned the Action Registry/keymap. Wait for the application's explicit
    # ready status so this gate tests keyboard behavior rather than an initialization race.
    Wait-DescendantName 'Nika Core готова до роботи.' | Out-Null

    $startControl = Wait-DescendantName 'Створити завдання'
    $tasksControl = Wait-DescendantName 'Завдання'
    $commandControl = Wait-DescendantName 'Що має зробити Nika?'

    Set-BoundControlFocus $startControl
    [System.Windows.Forms.SendKeys]::SendWait('%1')
    Wait-FocusName $tasksControl
    [System.Windows.Forms.SendKeys]::SendWait('^+p')
    Wait-FocusName $commandControl

    Write-Host 'WebView2 UI Automation descendants, exact semantic identity, and keyboard/focus flow verified successfully.'
    Write-Host (($names | Select-Object -Unique | Select-Object -First 40) -join ' | ')
}
finally {
    if ($null -ne $startupWatch) { $startupWatch.Stop() }
    if ($null -ne $process -and !$process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
    if ($null -eq $previousWebView2BrowserArgs) {
        Remove-Item Env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS -ErrorAction SilentlyContinue
    } else {
        $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = $previousWebView2BrowserArgs
    }
}
