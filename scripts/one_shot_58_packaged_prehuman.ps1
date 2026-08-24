param(
    [Parameter(Mandatory=$true)][string]$ExePath,
    [Parameter(Mandatory=$true)][string]$BundleDir,
    [Parameter(Mandatory=$true)][string]$DatabasePath,
    [Parameter(Mandatory=$true)][string]$TargetSourceSha,
    [Parameter(Mandatory=$true)][string]$HarnessSha,
    [Parameter(Mandatory=$true)][string]$EvidencePath,
    [string]$WindowTitle = 'Nika Core 0.1.0',
    [ValidateRange(30, 180)][int]$StartupTimeoutSeconds = 120
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public static class NikaOneShot58Native
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

$checks = New-Object 'System.Collections.Generic.List[object]'
$failures = New-Object 'System.Collections.Generic.List[string]'
$session = $null
$previousWebView2BrowserArgs = $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS
$previousDbPath = $env:NIKA_DB_PATH
$forceRendererAccessibilityArg = '--force-renderer-accessibility'
$productCommand = 'Create accessible expense application'
$localGoal = 'prehuman deterministic local ping'
$productId = $null
$approvalState = 'not_evaluated'

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
        return $true
    } catch {
        Add-Check $Name 'FAIL' $_.Exception.Message
        return $false
    }
}

function Require([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function Get-FullPath([string]$PathValue) {
    return [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $PathValue).Path)
}

function Get-ElementRuntimeId([System.Windows.Automation.AutomationElement]$Element) {
    $runtimeId = $Element.GetRuntimeId()
    if ($null -eq $runtimeId -or $runtimeId.Length -eq 0) {
        throw 'UI Automation element did not expose a RuntimeId.'
    }
    return [int[]]$runtimeId
}

function Add-UniqueElement($List, [System.Windows.Automation.AutomationElement]$Candidate) {
    foreach ($existing in $List) {
        try {
            if ([System.Windows.Automation.Automation]::Compare($existing, $Candidate)) { return }
        } catch [System.Windows.Automation.ElementNotAvailableException] {
            throw
        }
    }
    [void]$List.Add($Candidate)
}

function Get-ProcessExecutablePath([System.Diagnostics.Process]$Process, [int]$Attempts = 40) {
    for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
        $Process.Refresh()
        if ($Process.HasExited) {
            throw "Nika process $($Process.Id) exited before executable identity was readable."
        }
        try {
            $mainModule = $Process.MainModule
            if ($null -ne $mainModule -and -not [string]::IsNullOrWhiteSpace([string]$mainModule.FileName)) {
                return [System.IO.Path]::GetFullPath([string]$mainModule.FileName)
            }
        } catch [System.InvalidOperationException] {
            # The process can exist briefly before MainModule is readable. Retry within the bound.
        } catch [System.ComponentModel.Win32Exception] {
            # Treat transient module-query failure as startup uncertainty; never skip identity validation.
        }
        Start-Sleep -Milliseconds 100
    }
    throw "Nika process $($Process.Id) did not expose executable identity within the bounded startup window."
}

function Assert-ProcessGeneration {
    if ($null -eq $script:session) { throw 'No active packaged Nika session.' }
    $process = $script:session.Process
    $process.Refresh()
    if ($process.HasExited) { throw "Nika process $($script:session.ProcessId) exited unexpectedly." }
    if ($process.Id -ne $script:session.ProcessId) { throw 'Nika PID identity changed.' }
    if ($process.StartTime.ToUniversalTime().Ticks -ne $script:session.StartTicks) {
        throw 'Nika PID was rebound to another process generation.'
    }
    $currentExe = Get-ProcessExecutablePath $process
    if (-not [string]::Equals($currentExe, $script:session.ExePath, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Nika executable identity changed to '$currentExe'."
    }
}

function Find-ExactWindow {
    Assert-ProcessGeneration
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $nameCondition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty,
        $WindowTitle
    )
    $processCondition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ProcessIdProperty,
        $script:session.ProcessId
    )
    $condition = [System.Windows.Automation.AndCondition]::new(
        [System.Windows.Automation.Condition[]]@($nameCondition, $processCondition)
    )
    $matches = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $condition)
    if ($matches.Count -gt 1) {
        throw "Multiple top-level Nika windows matched exact title/PID. Count=$($matches.Count)."
    }
    if ($matches.Count -eq 0) { return $null }
    $candidate = $matches.Item(0)
    if ($null -ne $script:session.WindowHandle) {
        if ($candidate.Current.NativeWindowHandle -ne $script:session.WindowHandle) {
            throw 'Top-level Nika HWND generation changed.'
        }
        $rid = Get-ElementRuntimeId $candidate
        if (-not [System.Windows.Automation.Automation]::Compare([int[]]$script:session.WindowRuntimeId, [int[]]$rid)) {
            throw 'Top-level Nika RuntimeId generation changed.'
        }
    }
    return $candidate
}

