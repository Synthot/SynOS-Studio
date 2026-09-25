# Builds this configuration bundle into an installable image (Windows).
#
#   .\build.ps1            build; the ISO and its evidence land in .\dist
#   .\build.ps1 check      only check that this machine can build (installs nothing)
#   .\build.ps1 update     fetch the latest launcher scripts and replace these four files, then exit
#   .\build.ps1 -Yes       answer yes to the questions (install Docker Desktop)
#
# Needs Docker Desktop or Podman Desktop (WSL 2 backend) and 40 GB free.
# When neither is present it offers to install Docker Desktop with winget.
# Nothing else is installed: the SynOS build engine runs inside a container
# image published for the exact engine version this bundle was made for.
#
# Where the build's bytes land needs nothing from you on Podman: this script
# works out on its own that the disk to use is the one this bundle was
# unpacked to, and keeps images, layers and the chroot in .build\ right next
# to it. It says so once, the run that creates that directory, and always
# as an absolute path even when a relative one was given (a relative one
# is resolved from this bundle's own directory, before anything uses it -
# the runtime does not necessarily share this script's own working
# directory by the time it runs). A location inside the engine source or dist\ is
# refused, naming both paths: either one is copied, archived or hashed
# whole as part of the build, and storage written there mid-build corrupts
# it. -Storage X:\path (or SYNOS_CONTAINER_ROOT) puts it somewhere else
# instead - a person passes -Storage; a service sets the variable for
# unattended use.
# -ContainerRoot still works, an older spelling of the same flag. This is
# passed straight to Podman the way build.sh does, but has not been
# exercised on Windows by the project: Podman Desktop on Windows commonly
# talks to a WSL 2 machine, whose own virtual disk (not a Windows path) may
# be what actually needs to grow instead (Podman's machine set --disk-size,
# or move the WSL distribution with wsl --manage <name> --move).
# -ContainerRunroot/SYNOS_CONTAINER_RUNROOT does the same for Podman's small
# state directory, which otherwise stays at its own default. Docker
# Desktop's storage is one setting for the whole engine; there is no
# per-build override, and this script says so instead of guessing.
#
# With Podman, a real choice remains only when the bundle's own disk cannot
# back a container's overlay at all (FAT32/exFAT, common on a removable
# drive): then, and only then (a terminal, no -Yes), this script asks once
# whether to use Podman's own storage instead, and remembers the answer in
# .build\container-root so the next run is never asked again ("forget" it:
# del .build\container-root). -Storage/SYNOS_CONTAINER_ROOT/-ContainerRoot
# always win over the remembered value, and update it.
#
# Channels: a bundle is generated for one of two engine pipelines, recorded
# in its own bundle.json as "channel" ("stable" when the key is absent, so
# every bundle that already exists keeps working exactly as it does today).
# stable builds against the newest *released* engine that satisfies this
# bundle (a GitHub release tag, never a branch tip); development builds
# against the unreleased tip of the engine's main branch. SYNOS_CHANNEL (or
# -Channel) overrides the bundle's own value for a person who knows what
# they are doing. A development build says so plainly in this script's own
# output, in dist\build.log, and in the image it is built from.
#
# Environment: SYNOS_BUILDER_IMAGE (use another image), SYNOS_IMAGE_REPOSITORY
#   (another registry, default ghcr.io/synthot/synos-builder), SYNOS_ENGINE_SOURCE
#   (an engine checkout to build the image from), SYNOS_ENGINE_URL (source archive),
#   SYNOS_LAUNCHER_URL (source for `update`, default the engine's tools/ on GitHub),
#   SYNOS_CHANNEL=stable|development (override the bundle's own channel; see above),
#   SYNOS_NO_UPDATE_CHECK=1 (skip the launcher update check at the start of
#   check/build), SYNOS_YES=1, SYNOS_CONTAINER_ROOT (Podman only, same as
#   -Storage; prefer -Storage by hand, the variable for a service) and
#   SYNOS_CONTAINER_RUNROOT (Podman only; see above).
[CmdletBinding()]
param([string]$Command = "", [switch]$Yes, [string]$Storage = "", [string]$ContainerRoot = "", [string]$ContainerRunroot = "", [string]$Channel = "")
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
if ($env:SYNOS_YES) { $Yes = $true }
if ($Storage) { $ContainerRoot = $Storage }
if (-not $ContainerRoot -and $env:SYNOS_CONTAINER_ROOT) { $ContainerRoot = $env:SYNOS_CONTAINER_ROOT }
if (-not $ContainerRunroot -and $env:SYNOS_CONTAINER_RUNROOT) { $ContainerRunroot = $env:SYNOS_CONTAINER_RUNROOT }

# Resolves a path to an absolute one, from this bundle's own directory
# ($PSScriptRoot, set as the location just above): a relative storage
# location or engine checkout is used again later, sometimes after
# Build-EngineImage's own Push-Location, where a relative path would be
# interpreted by whatever directory the runtime finds itself started in
# instead. Made absolute once, here, so it is unambiguous everywhere after.
function Resolve-AbsolutePath([string]$Path) {
    if ([System.IO.Path]::IsPathRooted($Path)) { return $Path }
    return [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot $Path))
}
if ($ContainerRoot) { $ContainerRoot = Resolve-AbsolutePath $ContainerRoot }
if ($ContainerRunroot) { $ContainerRunroot = Resolve-AbsolutePath $ContainerRunroot }
if ($env:SYNOS_ENGINE_SOURCE) { $env:SYNOS_ENGINE_SOURCE = Resolve-AbsolutePath $env:SYNOS_ENGINE_SOURCE }

