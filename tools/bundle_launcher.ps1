# Builds this configuration bundle into an installable image (Windows).
#
#   .\build.ps1            build; the ISO and its evidence land in .\dist
#   .\build.ps1 check      only check that this machine can build (installs nothing)
#   .\build.ps1 -Yes       answer yes to the questions (install Docker Desktop)
#
# Needs Docker Desktop or Podman Desktop (WSL 2 backend) and 40 GB free.
# When neither is present it offers to install Docker Desktop with winget.
# Nothing else is installed: the SynOS build engine runs inside a container
# image published for the exact engine version this bundle was made for.
# Environment: SYNOS_BUILDER_IMAGE (use another image), SYNOS_IMAGE_REPOSITORY,
#   SYNOS_ENGINE_SOURCE (an engine checkout to build the image from), SYNOS_ENGINE_URL (source archive)
# (another registry, default ghcr.io/synthot/synos-builder), SYNOS_YES=1.
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
function Field([string]$File, [string]$Key) {
    $line = Get-Content -Path $File | Where-Object { $_ -match "^$Key\s*:" } | Select-Object -First 1
    if (-not $line) { return "" }
    return ($line -replace "^$Key\s*:\s*", "" -replace '\s*#.*$', "" -replace '^"|"$', "").Trim()
}

# ---------------------------------------------------------------- the bundle
if (-not (Test-Path "bundle.json")) { Fail "bundle.json is missing: run this script from the unzipped bundle folder" }
$descriptor = Get-Content -Raw -Path "bundle.json" | ConvertFrom-Json
$manifest = $descriptor.manifest
if (-not $manifest -or -not (Test-Path $manifest)) { Fail "bundle.json names no manifest, or $manifest is missing" }
$engine = if ($descriptor.engine -and $descriptor.engine.min) { $descriptor.engine.min } else { "" }
$base = Field $manifest "base"
$suite = Field $manifest "suite"
if (-not $base -or -not $suite) { Fail "$manifest does not name a base and a suite" }

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
        $env:SYNOS_LAUNCHER_REFRESHED = "1"
        & powershell -ExecutionPolicy Bypass -File $PSCommandPath @args
        exit $LASTEXITCODE
    }
}

# ---------------------------------------------------------------- the build
New-Item -ItemType Directory -Force -Path "dist" | Out-Null
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
if ($status -ne 0) {
    Write-Host ""
    Write-Host "the build did not finish (exit code $status). The complete output is in dist\build.log;"
    Write-Host "search it for the first 'FAILED' or 'error:' line. Running build.cmd again resumes from the cache."
    exit $status
}
$iso = Get-ChildItem -Path "dist" -Filter "*.iso" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
Write-Host ""
Write-Host "done. Your image: dist\$($iso.Name)"
Write-Host "next to it: .sha256 (checksum), .packages.lock, .sbom.cdx.json (what is inside), .resolved.json, build.log"
Write-Host ""
Write-Host "To install it:"
Write-Host "  USB stick:       write the ISO with Rufus (https://rufus.ie) or balenaEtcher; keep the default GPT/UEFI settings."
Write-Host "  Virtual machine: Hyper-V (Generation 2, Secure Boot template 'Microsoft UEFI Certificate Authority') or VirtualBox, 4 GB RAM, 40 GB disk."
Write-Host "  Boot it, try the live desktop, then run the installer from the menu."
