param(
    [ValidateSet('install','start','stop','uninstall','status','doctor','open')]
    [string]$Action = 'install'
)

$ErrorActionPreference = 'Stop'

$scriptDirectory = [IO.Path]::GetFullPath($PSScriptRoot)
$project = [IO.Path]::GetFullPath((Join-Path $scriptDirectory '..'))
$defaultInstallRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'CodexUplink'))
$installRoot = $defaultInstallRoot
$script:ActionHandled = $false
$script:InstallOutput = @()

$controlNames = @(
    'INSTALL.cmd',
    'START-GATEWAY.cmd',
    'STOP-GATEWAY.cmd',
    'STATUS.cmd',
    'DOCTOR.cmd',
    'OPEN-CODEX.cmd',
    'UNINSTALL.cmd'
)

function Get-FullPath {
    param([Parameter(Mandatory)][string]$Path)
    return [IO.Path]::GetFullPath($Path)
}

function Test-ExistingPath {
    param([Parameter(Mandatory)][string]$Path)
    return Test-Path -LiteralPath $Path
}

function Ensure-Directory {
    param([Parameter(Mandatory)][string]$Path)
    [IO.Directory]::CreateDirectory((Get-FullPath $Path)) | Out-Null
}

function Assert-SafePath {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Root
    )

    $target = (Get-FullPath $Path).TrimEnd('\')
    $container = (Get-FullPath $Root).TrimEnd('\')
    if ($target -ne $container -and !$target.StartsWith($container + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Unsafe path outside the declared installation container.'
    }
    $current = $target
    while ($true) {
        if (Test-ExistingPath $current) {
            $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Reparse point found in installation path.'
            }
        }
        $parentInfo = [IO.Directory]::GetParent($current)
        if ($null -eq $parentInfo) { break }
        $parent = $parentInfo.FullName
        if ([string]::IsNullOrEmpty($parent) -or $parent -eq $current) { break }
        $current = $parent
    }
}

function Remove-SafeTree {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Root
    )

    $target = (Get-FullPath $Path).TrimEnd('\')
    $container = (Get-FullPath $Root).TrimEnd('\')
    if ($target -eq $container -or !$target.StartsWith($container + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Unsafe recursive cleanup path.'
    }
    $current = $container
    while ($true) {
        try { $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop }
        catch [System.Management.Automation.ItemNotFoundException] { throw 'Recursive cleanup parent is missing.' }
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Reparse point found in recursive cleanup path.'
        }
        $parentInfo = [IO.Directory]::GetParent($current)
        if ($null -eq $parentInfo) { break }
        $parent = $parentInfo.FullName
        if ([string]::IsNullOrEmpty($parent) -or $parent -eq $current) { break }
        $current = $parent
    }
    if (!(Test-ExistingPath $target)) { return }
    $targetItem = Get-Item -LiteralPath $target -Force -ErrorAction Stop
    if (($targetItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Reparse point found in recursive cleanup path.'
    }
    $links = @(Get-ChildItem -LiteralPath $target -Force -Recurse -ErrorAction Stop |
        Where-Object { ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 })
    if ($links.Count -gt 0) { throw 'Reparse point found in recursive cleanup path.' }
    Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction Stop
}

function Get-InstalledRuntimePython {
    param([Parameter(Mandatory)][string]$Runtime)

    $runtime = Get-FullPath $Runtime
    $legacy = Join-Path $runtime 'Scripts\python.exe'
    $candidate = $null
    $resolver = Get-Command Get-CatsRuntimePython -ErrorAction SilentlyContinue
    if ($resolver) {
        try {
            $values = @(Get-CatsRuntimePython -Runtime $runtime)
            if ($values.Count -gt 0) {
                $candidate = [string]($values | Select-Object -Last 1)
            }
        } catch {
            $candidate = $null
        }
    }
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        $candidate = $null
    }
    $candidates = @()
    if ($candidate) { $candidates += $candidate }
    if (!$candidate -or $candidate -ne $legacy) { $candidates += $legacy }
    foreach ($path in $candidates) {
        try {
            $candidateItem = Get-Item -LiteralPath $path -Force -ErrorAction Stop
        } catch [System.Management.Automation.ItemNotFoundException] {
            continue
        }
        if ($candidateItem.PSIsContainer) { continue }
        if (($candidateItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Runtime Python executable must not be a reparse point.'
        }
        return $candidateItem.FullName
    }
    return $null
}

function Invoke-NativeQuiet {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter(Mandatory)][string[]]$ArgumentList,
        [Parameter(Mandatory)][string]$FailureMessage
    )

    & $FilePath @ArgumentList 1>$null 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw $FailureMessage
    }
}