# Refuses a storage location inside a directory this script is about to
# archive, copy or hash whole: "COPY . /opt/synos" in the engine's own
# Containerfile walks everything under the engine source, storage included;
# the evidence files next to the ISO (the SBOM, the checksums) are
# generated from dist\ the same way.
function Test-StorageInside([string]$ContextDir, [string]$ContextDescription) {
    if (-not $ContainerRoot) { return }
    $context = Resolve-AbsolutePath $ContextDir
    $normalizedContext = $context.TrimEnd('\') + '\'
    $normalizedStorage = $ContainerRoot.TrimEnd('\') + '\'
    if ($normalizedStorage.StartsWith($normalizedContext, [System.StringComparison]::OrdinalIgnoreCase) -or
        ($ContainerRoot -ieq $context)) {
        Fail "the build's storage ($ContainerRoot) is inside $ContextDescription ($context), which this build copies, archives or hashes whole; point -Storage at a location outside it" 2
    }
}

function Fail([string]$Message, [int]$Code = 1) { Write-Host "error: $Message" -ForegroundColor Red; exit $Code }
foreach ($value in @($Channel, $env:SYNOS_CHANNEL)) {
    if ($value -and $value -notin @("stable", "development")) { Fail "channel must be 'stable' or 'development' (got '$value')" }
}
function Ask([string]$Question) {
    if ($Yes) { return $true }
    $answer = Read-Host "$Question [y/N]"
    return $answer -match '^(y|yes)$'
}
function AskYes([string]$Question) {  # default: yes
    if ($Yes) { return $true }
    if ([Console]::IsInputRedirected) { return $false }
    $answer = Read-Host "$Question [Y/n]"
    return -not ($answer -match '^(n|no)$')
}
function Field([string]$File, [string]$Key) {
    $line = Get-Content -Path $File | Where-Object { $_ -match "^$Key\s*:" } | Select-Object -First 1
    if (-not $line) { return "" }
    return ($line -replace "^$Key\s*:\s*", "" -replace '\s*#.*$', "" -replace '^"|"$', "").Trim()
}

# ------------------------------------------------------------- the launchers
# Two files are "the same" launcher when they agree ignoring CR bytes: a bundle
# applied on a system that turns build.cmd's CRLF into LF must not be offered
# an update forever over a line-ending difference alone. A real change is
# still written with the CRLF bytes exactly as fetched.
function Test-SameIgnoringCr([string]$PathA, [string]$PathB) {
    if (-not (Test-Path $PathA -PathType Leaf) -or -not (Test-Path $PathB -PathType Leaf)) { return $false }
    $a = [System.IO.File]::ReadAllText($PathA) -replace "`r", ""
    $b = [System.IO.File]::ReadAllText($PathB) -replace "`r", ""
    return $a -eq $b
}
# A fetched launcher never replaces the real one until it looks like the
# right kind of script: this rules out a captive portal, a registry error
# page or an empty response silently bricking a bundle's launchers. Returns
# "" when the file is fine, or the reason it is refused.
function Test-Launcher([string]$Ext, [string]$Path) {
    if (-not (Test-Path $Path -PathType Leaf) -or (Get-Item $Path).Length -eq 0) { return "downloaded file is empty" }
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    $headLength = [Math]::Min(512, $bytes.Length)
    $head = [System.Text.Encoding]::ASCII.GetString($bytes, 0, $headLength).ToLowerInvariant()
    if ($head.Contains("<!doctype html") -or $head.Contains("<html")) { return "downloaded file is an HTML page, not a script" }
    $text = [System.Text.Encoding]::UTF8.GetString($bytes)
    $firstLine = ($text -split "`r?`n", 2)[0]
    switch ($Ext) {
        "sh"      { if ($firstLine -ne "#!/bin/sh") { return "does not start with #!/bin/sh" } }
        "command" { if ($firstLine -ne "#!/bin/sh") { return "does not start with #!/bin/sh" } }
        "cmd"     { if ($firstLine -ne "@echo off") { return "does not start with @echo off" } }
        "ps1"     {
            if (-not $firstLine.StartsWith("# Builds this configuration bundle")) { return "does not start with the expected header line" }
            if ($text -notmatch '(?m)^\s*param\(') { return "has no param( block" }
        }
    }
    return ""
}

# Fetches one launcher (SYNOS_ENGINE_SOURCE, an engine checkout, when set;
# otherwise a plain HTTP GET under BaseUrl) into Out. Used by both `update`
# and the launch-time check below, so they can never drift apart. Throws on
# any problem; nothing here is ever executed, eval'd or sourced.
function Get-Launcher([string]$Ext, [string]$Out, [string]$SrcDir, [string]$BaseUrl) {
    if ($SrcDir) {
        $from = Join-Path $SrcDir "tools\bundle_launcher.$Ext"
        if (-not (Test-Path $from -PathType Leaf)) { throw "$from is missing" }
        Copy-Item -Path $from -Destination $Out -Force
    } else {
        Invoke-WebRequest -Uri "$BaseUrl/bundle_launcher.$Ext" -OutFile $Out -UseBasicParsing -TimeoutSec 20
    }
}

# .\build.ps1 update: pulls the four current launchers (SYNOS_ENGINE_SOURCE, an
# engine checkout, when set; otherwise SYNOS_LAUNCHER_URL or the engine's own
# repository) and replaces the bundle's copies. All four are downloaded and
# validated before any of them is touched, so a bad source changes nothing.
function Invoke-Update {
    $launchers = [ordered]@{ "sh" = "build.sh"; "ps1" = "build.ps1"; "cmd" = "build.cmd"; "command" = "build.command" }
    $srcDir = $env:SYNOS_ENGINE_SOURCE
    $baseUrl = if ($env:SYNOS_LAUNCHER_URL) { $env:SYNOS_LAUNCHER_URL } else { "https://raw.githubusercontent.com/Synthot/SynOS-Studio/main/tools" }
    $workdir = Join-Path $PSScriptRoot ".build\update"
    if (Test-Path $workdir) { Remove-Item -Recurse -Force $workdir }
    New-Item -ItemType Directory -Force -Path $workdir | Out-Null
    foreach ($ext in $launchers.Keys) {
        $target = $launchers[$ext]
        $tmp = Join-Path $workdir $target
        try {
            Get-Launcher -Ext $ext -Out $tmp -SrcDir $srcDir -BaseUrl $baseUrl
        } catch {
            Remove-Item -Recurse -Force $workdir -ErrorAction SilentlyContinue
            Fail "${target}: $($_.Exception.Message) (nothing was changed)" 2
        }
        $problem = Test-Launcher -Ext $ext -Path $tmp
        if ($problem) {
            Remove-Item -Recurse -Force $workdir -ErrorAction SilentlyContinue
            Fail "${target}: $problem (nothing was changed)" 2
        }
    }
    $updated = 0
    $replaced = @()
    foreach ($ext in $launchers.Keys) {
        $target = $launchers[$ext]
        $tmp = Join-Path $workdir $target
        $dest = Join-Path $PSScriptRoot $target
        if (Test-SameIgnoringCr $tmp $dest) {
            Write-Host "${target}: already current"
            continue
        }
        try {
            Copy-Item -Path $tmp -Destination $dest -Force
        } catch {
            Remove-Item -Recurse -Force $workdir -ErrorAction SilentlyContinue
            if ($replaced.Count -gt 0) {
                Fail "could not replace ${target} (already replaced: $($replaced -join ' ')); run update again to finish" 2
            } else {
                Fail "could not replace $target" 2
            }
        }
        Write-Host "${target}: updated"
        $updated++
        $replaced += $target
    }
    Remove-Item -Recurse -Force $workdir -ErrorAction SilentlyContinue
    if ($updated -gt 0) { Write-Host "launchers updated ($updated of 4)." } else { Write-Host "launchers already up to date." }
}

# Called only when build.ps1 itself was just refreshed, below: pulls the other
# three launchers from the same source. Best effort and never blocks the
# build - a problem here is a one-line warning, not a failure.
function Update-Siblings {
    $siblings = [ordered]@{ "sh" = "build.sh"; "cmd" = "build.cmd"; "command" = "build.command" }
    $workdir = Join-Path $PSScriptRoot ".build\sibling-refresh"
    if (Test-Path $workdir) { Remove-Item -Recurse -Force $workdir }
    New-Item -ItemType Directory -Force -Path $workdir | Out-Null
    foreach ($ext in $siblings.Keys) {
        $target = $siblings[$ext]
        $tmp = Join-Path $workdir $target
        $fromSource = if ($env:SYNOS_ENGINE_SOURCE) { Join-Path $env:SYNOS_ENGINE_SOURCE "tools\bundle_launcher.$ext" } else { Join-Path $PSScriptRoot ".build\engine-src" }
        $candidate = if (Test-Path $fromSource -PathType Leaf) { Get-Item $fromSource } else { Get-ChildItem -Path $fromSource -Recurse -Filter "bundle_launcher.$ext" -ErrorAction SilentlyContinue | Select-Object -First 1 }
        if ($candidate) {
            Copy-Item -Path $candidate.FullName -Destination $tmp -Force
        } else {
            & $runtime @runtimeRootArgs run --rm $image cat "/opt/synos/tools/bundle_launcher.$ext" 2>$null | Set-Content -Path $tmp -Encoding utf8 -NoNewline
        }
        $problem = Test-Launcher -Ext $ext -Path $tmp
        if ($problem) {
            Write-Host "warning: $target was not refreshed ($problem); it was left unchanged."
            Remove-Item -Recurse -Force $workdir -ErrorAction SilentlyContinue
            return
        }
    }
    foreach ($ext in $siblings.Keys) {
        $target = $siblings[$ext]
        $tmp = Join-Path $workdir $target
        $dest = Join-Path $PSScriptRoot $target
        if (Test-SameIgnoringCr $tmp $dest) { continue }
        try { Copy-Item -Path $tmp -Destination $dest -Force }
        catch { Write-Host "warning: could not replace $target with the engine's current version." }
    }
    Remove-Item -Recurse -Force $workdir -ErrorAction SilentlyContinue
}

# Runs once, for "check" and "build" only (not "update", and never after a
# restart of this script): looks for newer launchers before anything that
# needs a runtime, disk space or a registry, and offers to fetch them. A
# network problem here is a one-line note, never a build failure.
function Test-ForLauncherUpdate {
    if ($env:SYNOS_NO_UPDATE_CHECK) { return }
    if ($env:SYNOS_LAUNCHER_REFRESHED) { return }
    $launchers = [ordered]@{ "sh" = "build.sh"; "ps1" = "build.ps1"; "cmd" = "build.cmd"; "command" = "build.command" }
    $srcDir = $env:SYNOS_ENGINE_SOURCE
    $baseUrl = if ($env:SYNOS_LAUNCHER_URL) { $env:SYNOS_LAUNCHER_URL } else { "https://raw.githubusercontent.com/Synthot/SynOS-Studio/main/tools" }
    $workdir = Join-Path $PSScriptRoot ".build\update-check"
    if (Test-Path $workdir) { Remove-Item -Recurse -Force $workdir }
    New-Item -ItemType Directory -Force -Path $workdir | Out-Null
    $changed = @()
    foreach ($ext in $launchers.Keys) {
        $target = $launchers[$ext]
        $tmp = Join-Path $workdir $target
        try {
            Get-Launcher -Ext $ext -Out $tmp -SrcDir $srcDir -BaseUrl $baseUrl
        } catch {
            Write-Host "could not check for a launcher update: ${target}: $($_.Exception.Message); continuing"
            Remove-Item -Recurse -Force $workdir -ErrorAction SilentlyContinue
            return
        }
        $problem = Test-Launcher -Ext $ext -Path $tmp
        if ($problem) {
            Write-Host "could not check for a launcher update: ${target}: $problem; continuing"
            Remove-Item -Recurse -Force $workdir -ErrorAction SilentlyContinue
            return
        }
        $dest = Join-Path $PSScriptRoot $target
        if (-not (Test-SameIgnoringCr $tmp $dest)) {
            $changed += $target
        }
    }
    if ($changed.Count -eq 0) {
        # The check reached its source and the launchers agree: mark this run
        # refreshed so the late, mid-build self-refresh (a different source:
        # the engine image, not SYNOS_LAUNCHER_URL) does not undo this with an
        # older copy and start a downgrade-then-offer-upgrade loop next time.
        Remove-Item -Recurse -Force $workdir -ErrorAction SilentlyContinue
        $env:SYNOS_LAUNCHER_REFRESHED = "1"
        return
    }
    Write-Host "a newer launcher is available: $($changed -join ' ')"
    $proceed = $false
    if ($Yes) {
        $proceed = $true
    } elseif ([Console]::IsInputRedirected) {
        Write-Host "run .\build.ps1 update to apply it."
    } elseif (AskYes "A newer launcher is available. Update now?") {
        $proceed = $true
    }
    if (-not $proceed) {
        Remove-Item -Recurse -Force $workdir -ErrorAction SilentlyContinue
        $env:SYNOS_LAUNCHER_REFRESHED = "1"
        return
    }
    $replaced = @()
    foreach ($ext in $launchers.Keys) {
        $target = $launchers[$ext]
        try {
            Copy-Item -Path (Join-Path $workdir $target) -Destination (Join-Path $PSScriptRoot $target) -Force
        } catch {
            Remove-Item -Recurse -Force $workdir -ErrorAction SilentlyContinue
            if ($replaced.Count -gt 0) {
                Fail "could not replace ${target} (already replaced: $($replaced -join ' ')); run .\build.ps1 update again to finish" 2
            } else {
                Fail "could not replace $target" 2
            }
        }
        $replaced += $target
    }
    Remove-Item -Recurse -Force $workdir -ErrorAction SilentlyContinue
    Write-Host "the launchers were updated; starting again."
    $env:SYNOS_LAUNCHER_REFRESHED = "1"
    $reExecArgs = @()
    if ($Command) { $reExecArgs += $Command }
    if ($Yes) { $reExecArgs += "-Yes" }
    if ($ContainerRoot) { $reExecArgs += @("-ContainerRoot", $ContainerRoot) }
    if ($ContainerRunroot) { $reExecArgs += @("-ContainerRunroot", $ContainerRunroot) }
    if ($Channel) { $reExecArgs += @("-Channel", $Channel) }
    & powershell -ExecutionPolicy Bypass -File $PSCommandPath @reExecArgs
    exit $LASTEXITCODE
}

if ($Command -eq "update") { Invoke-Update; exit 0 }

# ---------------------------------------------------------------- the bundle
if (-not (Test-Path "bundle.json")) { Fail "bundle.json is missing: run this script from the unzipped bundle folder" }
$descriptor = Get-Content -Raw -Path "bundle.json" | ConvertFrom-Json
$manifest = $descriptor.manifest
if (-not $manifest -or -not (Test-Path $manifest)) { Fail "bundle.json names no manifest, or $manifest is missing" }
$engine = if ($descriptor.engine -and $descriptor.engine.min) { $descriptor.engine.min } else { "" }
$base = Field $manifest "base"
$suite = Field $manifest "suite"
if (-not $base -or -not $suite) { Fail "$manifest does not name a base and a suite" }

# ------------------------------------------------------------- the channel
# Which engine pipeline this bundle builds against: recorded by the front end
# that generated it (bundle.json's "channel", "stable" assumed when absent,
# so every bundle generated before this existed keeps working exactly as it
# does today). -Channel/SYNOS_CHANNEL override it, and win over the bundle's
# own value either way.
$bundleChannel = if ($descriptor.channel -and $descriptor.channel -in @("stable", "development")) { $descriptor.channel }
                 elseif ($descriptor.channel) { Write-Host "note: bundle.json names an unknown channel '$($descriptor.channel)'; treating this bundle as stable."; "" }
                 else { "" }
if ($Channel) { $resolvedChannel = $Channel; $channelReason = "the -Channel flag" }
elseif ($env:SYNOS_CHANNEL) { $resolvedChannel = $env:SYNOS_CHANNEL; $channelReason = "the SYNOS_CHANNEL environment variable" }
elseif ($bundleChannel) { $resolvedChannel = $bundleChannel; $channelReason = "bundle" }
else { $resolvedChannel = "stable"; $channelReason = "default" }
switch ($channelReason) {
    "bundle" {
        if ($resolvedChannel -eq "development") { Write-Host "channel: development (this bundle was made by a development instance of the page)" }
        else { Write-Host "channel: stable (this bundle was made by the released page)" }
    }
    "default" { Write-Host "channel: stable (default; bundle.json names no channel)" }
    default { Write-Host "channel: $resolvedChannel (override: $channelReason)" }
}
if ($resolvedChannel -eq "development") {
    Write-Host "note: the development channel builds against the unreleased tip of the engine's main branch, not a released version."
}

Test-ForLauncherUpdate

# ---------------------------------------------------------------- the machine
function Find-Runtime {
    foreach ($name in "podman", "docker") {
        $found = Get-Command $name -ErrorAction SilentlyContinue
        if ($found) { return $found.Source }
    }
    return $null
}
$runtime = Find-Runtime
if (-not $runtime) {
    if ($Command -eq "check") { Fail "Docker Desktop or Podman Desktop is required; .\build.ps1 installs Docker Desktop for you" 2 }
    Write-Host "No container runtime found. The build runs inside a Linux container, so Docker Desktop (or Podman Desktop) is required."
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) { Fail "install Docker Desktop from https://www.docker.com/products/docker-desktop/ (WSL 2 backend), start it, then run this script again" 2 }
    Write-Host "This will run: winget install -e --id Docker.DockerDesktop  (WSL 2 is enabled by the installer; a restart may be needed)"
    if (-not (Ask "Install Docker Desktop now?")) { Fail "install Docker Desktop, start it, then run this script again" 2 }
    & winget install -e --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { Fail "the installation failed; install Docker Desktop by hand, then run this script again" 2 }
    Write-Host "Docker Desktop is installed. Start it from the Start menu, wait until it reports 'Engine running', then run this script again."
    exit 2
}
function Test-IsPodman { $runtime -match 'podman' }
function Test-IsDocker { $runtime -match 'docker' }

# Docker Desktop's storage is one setting for the whole engine; there is no
# per-build override. Printed when this script is asked for a location while
# running under docker, and again in the low-space refusal below, since
# moving it is the only fix there.
function Get-DockerStorageHelp {
@"
docker's storage is one setting for the whole engine; there is no per-build location.
  Docker Desktop: Settings > Resources > Advanced > Disk image location (move it), or enlarge the WSL virtual disk it already uses.
  or: install Podman Desktop, which takes a location per build (-Storage)
  or: build on a machine that already has room where Docker Desktop keeps its images
"@
}

if (($ContainerRoot -or $ContainerRunroot) -and (Test-IsDocker)) {
    Write-Host "error: docker cannot put this build's storage anywhere but its own daemon-wide location." -ForegroundColor Red
    Write-Host (Get-DockerStorageHelp)
    exit 2
}

$drive = (Get-Item -Path $PSScriptRoot).PSDrive
$freeGb = [math]::Floor($drive.Free / 1GB)
if ($freeGb -lt 40) { Fail "at least 40 GB free is needed on drive $($drive.Name): $freeGb GB available" 2 }

$minStoreGb = 30
$rememberedRootFile = Join-Path $PSScriptRoot ".build\container-root"
function Remember-ContainerRoot([string]$Value) {
    New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot ".build") | Out-Null
    Set-Content -Path $rememberedRootFile -Value $Value -Encoding utf8
}
# FAT32/exFAT (common on a removable drive) cannot back a container's
# overlay; anything Get-Volume cannot classify is assumed usable rather than
# guessed at (mirrors build.sh's graceful fallback when `stat` disagrees).
function Test-BundleDiskUnusable {
    try {
        $fsType = (Get-Volume -FilePath $PSScriptRoot -ErrorAction Stop).FileSystemType
        return $fsType -in @("FAT32", "FAT", "exFAT")
    } catch {
        return $false
    }
}

