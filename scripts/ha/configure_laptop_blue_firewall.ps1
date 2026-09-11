[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ExpectedComputerName = 'LAPTOP-2T5MN8EU'
$RuleName = 'Takealot Blue Read Only ERP 8502'
$BluePort = 8502
$AllowedRemoteAddresses = @(
    '100.70.103.11',
    '100.72.100.10',
    'LocalSubnet'
)

$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$Principal = [Security.Principal.WindowsPrincipal]::new($Identity)
if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator rights are required to configure the blue ERP firewall rule.'
}
if ($env:COMPUTERNAME -cne $ExpectedComputerName) {
    throw "Safety stop: expected $ExpectedComputerName, got $env:COMPUTERNAME."
}

$Existing = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
if ($null -eq $Existing) {
    New-NetFirewallRule `
        -DisplayName $RuleName `
        -Direction Inbound `
        -Action Allow `
        -Enabled True `
        -Profile Any `
        -Protocol TCP `
        -LocalPort $BluePort `
        -RemoteAddress $AllowedRemoteAddresses | Out-Null
}
else {
    $Existing | Set-NetFirewallRule `
        -Direction Inbound `
        -Action Allow `
        -Enabled True `
        -Profile Any | Out-Null
    $Existing | Get-NetFirewallPortFilter | Set-NetFirewallPortFilter `
        -Protocol TCP `
        -LocalPort $BluePort | Out-Null
    $Existing | Get-NetFirewallAddressFilter | Set-NetFirewallAddressFilter `
        -RemoteAddress $AllowedRemoteAddresses | Out-Null
}

$Rule = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction Stop
$PortFilter = $Rule | Get-NetFirewallPortFilter
$AddressFilter = $Rule | Get-NetFirewallAddressFilter
[pscustomobject]@{
    computer = $env:COMPUTERNAME
    display_name = $Rule.DisplayName
    enabled = [string]$Rule.Enabled
    direction = [string]$Rule.Direction
    action = [string]$Rule.Action
    protocol = [string]$PortFilter.Protocol
    local_port = [string]$PortFilter.LocalPort
    remote_address = @($AddressFilter.RemoteAddress)
} | ConvertTo-Json -Depth 4
