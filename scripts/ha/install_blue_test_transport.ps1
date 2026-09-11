[CmdletBinding()]
param([switch]$Apply)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$BlueRoot='D:\TakealotBlue'
$BlueCfg=Get-Content -LiteralPath "$BlueRoot\node.json" -Raw | ConvertFrom-Json
if ($BlueCfg.computer -cne $env:COMPUTERNAME -or $BlueCfg.mysql_port -ne 3307 -or
    $env:COMPUTERNAME -cnotin @('DESKTOP-NTRMANG','LAPTOP-2T5MN8EU')) { throw 'Unknown BLUE node' }
if (-not $Apply) { [pscustomobject]@{apply=$false;node=$BlueCfg.node;blue_sql_port=13317} | ConvertTo-Json; return }
$BluePrincipal=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $BluePrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Administrator required for BLUE transport only' }
if ($BlueCfg.node -eq 'main') {
    if (-not (Test-Path -LiteralPath "$BlueRoot\state\blue-test-prepared.json")) { throw 'BLUE preparation missing' }
    $BlueAddress=Get-NetIPAddress -AddressFamily IPv4 -IPAddress '192.168.110.180' -ErrorAction Stop
    if (-not $BlueAddress) { throw 'Expected main LAN address absent' }
    $BlueExisting=@(netsh.exe interface portproxy show all)
    if ($BlueExisting -match '\b13317\b' -and -not ($BlueExisting -match '192\.168\.110\.180\s+13317\s+127\.0\.0\.1\s+3307')) {
        throw 'Existing BLUE portproxy differs; refusing overwrite'
    }
    netsh.exe interface portproxy add v4tov4 listenaddress=192.168.110.180 listenport=13317 connectaddress=127.0.0.1 connectport=3307 protocol=tcp
    if ($LASTEXITCODE -ne 0) { throw 'BLUE LAN proxy failed' }
    $BlueRule=Get-NetFirewallRule -Name 'TakealotBlueSQL13317' -ErrorAction SilentlyContinue
    if (-not $BlueRule) {
        New-NetFirewallRule -Name 'TakealotBlueSQL13317' -DisplayName 'Takealot BLUE SQL 13317 laptop only' `
            -Direction Inbound -Action Allow -Protocol TCP -LocalAddress '192.168.110.180' -LocalPort 13317 `
            -RemoteAddress '192.168.110.13' -Profile Any | Out-Null
    }
} else {
    $BlueTaskName='Takealot Blue Database Bridge'
    $BlueTask=Get-ScheduledTask -TaskName $BlueTaskName -ErrorAction SilentlyContinue
    if ($BlueTask -and $BlueTask.Actions.Arguments -cne 'D:\TakealotBlue\blue_db_bridge.py') { throw 'BLUE bridge task name collision' }
    if (-not $BlueTask) {
        if (Get-NetTCPConnection -LocalPort 13317 -State Listen -ErrorAction SilentlyContinue) { throw 'BLUE loopback port in use' }
        $BlueAction=New-ScheduledTaskAction -Execute "$BlueRoot\runtime\Scripts\python.exe" `
            -Argument 'D:\TakealotBlue\blue_db_bridge.py' -WorkingDirectory $BlueRoot
        $BlueSettings=New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -MultipleInstances IgnoreNew -RestartCount 12 -RestartInterval ([TimeSpan]::FromMinutes(1)) -ExecutionTimeLimit ([TimeSpan]::Zero)
        Register-ScheduledTask -TaskName $BlueTaskName -Action $BlueAction -Trigger (New-ScheduledTaskTrigger -AtStartup) `
            -Principal (New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount) -Settings $BlueSettings | Out-Null
    }
    Start-ScheduledTask -TaskName $BlueTaskName
}
[pscustomobject]@{status='installed';node=$BlueCfg.node;blue_sql_port=13317;green_changed=$false} |
    ConvertTo-Json | Set-Content -LiteralPath "$BlueRoot\state\blue-test-transport.json" -Encoding UTF8
Get-Content -LiteralPath "$BlueRoot\state\blue-test-transport.json" -Raw