# A location already chosen for this bundle (by hand, or by answering the
# question below on an earlier run) is remembered, never asked twice.
$storageAsked = $false
if ((Test-IsPodman) -and (-not $ContainerRoot) -and (Test-Path $rememberedRootFile -PathType Leaf)) {
    $remembered = (Get-Content -Path $rememberedRootFile -Raw -ErrorAction SilentlyContinue)
    if ($remembered) { $remembered = $remembered.Trim() }
    if ($remembered -eq "default") {
        $storageAsked = $true
        Write-Host "using Podman's own storage for this build, not this bundle's disk (change: -Storage <path>; forget: del .build\container-root)"
    } elseif ($remembered) {
        $ContainerRoot = $remembered
        $storageAsked = $true
        Write-Host "using the remembered build storage: $ContainerRoot (change: -Storage <path>; forget: del .build\container-root)"
    }
}

# The bundle's own disk is the default, needing nothing from you: this
# script already knows where to build from where the bundle was unpacked,
# and that is where its storage goes too - no variable to learn, no
# question to answer. A real choice remains only when that disk cannot back
# a container's overlay at all; only then, and only when it can actually be
# answered in two seconds (a terminal, no -Yes, not just a check), is
# Podman's own storage offered instead.
$containerRootAuto = $false
if ((Test-IsPodman) -and (-not $ContainerRoot) -and (-not $storageAsked)) {
    $bundleStore = Join-Path $PSScriptRoot ".build\container-storage"
    if ((Test-BundleDiskUnusable) -and ($Command -ne "check") -and (-not $Yes) -and (-not [Console]::IsInputRedirected)) {
        $defaultStore = (& $runtime info --format '{{.Store.GraphRoot}}' 2>$null)
        if ($defaultStore -and ($defaultStore -notmatch '\{\{') -and ($defaultStore -notmatch '^/') -and (Test-Path $defaultStore)) {
            $defaultStoreDrive = (Get-Item -Path $defaultStore).PSDrive
            $defaultStoreGb = [math]::Floor($defaultStoreDrive.Free / 1GB)
            Write-Host "this bundle's own disk cannot back a container's overlay filesystem; Podman's own storage at $defaultStore ($defaultStoreGb GB free) can."
            if (AskYes "Use Podman's own storage there instead?") {
                $ContainerRoot = ""
            } else {
                $ContainerRoot = $bundleStore
            }
            $storageAsked = $true
            $valueToRemember = if ($ContainerRoot) { $ContainerRoot } else { "default" }
            Remember-ContainerRoot $valueToRemember
        }
    }
    if (-not $storageAsked) {
        $ContainerRoot = $bundleStore
        $containerRootAuto = $true
        if (-not (Test-Path $ContainerRoot)) {
            Write-Host "this build keeps its storage under $ContainerRoot; use -Storage <path> (or SYNOS_CONTAINER_ROOT) to put it somewhere else."
        }
    }
}

