param(
    [Parameter(Mandatory=$true)][string]$ExePath,
    [Parameter(Mandatory=$true)][string]$WindowTitle
)

$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted' -or -not $IsWindows) {
    throw 'Autostart restart proof requires an isolated GitHub-hosted Windows runner; never run on a user desktop.'
}
$ExePath = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $ExePath).Path)
$expectedCommand = if ($ExePath -match '[ \t]') { '"' + $ExePath + '"' } else { $ExePath }
$keyPath = 'Software\Microsoft\Windows\CurrentVersion\Run'
$key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($keyPath)
try {
    if ($null -ne $key -and $key.GetValueNames() -contains 'NikaCore') {
        throw 'Proof refuses to overwrite an existing NikaCore registration.'
    }
} finally { if ($null -ne $key) { $key.Dispose() } }

$proof = Join-Path $PSScriptRoot 'm5_uia_proof.ps1'
$pwsh = (Get-Process -Id $PID).Path
try {
    # Each invocation cold-starts the same real EXE with a new PID/window generation.
    # The existing M5 harness owns identity/focus, process cleanup and private DB fixtures.
    & $pwsh -NoProfile -File $proof -ExePath $ExePath -WindowTitle $WindowTitle -AutostartPhase Enable -VerifySourceSetup
    if ($LASTEXITCODE -ne 0) {
        # Hosted WebView2 can occasionally keep focus on the exact Save button while
        # dropping its keyboard activation. Never replay an unknown write: retry the
        # whole fresh-process Enable phase at most once only when the OS proves the
        # failed attempt left the test-owned registration completely absent.
        $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($keyPath)
        try {
            $afterFailedEnable = if ($null -eq $key) {
                $null
            } else {
                $key.GetValue('NikaCore', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            }
        } finally { if ($null -ne $key) { $key.Dispose() } }

        if ($null -eq $afterFailedEnable) {
            Write-Host 'Autostart enable attempt made no OS registration mutation; retrying once in a fresh process.'
            & $pwsh -NoProfile -File $proof -ExePath $ExePath -WindowTitle $WindowTitle -AutostartPhase Enable -VerifySourceSetup
            if ($LASTEXITCODE -ne 0) {
                throw 'Packaged autostart enable/source-setup phase failed after the single zero-mutation retry.'
            }
        } elseif ($afterFailedEnable -ceq $expectedCommand) {
            throw 'Packaged autostart enable/source-setup phase failed after changing OS registration; refusing to replay the mutation.'
        } else {
            throw 'Packaged autostart enable/source-setup phase failed with unexpected OS registration; refusing to replay the mutation.'
        }
    }
    & $pwsh -NoProfile -File $proof -ExePath $ExePath -WindowTitle $WindowTitle -AutostartPhase Observe
    if ($LASTEXITCODE -ne 0) { throw 'Packaged autostart persistence after process restart failed.' }
    & $pwsh -NoProfile -File $proof -ExePath $ExePath -WindowTitle $WindowTitle -AutostartPhase Disable
    if ($LASTEXITCODE -ne 0) {
        # A hosted WebView2 provider can occasionally drop the keyboard activation
        # while leaving focus on the exact Save button. Replaying an unknown write is
        # forbidden. Retry the whole fresh-process phase at most once only when the
        # OS proves that the failed attempt made no registration mutation at all.
        $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($keyPath)
        try {
            $afterFailedDisable = if ($null -eq $key) {
                $null
            } else {
                $key.GetValue('NikaCore', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            }
        } finally { if ($null -ne $key) { $key.Dispose() } }

        if ($afterFailedDisable -ceq $expectedCommand) {
            Write-Host 'Autostart disable attempt made no OS registration mutation; retrying once in a fresh process.'
            & $pwsh -NoProfile -File $proof -ExePath $ExePath -WindowTitle $WindowTitle -AutostartPhase Disable
            if ($LASTEXITCODE -ne 0) {
                throw 'Packaged autostart disable phase failed after the single zero-mutation retry.'
            }
        } elseif ($null -eq $afterFailedDisable) {
            throw 'Packaged autostart disable phase failed after changing OS registration; refusing to replay the mutation.'
        } else {
            throw 'Packaged autostart disable phase failed with unexpected OS registration; refusing to replay the mutation.'
        }
    }
    Write-Host 'Autostart UI -> registration -> fresh process -> persisted UI -> disable verified. Windows login execution and human NVDA remain unverified.'
} finally {
    # Remove only our exact test-owned value if a later phase failed. Preserve any
    # concurrently replaced registration; never delete the Run key or other values.
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($keyPath, $true)
    try {
        if ($null -ne $key) {
            $current = $key.GetValue('NikaCore', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            if ($current -ceq $expectedCommand) { $key.DeleteValue('NikaCore', $false) }
            elseif ($null -ne $current) { Write-Warning 'A foreign registration appeared; left untouched.' }
        }
    } finally { if ($null -ne $key) { $key.Dispose() } }
}
