[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^distributed-canary-[0-9]{8}-[0-9]{6}$')]
    [string]$BatchId,

    [Parameter(Mandatory = $true)]
    [ValidateRange(0, 10)]
    [int]$MaxJobs,

    [Parameter(Mandatory = $false)]
    [switch]$ReadDatabaseUrlFromStdin
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ExpectedComputerName = 'LAPTOP-2T5MN8EU'
$ExpectedDatabaseUser = 'takealot_app'
$ExpectedPrimaryHost = '100.70.103.11'
$ExpectedPrimaryPort = 13306
$ExpectedDatabase = 'takealot_ops'
$ProxyHost = '127.0.0.1'
$ProxyPort = 7897
$LogRoot = 'D:\TakealotHA\distributed-crawler'

if ($env:COMPUTERNAME -cne $ExpectedComputerName) {
    throw "Safety stop: expected $ExpectedComputerName, got $env:COMPUTERNAME."
}
if (-not $ReadDatabaseUrlFromStdin) {
    throw 'The temporary canary database URL must arrive through standard input.'
}
$ProjectCandidates = @(
    Get-ChildItem -LiteralPath 'D:\' -Directory -ErrorAction Stop |
        Where-Object {
            (Test-Path -LiteralPath (Join-Path $_.FullName 'pyproject.toml') -PathType Leaf) -and
            (Test-Path -LiteralPath (
                Join-Path $_.FullName 'src\takealot_ops\competitors\distributed_worker.py'
            ) -PathType Leaf)
        }
)
if ($ProjectCandidates.Count -ne 1) {
    throw "Expected exactly one Takealot project on D:, found $($ProjectCandidates.Count)."
}
$ProjectRoot = $ProjectCandidates[0].FullName
$PythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Project Python not found: $PythonPath"
}
$ProxyListener = Get-NetTCPConnection `
    -LocalAddress $ProxyHost `
    -LocalPort $ProxyPort `
    -State Listen `
    -ErrorAction SilentlyContinue
if ($null -eq $ProxyListener) {
    throw "Clash SOCKS listener is not available at $ProxyHost`:$ProxyPort."
}

$DatabaseUrl = [Console]::In.ReadToEnd().Trim()
if ([string]::IsNullOrWhiteSpace($DatabaseUrl)) {
    throw 'The temporary canary database URL is empty.'
}
try {
    $ParsedUrl = [Uri]$DatabaseUrl
}
catch {
    throw 'The temporary canary database URL is invalid.'
}
$UserInfoParts = $ParsedUrl.UserInfo -split ':', 2
if (
    $ParsedUrl.Scheme -cne 'mysql+pymysql' -or
    $UserInfoParts.Count -ne 2 -or
    [Uri]::UnescapeDataString($UserInfoParts[0]) -cne $ExpectedDatabaseUser -or
    $ParsedUrl.Host -cne $ExpectedPrimaryHost -or
    $ParsedUrl.Port -ne $ExpectedPrimaryPort -or
    $ParsedUrl.AbsolutePath.Trim('/') -cne $ExpectedDatabase -or
    $ParsedUrl.Fragment -ne '' -or
    $ParsedUrl.Query.TrimStart('?') -notin @('', 'charset=utf8mb4')
) {
    throw 'The temporary canary database URL does not match the guarded primary.'
}

$PreviousDatabaseUrl = $env:TAKEALOT_DISTRIBUTED_DATABASE_URL
$env:TAKEALOT_DISTRIBUTED_DATABASE_URL = $DatabaseUrl
$StdoutPath = Join-Path $LogRoot "$BatchId.stdout.log"
$StderrPath = Join-Path $LogRoot "$BatchId.stderr.log"
$Arguments = @(
    '-m',
    'takealot_ops.competitors.distributed_worker',
    '--project-root',
    $ProjectRoot,
    '--worker-id',
    'laptop-2t5mn8eu-clash',
    '--egress-label',
    'laptop-clash-rule-proxy-canary',
    '--proxy',
    "socks5://$ProxyHost`:$ProxyPort",
    '--batch-id',
    $BatchId,
    '--lease-seconds',
    '1800',
    '--max-jobs',
    [string]$MaxJobs
)
$ExitCode = -1
try {
    New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null
    Push-Location -LiteralPath $ProjectRoot
    try {
        & $PythonPath @Arguments 1>> $StdoutPath 2>> $StderrPath
        $ExitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    if ($ExitCode -ne 0) {
        throw "Distributed canary exited with code $ExitCode. See $StderrPath."
    }
    [pscustomobject]@{
        computer = $env:COMPUTERNAME
        status = 'success'
        batch_id = $BatchId
        max_jobs = $MaxJobs
        worker_id = 'laptop-2t5mn8eu-clash'
        primary = "$ExpectedPrimaryHost`:$ExpectedPrimaryPort"
        proxy = "$ProxyHost`:$ProxyPort"
        stdout = $StdoutPath
        stderr = $StderrPath
        database_url_persisted = $false
        password_recorded = $false
    } | ConvertTo-Json -Depth 4
}
finally {
    if ($null -eq $PreviousDatabaseUrl) {
        Remove-Item Env:\TAKEALOT_DISTRIBUTED_DATABASE_URL `
            -ErrorAction SilentlyContinue
    }
    else {
        $env:TAKEALOT_DISTRIBUTED_DATABASE_URL = $PreviousDatabaseUrl
    }
    $DatabaseUrl = $null
    $ParsedUrl = $null
    $UserInfoParts = $null
}
