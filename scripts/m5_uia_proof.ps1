param(
    [Parameter(Mandatory=$true)][string]$ExePath,
    [string]$WindowTitle = 'Nika Core M5 Proof'
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms

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
try {
    $process = Start-Process -FilePath $ExePath -PassThru
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

    $window = $null
    for ($attempt = 0; $attempt -lt 40 -and $null -eq $window; $attempt++) {
        Start-Sleep -Milliseconds 500
        if ($process.HasExited) {
            throw "Nika Core exited before the top-level UI Automation window appeared. Exit code: $($process.ExitCode)"
        }
        $window = Find-ExactWindow
    }
    if ($null -eq $window) {
        throw "Nika Core top-level window '$WindowTitle' for process $($process.Id) not found in UI Automation tree."
    }

    $names = @()
    $missing = $requiredNames
    for ($attempt = 0; $attempt -lt 40 -and $missing.Count -gt 0; $attempt++) {
        Start-Sleep -Milliseconds 500
        if ($process.HasExited) {
            throw "Nika Core exited while waiting for WebView2 accessibility descendants. Exit code: $($process.ExitCode)"
        }
        $window = Find-ExactWindow
        if ($null -eq $window) { continue }
        try {
            $descendants = $window.FindAll(
                [System.Windows.Automation.TreeScope]::Descendants,
                [System.Windows.Automation.Condition]::TrueCondition
            )
        } catch [System.Windows.Automation.ElementNotAvailableException] {
            $window = $null
            continue
        }
        $names = @()
        foreach ($element in $descendants) {
            try {
                if ($element.Current.Name) { $names += $element.Current.Name }
            } catch [System.Windows.Automation.ElementNotAvailableException] { }
        }
        $missing = @($requiredNames | Where-Object { $names -notcontains $_ })
    }

    if ($missing.Count -gt 0) {
        $preview = ($names | Select-Object -Unique | Select-Object -First 80) -join ' | '
        throw "WebView2 UIA descendants were not discoverable. Missing: $($missing -join ', '). Seen: $preview"
    }

    function Wait-DescendantName([string]$Expected, [int]$Attempts = 40) {
        $condition = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::NameProperty,
            $Expected
        )
        for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
            Start-Sleep -Milliseconds 250
            if ($process.HasExited) {
                throw "Nika Core exited while waiting for '$Expected'. Exit code: $($process.ExitCode)"
            }
            $currentWindow = Find-ExactWindow
            if ($null -eq $currentWindow) { continue }
            try {
                $element = $currentWindow.FindFirst(
                    [System.Windows.Automation.TreeScope]::Descendants,
                    $condition
                )
            } catch [System.Windows.Automation.ElementNotAvailableException] {
                continue
            }
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
    if ($null -ne $process -and !$process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
    if ($null -eq $previousWebView2BrowserArgs) {
        Remove-Item Env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS -ErrorAction SilentlyContinue
    } else {
        $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = $previousWebView2BrowserArgs
    }
}
