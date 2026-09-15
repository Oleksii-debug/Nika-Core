[CmdletBinding()]
param(
    [ValidateSet("Install", "Update", "Rollback")]
    [string]$Mode = "Install",
    [string]$BundlePath = "",
    [string]$Destination = "",
    [string]$RollbackOperationId = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not [string]::IsNullOrEmpty($RollbackOperationId)) {
    if ($RollbackOperationId -cnotmatch '^[0-9a-f]{32}$') {
        throw "RollbackOperationId must be exactly 32 lowercase hexadecimal characters."
    }
    if ($Mode -ne "Rollback") {
        throw "RollbackOperationId is only valid for Rollback mode."
    }
}

function Get-NikaFullPath {
    param([Parameter(Mandatory=$true)][string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "Path must not be empty."
    }

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $root = [System.IO.Path]::GetPathRoot($fullPath)
    if (-not [string]::IsNullOrWhiteSpace($root)) {
        $trimmedFullPath = $fullPath.TrimEnd(
            [System.IO.Path]::DirectorySeparatorChar,
            [System.IO.Path]::AltDirectorySeparatorChar
        )
        $trimmedRoot = $root.TrimEnd(
            [System.IO.Path]::DirectorySeparatorChar,
            [System.IO.Path]::AltDirectorySeparatorChar
        )
        if ([System.StringComparer]::OrdinalIgnoreCase.Equals($trimmedFullPath, $trimmedRoot)) {
            return $root
        }
    }
    return $fullPath.TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
}

function Test-NikaFullyQualifiedWindowsPath {
    param([Parameter(Mandatory=$true)][string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return $false }

    # Accept normal drive-rooted paths (C:\... / C:/...) and normal UNC
    # paths (\\server\share\...). Reject drive-relative/bare-drive,
    # root-relative and Win32 device-namespace spellings so GetFullPath cannot
    # inject process/current-drive state into installer data authority.
    if ($Path -match '^[A-Za-z]:[\\/]') { return $true }
    if ($Path -match '^[\\/]{2}(?![?.][\\/])[^\\/]+[\\/][^\\/]+(?:[\\/].*)?$') {
        return $true
    }
    return $false
}

function Test-NikaPathWithin {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)][string]$Root
    )
    $separator = [System.IO.Path]::DirectorySeparatorChar
    $rootPrefix = $Root
    if (
        -not $rootPrefix.EndsWith([string][System.IO.Path]::DirectorySeparatorChar) -and
        -not $rootPrefix.EndsWith([string][System.IO.Path]::AltDirectorySeparatorChar)
    ) {
        $rootPrefix += $separator
    }
    return (
        [System.StringComparer]::OrdinalIgnoreCase.Equals($Path, $Root) -or
        $Path.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)
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
        if (-not (Test-NikaFullyQualifiedWindowsPath -Path $localAppData)) {
            throw "LOCALAPPDATA must be a fully qualified local or UNC path."
        }
        $databasePath = Join-Path (Join-Path $localAppData "NikaCore") "nika_core.db"
    }
    elseif (-not (Test-NikaFullyQualifiedWindowsPath -Path $databasePath)) {
        throw "Configured Nika Core database path must be fully qualified."
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
    $volumeRoot = Get-NikaFullPath ([System.IO.Path]::GetPathRoot($fullPath))
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

function Remove-NikaTreeNoFollow {
    param([Parameter(Mandatory=$true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    $fullPath = Get-NikaFullPath $Path
    Assert-NikaNoReparsePathChain -Path $fullPath
    $rootItem = Get-Item -LiteralPath $fullPath -Force
    if (($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Installer cleanup refuses reparse points."
    }
    if (-not $rootItem.PSIsContainer) {
        Remove-Item -LiteralPath $fullPath -Force
        if (Test-Path -LiteralPath $fullPath) {
            throw "Installer cleanup did not remove the owned path."
        }
        return
    }

    # Directory.Delete(recursive=true) has the no-follow behavior required for
    # destructive cleanup on Windows: a directory reparse entry is removed as
    # an entry instead of recursively deleting through its external target.
    # A nested junction can leave the owned parent non-empty on some runtimes,
    # so retry once after revalidating the still-owned root/ancestor authority.
    $attempt = 0
    while (Test-Path -LiteralPath $fullPath) {
        $attempt += 1
        Assert-NikaNoReparsePathChain -Path $fullPath
        $currentRoot = Get-Item -LiteralPath $fullPath -Force
        if (($currentRoot.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Installer cleanup refuses reparse points."
        }
        if (-not $currentRoot.PSIsContainer) {
            throw "Installer cleanup root changed type during deletion."
        }

        $deleteError = $null
        try {
            [System.IO.Directory]::Delete($fullPath, $true)
        }
        catch [System.IO.IOException] {
            $deleteError = $_
        }

        if (-not (Test-Path -LiteralPath $fullPath)) {
            break
        }
        if ($attempt -ge 2) {
            if ($null -ne $deleteError) {
                throw $deleteError
            }
            throw "Installer cleanup did not remove the owned tree."
        }
    }

    if (Test-Path -LiteralPath $fullPath) {
        throw "Installer cleanup did not remove the owned tree."
    }
}

function Assert-NikaSafeDestination {
    param(
        [Parameter(Mandatory=$true)][string]$DestinationPath,
        [string]$SourceBundle = ""
    )
    $root = Get-NikaFullPath ([System.IO.Path]::GetPathRoot($DestinationPath))
    if ([System.StringComparer]::OrdinalIgnoreCase.Equals($DestinationPath, $root)) {
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
            $stem -in @("CON", "PRN", "AUX", "NUL", 'CONIN$', 'CONOUT$') -or
            $stem -match '^COM[1-9¹²³]$' -or
            $stem -match '^LPT[1-9¹²³]$'
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

function Get-NikaReleaseManifestDigest {
    param([Parameter(Mandatory=$true)][string]$BundleRoot)

    Assert-NikaReleaseBundle -BundleRoot $BundleRoot
    $manifestPath = Get-NikaFullPath (Join-Path $BundleRoot "release-manifest.json")
    Assert-NikaNoReparsePathChain -Path $manifestPath
    return (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-NikaRollbackOperationMarker {
    param(
        [Parameter(Mandatory=$true)][string]$MarkerPath,
        [Parameter(Mandatory=$true)][string]$DataRoot
    )

    if (-not (Test-Path -LiteralPath $MarkerPath)) {
        return $null
    }
    Assert-NikaNoReparsePathChain -Path $MarkerPath
    Assert-NikaDataMutationSeparation -DataRoot $DataRoot -MutationPaths @($MarkerPath)
    if (-not (Test-Path -LiteralPath $MarkerPath -PathType Leaf)) {
        throw "Rollback operation marker must be a regular file."
    }
    $markerItem = Get-Item -LiteralPath $MarkerPath -Force
    if (($markerItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Rollback operation marker must not be a reparse point."
    }

    try {
        $markerJson = Get-Content -LiteralPath $MarkerPath -Raw -Encoding UTF8
        $marker = $markerJson | ConvertFrom-Json
    }
    catch {
        throw "Rollback operation marker is invalid JSON."
    }
    Assert-NikaUniqueJsonObjectKeys -Json $markerJson
    Assert-NikaExactJsonObjectShape -Object $marker -RequiredKeys @(
        "marker_version",
        "operation_id",
        "source_digest",
        "target_digest"
    )

    if (
        $marker.marker_version -is [bool] -or
        -not (($marker.marker_version -is [int]) -or ($marker.marker_version -is [long])) -or
        [int64]$marker.marker_version -ne 1
    ) {
        throw "Rollback operation marker version is invalid."
    }
    if (-not ($marker.operation_id -is [string]) -or [string]$marker.operation_id -cnotmatch '^[0-9a-f]{32}$') {
        throw "Rollback operation marker identity is invalid."
    }
    foreach ($name in @("source_digest", "target_digest")) {
        $value = $marker.$name
        if (-not ($value -is [string]) -or [string]$value -cnotmatch '^[0-9a-f]{64}$') {
            throw "Rollback operation marker manifest identity is invalid."
        }
    }
    if ([string]$marker.source_digest -ceq [string]$marker.target_digest) {
        throw "Rollback operation marker source and target identities must differ."
    }

    return [pscustomobject]@{
        OperationId = [string]$marker.operation_id
        SourceDigest = [string]$marker.source_digest
        TargetDigest = [string]$marker.target_digest
    }
}

function Write-NikaRollbackOperationMarker {
    param(
        [Parameter(Mandatory=$true)][string]$MarkerPath,
        [Parameter(Mandatory=$true)][string]$OperationId,
        [Parameter(Mandatory=$true)][string]$SourceDigest,
        [Parameter(Mandatory=$true)][string]$TargetDigest,
        [Parameter(Mandatory=$true)][string]$DataRoot
    )

    if ($OperationId -cnotmatch '^[0-9a-f]{32}$') {
        throw "Rollback operation identity is invalid."
    }
    if ($SourceDigest -cnotmatch '^[0-9a-f]{64}$' -or $TargetDigest -cnotmatch '^[0-9a-f]{64}$') {
        throw "Rollback operation manifest identity is invalid."
    }
    if ($SourceDigest -ceq $TargetDigest) {
        throw "Rollback source and target images must differ."
    }

    $tempPath = "$MarkerPath.new-$([Guid]::NewGuid().ToString('N'))"
    Assert-NikaNoReparsePathChain -Path $MarkerPath
    Assert-NikaNoReparsePathChain -Path $tempPath
    Assert-NikaDataMutationSeparation -DataRoot $DataRoot -MutationPaths @($MarkerPath, $tempPath)
    if (Test-Path -LiteralPath $tempPath) {
        throw "Rollback operation marker staging path already exists."
    }

    $payload = [ordered]@{
        marker_version = 1
        operation_id = $OperationId
        source_digest = $SourceDigest
        target_digest = $TargetDigest
    } | ConvertTo-Json -Compress
    $encoding = New-Object System.Text.UTF8Encoding($false)
    $bytes = $encoding.GetBytes($payload)
    $stream = [System.IO.FileStream]::new(
        $tempPath,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None,
        4096,
        [System.IO.FileOptions]::WriteThrough
    )
    try {
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    }
    finally {
        $stream.Dispose()
    }

    try {
        Assert-NikaNoReparsePathChain -Path $tempPath
        if (Test-Path -LiteralPath $MarkerPath) {
            Assert-NikaNoReparsePathChain -Path $MarkerPath
            if (-not (Test-Path -LiteralPath $MarkerPath -PathType Leaf)) {
                throw "Rollback operation marker authority is not a regular file."
            }
            [System.IO.File]::Replace($tempPath, $MarkerPath, $null, $true)
        }
        else {
            [System.IO.File]::Move($tempPath, $MarkerPath)
        }
    }
    finally {
        if (Test-Path -LiteralPath $tempPath) {
            Assert-NikaNoReparsePathChain -Path $tempPath
            Remove-Item -LiteralPath $tempPath -Force
        }
    }

    $written = Get-NikaRollbackOperationMarker -MarkerPath $MarkerPath -DataRoot $DataRoot
    if (
        $null -eq $written -or
        [string]$written.OperationId -cne $OperationId -or
        [string]$written.SourceDigest -cne $SourceDigest -or
        [string]$written.TargetDigest -cne $TargetDigest
    ) {
        throw "Rollback operation marker write verification failed."
    }
}

function Remove-NikaRollbackOperationMarker {
    param(
        [Parameter(Mandatory=$true)][string]$MarkerPath,
        [Parameter(Mandatory=$true)][string]$DataRoot
    )

    if (-not (Test-Path -LiteralPath $MarkerPath)) {
        return
    }
    Assert-NikaNoReparsePathChain -Path $MarkerPath
    Assert-NikaDataMutationSeparation -DataRoot $DataRoot -MutationPaths @($MarkerPath)
    if (-not (Test-Path -LiteralPath $MarkerPath -PathType Leaf)) {
        throw "Rollback operation marker authority is not a regular file."
    }
    $markerItem = Get-Item -LiteralPath $MarkerPath -Force
    if (($markerItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Rollback operation marker must not be a reparse point."
    }
    Remove-Item -LiteralPath $MarkerPath -Force
    if (Test-Path -LiteralPath $MarkerPath) {
        throw "Rollback operation marker could not be retired."
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

function Get-NikaFirstUpdateTransaction {
    param(
        [Parameter(Mandatory=$true)][string]$ParentPath,
        [Parameter(Mandatory=$true)][string]$Leaf,
        [Parameter(Mandatory=$true)][string]$DataRoot
    )

    if (-not (Test-Path -LiteralPath $ParentPath -PathType Container)) {
        return $null
    }
    Assert-NikaNoReparsePathChain -Path $ParentPath
    $prefix = ".$Leaf.first-update-"
    $matches = @(
        Get-ChildItem -LiteralPath $ParentPath -Force | Where-Object {
            $_.Name.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
        }
    )
    if ($matches.Count -eq 0) {
        return $null
    }
    if ($matches.Count -ne 1) {
        throw "Multiple first-update transaction authorities are present."
    }

    $item = $matches[0]
    if (-not $item.PSIsContainer) {
        throw "First-update transaction authority is not a directory."
    }
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "First-update transaction authority must not be a reparse point."
    }
    $targetDigest = $item.Name.Substring($prefix.Length)
    if ($targetDigest -cnotmatch '^[0-9a-f]{64}$') {
        throw "First-update transaction authority has an invalid target identity."
    }

    $transactionPath = Get-NikaFullPath $item.FullName
    $candidatePath = Get-NikaFullPath (Join-Path $transactionPath "candidate")
    Assert-NikaNoReparsePathChain -Path $transactionPath
    Assert-NikaDataMutationSeparation -DataRoot $DataRoot -MutationPaths @(
        $transactionPath,
        $candidatePath
    )
    return [pscustomobject]@{
        Path = $transactionPath
        CandidatePath = $candidatePath
        TargetDigest = $targetDigest
    }
}

function Resolve-NikaInterruptedFirstUpdate {
    param(
        [Parameter(Mandatory=$true)][string]$DestinationPath,
        [Parameter(Mandatory=$true)][string]$RollbackPath,
        [Parameter(Mandatory=$true)][object]$Transaction,
        [Parameter(Mandatory=$true)][string]$DataRoot
    )

    if ($null -eq $Transaction) {
        return "none"
    }

    $transactionPath = [string]$Transaction.Path
    $candidatePath = [string]$Transaction.CandidatePath
    $targetDigest = [string]$Transaction.TargetDigest
    Assert-NikaNoReparsePathChain -Path $transactionPath
    Assert-NikaDataMutationSeparation -DataRoot $DataRoot -MutationPaths @(
        $DestinationPath,
        $RollbackPath,
        $transactionPath,
        $candidatePath
    )

    $hasDestination = Test-Path -LiteralPath $DestinationPath -PathType Container
    $hasRollback = Test-Path -LiteralPath $RollbackPath -PathType Container
    $hasCandidate = Test-Path -LiteralPath $candidatePath -PathType Container
    if ((Test-Path -LiteralPath $DestinationPath) -and -not $hasDestination) {
        throw "First-update recovery destination authority is not a directory."
    }
    if ((Test-Path -LiteralPath $RollbackPath) -and -not $hasRollback) {
        throw "First-update recovery rollback authority is not a directory."
    }
    if ((Test-Path -LiteralPath $candidatePath) -and -not $hasCandidate) {
        throw "First-update recovery candidate authority is not a directory."
    }

    if ($hasDestination -and -not $hasRollback) {
        Assert-NikaNoReparsePathChain -Path $DestinationPath
        Assert-NikaReleaseBundle -BundleRoot $DestinationPath
        Assert-NikaDataMutationSeparation -DataRoot $DataRoot -MutationPaths @(
            $DestinationPath,
            $RollbackPath,
            $transactionPath,
            $candidatePath
        )
        Remove-NikaTreeNoFollow -Path $transactionPath
        return "restored-precommand"
    }
    elseif (-not $hasDestination -and $hasRollback -and $hasCandidate) {
        Assert-NikaNoReparsePathChain -Path $RollbackPath
        Assert-NikaReleaseBundle -BundleRoot $RollbackPath
        Assert-NikaNoReparsePathChain -Path $candidatePath
        Assert-NikaReleaseBundle -BundleRoot $candidatePath
        Assert-NikaNoReparsePathChain -Path $DestinationPath
        Assert-NikaDataMutationSeparation -DataRoot $DataRoot -MutationPaths @(
            $DestinationPath,
            $RollbackPath,
            $transactionPath,
            $candidatePath
        )
        [System.IO.Directory]::Move($RollbackPath, $DestinationPath)

        Assert-NikaNoReparsePathChain -Path $DestinationPath
        Assert-NikaReleaseBundle -BundleRoot $DestinationPath
        Assert-NikaDataMutationSeparation -DataRoot $DataRoot -MutationPaths @(
            $DestinationPath,
            $RollbackPath,
            $transactionPath,
            $candidatePath
        )
        Remove-NikaTreeNoFollow -Path $transactionPath
        return "restored-precommand"
    }
    elseif ($hasDestination -and $hasRollback -and -not $hasCandidate) {
        Assert-NikaNoReparsePathChain -Path $DestinationPath
        Assert-NikaNoReparsePathChain -Path $RollbackPath
        Assert-NikaReleaseBundle -BundleRoot $DestinationPath
        Assert-NikaReleaseBundle -BundleRoot $RollbackPath
        $activeDigest = Get-NikaReleaseManifestDigest -BundleRoot $DestinationPath
        if ($activeDigest -cne $targetDigest) {
            throw "Committed first-update transaction target does not match the active image."
        }
        return "committed"
    }

    throw "First-update transaction state is ambiguous; refusing recovery."
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
        Assert-NikaNoReparsePathChain -Path $RetiredRollbackPath
        Assert-NikaReleaseBundle -BundleRoot $RetiredRollbackPath
        Assert-NikaNoReparsePathChain -Path $RollbackPath
        Assert-NikaDataMutationSeparation -DataRoot $DataRoot -MutationPaths @(
            $DestinationPath,
            $RollbackPath,
            $RetiredRollbackPath
        )
        [System.IO.Directory]::Move($RetiredRollbackPath, $RollbackPath)
    }
    elseif (-not $hasDestination -and $hasRollback) {
        Assert-NikaNoReparsePathChain -Path $RetiredRollbackPath
        Assert-NikaReleaseBundle -BundleRoot $RetiredRollbackPath
        Assert-NikaNoReparsePathChain -Path $RollbackPath
        Assert-NikaReleaseBundle -BundleRoot $RollbackPath
        Assert-NikaNoReparsePathChain -Path $DestinationPath
        Assert-NikaDataMutationSeparation -DataRoot $DataRoot -MutationPaths @(
            $DestinationPath,
            $RollbackPath,
            $RetiredRollbackPath
        )
        [System.IO.Directory]::Move($RollbackPath, $DestinationPath)
        Assert-NikaNoReparsePathChain -Path $RetiredRollbackPath
        Assert-NikaReleaseBundle -BundleRoot $RetiredRollbackPath
        Assert-NikaNoReparsePathChain -Path $RollbackPath
        Assert-NikaDataMutationSeparation -DataRoot $DataRoot -MutationPaths @(
            $DestinationPath,
            $RollbackPath,
            $RetiredRollbackPath
        )
        [System.IO.Directory]::Move($RetiredRollbackPath, $RollbackPath)
    }
    elseif ($hasDestination -and $hasRollback) {
        Assert-NikaNoReparsePathChain -Path $DestinationPath
        Assert-NikaNoReparsePathChain -Path $RollbackPath
        Assert-NikaReleaseBundle -BundleRoot $DestinationPath
        Assert-NikaReleaseBundle -BundleRoot $RollbackPath
        Assert-NikaNoReparsePathChain -Path $RetiredRollbackPath
        Assert-NikaDataMutationSeparation -DataRoot $DataRoot -MutationPaths @(
            $DestinationPath,
            $RollbackPath,
            $RetiredRollbackPath
        )
        Remove-NikaTreeNoFollow -Path $RetiredRollbackPath
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

function Resolve-NikaInterruptedRollback {
    param(
        [Parameter(Mandatory=$true)][string]$DestinationPath,
        [Parameter(Mandatory=$true)][string]$RollbackPath,
        [Parameter(Mandatory=$true)][string]$SwapPath,
        [Parameter(Mandatory=$true)][string]$DataRoot
    )

    if (-not (Test-Path -LiteralPath $SwapPath)) {
        return "none"
    }

    Assert-NikaNoReparsePathChain -Path $SwapPath
    Assert-NikaReleaseBundle -BundleRoot $SwapPath
    Assert-NikaDataMutationSeparation -DataRoot $DataRoot -MutationPaths @(
        $DestinationPath,
        $RollbackPath,
        $SwapPath
    )

    $hasDestination = Test-Path -LiteralPath $DestinationPath -PathType Container
    $hasRollback = Test-Path -LiteralPath $RollbackPath -PathType Container
    if ((Test-Path -LiteralPath $DestinationPath) -and -not $hasDestination) {
        throw "Interrupted rollback destination authority is not a directory."
    }
    if ((Test-Path -LiteralPath $RollbackPath) -and -not $hasRollback) {
        throw "Interrupted rollback image authority is not a directory."
    }

    if (-not $hasDestination -and $hasRollback) {
        Assert-NikaNoReparsePathChain -Path $SwapPath
        Assert-NikaReleaseBundle -BundleRoot $SwapPath
        Assert-NikaNoReparsePathChain -Path $RollbackPath
        Assert-NikaReleaseBundle -BundleRoot $RollbackPath
        Assert-NikaNoReparsePathChain -Path $DestinationPath
        Assert-NikaDataMutationSeparation -DataRoot $DataRoot -MutationPaths @(
            $DestinationPath,
            $RollbackPath,
            $SwapPath
        )
        [System.IO.Directory]::Move($SwapPath, $DestinationPath)
        $recoveryState = "restored-precommand"
    }
    elseif ($hasDestination -and -not $hasRollback) {
        Assert-NikaNoReparsePathChain -Path $DestinationPath
        Assert-NikaReleaseBundle -BundleRoot $DestinationPath
        Assert-NikaNoReparsePathChain -Path $SwapPath
        Assert-NikaReleaseBundle -BundleRoot $SwapPath
        Assert-NikaNoReparsePathChain -Path $RollbackPath
        Assert-NikaDataMutationSeparation -DataRoot $DataRoot -MutationPaths @(
            $DestinationPath,
            $RollbackPath,
            $SwapPath
        )
        [System.IO.Directory]::Move($DestinationPath, $RollbackPath)

        Assert-NikaNoReparsePathChain -Path $SwapPath
        Assert-NikaReleaseBundle -BundleRoot $SwapPath
        Assert-NikaNoReparsePathChain -Path $RollbackPath
        Assert-NikaReleaseBundle -BundleRoot $RollbackPath
        Assert-NikaNoReparsePathChain -Path $DestinationPath
        Assert-NikaDataMutationSeparation -DataRoot $DataRoot -MutationPaths @(
            $DestinationPath,
            $RollbackPath,
            $SwapPath
        )
        [System.IO.Directory]::Move($SwapPath, $DestinationPath)
        $recoveryState = "restored-precommand"
    }
    else {
        throw "Interrupted rollback state is ambiguous; refusing to mutate installer images."
    }

    Assert-NikaNoReparsePathChain -Path $DestinationPath
    Assert-NikaNoReparsePathChain -Path $RollbackPath
    Assert-NikaReleaseBundle -BundleRoot $DestinationPath
    Assert-NikaReleaseBundle -BundleRoot $RollbackPath
    if (Test-Path -LiteralPath $SwapPath) {
        throw "Interrupted rollback recovery left an unresolved swap image."
    }
    return $recoveryState
}

if ([string]::IsNullOrWhiteSpace($Destination)) {
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw "LOCALAPPDATA is required when Destination is omitted."
    }
    if (-not (Test-NikaFullyQualifiedWindowsPath -Path $env:LOCALAPPDATA)) {
        throw "LOCALAPPDATA must be a fully qualified local or UNC path."
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
$rollbackSwapPath = Join-Path $parent (".$leaf.rollback-swap")
$rollbackOperationMarkerPath = Join-Path $parent (".$leaf.rollback-operation.json")
$dataRoot = Get-NikaCanonicalDataRoot
Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @(
    $destinationPath,
    $rollbackPath,
    $retiredRollbackPath,
    $rollbackSwapPath,
    $rollbackOperationMarkerPath
)
$rollbackOperationMarker = Get-NikaRollbackOperationMarker `
    -MarkerPath $rollbackOperationMarkerPath `
    -DataRoot $dataRoot
$firstUpdateTransaction = Get-NikaFirstUpdateTransaction `
    -ParentPath $parent `
    -Leaf $leaf `
    -DataRoot $dataRoot
$transactionSignalCount = 0
if (Test-Path -LiteralPath $retiredRollbackPath) { $transactionSignalCount += 1 }
if (Test-Path -LiteralPath $rollbackSwapPath) { $transactionSignalCount += 1 }
if ($null -ne $firstUpdateTransaction) { $transactionSignalCount += 1 }
if ($transactionSignalCount -gt 1) {
    throw "Multiple interrupted installer transactions are present; refusing recovery."
}
Resolve-NikaInterruptedUpdate `
    -DestinationPath $destinationPath `
    -RollbackPath $rollbackPath `
    -RetiredRollbackPath $retiredRollbackPath `
    -DataRoot $dataRoot

if ((Test-Path -LiteralPath $rollbackSwapPath) -and $null -eq $rollbackOperationMarker) {
    throw "Interrupted rollback swap requires a durable rollback operation marker."
}

if ((Test-Path -LiteralPath $rollbackSwapPath) -and $null -ne $rollbackOperationMarker) {
    if (
        $Mode -eq "Rollback" -and
        -not [string]::IsNullOrEmpty($RollbackOperationId) -and
        $RollbackOperationId -cne [string]$rollbackOperationMarker.OperationId
    ) {
        throw "A new rollback operation cannot supersede an interrupted rollback operation."
    }

    Assert-NikaNoReparsePathChain -Path $rollbackSwapPath
    Assert-NikaReleaseBundle -BundleRoot $rollbackSwapPath
    $recoveryHasDestination = Test-Path -LiteralPath $destinationPath -PathType Container
    $recoveryHasRollback = Test-Path -LiteralPath $rollbackPath -PathType Container
    $swapDigest = Get-NikaReleaseManifestDigest -BundleRoot $rollbackSwapPath
    if ($swapDigest -cne [string]$rollbackOperationMarker.SourceDigest) {
        throw "Interrupted rollback swap image does not match the durable operation marker."
    }

    if (-not $recoveryHasDestination -and $recoveryHasRollback) {
        Assert-NikaReleaseBundle -BundleRoot $rollbackPath
        $recoveryRollbackDigest = Get-NikaReleaseManifestDigest -BundleRoot $rollbackPath
        if ($recoveryRollbackDigest -cne [string]$rollbackOperationMarker.TargetDigest) {
            throw "Interrupted rollback target image does not match the durable operation marker."
        }
    }
    elseif ($recoveryHasDestination -and -not $recoveryHasRollback) {
        Assert-NikaReleaseBundle -BundleRoot $destinationPath
        $recoveryDestinationDigest = Get-NikaReleaseManifestDigest -BundleRoot $destinationPath
        if ($recoveryDestinationDigest -cne [string]$rollbackOperationMarker.TargetDigest) {
            throw "Interrupted rollback active image does not match the durable operation marker."
        }
    }
    else {
        throw "Interrupted rollback marker state is ambiguous; refusing recovery."
    }
}

$rollbackRecoveryState = Resolve-NikaInterruptedRollback `
    -DestinationPath $destinationPath `
    -RollbackPath $rollbackPath `
    -SwapPath $rollbackSwapPath `
    -DataRoot $dataRoot
$rollbackOperationMarker = Get-NikaRollbackOperationMarker `
    -MarkerPath $rollbackOperationMarkerPath `
    -DataRoot $dataRoot
$firstUpdateRecoveryState = "none"
if ($null -ne $firstUpdateTransaction) {
    $firstUpdateRecoveryState = Resolve-NikaInterruptedFirstUpdate `
        -DestinationPath $destinationPath `
        -RollbackPath $rollbackPath `
        -Transaction $firstUpdateTransaction `
        -DataRoot $dataRoot
    if ($firstUpdateRecoveryState -ne "committed") {
        $firstUpdateTransaction = $null
    }
}

if ($firstUpdateRecoveryState -eq "committed" -and $Mode -eq "Rollback") {
    Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @(
        [string]$firstUpdateTransaction.Path,
        [string]$firstUpdateTransaction.CandidatePath
    )
    Remove-NikaTreeNoFollow -Path ([string]$firstUpdateTransaction.Path)
    $firstUpdateTransaction = $null
    $firstUpdateRecoveryState = "none"
}

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
    Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @(
        $destinationPath,
        $rollbackPath,
        $rollbackSwapPath,
        $rollbackOperationMarkerPath
    )
    if (-not (Test-Path -LiteralPath $destinationPath -PathType Container)) {
        if ($null -ne $rollbackOperationMarker) {
            throw "Rollback operation marker cannot be resolved without an active image."
        }
        Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @(
            $destinationPath,
            $rollbackPath,
            $rollbackSwapPath,
            $rollbackOperationMarkerPath
        )
        [System.IO.Directory]::Move($rollbackPath, $destinationPath)
        Write-Output $destinationPath
        exit 0
    }

    Assert-NikaReleaseBundle -BundleRoot $destinationPath
    $sourceDigest = Get-NikaReleaseManifestDigest -BundleRoot $destinationPath
    $targetDigest = Get-NikaReleaseManifestDigest -BundleRoot $rollbackPath
    if ($sourceDigest -ceq $targetDigest) {
        throw "Rollback source and target images must be byte-distinct manifest identities."
    }

    $requestedRollbackOperationId = $RollbackOperationId
    if ($null -ne $rollbackOperationMarker) {
        $markerMatchesPreCommand = (
            [string]$rollbackOperationMarker.SourceDigest -ceq $sourceDigest -and
            [string]$rollbackOperationMarker.TargetDigest -ceq $targetDigest
        )
        $markerMatchesCommitted = (
            [string]$rollbackOperationMarker.SourceDigest -ceq $targetDigest -and
            [string]$rollbackOperationMarker.TargetDigest -ceq $sourceDigest
        )

        if ([string]::IsNullOrEmpty($requestedRollbackOperationId)) {
            if ($markerMatchesCommitted) {
                Write-Output $destinationPath
                exit 0
            }
            if (-not $markerMatchesPreCommand) {
                throw "Rollback operation marker does not match the verified installer image pair."
            }
            $requestedRollbackOperationId = [string]$rollbackOperationMarker.OperationId
        }
        elseif ($requestedRollbackOperationId -ceq [string]$rollbackOperationMarker.OperationId) {
            if ($markerMatchesCommitted) {
                Write-Output $destinationPath
                exit 0
            }
            if (-not $markerMatchesPreCommand) {
                throw "Rollback operation identity is bound to a different verified image pair."
            }
        }
        else {
            if (-not $markerMatchesCommitted) {
                throw "A new rollback operation cannot supersede a non-terminal rollback operation."
            }
        }
    }
    elseif ([string]::IsNullOrEmpty($requestedRollbackOperationId)) {
        $requestedRollbackOperationId = [Guid]::NewGuid().ToString('N')
    }

    if (
        $null -eq $rollbackOperationMarker -or
        $requestedRollbackOperationId -cne [string]$rollbackOperationMarker.OperationId
    ) {
        Write-NikaRollbackOperationMarker `
            -MarkerPath $rollbackOperationMarkerPath `
            -OperationId $requestedRollbackOperationId `
            -SourceDigest $sourceDigest `
            -TargetDigest $targetDigest `
            -DataRoot $dataRoot
        $rollbackOperationMarker = Get-NikaRollbackOperationMarker `
            -MarkerPath $rollbackOperationMarkerPath `
            -DataRoot $dataRoot
    }

    $swapPath = $rollbackSwapPath
    if (Test-Path -LiteralPath $swapPath) {
        throw "Rollback cannot begin with an unresolved swap image."
    }
    Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @(
        $destinationPath,
        $rollbackPath,
        $swapPath,
        $rollbackOperationMarkerPath
    )
    $rollbackPhase = "start"
    try {
        Assert-NikaNoReparsePathChain -Path $destinationPath
        Assert-NikaReleaseBundle -BundleRoot $destinationPath
        Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @(
            $destinationPath,
            $rollbackPath,
            $swapPath,
            $rollbackOperationMarkerPath
        )
        [System.IO.Directory]::Move($destinationPath, $swapPath)
        $rollbackPhase = "active-staged"

        Assert-NikaNoReparsePathChain -Path $swapPath
        Assert-NikaReleaseBundle -BundleRoot $swapPath
        Assert-NikaNoReparsePathChain -Path $rollbackPath
        Assert-NikaReleaseBundle -BundleRoot $rollbackPath
        Assert-NikaNoReparsePathChain -Path $destinationPath
        Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @(
            $destinationPath,
            $rollbackPath,
            $swapPath,
            $rollbackOperationMarkerPath
        )
        [System.IO.Directory]::Move($rollbackPath, $destinationPath)
        $rollbackPhase = "rollback-activated"

        Assert-NikaNoReparsePathChain -Path $destinationPath
        Assert-NikaReleaseBundle -BundleRoot $destinationPath
        Assert-NikaNoReparsePathChain -Path $swapPath
        Assert-NikaReleaseBundle -BundleRoot $swapPath
        Assert-NikaNoReparsePathChain -Path $rollbackPath
        Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @(
            $destinationPath,
            $rollbackPath,
            $swapPath,
            $rollbackOperationMarkerPath
        )
        [System.IO.Directory]::Move($swapPath, $rollbackPath)
        $rollbackPhase = "complete"

        Assert-NikaNoReparsePathChain -Path $destinationPath
        Assert-NikaNoReparsePathChain -Path $rollbackPath
        Assert-NikaReleaseBundle -BundleRoot $destinationPath
        Assert-NikaReleaseBundle -BundleRoot $rollbackPath
        if (Test-Path -LiteralPath $swapPath) {
            throw "Rollback completed with an unresolved swap image."
        }
        $committedMarker = Get-NikaRollbackOperationMarker `
            -MarkerPath $rollbackOperationMarkerPath `
            -DataRoot $dataRoot
        if (
            $null -eq $committedMarker -or
            [string]$committedMarker.OperationId -cne $requestedRollbackOperationId -or
            [string]$committedMarker.SourceDigest -cne $sourceDigest -or
            [string]$committedMarker.TargetDigest -cne $targetDigest
        ) {
            throw "Rollback completed without its durable operation acknowledgement."
        }
    }
    catch {
        $rollbackError = $_
        try {
            Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @(
                $destinationPath,
                $rollbackPath,
                $swapPath,
                $rollbackOperationMarkerPath
            )
            if ($rollbackPhase -eq "active-staged") {
                if (
                    -not (Test-Path -LiteralPath $destinationPath) -and
                    (Test-Path -LiteralPath $swapPath -PathType Container)
                ) {
                    Assert-NikaNoReparsePathChain -Path $swapPath
                    Assert-NikaReleaseBundle -BundleRoot $swapPath
                    Assert-NikaNoReparsePathChain -Path $destinationPath
                    Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @(
                        $destinationPath,
                        $rollbackPath,
                        $swapPath,
                        $rollbackOperationMarkerPath
                    )
                    [System.IO.Directory]::Move($swapPath, $destinationPath)
                }
            }
            elseif ($rollbackPhase -eq "rollback-activated") {
                if (
                    (Test-Path -LiteralPath $destinationPath -PathType Container) -and
                    -not (Test-Path -LiteralPath $rollbackPath)
                ) {
                    Assert-NikaNoReparsePathChain -Path $destinationPath
                    Assert-NikaReleaseBundle -BundleRoot $destinationPath
                    Assert-NikaNoReparsePathChain -Path $rollbackPath
                    Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @(
                        $destinationPath,
                        $rollbackPath,
                        $swapPath,
                        $rollbackOperationMarkerPath
                    )
                    [System.IO.Directory]::Move($destinationPath, $rollbackPath)
                }
                if (
                    -not (Test-Path -LiteralPath $destinationPath) -and
                    (Test-Path -LiteralPath $swapPath -PathType Container)
                ) {
                    Assert-NikaNoReparsePathChain -Path $swapPath
                    Assert-NikaReleaseBundle -BundleRoot $swapPath
                    Assert-NikaNoReparsePathChain -Path $destinationPath
                    Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @(
                        $destinationPath,
                        $rollbackPath,
                        $swapPath,
                        $rollbackOperationMarkerPath
                    )
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
$bundleManifestDigest = Get-NikaReleaseManifestDigest -BundleRoot $bundleRoot

if ($null -ne $rollbackOperationMarker) {
    if ($Mode -ne "Update") {
        throw "A durable rollback operation marker may only be superseded by Update or Rollback."
    }
    if (
        -not (Test-Path -LiteralPath $destinationPath -PathType Container) -or
        -not (Test-Path -LiteralPath $rollbackPath -PathType Container)
    ) {
        throw "Update cannot supersede a rollback operation marker without a verified image pair."
    }
    Assert-NikaReleaseBundle -BundleRoot $destinationPath
    Assert-NikaReleaseBundle -BundleRoot $rollbackPath
    $currentDestinationDigest = Get-NikaReleaseManifestDigest -BundleRoot $destinationPath
    $currentRollbackDigest = Get-NikaReleaseManifestDigest -BundleRoot $rollbackPath
    if (
        [string]$rollbackOperationMarker.SourceDigest -cne $currentRollbackDigest -or
        [string]$rollbackOperationMarker.TargetDigest -cne $currentDestinationDigest
    ) {
        throw "Update cannot supersede a non-terminal rollback operation marker."
    }
    Remove-NikaRollbackOperationMarker `
        -MarkerPath $rollbackOperationMarkerPath `
        -DataRoot $dataRoot
    $rollbackOperationMarker = $null
}

if ($firstUpdateRecoveryState -eq "committed" -and $Mode -eq "Update") {
    if ($bundleManifestDigest -ceq [string]$firstUpdateTransaction.TargetDigest) {
        Write-Output $destinationPath
        exit 0
    }
    Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @(
        [string]$firstUpdateTransaction.Path,
        [string]$firstUpdateTransaction.CandidatePath
    )
    Remove-NikaTreeNoFollow -Path ([string]$firstUpdateTransaction.Path)
    $firstUpdateTransaction = $null
    $firstUpdateRecoveryState = "none"
}

if ($Mode -eq "Install" -and (Test-Path -LiteralPath $destinationPath)) {
    throw "Destination already exists; use Update."
}
if ($Mode -eq "Update" -and -not (Test-Path -LiteralPath $destinationPath -PathType Container)) {
    throw "Update requires an existing installed application."
}
$hadPriorRollback = $false
if ($Mode -eq "Update") {
    Assert-NikaReleaseBundle -BundleRoot $destinationPath
    $hadPriorRollback = Test-Path -LiteralPath $rollbackPath -PathType Container
    if ((Test-Path -LiteralPath $rollbackPath) -and -not $hadPriorRollback) {
        throw "Existing rollback authority is not a directory."
    }
    if ($hadPriorRollback) {
        Assert-NikaNoReparsePathChain -Path $rollbackPath
        Assert-NikaReleaseBundle -BundleRoot $rollbackPath
    }
}

Assert-NikaNoReparsePathChain -Path $destinationPath
Assert-NikaNoReparsePathChain -Path $bundleRoot
Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @($destinationPath, $rollbackPath)
New-Item -ItemType Directory -Path $parent -Force | Out-Null
Assert-NikaNoReparsePathChain -Path $destinationPath
$firstUpdateTransactionPath = ""
if ($Mode -eq "Update" -and -not $hadPriorRollback) {
    $firstUpdateTransactionPath = Join-Path $parent (".$leaf.first-update-$bundleManifestDigest")
    $stagePath = Join-Path $firstUpdateTransactionPath "candidate"
    Assert-NikaNoReparsePathChain -Path $firstUpdateTransactionPath
    Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @(
        $firstUpdateTransactionPath,
        $stagePath
    )
    if (Test-Path -LiteralPath $firstUpdateTransactionPath) {
        throw "First-update transaction authority already exists."
    }
    New-Item -ItemType Directory -Path $firstUpdateTransactionPath | Out-Null
    Assert-NikaNoReparsePathChain -Path $firstUpdateTransactionPath
}
else {
    $stagePath = Join-Path $parent (".$leaf.staging-$([Guid]::NewGuid().ToString('N'))")
}
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
                Remove-NikaTreeNoFollow -Path $retiredRollbackPath
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
        if ([string]::IsNullOrWhiteSpace($firstUpdateTransactionPath)) {
            Remove-NikaTreeNoFollow -Path $stagePath
        }
    }
}

Write-Output $destinationPath