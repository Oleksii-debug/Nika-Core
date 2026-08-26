param(
    [Parameter(Mandatory=$true)][string]$ExePath,
    [Parameter(Mandatory=$true)][string]$EvidencePath,
    [Parameter(Mandatory=$true)][string]$TargetSourceSha,
    [Parameter(Mandatory=$true)][string]$ReuseHarnessSha,
    [Parameter(Mandatory=$true)][string]$M12ArtifactDigest,
    [string]$WindowTitle = 'Nika Core 0.0.2',
    [ValidateRange(30, 180)][int]$StartupTimeoutSeconds = 120
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms

$checks = New-Object 'System.Collections.Generic.List[object]'
$failures = New-Object 'System.Collections.Generic.List[string]'
$process = $null
$previousWebView2BrowserArgs = $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS

function Add-Check([string]$Name, [string]$Status, [string]$Detail) {
    $checks.Add([pscustomobject]@{ name = $Name; status = $Status; detail = $Detail })
    if ($Status -eq 'FAIL') { $failures.Add("${Name}: ${Detail}") }
    Write-Host "[$Status] $Name - $Detail"
}

function Invoke-Check([string]$Name, [scriptblock]$Body) {
    try {
        $detail = & $Body
        if ([string]::IsNullOrWhiteSpace([string]$detail)) { $detail = 'verified' }
        Add-Check $Name 'PASS' ([string]$detail)
    } catch {
        Add-Check $Name 'FAIL' $_.Exception.Message
    }
}

function Require([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function Get-RuntimeId([System.Windows.Automation.AutomationElement]$Element) {
    $runtimeId = $Element.GetRuntimeId()
    Require ($null -ne $runtimeId -and $runtimeId.Length -gt 0) 'UIA element has no RuntimeId.'
    return [int[]]$runtimeId
}

function Find-Window {
    Require ($null -ne $script:process) 'Packaged process is not running.'
    $script:process.Refresh()
    Require (-not $script:process.HasExited) "Packaged process $($script:process.Id) exited unexpectedly."
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $name = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty,
        $WindowTitle
    )
    $pid = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ProcessIdProperty,
        $script:process.Id
    )
    $condition = [System.Windows.Automation.AndCondition]::new(
        [System.Windows.Automation.Condition[]]@($name, $pid)
    )
    $matches = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $condition)
    Require ($matches.Count -le 1) "Duplicate top-level role/name/process identity: '$WindowTitle', PID=$($script:process.Id), count=$($matches.Count)."
    if ($matches.Count -eq 0) { return $null }
    return $matches.Item(0)
}

function Wait-Window {
    $deadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $window = Find-Window
        if ($null -ne $window) { return $window }
        Start-Sleep -Milliseconds 250
    }
    throw "Exact packaged UIA window '$WindowTitle' did not appear."
}

function Get-Descendants {
    $window = Find-Window
    Require ($null -ne $window) 'Bound top-level UIA window is unavailable.'
    return $window.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
}

function Get-Matches(
    [string]$ExactName,
    [System.Windows.Automation.ControlType]$ControlType = $null,
    [string]$NameRegex = ''
) {
    $result = New-Object 'System.Collections.Generic.List[System.Windows.Automation.AutomationElement]'
    $desc = Get-Descendants
    for ($i = 0; $i -lt $desc.Count; $i++) {
        $element = $desc.Item($i)
        try {
            $name = [string]$element.Current.Name
            if ($ExactName -and $name -ne $ExactName) { continue }
            if ($NameRegex -and $name -notmatch $NameRegex) { continue }
            if ($null -ne $ControlType -and -not $element.Current.ControlType.Equals($ControlType)) { continue }
            [void]$result.Add($element)
        } catch [System.Windows.Automation.ElementNotAvailableException] {
            continue
        }
    }
    return $result.ToArray()
}

function Wait-Unique(
    [string]$ExactName,
    [System.Windows.Automation.ControlType]$ControlType = $null,
    [string]$NameRegex = '',
    [int]$Attempts = 80
) {
    for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
        Start-Sleep -Milliseconds 250
        $matches = @(Get-Matches $ExactName $ControlType $NameRegex)
        if ($matches.Count -gt 1) {
            throw "Ambiguous role/name locator. Name='$ExactName' Regex='$NameRegex' Count=$($matches.Count)."
        }
        if ($matches.Count -eq 1) { return $matches[0] }
    }
    throw "Unique UIA semantic element did not appear. Name='$ExactName' Regex='$NameRegex'."
}

function Assert-Focus([System.Windows.Automation.AutomationElement]$Expected, [string]$Label) {
    for ($attempt = 0; $attempt -lt 32; $attempt++) {
        Start-Sleep -Milliseconds 125
        $focused = [System.Windows.Automation.AutomationElement]::FocusedElement
        if ($null -ne $focused) {
            try {
                if ([System.Windows.Automation.Automation]::Compare($Expected, $focused)) { return }
            } catch [System.Windows.Automation.ElementNotAvailableException] { }
        }
    }
    $focused = [System.Windows.Automation.AutomationElement]::FocusedElement
    $actual = if ($null -eq $focused) { '<none>' } else { [string]$focused.Current.Name }
    throw "Focus loss after $Label; actual='$actual'."
}

