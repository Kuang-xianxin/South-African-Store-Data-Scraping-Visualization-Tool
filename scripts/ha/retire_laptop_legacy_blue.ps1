[CmdletBinding()]
param([switch]$Apply)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
if ($env:COMPUTERNAME -cne 'LAPTOP-2T5MN8EU') {
    throw 'Laptop-only operation. Main production must NEVER be targeted.'
}
$Principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Laptop administrator required.'
}
$StageRoot = 'D:\TakealotBlue'
$LegacyRoot = 'D:\TakealotMySQLReplica'
$LegacyTask = 'Takealot Blue Read Only ERP'
$LegacyIni = 'D:\TakealotMySQLReplica\my.ini'
$Marker = 'D:\TakealotHA\blue-green\legacy-blue-retired.json'
$Python = 'D:\TakealotBlue\runtime\Scripts\python.exe'
$ArchiveNames = @('data', 'binlog', 'relay', 'logs', 'my.ini')

function Assert-PhysicalPath([string]$Path) {
    $Full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    if ($Full -ine $Path.TrimEnd('\')) { throw "Noncanonical path: $Path" }
    $Item = Get-Item -LiteralPath $Full -Force
    while ($null -ne $Item) {
        if ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Linked path: $Full" }
        $Parent = Split-Path -Parent $Item.FullName
        if (-not $Parent -or $Parent -eq $Item.FullName) { break }
        $Item = Get-Item -LiteralPath $Parent -Force
    }
}

function Assert-NewWeb([int]$Port = 8503) {
    $Request = [Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/api/health")
    $Request.Proxy = $null
    $Request.Timeout = 10000
    $Response = $Request.GetResponse()
    try {
        $Reader = [IO.StreamReader]::new($Response.GetResponseStream())
        try { $Health = $Reader.ReadToEnd() | ConvertFrom-Json } finally { $Reader.Dispose() }
        if ($Health.status -ne 'ok' -or $Health.deployment -ne 'blue-stage-laptop' -or
            $Health.mode -ne 'read-only-test') { throw 'New blue health identity mismatch.' }
    } finally { $Response.Close() }
}

function Get-LegacyProcesses {
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq 'mysqld.exe' -and
        $_.CommandLine -match 'defaults-file="?D:\\TakealotMySQLReplica\\my\.ini(?:"|\s|$)'
    })
}

Assert-PhysicalPath $StageRoot
Assert-PhysicalPath $LegacyRoot
Assert-PhysicalPath (Split-Path -Parent $Marker)
if (Test-Path -LiteralPath $Marker) { throw 'Retirement marker exists. Audit its journal; do not repeat blindly.' }
foreach ($Name in $ArchiveNames) {
    $Source = Join-Path $LegacyRoot $Name
    Assert-PhysicalPath $Source
    if ((Get-Item -LiteralPath $Source).PSIsContainer -and
        @(Get-ChildItem -LiteralPath $Source -Recurse -Force -Attributes ReparsePoint).Count) {
        throw "Archive source contains links: $Source"
    }
}
$Service = Get-CimInstance Win32_Service -Filter "Name='MySQL80'"
if ($null -eq $Service -or $Service.State -ne 'Running' -or
    $Service.PathName -notmatch 'defaults-file="?D:\\TakealotMySQLReplica\\my\.ini(?:"|\s|$)') {
    throw 'MySQL80 is not the exact running legacy laptop service.'
}
if (@((Get-Service -Name MySQL80).DependentServices).Count) { throw 'Legacy service has dependents.' }
$StageService = Get-CimInstance Win32_Service -Filter "Name='TakealotBlueMySQL'"
if ($null -eq $StageService -or $StageService.State -ne 'Running' -or
    $StageService.PathName -notmatch 'D:\\TakealotBlue\\mysql\\my.ini') { throw 'New service mismatch.' }
$Task = Get-ScheduledTask -TaskName $LegacyTask
if (@($Task.Actions).Count -ne 1 -or $Task.Actions[0].Arguments -notlike '*\scripts\ha\run_laptop_blue_web.ps1*') {
    throw 'Unexpected legacy web task action.'
}
$RunnerMatch = [regex]::Match($Task.Actions[0].Arguments, '-File "([^"]+run_laptop_blue_web\.ps1)"')
if (-not $RunnerMatch.Success) { throw 'Cannot resolve the exact legacy runner.' }
$LegacyWebPath = [IO.Path]::ChangeExtension($RunnerMatch.Groups[1].Value, '.py')
Assert-PhysicalPath $LegacyWebPath
$LegacyWebPattern = [regex]::Escape($LegacyWebPath) + '(?:"|\s|$)'
$OldListener = @(Get-NetTCPConnection -LocalPort 8502 -State Listen)
if ($OldListener.Count -ne 1) { throw 'Legacy port identity is ambiguous.' }
$OldWeb = Get-CimInstance Win32_Process -Filter "ProcessId=$($OldListener[0].OwningProcess)"
if ($OldWeb.CommandLine -notmatch $LegacyWebPattern) {
    throw 'Port 8502 is not the expected legacy Python web process.'
}
$ExistingProxy = @(netsh.exe interface portproxy show all)
if ($ExistingProxy -match '\b8502\b') { throw 'Existing portproxy on 8502 must be reviewed.' }
if ((Get-Service iphlpsvc).Status -ne 'Running') { throw 'IP Helper must already be running.' }
$Rule = Get-NetFirewallRule -DisplayName 'Takealot Blue Read Only ERP 8502'
if ($Rule.Enabled -ne 'True' -or $Rule.Action -ne 'Allow') { throw 'Legacy restricted firewall rule missing.' }
$AddressFilter = $Rule | Get-NetFirewallAddressFilter
if (@($AddressFilter.RemoteAddress | Where-Object { $_ -notin @('LocalSubnet','100.70.103.11','100.72.100.10') }).Count) {
    throw 'Legacy firewall is broader than the approved LAN/tailnet sources.'
}
Assert-NewWeb
$Audit = & $Python (Join-Path $PSScriptRoot 'legacy_blue_preflight.py')
if ($LASTEXITCODE -ne 0) { throw 'Read-only SQL identity preflight refused retirement.' }
$Audit | Write-Output
if (-not $Apply) {
    Write-Output 'PREVIEW ONLY: retire laptop MySQL80 and old web task; 8502 aliases 8503; archive data/binlog/relay/logs/my.ini; keep shared binaries and offsite backups.'
    return
}

# Archive only the exact owned legacy children, never the root or shared binaries.
$ArchiveParent = Join-Path $StageRoot 'retired'
if (-not (Test-Path -LiteralPath $ArchiveParent)) { New-Item -ItemType Directory -Path $ArchiveParent | Out-Null }
Assert-PhysicalPath $ArchiveParent
$Archive = Join-Path $ArchiveParent ('legacy-blue-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
if (Test-Path -LiteralPath $Archive) { throw 'Archive already exists.' }
New-Item -ItemType Directory -Path $Archive | Out-Null
Assert-PhysicalPath $Archive
Export-ScheduledTask -TaskName $LegacyTask | Set-Content -LiteralPath (Join-Path $Archive 'web-task.xml') -Encoding Unicode
$Service | Select-Object Name,PathName,StartMode,StartName | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Archive 'service.json') -Encoding UTF8
$Audit | Set-Content -LiteralPath (Join-Path $Archive 'preflight.json') -Encoding UTF8
$Journal = [ordered]@{computer=$env:COMPUTERNAME;state='started';archive=$Archive;compatibility_port=8502;new_port=8503;updated_at=(Get-Date).ToString('o')}
$Journal | ConvertTo-Json | Set-Content -LiteralPath $Marker -Encoding UTF8
Disable-ScheduledTask -TaskName $LegacyTask | Out-Null
Stop-ScheduledTask -TaskName $LegacyTask
$OldWebProcesses = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -in @('python.exe','pythonw.exe') -and
    $_.CommandLine -match $LegacyWebPattern
})
foreach ($Process in $OldWebProcesses) { Stop-Process -Id $Process.ProcessId -Force -ErrorAction SilentlyContinue }
for ($Attempt=0; $Attempt -lt 20; $Attempt++) {
    if (-not (Get-NetTCPConnection -LocalPort 8502 -State Listen -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 500
}
if (Get-NetTCPConnection -LocalPort 8502 -State Listen -ErrorAction SilentlyContinue) { throw 'Legacy web port did not stop.' }
netsh.exe interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=8502 connectaddress=127.0.0.1 connectport=8503 protocol=tcp
if ($LASTEXITCODE -ne 0) { throw 'Compatibility port creation failed; legacy data is still intact.' }
$AliasHealthy = $false
for ($Attempt=0; $Attempt -lt 15; $Attempt++) {
    try { Assert-NewWeb 8502; $AliasHealthy=$true; break } catch { Start-Sleep -Milliseconds 500 }
}
if (-not $AliasHealthy) { throw 'New alias is not healthy; legacy database has not been stopped.' }
Unregister-ScheduledTask -TaskName $LegacyTask -Confirm:$false
$Journal.state='web-replaced'
$Journal | ConvertTo-Json | Set-Content -LiteralPath $Marker -Encoding UTF8

# Recheck SQL immediately before stopping the old service. No green connection is used.
& $Python (Join-Path $PSScriptRoot 'legacy_blue_preflight.py')
if ($LASTEXITCODE -ne 0) { throw 'Final database identity preflight failed.' }
Stop-Service -Name MySQL80
for ($Attempt=0; $Attempt -lt 60; $Attempt++) {
    $Pending = @(Get-LegacyProcesses)
    $Listening = @(Get-NetTCPConnection -LocalPort 3306 -State Listen -ErrorAction SilentlyContinue)
    if ($Pending.Count -eq 0 -and $Listening.Count -eq 0) { break }
    Start-Sleep -Milliseconds 500
}
if ($Pending.Count -or $Listening.Count) { throw 'Legacy mysqld still owns files; archive refused.' }
Assert-NewWeb
sc.exe delete MySQL80
if ($LASTEXITCODE -ne 0) { throw 'Legacy service unregister failed; archive refused.' }
foreach ($Name in $ArchiveNames) {
    $Source = Join-Path $LegacyRoot $Name
    $Destination = Join-Path $Archive $Name
    Assert-PhysicalPath $Source
    Assert-PhysicalPath $Archive
    if ([IO.Path]::GetFullPath($Destination) -ine "$Archive\$Name" -or (Test-Path -LiteralPath $Destination)) {
        throw 'Archive destination is not an unused exact child.'
    }
    Move-Item -LiteralPath $Source -Destination $Destination
}
Assert-NewWeb
Assert-NewWeb 8502
if (-not (Test-Path -LiteralPath 'D:\TakealotMySQLReplica\mysql-8.0.46-winx64\bin\mysqld.exe')) { throw 'Shared binary missing.' }
$Journal.state='completed'
$Journal.updated_at=(Get-Date).ToString('o')
$Journal | ConvertTo-Json | Set-Content -LiteralPath $Marker -Encoding UTF8
$Journal | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Archive 'retirement.json') -Encoding UTF8
$Journal | ConvertTo-Json
