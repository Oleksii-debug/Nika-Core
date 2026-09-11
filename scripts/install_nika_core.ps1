[CmdletBinding()]
param(
    [ValidateSet("Install", "Update", "Rollback")]
    [string]$Mode = "Install",
    [string]$BundlePath = "",
    [string]$Destination = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-NikaFullPath {
    param([Parameter(Mandatory=$true)][string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "Path must not be empty."
    }
    return [System.IO.Path]::GetFullPath($Path).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
}

function Test-NikaPathWithin {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)][string]$Root
    )
    $separator = [System.IO.Path]::DirectorySeparatorChar
    return (
        [System.StringComparer]::OrdinalIgnoreCase.Equals($Path, $Root) -or
        $Path.StartsWith($Root + $separator, [System.StringComparison]::OrdinalIgnoreCase)
    )
}

function Get-NikaCanonicalDataRoot {
    $databasePath = [System.Environment]::GetEnvironmentVariable("NIKA_DB_PATH")
    if ([string]::IsNullOrWhiteSpace($databasePath)) {
        $databasePath = [System.Environment]::GetEnvironmentVariable("NIKA_DATABASE_PATH")
    }
    if ([string]::IsNullOrWhiteSpace($databasePath)) {
        $localAppData = [System.Environment]::GetEnvironmentVariable("LOCALAPPDATA")
        if ([string]::IsNullOrWhiteSpace($localAppData)) {
            throw "LOCALAPPDATA is required to resolve the canonical Nika Core data root."
        }
        $databasePath = Join-Path (Join-Path $localAppData "NikaCore") "nika_core.db"
    }
    elseif (-not [System.IO.Path]::IsPathRooted($databasePath)) {
        throw "Configured Nika Core database path must be absolute."
    }

    $canonicalDatabase = Get-NikaFullPath $databasePath
    Assert-NikaNoReparsePathChain -Path $canonicalDatabase
    $dataRoot = Split-Path -Parent $canonicalDatabase
    if ([string]::IsNullOrWhiteSpace($dataRoot)) {
        throw "Configured Nika Core database path has no safe parent."
    }
    return Get-NikaFullPath $dataRoot
}

function Assert-NikaDataMutationSeparation {
    param(
        [Parameter(Mandatory=$true)][string]$DataRoot,
        [Parameter(Mandatory=$true)][string[]]$MutationPaths
    )

    $canonicalDataRoot = Get-NikaFullPath $DataRoot
    Assert-NikaNoReparsePathChain -Path $canonicalDataRoot
    foreach ($mutationPath in $MutationPaths) {
        $canonicalMutationPath = Get-NikaFullPath $mutationPath
        if (
            (Test-NikaPathWithin -Path $canonicalMutationPath -Root $canonicalDataRoot) -or
            (Test-NikaPathWithin -Path $canonicalDataRoot -Root $canonicalMutationPath)
        ) {
            throw "Installer mutation path must not overlap the canonical Nika Core data root."
        }
    }
}

function Assert-NikaNoReparsePathChain {
    param([Parameter(Mandatory=$true)][string]$Path)

    $fullPath = Get-NikaFullPath $Path
    $volumeRoot = [System.IO.Path]::GetPathRoot($fullPath).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $current = $fullPath
    while (-not [string]::IsNullOrWhiteSpace($current)) {
        if ([System.StringComparer]::OrdinalIgnoreCase.Equals($current, $volumeRoot)) {
            break
        }
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Reparse points are forbidden in installer path authority."
            }
        }
        $next = Split-Path -Parent $current
        if (
            [string]::IsNullOrWhiteSpace($next) -or
            [System.StringComparer]::OrdinalIgnoreCase.Equals($next, $current)
        ) {
            break
        }
        $current = Get-NikaFullPath $next
    }
}

