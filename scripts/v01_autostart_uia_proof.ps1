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
    if ($LASTEXITCODE -ne 0) { throw 'Packaged autostart enable/source-setup phase failed.' }
    & $pwsh -NoProfile -File $proof -ExePath $ExePath -WindowTitle $WindowTitle -AutostartPhase Observe
    if ($LASTEXITCODE -ne 0) { throw 'Packaged autostart persistence after process restart failed.' }
    & $pwsh -NoProfile -File $proof -ExePath $ExePath -WindowTitle $WindowTitle -AutostartPhase Disable
    if ($LASTEXITCODE -ne 0) { throw 'Packaged autostart disable phase failed.' }
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