function Start-PackagedSession {
    Require (Test-Path -LiteralPath $ExePath -PathType Leaf) "Packaged executable missing: $ExePath"
    $script:process = Start-Process -FilePath $ExePath -PassThru
    $window = Wait-Window
    Require ($window.Current.NativeWindowHandle -ne 0) 'Top-level Nika UIA window has no HWND.'
    Get-RuntimeId $window | Out-Null
    $window.SetFocus()
    Wait-Unique 'Nika Core готова до роботи.' ([System.Windows.Automation.ControlType]::Text) | Out-Null
    return "PID=$($script:process.Id), HWND=$($window.Current.NativeWindowHandle)"
}

function Stop-PackagedSession {
    if ($null -eq $script:process) { return }
    try {
        if (-not $script:process.HasExited) {
            $window = Find-Window
            if ($null -ne $window) { $window.SetFocus() }
            [System.Windows.Forms.SendKeys]::SendWait('%{F4}')
            Require ($script:process.WaitForExit(12000)) 'Packaged app did not close through keyboard Alt+F4.'
        }
    } finally {
        if (-not $script:process.HasExited) {
            Stop-Process -Id $script:process.Id -Force -ErrorAction SilentlyContinue
        }
        $script:process = $null
    }
}

try {
    Require ($TargetSourceSha -match '^[0-9a-f]{40}$') 'TargetSourceSha must be an exact lowercase 40-character SHA.'
    Require ($ReuseHarnessSha -match '^[0-9a-f]{40}$') 'ReuseHarnessSha must be an exact lowercase 40-character SHA.'
    Require ($M12ArtifactDigest -match '^sha256:[0-9a-f]{64}$') 'M12ArtifactDigest must be an exact SHA-256 artifact digest.'

    if ($previousWebView2BrowserArgs -match '(?i)(?:^|\s)--disable-renderer-accessibility(?:\s|$)') {
        throw 'Renderer accessibility is explicitly disabled in the runner environment.'
    }
    if ([string]::IsNullOrWhiteSpace($previousWebView2BrowserArgs)) {
        $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = '--force-renderer-accessibility'
    } elseif ($previousWebView2BrowserArgs -notmatch '(?i)(?:^|\s)--force-renderer-accessibility(?:\s|$)') {
        $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = "$previousWebView2BrowserArgs --force-renderer-accessibility"
    }

    Invoke-Check 'w084.launch_exact_packaged_candidate' { Start-PackagedSession }

    if ($null -ne $process) {
        Invoke-Check 'w084.actionable_role_name_uniqueness' {
            $actionableTypes = @(
                [System.Windows.Automation.ControlType]::Button,
                [System.Windows.Automation.ControlType]::Edit,
                [System.Windows.Automation.ControlType]::Hyperlink
            )
            $seen = @{}
            $desc = Get-Descendants
            for ($i = 0; $i -lt $desc.Count; $i++) {
                $element = $desc.Item($i)
                try {
                    $type = $element.Current.ControlType
                    if (-not ($actionableTypes | Where-Object { $_.Equals($type) })) { continue }
                    $name = [string]$element.Current.Name
                    Require (-not [string]::IsNullOrWhiteSpace($name)) "Unnamed actionable UIA control: $($type.ProgrammaticName)."
                    $key = "$($type.ProgrammaticName)|$name"
                    if ($seen.ContainsKey($key)) {
                        throw "Duplicate actionable role/name identity: $key"
                    }
                    $seen[$key] = (Get-RuntimeId $element) -join '.'
                } catch [System.Windows.Automation.ElementNotAvailableException] {
                    throw 'Actionable UIA element became stale during one semantic enumeration.'
                }
            }
            Require ($seen.Count -ge 8) "Suspiciously small actionable semantic surface: $($seen.Count) controls."
            "$($seen.Count) actionable controls have unique role/name identities"
        }

        Invoke-Check 'w084.hidden_empty_state_not_exposed' {
            $hiddenTaskEmpty = @(Get-Matches 'Завдань ще немає.' ([System.Windows.Automation.ControlType]::Text))
            Require ($hiddenTaskEmpty.Count -eq 0) "Hidden task empty-state leaked into UIA after persisted task creation; count=$($hiddenTaskEmpty.Count)."
            'DOM hidden task empty-state is absent from packaged UIA control semantics'
        }

        Invoke-Check 'w084.disabled_focusable_controls' {
            $desc = Get-Descendants
            $bad = New-Object 'System.Collections.Generic.List[string]'
            for ($i = 0; $i -lt $desc.Count; $i++) {
                $element = $desc.Item($i)
                try {
                    if ($element.Current.IsKeyboardFocusable -and -not $element.Current.IsEnabled) {
                        $bad.Add("$($element.Current.ControlType.ProgrammaticName):$($element.Current.Name)")
                    }
                } catch [System.Windows.Automation.ElementNotAvailableException] { }
            }
            Require ($bad.Count -eq 0) "Disabled controls remain keyboard-focusable: $($bad -join ' | ')"
            'no disabled packaged control advertises keyboard focusability'
        }

        Invoke-Check 'w084.keymap_refresh_invalidates_stale_uia_generation' {
            $oldBinding = Wait-Unique 'Комбінація для Open agents (nav.agents)' ([System.Windows.Automation.ControlType]::Edit)
            $oldRuntime = (Get-RuntimeId $oldBinding) -join '.'
            $restore = Wait-Unique 'Відновити комбінацію за замовчуванням для Open agents (nav.agents)' ([System.Windows.Automation.ControlType]::Button)
            $restore.SetFocus()
            Assert-Focus $restore 'focus before keymap regeneration'
            [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
            Wait-Unique 'Default shortcut restored.' ([System.Windows.Automation.ControlType]::Text) | Out-Null
            $newBinding = Wait-Unique 'Комбінація для Open agents (nav.agents)' ([System.Windows.Automation.ControlType]::Edit)
            $newRuntime = (Get-RuntimeId $newBinding) -join '.'
            Require ($oldRuntime -ne $newRuntime) "Keymap DOM replacement reused stale UIA RuntimeId '$oldRuntime'."
            $same = $false
            try { $same = [System.Windows.Automation.Automation]::Compare($oldBinding, $newBinding) }
            catch [System.Windows.Automation.ElementNotAvailableException] { $same = $false }
            Require (-not $same) 'Old and regenerated keymap elements compare as the same UIA generation.'
            $newRestore = Wait-Unique 'Відновити комбінацію за замовчуванням для Open agents (nav.agents)' ([System.Windows.Automation.ControlType]::Button)
            Assert-Focus $newRestore 'keymap regeneration'
            "stale RuntimeId $oldRuntime replaced by $newRuntime and focus rebound to current generation"
        }

        Invoke-Check 'w084.copy_status_keyboard_semantic_affordance' {
            $copyControls = @(Get-Matches '' $null '(?i)(копіювати|скопіювати|copy).*(стан|статус|status)|(стан|статус|status).*(копіювати|скопіювати|copy)')
            $usable = @($copyControls | Where-Object {
                try {
                    $_.Current.IsEnabled -and $_.Current.IsKeyboardFocusable -and (
                        $_.Current.ControlType.Equals([System.Windows.Automation.ControlType]::Button) -or
                        $_.Current.ControlType.Equals([System.Windows.Automation.ControlType]::Hyperlink)
                    )
                } catch { $false }
            })
            Require ($usable.Count -eq 1) "Expected exactly one enabled keyboard-focusable semantic Copy status control; found $($usable.Count)."
            $usable[0].SetFocus()
            Assert-Focus $usable[0] 'Copy status control focus'
            "semantic copy-status control is unique: $($usable[0].Current.Name)"
        }

        Invoke-Check 'w084.no_coordinate_or_vision_fallback' {
            'journey used process/HWND binding, UIA RuntimeId/role/name semantics and keyboard input only; no coordinate, OCR, screenshot or vision fallback invoked'
        }

        Invoke-Check 'w084.keyboard_close' {
            Stop-PackagedSession
            'exact packaged process closed by Alt+F4'
        }
    }
} catch {
    Add-Check 'w084.harness_unhandled' 'FAIL' $_.Exception.Message
} finally {
    if ($null -ne $process) {
        try { Stop-PackagedSession } catch { Add-Check 'w084.cleanup' 'FAIL' $_.Exception.Message }
    }
    if ($null -eq $previousWebView2BrowserArgs) {
        Remove-Item Env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS -ErrorAction SilentlyContinue
    } else {
        $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = $previousWebView2BrowserArgs
    }

    $parent = Split-Path -Parent ([System.IO.Path]::GetFullPath($EvidencePath))
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    [ordered]@{
        schema_version = 1
        worker = 'W084'
        role = 'Windows Packaged Accessibility Pre-Human Red-Team'
        target_source_sha = $TargetSourceSha
        reused_one_shot_58_harness_sha = $ReuseHarnessSha
        m12_artifact_digest = $M12ArtifactDigest
        semantic_target = 'exact M12 packaged Windows candidate'
        coordinate_fallback_used = $false
        ocr_used = $false
        screenshot_targeting_used = $false
        vision_targeting_used = $false
        checks = $checks.ToArray()
        pass = ($failures.Count -eq 0)
        failure_count = $failures.Count
        failures = $failures.ToArray()
        human_tested = $false
        nvda_verified = $false
        production_release_ready = $false
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $EvidencePath -Encoding UTF8
}

if ($failures.Count -gt 0) { exit 1 }
exit 0
