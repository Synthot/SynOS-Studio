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
# Environment: SYNOS_BUILDER_IMAGE (use another image), SYNOS_IMAGE_REPOSITORY
#   (another registry, default ghcr.io/synthot/synos-builder), SYNOS_ENGINE_SOURCE
#   (an engine checkout to build the image from), SYNOS_ENGINE_URL (source archive),
#   SYNOS_LAUNCHER_URL (source for `update`, default the engine's tools/ on GitHub),
#   SYNOS_NO_UPDATE_CHECK=1 (skip the launcher update check at the start of
#   check/build), SYNOS_YES=1.
[CmdletBinding()]
param([string]$Command = "", [switch]$Yes)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
if ($env:SYNOS_YES) { $Yes = $true }

function Fail([string]$Message, [int]$Code = 1) { Write-Host "error: $Message" -ForegroundColor Red; exit $Code }
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
            & $runtime run --rm $image cat "/opt/synos/tools/bundle_launcher.$ext" 2>$null | Set-Content -Path $tmp -Encoding utf8 -NoNewline
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
& $runtime info *> $null
if ($LASTEXITCODE -ne 0) { Fail "$runtime is installed but not running: start Docker Desktop (or Podman Desktop) and wait until the engine is running, then run this script again" 2 }
$drive = (Get-Item -Path $PSScriptRoot).PSDrive
$freeGb = [math]::Floor($drive.Free / 1GB)
if ($freeGb -lt 40) { Fail "at least 40 GB free is needed on drive $($drive.Name): $freeGb GB available" 2 }

# ---------------------------------------------------------------- the image
# The published image is preferred. When none can be pulled (not published yet,
# a registry that refuses anonymous pulls, no network to it), the same image is
# built here from the engine source, once, and kept as synos-builder:<base>-<suite>-local.
function Build-EngineImage {
    $src = $env:SYNOS_ENGINE_SOURCE
    $sourceId = "local"
    if (-not $src) {
        $url = if ($env:SYNOS_ENGINE_URL) { $env:SYNOS_ENGINE_URL } else { "https://github.com/Synthot/SynOS-Studio/archive/refs/heads/main.zip" }
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
    if (-not (Test-Path (Join-Path $src "bases\$base\Containerfile"))) { Fail "$src has no bases\$base\Containerfile: not an engine checkout" 2 }
    $localTag = "synos-builder:$base-$suite-$sourceId"
    & $runtime image inspect $localTag *> $null
    if ($LASTEXITCODE -eq 0) { Write-Host "using the engine image built earlier on this machine from this engine source: $localTag"; return $localTag }
    Write-Host "building the engine image $localTag from $src (about 20 minutes, once)."
    Write-Host "Each build step and package is shown as it happens; the complete output is kept in dist\image-build.log"
    New-Item -ItemType Directory -Force -Path "dist" | Out-Null
    $log = Join-Path $PSScriptRoot "dist\image-build.log"
    $started = Get-Date
    Push-Location $src
    & $runtime build --build-arg "SUITE=$suite" -t $localTag -f "bases/$base/Containerfile" . 2>&1 |
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
    $pinned = if ($engine) { "${repository}:$base-$suite-v$engine" } else { "${repository}:$base-$suite" }
    $moving = "${repository}:$base-$suite"
    Write-Host "pulling the build engine $pinned (one-time download, about 1.5 GB)"
    & $runtime pull $pinned *> $null
    if ($LASTEXITCODE -eq 0) { $image = $pinned }
    else {
        & $runtime pull $moving *> $null
        if ($LASTEXITCODE -eq 0) { Write-Host "no image pinned to engine $engine; using the current $moving"; $image = $moving }
        elseif ($Command -eq "check") { $image = "none published: it will be built here at the first build, about 20 minutes" }
        else {
            Write-Host "no published engine image can be pulled from $repository (not published yet, or the registry refused);"
            Write-Host "the image is built here instead, from the engine source."
            $image = Build-EngineImage
        }
    }
}

if ($Command -eq "check") {
    Write-Host "ready: $runtime, $freeGb GB free, engine image $image"
    exit 0
}

# ---------------------------------------------------------------- this script
# The engine that builds the image also carries the current launcher. A bundle
# downloaded before a launcher fix is refreshed here, once, then restarted.
if (-not $env:SYNOS_LAUNCHER_REFRESHED) {
    $latest = ""
    $fromSource = if ($env:SYNOS_ENGINE_SOURCE) { Join-Path $env:SYNOS_ENGINE_SOURCE "tools\bundle_launcher.ps1" } else { Join-Path $PSScriptRoot ".build\engine-src" }
    $candidate = Get-ChildItem -Path $fromSource -Recurse -Filter "bundle_launcher.ps1" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($candidate) { $latest = Get-Content -Raw $candidate.FullName }
    else { $latest = (& $runtime run --rm $image cat /opt/synos/tools/bundle_launcher.ps1 2>$null) -join "`n" }
    $self = Get-Content -Raw $PSCommandPath
    if ($latest -and $latest.StartsWith("#") -and ($latest.Trim() -ne $self.Trim())) {
        Set-Content -Path $PSCommandPath -Value $latest -Encoding utf8
        Write-Host "build.ps1 was updated to the engine's current launcher; starting again."
        Update-Siblings
        $env:SYNOS_LAUNCHER_REFRESHED = "1"
        & powershell -ExecutionPolicy Bypass -File $PSCommandPath @args
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
Write-Host "building $manifest with $image"
Write-Host "first build about 40 minutes; the cache volume synos-cache-$base-$suite makes the next ones shorter."
Write-Host "the full output is kept in dist\build.log"
& $runtime run --rm --privileged `
    -v "${PSScriptRoot}:/bundle" `
    -v "synos-cache-$base-${suite}:/opt/synos/.build" `
    -v /opt/synos/new_building_os -v /opt/synos/image `
    -e SYNOS_KEYS_DIR=.build/keys `
    -e SYNOS_SIGNING_KEY -e SYNOS_SIGNING_KEY_FILE `
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
