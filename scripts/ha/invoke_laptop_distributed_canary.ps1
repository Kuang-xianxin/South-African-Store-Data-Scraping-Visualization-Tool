[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^distributed-canary-[0-9]{8}-[0-9]{6}$')]
    [string]$BatchId,

    [Parameter(Mandatory = $true)]
    [ValidateRange(0, 10)]
    [int]$MaxJobs,

    [Parameter(Mandatory = $false)]
    [string]$LaptopSshTarget = 'takealot-admin-laptop'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ExpectedComputerName = 'DESKTOP-NTRMANG'
$ExpectedDatabaseUser = 'takealot_app'
$ExpectedLocalHosts = @('127.0.0.1', 'localhost')
$ExpectedLocalPort = 3306
$ExpectedDatabase = 'takealot_ops'
$RemotePrimaryHost = '100.70.103.11'
$RemotePrimaryPort = 13306
$RemoteRunner = 'D:\TakealotHA\bin\run_laptop_distributed_canary.ps1'

if ($env:COMPUTERNAME -cne $ExpectedComputerName) {
    throw "Safety stop: expected $ExpectedComputerName, got $env:COMPUTERNAME."
}
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$PythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Project Python not found: $PythonPath"
}
$PythonCode = @'
from pathlib import Path
from takealot_ops.settings import DashboardSettings
import sys

sys.stdout.write(DashboardSettings.from_env(Path.cwd()).database_url)
'@
$LocalDatabaseUrl = [string](& $PythonPath -c $PythonCode)
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($LocalDatabaseUrl)) {
    throw 'Failed to load the local application database URL.'
}
try {
    $ParsedLocalUrl = [Uri]$LocalDatabaseUrl
}
catch {
    throw 'The local application database URL is invalid.'
}
$LocalUserInfoParts = $ParsedLocalUrl.UserInfo -split ':', 2
if (
    $ParsedLocalUrl.Scheme -cne 'mysql+pymysql' -or
    $LocalUserInfoParts.Count -ne 2 -or
    [Uri]::UnescapeDataString($LocalUserInfoParts[0]) -cne $ExpectedDatabaseUser -or
    $ParsedLocalUrl.Host -notin $ExpectedLocalHosts -or
    $ParsedLocalUrl.Port -ne $ExpectedLocalPort -or
    $ParsedLocalUrl.AbsolutePath.Trim('/') -cne $ExpectedDatabase
) {
    throw 'The local application database URL is not the guarded main account.'
}
$AuthorityStart = $LocalDatabaseUrl.IndexOf('://') + 3
$AtIndex = $LocalDatabaseUrl.LastIndexOf('@')
$PathStart = $LocalDatabaseUrl.IndexOf('/', $AtIndex)
if ($AuthorityStart -lt 3 -or $AtIndex -le $AuthorityStart -or $PathStart -le $AtIndex) {
    throw 'The local application database URL authority cannot be replaced safely.'
}
$RemoteDatabaseUrl = (
    $LocalDatabaseUrl.Substring(0, $AtIndex + 1) +
    "$RemotePrimaryHost`:$RemotePrimaryPort" +
    $LocalDatabaseUrl.Substring($PathStart)
)

$StartInfo = New-Object Diagnostics.ProcessStartInfo
$StartInfo.FileName = (Get-Command ssh.exe -ErrorAction Stop).Source
$StartInfo.Arguments = (
    "-o BatchMode=yes -o ConnectTimeout=10 $LaptopSshTarget " +
    'powershell.exe -NoProfile -NonInteractive -OutputFormat Text ' +
    "-ExecutionPolicy Bypass -File $RemoteRunner " +
    "-BatchId $BatchId -MaxJobs $MaxJobs -ReadDatabaseUrlFromStdin"
)
$StartInfo.UseShellExecute = $false
$StartInfo.CreateNoWindow = $true
$StartInfo.RedirectStandardInput = $true
$StartInfo.RedirectStandardOutput = $true
$StartInfo.RedirectStandardError = $true

$Process = New-Object Diagnostics.Process
$Process.StartInfo = $StartInfo
if (-not $Process.Start()) {
    throw 'Failed to start the guarded laptop canary SSH session.'
}
try {
    $Process.StandardInput.Write($RemoteDatabaseUrl)
    $Process.StandardInput.Close()
    $StandardOutput = $Process.StandardOutput.ReadToEnd()
    $StandardError = $Process.StandardError.ReadToEnd()
    $Process.WaitForExit()
    if ($Process.ExitCode -ne 0) {
        throw "Laptop canary failed with exit code $($Process.ExitCode). $StandardError"
    }
    $RemoteResult = $StandardOutput | ConvertFrom-Json -ErrorAction Stop
    if (
        [string]$RemoteResult.computer -cne 'LAPTOP-2T5MN8EU' -or
        [string]$RemoteResult.status -cne 'success' -or
        [string]$RemoteResult.batch_id -cne $BatchId -or
        [int]$RemoteResult.max_jobs -ne $MaxJobs
    ) {
        throw 'Laptop canary returned an unexpected execution identity.'
    }
    $RemoteResult | ConvertTo-Json -Depth 5
}
finally {
    $Process.Dispose()
    $LocalDatabaseUrl = $null
    $RemoteDatabaseUrl = $null
    $ParsedLocalUrl = $null
    $LocalUserInfoParts = $null
}