function Invoke-DesktopJson {
    param(
        [Parameter(Mandatory)][string]$Python,
        [Parameter(Mandatory)][string]$Home,
        [Parameter(Mandatory)][ValidateSet('stop','uninstall')][string]$Command
    )

    $output = @(& $Python '-m' 'uplink12.desktop' '--home' $Home $Command 2>$null)
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "Cannot safely $Command previous installation"
    }
    $text = ($output -join [Environment]::NewLine).Trim()
    if ([string]::IsNullOrWhiteSpace($text)) {
        throw "Cannot verify $Command result"
    }
    try {
        return ($text | ConvertFrom-Json)
    } catch {
        throw "Cannot verify $Command result"
    }
}

function Resolve-PhysicalInstallRoot {
    param(
        [Parameter(Mandatory)][string]$Python,
        [Parameter(Mandatory)][string]$Candidate
    )

    $output = @(& $Python '-c' 'from pathlib import Path;import sys;print(Path(sys.argv[1]).resolve())' $Candidate 2>$null)
    if ($LASTEXITCODE -ne 0) {
        throw 'Cannot resolve the installed runtime location'
    }
    $resolved = ($output | Select-Object -Last 1 | Out-String).Trim()
    if ([string]::IsNullOrWhiteSpace($resolved)) {
        throw 'Cannot resolve the installed runtime location'
    }
    return Get-FullPath $resolved
}

