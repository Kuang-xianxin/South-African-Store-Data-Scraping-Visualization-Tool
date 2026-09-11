[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Archive,
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{64}$')][string]$Sha256,
    [switch]$Apply
)
$ErrorActionPreference='Stop'
$BlueRoot='D:\TakealotBlue'
$BluePython=Join-Path $BlueRoot 'runtime\Scripts\python.exe'
$BlueSync=Join-Path $BlueRoot 'staging\blue_sync_release.py'
$BlueCfg=Get-Content -LiteralPath "$BlueRoot\node.json" -Encoding UTF8 -Raw | ConvertFrom-Json
if ($BlueCfg.computer -cne $env:COMPUTERNAME -or $BlueCfg.mysql_port -ne 3307) { throw 'Unknown BLUE node' }
$BlueArchivePath=(Resolve-Path -LiteralPath $Archive).Path
if (-not $BlueArchivePath.StartsWith("$BlueRoot\staging\",[StringComparison]::OrdinalIgnoreCase)) {
    throw 'The verified release must be staged inside BLUE'
}
& $BluePython $BlueSync inspect --archive $BlueArchivePath --sha256 $Sha256
if ($LASTEXITCODE -ne 0) { throw 'Release verification failed' }
if (-not $Apply) { return }
$BluePrincipal=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $BluePrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator required to reload existing BLUE SYSTEM tasks'
}
& $BluePython "$BlueRoot\blue_activation.py" preflight
if ($LASTEXITCODE -ne 0) { throw 'BLUE preflight failed; running services preserved' }
$BlueWorker=Get-Content -LiteralPath "$BlueRoot\state\durable-worker.json" -Encoding UTF8 -Raw | ConvertFrom-Json
if ($BlueWorker.state -ne 'idle' -or $BlueWorker.current_plid -or
    @($BlueWorker.journal.PSObject.Properties | Where-Object {$_.Name -in @('running','pending','blocked') -and $_.Value -gt 0}).Count) {
    throw 'BLUE worker is busy or has undelivered observations'
}
$BlueTasks=@{
    'Takealot Blue Stage Web'='-NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\TakealotBlue\run_blue_web.ps1'
    'Takealot Blue Crawler Worker'='-NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\TakealotBlue\run_blue_worker.ps1'
}
foreach($BlueTaskName in $BlueTasks.Keys) {
    $BlueTask=Get-ScheduledTask -TaskName $BlueTaskName
    if ($BlueTask.Actions.Arguments -cne $BlueTasks[$BlueTaskName]) { throw 'Unexpected BLUE task action' }
}
$BlueReport=Join-Path $BlueRoot 'state\common-release-install.json'
$BlueLogs=Join-Path $BlueRoot 'logs\common-release-install.log'
try {
    foreach($BlueTaskName in $BlueTasks.Keys) { Stop-ScheduledTask -TaskName $BlueTaskName }
    $BlueProcesses=@(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
        $_.CommandLine -match '(?:^|[\s"])D:\\TakealotBlue\\blue_(?:web|worker)\.py(?:[\s"]|$)' })
    foreach($BlueProcess in $BlueProcesses) { Stop-Process -Id $BlueProcess.ProcessId -Force -ErrorAction SilentlyContinue }
    $BlueDeadline=(Get-Date).AddSeconds(25)
    do {
        # BLUE binds loopback on main and wildcard on laptop. Tailscale may keep its own
        # 8503 proxy listeners open while the application is safely stopped.
        $BlueListener=@(Get-NetTCPConnection -State Listen -LocalPort 8503 -ErrorAction SilentlyContinue |
            Where-Object {$_.LocalAddress -in @('127.0.0.1','0.0.0.0','::1','::')})
        if (-not $BlueListener) { break }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $BlueDeadline)
    if ($BlueListener) { throw 'BLUE web has not stopped; no files changed' }
    & $BluePython $BlueSync apply --archive $BlueArchivePath --sha256 $Sha256 2>&1 | Tee-Object -FilePath $BlueLogs
    if ($LASTEXITCODE -ne 0) { throw 'BLUE overlay failed; inspect saved per-file backup' }
    foreach($BlueTaskName in $BlueTasks.Keys) { Start-ScheduledTask -TaskName $BlueTaskName }
    [pscustomobject]@{status='started';node=$BlueCfg.node;archive_sha256=$Sha256;at=(Get-Date).ToString('o')} |
        ConvertTo-Json | Set-Content -LiteralPath $BlueReport -Encoding UTF8
} catch {
    # Preserve failed-release evidence; do not start a partly installed application.
    [pscustomobject]@{status='failed';node=$BlueCfg.node;error=$_.Exception.Message;at=(Get-Date).ToString('o')} |
        ConvertTo-Json | Set-Content -LiteralPath $BlueReport -Encoding UTF8
    throw
}
