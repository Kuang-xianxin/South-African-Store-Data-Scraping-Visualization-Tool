[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$TaskName = 'Takealot HA Laptop Lease Guard'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ExpectedComputerName = 'LAPTOP-2T5MN8EU'
if ($env:COMPUTERNAME -cne $ExpectedComputerName) {
    throw "Safety stop: expected $ExpectedComputerName, got $env:COMPUTERNAME."
}
$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$Principal = [Security.Principal.WindowsPrincipal]::new($Identity)
if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator rights are required to register the laptop witness guard.'
}

$RunnerPath = (
    Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'run_laptop_witness_guard.ps1')
).Path
$PowerShellPath = (Get-Command powershell.exe -ErrorAction Stop).Source
$Action = New-ScheduledTaskAction `
    -Execute $PowerShellPath `
    -Argument (
        '-NoProfile -NonInteractive -ExecutionPolicy Bypass ' +
        "-File `"$RunnerPath`""
    ) `
    -WorkingDirectory (Split-Path -Parent $RunnerPath)
$Trigger = New-ScheduledTaskTrigger -AtStartup
$TaskPrincipal = New-ScheduledTaskPrincipal `
    -UserId 'SYSTEM' `
    -LogonType ServiceAccount `
    -RunLevel Highest
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -RestartCount 1440 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
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
[xml]$TaskXml = Export-ScheduledTask -TaskName $TaskName
$TriggerElements = @(
    $TaskXml.SelectNodes(
        "/*[local-name()='Task']/*[local-name()='Triggers']/*"
    )
)
[pscustomobject]@{
    computer = $env:COMPUTERNAME
    task_name = $Installed.TaskName
    state = [string]$Installed.State
    user_id = [string]$Installed.Principal.UserId
    enabled = [bool]$Installed.Settings.Enabled
    trigger_count = $TriggerElements.Count
    runner = $RunnerPath
}
