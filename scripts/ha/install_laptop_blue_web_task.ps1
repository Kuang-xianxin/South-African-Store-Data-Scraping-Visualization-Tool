[CmdletBinding()]
param(
    [string]$TaskName = 'Takealot Blue Read Only ERP',
    [string]$InteractiveUser = 'LAPTOP-2T5MN8EU\QQ276'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (Test-Path -LiteralPath 'D:\TakealotHA\blue-green\legacy-blue-retired.json') {
    throw 'Legacy blue is retired. Do not reinstall its startup task.'
}

$ExpectedComputerName = 'LAPTOP-2T5MN8EU'
$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$Principal = [Security.Principal.WindowsPrincipal]::new($Identity)
if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator rights are required to register the blue ERP task.'
}
if ($env:COMPUTERNAME -cne $ExpectedComputerName) {
    throw "Safety stop: expected $ExpectedComputerName, got $env:COMPUTERNAME."
}

$RunnerPath = (Resolve-Path -LiteralPath (
    Join-Path $PSScriptRoot 'run_laptop_blue_web.ps1'
)).Path
$PowerShellPath = (Get-Command powershell.exe -ErrorAction Stop).Source
$Action = New-ScheduledTaskAction `
    -Execute $PowerShellPath `
    -Argument (
        '-NoProfile -NonInteractive -ExecutionPolicy Bypass ' +
        "-File `"$RunnerPath`""
    ) `
    -WorkingDirectory (Split-Path -Parent $RunnerPath)
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $InteractiveUser
$TaskPrincipal = New-ScheduledTaskPrincipal `
    -UserId $InteractiveUser `
    -LogonType Interactive `
    -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 12 `
    -RestartInterval ([TimeSpan]::FromMinutes(1)) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $TaskPrincipal `
    -Settings $Settings `
    -Force | Out-Null
Enable-ScheduledTask -TaskName $TaskName | Out-Null

$Installed = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
[pscustomobject]@{
    computer = $env:COMPUTERNAME
    task_name = $Installed.TaskName
    state = [string]$Installed.State
    enabled = [bool]$Installed.Settings.Enabled
    user_id = [string]$Installed.Principal.UserId
    logon_type = [string]$Installed.Principal.LogonType
    run_level = [string]$Installed.Principal.RunLevel
    runner = $RunnerPath
} | ConvertTo-Json -Depth 4