function Get-BoundRoots {
    $window = Find-ExactWindow
    if ($null -eq $window) { throw 'Bound Nika top-level UIA window is unavailable.' }
    $roots = New-Object 'System.Collections.Generic.List[System.Windows.Automation.AutomationElement]'
    Add-UniqueElement $roots $window
    $handle = $window.Current.NativeWindowHandle
    if ($handle -eq 0) { throw 'Bound Nika window has no native HWND.' }
    foreach ($childHandle in [NikaOneShot58Native]::GetDescendantWindows([IntPtr]::new($handle))) {
        try {
            $element = [System.Windows.Automation.AutomationElement]::FromHandle($childHandle)
            if ($null -ne $element) { Add-UniqueElement $roots $element }
        } catch [System.Windows.Automation.ElementNotAvailableException] {
            throw
        }
    }
    return $roots.ToArray()
}

function Get-UniqueCandidates(
    [string]$ExactName,
    [System.Windows.Automation.ControlType]$ControlType = $null,
    [string]$NameRegex = ''
) {
    $result = New-Object 'System.Collections.Generic.List[System.Windows.Automation.AutomationElement]'
    foreach ($root in (Get-BoundRoots)) {
        $elements = New-Object 'System.Collections.Generic.List[System.Windows.Automation.AutomationElement]'
        [void]$elements.Add($root)
        $desc = $root.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            [System.Windows.Automation.Condition]::TrueCondition
        )
        for ($i = 0; $i -lt $desc.Count; $i++) { [void]$elements.Add($desc.Item($i)) }
        foreach ($element in $elements) {
            try {
                $name = [string]$element.Current.Name
                if ($ExactName -and $name -ne $ExactName) { continue }
                if ($NameRegex -and $name -notmatch $NameRegex) { continue }
                if ($null -ne $ControlType -and -not $element.Current.ControlType.Equals($ControlType)) { continue }
                Add-UniqueElement $result $element
            } catch [System.Windows.Automation.ElementNotAvailableException] {
                throw
            }
        }
    }
    return $result.ToArray()
}

function Wait-UniqueElement(
    [string]$ExactName,
    [System.Windows.Automation.ControlType]$ControlType = $null,
    [string]$NameRegex = '',
    [int]$Attempts = 80
) {
    for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
        Start-Sleep -Milliseconds 250
        Assert-ProcessGeneration
        try {
            $matches = @(Get-UniqueCandidates $ExactName $ControlType $NameRegex)
        } catch [System.Windows.Automation.ElementNotAvailableException] {
            continue
        }
        if ($matches.Count -gt 1) {
            $descriptions = @($matches | ForEach-Object {
                $rid = (Get-ElementRuntimeId $_) -join '.'
                "$($_.Current.ControlType.ProgrammaticName):$($_.Current.Name):$rid"
            }) -join ' | '
            throw "Ambiguous UIA semantic locator. Name='$ExactName' Regex='$NameRegex'. $descriptions"
        }
        if ($matches.Count -eq 1) { return $matches[0] }
    }
    throw "Expected unique semantic UIA element did not appear. Name='$ExactName' Regex='$NameRegex'."
}

function Assert-ExactFocus([System.Windows.Automation.AutomationElement]$Expected, [string]$Label) {
    for ($attempt = 0; $attempt -lt 24; $attempt++) {
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
    throw "Expected exact focus '$Label', got '$actual'."
}

function Get-EditValue([System.Windows.Automation.AutomationElement]$Element) {
    $pattern = $null
    if (-not $Element.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$pattern)) {
        throw "Editable '$($Element.Current.Name)' does not expose UIA ValuePattern."
    }
    return [string]$pattern.Current.Value
}

