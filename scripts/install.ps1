# Put the herdr-bar binary at bin\herdr-bar.exe for this checkout.
#
# Herdr runs this as the plugin's Windows [[build]] step on `herdr plugin
# install` (updating is reinstalling; `plugin link` never runs it: run it
# yourself for a local checkout). It downloads the prebuilt binary of the manifest's version
# from the GitHub release, checks it against the release's SHA256SUMS, and
# falls back to `cargo build --release` (Rust 1.88+) when there is no binary for this
# machine or the download or the check fails.
#
#   HERDR_BAR_BUILD=source     skip the download and build with cargo
#   HERDR_BAR_DOWNLOAD_BASE=   release download URL to use instead of GitHub's
#
# The manifest's shared commands name `bin/herdr-bar`, so bin\herdr-bar (no
# extension) is installed beside bin\herdr-bar.exe: launchers that append .exe
# find the one, launchers that take the path as written find the other.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2

$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
$BinDir = Join-Path $Root 'bin'
$Target = Join-Path $BinDir 'herdr-bar.exe'
$Alias = Join-Path $BinDir 'herdr-bar'
$RepoUrl = 'https://github.com/rob-64/herdr-bar'

function Say([string]$Message) { [Console]::Error.WriteLine("herdr-bar: $Message") }

$Version = $null
foreach ($Line in Get-Content -LiteralPath (Join-Path $Root 'herdr-plugin.toml')) {
    if ($Line -match '^version\s*=\s*"([^"]*)"') { $Version = $Matches[1]; break }
}
if (-not $Version) {
    Say 'cannot read the version from herdr-plugin.toml'
    exit 1
}

function Get-AssetName {
    $Arch = $null
    try {
        $Arch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
    } catch {
        # Windows PowerShell on an old .NET: a 32-bit shell on 64-bit Windows
        # reports the native architecture in PROCESSOR_ARCHITEW6432.
        $Arch = $env:PROCESSOR_ARCHITEW6432
        if (-not $Arch) { $Arch = $env:PROCESSOR_ARCHITECTURE }
    }
    switch -Regex ($Arch) {
        '^(X64|AMD64)$' { return 'herdr-bar-windows-x86_64.exe' }
        '^ARM64$' { return 'herdr-bar-windows-aarch64.exe' }
        default { return $null }
    }
}

# Run a native command without PowerShell 5.1 turning its stderr output into a
# terminating NativeCommandError under $ErrorActionPreference = 'Stop'.
# Returns the exit code; stdout goes to $script:NativeOutput.
function Invoke-Native([string]$Command, [string[]]$Arguments, [switch]$ShowOutput) {
    $Saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($ShowOutput) {
            & $Command @Arguments 2>&1 | ForEach-Object { [Console]::Error.WriteLine("$_") }
            $script:NativeOutput = ''
        } else {
            $script:NativeOutput = (& $Command @Arguments 2>$null | Out-String).Trim()
        }
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $Saved
    }
}

# Windows cannot overwrite a running .exe but can rename it, so an existing
# binary is moved aside to <name>.old.<pid> before the new one is renamed in.
# The .exe and the extensionless alias are staged first, then swapped, and a
# failed swap restores whatever was moved aside.
function Install-Binary([string]$Source) {
    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
    foreach ($Stale in @(Get-ChildItem -LiteralPath $BinDir -Filter 'herdr-bar*.old.*' -ErrorAction SilentlyContinue)) {
        Remove-Item -LiteralPath $Stale.FullName -Force -ErrorAction SilentlyContinue
    }
    $Pairs = @()
    try {
        foreach ($Destination in @($Target, $Alias)) {
            $Temp = "$Destination.tmp.$PID"
            Copy-Item -LiteralPath $Source -Destination $Temp -Force
            $Pairs += ,@($Destination, $Temp, "$Destination.old.$PID")
        }
    } catch {
        foreach ($Pair in $Pairs) { Remove-Item -LiteralPath $Pair[1] -Force -ErrorAction SilentlyContinue }
        throw
    }
    $Done = @()
    try {
        foreach ($Pair in $Pairs) {
            if (Test-Path -LiteralPath $Pair[0]) {
                Move-Item -LiteralPath $Pair[0] -Destination $Pair[2] -Force
            }
            Move-Item -LiteralPath $Pair[1] -Destination $Pair[0] -Force
            $Done += ,$Pair
        }
    } catch {
        foreach ($Pair in $Pairs) {
            Remove-Item -LiteralPath $Pair[1] -Force -ErrorAction SilentlyContinue
            if (Test-Path -LiteralPath $Pair[2]) {
                $IsDone = @($Done | Where-Object { $_[0] -eq $Pair[0] }).Count -gt 0
                if ($IsDone) {
                    Remove-Item -LiteralPath $Pair[0] -Force -ErrorAction SilentlyContinue
                }
                if (-not (Test-Path -LiteralPath $Pair[0])) {
                    Move-Item -LiteralPath $Pair[2] -Destination $Pair[0] -Force -ErrorAction SilentlyContinue
                }
            }
        }
        throw
    }
    # The old copies may still be running; deleting them is best-effort and
    # the next install retries.
    foreach ($Pair in $Pairs) { Remove-Item -LiteralPath $Pair[2] -Force -ErrorAction SilentlyContinue }
}

