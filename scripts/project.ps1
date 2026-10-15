# ===========================================================================
#  InSilicoTrial MAS - start, stop and inspect this project's containers (Windows).
#
#  Same verbs as scripts/project.sh, for PowerShell on Windows and for anyone who
#  prefers it inside WSL. Only this compose project is touched, and nothing starts
#  on its own after a reboot.
#
#      .\scripts\project.ps1 start            # Studio + a demo run (http://localhost:8765)
#      .\scripts\project.ps1 start studio     # only the Studio
#      .\scripts\project.ps1 start spark      # Spark profile: JRE image, one simulation
#      .\scripts\project.ps1 start dev        # interactive shell in the dev image
#      .\scripts\project.ps1 status           # what runs here, and what else runs on this PC
#      .\scripts\project.ps1 logs studio      # follow a service log
#      .\scripts\project.ps1 stop             # stop the containers, keep the results volume
#      .\scripts\project.ps1 down             # stop and remove containers/network
#      .\scripts\project.ps1 purge            # also delete the results volume (asks first)
#      .\scripts\project.ps1 url              # where the Studio answers
#      .\scripts\project.ps1 doctor           # ports, docker, compose, other projects
#
#  If PowerShell refuses to run the file, start it with:
#      powershell -ExecutionPolicy Bypass -File .\scripts\project.ps1 start
# ===========================================================================

[CmdletBinding()]
param(
    [Parameter(Position = 0)][string] $Verb = "help",
    [Parameter(Position = 1)][string] $Target = "",
    [int] $Port = 0
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $RootDir

$ProjectName = "insilico-trial-mas"          # keep in sync with `name:` in docker-compose.yml
if ($Port -eq 0) { $Port = if ($env:INSILICO_STUDIO_PORT) { [int]$env:INSILICO_STUDIO_PORT } else { 8765 } }
$env:INSILICO_STUDIO_PORT = "$Port"
$env:DOCKER_CLI_HINTS = "false"

function Write-Step { param([string] $Text) Write-Host "`n==> $Text" -ForegroundColor Cyan }
function Write-Ok   { param([string] $Text) Write-Host "    ok   $Text" -ForegroundColor Green }
function Write-Warn { param([string] $Text) Write-Host "    warn $Text" -ForegroundColor Yellow }
function Fail       { param([string] $Text) Write-Host "`nERROR: $Text" -ForegroundColor Red; exit 1 }

function Show-Usage {
    Get-Content $MyInvocation.MyCommand.Path | Select-Object -Skip 1 -First 22 |
        ForEach-Object { $_ -replace '^# ?', '' }
    Write-Host ""
    Write-Host "Services"
    Write-Host "  studio   the readout UI (published on INSILICO_STUDIO_PORT, default 8765)"
    Write-Host "  demo     one-shot simulation; exits when the report is written"
    Write-Host "  spark    Spark-engine simulation (profile: spark)"
    Write-Host "  dev      interactive shell in the development image (profile: tools)"
    Write-Host ""
    Write-Host "The compose project name isolates this stack: containers are prefixed with it"
    Write-Host "and results live in the volume insilico-trial-mas_artifacts."
}

function Assert-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail "docker is not installed or not on PATH" }
    docker info *> $null
    if ($LASTEXITCODE -ne 0) { Fail "the Docker daemon is not reachable (start Docker Desktop)" }
    docker compose version *> $null
    if ($LASTEXITCODE -ne 0) { Fail "the docker compose plugin is required (docker compose version failed)" }
}

function Invoke-Compose {
    # Always called as `Invoke-Compose @("up", "-d", "studio")`: a bare `-d` would be
    # parsed as a PowerShell parameter name and dropped, running compose attached.
    param([string[]] $Arguments)
    & docker compose @Arguments
    if ($LASTEXITCODE -ne 0) { Fail "docker compose $($Arguments -join ' ') failed" }
}

function Get-OtherProjects {
    # Read-only: a shared machine usually has other stacks, and we never touch them.
    try {
        $json = docker compose ls --format json 2>$null | ConvertFrom-Json
    } catch { return @() }
    return @($json | Where-Object { $_.Name -ne $ProjectName -and $_.Status })
}

function Test-PortBusy {
    # Get-NetTCPConnection exists only on Windows, so a socket probe is used first:
    # it behaves the same on Windows, WSL and Linux.
    param([int] $Number)
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $client.Connect("127.0.0.1", $Number)
        return $true
    }
    catch { return $false }
    finally { $client.Dispose() }
}