function Focus-CommandInput {
    [System.Windows.Forms.SendKeys]::SendWait('^+p')
    $edit = Wait-UniqueElement 'Що має зробити Nika?' ([System.Windows.Automation.ControlType]::Edit)
    Assert-ExactFocus $edit 'command input'
    return $edit
}

function Set-CommandText([string]$Text) {
    $edit = Focus-CommandInput
    [System.Windows.Forms.SendKeys]::SendWait('^a')
    [System.Windows.Forms.SendKeys]::SendWait($Text)
    Start-Sleep -Milliseconds 150
    $actual = Get-EditValue $edit
    if ($actual -ne $Text) { throw "Command text mismatch. Expected '$Text', got '$actual'." }
    return $edit
}

function Dispatch-CreateFromCommandInput([string]$Text) {
    Set-CommandText $Text | Out-Null
    [System.Windows.Forms.SendKeys]::SendWait('{TAB}')
    $create = Wait-UniqueElement 'Створити завдання' ([System.Windows.Automation.ControlType]::Button)
    Assert-ExactFocus $create 'create task button after Tab from command input'
    [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
    return $create
}

function Start-NikaSession {
    if ($null -ne $script:session) { throw 'A packaged Nika session is already active.' }
    $process = Start-Process -FilePath $ExePath -PassThru
    $sessionObject = [pscustomobject]@{
        Process = $process
        ProcessId = $process.Id
        StartTicks = $process.StartTime.ToUniversalTime().Ticks
        ExePath = $ExePath
        WindowHandle = $null
        WindowRuntimeId = $null
    }
    $script:session = $sessionObject
    Assert-ProcessGeneration
    $deadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
    $window = $null
    while ([DateTime]::UtcNow -lt $deadline -and $null -eq $window) {
        if ($process.HasExited) { throw "Nika exited during startup with code $($process.ExitCode)." }
        $window = Find-ExactWindow
        if ($null -eq $window) { Start-Sleep -Milliseconds 250 }
    }
    if ($null -eq $window) { throw "Exact Nika UIA window '$WindowTitle' did not appear." }
    $script:session.WindowHandle = $window.Current.NativeWindowHandle
    $script:session.WindowRuntimeId = Get-ElementRuntimeId $window
    if ($script:session.WindowHandle -eq 0) { throw 'Nika window did not expose HWND.' }
    $window.SetFocus()
    Wait-UniqueElement 'Nika Core готова до роботи.' ([System.Windows.Automation.ControlType]::Text) | Out-Null
    return "PID=$($script:session.ProcessId), HWND=$($script:session.WindowHandle)"
}

function Stop-NikaSession([string]$Reason) {
    if ($null -eq $script:session) { return }
    $process = $script:session.Process
    try {
        if (-not $process.HasExited) {
            $window = Find-ExactWindow
            if ($null -ne $window) { $window.SetFocus() }
            [System.Windows.Forms.SendKeys]::SendWait('%{F4}')
            if (-not $process.WaitForExit(12000)) {
                throw "Nika did not close through keyboard Alt+F4 during $Reason."
            }
        }
    } finally {
        if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
        $script:session = $null
    }
}

function Verify-ManifestAndNotices {
    $manifestPath = Join-Path $BundleDir 'release-manifest.json'
    $noticePath = Join-Path $BundleDir 'THIRD_PARTY_NOTICES.txt'
    $pf11Path = Join-Path $BundleDir 'pf11-packaged-product-journey.json'
    Require (Test-Path -LiteralPath $manifestPath -PathType Leaf) 'release-manifest.json is missing.'
    Require (Test-Path -LiteralPath $noticePath -PathType Leaf) 'THIRD_PARTY_NOTICES.txt is missing.'
    Require (Test-Path -LiteralPath $pf11Path -PathType Leaf) 'PF11 packaged evidence is missing.'
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    Require ($manifest.manifest_version -eq 2) 'Unexpected release manifest version.'
    Require ($manifest.product -eq 'NikaCore') 'Release manifest product is not NikaCore.'
    Require ($manifest.source_sha -eq $TargetSourceSha) "Manifest source SHA '$($manifest.source_sha)' != target '$TargetSourceSha'."
    $listed = @{}
    foreach ($entry in $manifest.files) {
        $relative = [string]$entry.path
        Require (-not $listed.ContainsKey($relative)) "Duplicate manifest path: $relative"
        $listed[$relative] = $true
        $candidate = Join-Path $BundleDir ($relative -replace '/', [System.IO.Path]::DirectorySeparatorChar)
        Require (Test-Path -LiteralPath $candidate -PathType Leaf) "Manifest file missing: $relative"
        $item = Get-Item -LiteralPath $candidate
        Require ($item.Length -eq [long]$entry.size) "Manifest size mismatch: $relative"
        $actualHash = (Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash.ToLowerInvariant()
        Require ($actualHash -eq ([string]$entry.sha256).ToLowerInvariant()) "Manifest SHA-256 mismatch: $relative"
    }
    Require ($listed.ContainsKey('NikaCore.exe')) 'Manifest does not list NikaCore.exe.'
    Require ($listed.ContainsKey('THIRD_PARTY_NOTICES.txt')) 'Manifest does not list THIRD_PARTY_NOTICES.txt.'
    Require ((Get-Item -LiteralPath $noticePath).Length -gt 0) 'THIRD_PARTY_NOTICES.txt is empty.'
    $pf11 = Get-Content -LiteralPath $pf11Path -Raw -Encoding UTF8 | ConvertFrom-Json
    Require ($pf11.source_sha -eq $TargetSourceSha) 'PF11 evidence source SHA mismatch.'
    Require ($pf11.packaged_executable_proven -eq $true) 'PF11 evidence does not prove packaged executable.'
    Require ($pf11.restart_replay_proven -eq $true) 'PF11 evidence does not prove restart replay.'
    Require ($pf11.human_tested -eq $false) 'Packaged evidence incorrectly grants HUMAN_TESTED.'
    Require ($pf11.nvda_verified -eq $false) 'Packaged evidence incorrectly grants NVDA_VERIFIED.'
    return "manifest/notices/full file hashes bound to $TargetSourceSha"
}

try {
    $ExePath = Get-FullPath $ExePath
    $BundleDir = Get-FullPath $BundleDir
    $DatabasePath = [System.IO.Path]::GetFullPath($DatabasePath)
    $EvidencePath = [System.IO.Path]::GetFullPath($EvidencePath)
    Require ($TargetSourceSha -match '^[0-9a-f]{40}$') 'TargetSourceSha must be an exact 40-char lowercase SHA.'
    Require ($HarnessSha -match '^[0-9a-f]{40}$') 'HarnessSha must be an exact 40-char lowercase SHA.'
    Require ($ExePath.StartsWith($BundleDir, [System.StringComparison]::OrdinalIgnoreCase)) 'Executable is outside package bundle.'

    if ($previousWebView2BrowserArgs -match '(?i)(?:^|\s)--disable-renderer-accessibility(?:\s|$)') {
        throw 'Renderer accessibility is explicitly disabled in the runner environment.'
    }
    if ([string]::IsNullOrWhiteSpace($previousWebView2BrowserArgs)) {
        $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = $forceRendererAccessibilityArg
    } elseif ($previousWebView2BrowserArgs -notmatch '(?i)(?:^|\s)--force-renderer-accessibility(?:\s|$)') {
        $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = "$previousWebView2BrowserArgs $forceRendererAccessibilityArg"
    }
    $dbParent = Split-Path -Parent $DatabasePath
    New-Item -ItemType Directory -Path $dbParent -Force | Out-Null
    Remove-Item -LiteralPath $DatabasePath -Force -ErrorAction SilentlyContinue
    $env:NIKA_DB_PATH = $DatabasePath

    Invoke-Check 'package.integrity' { Verify-ManifestAndNotices } | Out-Null

    Invoke-Check 'session1.launch_semantics' { Start-NikaSession } | Out-Null
    if ($null -ne $session) {
        Invoke-Check 'session1.required_webview2_descendants' {
            Wait-UniqueElement 'Що має зробити Nika?' ([System.Windows.Automation.ControlType]::Edit) | Out-Null
            Wait-UniqueElement 'Створити завдання' ([System.Windows.Automation.ControlType]::Button) | Out-Null
            Wait-UniqueElement 'Клавіатура' ([System.Windows.Automation.ControlType]::Hyperlink) | Out-Null
            Wait-UniqueElement 'Завдання' ([System.Windows.Automation.ControlType]::Text) | Out-Null
            'required semantic descendants are unique and discoverable'
        } | Out-Null

        Invoke-Check 'session1.keyboard_navigation' {
            $start = Wait-UniqueElement 'Створити завдання' ([System.Windows.Automation.ControlType]::Button)
            $start.SetFocus()
            Assert-ExactFocus $start 'create task button'
            [System.Windows.Forms.SendKeys]::SendWait('%1')
            $tasks = Wait-UniqueElement 'Завдання' ([System.Windows.Automation.ControlType]::Text)
            Assert-ExactFocus $tasks 'tasks heading after Alt+1'
            [System.Windows.Forms.SendKeys]::SendWait('^+p')
            $command = Wait-UniqueElement 'Що має зробити Nika?' ([System.Windows.Automation.ControlType]::Edit)
            Assert-ExactFocus $command 'command input after Ctrl+Shift+P'
            'Alt+1 and Ctrl+Shift+P focus exact semantic controls'
        } | Out-Null

        Invoke-Check 'session1.create_product_project' {
            Dispatch-CreateFromCommandInput $productCommand | Out-Null
            $status = Wait-UniqueElement '' ([System.Windows.Automation.ControlType]::Text) '^ProductProject створено або відкрито: (product-[0-9a-f]{64}); spec version 1\.$'
            $message = [string]$status.Current.Name
            if ($message -notmatch '^ProductProject створено або відкрито: (product-[0-9a-f]{64}); spec version 1\.$') {
                throw "Unexpected ProductProject create message: $message"
            }
            $script:productId = $Matches[1]
            $tasks = Wait-UniqueElement 'Завдання' ([System.Windows.Automation.ControlType]::Text)
            Assert-ExactFocus $tasks 'tasks heading after ProductProject create'
            "created/selected $script:productId"
        } | Out-Null

        if ($null -ne $productId) {
            Invoke-Check 'session1.explicit_select_product_project' {
                Dispatch-CreateFromCommandInput "Open ProductProject $script:productId" | Out-Null
                Wait-UniqueElement '' ([System.Windows.Automation.ControlType]::Text) "^ProductProject відкрито: $([regex]::Escape($script:productId)); spec version 1; state .+\.$" | Out-Null
                "explicit reopen/select retained $script:productId"
            } | Out-Null

            Invoke-Check 'session1.show_current_product_project' {
                Dispatch-CreateFromCommandInput 'Show current ProductProject' | Out-Null
                Wait-UniqueElement '' ([System.Windows.Automation.ControlType]::Text) "^Поточний ProductProject: $([regex]::Escape($script:productId)); spec version 1; state .+; goal: $([regex]::Escape($productCommand))\.$" | Out-Null
                'current durable ProductProject is visible as semantic text'
            } | Out-Null
        }

        Invoke-Check 'session1.safe_deterministic_local_goal' {
            Dispatch-CreateFromCommandInput $localGoal | Out-Null
            Wait-UniqueElement "Завдання виконано в безпечному режимі без LLM: $localGoal" ([System.Windows.Automation.ControlType]::Text) | Out-Null
            Wait-UniqueElement "$localGoal — completed" ([System.Windows.Automation.ControlType]::Text) | Out-Null
            'ordinary goal completed through packaged deterministic ReferenceRuntime and persisted task view'
        } | Out-Null

        Invoke-Check 'session1.recoverable_error_and_focus' {
            $tasks = Wait-UniqueElement 'Завдання' ([System.Windows.Automation.ControlType]::Text)
            $tasks.SetFocus()
            Assert-ExactFocus $tasks 'tasks heading before rejected pause'
            [System.Windows.Forms.SendKeys]::SendWait('^p')
            Wait-UniqueElement 'Немає активного завдання, яке можна призупинити.' ([System.Windows.Automation.ControlType]::Text) | Out-Null
            Assert-ExactFocus $tasks 'tasks heading after rejected pause'
            'rejected pause is readable and the semantic trigger retains deterministic focus'
        } | Out-Null

        Invoke-Check 'session1.editable_shortcut_override_setup' {
            $binding = Wait-UniqueElement 'Комбінація для Open agents' ([System.Windows.Automation.ControlType]::Edit)
            $binding.SetFocus()
            Assert-ExactFocus $binding 'Open agents binding field'
            [System.Windows.Forms.SendKeys]::SendWait('^a')
            [System.Windows.Forms.SendKeys]::SendWait('Backspace')
            [System.Windows.Forms.SendKeys]::SendWait('{TAB}')
            Start-Sleep -Milliseconds 150
            $focused = [System.Windows.Automation.AutomationElement]::FocusedElement
            Require ($null -ne $focused) 'Focus disappeared after Tab from key binding input.'
            Require ($focused.Current.ControlType.Equals([System.Windows.Automation.ControlType]::Button)) 'Tab did not reach a semantic save button.'
            Require ($focused.Current.Name -eq 'Зберегти / очистити') "Unexpected save control '$($focused.Current.Name)'."
            [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
            Wait-UniqueElement 'Shortcut saved.' ([System.Windows.Automation.ControlType]::Text) | Out-Null
            'nav.agents temporarily mapped to Backspace using keyboard-accessible Settings control'
        } | Out-Null

        Invoke-Check 'session1.standard_editing_keys_under_conflicting_app_binding' {
            $edit = Set-CommandText 'abc'
            [System.Windows.Forms.SendKeys]::SendWait('{BACKSPACE}')
            Start-Sleep -Milliseconds 200
            Assert-ExactFocus $edit 'command input after Backspace'
            Require ((Get-EditValue $edit) -eq 'ab') "Backspace was intercepted; edit value is '$(Get-EditValue $edit)'."
            [System.Windows.Forms.SendKeys]::SendWait('{LEFT}')
            Assert-ExactFocus $edit 'command input after Left'
            [System.Windows.Forms.SendKeys]::SendWait('{DELETE}')
            Start-Sleep -Milliseconds 150
            Require ((Get-EditValue $edit) -eq 'a') "Delete/navigation editing result is '$(Get-EditValue $edit)', expected 'a'."
            [System.Windows.Forms.SendKeys]::SendWait('{HOME}')
            [System.Windows.Forms.SendKeys]::SendWait('{END}')
            Assert-ExactFocus $edit 'command input after Home/End'
            'Backspace/Delete/arrows/Home/End remain native even when an app action owns Backspace'
        } | Out-Null

        Invoke-Check 'session1.restore_keymap_default' {
            $binding = Wait-UniqueElement 'Комбінація для Open agents' ([System.Windows.Automation.ControlType]::Edit)
            $binding.SetFocus()
            [System.Windows.Forms.SendKeys]::SendWait('{TAB}')
            [System.Windows.Forms.SendKeys]::SendWait('{TAB}')
            Start-Sleep -Milliseconds 150
            $focused = [System.Windows.Automation.AutomationElement]::FocusedElement
            Require ($null -ne $focused) 'Focus disappeared before default-restore button.'
            Require ($focused.Current.ControlType.Equals([System.Windows.Automation.ControlType]::Button)) 'Expected default-restore button.'
            Require ($focused.Current.Name -eq 'За замовчуванням') "Unexpected restore control '$($focused.Current.Name)'."
            [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
            Wait-UniqueElement 'Default shortcut restored.' ([System.Windows.Automation.ControlType]::Text) | Out-Null
            'temporary keymap mutation restored'
        } | Out-Null

        Invoke-Check 'session1.approval_ui_boundary' {
            $buttons = @(Get-UniqueCandidates '' ([System.Windows.Automation.ControlType]::Button) '(?i)(approve|approval|reject|схвал|погод|відхил)')
            if ($buttons.Count -eq 0) {
                $script:approvalState = 'not_integrated_in_packaged_semantics'
                return 'no approval/rejection semantic control is integrated on exact target; no approval credit awarded'
            }
            $script:approvalState = 'detected_not_exercised'
            throw 'Approval-like packaged semantic control is integrated but ONE-SHOT-58 lacks a proven safe R0-R4 fixture for it.'
        } | Out-Null

        Invoke-Check 'session1.keyboard_close' {
            Stop-NikaSession 'first journey close'
            'closed exact packaged process via Alt+F4'
        } | Out-Null
    }

    if ($null -ne $productId) {
        Invoke-Check 'persisted_product_factory_state_via_packaged_cli' {
            $proofPath = Join-Path (Split-Path -Parent $EvidencePath) 'one-shot-58-pf11-restart-proof.json'
            Remove-Item -LiteralPath $proofPath -Force -ErrorAction SilentlyContinue
            & $ExePath --pf11-proof --pf11-proof-output $proofPath --pf11-proof-command $productCommand
            if ($LASTEXITCODE -ne 0) { throw "Packaged --pf11-proof exited $LASTEXITCODE." }
            $proof = Get-Content -LiteralPath $proofPath -Raw -Encoding UTF8 | ConvertFrom-Json
            Require ($proof.project_id -eq $script:productId) "Persisted project id '$($proof.project_id)' != UI-created '$script:productId'."
            Require ($proof.spec_version -eq 1) 'Persisted ProductProject spec_version changed.'
            Require ($proof.command_center_state_proven -eq $true) 'Packaged CLI did not prove ProductCommandCenter state.'
            Require ($proof.restart_selection_integrity_proven -eq $true) 'Packaged CLI did not prove restart selection integrity.'
            Require ($proof.human_tested -eq $false -and $proof.nvda_verified -eq $false) 'CLI evidence incorrectly granted human/NVDA status.'
            'packaged executable independently reopened the same SQLite ProductProject and bounded state'
        } | Out-Null
    }

    Invoke-Check 'session2.restart_launch' { Start-NikaSession } | Out-Null
    if ($null -ne $session -and $null -ne $productId) {
        Invoke-Check 'session2.recover_and_show_current' {
            Dispatch-CreateFromCommandInput 'Show current ProductProject' | Out-Null
            Wait-UniqueElement '' ([System.Windows.Automation.ControlType]::Text) "^Поточний ProductProject: $([regex]::Escape($script:productId)); spec version 1; state .+; goal: $([regex]::Escape($productCommand))\.$" | Out-Null
            'restart recovered durable presentation selection and showed exact current project'
        } | Out-Null
        Invoke-Check 'session2.keyboard_close' { Stop-NikaSession 'second journey close'; 'closed via Alt+F4' } | Out-Null
    } elseif ($null -ne $session) {
        Stop-NikaSession 'cleanup after missing project id'
    }

    Invoke-Check 'session3.reopen_after_close' { Start-NikaSession } | Out-Null
    if ($null -ne $session -and $null -ne $productId) {
        Invoke-Check 'session3.current_state_still_recoverable' {
            Dispatch-CreateFromCommandInput 'Show current ProductProject' | Out-Null
            Wait-UniqueElement '' ([System.Windows.Automation.ControlType]::Text) "^Поточний ProductProject: $([regex]::Escape($script:productId)); spec version 1; state .+; goal: $([regex]::Escape($productCommand))\.$" | Out-Null
            'second reopen preserved same ProductProject identity/state'
        } | Out-Null
        Invoke-Check 'session3.keyboard_close' { Stop-NikaSession 'third journey close'; 'closed via Alt+F4' } | Out-Null
    } elseif ($null -ne $session) {
        Stop-NikaSession 'final cleanup after missing project id'
    }
}
catch {
    Add-Check 'harness.unhandled' 'FAIL' $_.Exception.Message
}
finally {
    if ($null -ne $session) {
        try { Stop-NikaSession 'final cleanup' } catch { Add-Check 'harness.cleanup' 'FAIL' $_.Exception.Message }
    }
    if ($null -eq $previousWebView2BrowserArgs) {
        Remove-Item Env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS -ErrorAction SilentlyContinue
    } else {
        $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = $previousWebView2BrowserArgs
    }
    if ($null -eq $previousDbPath) {
        Remove-Item Env:NIKA_DB_PATH -ErrorAction SilentlyContinue
    } else {
        $env:NIKA_DB_PATH = $previousDbPath
    }

    $evidenceParent = Split-Path -Parent $EvidencePath
    New-Item -ItemType Directory -Path $evidenceParent -Force | Out-Null
    $payload = [ordered]@{
        schema_version = 1
        target_source_sha = $TargetSourceSha
        harness_sha = $HarnessSha
        executable_path = $ExePath
        bundle_path = $BundleDir
        database_path = $DatabasePath
        product_project_id = $productId
        approval_ui = $approvalState
        coordinate_fallback_used = $false
        semantic_tiers = @('UIAutomation exact process/window generation', 'WebView2 semantic descendants', 'keyboard shortcuts/focus', 'UIA ValuePattern readback')
        checks = $checks.ToArray()
        pass = ($failures.Count -eq 0)
        failure_count = $failures.Count
        failures = $failures.ToArray()
        human_tested = $false
        nvda_verified = $false
        production_release_ready = $false
    }
    $payload | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $EvidencePath -Encoding UTF8
    Write-Host "ONE-SHOT-58 evidence: $EvidencePath"
    Write-Host "ONE-SHOT-58 failures: $($failures.Count)"
}

if ($failures.Count -gt 0) { exit 1 }
exit 0
