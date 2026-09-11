[CmdletBinding()]
param(
    [int]$HealthTimeoutSeconds = 45
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ExpectedComputerName = 'DESKTOP-NTRMANG'
$ExpectedDeployment = 'green-main'
$GreenPort = 8501
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$PythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$RunnerPath = Join-Path $PSScriptRoot 'run_main_green_web.py'
$LogRoot = 'D:\TakealotHA\blue-green'

if ($env:COMPUTERNAME -cne $ExpectedComputerName) {
    throw "Safety stop: expected $ExpectedComputerName, got $env:COMPUTERNAME."
}
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Project Python not found: $PythonPath"
}
if (-not (Test-Path -LiteralPath $RunnerPath -PathType Leaf)) {
    throw "Green runner not found: $RunnerPath"
}
if (Get-NetTCPConnection -State Listen -LocalPort $GreenPort -ErrorAction SilentlyContinue) {
    throw "Safety stop: TCP port $GreenPort is already in use."
}

New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null
$Timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$StdoutPath = Join-Path $LogRoot "main-green-$Timestamp.stdout.log"
$StderrPath = Join-Path $LogRoot "main-green-$Timestamp.stderr.log"
$Process = Start-Process `
    -FilePath $PythonPath `
    -ArgumentList @($RunnerPath) `
    -WorkingDirectory $ProjectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $StdoutPath `
    -RedirectStandardError $StderrPath `
    -PassThru

$Healthy = $false
$HealthBody = $null
$Deadline = (Get-Date).AddSeconds($HealthTimeoutSeconds)
while ((Get-Date) -lt $Deadline) {
    if ($Process.HasExited) {
        break
    }
    try {
        $Response = Invoke-WebRequest `
            -UseBasicParsing `
            -Uri "http://127.0.0.1:$GreenPort/api/health" `
            -TimeoutSec 3
        if (
            $Response.StatusCode -eq 200 -and
            $Response.Headers['X-Takealot-Deployment'] -ceq $ExpectedDeployment
        ) {
            $Healthy = $true
            $HealthBody = $Response.Content
            break
        }
    }
    catch {
    }
    Start-Sleep -Milliseconds 500
}

if (-not $Healthy) {
    if (-not $Process.HasExited) {
        Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
    }
    $StderrTail = if (Test-Path -LiteralPath $StderrPath -PathType Leaf) {
        @((Get-Content -LiteralPath $StderrPath -Tail 30)) -join ' | '
    }
    else {
        'No stderr log was created.'
    }
    throw "Green ERP did not become healthy; stderr: $StderrTail"
}

[pscustomobject]@{
    status = 'healthy'
    pid = $Process.Id
    deployment = $ExpectedDeployment
    health_body = $HealthBody
    stdout = $StdoutPath
    stderr = $StderrPath
} | ConvertTo-Json -Depth 4
