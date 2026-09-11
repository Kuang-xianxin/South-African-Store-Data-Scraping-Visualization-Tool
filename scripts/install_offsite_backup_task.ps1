param(
    [Parameter(Mandatory = $false)]
    [string]$ProjectPath = (Get-Location).Path,

    [Parameter(Mandatory = $false)]
    [ValidatePattern('^(?:[01]\d|2[0-3]):[0-5]\d$')]
    [string]$BackupAt = '03:30'
)

$ErrorActionPreference = 'Stop'
$ResolvedProjectPath = (Resolve-Path -LiteralPath $ProjectPath).Path
$BackupScriptPath = Join-Path $ResolvedProjectPath 'scripts\run_offsite_backup.ps1'
$PasswordPath = Join-Path $env:LOCALAPPDATA `
    'TakealotOffsiteBackup\restic-password.clixml'

foreach ($RequiredPath in @($BackupScriptPath, $PasswordPath)) {
    if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
        throw "Required offsite backup file is missing: $RequiredPath"
    }
}

$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$TaskName = 'Takealot Encrypted Offsite Backup'
$ActionArguments = '-NoProfile -NonInteractive -WindowStyle Hidden ' +
    "-ExecutionPolicy Bypass -File `"$BackupScriptPath`" " +
    "-ProjectPath `"$ResolvedProjectPath`""
$Action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument $ActionArguments `
    -WorkingDirectory $ResolvedProjectPath

$DailyTrigger = New-ScheduledTaskTrigger -Daily -At $BackupAt
$LogonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
$LogonTrigger.Delay = 'PT10M'
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 8 `
    -RestartInterval (New-TimeSpan -Minutes 30) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$Principal = New-ScheduledTaskPrincipal `
    -UserId $CurrentUser `
    -LogonType Interactive `
    -RunLevel Limited
$Task = New-ScheduledTask `
    -Action $Action `
    -Trigger @($DailyTrigger, $LogonTrigger) `
    -Settings $Settings `
    -Principal $Principal `
    -Description 'Create an encrypted incremental Restic backup on the Tailscale laptop.'

Register-ScheduledTask -TaskName $TaskName -InputObject $Task -Force | Out-Null
Write-Host "Installed $TaskName at $BackupAt and 10 minutes after Windows sign-in."
Write-Host 'The task runs only for the current signed-in Windows user.'