$runtimeRootArgs = @()
if ((Test-IsPodman) -and ($ContainerRoot -or $ContainerRunroot)) {
    if ($ContainerRoot) {
        Test-StorageInside (Join-Path $PSScriptRoot "dist") "the build's output directory (dist\)"
        if ($env:SYNOS_ENGINE_SOURCE) { Test-StorageInside $env:SYNOS_ENGINE_SOURCE "the engine source this image would be built from" }
        New-Item -ItemType Directory -Force -Path $ContainerRoot | Out-Null
        $runtimeRootArgs += @("--root", $ContainerRoot)
        if (-not $containerRootAuto) { Remember-ContainerRoot $ContainerRoot }
    }
    if ($ContainerRunroot) {
        New-Item -ItemType Directory -Force -Path $ContainerRunroot | Out-Null
        $runtimeRootArgs += @("--runroot", $ContainerRunroot)
    }
}
& $runtime @runtimeRootArgs info *> $null
if ($LASTEXITCODE -ne 0) { Fail "$runtime is installed but not running: start Docker Desktop (or Podman Desktop) and wait until the engine is running, then run this script again" 2 }

# The chroot, the image staging and the cache live in the container runtime's
# own storage, not next to the bundle. When it reports a path inside its own
# Linux VM (starts with "/"), Windows cannot stat it directly, so the check
# is skipped rather than guessed at; a real Windows path (ContainerRoot set
# to a drive letter) is checked the same way build.sh checks one.
if ($ContainerRoot -and (Test-IsPodman)) {
    $store = $ContainerRoot
} else {
    $store = (& $runtime info --format '{{.DockerRootDir}}' 2>$null)
    if (-not $store -or $store -match '\{\{' -or $store -eq '<no value>') {
        $store = (& $runtime @runtimeRootArgs info --format '{{.Store.GraphRoot}}' 2>$null)
    }
}
if ($store -and ($store -notmatch '^/') -and (Test-Path $store)) {
    $storeDrive = (Get-Item -Path $store).PSDrive
    $storeFreeGb = [math]::Floor($storeDrive.Free / 1GB)
    if ($storeFreeGb -lt $minStoreGb) {
        if (Test-IsPodman) {
            Fail @"
this build needs 30 GB free; only $storeFreeGb GB is free at $store, where podman keeps the build.
  no root needed: -Storage X:\path\with\room  (or SYNOS_CONTAINER_ROOT)
  or: free space at $store
  or: move podman's own default storage (root): graphroot in storage.conf, or podman machine set --disk-size for a WSL machine
"@ 2
        } else {
            Fail @"
this build needs 30 GB free; only $storeFreeGb GB is free at $store, where docker keeps the build.
  free space at $store, or install Podman Desktop (moves its storage with -Storage, no daemon setting), or:
$(Get-DockerStorageHelp)
"@ 2
        }
    }
}

