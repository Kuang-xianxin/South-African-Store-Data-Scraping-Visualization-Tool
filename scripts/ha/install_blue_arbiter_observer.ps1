[CmdletBinding()]
param([switch]$Apply, [switch]$PrepareOnly)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$BlueRoot = 'D:\TakealotBlue'
$BluePython = Join-Path $BlueRoot 'runtime\Scripts\python.exe'
$BlueObserver = Join-Path $BlueRoot 'blue_arbiter_observer.py'
$BlueTaskName = 'Takealot Blue Arbiter Observer'
$BlueNode = Get-Content -LiteralPath (Join-Path $BlueRoot 'node.json') -Raw | ConvertFrom-Json
if ($BlueNode.computer -cne $env:COMPUTERNAME -or
    $env:COMPUTERNAME -cnotin @('DESKTOP-NTRMANG', 'LAPTOP-2T5MN8EU') -or
    $BlueNode.mysql_port -ne 3307 -or $BlueNode.auto_failover -or $BlueNode.formal_crawler_enabled) {
    throw 'Unexpected blue node or stage'
}
if (-not $Apply) {
    [pscustomobject]@{Apply=$false; Task=$BlueTaskName; Mode='observe'; MySqlChanges=$false} |
        ConvertTo-Json
    exit 0
}
$BlueFactsJson = & $BluePython $BlueObserver facts
if ($LASTEXITCODE -ne 0) { throw 'Blue identity audit failed' }
$BlueFacts = $BlueFactsJson | ConvertFrom-Json
if ($BlueFacts.read_only -ne 1 -or $BlueFacts.super_read_only -ne 1) {
    throw 'Observer installation requires an already read-only blue database'
}
$BlueSecretDir = Join-Path $BlueRoot 'secrets'
$BlueKeyPath = Join-Path $BlueSecretDir 'arbiter_ed25519'
if (-not (Test-Path -LiteralPath $BlueKeyPath)) {
    # Native empty arguments are not reliably forwarded by Windows PowerShell 5.1.
    $BlueKeyCode = 'import subprocess; subprocess.run(["ssh-keygen.exe","-q","-t","ed25519","-N","","-C","takealot-blue-arbiter","-f",r"D:\TakealotBlue\secrets\arbiter_ed25519"],check=True)'
    $BlueKeyCode | & $BluePython -
    if ($LASTEXITCODE -ne 0) { throw 'Blue key generation failed' }
}
if (-not (Test-Path -LiteralPath ($BlueKeyPath + '.pub'))) { throw 'Missing public key; refuse replacement' }
$BlueIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $BlueKeyPath /inheritance:r /grant:r "${BlueIdentity}:(F)" '*S-1-5-18:(F)' '*S-1-5-32-544:(F)' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Private-key ACL failed' }
# Pin the already verified cloud host key. No new trust-on-first-use or keyscan.
$BlueKnownHost = '119.91.117.232 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIG0glnmAXA0BzJ9OxTlLVSGnTJqJCi+CrlCtNDw/kbqF'
$BlueUtf8 = [Text.UTF8Encoding]::new($false)
[IO.File]::WriteAllText((Join-Path $BlueSecretDir 'arbiter_known_hosts'), $BlueKnownHost + "`n", $BlueUtf8)
$BlueArbiterHost = if ($BlueNode.node -eq 'laptop') { '100.72.100.10' } else { '119.91.117.232' }
$BlueObserverConfig = @{cluster='takealot-blue-3307-v1'; node=$BlueNode.node; mode='observe'; host=$BlueArbiterHost}
[IO.File]::WriteAllText((Join-Path $BlueRoot 'arbiter.json'), ($BlueObserverConfig | ConvertTo-Json), $BlueUtf8)
if ($PrepareOnly) {
    [pscustomobject]@{Node=$BlueNode.node; Prepared=$true; PublicKey=(Get-Content -LiteralPath ($BlueKeyPath + '.pub') -Raw).Trim(); Facts=$BlueFacts} | ConvertTo-Json -Depth 5
    exit 0
}
$BluePrincipal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $BluePrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator required only to register the blue startup observer'
}
# Windows OpenSSH rejects a SYSTEM key readable by an additional named user.
# Preserve recovery through Administrators; the running observer needs SYSTEM only.
& icacls.exe $BlueKeyPath /setowner '*S-1-5-32-544' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'SYSTEM observer key ownership failed' }
& icacls.exe $BlueKeyPath /remove:g $BlueIdentity | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'SYSTEM observer key ACL failed' }
$ExistingBlueTask = Get-ScheduledTask -TaskName $BlueTaskName -ErrorAction SilentlyContinue
if ($ExistingBlueTask) {
    if ($ExistingBlueTask.Actions.Execute -ne $BluePython -or
        $ExistingBlueTask.Actions.Arguments -ne 'D:\TakealotBlue\blue_arbiter_observer.py run') {
        throw 'Existing task is not the expected blue observer'
    }
    Stop-ScheduledTask -TaskName $BlueTaskName
    $BlueObservers = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -in @('python.exe', 'pythonw.exe') -and
        $_.CommandLine -match '(?i)D:\\TakealotBlue\\blue_arbiter_observer\.py[" ]+run(?:\s|$)'
    })
    foreach ($BlueObserverProcess in $BlueObservers) {
        Stop-Process -Id $BlueObserverProcess.ProcessId -Force -ErrorAction SilentlyContinue
    }
} else {
    $BlueAction = New-ScheduledTaskAction -Execute $BluePython -Argument 'D:\TakealotBlue\blue_arbiter_observer.py run' -WorkingDirectory $BlueRoot
    $BlueTrigger = New-ScheduledTaskTrigger -AtStartup
    $BlueTaskPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $BlueSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 12 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $BlueTaskName -Action $BlueAction -Trigger $BlueTrigger -Principal $BlueTaskPrincipal -Settings $BlueSettings | Out-Null
}
Start-ScheduledTask -TaskName $BlueTaskName
$BlueResult = @{status='installed'; task=$BlueTaskName; mode='observe'; node=$BlueNode.node; at=(Get-Date -Format o)}
[IO.File]::WriteAllText((Join-Path $BlueRoot 'state\arbiter-install.json'), ($BlueResult | ConvertTo-Json), $BlueUtf8)
$BlueResult | ConvertTo-Json
