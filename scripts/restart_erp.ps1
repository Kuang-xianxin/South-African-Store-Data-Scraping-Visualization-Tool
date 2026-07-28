[CmdletBinding()]
param(
    [int]$HealthTimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "Project Python not found: $pythonPath"
}

$portText = & $pythonPath -c "from pathlib import Path; from takealot_ops.settings import DashboardSettings; print(DashboardSettings.from_env(Path.cwd()).dashboard_port)" 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Unable to read the ERP port: $portText"
}
$port = [int]($portText | Select-Object -Last 1)

$listeners = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
foreach ($listener in $listeners) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
    if ($null -eq $process -or $process.CommandLine -notlike "*takealot_ops.erp.web:app*") {
        throw "Port $port is owned by a process outside this ERP; restart aborted."
    }
    Stop-Process -Id $listener.OwningProcess
}

$releaseDeadline = (Get-Date).AddSeconds(10)
while (
    (Get-Date) -lt $releaseDeadline -and
    (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
) {
    Start-Sleep -Milliseconds 250
}
if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
    throw "The old ERP stopped, but port $port was not released within 10 seconds."
}

$logDirectory = Join-Path $projectRoot "logs"
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$stdoutPath = Join-Path $logDirectory "erp.stdout.log"
$stderrPath = Join-Path $logDirectory "erp.stderr.log"
$launcher = Start-Process `
    -FilePath $pythonPath `
    -ArgumentList @("-m", "takealot_ops.cli", "dashboard") `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath `
    -PassThru

$healthUrl = "http://127.0.0.1:$port/api/health"
$healthDeadline = (Get-Date).AddSeconds($HealthTimeoutSeconds)
$healthy = $false
while ((Get-Date) -lt $healthDeadline) {
    if ($launcher.HasExited) {
        break
    }
    try {
        $response = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
        if ($response.status -eq "ok") {
            $healthy = $true
            break
        }
    }
    catch {
        Start-Sleep -Milliseconds 500
    }
}

if (-not $healthy) {
    $stderrTail = if (Test-Path -LiteralPath $stderrPath) {
        (Get-Content -LiteralPath $stderrPath -Tail 20) -join [Environment]::NewLine
    }
    else {
        "No stderr log was created."
    }
    throw "ERP did not pass its health check after restart: $healthUrl`n$stderrTail"
}

Write-Output "ERP restarted and passed health check: $healthUrl (launcher PID $($launcher.Id))"
