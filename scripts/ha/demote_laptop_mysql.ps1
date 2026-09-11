[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$Reason = 'witness lease unavailable'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ExpectedComputerName = 'LAPTOP-2T5MN8EU'
$ErpTaskName = 'Takealot HA ERP'
$ErpPort = 8501
$ReplicaRoot = 'D:\TakealotMySQLReplica'
$CredentialPath = Join-Path $ReplicaRoot 'secrets\mysql-dba.dpapi'
$MySqlPath = Join-Path $ReplicaRoot 'mysql-8.0.46-winx64\bin\mysql.exe'
$StateRoot = 'D:\TakealotHA\witness'
$DemotionLogPath = Join-Path $StateRoot 'laptop-demotion.log'

function Write-DemotionLog {
    param([Parameter(Mandatory = $true)][string]$Message)

    New-Item -ItemType Directory -Path $StateRoot -Force | Out-Null
    $Timestamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz')
    Add-Content -LiteralPath $DemotionLogPath -Encoding UTF8 -Value "[$Timestamp] $Message"
}

function Invoke-MySql {
    param(
        [Parameter(Mandatory = $true)][string]$User,
        [Parameter(Mandatory = $true)][string]$Password,
        [Parameter(Mandatory = $true)][string]$Sql
    )

    $PreviousPassword = $env:MYSQL_PWD
    $SavedErrorActionPreference = $ErrorActionPreference
    $env:MYSQL_PWD = $Password
    $ErrorActionPreference = 'Continue'
    try {
        $Output = @(
            & $MySqlPath `
                --protocol=TCP `
                --host=127.0.0.1 `
                --port=3306 `
                "--user=$User" `
                --batch `
                --raw `
                --skip-column-names `
                "--execute=$Sql" 2>&1
        )
        $ExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $SavedErrorActionPreference
        if ($null -eq $PreviousPassword) {
            Remove-Item Env:\MYSQL_PWD -ErrorAction SilentlyContinue
        }
        else {
            $env:MYSQL_PWD = $PreviousPassword
        }
    }
    if ($ExitCode -ne 0) {
        throw "mysql.exe failed with exit code $ExitCode."
    }
    return @($Output | ForEach-Object { [string]$_ })
}

if ($env:COMPUTERNAME -cne $ExpectedComputerName) {
    throw "Safety stop: expected $ExpectedComputerName, got $env:COMPUTERNAME."
}
foreach ($Path in @($CredentialPath, $MySqlPath)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required file not found: $Path"
    }
}

$Task = Get-ScheduledTask -TaskName $ErpTaskName -ErrorAction SilentlyContinue
if ($null -ne $Task -and [string]$Task.State -eq 'Running') {
    Stop-ScheduledTask -TaskName $ErpTaskName -ErrorAction SilentlyContinue
}
$ReleaseDeadline = (Get-Date).AddSeconds(20)
while (
    (Get-Date) -lt $ReleaseDeadline -and
    (Get-NetTCPConnection -LocalPort $ErpPort -State Listen -ErrorAction SilentlyContinue)
) {
    Start-Sleep -Milliseconds 250
}
if (Get-NetTCPConnection -LocalPort $ErpPort -State Listen -ErrorAction SilentlyContinue) {
    throw "Fail-closed stop could not release ERP port $ErpPort."
}

$ProtectedBytes = $null
$PlainBytes = $null
$DbaSecret = $null
$DbaPassword = $null
try {
    Add-Type -AssemblyName System.Security
    $ProtectedBytes = [IO.File]::ReadAllBytes($CredentialPath)
    $PlainBytes = [Security.Cryptography.ProtectedData]::Unprotect(
        $ProtectedBytes,
        $null,
        [Security.Cryptography.DataProtectionScope]::LocalMachine
    )
    $DbaSecret = [Text.Encoding]::UTF8.GetString($PlainBytes)
    $DbaParts = $DbaSecret -split [char]10, 2
    if ($DbaParts.Count -ne 2) {
        throw 'The laptop DBA credential has an unexpected format.'
    }
    $DbaUser = $DbaParts[0].Trim()
    $DbaPassword = $DbaParts[1].Trim()
    Invoke-MySql `
        -User $DbaUser `
        -Password $DbaPassword `
        -Sql 'SET GLOBAL read_only=ON; SET GLOBAL super_read_only=ON;' |
        Out-Null
    $State = @(
        Invoke-MySql `
            -User $DbaUser `
            -Password $DbaPassword `
            -Sql (
                "SELECT CONCAT(@@hostname,'|',@@server_id,'|'," +
                "@@global.read_only,'|',@@global.super_read_only);"
            )
    )[0]
    if ($State -cne 'LAPTOP-2T5MN8EU|2|1|1') {
        throw "Laptop fail-closed state verification failed: $State"
    }
    $SafeReason = ($Reason -replace '[\r\n]+', ' ').Trim()
    Write-DemotionLog "ERP stopped and MySQL fenced read-only. reason=$SafeReason"
    [pscustomobject]@{
        computer = $env:COMPUTERNAME
        status = 'demoted'
        erp_listening = $false
        read_only = 1
        super_read_only = 1
        reason = $SafeReason
    } | ConvertTo-Json -Compress
}
finally {
    $DbaSecret = $null
    $DbaPassword = $null
    if ($null -ne $PlainBytes) {
        [Array]::Clear($PlainBytes, 0, $PlainBytes.Length)
    }
    if ($null -ne $ProtectedBytes) {
        [Array]::Clear($ProtectedBytes, 0, $ProtectedBytes.Length)
    }
}
