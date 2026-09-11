[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (Test-Path -LiteralPath 'D:\TakealotHA\blue-green\legacy-blue-retired.json') {
    throw 'Legacy blue is retired. Use the new blue stage on 8503.'
}
$ProgressPreference = 'SilentlyContinue'

$ExpectedComputerName = 'LAPTOP-2T5MN8EU'
if ($env:COMPUTERNAME -cne $ExpectedComputerName) {
    throw "Safety stop: expected $ExpectedComputerName, got $env:COMPUTERNAME."
}

$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$PythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$RunnerPath = Join-Path $PSScriptRoot 'run_laptop_blue_web.py'
$LogRoot = 'D:\TakealotHA\blue-green'
$SupervisorLog = Join-Path $LogRoot 'laptop-blue-runner.log'
$StdoutPath = Join-Path $LogRoot 'laptop-blue.stdout.log'
$StderrPath = Join-Path $LogRoot 'laptop-blue.stderr.log'

foreach ($RequiredPath in @($PythonPath, $RunnerPath)) {
    if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
        throw "Required blue runner file not found: $RequiredPath"
    }
}

New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null
$StartedAt = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz')
Add-Content -LiteralPath $SupervisorLog -Encoding UTF8 -Value (
    "[$StartedAt] Starting laptop read-only blue ERP."
)

Push-Location -LiteralPath $ProjectRoot
try {
    $BlueProcess = Start-Process `
        -FilePath $PythonPath `
        -ArgumentList @($RunnerPath) `
        -WorkingDirectory $ProjectRoot `
        -RedirectStandardOutput $StdoutPath `
        -RedirectStandardError $StderrPath `
        -WindowStyle Hidden `
        -Wait `
        -PassThru
    $ExitCode = $BlueProcess.ExitCode
}
finally {
    Pop-Location
}

$EndedAt = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz')
Add-Content -LiteralPath $SupervisorLog -Encoding UTF8 -Value (
    "[$EndedAt] Laptop blue ERP exited with code $ExitCode."
)
exit $ExitCode
