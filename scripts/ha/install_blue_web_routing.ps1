[CmdletBinding()]
param([switch]$Apply)
$ErrorActionPreference='Stop'
$BlueRoot='D:\TakealotBlue'
$BlueCfg=Get-Content -LiteralPath "$BlueRoot\node.json" -Raw -Encoding UTF8 | ConvertFrom-Json
if($BlueCfg.computer -cne $env:COMPUTERNAME -or $BlueCfg.mysql_port -ne 3307){throw 'Unexpected BLUE node'}
$BlueTaskName='Takealot Blue Stage Web'
$BluePrincipal=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if($Apply -and -not $BluePrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'Administrator required for the BLUE web SYSTEM task'}
$BlueTask=Get-ScheduledTask -TaskName $BlueTaskName
if($BlueTask.Actions.Arguments -cne '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\TakealotBlue\run_blue_web.ps1'){throw 'Unexpected BLUE web task action'}
$BluePython="$BlueRoot\runtime\Scripts\python.exe"
$BlueInstaller="$BlueRoot\staging\apply_blue_web_routing.py"
& $BluePython $BlueInstaller
if($LASTEXITCODE -ne 0){throw 'BLUE routing preflight failed'}
if(-not $Apply){return}
foreach($BlueStateFile in @('search-ranking-batch.json','competitor-scheduled-batch.json')){
 $BlueStatePath=Join-Path "$BlueRoot\app\logs" $BlueStateFile
 if(Test-Path -LiteralPath $BlueStatePath){
  $BlueState=Get-Content -LiteralPath $BlueStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
  if($BlueState.status -in @('running','stopping') -or $BlueState.run_status -in @('running','retry_wait')){throw 'A BLUE web task is active; preserve it'}
 }
}
Stop-ScheduledTask -TaskName $BlueTaskName
$BlueWebProcesses=@(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {$_.CommandLine -match '(?:^|[\s"])D:\\TakealotBlue\\blue_web\.py(?:[\s"]|$)'})
foreach($BlueProcess in $BlueWebProcesses){Stop-Process -Id $BlueProcess.ProcessId -Force -ErrorAction SilentlyContinue}
$BlueDeadline=(Get-Date).AddSeconds(20)
do{
 $BlueListening=@(Get-NetTCPConnection -State Listen -LocalPort 8503 -ErrorAction SilentlyContinue | Where-Object {$_.LocalAddress -in @('127.0.0.1','0.0.0.0','::1','::')})
 if(-not $BlueListening){break}
 Start-Sleep -Milliseconds 250
}while((Get-Date) -lt $BlueDeadline)
if($BlueListening){throw 'BLUE web did not stop; files preserved'}
try{
 & $BluePython $BlueInstaller --apply
 if($LASTEXITCODE -ne 0){throw 'BLUE web overlay failed'}
 Start-ScheduledTask -TaskName $BlueTaskName
 $BlueReport=@{status='started';node=$BlueCfg.node;at=(Get-Date).ToString('o');crawler_restarted=$false}
 $BlueReport | ConvertTo-Json | Set-Content -LiteralPath "$BlueRoot\state\web-routing-install.json" -Encoding UTF8
}catch{
 Start-ScheduledTask -TaskName $BlueTaskName
 throw
}
