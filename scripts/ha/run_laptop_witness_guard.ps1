[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ExpectedComputerName = 'LAPTOP-2T5MN8EU'
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$PythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$IdentityFile = 'D:\TakealotHA\witness\laptop-witness-ed25519'
$KnownHostsFile = 'D:\TakealotHA\witness\known_hosts'
$StatusFile = 'D:\TakealotHA\witness\laptop-guard-status.json'
$StopFile = 'D:\TakealotHA\witness\laptop-guard.stop'
$FailCloseScript = Join-Path $PSScriptRoot 'demote_laptop_mysql.ps1'
$LogDirectory = Join-Path $ProjectRoot 'logs'
$StdoutPath = Join-Path $LogDirectory 'ha-laptop-guard.stdout.log'
$StderrPath = Join-Path $LogDirectory 'ha-laptop-guard.stderr.log'

if ($env:COMPUTERNAME -cne $ExpectedComputerName) {
    throw "Safety stop: expected $ExpectedComputerName, got $env:COMPUTERNAME."
}
if (
    [Security.Principal.WindowsIdentity]::GetCurrent().User.Value -cne
    'S-1-5-18'
) {
    throw 'The laptop witness guard must run as SYSTEM.'
}
foreach ($Path in @(
    $PythonPath,
    $IdentityFile,
    $KnownHostsFile,
    $FailCloseScript
)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required HA guard file not found: $Path"
    }
}

New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
$env:TAKEALOT_PROJECT_ROOT = $ProjectRoot
Push-Location -LiteralPath $ProjectRoot
try {
    & $PythonPath `
        -m takealot_ops.ha.guard `
        --node-id laptop-2t5mn8eu `
        --witness-host 100.72.100.10 `
        --witness-port 22 `
        --identity-file $IdentityFile `
        --known-hosts-file $KnownHostsFile `
        --status-file $StatusFile `
        --stop-file $StopFile `
        --fail-close-script $FailCloseScript `
        1>> $StdoutPath `
        2>> $StderrPath
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
