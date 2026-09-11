[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ExpectedComputerName = 'LAPTOP-2T5MN8EU'
$ServiceName = 'MySQL80'
$ReplicaRoot = 'D:\TakealotMySQLReplica'
$ReplicaConfigPath = Join-Path $ReplicaRoot 'my.ini'
$MySqlClientPath = Join-Path $ReplicaRoot 'mysql-8.0.46-winx64\bin\mysql.exe'
$DbaCredentialPath = Join-Path $ReplicaRoot 'secrets\mysql-dba.dpapi'

if ($env:COMPUTERNAME -ne $ExpectedComputerName) {
    throw "Safety stop: current computer is $env:COMPUTERNAME, expected $ExpectedComputerName."
}
foreach ($RequiredPath in @(
    $ReplicaConfigPath,
    $MySqlClientPath,
    $DbaCredentialPath
)) {
    if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
        throw "Required replica file not found: $RequiredPath"
    }
}

Add-Type -AssemblyName System.Security
$ProtectedSecretBytes = [IO.File]::ReadAllBytes($DbaCredentialPath)
$PlainSecretBytes = [Security.Cryptography.ProtectedData]::Unprotect(
    $ProtectedSecretBytes,
    $null,
    [Security.Cryptography.DataProtectionScope]::LocalMachine
)
$SecretText = [Text.Encoding]::UTF8.GetString($PlainSecretBytes)
$SecretParts = $SecretText -split "`n", 2
if ($SecretParts.Count -ne 2 -or $SecretParts[0] -cne 'takealot_dba') {
    throw 'The protected DBA credential payload is invalid.'
}

$PreviousMySqlPassword = $env:MYSQL_PWD
$env:MYSQL_PWD = $SecretParts[1]
try {
    $VariablesSql = (
        "SELECT CONCAT_WS('|', VERSION(), @@GLOBAL.server_id, " +
        "@@GLOBAL.gtid_mode, @@GLOBAL.enforce_gtid_consistency, " +
        "@@GLOBAL.read_only, @@GLOBAL.super_read_only, @@GLOBAL.log_bin, " +
        "@@GLOBAL.log_replica_updates, @@GLOBAL.relay_log_recovery, " +
        "@@GLOBAL.bind_address);"
    )
    $SavedErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $VariableOutput = @(
            & $MySqlClientPath `
                --protocol=TCP `
                --host=127.0.0.1 `
                --port=3306 `
                --user=takealot_dba `
                --batch `
                --skip-column-names `
                --execute=$VariablesSql 2>&1
        )
        $VariableExitCode = $LASTEXITCODE
        $DatabaseOutput = @(
            & $MySqlClientPath `
                --protocol=TCP `
                --host=127.0.0.1 `
                --port=3306 `
                --user=takealot_dba `
                --batch `
                --skip-column-names `
                --execute='SHOW DATABASES;' 2>&1
        )
        $DatabaseExitCode = $LASTEXITCODE
        $ReplicaOutput = @(
            & $MySqlClientPath `
                --protocol=TCP `
                --host=127.0.0.1 `
                --port=3306 `
                --user=takealot_dba `
                --batch `
                --skip-column-names `
                --execute='SHOW REPLICA STATUS;' 2>&1
        )
        $ReplicaExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $SavedErrorActionPreference
    }
    if ($VariableExitCode -ne 0) {
        throw "Variable verification failed: $($VariableOutput -join ' ')"
    }
    if ($DatabaseExitCode -ne 0) {
        throw 'Database enumeration failed.'
    }
    if ($ReplicaExitCode -ne 0) {
        throw 'Replica status query failed.'
    }
}
finally {
    if ($null -eq $PreviousMySqlPassword) {
        Remove-Item Env:\MYSQL_PWD -ErrorAction SilentlyContinue
    }
    else {
        $env:MYSQL_PWD = $PreviousMySqlPassword
    }
    [Array]::Clear($PlainSecretBytes, 0, $PlainSecretBytes.Length)
    [Array]::Clear($ProtectedSecretBytes, 0, $ProtectedSecretBytes.Length)
    $SecretText = $null
    $SecretParts = $null
}

$Service = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
$Listeners3306 = @(
    Get-NetTCPConnection -LocalPort 3306 -State Listen -ErrorAction SilentlyContinue |
        Select-Object LocalAddress, LocalPort, OwningProcess
)
$Listeners33060 = @(
    Get-NetTCPConnection -LocalPort 33060 -State Listen -ErrorAction SilentlyContinue |
        Select-Object LocalAddress, LocalPort, OwningProcess
)
$CredentialAcl = @(
    (Get-Acl -LiteralPath $DbaCredentialPath).Access |
        ForEach-Object {
            [pscustomobject]@{
                identity = $_.IdentityReference.Value
                rights = $_.FileSystemRights.ToString()
                type = $_.AccessControlType.ToString()
                inherited = $_.IsInherited
            }
        }
)
$LegacyQuarantines = @(
    Get-ChildItem `
        -LiteralPath 'C:\ProgramData\MySQL\MySQL Server 8.0' `
        -Directory `
        -Filter 'Data.pre-takealot-replica-*' `
        -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty FullName
)
$Databases = @($DatabaseOutput | ForEach-Object { [string]$_ })
$VariableValues = ([string]($VariableOutput -join '')).Split('|')
if ($VariableValues.Count -ne 10) {
    throw "Unexpected variable verification output: $($VariableOutput -join ' ')"
}

[pscustomobject]@{
    computer = $env:COMPUTERNAME
    service_state = [string]$Service.State
    service_start_mode = [string]$Service.StartMode
    service_command = [string]$Service.PathName
    version = $VariableValues[0]
    server_id = [int]$VariableValues[1]
    gtid_mode = $VariableValues[2]
    enforce_gtid_consistency = $VariableValues[3]
    read_only = [bool][int]$VariableValues[4]
    super_read_only = [bool][int]$VariableValues[5]
    log_bin = [bool][int]$VariableValues[6]
    log_replica_updates = [bool][int]$VariableValues[7]
    relay_log_recovery = [bool][int]$VariableValues[8]
    bind_address = $VariableValues[9]
    databases = $Databases
    takealot_ops_exists = $Databases -contains 'takealot_ops'
    replica_configured = @($ReplicaOutput).Count -gt 0
    listeners_3306 = $Listeners3306
    listeners_33060 = $Listeners33060
    credential_decrypt_round_trip = $true
    credential_acl = $CredentialAcl
    legacy_data_quarantines = $LegacyQuarantines
    active_data_exists = Test-Path -LiteralPath (Join-Path $ReplicaRoot 'data') -PathType Container
} | ConvertTo-Json -Depth 8
