[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Low')]
param(
    [int]$InterfaceIndex = 18,
    [string]$ExpectedAddress = '192.168.110.180',
    [string]$ExpectedGateway = '192.168.110.1',
    [string]$ResultPath = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Destination = '119.91.117.232/32'
$State = [ordered]@{ success = $false; destination = $Destination; changed = $false }

try {
    if ($env:COMPUTERNAME -cne 'DESKTOP-NTRMANG') {
        throw 'This route repair is restricted to the production desktop.'
    }
    $Network = Get-NetIPConfiguration -InterfaceIndex $InterfaceIndex
    if ($Network.NetAdapter.Status -ne 'Up' -or
        $ExpectedAddress -notin @($Network.IPv4Address.IPAddress) -or
        $ExpectedGateway -notin @($Network.IPv4DefaultGateway.NextHop)) {
        throw 'The physical interface address or gateway no longer matches the verified route.'
    }
    $Adapter = Get-NetAdapter | Where-Object { $_.ifIndex -eq $InterfaceIndex }
    if (-not $Adapter.HardwareInterface) {
        throw 'The selected interface is not a physical network adapter.'
    }
    $Existing = @(Get-NetRoute -AddressFamily IPv4 -DestinationPrefix $Destination -ErrorAction SilentlyContinue)
    if (@($Existing | Where-Object {
        $_.InterfaceIndex -ne $InterfaceIndex -or $_.NextHop -ne $ExpectedGateway
    }).Count -gt 0) {
        throw 'An existing route has a different destination interface or gateway; no changes made.'
    }
    if ($Existing.Count -eq 0 -and $PSCmdlet.ShouldProcess(
        "$Destination via $ExpectedGateway on interface $InterfaceIndex",
        'Add one persistent host route; leave default routes and services running'
    )) {
        $Principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
        if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
            throw 'Administrator approval is required to add this host route.'
        }
        & "$env:SystemRoot\System32\route.exe" -p ADD 119.91.117.232 MASK 255.255.255.255 $ExpectedGateway METRIC 1 IF $InterfaceIndex
        if ($LASTEXITCODE -ne 0) { throw "route.exe failed with exit code $LASTEXITCODE" }
        $State.changed = $true
    }
    $State.routes = @(Get-NetRoute -AddressFamily IPv4 -DestinationPrefix $Destination -ErrorAction SilentlyContinue |
        Select-Object DestinationPrefix, NextHop, InterfaceIndex, RouteMetric)
    if (-not $WhatIfPreference -and $State.routes.Count -eq 0) {
        throw 'The expected host route was not found after the operation.'
    }
    $State.success = $true
} catch {
    $State.error = $_.Exception.Message
} finally {
    $Json = $State | ConvertTo-Json -Depth 4
    if ($ResultPath) { [IO.File]::WriteAllText($ResultPath, $Json, [Text.UTF8Encoding]::new($false)) }
    Write-Output $Json
}
if (-not $State.success) { exit 1 }
