[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [ValidateRange(1, 60)]
    [int]$ReconnectDelaySeconds = 3
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ExpectedComputerName = 'LAPTOP-2T5MN8EU'
$RelayRoot = 'D:\TakealotRelay'
$KeyPath = Join-Path $RelayRoot 'secrets\laptop-tencent-relay-ed25519'
$KnownHostsPath = Join-Path $RelayRoot 'known_hosts'
$LogDirectory = Join-Path $RelayRoot 'logs'
$LogPath = Join-Path $LogDirectory 'laptop-tencent-relay.log'
$SshPath = 'C:\Windows\System32\OpenSSH\ssh.exe'
$RemoteHost = '119.91.117.232'
$RemoteUser = 'takealot-relay'

if ($env:COMPUTERNAME -cne $ExpectedComputerName) {
    throw "Safety stop: current computer is $env:COMPUTERNAME, expected $ExpectedComputerName."
}
foreach ($RequiredPath in @($SshPath, $KeyPath, $KnownHostsPath)) {
    if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
        throw "Required relay file not found: $RequiredPath"
    }
}

New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
$CreatedNew = $false
$Mutex = [Threading.Mutex]::new(
    $true,
    'Global\TakealotLaptopTencentRelay',
    [ref]$CreatedNew
)
if (-not $CreatedNew) {
    $Mutex.Dispose()
    exit 0
}

function Write-RelayLog {
    param([Parameter(Mandatory = $true)][string]$Message)

    $Timestamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz')
    Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value "[$Timestamp] $Message"
}

$SshArguments = @(
    '-N',
    '-T',
    '-i', $KeyPath,
    '-o', 'BatchMode=yes',
    '-o', 'ConnectionAttempts=1',
    '-o', 'ConnectTimeout=10',
    '-o', 'ExitOnForwardFailure=yes',
    '-o', 'HostKeyAlgorithms=ssh-ed25519',
    '-o', 'ServerAliveInterval=10',
    '-o', 'ServerAliveCountMax=3',
    '-o', 'StrictHostKeyChecking=yes',
    '-o', "UserKnownHostsFile=$KnownHostsPath",
    '-R', '127.0.0.1:22022:127.0.0.1:22',
    '-R', '127.0.0.1:23306:127.0.0.1:3306',
    '-R', '127.0.0.1:27897:127.0.0.1:7897',
    "$RemoteUser@$RemoteHost"
)

try {
    while ($true) {
        Write-RelayLog -Message 'Opening restricted reverse tunnel to Tencent.'
        $PreviousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $Output = @(& $SshPath @SshArguments 2>&1)
            $ExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $PreviousErrorActionPreference
        }
        foreach ($Line in $Output) {
            $Text = ([string]$Line -replace '[\r\n]+', ' ').Trim()
            if ($Text) {
                Write-RelayLog -Message "ssh: $Text"
            }
        }
        Write-RelayLog -Message "Tunnel exited with code $ExitCode; retrying in $ReconnectDelaySeconds seconds."
        Start-Sleep -Seconds $ReconnectDelaySeconds
    }
}
finally {
    $Mutex.ReleaseMutex()
    $Mutex.Dispose()
}
