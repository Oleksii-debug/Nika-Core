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
try {
    $process = Start-Process -FilePath $ExePath -PassThru
    $startupWatch = [System.Diagnostics.Stopwatch]::StartNew()
    $startupTimeout = [System.TimeSpan]::FromSeconds($StartupTimeoutSeconds)
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $nameCondition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty,
        $WindowTitle
    )
    $processCondition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ProcessIdProperty,
        $process.Id
    )
    $windowCondition = [System.Windows.Automation.AndCondition]::new(
        [System.Windows.Automation.Condition[]]@($nameCondition, $processCondition)
    )

    function Find-ExactWindow {
        $matches = $root.FindAll(
            [System.Windows.Automation.TreeScope]::Children,
            $windowCondition
        )
        if ($matches.Count -gt 1) {
            throw "Multiple Nika Core top-level UI Automation windows matched title '$WindowTitle' and process $($process.Id)."
        }
        if ($matches.Count -eq 1) { return $matches.Item(0) }
        return $null
    }

    # Chromium/WebView2 accessibility providers can appear below native child HWNDs
    # even when the exact host window's initial UIA descendant query is empty. Walk
    # only HWNDs that are descendants of the already PID+title-bound host window,
    # convert them back into UIA elements, and query semantics there. This activates
    # providers without coordinates, another process, or a relaunch. Hosted cold
    # starts are given one explicit bounded deadline rather than two brittle fixed
    # attempt windows that can expire at the provider-startup boundary.
    function Get-BoundSearchRoots([System.Windows.Automation.AutomationElement]$ExactWindow) {
        $searchRoots = New-Object 'System.Collections.Generic.List[System.Windows.Automation.AutomationElement]'
        $searchRoots.Add($ExactWindow)
        $nativeHandleValue = $ExactWindow.Current.NativeWindowHandle
        if ($nativeHandleValue -eq 0) { return $searchRoots }

        $nativeHandle = [System.IntPtr]::new($nativeHandleValue)
        foreach ($childHandle in [NikaUiaNative]::GetDescendantWindows($nativeHandle)) {
            try {
                $childElement = [System.Windows.Automation.AutomationElement]::FromHandle($childHandle)
                if ($null -ne $childElement) { $searchRoots.Add($childElement) }
            } catch [System.Windows.Automation.ElementNotAvailableException] { }
        }
        return $searchRoots
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
                    } catch [System.Windows.Automation.ElementNotAvailableException] { }
                }
            } catch [System.Windows.Automation.ElementNotAvailableException] { }
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
        foreach ($searchRoot in (Get-BoundSearchRoots $ExactWindow)) {
            try {
                if ($searchRoot.Current.Name -eq $Expected) { return $searchRoot }
                $element = $searchRoot.FindFirst(
                    [System.Windows.Automation.TreeScope]::Descendants,
                    $condition
                )
                if ($null -ne $element) { return $element }
            } catch [System.Windows.Automation.ElementNotAvailableException] { }
        }
        return $null
    }

    $window = $null
    while ($startupWatch.Elapsed -lt $startupTimeout -and $null -eq $window) {
        if ($process.HasExited) {
            throw "Nika Core exited before the top-level UI Automation window appeared. Exit code: $($process.ExitCode)"
        }
        $window = Find-ExactWindow
        if ($null -eq $window) { Start-Sleep -Milliseconds 250 }
    }
    if ($null -eq $window) {
        $elapsed = [Math]::Round($startupWatch.Elapsed.TotalSeconds, 1)
        throw "Nika Core top-level window '$WindowTitle' for process $($process.Id) was not found within the bounded $StartupTimeoutSeconds-second startup deadline (elapsed ${elapsed}s)."
    }

    $names = @()
    $missing = $requiredNames
    while ($startupWatch.Elapsed -lt $startupTimeout -and $missing.Count -gt 0) {
        if ($process.HasExited) {
            throw "Nika Core exited while waiting for WebView2 accessibility descendants. Exit code: $($process.ExitCode)"
        }
        $window = Find-ExactWindow
        if ($null -ne $window) {
            $names = Get-BoundDescendantNames $window
            $missing = @($requiredNames | Where-Object { $names -notcontains $_ })
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
            if ($process.HasExited) {
                throw "Nika Core exited while waiting for '$Expected'. Exit code: $($process.ExitCode)"
            }
            $currentWindow = Find-ExactWindow
            if ($null -eq $currentWindow) { continue }
            $element = Find-BoundDescendantName $currentWindow $Expected
            if ($null -ne $element) { return $element }
        }
        throw "Expected UI Automation descendant '$Expected' did not appear."
    }

    function Wait-FocusName([string]$Expected) {
        for ($attempt = 0; $attempt -lt 20; $attempt++) {
            Start-Sleep -Milliseconds 250
            $focused = [System.Windows.Automation.AutomationElement]::FocusedElement
            if ($null -ne $focused -and $focused.Current.Name -eq $Expected) { return }
        }
        $actual = [System.Windows.Automation.AutomationElement]::FocusedElement
        $actualName = if ($null -eq $actual) { '<none>' } else { $actual.Current.Name }
        throw "Expected keyboard focus '$Expected', got '$actualName'."
    }

    # The DOM can be visible in UIA before the asynchronous pywebview JS API call
    # has returned the Action Registry/keymap. Wait for the application's explicit
    # ready status so this gate tests keyboard behavior rather than an initialization race.
    Wait-DescendantName 'Nika Core готова до роботи.' | Out-Null

    $startControl = Wait-DescendantName 'Створити завдання'
    $startControl.SetFocus()
    Wait-FocusName 'Створити завдання'

    [System.Windows.Forms.SendKeys]::SendWait('%1')
    Wait-FocusName 'Завдання'
    [System.Windows.Forms.SendKeys]::SendWait('^+p')
    Wait-FocusName 'Що має зробити Nika?'

    Write-Host 'WebView2 UI Automation descendants, bridge readiness, and keyboard/focus flow verified successfully.'
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
