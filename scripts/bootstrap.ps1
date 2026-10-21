# Private, side-effect-free runtime bootstrap helpers for Windows x64.
# Dot-source this file from the installer before calling either exported function.

function ConvertTo-CatsAbsolutePath {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)][string]$Label
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "$Label must be an absolute Windows path."
    }
    # Do not silently normalize a caller supplied traversal path.
    if ($Path.Length -lt 3 -or
        $Path -notmatch '^[A-Za-z]:[\\/]' -or
        $Path -match '(?i)(^|[\\/])\.\.(?:[\\/]|$)' -or
        $Path -match '(?i)(^|[\\/])\.(?:[\\/]|$)' -or
        $Path.Substring(2) -match '[<>"|?*]') {
        throw "$Label must be a clean absolute Windows path without traversal segments."
    }
    try {
        $full = [IO.Path]::GetFullPath($Path)
    } catch {
        throw "$Label is not a valid Windows path."
    }
    if ($full.TrimEnd('\') -match '^[A-Za-z]:$') {
        throw "$Label must name a dedicated directory, not a drive root."
    }
    return $full.TrimEnd('\')
}

function Test-CatsReparsePoint {
    param([Parameter(Mandatory=$true)][string]$Path)
    try {
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    } catch [System.Management.Automation.ItemNotFoundException] {
        return $false
    }
    return (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
}

function Assert-CatsNoReparseAncestors {
    param([Parameter(Mandatory=$true)][string]$Path)
    $current = [IO.Path]::GetFullPath($Path)
    while ($true) {
        if (Test-CatsReparsePoint -Path $current) {
            throw "A reparse point is present in the runtime path."
        }
        $parentInfo = [IO.Directory]::GetParent($current)
        if ($null -eq $parentInfo) { break }
        $parent = $parentInfo.FullName
        if ([string]::IsNullOrEmpty($parent) -or $parent -eq $current) { break }
        $current = $parent
    }
}

function Assert-CatsNoReparseBelow {
    param([Parameter(Mandatory=$true)][string]$Path)
    try { Get-Item -LiteralPath $Path -Force -ErrorAction Stop | Out-Null }
    catch [System.Management.Automation.ItemNotFoundException] { return }
    if (Test-CatsReparsePoint -Path $Path) {
        throw "A reparse point is present in the runtime staging path."
    }
    $children = @(Get-ChildItem -LiteralPath $Path -Force -Recurse -ErrorAction Stop)
    foreach ($child in $children) {
        if (($child.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "A reparse point is present in the runtime staging path."
        }
    }
}

function Test-CatsPathUnderRoot {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)][string]$Root
    )

    try {
        $candidate = [IO.Path]::GetFullPath($Path).TrimEnd('\')
        $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    } catch {
        return $false
    }
    if ([string]::IsNullOrEmpty($candidate) -or [string]::IsNullOrEmpty($rootFull)) {
        return $false
    }
    return $candidate.StartsWith($rootFull + '\', [StringComparison]::OrdinalIgnoreCase)
}

function Get-CatsPayloadManifest {
    param(
        [Parameter(Mandatory=$true)][string]$Source,
        [Parameter(Mandatory=$true)][string]$Target,
        [Parameter(Mandatory=$true)][string]$Root
    )

    $sourceFull = [IO.Path]::GetFullPath($Source).TrimEnd('\')
    $targetFull = [IO.Path]::GetFullPath($Target).TrimEnd('\')
    if (-not (Test-CatsPathUnderRoot -Path $targetFull -Root $Root)) {
        throw 'The runtime payload target escaped the destination directory.'
    }
    Assert-CatsNoReparseBelow -Path $sourceFull
    $sourceItem = Get-Item -LiteralPath $sourceFull -Force -ErrorAction Stop
    if (($sourceItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'The runtime payload contains a reparse point.'
    }

    $files = New-Object 'System.Collections.Generic.List[object]'
    $directories = New-Object 'System.Collections.Generic.List[string]'
    if ($sourceItem.PSIsContainer) {
        [void]$directories.Add($targetFull)
        $children = @(Get-ChildItem -LiteralPath $sourceFull -Force -Recurse -ErrorAction Stop)
        foreach ($child in $children) {
            if (($child.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'The runtime payload contains a reparse point.'
            }
            $relative = $child.FullName.Substring($sourceFull.Length).TrimStart('\')
            if ([string]::IsNullOrEmpty($relative)) { continue }
            $childTarget = [IO.Path]::GetFullPath((Join-Path $targetFull $relative)).TrimEnd('\')
            if (-not (Test-CatsPathUnderRoot -Path $childTarget -Root $Root)) {
                throw 'The runtime payload target escaped the destination directory.'
            }
            if ($child.PSIsContainer) {
                [void]$directories.Add($childTarget)
            } else {
                $hash = (Get-FileHash -LiteralPath $child.FullName -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
                [void]$files.Add([pscustomobject]@{ Path = $childTarget; Sha256 = $hash })
            }
        }
    } else {
        $hash = (Get-FileHash -LiteralPath $sourceFull -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
        [void]$files.Add([pscustomobject]@{ Path = $targetFull; Sha256 = $hash })
    }
    return [pscustomobject]@{
        Files = @($files.ToArray())
        Directories = @($directories.ToArray())
        Moved = $false
    }
}

function Remove-CatsMovedPayload {
    param(
        [Parameter(Mandatory=$true)]$Manifest,
        [Parameter(Mandatory=$true)][string]$Root
    )

    if ($Manifest.Moved -ne $true) { return }
    foreach ($file in @($Manifest.Files | Sort-Object { $_.Path.Length } -Descending) ) {
        if (-not (Test-CatsPathUnderRoot -Path $file.Path -Root $Root)) { continue }
        try {
            Assert-CatsNoReparseAncestors -Path $file.Path
            if (Test-CatsReparsePoint -Path $file.Path) { continue }
            $item = Get-Item -LiteralPath $file.Path -Force -ErrorAction Stop
            if ($item.PSIsContainer) { continue }
            $actual = (Get-FileHash -LiteralPath $file.Path -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
            # A changed file may be a concurrent write or replacement; preserve it.
            if ($actual -ne $file.Sha256.ToLowerInvariant()) { continue }
            Remove-Item -LiteralPath $file.Path -Force -ErrorAction Stop
        } catch { }
    }
    foreach ($directory in @($Manifest.Directories | Sort-Object Length -Descending) ) {
        if (-not (Test-CatsPathUnderRoot -Path $directory -Root $Root)) { continue }
        try {
            Assert-CatsNoReparseAncestors -Path $directory
            if (Test-CatsReparsePoint -Path $directory) { continue }
            $item = Get-Item -LiteralPath $directory -Force -ErrorAction Stop
            if (-not $item.PSIsContainer) { continue }
            # Only remove directories that are still empty. Concurrent additions remain.
            if (@(Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop).Count -eq 0) {
                Remove-Item -LiteralPath $directory -Force -ErrorAction Stop
            }
        } catch { }
    }
}

function Assert-CatsWindowsX64 {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or
        -not [Environment]::Is64BitOperatingSystem -or
        (($env:PROCESSOR_ARCHITECTURE -ne 'AMD64') -and ($env:PROCESSOR_ARCHITEW6432 -ne 'AMD64'))) {
        throw 'The private embedded runtime bootstrap supports Windows x64 only; this machine is not a validated Windows x64 target.'
    }
}

function Invoke-CatsVerifiedDownload {
    param(
        [Parameter(Mandatory=$true)][string]$Uri,
        [Parameter(Mandatory=$true)][string]$OutputPath,
        [Parameter(Mandatory=$true)][string]$ExpectedSha256,
        [Parameter(Mandatory=$true)][string]$Label,
        [Parameter(Mandatory=$true)][string]$ExpectedHost
    )

    try {
        $parsed = [Uri]$Uri
        if ($parsed.Scheme -ne 'https' -or $parsed.Host -ne $ExpectedHost -or
            -not [string]::IsNullOrEmpty($parsed.UserInfo) -or
            -not [string]::IsNullOrEmpty($parsed.Query) -or
            -not [string]::IsNullOrEmpty($parsed.Fragment)) {
            throw 'The fixed download URL failed its HTTPS/host validation.'
        }
    } catch {
        throw "The fixed $Label download URL is invalid."
    }

    try {
        # No -Proxy or credentials are supplied: Invoke-WebRequest uses the
        # configured Windows/system proxy in the normal PowerShell manner.
        $ProgressPreference = 'SilentlyContinue'
        Invoke-WebRequest -Uri $Uri -OutFile $OutputPath -UseBasicParsing -ErrorAction Stop | Out-Null
    } catch {
        throw "Could not download the fixed $Label artifact."
    }

    try {
        $actual = (Get-FileHash -LiteralPath $OutputPath -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
    } catch {
        throw "Could not hash the downloaded $Label artifact."
    }
    if ($actual -ne $ExpectedSha256.ToLowerInvariant()) {
        throw "The downloaded $Label artifact failed SHA-256 verification."
    }
}

function Expand-CatsZipSafe {
    param(
        [Parameter(Mandatory=$true)][string]$ArchivePath,
        [Parameter(Mandatory=$true)][string]$Destination
    )

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    Assert-CatsNoReparseAncestors -Path $Destination
    $root = [IO.Path]::GetFullPath($Destination).TrimEnd('\')
    $prefix = $root + '\'
    $archive = $null
    try {
        $archive = [IO.Compression.ZipFile]::OpenRead($ArchivePath)
        foreach ($entry in $archive.Entries) {
            $name = $entry.FullName
            if ([string]::IsNullOrEmpty($name)) { continue }
            $normalized = $name.Replace('/', '\')
            if ($normalized.StartsWith('\') -or $normalized -match '^[A-Za-z]:' -or
                $normalized -match '(?i)(^|\\)\.\.(?:\\|$)' -or
                $normalized -match '(?i)(^|\\)\.(?:\\|$)' -or
                $normalized -match '[<>:"|?*]') {
                throw 'The downloaded archive contains an unsafe path.'
            }
            $target = [IO.Path]::GetFullPath((Join-Path $Destination $normalized))
            if (-not ($target.Equals($root, [StringComparison]::OrdinalIgnoreCase) -or
                $target.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase))) {
                throw 'The downloaded archive contains a path outside the runtime directory.'
            }
            if ($name.EndsWith('/') -or $name.EndsWith('\')) {
                [IO.Directory]::CreateDirectory($target) | Out-Null
                continue
            }
            $parent = [IO.Path]::GetDirectoryName($target)
            if ($parent) { [IO.Directory]::CreateDirectory($parent) | Out-Null }
            try { Get-Item -LiteralPath $target -Force -ErrorAction Stop | Out-Null; $targetExists = $true }
            catch [System.Management.Automation.ItemNotFoundException] { $targetExists = $false }
            if ($targetExists) {
                throw 'The downloaded archive contains duplicate output paths.'
            }
            $inputStream = $null
            $outputStream = $null
            try {
                $inputStream = $entry.Open()
                $outputStream = [IO.File]::Open($target, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
                $inputStream.CopyTo($outputStream)
            } finally {
                if ($outputStream) { $outputStream.Dispose() }
                if ($inputStream) { $inputStream.Dispose() }
            }
        }
    } finally {
        if ($archive) { $archive.Dispose() }
    }
}

function New-CatsRuntime {
    [CmdletBinding()]
    param([Parameter(Mandatory=$true)][string]$Destination)

    Assert-CatsWindowsX64
    $destinationFull = ConvertTo-CatsAbsolutePath -Path $Destination -Label 'Destination'
    Assert-CatsNoReparseAncestors -Path $destinationFull
    try {
        $destinationItem = Get-Item -LiteralPath $destinationFull -Force -ErrorAction Stop
        $destinationExisted = $true
    } catch [System.Management.Automation.ItemNotFoundException] {
        $destinationItem = $null
        $destinationExisted = $false
    }
    $destinationCreated = $false
    $stage = $null
    $movedPayload = New-Object 'System.Collections.Generic.List[object]'
    try {
        if ($destinationExisted) {
            if (-not $destinationItem.PSIsContainer) { throw 'Destination must be an empty directory.' }
            if (($destinationItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Destination must not be a reparse point.'
            }
            if (@(Get-ChildItem -LiteralPath $destinationFull -Force -ErrorAction Stop).Count -ne 0) {
                throw 'Destination must be an empty directory; no existing files were changed.'
            }
        } else {
            $parentInfo = [IO.Directory]::GetParent($destinationFull)
            $parent = if ($null -eq $parentInfo) { $null } else { $parentInfo.FullName }
            if ([string]::IsNullOrEmpty($parent) -or !(Test-Path -LiteralPath $parent -PathType Container)) {
                throw 'The parent of Destination must already exist.'
            }
            Assert-CatsNoReparseAncestors -Path $parent
            [IO.Directory]::CreateDirectory($destinationFull) | Out-Null
            $destinationCreated = $true
        }

        $stage = Join-Path $destinationFull '.cats-bootstrap-stage'
        try { Get-Item -LiteralPath $stage -Force -ErrorAction Stop | Out-Null; $stageExists = $true }
        catch [System.Management.Automation.ItemNotFoundException] { $stageExists = $false }
        if ($stageExists) {
            throw 'Destination contains a stale bootstrap staging path.'
        }
        [IO.Directory]::CreateDirectory($stage) | Out-Null
        $downloads = Join-Path $stage 'downloads'
        $payload = Join-Path $stage 'payload'
        [IO.Directory]::CreateDirectory($downloads) | Out-Null
        [IO.Directory]::CreateDirectory($payload) | Out-Null

        $pythonZip = Join-Path $downloads 'python-3.14.9-embed-amd64.zip'
        $pipWheel = Join-Path $downloads 'pip-26.2.2-py3-none-any.whl'
        Invoke-CatsVerifiedDownload `
            -Uri 'https://www.python.org/ftp/python/3.14.9/python-3.14.9-embed-amd64.zip' `
            -OutputPath $pythonZip `
            -ExpectedSha256 '3c01d7cfc666f746a6f064f60fb2156ab0ec464d3d738ab796371a46a9bb24a2' `
            -Label 'Python 3.14.9 embeddable runtime' `
            -ExpectedHost 'www.python.org'
        Invoke-CatsVerifiedDownload `
            -Uri 'https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl' `
            -OutputPath $pipWheel `
            -ExpectedSha256 'fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70' `
            -Label 'pip 26.2.2 bootstrap wheel' `
            -ExpectedHost 'files.pythonhosted.org'

        Expand-CatsZipSafe -ArchivePath $pythonZip -Destination $payload
        $sitePackages = Join-Path $payload 'Lib\site-packages'
        [IO.Directory]::CreateDirectory($sitePackages) | Out-Null
        Expand-CatsZipSafe -ArchivePath $pipWheel -Destination $sitePackages

        $pth = Join-Path $payload 'python314._pth'
        $embeddedPython = Join-Path $payload 'python.exe'
        if (!(Test-Path -LiteralPath $pth -PathType Leaf) -or !(Test-Path -LiteralPath $embeddedPython -PathType Leaf)) {
            throw 'The downloaded embeddable runtime is missing its expected Python files.'
        }
        $pthContent = "python314.zip`r`n.`r`nLib\site-packages`r`nimport site`r`n"
        [IO.File]::WriteAllText($pth, $pthContent, [Text.Encoding]::ASCII)
        Assert-CatsNoReparseBelow -Path $payload

        # Validate the private pip bootstrap with the extracted interpreter;
        # no system Python, PATH entry, registry key, or user configuration is used.
        & $embeddedPython -c "import pip,sys;sys.exit(0 if pip.__version__ == '26.2.2' else 1)" 2>$null
        if ($LASTEXITCODE -ne 0) { throw 'The private pip bootstrap failed validation.' }

        $payloadItems = @(Get-ChildItem -LiteralPath $payload -Force -ErrorAction Stop)
        foreach ($item in $payloadItems) {
            $target = Join-Path $destinationFull $item.Name
            try { Get-Item -LiteralPath $target -Force -ErrorAction Stop | Out-Null; $targetExists = $true }
            catch [System.Management.Automation.ItemNotFoundException] { $targetExists = $false }
            if ($targetExists) {
                throw 'Destination changed while the runtime was being prepared; no existing file was overwritten.'
            }
            Assert-CatsNoReparseAncestors -Path $target
            $manifest = Get-CatsPayloadManifest -Source $item.FullName -Target $target -Root $destinationFull
            [void]$movedPayload.Add($manifest)
            Move-Item -LiteralPath $item.FullName -Destination $target -ErrorAction Stop
            $manifest.Moved = $true
        }
        Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction Stop
        return $destinationFull
    } catch {
        foreach ($manifest in $movedPayload) {
            try { Remove-CatsMovedPayload -Manifest $manifest -Root $destinationFull } catch { }
        }
        if ($stage -and (Test-Path -LiteralPath $stage)) {
            try {
                Assert-CatsNoReparseBelow -Path $stage
                Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction Stop
            } catch { }
        }
        if ($destinationCreated -and (Test-Path -LiteralPath $destinationFull)) {
            try {
                Assert-CatsNoReparseAncestors -Path $destinationFull
                if (-not (Test-CatsReparsePoint -Path $destinationFull) -and
                    @(Get-ChildItem -LiteralPath $destinationFull -Force -ErrorAction Stop).Count -eq 0) {
                    Remove-Item -LiteralPath $destinationFull -Force -ErrorAction Stop
                }
            } catch { }
        }
        throw
    }
}

function Get-CatsRuntimePython {
    [CmdletBinding()]
    param([Parameter(Mandatory=$true)][string]$Runtime)

    $runtimeFull = ConvertTo-CatsAbsolutePath -Path $Runtime -Label 'Runtime'
    if (!(Test-Path -LiteralPath $runtimeFull -PathType Container)) {
        throw 'The requested runtime directory does not exist.'
    }
    Assert-CatsNoReparseAncestors -Path $runtimeFull
    $candidates = @(
        (Join-Path $runtimeFull 'python.exe'),
        (Join-Path $runtimeFull 'Scripts\python.exe')
    )
    foreach ($candidate in $candidates) {
        try { $candidateItem = Get-Item -LiteralPath $candidate -Force -ErrorAction Stop }
        catch [System.Management.Automation.ItemNotFoundException] { $candidateItem = $null }
        if ($candidateItem -and -not $candidateItem.PSIsContainer) {
            if (($candidateItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'The runtime Python executable must not be a reparse point.'
            }
            return $candidateItem.FullName
        }
    }
    throw 'No compatible runtime Python executable was found (checked python.exe and Scripts\python.exe).'
}
