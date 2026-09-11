[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [switch]$Execute
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ExpectedComputerName = 'DESKTOP-NTRMANG'
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$RunnerPath = Join-Path $ProjectRoot 'scripts\ha\run_main_tencent_relay.ps1'
$KeyPath = Join-Path $env:USERPROFILE '.ssh\takealot_tencent_relay_main_ed25519'
$RunKeyPath = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$RunValueName = 'TakealotTencentRelay'
$PowerShellArguments = (
    '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass ' +
    '-File "' + $RunnerPath + '"'
)
$RunCommand = 'powershell.exe ' + $PowerShellArguments

if ($env:COMPUTERNAME -cne $ExpectedComputerName) {
    throw "Safety stop: current computer is $env:COMPUTERNAME, expected $ExpectedComputerName."
}
foreach ($RequiredPath in @($RunnerPath, $KeyPath)) {
    if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
        throw "Required relay file not found: $RequiredPath"
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

$ExistingRunValue = try {
    Get-ItemPropertyValue `
        -Path $RunKeyPath `
        -Name $RunValueName `
        -ErrorAction Stop
}
catch [Management.Automation.PSArgumentException] {
    $null
}
$ExistingProcesses = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.Name -eq 'powershell.exe' -and
            $_.CommandLine -like '*run_main_tencent_relay.ps1*'
        }
)
$OccupiedPorts = @(
    Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object LocalPort -in @(2222, 13307, 17890)
)

$Preflight = [ordered]@{
    computer = $env:COMPUTERNAME
    execute = [bool]$Execute
    startup_value_exists = $null -ne $ExistingRunValue
    startup_value_matches = $ExistingRunValue -ceq $RunCommand
    running_processes = $ExistingProcesses.Count
    occupied_ports = @($OccupiedPorts | ForEach-Object {
        "$($_.LocalAddress):$($_.LocalPort) pid=$($_.OwningProcess)"
    })
    runner = $RunnerPath
    key = $KeyPath
}
if (-not $Execute) {
    $Preflight | ConvertTo-Json -Depth 5
    exit 0
}
if ($null -ne $ExistingRunValue -and $ExistingRunValue -cne $RunCommand) {
    throw "Safety stop: HKCU startup value already has unexpected content: $RunValueName"
}
if ($ExistingProcesses.Count -eq 0 -and $OccupiedPorts.Count -gt 0) {
    throw 'Safety stop: one or more relay ports are owned by another process.'
}

New-Item -Path $RunKeyPath -Force | Out-Null
Set-ItemProperty -Path $RunKeyPath -Name $RunValueName -Value $RunCommand
if ($ExistingProcesses.Count -eq 0) {
    Start-Process `
        -FilePath 'powershell.exe' `
        -ArgumentList $PowerShellArguments `
        -WindowStyle Hidden | Out-Null
}
Start-Sleep -Seconds 3

$RelayListeners = @(
    Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object {
            $_.LocalAddress -eq '127.0.0.1' -and
            $_.LocalPort -in @(2222, 13307, 17890)
        }
)
$RelayPorts = @(
    $RelayListeners |
        ForEach-Object { [int]$_.LocalPort } |
        Sort-Object -Unique
)
if ($RelayPorts.Count -ne 3) {
    throw 'Main Tencent relay did not establish all three loopback listeners.'
}

[pscustomobject]@{
    computer = $env:COMPUTERNAME
    status = 'installed'
    startup = 'HKCU Run at user logon'
    startup_value = $RunValueName
    listeners = @($RelayListeners | Sort-Object LocalPort | ForEach-Object {
        "$($_.LocalAddress):$($_.LocalPort)"
    })
} | ConvertTo-Json -Depth 4
