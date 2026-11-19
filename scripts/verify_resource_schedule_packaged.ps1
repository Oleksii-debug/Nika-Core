param(
    [Parameter(Mandatory=$true)][string]$ExePath
)

$ErrorActionPreference = 'Stop'
if (-not $IsWindows) { throw 'The frozen resource/schedule proof requires Windows.' }
function Assert-ResourceScheduleEvidence($Evidence) {
    $expectedKeys = @('schema_version', 'fixture_kind', 'actual_builder', 'profile_persisted_after_restart',
        'scheduled_runtime_completed', 'scheduled_running_event_count', 'completed_task_not_replayed',
        'pending_pause_resume_cancel', 'cancelled_running_event_count', 'human_tested', 'nvda_verified',
        'production_release_ready')
    if (@($Evidence.Keys).Count -ne $expectedKeys.Count -or
        @($expectedKeys | Where-Object { -not $Evidence.ContainsKey($_) }).Count -ne 0) {
        throw 'The packaged resource/schedule evidence shape is invalid.'
    }
    foreach ($name in @('schema_version', 'scheduled_running_event_count', 'cancelled_running_event_count')) {
        if ($Evidence[$name] -isnot [int] -and $Evidence[$name] -isnot [long]) {
            throw 'Packaged proof version and event counts must be exact JSON integers.'
        }
    }
    if ($Evidence.schema_version -ne 1 -or $Evidence.fixture_kind -isnot [string] -or
        $Evidence.fixture_kind -cne 'controlled_temporary_sources' -or
        $Evidence.scheduled_running_event_count -ne 1 -or $Evidence.cancelled_running_event_count -ne 0) {
        throw 'The packaged resource/schedule evidence identity or event count is invalid.'
    }
    foreach ($name in @('actual_builder', 'profile_persisted_after_restart', 'scheduled_runtime_completed',
        'completed_task_not_replayed', 'pending_pause_resume_cancel')) {
        if ($Evidence[$name] -isnot [bool] -or $Evidence[$name] -ne $true) {
            throw "The packaged resource/schedule proof is missing actual evidence: $name"
        }
    }
    foreach ($name in @('human_tested', 'nvda_verified', 'production_release_ready')) {
        if ($Evidence[$name] -isnot [bool] -or $Evidence[$name] -ne $false) {
            throw 'Automated resource/schedule proof cannot award human or release acceptance.'
        }
    }
}

# Negative consumer oracles run on the actual Windows PowerShell types. Never
# accept strings, Booleans or floating-point carriers as JSON integer evidence.
$knownEvidence = @{
    schema_version = 1; fixture_kind = 'controlled_temporary_sources'; actual_builder = $true
    profile_persisted_after_restart = $true; scheduled_runtime_completed = $true
    scheduled_running_event_count = 1; completed_task_not_replayed = $true
    pending_pause_resume_cancel = $true; cancelled_running_event_count = 0
    human_tested = $false; nvda_verified = $false; production_release_ready = $false
}
Assert-ResourceScheduleEvidence $knownEvidence
foreach ($name in @('schema_version', 'scheduled_running_event_count', 'cancelled_running_event_count')) {
    foreach ($badValue in @($true, '1', 1.0, $false, '0', 0.0)) {
        $probe = $knownEvidence.Clone()
        $probe[$name] = $badValue
        $rejected = $false
        try { Assert-ResourceScheduleEvidence $probe } catch { $rejected = $true }
        if (-not $rejected) { throw 'The resource/schedule evidence consumer accepted a malformed integer carrier.' }
    }
}
$missingKey = $knownEvidence.Clone()
$missingKey.Remove('actual_builder')
$rejectedMissing = $false
try { Assert-ResourceScheduleEvidence $missingKey } catch { $rejectedMissing = $true }
if (-not $rejectedMissing) { throw 'The resource/schedule evidence consumer accepted missing evidence.' }

$resolvedExe = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $ExePath).Path)
if (-not (Test-Path -LiteralPath $resolvedExe -PathType Leaf)) {
    throw 'The exact packaged executable does not exist.'
}
$proofRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('Nika resource schedule — ' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $proofRoot | Out-Null
$outputPath = Join-Path $proofRoot 'controlled-proof.json'
$process = $null
try {
    # The executable creates its own private DB/source fixture. This argument
    # selects only the bounded evidence destination, never a user database.
    $arguments = @('--resource-schedule-proof', '--resource-schedule-proof-output', ('"' + $outputPath + '"'))
    $process = Start-Process -FilePath $resolvedExe -ArgumentList $arguments -PassThru
    if (-not $process.WaitForExit(60000)) {
        throw 'The exact packaged resource/schedule proof exceeded its bounded deadline.'
    }
    $process.Refresh()
    if ($process.ExitCode -ne 0) { throw 'The exact packaged resource/schedule proof failed.' }
    if (-not (Test-Path -LiteralPath $outputPath -PathType Leaf)) {
        throw 'The packaged resource/schedule proof produced no evidence.'
    }
    $evidence = Get-Content -LiteralPath $outputPath -Raw -Encoding UTF8 | ConvertFrom-Json -AsHashtable
    Assert-ResourceScheduleEvidence $evidence
    Write-Host 'Exact frozen builder: persisted profile, real scheduled completion, one start, no replay, pending pause/resume/cancel verified.'
}
finally {
    if ($null -ne $process) {
        if (-not $process.HasExited) { $process.Kill(); $process.WaitForExit(5000) | Out-Null }
        $process.Dispose()
    }
    if (Test-Path -LiteralPath $proofRoot) { Remove-Item -LiteralPath $proofRoot -Recurse -Force }
}
