<#
.SYNOPSIS
  Start (or stop) all RepoMonitoring services: the WSL2 datastores, the Claude triage
  service, and the monitor dashboard.

.DESCRIPTION
  up     - bring up Postgres+Redis (WSL2), then the triage service and the monitor
           as background processes (logs under logs/). Waits for each port.
  down   - stop the triage service and monitor. Add -IncludeDatastores to also stop
           Postgres+Redis (their data persists either way).
  status - show what is listening.

  Assumes one-time setup is done (infra/stack.sh cluster created, `alembic upgrade head`,
  `python provisioning/ingest.py ...`). See DEMO.md.

.EXAMPLE
  ./infra/serve.ps1 up
  ./infra/serve.ps1 up -DataDir live-stage3,live-recreate -CacheOnly
  ./infra/serve.ps1 down
  ./infra/serve.ps1 status
#>
[CmdletBinding()]
param(
  [ValidateSet('up', 'down', 'status')]
  [string]$Action = 'up',
  [string[]]$DataDir = @('live-stage3'),
  [int]$MonitorPort = 8378,
  [int]$TriagePort = 8377,
  [switch]$CacheOnly,          # triage from cache only (offline, no keys/tokens)
  [switch]$IncludeDatastores   # 'down' also stops Postgres+Redis
)

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Logs = Join-Path $Root 'logs'
New-Item -ItemType Directory -Force -Path $Logs | Out-Null

# repo root as a WSL path: C:\a\b -> /mnt/c/a/b
$WslRoot = '/mnt/' + $Root.Substring(0, 1).ToLower() + ($Root.Substring(2) -replace '\\', '/')

function Test-Port([int]$Port) {
  $c = New-Object Net.Sockets.TcpClient
  try { $c.Connect('127.0.0.1', $Port); return $true } catch { return $false } finally { $c.Close() }
}

function Wait-Port([int]$Port, [int]$TimeoutSec = 30) {
  for ($i = 0; $i -lt $TimeoutSec; $i++) {
    if (Test-Port $Port) { return $true }
    Start-Sleep -Seconds 1
  }
  return $false
}

function Start-Bg([string]$Name, [string[]]$PyArgs) {
  if (Test-Path (Join-Path $Logs "$Name.pid")) {
    $old = Get-Content (Join-Path $Logs "$Name.pid")
    if (Get-Process -Id $old -ErrorAction SilentlyContinue) {
      Write-Host "  $Name already running (pid $old)"; return
    }
  }
  $out = Join-Path $Logs "$Name.out.log"
  $err = Join-Path $Logs "$Name.err.log"
  $p = Start-Process -FilePath 'python' -ArgumentList $PyArgs -WorkingDirectory $Root `
    -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden -PassThru
  $p.Id | Set-Content (Join-Path $Logs "$Name.pid")
  Write-Host "  started $Name (pid $($p.Id))  logs: logs/$Name.out.log"
}

function Stop-Bg([string]$Name, [int]$Port) {
  $pf = Join-Path $Logs "$Name.pid"
  if (Test-Path $pf) {
    $procId = Get-Content $pf
    try { Stop-Process -Id $procId -Force -ErrorAction Stop; Write-Host "  stopped $Name (pid $procId)" } catch {}
    Remove-Item $pf -Force
  }
  # belt and suspenders: whoever still holds the port
  Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
}

function Invoke-Stack([string]$Sub) {
  wsl -d Ubuntu -- bash -c "cd '$WslRoot' && bash infra/stack.sh $Sub"
}

switch ($Action) {

  'up' {
    Write-Host '[1/3] datastores (WSL2 Postgres + Redis)...'
    Invoke-Stack 'up'
    if (-not (Wait-Port 5544 20)) { throw 'Postgres (5544) did not come up' }
    if (-not (Wait-Port 6380 20)) { throw 'Redis (6380) did not come up' }

    foreach ($d in $DataDir) {
      if (-not (Test-Path (Join-Path $Root (Join-Path $d 'build-capture.json')))) {
        Write-Warning "data dir '$d' has no build-capture.json - run provisioning/recreate.py --out-dir $d first"
      }
    }

    Write-Host '[2/3] triage service...'
    $triageArgs = @('triage-service/claude_server.py', '--port', "$TriagePort")
    if ($CacheOnly) { $triageArgs += '--cache-only' }
    Start-Bg 'triage' $triageArgs
    if (-not (Wait-Port $TriagePort 30)) { Write-Warning "triage ($TriagePort) not listening yet - check logs/triage.err.log" }

    Write-Host '[3/3] monitor...'
    $monArgs = @('monitor/app.py', '--port', "$MonitorPort", '--triage-url', "http://127.0.0.1:$TriagePort/triage")
    foreach ($d in $DataDir) { $monArgs += @('--data-dir', $d) }
    Start-Bg 'monitor' $monArgs
    if (-not (Wait-Port $MonitorPort 20)) { Write-Warning "monitor ($MonitorPort) not listening yet - check logs/monitor.err.log" }

    Write-Host ''
    Write-Host "dashboard : http://127.0.0.1:$MonitorPort/"
    Write-Host "triage    : http://127.0.0.1:$TriagePort/health  (mode: $(if ($CacheOnly) {'cache-only'} else {'live'}))"
    Write-Host "stop with : ./infra/serve.ps1 down"
  }

  'down' {
    Write-Host 'stopping monitor + triage...'
    Stop-Bg 'monitor' $MonitorPort
    Stop-Bg 'triage' $TriagePort
    if ($IncludeDatastores) {
      Write-Host 'stopping datastores...'
      Invoke-Stack 'down'
    } else {
      Write-Host 'datastores left running (data persists; use -IncludeDatastores to stop them)'
    }
  }

  'status' {
    Write-Host 'datastores:'
    Invoke-Stack 'status'
    Write-Host 'services:'
    $t = if (Test-Port $TriagePort) { 'UP' } else { 'down' }
    $m = if (Test-Port $MonitorPort) { 'UP' } else { 'down' }
    Write-Host "  triage    127.0.0.1:$TriagePort  $t"
    Write-Host "  monitor   127.0.0.1:$MonitorPort  $m"
  }
}