function Assert-NikaSafeDestination {
    param(
        [Parameter(Mandatory=$true)][string]$DestinationPath,
        [string]$SourceBundle = ""
    )
    $root = [System.IO.Path]::GetPathRoot($DestinationPath)
    if ([System.StringComparer]::OrdinalIgnoreCase.Equals($DestinationPath, $root.TrimEnd([System.IO.Path]::DirectorySeparatorChar))) {
        throw "Installing to a drive root is forbidden."
    }

    foreach ($name in @("WINDIR", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")) {
        $value = [System.Environment]::GetEnvironmentVariable($name)
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            $systemRoot = Get-NikaFullPath $value
            if (Test-NikaPathWithin -Path $DestinationPath -Root $systemRoot) {
                throw "System directory install is forbidden."
            }
        }
    }

    if (-not [string]::IsNullOrWhiteSpace($SourceBundle)) {
        if (
            (Test-NikaPathWithin -Path $DestinationPath -Root $SourceBundle) -or
            (Test-NikaPathWithin -Path $SourceBundle -Root $DestinationPath)
        ) {
            throw "Bundle and destination must not overlap."
        }
    }
}

function Test-NikaSafeRelativePath {
    param([Parameter(Mandatory=$true)][string]$RelativePath)
    if ([string]::IsNullOrWhiteSpace($RelativePath)) { return $false }
    if ([System.IO.Path]::IsPathRooted($RelativePath)) { return $false }
    if ($RelativePath.Contains("\")) { return $false }
    if ($RelativePath -match '[\x00-\x1f<>:"|?*]') { return $false }
    $parts = $RelativePath.Split("/")
    if ($parts.Count -eq 0) { return $false }
    foreach ($part in $parts) {
        if ([string]::IsNullOrWhiteSpace($part) -or $part -eq "." -or $part -eq "..") {
            return $false
        }
        # Match the canonical release-manifest path policy before Win32 lookup.
        if ($part.EndsWith(".") -or $part.EndsWith(" ")) {
            return $false
        }
        $stem = $part.Split(".")[0].TrimEnd(" ", ".").ToUpperInvariant()
        if (
            $stem -in @("CON", "PRN", "AUX", "NUL") -or
            $stem -match '^COM[1-9]$' -or
            $stem -match '^LPT[1-9]$'
        ) {
            return $false
        }
    }
    return $true
}

function Assert-NikaUniqueJsonObjectKeys {
    param([Parameter(Mandatory=$true)][string]$Json)

    # ConvertFrom-Json is the syntax/value decoder, but Windows PowerShell collapses
    # duplicate object members. This bounded structural pass runs only after syntax
    # validation and tracks decoded key identities per object before trust decisions.
    $stack = [System.Collections.Generic.List[object]]::new()
    $index = 0
    while ($index -lt $Json.Length) {
        $character = $Json[$index]
        if ($character -eq '"') {
            $start = $index
            $index += 1
            $closed = $false
            while ($index -lt $Json.Length) {
                if ($Json[$index] -eq '\') {
                    $index += 2
                    continue
                }
                if ($Json[$index] -eq '"') {
                    $closed = $true
                    break
                }
                $index += 1
            }
            if (-not $closed) {
                throw "Release manifest is invalid JSON."
            }

            $token = $Json.Substring($start, $index - $start + 1)
            if ($stack.Count -gt 0) {
                $frame = $stack[$stack.Count - 1]
                if ($frame.Kind -eq "object" -and $frame.ExpectKey) {
                    try {
                        $key = $token | ConvertFrom-Json
                    }
                    catch {
                        throw "Release manifest is invalid JSON."
                    }
                    if (-not ($key -is [string])) {
                        throw "Release manifest is invalid JSON."
                    }
                    if (-not $frame.Keys.Add([string]$key)) {
                        throw "Release manifest contains a duplicate JSON member."
                    }
                    $frame.ExpectKey = $false
                }
            }
            $index += 1
            continue
        }

        switch ($character) {
            '{' {
                $stack.Add([pscustomobject]@{
                    Kind = "object"
                    ExpectKey = $true
                    Keys = [System.Collections.Generic.HashSet[string]]::new(
                        [System.StringComparer]::Ordinal
                    )
                })
                break
            }
            '[' {
                $stack.Add([pscustomobject]@{
                    Kind = "array"
                    ExpectKey = $false
                    Keys = $null
                })
                break
            }
            '}' {
                if ($stack.Count -eq 0 -or $stack[$stack.Count - 1].Kind -ne "object") {
                    throw "Release manifest is invalid JSON."
                }
                $stack.RemoveAt($stack.Count - 1)
                break
            }
            ']' {
                if ($stack.Count -eq 0 -or $stack[$stack.Count - 1].Kind -ne "array") {
                    throw "Release manifest is invalid JSON."
                }
                $stack.RemoveAt($stack.Count - 1)
                break
            }
            ',' {
                if ($stack.Count -gt 0) {
                    $frame = $stack[$stack.Count - 1]
                    if ($frame.Kind -eq "object") {
                        $frame.ExpectKey = $true
                    }
                }
                break
            }
        }
        $index += 1
    }

    if ($stack.Count -ne 0) {
        throw "Release manifest is invalid JSON."
    }
}

function Assert-NikaExactJsonObjectShape {
    param(
        [Parameter(Mandatory=$true)][object]$Object,
        [Parameter(Mandatory=$true)][string[]]$RequiredKeys
    )

    if ($null -eq $Object -or $Object -isnot [pscustomobject]) {
        throw "Release manifest object shape is invalid."
    }
    $actualKeys = @($Object.PSObject.Properties.Name)
    if ($actualKeys.Count -ne $RequiredKeys.Count) {
        throw "Release manifest object shape is invalid."
    }
    foreach ($key in $RequiredKeys) {
        if (-not ($actualKeys -ccontains $key)) {
            throw "Release manifest object shape is invalid."
        }
    }
}

function Get-NikaManifestProperty {
    param(
        [Parameter(Mandatory=$true)][object]$Object,
        [Parameter(Mandatory=$true)][string]$Name
    )
    if ($Object.PSObject.Properties.Name -notcontains $Name) {
        throw "Release manifest is missing required metadata."
    }
    return $Object.$Name
}

function Assert-NikaReleaseBundle {
    param([Parameter(Mandatory=$true)][string]$BundleRoot)

    if (-not (Test-Path -LiteralPath $BundleRoot -PathType Container)) {
        throw "BundlePath does not exist."
    }
    Assert-NikaNoReparsePathChain -Path $BundleRoot
    $manifestPath = Join-Path $BundleRoot "release-manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "Release manifest is missing."
    }

    try {
        $manifestJson = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8
        $manifest = $manifestJson | ConvertFrom-Json
    }
    catch {
        throw "Release manifest is invalid JSON."
    }
    Assert-NikaUniqueJsonObjectKeys -Json $manifestJson
    Assert-NikaExactJsonObjectShape -Object $manifest -RequiredKeys @(
        "manifest_version",
        "product",
        "version",
        "source_sha",
        "files"
    )

    $manifestVersion = Get-NikaManifestProperty -Object $manifest -Name "manifest_version"
    $product = Get-NikaManifestProperty -Object $manifest -Name "product"
    $version = Get-NikaManifestProperty -Object $manifest -Name "version"
    $sourceSha = Get-NikaManifestProperty -Object $manifest -Name "source_sha"
    $rawFiles = Get-NikaManifestProperty -Object $manifest -Name "files"

    if (
        $manifestVersion -is [bool] -or
        -not (($manifestVersion -is [int]) -or ($manifestVersion -is [long])) -or
        [int64]$manifestVersion -ne 2
    ) {
        throw "Unsupported release manifest version."
    }
    if (
        -not ($product -is [string]) -or
        -not [System.StringComparer]::Ordinal.Equals([string]$product, "NikaCore")
    ) {
        throw "Release manifest product mismatch."
    }
    if (
        -not ($version -is [string]) -or
        [string]::IsNullOrWhiteSpace([string]$version) -or
        [string]$version -cne ([string]$version).Trim()
    ) {
        throw "Release manifest product version is invalid."
    }
    if (-not ($sourceSha -is [string]) -or [string]$sourceSha -cnotmatch '^[0-9a-f]{40}$') {
        throw "Release manifest source SHA is invalid."
    }
    if ($rawFiles -isnot [System.Array]) {
        throw "Release manifest files collection is invalid."
    }
    $files = @($rawFiles)
    if ($files.Count -eq 0) { throw "Release manifest contains no files." }

    $expected = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    $exeBound = $false
    foreach ($entry in $files) {
        Assert-NikaExactJsonObjectShape -Object $entry -RequiredKeys @("path", "size", "sha256")
        $rawRelative = Get-NikaManifestProperty -Object $entry -Name "path"
        $size = Get-NikaManifestProperty -Object $entry -Name "size"
        $rawSha256 = Get-NikaManifestProperty -Object $entry -Name "sha256"
        if (-not ($rawRelative -is [string])) {
            throw "Release manifest contains an unsafe path."
        }
        if (-not ($rawSha256 -is [string])) {
            throw "Release manifest contains an invalid file digest."
        }
        $relative = [string]$rawRelative
        $sha256 = [string]$rawSha256

        if (-not (Test-NikaSafeRelativePath -RelativePath $relative)) {
            throw "Release manifest contains an unsafe path."
        }
        if (-not $expected.Add($relative)) {
            throw "Release manifest contains a duplicate Windows path."
        }
        if (
            $size -is [bool] -or
            -not (($size -is [int]) -or ($size -is [long])) -or
            [int64]$size -lt 0
        ) {
            throw "Release manifest contains an invalid file size."
        }
        if ($sha256 -cnotmatch '^[0-9a-f]{64}$') {
            throw "Release manifest contains an invalid file digest."
        }

        $nativeRelative = $relative.Replace("/", [System.IO.Path]::DirectorySeparatorChar)
        $candidate = Get-NikaFullPath (Join-Path $BundleRoot $nativeRelative)
        if (-not (Test-NikaPathWithin -Path $candidate -Root $BundleRoot)) {
            throw "Release manifest path escapes the bundle."
        }
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            throw "Release bundle is missing a manifest-bound file."
        }
        $item = Get-Item -LiteralPath $candidate -Force
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Release bundle contains a reparse point."
        }
        if ($item.Length -ne [int64]$size) {
            throw "Release bundle file size does not match the manifest."
        }
        $actualSha = (Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualSha -cne $sha256) {
            throw "Release bundle file digest does not match the manifest."
        }
        if ([System.StringComparer]::OrdinalIgnoreCase.Equals($relative, "NikaCore.exe")) {
            $exeBound = $true
        }
    }
    if (-not $exeBound) { throw "Release manifest does not bind NikaCore.exe." }

    foreach ($item in Get-ChildItem -LiteralPath $BundleRoot -Recurse -Force) {
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Release bundle contains a reparse point."
        }
        if ($item.PSIsContainer) { continue }
        $itemPath = Get-NikaFullPath $item.FullName
        if (-not (Test-NikaPathWithin -Path $itemPath -Root $BundleRoot)) {
            throw "Release bundle item escapes bundle root."
        }
        $relative = $itemPath.Substring($BundleRoot.Length).TrimStart([System.IO.Path]::DirectorySeparatorChar).Replace("\", "/")
        if ($relative -eq "release-manifest.json") { continue }
        if (-not $expected.Contains($relative)) {
            throw "Release bundle contains a file not bound by the manifest."
        }
    }
}