# ---------------------------------------------------------------- the image
# The published image is preferred. On the stable channel only a release that
# satisfies this bundle is ever pulled or built - never the moving tag, never
# the development branch, in any form. The development channel is unchanged
# from before: the moving tag, then a local build from the tip of the main
# branch - both named so an image built this way is never mistaken for a
# released one.
$GithubRepoUrl = "https://github.com/Synthot/SynOS-Studio"
$GithubTagsApi = "https://api.github.com/repos/Synthot/SynOS-Studio/tags?per_page=100"

function Test-VersionGe([string]$A, [string]$B) {
    try { return ([version]$A) -ge ([version]$B) } catch { return $false }
}

# Resolves the release this bundle should build against on the stable
# channel: the newest tag (v<version> on the public repository) at or above
# engine.min, read from GitHub's tags API - no token needed, never guessed
# from a moving branch. Sets $script:resolvedVersion/resolvedTag/resolvedNote,
# or refuses with Fail when nothing satisfies the bundle. When the API cannot
# be reached at all (offline, or its anonymous rate limit) this falls back to
# the one release the bundle names directly - archive/refs/tags/v<engine>.zip,
# no further API calls - rather than an opaque network error.
function Resolve-StableRelease {
    $tags = $null
    try { $tags = Invoke-RestMethod -Uri $GithubTagsApi -TimeoutSec 20 -Headers @{ Accept = "application/vnd.github+json" } }
    catch { $tags = $null }
    if ($tags) {
        $versions = $tags | ForEach-Object { $_.name } | Where-Object { $_ -match '^v\d+(\.\d+)*$' } |
            ForEach-Object { $_.Substring(1) } | Select-Object -Unique
        $overallBest = $null
        foreach ($v in $versions) { if (-not $overallBest -or (Test-VersionGe $v $overallBest)) { $overallBest = $v } }
        if (-not $overallBest) {
            Fail "GitHub's release list for $GithubRepoUrl came back but named no v<version> tag; set SYNOS_ENGINE_URL, SYNOS_ENGINE_SOURCE or SYNOS_BUILDER_IMAGE, or build with -Channel development" 2
        }
        $best = $null
        foreach ($v in $versions) {
            if ($engine -and -not (Test-VersionGe $v $engine)) { continue }
            if (-not $best -or (Test-VersionGe $v $best)) { $best = $v }
        }
        if (-not $engine) { $best = $overallBest }
        if (-not $best) {
            Fail "this bundle needs engine $engine; the newest release is $overallBest. Wait for a release that satisfies it, build your own engine (SYNOS_ENGINE_SOURCE=<checkout> .\build.ps1), or build with -Channel development for the unreleased engine." 2
        }
        $script:resolvedVersion = $best
        $script:resolvedTag = "v$best"
        $script:resolvedNote = "the newest published release satisfying this bundle, from GitHub's release list"
        return
    }
    if ($engine) {
        $script:resolvedVersion = $engine
        $script:resolvedTag = "v$engine"
        $script:resolvedNote = "GitHub's release list could not be checked (offline, or its anonymous rate limit); using the release this bundle names directly"
        return
    }
    Fail "this bundle names no minimum engine version and GitHub's release list could not be checked (offline, or its anonymous rate limit); set SYNOS_ENGINE_URL or SYNOS_ENGINE_SOURCE, try again shortly, or build with -Channel development" 2
}

