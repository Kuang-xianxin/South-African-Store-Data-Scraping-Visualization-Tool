[CmdletBinding()]
param([switch]$Apply)
$ErrorActionPreference='Stop'
$BlueRoot='D:\TakealotBlue'
$BlueCfg=Get-Content -LiteralPath "$BlueRoot\node.json" -Raw | ConvertFrom-Json
if ($BlueCfg.computer -cne $env:COMPUTERNAME -or $BlueCfg.mysql_port -ne 3307) { throw 'Unknown BLUE node' }
if (-not $Apply) { [pscustomobject]@{apply=$false;node=$BlueCfg.node;web=8503;green_changed=$false} | ConvertTo-Json; return }
$BluePrincipal=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $BluePrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Administrator required for BLUE startup tasks' }
try {
    # No MySQL service restart, no green/legacy task touched, no authority to promote a replica.
    & "$BlueRoot\runtime\Scripts\python.exe" "$BlueRoot\blue_activation.py" preflight
    if ($LASTEXITCODE -ne 0) { throw 'BLUE writable preflight failed; running web preserved' }
    & "$BlueRoot\runtime\Scripts\python.exe" "$BlueRoot\blue_activation.py" activate
    if ($LASTEXITCODE -ne 0) { throw 'BLUE activation marker failed; running web preserved' }
    $BlueWeb=Get-ScheduledTask -TaskName 'Takealot Blue Stage Web'
    if ($BlueWeb.Actions.Arguments -cne '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\TakealotBlue\run_blue_web.ps1') { throw 'Unexpected BLUE web task' }
    Stop-ScheduledTask -TaskName 'Takealot Blue Stage Web'
    $BlueProcesses=@(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
        $_.CommandLine -match '(?:^|[\s"])D:\\TakealotBlue\\blue_web\.py(?:[\s"]|$)' })
    foreach ($BlueProcess in $BlueProcesses) { Stop-Process -Id $BlueProcess.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-ScheduledTask -TaskName 'Takealot Blue Stage Web'
    $BlueTaskName='Takealot Blue Crawler Worker'
    $BlueExisting=Get-ScheduledTask -TaskName $BlueTaskName -ErrorAction SilentlyContinue
    if ($BlueExisting -and $BlueExisting.Actions.Arguments -cne '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\TakealotBlue\run_blue_worker.ps1') { throw 'Unexpected BLUE worker task' }
    if ($BlueExisting) {
        Stop-ScheduledTask -TaskName $BlueTaskName
        $BlueWorkerProcesses=@(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
            $_.CommandLine -match '(?:^|[\s"])D:\\TakealotBlue\\blue_worker\.py(?:[\s"]|$)' })
        foreach ($BlueWorkerProcess in $BlueWorkerProcesses) { Stop-Process -Id $BlueWorkerProcess.ProcessId -Force -ErrorAction SilentlyContinue }
    }
    if (-not $BlueExisting) {
        $BlueAction=New-ScheduledTaskAction -Execute (Get-Command powershell.exe).Source `
            -Argument '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\TakealotBlue\run_blue_worker.ps1' -WorkingDirectory $BlueRoot
        $BlueSettings=New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -MultipleInstances IgnoreNew -RestartCount 12 -RestartInterval ([TimeSpan]::FromMinutes(1)) -ExecutionTimeLimit ([TimeSpan]::Zero)
        Register-ScheduledTask -TaskName $BlueTaskName -Action $BlueAction -Trigger (New-ScheduledTaskTrigger -AtStartup) `
            -Principal (New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount) -Settings $BlueSettings | Out-Null
    }
    Start-ScheduledTask -TaskName $BlueTaskName
    [pscustomobject]@{status='started';node=$BlueCfg.node;green_changed=$false} | ConvertTo-Json |
        Set-Content -LiteralPath "$BlueRoot\state\writable-activation.json" -Encoding UTF8
} catch {
    [pscustomobject]@{status='failed';node=$BlueCfg.node;error=$_.Exception.Message} | ConvertTo-Json |
        Set-Content -LiteralPath "$BlueRoot\state\writable-activation.json" -Encoding UTF8
    throw
}
