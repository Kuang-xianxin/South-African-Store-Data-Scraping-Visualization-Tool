[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$BlueRoot = 'D:\TakealotBlue'
$BlueConfig = Get-Content -LiteralPath (Join-Path $BlueRoot 'node.json') -Raw | ConvertFrom-Json
if ($env:COMPUTERNAME -cne $BlueConfig.computer) { throw 'Wrong blue node' }
for ($BlueWait=0; $BlueWait -lt 30; $BlueWait++) {
    if ((Get-Service -Name 'TakealotBlueMySQL').Status -eq 'Running') { break }
    Start-Sleep -Seconds 1
}
$BlueProcess = Start-Process -FilePath (Join-Path $BlueRoot 'runtime\Scripts\python.exe') `
    -ArgumentList @('D:\TakealotBlue\blue_web.py') -WorkingDirectory (Join-Path $BlueRoot 'app') `
    -WindowStyle Hidden -Wait -PassThru `
    -RedirectStandardOutput (Join-Path $BlueRoot 'logs\blue-web-out.log') `
    -RedirectStandardError (Join-Path $BlueRoot 'logs\blue-web-error.log')
exit $BlueProcess.ExitCode
