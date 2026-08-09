[CmdletBinding()]
param(
    [int]$MySqlWaitSeconds = 60,
    [int]$HealthTimeoutSeconds = 60
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$PythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$RestartScriptPath = Join-Path $ProjectRoot 'scripts\restart_erp.ps1'
$LogDirectory = Join-Path $ProjectRoot 'logs'
$StartupLogPath = Join-Path $LogDirectory 'erp-startup.log'

if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Project Python not found: $PythonPath"
}
if (-not (Test-Path -LiteralPath $RestartScriptPath -PathType Leaf)) {
    throw "ERP restart script not found: $RestartScriptPath"
}

New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
$StartupMutex = New-Object System.Threading.Mutex(
    $false,
    'Local\TakealotErpStartup'
)
$StartupMutexAcquired = $false

function Write-ErpStartupLog {
    param([Parameter(Mandatory = $true)][string]$Message)
    $Timestamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz')
    Add-Content -LiteralPath $StartupLogPath -Encoding UTF8 -Value "[$Timestamp] $Message"
}

try {
    try {
        $StartupMutexAcquired = $StartupMutex.WaitOne([TimeSpan]::FromSeconds(120))
    }
    catch [System.Threading.AbandonedMutexException] {
        $StartupMutexAcquired = $true
    }
    if (-not $StartupMutexAcquired) {
        throw 'Another ERP startup check did not finish within 120 seconds.'
    }

    $MySqlService = Get-Service -Name 'MySQL80' -ErrorAction SilentlyContinue
    if ($null -ne $MySqlService -and $MySqlService.Status -ne 'Running') {
        $MySqlDeadline = (Get-Date).AddSeconds($MySqlWaitSeconds)
        while ((Get-Date) -lt $MySqlDeadline -and $MySqlService.Status -ne 'Running') {
            Start-Sleep -Seconds 1
            $MySqlService.Refresh()
        }
        if ($MySqlService.Status -ne 'Running') {
            throw "MySQL80 did not reach Running within $MySqlWaitSeconds seconds."
        }
    }

    $env:TAKEALOT_PROJECT_ROOT = $ProjectRoot
    Push-Location -LiteralPath $ProjectRoot
    try {
        $PortOutput = @(
            & $PythonPath -c "from pathlib import Path; from takealot_ops.settings import DashboardSettings; print(DashboardSettings.from_env(Path.cwd()).dashboard_port)" 2>&1
        )
        $PortExitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    if ($PortExitCode -ne 0) {
        throw "Unable to read the ERP port: $($PortOutput -join ' ')"
    }
    $Port = [int]($PortOutput | Select-Object -Last 1)
    $HealthUrl = "http://127.0.0.1:$Port/api/health"

    $HealthResponse = $null
    try {
        $HealthResponse = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 3
    }
    catch {
        # A failed probe is the expected path after a real Windows shutdown.
    }
    if (
        $null -ne $HealthResponse -and
        $HealthResponse.status -eq 'ok' -and
        $HealthResponse.application -eq 'takealot-erp'
    ) {
        Write-ErpStartupLog "ERP already healthy at $HealthUrl; no restart needed."
        Write-Output "ERP already healthy: $HealthUrl"
        return
    }

    Write-ErpStartupLog "ERP unavailable at $HealthUrl; starting the formal restart chain."
    & $RestartScriptPath -HealthTimeoutSeconds $HealthTimeoutSeconds
    Write-ErpStartupLog "ERP startup completed and health check passed at $HealthUrl."
}
catch {
    $Failure = ($_.Exception.Message -replace '[\r\n]+', ' ').Trim()
    Write-ErpStartupLog "ERP startup failed: $Failure"
    throw
}
finally {
    if ($StartupMutexAcquired) {
        $StartupMutex.ReleaseMutex()
    }
    $StartupMutex.Dispose()
}