function Invoke-Download {
    $Asset = Get-AssetName
    if (-not $Asset) {
        Say "no prebuilt binary for this architecture"
        return $false
    }
    $Base = $env:HERDR_BAR_DOWNLOAD_BASE
    if (-not $Base) { $Base = "$RepoUrl/releases/download/v$Version" }
    if ($Base -notmatch '^(https|file)://') { Say "warning: HERDR_BAR_DOWNLOAD_BASE is not https: $Base" }
    $Temp = Join-Path ([System.IO.Path]::GetTempPath()) ("herdr-bar-" + [System.Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $Temp | Out-Null
    try {
        Say "downloading $Asset $Version"
        try {
            # Windows PowerShell 5.1 still defaults to TLS 1.0 on some systems.
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        } catch {}
        $ProgressPreference = 'SilentlyContinue'
        $File = Join-Path $Temp $Asset
        $Sums = Join-Path $Temp 'SHA256SUMS'
        try {
            Invoke-WebRequest -UseBasicParsing -TimeoutSec 300 -Uri "$Base/$Asset" -OutFile $File
            Invoke-WebRequest -UseBasicParsing -TimeoutSec 300 -Uri "$Base/SHA256SUMS" -OutFile $Sums
        } catch {
            Say "download failed: $($_.Exception.Message)"
            return $false
        }
        $Expected = $null
        foreach ($Line in Get-Content -LiteralPath $Sums) {
            $Fields = $Line.Trim() -split '\s+', 2
            if ($Fields.Count -eq 2 -and ($Fields[1] -eq $Asset -or $Fields[1] -eq "*$Asset")) {
                $Expected = $Fields[0].ToLowerInvariant()
                break
            }
        }
        if (-not $Expected) {
            Say "SHA256SUMS has no entry for $Asset"
            return $false
        }
        $Actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $File).Hash.ToLowerInvariant()
        if ($Expected -ne $Actual) {
            Say "checksum mismatch for $Asset (expected $Expected, got $Actual)"
            return $false
        }
        # Run it from bin\ rather than %TEMP%, which AppLocker or similar
        # policies may block, and only keep it if it reports this version.
        New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
        $Check = Join-Path $BinDir ".herdr-bar.check.$PID.exe"
        Copy-Item -LiteralPath $File -Destination $Check -Force
        try {
            $Code = Invoke-Native $Check @('--version')
            if ($Code -ne 0) {
                Say "the downloaded $Asset does not run on this machine"
                return $false
            }
            if ($script:NativeOutput -ne "herdr-bar $Version") {
                Say "downloaded binary reports '$($script:NativeOutput)', expected 'herdr-bar $Version'"
                return $false
            }
            Install-Binary $Check
        } finally {
            Remove-Item -LiteralPath $Check -Force -ErrorAction SilentlyContinue
        }
        return $true
    } catch {
        Say "download failed: $($_.Exception.Message)"
        return $false
    } finally {
        Remove-Item -LiteralPath $Temp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-SourceBuild {
    if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
        Say 'cargo is not installed (building herdr-bar needs Rust 1.88 or newer)'
        return $false
    }
    Say 'building from source with cargo'
    $CargoArgs = @('build', '--release')
    if (Test-Path -LiteralPath (Join-Path $Root 'Cargo.lock')) { $CargoArgs += '--locked' }
    try {
        # target\ is left in place: in a development clone it is the build cache.
        $Code = Invoke-Native 'cargo' $CargoArgs -ShowOutput
        if ($Code -ne 0) {
            Say "cargo build failed (exit $Code; herdr-bar needs Rust 1.88 or newer)"
            return $false
        }
        Install-Binary (Join-Path $Root 'target\release\herdr-bar.exe')
        return $true
    } catch {
        Say "cargo build failed: $($_.Exception.Message)"
        return $false
    }
}

if ($env:HERDR_BAR_BUILD -ne 'source' -and (Invoke-Download)) {
    Say "installed $Target"
    exit 0
}
if (Invoke-SourceBuild) {
    Say "installed $Target"
    exit 0
}
Say "could not install herdr-bar ${Version}: no prebuilt binary could be downloaded"
Say 'and building from source failed. Install Rust 1.88 or newer'
Say '(https://rustup.rs), then reinstall the plugin:'
Say '  herdr plugin install rob-64/herdr-bar'
exit 1