function Copy-NikaBundleToStage {
    param(
        [Parameter(Mandatory=$true)][string]$BundleRoot,
        [Parameter(Mandatory=$true)][string]$StagePath
    )
    New-Item -ItemType Directory -Path $StagePath | Out-Null
    foreach ($item in Get-ChildItem -LiteralPath $BundleRoot -Force) {
        Copy-Item -LiteralPath $item.FullName -Destination $StagePath -Recurse
    }
    Assert-NikaReleaseBundle -BundleRoot $StagePath
}

function Resolve-NikaInterruptedUpdate {
    param(
        [Parameter(Mandatory=$true)][string]$DestinationPath,
        [Parameter(Mandatory=$true)][string]$RollbackPath,
        [Parameter(Mandatory=$true)][string]$RetiredRollbackPath,
        [Parameter(Mandatory=$true)][string]$DataRoot
    )

    if (-not (Test-Path -LiteralPath $RetiredRollbackPath)) {
        return
    }

    Assert-NikaNoReparsePathChain -Path $RetiredRollbackPath
    Assert-NikaReleaseBundle -BundleRoot $RetiredRollbackPath
    Assert-NikaDataMutationSeparation -DataRoot $DataRoot -MutationPaths @(
        $DestinationPath,
        $RollbackPath,
        $RetiredRollbackPath
    )

    $hasDestination = Test-Path -LiteralPath $DestinationPath -PathType Container
    $hasRollback = Test-Path -LiteralPath $RollbackPath -PathType Container

    if ($hasDestination -and -not $hasRollback) {
        Assert-NikaNoReparsePathChain -Path $DestinationPath
        Assert-NikaReleaseBundle -BundleRoot $DestinationPath
        [System.IO.Directory]::Move($RetiredRollbackPath, $RollbackPath)
    }
    elseif (-not $hasDestination -and $hasRollback) {
        Assert-NikaNoReparsePathChain -Path $RollbackPath
        Assert-NikaReleaseBundle -BundleRoot $RollbackPath
        [System.IO.Directory]::Move($RollbackPath, $DestinationPath)
        [System.IO.Directory]::Move($RetiredRollbackPath, $RollbackPath)
    }
    elseif ($hasDestination -and $hasRollback) {
        Assert-NikaNoReparsePathChain -Path $DestinationPath
        Assert-NikaNoReparsePathChain -Path $RollbackPath
        Assert-NikaReleaseBundle -BundleRoot $DestinationPath
        Assert-NikaReleaseBundle -BundleRoot $RollbackPath
        Remove-Item -LiteralPath $RetiredRollbackPath -Recurse -Force
    }
    else {
        throw "Interrupted update state cannot be restored without losing a verified image."
    }

    Assert-NikaNoReparsePathChain -Path $DestinationPath
    Assert-NikaNoReparsePathChain -Path $RollbackPath
    Assert-NikaReleaseBundle -BundleRoot $DestinationPath
    Assert-NikaReleaseBundle -BundleRoot $RollbackPath
    if (Test-Path -LiteralPath $RetiredRollbackPath) {
        throw "Interrupted update recovery left an unresolved retired rollback image."
    }
}

