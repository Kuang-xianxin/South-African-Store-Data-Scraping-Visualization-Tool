param(
    [Parameter(Mandatory = $false)]
    [ValidateSet('127.0.0.1')]
    [string]$ListenAddress = '127.0.0.1',

    [Parameter(Mandatory = $false)]
    [ValidateRange(1024, 65535)]
    [int]$ListenPort = 17890,

    [Parameter(Mandatory = $false)]
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$SshHost = 'takealot-backup-laptop',

    [Parameter(Mandatory = $false)]
    [ValidateSet('127.0.0.1')]
    [string]$RemoteProxyAddress = '127.0.0.1',

    [Parameter(Mandatory = $false)]
    [ValidateRange(1024, 65535)]
    [int]$RemoteProxyPort = 7897,

    [Parameter(Mandatory = $false)]
    [ValidateRange(5, 3600)]
    [int]$RetrySeconds = 30
)

$ErrorActionPreference = 'Stop'
$StateRoot = Join-Path $env:LOCALAPPDATA 'TakealotCompetitorEgress'
$LogPath = Join-Path $StateRoot 'tunnel.log'
$PreviousLogPath = Join-Path $StateRoot 'tunnel.previous.log'
$MaxLogBytes = 1MB

New-Item -ItemType Directory -Path $StateRoot -Force | Out-Null

function Write-TunnelLog {
    param([Parameter(Mandatory = $true)][string]$Message)

    if (
        (Test-Path -LiteralPath $LogPath -PathType Leaf) -and
        (Get-Item -LiteralPath $LogPath).Length -ge $MaxLogBytes
    ) {
        Move-Item -LiteralPath $LogPath -Destination $PreviousLogPath -Force
    }
    $Line = "[$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz'))] $Message"
    Add-Content -LiteralPath $LogPath -Value $Line -Encoding UTF8
    Write-Host $Line
}

$CreatedNew = $false
$Mutex = [System.Threading.Mutex]::new(
    $true,
    'Local\TakealotCompetitorEgressTunnel',
    [ref]$CreatedNew
)
if (-not $CreatedNew) {
    $Mutex.Dispose()
    Write-TunnelLog -Message 'Another tunnel watchdog is already running; this run is skipped.'
    exit 0
}

try {
    $SshCommand = Get-Command 'ssh.exe' -ErrorAction SilentlyContinue
    if ($null -eq $SshCommand) {
        throw 'Windows OpenSSH client ssh.exe is not installed or not on PATH.'
    }
    $LocalEndpoint = "${ListenAddress}:$ListenPort"
    $RemoteProxyEndpoint = "${RemoteProxyAddress}:$RemoteProxyPort"
    $ForwardSpec = "${LocalEndpoint}:${RemoteProxyEndpoint}"
    $Arguments = @(
        '-N',
        '-T',
        '-L', $ForwardSpec,
        '-o', 'BatchMode=yes',
        '-o', 'ConnectTimeout=15',
        '-o', 'ExitOnForwardFailure=yes',
        '-o', 'ServerAliveInterval=30',
        '-o', 'ServerAliveCountMax=3',
        '-o', 'StrictHostKeyChecking=yes',
        $SshHost
    )

    while ($true) {
        Write-TunnelLog -Message (
            "Forwarding loopback endpoint $LocalEndpoint through $SshHost " +
            "to laptop proxy $RemoteProxyEndpoint."
        )
        $PreviousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $Output = & $SshCommand.Source @Arguments 2>&1
            $SshExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $PreviousErrorActionPreference
        }
        foreach ($Line in $Output) {
            $SafeLine = ([string]$Line).Replace("`r", ' ').Replace("`n", ' ')
            if (-not [string]::IsNullOrWhiteSpace($SafeLine)) {
                Write-TunnelLog -Message "ssh: $SafeLine"
            }
        }
        Write-TunnelLog -Message (
            "Tunnel disconnected with exit code $SshExitCode; " +
            "retrying in $RetrySeconds seconds."
        )
        Start-Sleep -Seconds $RetrySeconds
    }
}
catch {
    Write-TunnelLog -Message "Tunnel watchdog failed: $($_.Exception.Message)"
    exit 1
}
finally {
    $Mutex.ReleaseMutex()
    $Mutex.Dispose()
}