function Build-EngineImage([switch]$Forced) {
    $src = $env:SYNOS_ENGINE_SOURCE
    $sourceId = "local"
    if (-not $src) {
        if ($env:SYNOS_ENGINE_URL) {
            $url = $env:SYNOS_ENGINE_URL
        } elseif ($resolvedChannel -eq "development") {
            $url = "https://github.com/Synthot/SynOS-Studio/archive/refs/heads/main.zip"
        } else {
            if (-not $script:resolvedTag) { Resolve-StableRelease }
            $url = "$GithubRepoUrl/archive/refs/tags/$($script:resolvedTag).zip"
            Write-Host "channel stable: building from release $($script:resolvedTag) ($($script:resolvedNote))"
        }
        Write-Host "downloading the engine source from $url"
        New-Item -ItemType Directory -Force -Path ".build" | Out-Null
        Invoke-WebRequest -Uri $url -OutFile ".build\engine-src.zip"
        if (Test-Path ".build\engine-src") { Remove-Item -Recurse -Force ".build\engine-src" }
        Expand-Archive -Path ".build\engine-src.zip" -DestinationPath ".build\engine-src"
        $src = (Get-ChildItem -Path ".build\engine-src" -Directory | Select-Object -First 1).FullName
        # The image carries a copy of the engine, so it is named after the source it was
        # built from; a changed engine gives a new name and a rebuild (layer cache keeps it short).
        $sourceId = (Get-FileHash -Algorithm SHA256 ".build\engine-src.zip").Hash.Substring(0, 12).ToLower()
    }
    $src = Resolve-AbsolutePath $src
    Test-StorageInside $src "the engine source this image is built from"
    if (-not (Test-Path (Join-Path $src "bases\$base\Containerfile"))) { Fail "$src has no bases\$base\Containerfile: not an engine checkout" 2 }
    $script:buildSrc = $src
    $localTag = if ($resolvedChannel -eq "development") { "synos-builder:$base-$suite-dev-$sourceId" } else { "synos-builder:$base-$suite-$sourceId" }
    if (-not $Forced) {
        & $runtime @runtimeRootArgs image inspect $localTag *> $null
        if ($LASTEXITCODE -eq 0) { Write-Host "using the engine image built earlier on this machine from this engine source: $localTag"; return $localTag }
    }
    Write-Host "building the engine image $localTag from $src (about 20 minutes, once)."
    Write-Host "Each build step and package is shown as it happens; the complete output is kept in dist\image-build.log"
    New-Item -ItemType Directory -Force -Path "dist" | Out-Null
    $log = Join-Path $PSScriptRoot "dist\image-build.log"
    $started = Get-Date
    Push-Location $src
    & $runtime @runtimeRootArgs build --build-arg "SUITE=$suite" -t $localTag -f "bases/$base/Containerfile" . 2>&1 |
        Tee-Object -FilePath $log |
        ForEach-Object { if ("$_" -match '^(STEP \d+/\d+|Step \d+/\d+|#\d+ \[\d+/\d+\]|Get:\d+ |Setting up |Successfully )') { Write-Host "  $_" } }
    $code = $LASTEXITCODE
    Pop-Location
    Write-Host ("engine image built in {0} min" -f [int]((Get-Date) - $started).TotalMinutes)
    if ($code -ne 0) { Fail "building the engine image failed; see dist\image-build.log" 2 }
    return $localTag
}
$repository = if ($env:SYNOS_IMAGE_REPOSITORY) { $env:SYNOS_IMAGE_REPOSITORY } else { "ghcr.io/synthot/synos-builder" }
$image = $env:SYNOS_BUILDER_IMAGE
if (-not $image) {
    if ($resolvedChannel -eq "development") {
        $pinned = if ($engine) { "${repository}:$base-$suite-v$engine" } else { "${repository}:$base-$suite" }
        $moving = "${repository}:$base-$suite"
        Write-Host "pulling the build engine $pinned (one-time download, about 1.5 GB)"
        & $runtime @runtimeRootArgs pull $pinned *> $null
        if ($LASTEXITCODE -eq 0) { $image = $pinned }
        else {
            & $runtime @runtimeRootArgs pull $moving *> $null
            if ($LASTEXITCODE -eq 0) { Write-Host "no image pinned to engine $engine; using the current $moving"; $image = $moving }
            elseif ($Command -eq "check") { $image = "none published: it will be built here at the first build, about 20 minutes" }
            else {
                Write-Host "no published engine image can be pulled from $repository (not published yet, or the registry refused);"
                Write-Host "the image is built here instead, from the engine source."
                $image = Build-EngineImage
            }
        }
    } else {
        $pulled = $false
        $pinned = $null
        if ($engine) {
            $pinned = "${repository}:$base-$suite-v$engine"
            Write-Host "pulling the build engine $pinned (one-time download, about 1.5 GB)"
            & $runtime @runtimeRootArgs pull $pinned *> $null
            if ($LASTEXITCODE -eq 0) { $image = $pinned; $pulled = $true }
        }
        if (-not $pulled) {
            Resolve-StableRelease
            Write-Host "channel stable: using release $($script:resolvedTag) ($($script:resolvedNote))"
            $pinned2 = "${repository}:$base-$suite-v$($script:resolvedVersion)"
            if ($pinned2 -ne $pinned) {
                Write-Host "pulling the build engine $pinned2 (one-time download, about 1.5 GB)"
                & $runtime @runtimeRootArgs pull $pinned2 *> $null
                if ($LASTEXITCODE -eq 0) { $image = $pinned2; $pulled = $true }
            }
        }
        if (-not $pulled) {
            if ($Command -eq "check") { $image = "none published: it will be built here at the first build, about 20 minutes" }
            else {
                Write-Host "no published engine image can be pulled from $repository for this release (not published yet, or the registry refused);"
                Write-Host "the image is built here instead, from the matching release source - never from the development branch."
                $image = Build-EngineImage
            }
        }
    }
}

