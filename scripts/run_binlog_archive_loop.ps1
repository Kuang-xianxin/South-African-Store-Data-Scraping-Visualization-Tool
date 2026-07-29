param(
    [Parameter(Mandatory = $false)]
    [string]$ProjectPath = (Get-Location).Path
)

$ErrorActionPreference = 'Stop'
$ResolvedProjectPath = (Resolve-Path -LiteralPath $ProjectPath).Path
$PythonPath = Join-Path $ResolvedProjectPath '.venv\Scripts\python.exe'
$CreatedNew = $false
$Mutex = [System.Threading.Mutex]::new(
    $true,
    'Local\TakealotMySQLBinlogArchiveLoop',
    [ref]$CreatedNew
)

if (-not $CreatedNew) {
    $Mutex.Dispose()
    exit 0
}

try {
    $env:TAKEALOT_PROJECT_ROOT = $ResolvedProjectPath
    Set-Location -LiteralPath $ResolvedProjectPath
    while ($true) {
        & $PythonPath -m takealot_ops.cli binlog-archive
        Start-Sleep -Seconds 60
    }
}
finally {
    $Mutex.ReleaseMutex()
    $Mutex.Dispose()
}
