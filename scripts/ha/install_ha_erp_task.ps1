[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ExpectedComputerName,

    [Parameter(Mandatory = $false)]
    [string]$TaskName = 'Takealot HA ERP'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$Principal = [Security.Principal.WindowsPrincipal]::new($Identity)
if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator rights are required to register the HA ERP task.'
}
if ($env:COMPUTERNAME -cne $ExpectedComputerName) {
    throw "Safety stop: expected computer $ExpectedComputerName, got $env:COMPUTERNAME."
}

$RunnerPath = (
    Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'run_ha_erp.ps1')
).Path
$PowerShellPath = (Get-Command powershell.exe -ErrorAction Stop).Source
$ActionArguments = (
    '-NoProfile -NonInteractive -ExecutionPolicy Bypass ' +
    "-File `"$RunnerPath`" -ExpectedComputerName `"$ExpectedComputerName`""
)
$Action = New-ScheduledTaskAction `
    -Execute $PowerShellPath `
    -Argument $ActionArguments `
    -WorkingDirectory (Split-Path -Parent $RunnerPath)
$TaskPrincipal = New-ScheduledTaskPrincipal `
    -UserId 'NT AUTHORITY\NETWORK SERVICE' `
    -LogonType ServiceAccount `
    -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
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
    logon_type = [string]$Installed.Principal.LogonType
    run_level = [string]$Installed.Principal.RunLevel
    enabled = [bool]$Installed.Settings.Enabled
    trigger_count = $TriggerElements.Count
    runner = $RunnerPath
}
