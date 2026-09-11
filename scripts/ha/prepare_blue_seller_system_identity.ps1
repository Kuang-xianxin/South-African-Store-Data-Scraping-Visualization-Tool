$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$blueRoot = 'D:\TakealotBlue'
$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Administrator is required to prepare the BLUE SYSTEM identity' }
$taskName = 'TakealotBlueSellerIdentityPrepare-' + [guid]::NewGuid().ToString('N')
$receipt = Join-Path $blueRoot 'state\seller-api\system-identity-public.json'
$probe = Join-Path $PSScriptRoot 'blue_seller_system_acceptance.py'
$manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ((Get-FileHash -LiteralPath $probe -Algorithm SHA256).Hash.ToLowerInvariant() -ne $manifest.identity_probe_sha256) { throw 'SYSTEM probe hash mismatch' }
$action = New-ScheduledTaskAction -Execute (Join-Path $blueRoot 'runtime\Scripts\python.exe') -Argument ('-X utf8 "' + $probe + '"') -WorkingDirectory $blueRoot
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -Hidden -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$registered = $false
try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings | Out-Null
    $registered = $true
    $started = Get-Date
    Start-ScheduledTask -TaskName $taskName
    do {
        Start-Sleep -Seconds 2
        $info = Get-ScheduledTaskInfo -TaskName $taskName
        $task = Get-ScheduledTask -TaskName $taskName
    } while (($info.LastRunTime -lt $started.AddSeconds(-2) -or $task.State -eq 'Running') -and (Get-Date) -lt $started.AddSeconds(110))
    if ($info.LastTaskResult -ne 0 -or $task.State -eq 'Running' -or -not (Test-Path -LiteralPath $receipt)) { throw 'SYSTEM identity preparation did not complete successfully' }
    $value = Get-Content -LiteralPath $receipt -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($value.sid -ne 'S-1-5-18' -or $value.scope -ne 'seller-only') { throw 'Unexpected task identity receipt' }
    $value | ConvertTo-Json -Compress
} finally {
    if ($registered) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false }
}
