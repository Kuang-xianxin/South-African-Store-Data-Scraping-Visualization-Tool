param(
    [Parameter(Mandatory = $false)]
    [string]$ProjectPath = (Get-Location).Path,

    [Parameter(Mandatory = $false)]
    [ValidateRange(1024, 65535)]
    [int]$ListenPort = 17890,

    [Parameter(Mandatory = $false)]
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$SshHost = 'takealot-backup-laptop',

    [Parameter(Mandatory = $false)]
    [ValidateRange(1024, 65535)]
    [int]$RemoteProxyPort = 7897
)

$ErrorActionPreference = 'Stop'
$ResolvedProjectPath = (Resolve-Path -LiteralPath $ProjectPath).Path
$TunnelScriptPath = Join-Path $ResolvedProjectPath `
    'scripts\run_competitor_egress_tunnel.ps1'
if (-not (Test-Path -LiteralPath $TunnelScriptPath -PathType Leaf)) {
    throw "Tunnel watchdog is missing: $TunnelScriptPath"
}
if ($null -eq (Get-Command 'ssh.exe' -ErrorAction SilentlyContinue)) {
    throw 'Windows OpenSSH client ssh.exe is not installed or not on PATH.'
}

$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$TaskName = 'Takealot Competitor Laptop Egress Tunnel'
$ActionArguments = '-NoProfile -NonInteractive -WindowStyle Hidden ' +
    "-ExecutionPolicy Bypass -File `"$TunnelScriptPath`" " +
    "-ListenPort $ListenPort -SshHost `"$SshHost`" " +
    "-RemoteProxyPort $RemoteProxyPort"
$Action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument $ActionArguments `
    -WorkingDirectory $ResolvedProjectPath
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
$Trigger.Delay = 'PT1M'
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$Principal = New-ScheduledTaskPrincipal `
    -UserId $CurrentUser `
    -LogonType Interactive `
    -RunLevel Limited
$Task = New-ScheduledTask `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description (
        'Forward a loopback-only endpoint through SSH to the laptop Clash ' +
        'proxy for competitor public browser egress. No Seller API or ' +
        'database credentials are sent.'
    )

Register-ScheduledTask -TaskName $TaskName -InputObject $Task -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Installed and started $TaskName on 127.0.0.1:$ListenPort."
Write-Host "Laptop proxy target: 127.0.0.1:$RemoteProxyPort."
Write-Host 'The watchdog reconnects automatically when the laptop returns online.'
