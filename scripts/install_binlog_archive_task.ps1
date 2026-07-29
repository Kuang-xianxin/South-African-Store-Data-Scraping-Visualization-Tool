param(
    [Parameter(Mandatory = $false)]
    [string]$ProjectPath = (Get-Location).Path,

    [Parameter(Mandatory = $false)]
    [ValidatePattern('^(?:[01]\d|2[0-3]):[0-5]\d$')]
    [string]$MaintenanceAt = '02:30'
)

$ErrorActionPreference = 'Stop'
$ResolvedProjectPath = (Resolve-Path -LiteralPath $ProjectPath).Path
$ProjectDrive = (Get-Item -LiteralPath $ResolvedProjectPath).PSDrive.Name
if ($ProjectDrive -ne 'D') {
    throw 'The formal project and all backup artifacts must be on drive D:.'
}

$PythonPath = Join-Path $ResolvedProjectPath '.venv\Scripts\python.exe'
$ArchiveLoopPath = Join-Path $ResolvedProjectPath 'scripts\run_binlog_archive_loop.ps1'
$MaintenanceLoopPath = Join-Path $ResolvedProjectPath 'scripts\run_binlog_maintenance_loop.ps1'
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Project Python environment not found: $PythonPath"
}

$env:TAKEALOT_PROJECT_ROOT = $ResolvedProjectPath
Push-Location -LiteralPath $ResolvedProjectPath
try {
    & $PythonPath -m takealot_ops.cli binlog-archive-status --preflight
    if ($LASTEXITCODE -ne 0) {
        throw 'Binlog archive preflight failed. Check .env and the backup account grants.'
    }
}
finally {
    Pop-Location
}

$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal `
    -UserId $CurrentUser `
    -LogonType S4U `
    -RunLevel Limited

function New-BinlogTaskAction {
    param(
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [Parameter(Mandatory = $false)][string]$AdditionalArguments = ''
    )
    $ActionArguments = "-NoProfile -NonInteractive -WindowStyle Hidden " +
        "-ExecutionPolicy Bypass -File `"$ScriptPath`" " +
        "-ProjectPath `"$ResolvedProjectPath`" $AdditionalArguments"
    return New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $ActionArguments
}

$ArchiveTaskName = 'Takealot MySQL Binlog Local Archive'
$ArchiveSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$ArchiveTask = New-ScheduledTask `
    -Action (New-BinlogTaskAction -ScriptPath $ArchiveLoopPath) `
    -Trigger (New-ScheduledTaskTrigger -AtStartup) `
    -Settings $ArchiveSettings `
    -Principal $Principal `
    -Description 'Continuously archive MySQL binary logs to the D drive.'
$MaintenanceTaskName = 'Takealot MySQL Binlog Local Archive Maintenance'
$MaintenanceSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5)
$MaintenanceTask = New-ScheduledTask `
    -Action (
        New-BinlogTaskAction `
            -ScriptPath $MaintenanceLoopPath `
            -AdditionalArguments "-MaintenanceAt $MaintenanceAt"
    ) `
    -Trigger (New-ScheduledTaskTrigger -Daily -At $MaintenanceAt) `
    -Settings $MaintenanceSettings `
    -Principal $Principal `
    -Description 'Verify closed D-drive binlog archives and remove expired files.'
try {
    Register-ScheduledTask -TaskName $ArchiveTaskName -InputObject $ArchiveTask -Force
    Register-ScheduledTask -TaskName $MaintenanceTaskName -InputObject $MaintenanceTask -Force
    Start-ScheduledTask -TaskName $ArchiveTaskName
    Write-Host "Installed and started $ArchiveTaskName"
    Write-Host "Installed $MaintenanceTaskName at $MaintenanceAt"
}
catch [Microsoft.Management.Infrastructure.CimException] {
    Write-Warning 'Scheduled Task registration was denied; using current-user logon startup.'
    $RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    $ArchiveStartup = "powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden " +
        "-ExecutionPolicy Bypass -File `"$ArchiveLoopPath`" " +
        "-ProjectPath `"$ResolvedProjectPath`""
    $MaintenanceStartup = "powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden " +
        "-ExecutionPolicy Bypass -File `"$MaintenanceLoopPath`" " +
        "-ProjectPath `"$ResolvedProjectPath`" -MaintenanceAt $MaintenanceAt"
    New-Item -Path $RunKey -Force | Out-Null
    New-ItemProperty `
        -Path $RunKey `
        -Name 'TakealotMySQLBinlogArchive' `
        -Value $ArchiveStartup `
        -PropertyType String `
        -Force | Out-Null
    New-ItemProperty `
        -Path $RunKey `
        -Name 'TakealotMySQLBinlogMaintenance' `
        -Value $MaintenanceStartup `
        -PropertyType String `
        -Force | Out-Null
    Start-Process `
        -FilePath 'powershell.exe' `
        -ArgumentList "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ArchiveLoopPath`" -ProjectPath `"$ResolvedProjectPath`"" `
        -WindowStyle Hidden
    Start-Process `
        -FilePath 'powershell.exe' `
        -ArgumentList "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$MaintenanceLoopPath`" -ProjectPath `"$ResolvedProjectPath`" -MaintenanceAt $MaintenanceAt" `
        -WindowStyle Hidden
    Write-Host 'Installed and started current-user logon startup loops.'
    Write-Host 'The archive resumes after Windows sign-in rather than before sign-in.'
}
Write-Host "Archive directory: $ResolvedProjectPath\backups\binlog"
