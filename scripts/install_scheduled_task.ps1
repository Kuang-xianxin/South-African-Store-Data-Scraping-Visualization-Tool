param(
    [Parameter(Mandatory = $false)]
    [string]$ProjectPath = (Get-Location).Path,

    [Parameter(Mandatory = $false)]
    [ValidatePattern('^(?:[01]\d|2[0-3]):[0-5]\d$')]
    [string]$DailyAt = '08:30'
)

$ErrorActionPreference = 'Stop'
$ResolvedProjectPath = (Resolve-Path -LiteralPath $ProjectPath).Path
$PythonPath = Join-Path $ResolvedProjectPath '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Python environment not found: $PythonPath"
}

# Scheduled command: python -m takealot_ops.cli daily-run
# The host setting remains loopback-only even if another user-level setting exists.
$EscapedProjectPath = $ResolvedProjectPath.Replace("'", "''")
$EscapedPythonPath = $PythonPath.Replace("'", "''")
$Command = "`$env:TAKEALOT_PROJECT_ROOT='$EscapedProjectPath'; " +
    "`$env:TAKEALOT_DASHBOARD_HOST='127.0.0.1'; " +
    "Set-Location -LiteralPath '$EscapedProjectPath'; " +
    "& '$EscapedPythonPath' -m takealot_ops.cli daily-run"
$ActionArguments = "-NoProfile -NonInteractive -WindowStyle Hidden -Command `"$Command`""
$Action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $ActionArguments
$Trigger = New-ScheduledTaskTrigger -Daily -At $DailyAt
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName 'Takealot Operations Daily Run' `
    -Description 'Read-only Takealot collection, reporting, integrity check, and backup.' `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Force

Write-Host "Scheduled task installed for $DailyAt from $ResolvedProjectPath"
