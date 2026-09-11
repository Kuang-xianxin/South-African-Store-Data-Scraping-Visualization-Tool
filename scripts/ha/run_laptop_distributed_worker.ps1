[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$BatchId = '',

    [Parameter(Mandatory = $false)]
    [ValidateRange(0, 1000000)]
    [int]$MaxJobs = 0,

    [Parameter(Mandatory = $false)]
    [ValidateRange(60, 7200)]
    [int]$LeaseSeconds = 1800
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ExpectedComputerName = 'LAPTOP-2T5MN8EU'
$ExpectedUser = 'takealot_crawler'
$PrimaryHost = '100.70.103.11'
$PrimaryPort = 13306
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$PythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$CredentialPath = 'D:\TakealotHA\secrets\distributed-crawler-primary.dpapi'
$LogRoot = 'D:\TakealotHA\distributed-crawler'
$StdoutPath = Join-Path $LogRoot 'laptop-worker.stdout.log'
$StderrPath = Join-Path $LogRoot 'laptop-worker.stderr.log'
$ProxyHost = '127.0.0.1'
$ProxyPort = 7897

if ($env:COMPUTERNAME -cne $ExpectedComputerName) {
    throw "Safety stop: expected $ExpectedComputerName, got $env:COMPUTERNAME."
}
foreach ($RequiredPath in @($PythonPath, $CredentialPath)) {
    if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
        throw "Required worker file not found: $RequiredPath"
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot 'pyproject.toml') -PathType Leaf)) {
    throw 'Resolved project root is not the Takealot project.'
}
$ProxyListener = Get-NetTCPConnection `
    -LocalAddress $ProxyHost `
    -LocalPort $ProxyPort `
    -State Listen `
    -ErrorAction SilentlyContinue
if ($null -eq $ProxyListener) {
    throw "Clash SOCKS listener is not available at $ProxyHost`:$ProxyPort."
}

Add-Type -AssemblyName System.Security
$ProtectedBytes = [IO.File]::ReadAllBytes($CredentialPath)
$PlainBytes = [Security.Cryptography.ProtectedData]::Unprotect(
    $ProtectedBytes,
    $null,
    [Security.Cryptography.DataProtectionScope]::LocalMachine
)
$SecretText = [Text.Encoding]::UTF8.GetString($PlainBytes)
$SecretParts = $SecretText -split "`n", 2
if ($SecretParts.Count -ne 2 -or $SecretParts[0].Trim() -cne $ExpectedUser) {
    throw 'The protected distributed crawler credential is invalid.'
}
$CrawlerPassword = $SecretParts[1].Trim()
$EncodedUser = [Uri]::EscapeDataString($ExpectedUser)
$EncodedPassword = [Uri]::EscapeDataString($CrawlerPassword)
$PreviousDatabaseUrl = $env:TAKEALOT_DISTRIBUTED_DATABASE_URL
$env:TAKEALOT_DISTRIBUTED_DATABASE_URL = (
    "mysql+pymysql://$EncodedUser`:$EncodedPassword@$PrimaryHost`:$PrimaryPort/" +
    'takealot_ops?charset=utf8mb4'
)

$Arguments = @(
    '-m',
    'takealot_ops.competitors.distributed_worker',
    '--project-root',
    $ProjectRoot,
    '--worker-id',
    'laptop-2t5mn8eu-clash',
    '--egress-label',
    'laptop-clash-rule-proxy',
    '--proxy',
    "socks5://$ProxyHost`:$ProxyPort",
    '--lease-seconds',
    [string]$LeaseSeconds,
    '--max-jobs',
    [string]$MaxJobs
)
if (-not [string]::IsNullOrWhiteSpace($BatchId)) {
    $Arguments += @('--batch-id', $BatchId.Trim())
}

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
        throw "Distributed crawler worker exited with code $ExitCode. See $StderrPath."
    }
    [pscustomobject]@{
        computer = $env:COMPUTERNAME
        status = 'success'
        batch_id = $BatchId
        max_jobs = $MaxJobs
        worker_id = 'laptop-2t5mn8eu-clash'
        primary = "$PrimaryHost`:$PrimaryPort"
        proxy = "$ProxyHost`:$ProxyPort"
        stdout = $StdoutPath
        stderr = $StderrPath
        password_recorded = $false
    } | ConvertTo-Json -Depth 4
}
finally {
    if ($null -eq $PreviousDatabaseUrl) {
        Remove-Item Env:\TAKEALOT_DISTRIBUTED_DATABASE_URL -ErrorAction SilentlyContinue
    }
    else {
        $env:TAKEALOT_DISTRIBUTED_DATABASE_URL = $PreviousDatabaseUrl
    }
    $CrawlerPassword = $null
    $EncodedPassword = $null
    $SecretText = $null
    $SecretParts = $null
    if ($null -ne $ProtectedBytes) {
        [Array]::Clear($ProtectedBytes, 0, $ProtectedBytes.Length)
    }
    if ($null -ne $PlainBytes) {
        [Array]::Clear($PlainBytes, 0, $PlainBytes.Length)
    }
}