function ConvertTo-InstallProxy {
    param([AllowNull()][string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw 'Unsupported proxy configuration; PAC, SOCKS, credentials, or malformed HTTP(S) proxies are not supported.'
    }
    $value = $Value.Trim()
    if ($value.Contains(';') -or ($value.Contains('=') -and !$value.Contains('://'))) {
        $entries = @{}
        foreach ($part in $value.Split(';')) {
            if ([string]::IsNullOrWhiteSpace($part)) { continue }
            $pair = $part.Split('=',2)
            if ($pair.Count -eq 2) { $entries[$pair[0].Trim().ToLowerInvariant()] = $pair[1].Trim() }
        }
        $value = if ($entries.ContainsKey('https')) { $entries['https'] } elseif ($entries.ContainsKey('http')) { $entries['http'] } else { $null }
        if ([string]::IsNullOrWhiteSpace($value)) {
            throw 'Unsupported proxy configuration; PAC, SOCKS, credentials, or malformed HTTP(S) proxies are not supported.'
        }
    }
    if ($value -notmatch '^[A-Za-z][A-Za-z0-9+.-]*://') {
        $value = 'http://' + $value
    }
    try {
        $uri = [Uri]$value
        $port = $uri.Port
    } catch {
        throw 'Unsupported proxy configuration; PAC, SOCKS, credentials, or malformed HTTP(S) proxies are not supported.'
    }
    if ($uri.Scheme.ToLowerInvariant() -notin @('http','https') -or
        [string]::IsNullOrWhiteSpace($uri.Host) -or
        ![string]::IsNullOrEmpty($uri.UserInfo) -or
        (($uri.AbsolutePath -ne '/') -and ($uri.AbsolutePath -ne '')) -or
        ![string]::IsNullOrEmpty($uri.Query) -or
        ![string]::IsNullOrEmpty($uri.Fragment) -or
        $port -lt 1 -or $port -gt 65535) {
        throw 'Unsupported proxy configuration; PAC, SOCKS, credentials, or malformed HTTP(S) proxies are not supported.'
    }
    return $value.TrimEnd('/')
}

function Get-InstallProxy {
    foreach ($name in @('HTTPS_PROXY','https_proxy','HTTP_PROXY','http_proxy')) {
        $value = [Environment]::GetEnvironmentVariable($name)
        if (![string]::IsNullOrWhiteSpace($value)) {
            return ConvertTo-InstallProxy $value
        }
    }

    $keyPath = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
    $settings = Get-ItemProperty -LiteralPath $keyPath -ErrorAction SilentlyContinue
    if ($null -ne $settings -and ![string]::IsNullOrWhiteSpace([string]$settings.AutoConfigURL)) {
        throw 'PAC proxy configuration detected; set an explicit HTTP(S) proxy before installing.'
    }
    if ($null -ne $settings -and [int]$settings.ProxyEnable -ne 0) {
        return ConvertTo-InstallProxy ([string]$settings.ProxyServer)
    }
    return $null
}

function Import-CatsBootstrap {
    param([Parameter(Mandatory)][string]$Root)

    $path = Join-Path (Get-FullPath $Root) 'scripts\bootstrap.ps1'
    if (!(Test-ExistingPath $path)) {
        throw 'Runtime bootstrap is missing from the release package.'
    }
    . $path *> $null
    if (!(Get-Command New-CatsRuntime -ErrorAction SilentlyContinue) -or
        !(Get-Command Get-CatsRuntimePython -ErrorAction SilentlyContinue)) {
        throw 'Runtime bootstrap does not provide the required Cats runtime interface.'
    }
}

function New-TransactionRoot {
    param([Parameter(Mandatory)][string]$InstallPath)

    $installPath = Get-FullPath $InstallPath
    $parent = [IO.Path]::GetDirectoryName($installPath)
    if ([string]::IsNullOrWhiteSpace($parent)) {
        throw 'Cannot create an isolated installer staging directory.'
    }
    $parentInfo = [IO.Directory]::GetParent($parent)
    if ($null -eq $parentInfo) { throw 'Cannot create an isolated installer staging directory.' }
    Assert-SafePath -Path $parent -Root $parentInfo.FullName
    $name = '.CodexUplink-stage-' + [Guid]::NewGuid().ToString('N')
    $path = Join-Path $parent $name
    Assert-SafePath -Path $path -Root $parent
    Ensure-Directory $path
    return Get-FullPath $path
}

function Remove-TransactionRoot {
    param([AllowNull()][string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return }
    if (Test-ExistingPath $Path) {
        $resolved = Get-FullPath $Path
        $parentInfo = [IO.Directory]::GetParent($resolved)
        if ($null -ne $parentInfo) {
            try { Remove-SafeTree -Path $resolved -Root $parentInfo.FullName } catch { }
        }
    }
}

function New-StagedControls {
    param(
        [Parameter(Mandatory)][string]$SourceRoot,
        [Parameter(Mandatory)][string]$TransactionRoot
    )

    $transactionRoot = Get-FullPath $TransactionRoot
    $sourceRoot = Get-FullPath $SourceRoot
    $transactionParent = [IO.Directory]::GetParent($transactionRoot)
    $sourceParent = [IO.Directory]::GetParent($sourceRoot)
    if ($null -eq $transactionParent -or $null -eq $sourceParent) {
        throw 'Release staging path could not be resolved.'
    }
    Assert-SafePath -Path $transactionRoot -Root $transactionParent.FullName
    Assert-SafePath -Path $sourceRoot -Root $sourceParent.FullName
    $controls = Join-Path $transactionRoot 'controls'
    Assert-SafePath -Path $controls -Root $transactionRoot
    Ensure-Directory (Join-Path $controls 'scripts')
    foreach ($name in $controlNames) {
        $source = Join-Path $SourceRoot $name
        if (!(Test-ExistingPath $source)) { throw "Release entrypoint missing: $name" }
        $destination = Join-Path $controls $name
        Assert-SafePath -Path $source -Root $sourceRoot
        Assert-SafePath -Path $destination -Root $controls
        Copy-Item -LiteralPath $source -Destination $destination -Force
    }
    $windowsSource = Join-Path $SourceRoot 'scripts\windows.ps1'
    $bootstrapSource = Join-Path $SourceRoot 'scripts\bootstrap.ps1'
    if (!(Test-ExistingPath $windowsSource) -or !(Test-ExistingPath $bootstrapSource)) {
        throw 'Release runtime bootstrap or Windows installer is missing.'
    }
    $windowsDestination = Join-Path $controls 'scripts\windows.ps1'
    $bootstrapDestination = Join-Path $controls 'scripts\bootstrap.ps1'
    Assert-SafePath -Path $windowsSource -Root $sourceRoot
    Assert-SafePath -Path $bootstrapSource -Root $sourceRoot
    Assert-SafePath -Path $windowsDestination -Root $controls
    Assert-SafePath -Path $bootstrapDestination -Root $controls
    Copy-Item -LiteralPath $windowsSource -Destination $windowsDestination -Force
    Copy-Item -LiteralPath $bootstrapSource -Destination $bootstrapDestination -Force
    return Get-FullPath $controls
}

function Backup-And-InstallControls {
    param(
        [Parameter(Mandatory)][string]$InstallPath,
        [Parameter(Mandatory)][string]$StagedControls,
        [Parameter(Mandatory)][string]$BackupRoot
    )

    $installParent = [IO.Directory]::GetParent((Get-FullPath $InstallPath))
    $backupParent = [IO.Directory]::GetParent((Get-FullPath $BackupRoot))
    $stagedParent = [IO.Directory]::GetParent((Get-FullPath $StagedControls))
    if ($null -eq $installParent -or $null -eq $backupParent -or $null -eq $stagedParent) {
        throw 'Control deployment path could not be resolved.'
    }
    Assert-SafePath -Path $InstallPath -Root $installParent.FullName
    Assert-SafePath -Path $BackupRoot -Root $backupParent.FullName
    Assert-SafePath -Path $StagedControls -Root $stagedParent.FullName
    Assert-SafePath -Path (Join-Path $InstallPath 'scripts') -Root (Get-FullPath $InstallPath)
    Assert-SafePath -Path (Join-Path $BackupRoot 'scripts') -Root (Get-FullPath $BackupRoot)
    Ensure-Directory (Join-Path $BackupRoot 'scripts')
    Ensure-Directory (Join-Path $InstallPath 'scripts')
    $allNames = @($controlNames + 'scripts\windows.ps1' + 'scripts\bootstrap.ps1')
    $backed = New-Object System.Collections.Generic.List[string]
    $installed = New-Object System.Collections.Generic.List[string]
    $state = @{ Backed = $backed; Installed = $installed }
    try {
        foreach ($name in $allNames) {
            $destination = Join-Path $InstallPath $name
            $source = Join-Path $StagedControls $name
            $backup = Join-Path $BackupRoot $name
            Assert-SafePath -Path $destination -Root (Get-FullPath $InstallPath)
            Assert-SafePath -Path $source -Root (Get-FullPath $StagedControls)
            Assert-SafePath -Path $backup -Root (Get-FullPath $BackupRoot)
            if (Test-ExistingPath $destination) {
                Move-Item -LiteralPath $destination -Destination $backup -Force
                $backed.Add($name)
            }
            Move-Item -LiteralPath $source -Destination $destination -Force
            $installed.Add($name)
        }
        return $state
    } catch {
        try { Restore-Controls -InstallPath $InstallPath -BackupRoot $BackupRoot -ControlState $state } catch { }
        throw 'Control script deployment failed; previous scripts were retained.'
    }
}

function Restore-Controls {
    param(
        [Parameter(Mandatory)][string]$InstallPath,
        [Parameter(Mandatory)][string]$BackupRoot,
        [Parameter(Mandatory)]$ControlState
    )

    $installParent = [IO.Directory]::GetParent((Get-FullPath $InstallPath))
    $backupParent = [IO.Directory]::GetParent((Get-FullPath $BackupRoot))
    if ($null -eq $installParent -or $null -eq $backupParent) {
        throw 'Control restore path could not be resolved.'
    }
    Assert-SafePath -Path $InstallPath -Root $installParent.FullName
    Assert-SafePath -Path $BackupRoot -Root $backupParent.FullName
    foreach ($name in $ControlState.Installed) {
        $destination = Join-Path $InstallPath $name
        Assert-SafePath -Path $destination -Root (Get-FullPath $InstallPath)
        if (Test-ExistingPath $destination) {
            Remove-Item -LiteralPath $destination -Force -ErrorAction SilentlyContinue
            if (Test-ExistingPath $destination) { throw 'Could not remove staged control script during rollback.' }
        }
    }
    foreach ($name in $ControlState.Backed) {
        $backup = Join-Path $BackupRoot $name
        $destination = Join-Path $InstallPath $name
        Assert-SafePath -Path $backup -Root (Get-FullPath $BackupRoot)
        Assert-SafePath -Path $destination -Root (Get-FullPath $InstallPath)
        if (!(Test-ExistingPath $backup)) { throw 'Previous control script backup is missing.' }
        Move-Item -LiteralPath $backup -Destination $destination -Force
        if (!(Test-ExistingPath $destination)) { throw 'Previous control script could not be restored.' }
    }
}

function Get-EnabledState {
    param([Parameter(Mandatory)][string]$InstallPath)
    $settings = Join-Path $InstallPath 'settings.json'
    if (!(Test-ExistingPath $settings)) { return $false }
    try {
        $doc = Get-Content -LiteralPath $settings -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($null -eq $doc.enabled) { return $false }
        if ($doc.enabled -is [bool]) { return $doc.enabled }
        if ($doc.enabled -is [string]) {
            switch ($doc.enabled.Trim().ToLowerInvariant()) {
                'true' { return $true }
                'false' { return $false }
            }
        }
        throw 'enabled must be a boolean'
    } catch {
        throw 'Existing installation settings are invalid; refusing to replace the runtime.'
    }
}

function Commit-StagedRuntime {
    param(
        [Parameter(Mandatory)][string]$InstallPath,
        [Parameter(Mandatory)][string]$StagedRuntime,
        [Parameter(Mandatory)][string]$StagedControls,
        [Parameter(Mandatory)][string]$TransactionRoot
    )

    $installRootExisted = Test-ExistingPath $InstallPath
    $oldRuntime = Join-Path $InstallPath 'runtime'
    $oldRuntimeBackup = Join-Path $TransactionRoot 'old-runtime'
    $controlBackup = Join-Path $TransactionRoot 'old-controls'
    $controlState = $null
    $runtimeMoved = $false
    try {
        $installParent = [IO.Directory]::GetParent((Get-FullPath $InstallPath))
        $transactionParent = [IO.Directory]::GetParent((Get-FullPath $TransactionRoot))
        if ($null -eq $installParent -or $null -eq $transactionParent) {
            throw 'Runtime replacement path could not be resolved.'
        }
        Assert-SafePath -Path $InstallPath -Root $installParent.FullName
        Assert-SafePath -Path $TransactionRoot -Root $transactionParent.FullName
        Ensure-Directory $InstallPath
        Assert-SafePath -Path $oldRuntime -Root (Get-FullPath $InstallPath)
        Assert-SafePath -Path $oldRuntimeBackup -Root (Get-FullPath $TransactionRoot)
        Assert-SafePath -Path $StagedRuntime -Root (Get-FullPath $TransactionRoot)
        if (Test-ExistingPath $oldRuntime) {
            Move-Item -LiteralPath $oldRuntime -Destination $oldRuntimeBackup -Force
            $runtimeMoved = $true
        }
        Move-Item -LiteralPath $StagedRuntime -Destination $oldRuntime -Force
        $controlState = Backup-And-InstallControls -InstallPath $InstallPath -StagedControls $StagedControls -BackupRoot $controlBackup
        return @{
            OldRuntime = $oldRuntimeBackup
            RuntimePath = $oldRuntime
            ControlBackup = $controlBackup
            ControlState = $controlState
            HadOldRuntime = $runtimeMoved
            HadInstallRoot = $installRootExisted
            TransactionRoot = $TransactionRoot
        }
    } catch {
        if ($controlState) {
            Restore-Controls -InstallPath $InstallPath -BackupRoot $controlBackup -ControlState $controlState
        }
        Assert-SafePath -Path $oldRuntime -Root (Get-FullPath $InstallPath)
        if (Test-ExistingPath $oldRuntime) {
            Remove-SafeTree -Path $oldRuntime -Root $InstallPath
        }
        Assert-SafePath -Path $oldRuntimeBackup -Root (Get-FullPath $TransactionRoot)
        if ($runtimeMoved -and (Test-ExistingPath $oldRuntimeBackup)) {
            Move-Item -LiteralPath $oldRuntimeBackup -Destination $oldRuntime -Force
        }
        throw 'Runtime replacement failed; previous runtime was retained.'
    }
}

function Restore-StagedRuntime {
    param(
        [Parameter(Mandatory)][string]$InstallPath,
        [Parameter(Mandatory)]$CommitState
    )

    if ($CommitState.ControlState) {
        Restore-Controls -InstallPath $InstallPath -BackupRoot $CommitState.ControlBackup -ControlState $CommitState.ControlState
    }
    $runtime = Join-Path $InstallPath 'runtime'
    Assert-SafePath -Path $runtime -Root (Get-FullPath $InstallPath)
    if (Test-ExistingPath $runtime) {
        Remove-SafeTree -Path $runtime -Root $InstallPath
    }
    if ($CommitState.HadOldRuntime) {
        Assert-SafePath -Path $CommitState.OldRuntime -Root (Get-FullPath $CommitState.TransactionRoot)
        if (!(Test-ExistingPath $CommitState.OldRuntime)) { throw 'Previous runtime backup is missing.' }
        Assert-SafePath -Path $runtime -Root (Get-FullPath $InstallPath)
        Move-Item -LiteralPath $CommitState.OldRuntime -Destination $runtime -Force
        if (!(Test-ExistingPath $runtime)) { throw 'Previous runtime could not be restored.' }
    }
}

function Test-CleanupPath {
    param([Parameter(Mandatory)][string]$Path)

    $resolved = (Get-FullPath $Path).TrimEnd('\')
    $allowed = @(
        (Get-FullPath (Join-Path $env:LOCALAPPDATA 'CodexUplink')).TrimEnd('\'),
        (Get-FullPath (Join-Path $env:LOCALAPPDATA 'Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\CodexUplink')).TrimEnd('\')
    )
    if ($allowed -notcontains $resolved) { throw 'Unsafe cleanup path' }
    $root = Get-Item -LiteralPath $resolved -ErrorAction Stop
    if ($root.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) { throw 'Unsafe cleanup path' }
    $links = Get-ChildItem -LiteralPath $resolved -Force -Recurse -ErrorAction Stop |
        Where-Object { $_.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint) }
    if ($links) { throw 'Junction or symlink found; automatic directory removal refused. Integration is already removed.' }
    return $resolved
}

function Install-FromSource {
    param([Parameter(Mandatory)][string]$SourceRoot)

    $sourceRoot = Get-FullPath $SourceRoot
    Import-CatsBootstrap -Root $sourceRoot
    $oldRuntimePath = Join-Path $installRoot 'runtime'
    $oldRuntimeExists = Test-ExistingPath $oldRuntimePath
    $oldRuntimePython = Get-InstalledRuntimePython -Runtime $oldRuntimePath
    if ($oldRuntimeExists -and !$oldRuntimePython) {
        throw 'Existing runtime is not compatible; refusing to replace it.'
    }
    if ($oldRuntimePython) {
        $installRoot = Resolve-PhysicalInstallRoot -Python $oldRuntimePython -Candidate $installRoot
        $oldRuntimePath = Join-Path $installRoot 'runtime'
        $oldRuntimeExists = Test-ExistingPath $oldRuntimePath
        $oldRuntimePython = Get-InstalledRuntimePython -Runtime $oldRuntimePath
        if ($oldRuntimeExists -and !$oldRuntimePython) {
            throw 'Existing runtime is not compatible; refusing to replace it.'
        }
    }
    $oldEnabled = Get-EnabledState -InstallPath $installRoot
    $transaction = New-TransactionRoot -InstallPath $installRoot
    $stagedRuntime = Join-Path $transaction 'runtime'
    $stagedControls = $null
    $commitState = $null
    $oldStopped = $false
    $newRuntimePython = $null
    $commitAttempted = $false
    $preserveTransaction = $false
    try {
        $proxy = Get-InstallProxy
        Assert-SafePath -Path $stagedRuntime -Root (Get-FullPath $transaction)
        Ensure-Directory $stagedRuntime
        $null = New-CatsRuntime -Destination (Get-FullPath $stagedRuntime)
        $newRuntimePython = Get-InstalledRuntimePython -Runtime $stagedRuntime
        if (!$newRuntimePython) { throw 'Runtime bootstrap did not create a usable Python runtime.' }

        $pipArgs = @('-m','pip','--isolated','install','--no-input','--disable-pip-version-check','--only-binary=:all:','--upgrade')
        if ($proxy) { $pipArgs += @('--proxy',$proxy) }
        $pipArgs += $sourceRoot
        Invoke-NativeQuiet -FilePath $newRuntimePython -ArgumentList $pipArgs -FailureMessage 'Dependency installation failed; previous installation was not changed.'
        Invoke-NativeQuiet -FilePath $newRuntimePython -ArgumentList @('-c','import aiohttp,multidict,websockets,yarl,tomlkit,zstandard,uplink12.desktop') -FailureMessage 'Runtime dependency smoke test failed; previous installation was not changed.'
        Invoke-NativeQuiet -FilePath $newRuntimePython -ArgumentList @('-m','uplink12.desktop','--help') -FailureMessage 'Desktop command smoke test failed; previous installation was not changed.'
        $stagedControls = New-StagedControls -SourceRoot $sourceRoot -TransactionRoot $transaction

        if ($oldRuntimePython) {
            $oldStop = Invoke-DesktopJson -Python $oldRuntimePython -Home $installRoot -Command 'stop'
            if ($oldStop.stopped -ne $true) { throw 'Previous gateway did not stop; staging was discarded.' }
            $oldStopped = $true
        }

        $commitAttempted = $true
        $commitState = Commit-StagedRuntime -InstallPath $installRoot -StagedRuntime $stagedRuntime -StagedControls $stagedControls -TransactionRoot $transaction
        $newRuntimePython = Get-InstalledRuntimePython -Runtime (Join-Path $installRoot 'runtime')
        if (!$newRuntimePython) { throw 'Committed runtime is not usable.' }
        $installRoot = Resolve-PhysicalInstallRoot -Python $newRuntimePython -Candidate $installRoot
        $newRuntimePython = Get-InstalledRuntimePython -Runtime (Join-Path $installRoot 'runtime')
        if (!$newRuntimePython) { throw 'Committed runtime location is not usable.' }

        $activationOutput = @(& $newRuntimePython '-m' 'uplink12.desktop' '--home' $installRoot 'install' 2>$null)
        if ($LASTEXITCODE -ne 0) { throw 'New installation activation failed; previous installation is being restored.' }
        $activationText = ($activationOutput -join [Environment]::NewLine).Trim()
        try {
            $activation = $activationText | ConvertFrom-Json
        } catch {
            throw 'New installation returned an unverifiable activation result; previous installation is being restored.'
        }
        if ($activation.installed -ne $true -or $activation.gateway_ready -ne $true -or $activation.autostart -ne $true) {
            throw 'New installation did not confirm gateway readiness and login startup; previous installation is being restored.'
        }
        $script:InstallOutput = $activationOutput
        $script:ActionHandled = $true
        return $installRoot
    } catch {
        $failure = $_.Exception.Message
        if ($commitAttempted -and !$commitState) { $preserveTransaction = $true }
        if ($commitState) {
            $newStopVerified = $false
            $newSettings = Join-Path $installRoot 'settings.json'
            if (Test-ExistingPath $newSettings) {
                if ($newRuntimePython -and (Test-ExistingPath $newRuntimePython)) {
                    try {
                        $newStop = Invoke-DesktopJson -Python $newRuntimePython -Home $installRoot -Command 'stop'
                        $newStopVerified = ($newStop.stopped -eq $true)
                    } catch { }
                }
            }
            if (!$newStopVerified) {
                $preserveTransaction = $true
                throw 'Installation failed and the new gateway could not be stopped; keep the installation directory for recovery.'
            }
            try {
                Restore-StagedRuntime -InstallPath $installRoot -CommitState $commitState
            } catch {
                $preserveTransaction = $true
                throw 'Installation failed and automatic runtime rollback also failed; keep the installation directory for recovery.'
            }
        }
        if ($oldStopped -and $oldEnabled) {
            $restoredPython = Get-InstalledRuntimePython -Runtime (Join-Path $installRoot 'runtime')
            if ($restoredPython) {
                & $restoredPython '-m' 'uplink12.desktop' '--home' $installRoot 'install' 1>$null 2>$null
                if ($LASTEXITCODE -ne 0) {
                    throw 'Installation failed; previous runtime was restored but could not be re-enabled automatically.'
                }
            } else {
                throw 'Installation failed; previous runtime was restored but could not be re-enabled automatically.'
            }
        }
        if ($commitState -and !$commitState.HadOldRuntime -and !$commitState.HadInstallRoot -and (Test-ExistingPath $installRoot)) {
            try {
                $safeNewRoot = Test-CleanupPath -Path $installRoot
                Remove-Item -LiteralPath $safeNewRoot -Recurse -Force -ErrorAction Stop
            } catch {
                $preserveTransaction = $true
                throw 'Installation failed; the new directory could not be safely removed. Keep it for recovery.'
            }
        }
        throw $failure
    } finally {
        if (!$preserveTransaction) { Remove-TransactionRoot -Path $transaction }
    }
}

$sourceHasProject = Test-ExistingPath (Join-Path $project 'pyproject.toml')
$installedRuntimePython = $null
if (!$sourceHasProject -and (Test-ExistingPath (Join-Path $project 'runtime'))) {
    $installRoot = $project
}
$bootstrapForCurrent = Join-Path $project 'scripts\bootstrap.ps1'
if (Test-ExistingPath $bootstrapForCurrent) {
    . $bootstrapForCurrent *> $null
}

if ($Action -eq 'install' -and $sourceHasProject) {
    $installRoot = Install-FromSource -SourceRoot $project
    $installedRuntimePython = Get-InstalledRuntimePython -Runtime (Join-Path $installRoot 'runtime')
} else {
    $installedRuntimePython = Get-InstalledRuntimePython -Runtime (Join-Path $installRoot 'runtime')
}

if (!$installedRuntimePython) {
    throw 'Not installed. Run INSTALL.cmd from the release package first.'
}

$installRoot = Resolve-PhysicalInstallRoot -Python $installedRuntimePython -Candidate $installRoot
$runtimePython = Get-InstalledRuntimePython -Runtime (Join-Path $installRoot 'runtime')
if (!$runtimePython) { throw 'Installed runtime could not be resolved.' }

if ($script:ActionHandled) {
    $script:InstallOutput | Write-Output
    return
}

if ($Action -in @('stop','uninstall')) {
    $result = Invoke-DesktopJson -Python $runtimePython -Home $installRoot -Command $Action
    if ($result.stopped -ne $true) {
        throw 'Gateway did not stop; installation was retained for a safe retry.'
    }
    if ($Action -eq 'uninstall') {
        $resolved = Test-CleanupPath -Path $installRoot
        Remove-Item -LiteralPath $resolved -Recurse -Force
        Write-Output 'Uninstalled. Codex configuration restored; original proxy and certificates unchanged.'
    }
    return
}

& $runtimePython '-m' 'uplink12.desktop' '--home' $installRoot $Action
if ($LASTEXITCODE -ne 0) {
    throw 'Command failed; see the sanitized error above.'
}
