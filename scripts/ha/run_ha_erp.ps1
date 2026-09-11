[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ExpectedComputerName,

    [Parameter(Mandatory = $false)]
    [string]$PlaywrightBrowsersPath = 'D:\TakealotRuntime\playwright'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

if ($env:COMPUTERNAME -cne $ExpectedComputerName) {
    throw "Safety stop: expected computer $ExpectedComputerName, got $env:COMPUTERNAME."
}

$ProjectRoot = (
    Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')
).Path
$PythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$LogDirectory = Join-Path $ProjectRoot 'logs'
$SupervisorLogPath = Join-Path $LogDirectory 'ha-erp-runner.log'
$StdoutPath = Join-Path $LogDirectory 'ha-erp.stdout.log'
$StderrPath = Join-Path $LogDirectory 'ha-erp.stderr.log'

if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Project Python not found: $PythonPath"
}

New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null

function Write-HaRunnerLog {
    param([Parameter(Mandatory = $true)][string]$Message)

    $Timestamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz')
    Add-Content -LiteralPath $SupervisorLogPath -Encoding UTF8 -Value "[$Timestamp] $Message"
}

$env:TAKEALOT_PROJECT_ROOT = $ProjectRoot
if (Test-Path -LiteralPath $PlaywrightBrowsersPath -PathType Container) {
    $env:PLAYWRIGHT_BROWSERS_PATH = $PlaywrightBrowsersPath
}

$RolePreflight = @'
import json
from pathlib import Path

from sqlalchemy import create_engine, text

from takealot_ops.settings import DashboardSettings

root = Path.cwd()
settings = DashboardSettings.from_env(root)
engine = create_engine(settings.database_url, pool_pre_ping=True)
try:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT @@global.server_id, @@global.read_only, "
                "@@global.super_read_only"
            )
        ).one()
    print(
        json.dumps(
            {
                "server_id": int(row[0]),
                "read_only": int(row[1]),
                "super_read_only": int(row[2]),
            }
        )
    )
finally:
    engine.dispose()
'@
$RolePreflightBase64 = [Convert]::ToBase64String(
    [Text.Encoding]::UTF8.GetBytes($RolePreflight)
)
$RolePreflightLauncher = (
    "import base64; exec(base64.b64decode('$RolePreflightBase64'))"
)

Push-Location -LiteralPath $ProjectRoot
try {
    $SavedErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $PreflightOutput = @(& $PythonPath -c $RolePreflightLauncher 2>&1)
        $PreflightExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $SavedErrorActionPreference
    }
}
finally {
    Pop-Location
}
if ($PreflightExitCode -ne 0) {
    $Failure = "Unable to verify local MySQL role: $($PreflightOutput -join ' ')"
    Write-HaRunnerLog "HA ERP start refused: $Failure"
    throw $Failure
}
$Role = $PreflightOutput[-1] | ConvertFrom-Json
if ([int]$Role.read_only -ne 0 -or [int]$Role.super_read_only -ne 0) {
    $Failure = (
        'Safety stop: HA ERP may start only after local MySQL is writable; ' +
        "server_id=$($Role.server_id), read_only=$($Role.read_only), " +
        "super_read_only=$($Role.super_read_only)."
    )
    Write-HaRunnerLog "HA ERP start refused: $Failure"
    throw $Failure
}

Write-HaRunnerLog (
    "Starting HA ERP on server_id=$($Role.server_id) after writable-role preflight."
)
Push-Location -LiteralPath $ProjectRoot
try {
    & $PythonPath -m takealot_ops.cli dashboard 1>> $StdoutPath 2>> $StderrPath
    $DashboardExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
Write-HaRunnerLog "HA ERP exited with code $DashboardExitCode."
exit $DashboardExitCode
