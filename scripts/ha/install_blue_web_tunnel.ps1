[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$BlueRoot = 'D:\TakealotBlue'
$BluePrincipal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $BluePrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Administrator required to register BLUE SYSTEM tunnel' }
& "$BlueRoot\run_blue_web_tunnel.ps1" -PreflightOnly
$BlueKeyAcl = [Security.AccessControl.FileSecurity]::new()
$BlueKeyAcl.SetAccessRuleProtection($true, $false)
$BlueAdmins = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
$BlueSystem = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$BlueKeyAcl.SetOwner($BlueAdmins)
$BlueKeyAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($BlueAdmins, 'FullControl', 'Allow'))
$BlueKeyAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($BlueSystem, 'Read', 'Allow'))
Set-Acl -LiteralPath "$BlueRoot\secrets\web-tunnel-ed25519" -AclObject $BlueKeyAcl
$BlueTaskName = 'Takealot Blue Web Tunnel'
$BlueExisting = Get-ScheduledTask -TaskName $BlueTaskName -ErrorAction SilentlyContinue
if ($null -ne $BlueExisting) { throw 'BLUE tunnel task already exists; inspect before replacement' }
$BlueAction = New-ScheduledTaskAction -Execute 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' -Argument '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\TakealotBlue\run_blue_web_tunnel.ps1' -WorkingDirectory $BlueRoot
$BlueTrigger = New-ScheduledTaskTrigger -AtStartup
$BlueSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$BlueIdentity = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $BlueTaskName -Action $BlueAction -Trigger $BlueTrigger -Settings $BlueSettings -Principal $BlueIdentity -Description 'BLUE web only: restricted reverse SSH to Tencent loopback; no database or crawler restart' | Out-Null
Start-ScheduledTask -TaskName $BlueTaskName
@{ installed = $true; task = $BlueTaskName; at = (Get-Date).ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath "$BlueRoot\state\web-tunnel-install.json" -Encoding UTF8
