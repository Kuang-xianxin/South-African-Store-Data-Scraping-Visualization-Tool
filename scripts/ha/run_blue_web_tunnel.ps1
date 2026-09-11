[CmdletBinding()]
param([switch]$PreflightOnly)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$BlueRoot = 'D:\TakealotBlue'
$BlueNode = Get-Content -LiteralPath "$BlueRoot\node.json" -Raw -Encoding UTF8 | ConvertFrom-Json
$BluePorts = @{ main = 18505; laptop = 18506 }
if ($BlueNode.computer -cne $env:COMPUTERNAME -or $BlueNode.mysql_port -ne 3307 -or -not $BluePorts.ContainsKey($BlueNode.node)) {
    throw 'Unexpected BLUE node identity'
}
$BlueKey = "$BlueRoot\secrets\web-tunnel-ed25519"
$BlueKnownHosts = "$BlueRoot\secrets\web-tunnel-known-hosts"
$BlueSsh = 'C:\Windows\System32\OpenSSH\ssh.exe'
foreach ($BlueFile in @($BlueKey, $BlueKnownHosts, $BlueSsh)) {
    if (-not (Test-Path -LiteralPath $BlueFile -PathType Leaf)) { throw "Missing BLUE tunnel file: $BlueFile" }
}
$BlueBind = "127.0.0.1:$($BluePorts[$BlueNode.node])"
$BlueRemoteHosts = @('119.91.117.232', '100.72.100.10')
$BlueRemoteIndex = 0
if ($PreflightOnly) {
    @{ node = $BlueNode.node; remote_bind = $BlueBind; target = '127.0.0.1:8503'; remote_hosts = $BlueRemoteHosts } | ConvertTo-Json -Compress
    return
}
$BlueCreated = $false
$BlueMutex = [Threading.Mutex]::new($true, 'Global\TakealotBlueWebTunnel', [ref]$BlueCreated)
if (-not $BlueCreated) { $BlueMutex.Dispose(); exit 0 }
$BlueState = "$BlueRoot\state\web-tunnel.json"
$BlueStop = "$BlueRoot\state\web-tunnel.stop"
$BlueError = "$BlueRoot\logs\web-tunnel-error.log"
$BlueChild = $null
$BlueArguments = @(
    '-F', 'NUL', '-4', '-N', '-T', '-n',
    '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
    '-o', 'HostKeyAlgorithms=ssh-ed25519', '-o', 'ExitOnForwardFailure=yes',
    '-o', 'HostKeyAlias=119.91.117.232',
    '-o', 'ConnectionAttempts=1', '-o', 'ConnectTimeout=8',
    '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=3', '-o', 'LogLevel=ERROR',
    '-o', "UserKnownHostsFile=$BlueKnownHosts", '-i', $BlueKey,
    '-R', "${BlueBind}:127.0.0.1:8503"
)
function Write-BlueTunnelState([string]$Phase, [int]$ChildId = 0) {
    @{ at = (Get-Date).ToString('o'); node = $BlueNode.node; phase = $Phase;
       supervisor_pid = $PID; ssh_pid = $ChildId; remote_bind = $BlueBind; target = '127.0.0.1:8503';
       remote_host = $BlueRemoteHosts[$BlueRemoteIndex] } |
       ConvertTo-Json | Set-Content -LiteralPath $BlueState -Encoding UTF8
}
try {
    while (-not (Test-Path -LiteralPath $BlueStop)) {
        Write-BlueTunnelState 'connecting'
        $BlueDestination = 'takealot-relay@' + $BlueRemoteHosts[$BlueRemoteIndex]
        $BlueChild = Start-Process -FilePath $BlueSsh -ArgumentList @($BlueArguments + $BlueDestination) -WindowStyle Hidden -PassThru -RedirectStandardError $BlueError
        while (-not $BlueChild.WaitForExit(2000)) {
            Write-BlueTunnelState 'ssh_running' $BlueChild.Id
            if (Test-Path -LiteralPath $BlueStop) { break }
        }
        if (-not $BlueChild.HasExited) { $BlueChild.Kill(); $BlueChild.WaitForExit() }
        Write-BlueTunnelState 'disconnected'
        $BlueChild.Dispose()
        $BlueChild = $null
        # Reconnect the same authenticated loopback forward through the other
        # path. Never move an established tunnel or alter ERP/crawler processes.
        $BlueRemoteIndex = ($BlueRemoteIndex + 1) % $BlueRemoteHosts.Count
        if (-not (Test-Path -LiteralPath $BlueStop)) { Start-Sleep -Seconds 3 }
    }
} finally {
    if ($null -ne $BlueChild) {
        if (-not $BlueChild.HasExited) { $BlueChild.Kill(); $BlueChild.WaitForExit() }
        $BlueChild.Dispose()
    }
    Write-BlueTunnelState 'stopped'
    $BlueMutex.ReleaseMutex()
    $BlueMutex.Dispose()
}
