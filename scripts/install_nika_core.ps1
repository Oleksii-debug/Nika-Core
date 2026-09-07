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
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw "Release manifest is invalid JSON."
    }

    $manifestVersion = Get-NikaManifestProperty -Object $manifest -Name "manifest_version"
    $product = Get-NikaManifestProperty -Object $manifest -Name "product"
    $sourceSha = Get-NikaManifestProperty -Object $manifest -Name "source_sha"
    $files = @(Get-NikaManifestProperty -Object $manifest -Name "files")

    if ($manifestVersion -ne 2) { throw "Unsupported release manifest version." }
    if ($product -ne "NikaCore") { throw "Release manifest product mismatch." }
    if ([string]$sourceSha -cnotmatch '^[0-9a-f]{40}$') {
        throw "Release manifest source SHA is invalid."
    }
    if ($files.Count -eq 0) { throw "Release manifest contains no files." }

    $expected = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    $exeBound = $false
    foreach ($entry in $files) {
        $relative = [string](Get-NikaManifestProperty -Object $entry -Name "path")
        $size = Get-NikaManifestProperty -Object $entry -Name "size"
        $sha256 = [string](Get-NikaManifestProperty -Object $entry -Name "sha256")

        if (-not (Test-NikaSafeRelativePath -RelativePath $relative)) {
            throw "Release manifest contains an unsafe path."
        }
        if (-not $expected.Add($relative)) {
            throw "Release manifest contains a duplicate Windows path."
        }
        if ($size -is [bool] -or -not ($size -is [ValueType]) -or [int64]$size -lt 0) {
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
    if (-not (Test-Path -LiteralPath $destinationPath -PathType Container)) {
        [System.IO.Directory]::Move($rollbackPath, $destinationPath)
        Write-Output $destinationPath
        exit 0
    }

    Assert-NikaReleaseBundle -BundleRoot $destinationPath
    $swapPath = Join-Path $parent (".$leaf.swap-$([Guid]::NewGuid().ToString('N'))")
    try {
        [System.IO.Directory]::Move($destinationPath, $swapPath)
        [System.IO.Directory]::Move($rollbackPath, $destinationPath)
        [System.IO.Directory]::Move($swapPath, $rollbackPath)
    }
    catch {
        if (
            -not (Test-Path -LiteralPath $destinationPath) -and
            (Test-Path -LiteralPath $swapPath -PathType Container)
        ) {
            [System.IO.Directory]::Move($swapPath, $destinationPath)
        }
        throw
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
New-Item -ItemType Directory -Path $parent -Force | Out-Null
Assert-NikaNoReparsePathChain -Path $destinationPath
$stagePath = Join-Path $parent (".$leaf.staging-$([Guid]::NewGuid().ToString('N'))")
try {
    Copy-NikaBundleToStage -BundleRoot $bundleRoot -StagePath $stagePath

    Assert-NikaNoReparsePathChain -Path $stagePath
    Assert-NikaNoReparsePathChain -Path $destinationPath

    if ($Mode -eq "Install") {
        [System.IO.Directory]::Move($stagePath, $destinationPath)
        Assert-NikaNoReparsePathChain -Path $destinationPath
        Assert-NikaReleaseBundle -BundleRoot $destinationPath
    }
    else {
        if (Test-Path -LiteralPath $rollbackPath) {
            Assert-NikaNoReparsePathChain -Path $rollbackPath
            Remove-Item -LiteralPath $rollbackPath -Recurse -Force
        }
        Assert-NikaNoReparsePathChain -Path $destinationPath
        [System.IO.Directory]::Move($destinationPath, $rollbackPath)
        $failedActivationPath = Join-Path $parent (".$leaf.failed-$([Guid]::NewGuid().ToString('N'))")
        try {
            Assert-NikaNoReparsePathChain -Path $stagePath
            Assert-NikaNoReparsePathChain -Path $rollbackPath
            [System.IO.Directory]::Move($stagePath, $destinationPath)
            Assert-NikaNoReparsePathChain -Path $destinationPath
            Assert-NikaReleaseBundle -BundleRoot $destinationPath
        }
        catch {
            $activationError = $_
            if (Test-Path -LiteralPath $destinationPath -PathType Container) {
                [System.IO.Directory]::Move($destinationPath, $failedActivationPath)
            }
            if (
                -not (Test-Path -LiteralPath $destinationPath) -and
                (Test-Path -LiteralPath $rollbackPath -PathType Container)
            ) {
                Assert-NikaNoReparsePathChain -Path $rollbackPath
                [System.IO.Directory]::Move($rollbackPath, $destinationPath)
                Assert-NikaNoReparsePathChain -Path $destinationPath
                Assert-NikaReleaseBundle -BundleRoot $destinationPath
            }
            throw $activationError
        }
    }
}
finally {
    if (Test-Path -LiteralPath $stagePath) {
        Assert-NikaNoReparsePathChain -Path $stagePath
        Remove-Item -LiteralPath $stagePath -Recurse -Force
    }
}

Write-Output $destinationPath