# The failure this guards against: a cached image (built earlier, from an
# older source, or pulled once and kept) carrying an engine below what this
# bundle needs. Read plainly instead of assumed, and rebuilt from the right
# source rather than left to fail deep in the build with two bare numbers.
function Test-EngineVersion {
    if (-not $engine) { return }
    if ($image -like "none published:*") { return }
    $actual = ""
    if ($script:buildSrc -and (Test-Path (Join-Path $script:buildSrc "VERSION"))) {
        $actual = (Get-Content -Raw (Join-Path $script:buildSrc "VERSION")).Trim()
    } else {
        $actual = ((& $runtime @runtimeRootArgs run --rm $image cat /opt/synos/VERSION 2>$null) -join "").Trim()
    }
    if (-not $actual -or $actual -notmatch '^\d+(\.\d+)*$') { return }
    if (Test-VersionGe $actual $engine) { return }
    if ($env:SYNOS_BUILDER_IMAGE) {
        Fail "SYNOS_BUILDER_IMAGE=$image carries engine $actual, older than this bundle needs ($engine); use a newer image, or unset SYNOS_BUILDER_IMAGE to let this script choose one" 2
    }
    Write-Host "the image $image carries engine $actual, older than this bundle needs ($engine); rebuilding it from the matching source instead of using a stale image."
    $script:image = Build-EngineImage -Forced
}

