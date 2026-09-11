[CmdletBinding()]
param([switch]$Apply)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$BlueRoot = 'D:\TakealotBlue'
$BlueServiceName = 'TakealotBlueMySQL'
$BlueTaskName = 'Takealot Blue Stage Web'
$BluePython = Join-Path $BlueRoot 'runtime\Scripts\python.exe'
$BlueConfig = Get-Content -LiteralPath (Join-Path $BlueRoot 'node.json') -Raw | ConvertFrom-Json
if ($env:COMPUTERNAME -cne $BlueConfig.computer -or
    $env:COMPUTERNAME -cnotin @('DESKTOP-NTRMANG', 'LAPTOP-2T5MN8EU') -or
    $BlueConfig.mysql_port -ne 3307) { throw 'Unexpected blue node identity' }
$BlueIni = Join-Path $BlueRoot 'mysql\my.ini'
$BlueIniText = Get-Content -LiteralPath $BlueIni -Raw
foreach ($BlueSetting in @('port=3307', 'datadir=D:/TakealotBlue/mysql/data',
                           'read-only=ON', 'super-read-only=ON')) {
    if ($BlueSetting -cnotin ($BlueIniText -split '\r?\n')) { throw 'Unexpected blue MySQL config' }
}
$ExistingBlueService = Get-CimInstance Win32_Service -Filter "Name='TakealotBlueMySQL'"
if ($ExistingBlueService -and $ExistingBlueService.PathName -notlike '*D:\TakealotBlue\mysql\my.ini*') {
    throw 'Service name is owned by an unexpected installation'
}
$ExistingBlueTask = Get-ScheduledTask -TaskName $BlueTaskName -ErrorAction SilentlyContinue
if ($ExistingBlueTask -and $ExistingBlueTask.Actions.Arguments -notlike '*D:\TakealotBlue\run_blue_web.ps1*') {
    throw 'Task name is owned by an unexpected installation'
}
if (-not $Apply) {
    [pscustomobject]@{Apply=$false; Service=$BlueServiceName; Task=$BlueTaskName; Root=$BlueRoot} |
        ConvertTo-Json
    exit 0
}
$BluePrincipal = [Security.Principal.WindowsPrincipal]::new(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $BluePrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator rights are required for the new blue service and startup task'
}
try {
    # mysqld bootstrap may assign creator-only file ACLs. Grant only the service identity
    # on this exact new blue subtree; never alter a production data directory's ACLs.
    $BlueDataPath = (Resolve-Path -LiteralPath (Join-Path $BlueRoot 'mysql\data')).Path
    if ($BlueDataPath -cne 'D:\TakealotBlue\mysql\data') { throw 'Unexpected blue data path' }
    & icacls.exe $BlueDataPath /grant:r '*S-1-5-18:(OI)(CI)F' /T /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Blue service data ACL update failed' }
    if (-not $ExistingBlueService) {
        $BlueListener = @(Get-NetTCPConnection -LocalPort 3307 -State Listen -ErrorAction SilentlyContinue)
        if ($BlueListener.Count -gt 0) {
            # connection() validates hostname, server_id, port AND datadir before SHUTDOWN.
            & $BluePython -c "import sys; sys.path.insert(0,'D:/TakealotBlue'); import blue_node; c=blue_node.connection(); c.cursor().execute('SHUTDOWN'); c.close()"
            if ($LASTEXITCODE -ne 0) { throw 'Blue database shutdown preflight failed' }
            for ($BlueWait=0; $BlueWait -lt 60; $BlueWait++) {
                $PendingBlueProcesses = @(Get-CimInstance Win32_Process -Filter "Name='mysqld.exe'" |
                    Where-Object { $_.CommandLine -like '*--defaults-file=D:\TakealotBlue\mysql\my.ini*' })
                if (-not (Get-NetTCPConnection -LocalPort 3307 -State Listen -ErrorAction SilentlyContinue) `
                    -and $PendingBlueProcesses.Count -eq 0) { break }
                Start-Sleep -Milliseconds 500
            }
            if ((Get-NetTCPConnection -LocalPort 3307 -State Listen -ErrorAction SilentlyContinue) `
                -or $PendingBlueProcesses.Count -ne 0) {
                throw 'Blue process or port has not been released'
            }
        }
        $BlueMySQL = Join-Path $BlueConfig.mysql_bin 'mysqld.exe'
        & $BlueMySQL --install $BlueServiceName "--defaults-file=$BlueIni"
        if ($LASTEXITCODE -ne 0) { throw 'Blue MySQL service installation failed' }
    }
    Set-Service -Name $BlueServiceName -StartupType Automatic
    Start-Service -Name $BlueServiceName
    & $BluePython (Join-Path $BlueRoot 'blue_node.py') status
    if ($LASTEXITCODE -ne 0) { throw 'Blue MySQL service identity check failed' }

    # Stop only our temporary staged blue web, never any production listener.
    if ($ExistingBlueTask -and $ExistingBlueTask.State -eq 'Running') {
        Stop-ScheduledTask -TaskName $BlueTaskName
    }
    $BlueWebProcesses = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match '(?:^|[\s"])D:\\TakealotBlue\\blue_web\.py(?:[\s"]|$)' })
    foreach ($BlueWebProcess in $BlueWebProcesses) {
        Stop-Process -Id $BlueWebProcess.ProcessId -Force -ErrorAction SilentlyContinue
    }
    $BlueAction = New-ScheduledTaskAction -Execute (Get-Command powershell.exe).Source `
        -Argument '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\TakealotBlue\run_blue_web.ps1' `
        -WorkingDirectory $BlueRoot
    $BlueTaskPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount
    $BlueTaskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew `
        -RestartCount 12 -RestartInterval ([TimeSpan]::FromMinutes(1)) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $BlueTaskName -Action $BlueAction `
        -Trigger (New-ScheduledTaskTrigger -AtStartup) -Principal $BlueTaskPrincipal `
        -Settings $BlueTaskSettings -Force | Out-Null
    Start-ScheduledTask -TaskName $BlueTaskName
    if ($env:COMPUTERNAME -ceq 'LAPTOP-2T5MN8EU') {
        $BlueFirewallName = 'TakealotBlueStage8503'
        $BlueRule = Get-NetFirewallRule -Name $BlueFirewallName -ErrorAction SilentlyContinue
        if (-not $BlueRule) {
            New-NetFirewallRule -Name $BlueFirewallName -DisplayName $BlueFirewallName `
                -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8503 `
                -RemoteAddress @('100.70.103.11','100.72.100.10','LocalSubnet') `
                -Profile Any | Out-Null
        }
    }
    [pscustomobject]@{Status='installed'; Computer=$env:COMPUTERNAME; Service=$BlueServiceName;
        Task=$BlueTaskName; AutoFailover=$false} | ConvertTo-Json |
        Set-Content -LiteralPath (Join-Path $BlueRoot 'state\services-install.json') -Encoding UTF8
}
catch {
    [pscustomobject]@{Status='failed'; Computer=$env:COMPUTERNAME;
        Message=$_.Exception.Message} | ConvertTo-Json |
        Set-Content -LiteralPath (Join-Path $BlueRoot 'state\services-install.json') -Encoding UTF8
    throw
}
