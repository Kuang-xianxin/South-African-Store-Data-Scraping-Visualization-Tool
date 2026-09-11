[CmdletBinding()]
param([switch]$PreflightOnly, [switch]$ServiceMode)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$SshPath = 'C:\Windows\System32\OpenSSH\ssh.exe'
$KeyPath = Join-Path $env:USERPROFILE '.ssh\takealot_tencent_witness_ed25519'
$KnownHostsPath = Join-Path $env:USERPROFILE '.ssh\known_hosts'
$StateRoot = Join-Path $env:LOCALAPPDATA 'TakealotGreenPublicTunnel'
$Forward = '127.0.0.1:18502:127.0.0.1:8501'
$RemoteUser = 'ubuntu'
$RemoteBind = '127.0.0.1:18502'
$MutexName = 'Local\TakealotGreenPublicTunnel'
if ($ServiceMode) {
    $StateRoot = 'C:\ProgramData\TakealotGreenTunnel\state'
    $KeyPath = 'C:\ProgramData\TakealotGreenTunnel\secrets\id_ed25519'
    $KnownHostsPath = 'C:\ProgramData\TakealotGreenTunnel\known_hosts'
    $Forward = '127.0.0.1:18503:127.0.0.1:8501'
    $RemoteUser = 'takealot-green'
    $RemoteBind = '127.0.0.1:18503'
    $MutexName = 'Global\TakealotGreenPublicTunnelService'
}

if ($env:COMPUTERNAME -cne 'DESKTOP-NTRMANG') {
    throw 'This tunnel is restricted to the production main computer.'
}
foreach ($RequiredFile in @($SshPath, $KeyPath, $KnownHostsPath)) {
    if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
        throw "Required file missing: $RequiredFile"
    }
}
$Route = @(Get-NetRoute -DestinationPrefix '119.91.117.232/32' -ErrorAction SilentlyContinue |
    Where-Object { $_.InterfaceIndex -eq 18 -and $_.NextHop -eq '192.168.110.1' })
if ($Route.Count -eq 0 -and ($PreflightOnly -or -not $ServiceMode)) {
    throw 'The verified physical route to Tencent is missing; run the route preflight first.'
}
if ($PreflightOnly) {
    [pscustomobject]@{ status = 'ready'; forward = $Forward; destination = '119.91.117.232:22' } | ConvertTo-Json
    exit 0
}

New-Item -ItemType Directory -Path $StateRoot -Force | Out-Null
$NewMutex = $false
$Mutex = [Threading.Mutex]::new($true, $MutexName, [ref]$NewMutex)
if (-not $NewMutex) { $Mutex.Dispose(); exit 0 }
$Child = $null
$StopFile = Join-Path $StateRoot 'stop'
$StateFile = Join-Path $StateRoot 'status.json'
$LogFile = Join-Path $StateRoot 'ssh.stderr.log'
$SshArguments = @(
    '-F', 'NUL', '-4', '-N', '-T', '-n', '-b', '192.168.110.180',
    '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
    '-o', 'IdentitiesOnly=yes', '-o', 'ExitOnForwardFailure=yes',
    '-o', 'ConnectionAttempts=1', '-o', 'ConnectTimeout=8',
    '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=3',
    '-o', 'LogLevel=ERROR', '-o', ('UserKnownHostsFile="' + $KnownHostsPath + '"'),
    '-i', ('"' + $KeyPath + '"'), '-R', $Forward, "$RemoteUser@119.91.117.232"
)
function Write-TunnelState([string]$Phase, [int]$ChildId = 0, [int]$ExitCode = 0) {
    [pscustomobject]@{
        observed_at = (Get-Date).ToString('o'); phase = $Phase
        supervisor_pid = $PID; ssh_pid = $ChildId; exit_code = $ExitCode
        remote_bind = $RemoteBind; local_target = '127.0.0.1:8501'
    } | ConvertTo-Json | Set-Content -LiteralPath $StateFile -Encoding UTF8
}
try {
    while (-not (Test-Path -LiteralPath $StopFile)) {
        if ($ServiceMode) {
            $ReadyAddress = @(Get-NetIPAddress -InterfaceIndex 18 -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                Where-Object { $_.IPAddress -eq '192.168.110.180' -and $_.AddressState -eq 'Preferred' })
            $ReadyRoute = @(Get-NetRoute -DestinationPrefix '119.91.117.232/32' -ErrorAction SilentlyContinue |
                Where-Object { $_.InterfaceIndex -eq 18 -and $_.NextHop -eq '192.168.110.1' })
            if ($ReadyAddress.Count -eq 0 -or $ReadyRoute.Count -eq 0) {
                Write-TunnelState 'waiting_network'
                Start-Sleep -Seconds 10
                continue
            }
        }
        Write-TunnelState 'connecting'
        $Child = Start-Process -FilePath $SshPath -ArgumentList $SshArguments `
            -WindowStyle Hidden -PassThru -RedirectStandardError $LogFile
        while (-not $Child.WaitForExit(2000)) {
            # A live SSH process is not an end-to-end ERP health assertion.
            Write-TunnelState 'ssh_running' $Child.Id
            if (Test-Path -LiteralPath $StopFile) { break }
        }
        if (-not $Child.HasExited) { $Child.Kill(); $Child.WaitForExit() }
        Write-TunnelState 'disconnected' $Child.Id $Child.ExitCode
        $Child.Dispose()
        $Child = $null
        if (-not (Test-Path -LiteralPath $StopFile)) { Start-Sleep -Seconds 3 }
    }
}
finally {
    if ($null -ne $Child) {
        if (-not $Child.HasExited) { $Child.Kill(); $Child.WaitForExit() }
        $Child.Dispose()
    }
    Write-TunnelState 'stopped'
    $Mutex.ReleaseMutex()
    $Mutex.Dispose()
}
