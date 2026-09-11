[CmdletBinding()]
param([switch]$Execute)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$TaskName = 'Takealot Green Public Tunnel'
$Runtime = 'C:\ProgramData\TakealotGreenTunnel'
$SourceKey = 'C:\Users\Mayn\.ssh\takealot_green_service_ed25519'
$SourceRunner = Join-Path $PSScriptRoot 'run_main_green_public_tunnel.ps1'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Evidence = Join-Path $ProjectRoot 'logs\public-green-stability-20260907'
$SourceKnownHosts = Join-Path $Evidence 'tunnel_known_hosts'
$Runner = Join-Path $Runtime 'run_main_green_public_tunnel.ps1'
if ($env:COMPUTERNAME -cne 'DESKTOP-NTRMANG') { throw 'Unexpected computer.' }
foreach ($f in @($SourceKey, $SourceRunner, $SourceKnownHosts)) {
    if (-not (Test-Path -LiteralPath $f -PathType Leaf)) { throw "Missing prerequisite: $f" }
}
$tokens = $null; $parseErrors = $null
[Management.Automation.Language.Parser]::ParseFile($SourceRunner, [ref]$tokens, [ref]$parseErrors) | Out-Null
if ($parseErrors.Count) { throw $parseErrors[0].Message }
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) { throw 'Task already exists; inspect before replacing.' }
$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$IsAdmin = ([Security.Principal.WindowsPrincipal]::new($Identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $Execute) {
    [pscustomobject]@{ status = 'ready'; task = $TaskName; administrator = $IsAdmin; runtime = $Runtime; remote_bind = '127.0.0.1:18503' } | ConvertTo-Json
    exit 0
}
if (-not $IsAdmin) { throw 'Windows administrator elevation is required to register SYSTEM startup.' }
if (Test-Path -LiteralPath $Runtime) { throw 'Runtime already exists; inspect before replacing.' }
New-Item -ItemType Directory -Path $Runtime | Out-Null
$Acl = [Security.AccessControl.DirectorySecurity]::new()
$System = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$Admins = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
$Users = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-545')
$Acl.SetOwner($System); $Acl.SetAccessRuleProtection($true, $false)
foreach ($Sid in @($System, $Admins)) {
    $Acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($Sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
}
$Acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($Users, 'ReadAndExecute', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
Set-Acl -LiteralPath $Runtime -AclObject $Acl
New-Item -ItemType Directory -Path (Join-Path $Runtime 'state'), (Join-Path $Runtime 'secrets') | Out-Null
$SecretAcl = [Security.AccessControl.DirectorySecurity]::new()
$SecretAcl.SetOwner($System); $SecretAcl.SetAccessRuleProtection($true, $false)
foreach ($Sid in @($System, $Admins)) {
    $SecretAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($Sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
}
Set-Acl -LiteralPath (Join-Path $Runtime 'secrets') -AclObject $SecretAcl
Copy-Item -LiteralPath $SourceRunner -Destination $Runner
Copy-Item -LiteralPath $SourceKnownHosts -Destination (Join-Path $Runtime 'known_hosts')
$InstalledKey = Join-Path $Runtime 'secrets\id_ed25519'
Copy-Item -LiteralPath $SourceKey -Destination $InstalledKey
$KeyAcl = [Security.AccessControl.FileSecurity]::new()
$KeyAcl.SetOwner($System); $KeyAcl.SetAccessRuleProtection($true, $false)
foreach ($Sid in @($System, $Admins)) {
    $KeyAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($Sid, 'FullControl', 'Allow'))
}
Set-Acl -LiteralPath $InstalledKey -AclObject $KeyAcl
$Action = New-ScheduledTaskAction -Execute 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $Runner + '" -ServiceMode')
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description 'Production green loopback SSH tunnel; starts at boot without user logon.' | Out-Null
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 4
$Task = Get-ScheduledTask -TaskName $TaskName
$Result = [pscustomobject]@{ status = 'installed'; task = $TaskName; state = [string]$Task.State; run_as = $Task.Principal.UserId; logon_type = [string]$Task.Principal.LogonType; startup = 'AtStartup'; runtime = $Runtime }
$Result | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Evidence 'service-install.json') -Encoding UTF8
$Result | ConvertTo-Json