if ([string]::IsNullOrWhiteSpace($Destination)) {
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw "LOCALAPPDATA is required when Destination is omitted."
    }
    $Destination = Join-Path $env:LOCALAPPDATA "Programs\NikaCore"
}

$destinationPath = Get-NikaFullPath $Destination
$parent = Split-Path -Parent $destinationPath
$leaf = Split-Path -Leaf $destinationPath
if ([string]::IsNullOrWhiteSpace($parent) -or [string]::IsNullOrWhiteSpace($leaf)) {
    throw "Destination must name an application directory."
}
Assert-NikaNoReparsePathChain -Path $destinationPath
Assert-NikaSafeDestination -DestinationPath $destinationPath

$rollbackPath = Join-Path $parent (".$leaf.rollback")
$retiredRollbackPath = Join-Path $parent (".$leaf.rollback-retired")
$dataRoot = Get-NikaCanonicalDataRoot
Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @(
    $destinationPath,
    $rollbackPath,
    $retiredRollbackPath
)
Resolve-NikaInterruptedUpdate `
    -DestinationPath $destinationPath `
    -RollbackPath $rollbackPath `
    -RetiredRollbackPath $retiredRollbackPath `
    -DataRoot $dataRoot

if ($Mode -eq "Rollback") {
    if (-not (Test-Path -LiteralPath $rollbackPath -PathType Container)) {
        throw "No rollback image is available."
    }
    Assert-NikaNoReparsePathChain -Path $destinationPath
    Assert-NikaNoReparsePathChain -Path $rollbackPath
    Assert-NikaReleaseBundle -BundleRoot (Get-NikaFullPath $rollbackPath)
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    Assert-NikaNoReparsePathChain -Path $destinationPath
    Assert-NikaNoReparsePathChain -Path $rollbackPath
    Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @($destinationPath, $rollbackPath)
    if (-not (Test-Path -LiteralPath $destinationPath -PathType Container)) {
        Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @($destinationPath, $rollbackPath)
        [System.IO.Directory]::Move($rollbackPath, $destinationPath)
        Write-Output $destinationPath
        exit 0
    }

    Assert-NikaReleaseBundle -BundleRoot $destinationPath
    $swapPath = Join-Path $parent (".$leaf.swap-$([Guid]::NewGuid().ToString('N'))")
    Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @($destinationPath, $rollbackPath, $swapPath)
    $rollbackPhase = "start"
    try {
        Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @($destinationPath, $rollbackPath, $swapPath)
        [System.IO.Directory]::Move($destinationPath, $swapPath)
        $rollbackPhase = "active-staged"
        Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @($destinationPath, $rollbackPath, $swapPath)
        [System.IO.Directory]::Move($rollbackPath, $destinationPath)
        $rollbackPhase = "rollback-activated"
        Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @($destinationPath, $rollbackPath, $swapPath)
        [System.IO.Directory]::Move($swapPath, $rollbackPath)
        $rollbackPhase = "complete"
    }
    catch {
        $rollbackError = $_
        try {
            Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @($destinationPath, $rollbackPath, $swapPath)
            if ($rollbackPhase -eq "active-staged") {
                if (
                    -not (Test-Path -LiteralPath $destinationPath) -and
                    (Test-Path -LiteralPath $swapPath -PathType Container)
                ) {
                    Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @($destinationPath, $rollbackPath, $swapPath)
                    [System.IO.Directory]::Move($swapPath, $destinationPath)
                }
            }
            elseif ($rollbackPhase -eq "rollback-activated") {
                if (
                    (Test-Path -LiteralPath $destinationPath -PathType Container) -and
                    -not (Test-Path -LiteralPath $rollbackPath)
                ) {
                    Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @($destinationPath, $rollbackPath, $swapPath)
                    [System.IO.Directory]::Move($destinationPath, $rollbackPath)
                }
                if (
                    -not (Test-Path -LiteralPath $destinationPath) -and
                    (Test-Path -LiteralPath $swapPath -PathType Container)
                ) {
                    Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @($destinationPath, $rollbackPath, $swapPath)
                    [System.IO.Directory]::Move($swapPath, $destinationPath)
                }
            }

            Assert-NikaNoReparsePathChain -Path $destinationPath
            Assert-NikaNoReparsePathChain -Path $rollbackPath
            Assert-NikaReleaseBundle -BundleRoot $destinationPath
            Assert-NikaReleaseBundle -BundleRoot $rollbackPath
            if (Test-Path -LiteralPath $swapPath) {
                throw "Rollback recovery left an unresolved swap image."
            }
        }
        catch {
            throw "Rollback failed and the verified pre-command pair could not be restored."
        }
        throw $rollbackError
    }
    Write-Output $destinationPath
    exit 0
}

