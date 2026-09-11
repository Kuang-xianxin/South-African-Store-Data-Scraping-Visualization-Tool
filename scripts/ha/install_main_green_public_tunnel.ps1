[CmdletBinding(SupportsShouldProcess = $true)]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Runner = Join-Path $PSScriptRoot 'run_main_green_public_tunnel.ps1'
$StartupKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$StartupName = 'TakealotGreenPublicTunnel'
$Arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $Runner + '"'
$Command = 'powershell.exe ' + $Arguments
$StateRoot = Join-Path $env:LOCALAPPDATA 'TakealotGreenPublicTunnel'

# Run preflight in a child shell because it exits on success.
& powershell.exe -NoProfile -NonInteractive -File $Runner -PreflightOnly
if ($LASTEXITCODE -ne 0) { throw 'Tunnel preflight failed.' }
if (Test-Path -LiteralPath (Join-Path $StateRoot 'stop')) {
    throw 'Tunnel is explicitly stopped; inspect the stop marker before reinstalling.'
}
$Existing = Get-ItemProperty -LiteralPath $StartupKey -Name $StartupName -ErrorAction SilentlyContinue
if ($null -ne $Existing -and $Existing.$StartupName -cne $Command) {
    throw 'A different startup entry already uses this name.'
}
if ($PSCmdlet.ShouldProcess('Main green public tunnel', 'Install user-logon startup and start reconnecting SSH supervisor')) {
    New-ItemProperty -Path $StartupKey -Name $StartupName -Value $Command -PropertyType String -Force | Out-Null
    Start-Process -FilePath 'powershell.exe' -ArgumentList $Arguments -WindowStyle Hidden | Out-Null
    [pscustomobject]@{ status = 'started'; startup = 'current user logon'; name = $StartupName } | ConvertTo-Json
}
