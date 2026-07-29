param(
    [Parameter(Mandatory = $false)]
    [string]$ProjectPath = (Get-Location).Path,

    [Parameter(Mandatory = $false)]
    [ValidatePattern('^(?:[01]\d|2[0-3]):[0-5]\d$')]
    [string]$MaintenanceAt = '02:30'
)

$ErrorActionPreference = 'Stop'
$ResolvedProjectPath = (Resolve-Path -LiteralPath $ProjectPath).Path
$PythonPath = Join-Path $ResolvedProjectPath '.venv\Scripts\python.exe'
$CreatedNew = $false
$Mutex = [System.Threading.Mutex]::new(
    $true,
    'Local\TakealotMySQLBinlogMaintenanceLoop',
    [ref]$CreatedNew
)

if (-not $CreatedNew) {
    $Mutex.Dispose()
    exit 0
}

try {
    $env:TAKEALOT_PROJECT_ROOT = $ResolvedProjectPath
    Set-Location -LiteralPath $ResolvedProjectPath
    & $PythonPath -m takealot_ops.cli binlog-archive-maintain

    $Parts = $MaintenanceAt.Split(':')
    while ($true) {
        $Now = Get-Date
        $NextRun = $Now.Date.AddHours([int]$Parts[0]).AddMinutes([int]$Parts[1])
        if ($NextRun -le $Now) {
            $NextRun = $NextRun.AddDays(1)
        }
        Start-Sleep -Seconds ([Math]::Ceiling(($NextRun - $Now).TotalSeconds))
        & $PythonPath -m takealot_ops.cli binlog-archive-maintain
    }
}
finally {
    $Mutex.ReleaseMutex()
    $Mutex.Dispose()
}