function Start-ProjectService {
    param([string] $Name)
    Assert-Docker
    Write-Step "Starting: $Name"
    switch ($Name) {
        "all"    { Invoke-Compose @("up", "-d", "studio"); Invoke-Compose @("up", "demo") }
        "studio" { Invoke-Compose @("up", "-d", "studio") }
        "demo"   { Invoke-Compose @("run", "--rm", "demo") }
        "spark"  { Invoke-Compose @("--profile", "spark", "run", "--rm", "spark") }
        "dev"    { Invoke-Compose @("--profile", "tools", "run", "--rm", "dev") }
        "test"   { Invoke-Compose @("--profile", "ci", "build", "test") }
        default  { Fail "unknown service: $Name (studio, demo, spark, dev, test, all)" }
    }
    if ($Name -eq "all" -or $Name -eq "studio") {
        Write-Ok "Studio: http://localhost:$Port"
        if (Test-PortBusy -Number $Port) {
            $owner = (docker ps --filter "publish=$Port" --format "{{.Names}}" | Select-Object -First 1)
            if ($owner) { Write-Ok "port $Port is served by $owner" }
            else { Write-Warn "port $Port is in use by another program - rerun with -Port 8877" }
        }
        Write-Ok "stop it later with: .\scripts\project.ps1 stop"
    }
}

function Stop-ProjectContainers {
    Assert-Docker
    Write-Step "Stopping this project's containers (other projects are left alone)"
    Invoke-Compose @("stop")
    Write-Ok "containers stopped; the results volume is intact"
    Write-Ok "start again with: .\scripts\project.ps1 start"
}

function Remove-ProjectStack {
    param([switch] $Volumes)
    Assert-Docker
    if ($Volumes) {
        Write-Step "This deletes the results volume of $ProjectName (all runs and reports)"
        $answer = Read-Host "    type 'yes' to continue"
        if ($answer -ne "yes") { Write-Warn "cancelled"; return }
        Invoke-Compose @("--profile", "spark", "--profile", "tools", "--profile", "ci", "down", "-v", "--remove-orphans")
        Write-Ok "containers, network and volume removed"
    }
    else {
        Write-Step "Stopping and removing this project's containers and network"
        Invoke-Compose @("--profile", "spark", "--profile", "tools", "--profile", "ci", "down", "--remove-orphans")
        Write-Ok "removed; the results volume is kept (use 'purge' to delete it)"
    }
}

function Show-ProjectStatus {
    Assert-Docker
    Write-Step "This project: $ProjectName"
    $running = docker compose ps --format "{{.Service}}`t{{.Status}}`t{{.Ports}}" 2>$null
    if (-not $running) { Write-Host "    nothing is running" } else { $running | ForEach-Object { Write-Host "    $_" } }

    Write-Step "Other compose projects on this machine (read-only)"
    $others = Get-OtherProjects
    if ($others.Count -eq 0) { Write-Host "    none" }
    else { $others | ForEach-Object { Write-Host "    $($_.Name): $($_.Status)" } }

    Write-Step "Restart policy of this project's containers"
    $ids = docker compose ps -q 2>$null
    if (-not $ids) { Write-Host "    no containers to inspect" }
    else {
        docker inspect --format "    {{.Name}}: restart={{.HostConfig.RestartPolicy.Name}}" @($ids)
        Write-Host "    (restart=none means a server reboot never brings them back)"
    }
}

function Show-ProjectDoctor {
    Assert-Docker
    Write-Step "Docker"
    docker version --format "    client {{.Client.Version}} / server {{.Server.Version}}"
    docker compose version | ForEach-Object { Write-Host "    $_" }

    Write-Step "Ports"
    if (Test-PortBusy -Number $Port) {
        $owner = (docker ps --filter "publish=$Port" --format "{{.Names}}" | Select-Object -First 1)
        if ($owner) { Write-Ok "port $Port is held by $owner" }
        else { Write-Warn "port $Port is used by another program - use -Port <number>" }
    }
    else { Write-Ok "port $Port is free" }

    Write-Step "Autostart after a reboot"
    Write-Ok 'every service declares restart: "no"'
    Write-Host "    containers started earlier by another policy can be fixed with:"
    Write-Host "      docker update --restart=no (docker ps -aq)"

    Write-Step "Disk footprint"
    docker system df --format "    {{.Type}}: {{.Size}} (reclaimable {{.Reclaimable}})"

    Write-Step "Other compose projects"
    $others = Get-OtherProjects
    if ($others.Count -eq 0) { Write-Host "    none" }
    else { $others | ForEach-Object { Write-Host "    $($_.Name): $($_.Status)" } }
}

switch ($Verb.ToLower()) {
    "start"   { Start-ProjectService -Name ($(if ($Target) { $Target } else { "all" })) }
    "stop"    { Stop-ProjectContainers }
    "down"    { Remove-ProjectStack }
    "purge"   { Remove-ProjectStack -Volumes }
    "restart" { Stop-ProjectContainers; Start-ProjectService -Name ($(if ($Target) { $Target } else { "all" })) }
    "status"  { Show-ProjectStatus }
    "ps"      { Show-ProjectStatus }
    "logs"    {
        Assert-Docker
        $service = if ($Target) { $Target } else { "studio" }
        Write-Step "Following the log of $service (Ctrl-C to stop following)"
        Invoke-Compose @("logs", "-f", "--tail", "50", $service)
    }
    "url"     { Write-Host "http://localhost:$Port" }
    "doctor"  { Show-ProjectDoctor }
    default   { Show-Usage }
}
