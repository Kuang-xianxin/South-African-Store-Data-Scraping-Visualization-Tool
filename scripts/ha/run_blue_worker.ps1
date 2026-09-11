[CmdletBinding()]
param()
$ErrorActionPreference='Stop'
$BlueRoot='D:\TakealotBlue'
$BlueProcess=Start-Process -FilePath "$BlueRoot\runtime\Scripts\python.exe" `
    -ArgumentList @('D:\TakealotBlue\blue_worker.py') -WorkingDirectory "$BlueRoot\app" `
    -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput "$BlueRoot\logs\blue-worker-out.log" `
    -RedirectStandardError "$BlueRoot\logs\blue-worker-error.log"
exit $BlueProcess.ExitCode