$bundleRoot = Get-NikaFullPath $BundlePath
Assert-NikaSafeDestination -DestinationPath $destinationPath -SourceBundle $bundleRoot
Assert-NikaReleaseBundle -BundleRoot $bundleRoot

if ($Mode -eq "Install" -and (Test-Path -LiteralPath $destinationPath)) {
    throw "Destination already exists; use Update."
}
if ($Mode -eq "Update" -and -not (Test-Path -LiteralPath $destinationPath -PathType Container)) {
    throw "Update requires an existing installed application."
}
if ($Mode -eq "Update") {
    Assert-NikaReleaseBundle -BundleRoot $destinationPath
}

Assert-NikaNoReparsePathChain -Path $destinationPath
Assert-NikaNoReparsePathChain -Path $bundleRoot
Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @($destinationPath, $rollbackPath)
New-Item -ItemType Directory -Path $parent -Force | Out-Null
Assert-NikaNoReparsePathChain -Path $destinationPath
$stagePath = Join-Path $parent (".$leaf.staging-$([Guid]::NewGuid().ToString('N'))")
Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @($destinationPath, $rollbackPath, $stagePath)
try {
    Copy-NikaBundleToStage -BundleRoot $bundleRoot -StagePath $stagePath

    Assert-NikaNoReparsePathChain -Path $stagePath
    Assert-NikaNoReparsePathChain -Path $destinationPath
    Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @($destinationPath, $rollbackPath, $stagePath)

    if ($Mode -eq "Install") {
        $failedInstallPath = Join-Path $parent (".$leaf.failed-$([Guid]::NewGuid().ToString('N'))")
        Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @($destinationPath, $rollbackPath, $stagePath, $failedInstallPath)
        try {
            Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @($destinationPath, $rollbackPath, $stagePath, $failedInstallPath)
            [System.IO.Directory]::Move($stagePath, $destinationPath)
            Assert-NikaNoReparsePathChain -Path $destinationPath
            Assert-NikaReleaseBundle -BundleRoot $destinationPath
        }
        catch {
            $activationError = $_
            if (Test-Path -LiteralPath $destinationPath -PathType Container) {
                Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @($destinationPath, $rollbackPath, $stagePath, $failedInstallPath)
                [System.IO.Directory]::Move($destinationPath, $failedInstallPath)
            }
            if (Test-Path -LiteralPath $destinationPath) {
                throw "Install activation failed and the invalid destination could not be quarantined."
            }
            throw $activationError
        }
    }
    else {
        $failedActivationPath = Join-Path $parent (".$leaf.failed-$([Guid]::NewGuid().ToString('N'))")
        $hadPriorRollback = Test-Path -LiteralPath $rollbackPath -PathType Container
        if ((Test-Path -LiteralPath $rollbackPath) -and -not $hadPriorRollback) {
            throw "Existing rollback authority is not a directory."
        }
        if ($hadPriorRollback) {
            Assert-NikaNoReparsePathChain -Path $rollbackPath
            Assert-NikaReleaseBundle -BundleRoot $rollbackPath
        }
        Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @(
            $destinationPath,
            $rollbackPath,
            $retiredRollbackPath,
            $stagePath,
            $failedActivationPath
        )

        $priorRollbackRetired = $false
        $activeMovedToRollback = $false
        try {
            if ($hadPriorRollback) {
                if (Test-Path -LiteralPath $retiredRollbackPath) {
                    throw "Update cannot begin with an unresolved retired rollback image."
                }
                Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @(
                    $destinationPath,
                    $rollbackPath,
                    $retiredRollbackPath,
                    $stagePath,
                    $failedActivationPath
                )
                [System.IO.Directory]::Move($rollbackPath, $retiredRollbackPath)
                $priorRollbackRetired = $true
                Assert-NikaNoReparsePathChain -Path $retiredRollbackPath
                Assert-NikaReleaseBundle -BundleRoot $retiredRollbackPath
            }

            Assert-NikaNoReparsePathChain -Path $destinationPath
            Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @(
                $destinationPath,
                $rollbackPath,
                $retiredRollbackPath,
                $stagePath,
                $failedActivationPath
            )
            [System.IO.Directory]::Move($destinationPath, $rollbackPath)
            $activeMovedToRollback = $true
            Assert-NikaNoReparsePathChain -Path $rollbackPath
            Assert-NikaReleaseBundle -BundleRoot $rollbackPath

            Assert-NikaNoReparsePathChain -Path $stagePath
            Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @(
                $destinationPath,
                $rollbackPath,
                $retiredRollbackPath,
                $stagePath,
                $failedActivationPath
            )
            [System.IO.Directory]::Move($stagePath, $destinationPath)
            Assert-NikaNoReparsePathChain -Path $destinationPath
            Assert-NikaReleaseBundle -BundleRoot $destinationPath
        }
        catch {
            $updateError = $_
            try {
                Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @(
                    $destinationPath,
                    $rollbackPath,
                    $retiredRollbackPath,
                    $stagePath,
                    $failedActivationPath
                )
                if ($activeMovedToRollback) {
                    if (Test-Path -LiteralPath $destinationPath -PathType Container) {
                        [System.IO.Directory]::Move($destinationPath, $failedActivationPath)
                    }
                    if (
                        -not (Test-Path -LiteralPath $destinationPath) -and
                        (Test-Path -LiteralPath $rollbackPath -PathType Container)
                    ) {
                        Assert-NikaNoReparsePathChain -Path $rollbackPath
                        [System.IO.Directory]::Move($rollbackPath, $destinationPath)
                        $activeMovedToRollback = $false
                    }
                }
                if ($priorRollbackRetired) {
                    if (Test-Path -LiteralPath $rollbackPath) {
                        throw "Update recovery cannot restore the prior rollback over an occupied path."
                    }
                    Assert-NikaNoReparsePathChain -Path $retiredRollbackPath
                    [System.IO.Directory]::Move($retiredRollbackPath, $rollbackPath)
                    $priorRollbackRetired = $false
                }

                Assert-NikaNoReparsePathChain -Path $destinationPath
                Assert-NikaReleaseBundle -BundleRoot $destinationPath
                if ($hadPriorRollback) {
                    Assert-NikaNoReparsePathChain -Path $rollbackPath
                    Assert-NikaReleaseBundle -BundleRoot $rollbackPath
                }
                elseif (Test-Path -LiteralPath $rollbackPath) {
                    throw "Update recovery created an unexpected rollback image."
                }
                if (Test-Path -LiteralPath $retiredRollbackPath) {
                    throw "Update recovery left an unresolved retired rollback image."
                }
            }
            catch {
                throw "Update failed and the verified pre-command active/rollback pair could not be restored."
            }
            throw $updateError
        }

        if ($priorRollbackRetired -and (Test-Path -LiteralPath $retiredRollbackPath)) {
            try {
                Assert-NikaNoReparsePathChain -Path $retiredRollbackPath
                Assert-NikaReleaseBundle -BundleRoot $retiredRollbackPath
                Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @(
                    $destinationPath,
                    $rollbackPath,
                    $retiredRollbackPath
                )
                Remove-Item -LiteralPath $retiredRollbackPath -Recurse -Force
                $priorRollbackRetired = $false
            }
            catch {
                Write-Warning "Update activated successfully; prior rollback cleanup is deferred to the next installer run."
            }
        }
    }
}
finally {
    if (Test-Path -LiteralPath $stagePath) {
        Assert-NikaNoReparsePathChain -Path $stagePath
        Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @($destinationPath, $rollbackPath, $stagePath)
        Remove-Item -LiteralPath $stagePath -Recurse -Force
    }
}

Write-Output $destinationPath