if ($Command -eq "check") {
    $rootArgsText = if ($runtimeRootArgs.Count -gt 0) { " " + ($runtimeRootArgs -join " ") } else { "" }
    $storeText = if ($null -ne $storeFreeGb) { ", $storeFreeGb GB free in $store" } else { "" }
    Write-Host "ready: $runtime$rootArgsText, $freeGb GB free here$storeText, engine image $image"
    exit 0
}

Test-EngineVersion

# ---------------------------------------------------------------- this script
# The engine that builds the image also carries the current launcher. A bundle
# downloaded before a launcher fix is refreshed here, once, then restarted.
if (-not $env:SYNOS_LAUNCHER_REFRESHED) {
    $latest = ""
    $fromSource = if ($env:SYNOS_ENGINE_SOURCE) { Join-Path $env:SYNOS_ENGINE_SOURCE "tools\bundle_launcher.ps1" } else { Join-Path $PSScriptRoot ".build\engine-src" }
    $candidate = Get-ChildItem -Path $fromSource -Recurse -Filter "bundle_launcher.ps1" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($candidate) { $latest = Get-Content -Raw $candidate.FullName }
    else { $latest = (& $runtime @runtimeRootArgs run --rm $image cat /opt/synos/tools/bundle_launcher.ps1 2>$null) -join "`n" }
    $self = Get-Content -Raw $PSCommandPath
    if ($latest -and $latest.StartsWith("#") -and ($latest.Trim() -ne $self.Trim())) {
        Set-Content -Path $PSCommandPath -Value $latest -Encoding utf8
        Write-Host "build.ps1 was updated to the engine's current launcher; starting again."
        Update-Siblings
        $env:SYNOS_LAUNCHER_REFRESHED = "1"
        $reExecArgs = @()
        if ($Command) { $reExecArgs += $Command }
        if ($Yes) { $reExecArgs += "-Yes" }
        if ($ContainerRoot) { $reExecArgs += @("-ContainerRoot", $ContainerRoot) }
        if ($ContainerRunroot) { $reExecArgs += @("-ContainerRunroot", $ContainerRunroot) }
        if ($Channel) { $reExecArgs += @("-Channel", $Channel) }
        & powershell -ExecutionPolicy Bypass -File $PSCommandPath @reExecArgs
        exit $LASTEXITCODE
    }
}

# ---------------------------------------------------------------- the build
New-Item -ItemType Directory -Force -Path "dist" | Out-Null
# One build of a bundle at a time: two builds would share dist\ and the cache
# volume, and apt in the second stops on the first one's lock.
if (Test-Path "dist\build.pid") {
    $other = (Get-Content "dist\build.pid" -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($other -and (Get-Process -Id $other -ErrorAction SilentlyContinue)) {
        Fail "another build of this bundle is already running (process $other, output in dist\build.log); wait for it to finish, or stop it, then run this script again" 2
    }
}
Set-Content -Path "dist\build.pid" -Value $PID
if (Test-Path "dist\build.log") { Move-Item -Force "dist\build.log" "dist\build.previous.log" }
# Seeded here, before the container appends to it, so a development build is
# obvious in the log itself and not only in this script's own terminal output.
if ($resolvedChannel -eq "development") {
    Set-Content -Path "dist\build.log" -Value "channel: development (unreleased engine, tip of the main branch - not a release build)"
    Write-Host "building with the development engine (unreleased, tip of the main branch) - this image and ISO are not a release build."
} else {
    Set-Content -Path "dist\build.log" -Value "channel: stable"
}
Write-Host "building $manifest with $image"
Write-Host "first build about 40 minutes; the cache volume synos-cache-$base-$suite makes the next ones shorter."
Write-Host "the full output is kept in dist\build.log"
& $runtime @runtimeRootArgs run --rm --privileged `
    -v "${PSScriptRoot}:/bundle" `
    -v "synos-cache-$base-${suite}:/opt/synos/.build" `
    -v /opt/synos/new_building_os -v /opt/synos/image `
    -e SYNOS_KEYS_DIR=.build/keys `
    -e SYNOS_SIGNING_KEY -e SYNOS_SIGNING_KEY_FILE `
    -e SYNOS_CHANNEL="$resolvedChannel" `
    $image synos build /bundle --output /bundle/dist --log /bundle/dist/build.log
$status = $LASTEXITCODE
Remove-Item -Force "dist\build.pid" -ErrorAction SilentlyContinue
if ($status -ne 0) {
    Write-Host ""
    Write-Host "the build did not finish (exit code $status). The first errors in dist\build.log:"
    Select-String -Path "dist\build.log" -Pattern 'No space left on device|dpkg: error|dpkg-query: error|^E: |cannot allocate memory|FAILED|package error|skipped .*prebuild|Traceback|error:' -ErrorAction SilentlyContinue | Where-Object { $_.Line -notmatch 'locale' } | Select-Object -First 6 | ForEach-Object { Write-Host "  $($_.LineNumber): $($_.Line)" }
    Write-Host "The complete output is in dist\build.log. Running build.cmd again resumes from the cache."
    Write-Host "The build runs inside Docker Desktop's Linux disk image: 'No space left on device' means that image is full; enlarge it in Settings > Resources (80 GB or more)."
    exit $status
}
$iso = Get-ChildItem -Path "dist" -Filter "*.iso" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
Write-Host ""
Write-Host "done. Your image: dist\$($iso.Name)"
Write-Host "next to it: .sha256 (checksum), .packages.lock, .sbom.cdx.json (what is inside), .resolved.json, build.log"
Write-Host "verify it: cd dist; Get-FileHash -Algorithm SHA256 $($iso.Name) (compare with $($iso.BaseName).sha256)"
Write-Host ""
Write-Host "To install it:"
Write-Host "  USB stick:       write the ISO with Rufus (https://rufus.ie) or balenaEtcher; keep the default GPT/UEFI settings."
Write-Host "  Virtual machine: Hyper-V (Generation 2, Secure Boot template 'Microsoft UEFI Certificate Authority') or VirtualBox, 4 GB RAM, 40 GB disk."
Write-Host "  Boot it, try the live desktop, then run the installer from the menu."
