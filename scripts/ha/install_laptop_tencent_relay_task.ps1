[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [switch]$Execute
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ExpectedComputerName = 'LAPTOP-2T5MN8EU'
$TaskName = 'Takealot Tencent Relay'
$RelayRoot = 'D:\TakealotRelay'
$RunnerPath = Join-Path $RelayRoot 'run_laptop_tencent_relay.ps1'
$KeyPath = Join-Path $RelayRoot 'secrets\laptop-tencent-relay-ed25519'
$KnownHostsPath = Join-Path $RelayRoot 'known_hosts'

function Test-IsAdministrator {
    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $Principal = [Security.Principal.WindowsPrincipal]::new($Identity)
    return $Principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

if ($env:COMPUTERNAME -cne $ExpectedComputerName) {
    throw "Safety stop: current computer is $env:COMPUTERNAME, expected $ExpectedComputerName."
}
$IsAdministrator = Test-IsAdministrator
if ($Execute -and -not $IsAdministrator) {
    throw 'Safety stop: an elevated administrator token is required.'
}
foreach ($RequiredPath in @($RunnerPath, $KeyPath, $KnownHostsPath)) {
    if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
        throw "Required relay file not found: $RequiredPath"
    }
}

if ($Execute) {
    $SystemSid = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $AdministratorsSid = [Security.Principal.SecurityIdentifier]::new(
        'S-1-5-32-544'
    )
    $KeyAcl = [Security.AccessControl.FileSecurity]::new()
    $KeyAcl.SetOwner($SystemSid)
    $KeyAcl.SetAccessRuleProtection($true, $false)
    foreach ($Sid in @($SystemSid, $AdministratorsSid)) {
        $KeyAcl.AddAccessRule(
            [Security.AccessControl.FileSystemAccessRule]::new(
                $Sid,
                [Security.AccessControl.FileSystemRights]::FullControl,
                [Security.AccessControl.AccessControlType]::Allow
            )
        )
    }
    Set-Acl -LiteralPath $KeyPath -AclObject $KeyAcl
    $VerifiedKeyAcl = Get-Acl -LiteralPath $KeyPath
    $AllowedSids = @(
        $VerifiedKeyAcl.Access |
            Where-Object AccessControlType -eq Allow |
            ForEach-Object { $_.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]
            ).Value }
    )
    if (
        $VerifiedKeyAcl.Owner -cne 'NT AUTHORITY\SYSTEM' -or
        -not $VerifiedKeyAcl.AreAccessRulesProtected -or
        $AllowedSids.Count -ne 2 -or
        $AllowedSids -notcontains 'S-1-5-18' -or
        $AllowedSids -notcontains 'S-1-5-32-544'
    ) {
        throw 'Relay private-key ACL did not converge to SYSTEM and Administrators only.'
    }
}

$Tokens = $null
$ParseErrors = $null
[Management.Automation.Language.Parser]::ParseFile(
    $RunnerPath,
    [ref]$Tokens,
    [ref]$ParseErrors
) | Out-Null
if (@($ParseErrors).Count -gt 0) {
    throw "Relay runner has PowerShell parser errors: $($ParseErrors[0].Message)"
}

$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$Preflight = [ordered]@{
    computer = $env:COMPUTERNAME
    execute = [bool]$Execute
    is_administrator = $IsAdministrator
    task_name = $TaskName
    task_exists = $null -ne $ExistingTask
    runner = $RunnerPath
    key = $KeyPath
    known_hosts = $KnownHostsPath
}
if (-not $Execute) {
    $Preflight | ConvertTo-Json -Depth 4
    exit 0
}
if ($null -ne $ExistingTask) {
    throw "Safety stop: scheduled task already exists: $TaskName"
}

$PowerShellArguments = (
    '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass ' +
    '-File "' + $RunnerPath + '"'
)
$Action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument $PowerShellArguments
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Principal = New-ScheduledTaskPrincipal `
    -UserId 'SYSTEM' `
    -LogonType ServiceAccount `
    -RunLevel Highest
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable

$Task = New-ScheduledTask `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings `
    -Description 'Keep the laptop SSH, MySQL, and Clash relay bound only to Tencent loopback.'
Register-ScheduledTask -TaskName $TaskName -InputObject $Task | Out-Null
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3
$Installed = Get-ScheduledTask -TaskName $TaskName
$Info = Get-ScheduledTaskInfo -TaskName $TaskName

[pscustomobject]@{
    computer = $env:COMPUTERNAME
    status = 'installed'
    task_name = $TaskName
    task_state = [string]$Installed.State
    last_task_result = [int]$Info.LastTaskResult
    run_as = [string]$Installed.Principal.UserId
    trigger = 'AtStartup'
} | ConvertTo-Json -Depth 4
