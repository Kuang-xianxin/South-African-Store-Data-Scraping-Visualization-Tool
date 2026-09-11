[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [switch]$Execute
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ExpectedComputerName = 'LAPTOP-2T5MN8EU'
$ReplicaRoot = 'D:\TakealotMySQLReplica'
$QuarantineRoot = Join-Path $ReplicaRoot 'quarantine'
$CredentialPath = Join-Path $ReplicaRoot 'secrets\mysql-dba.dpapi'
$MySqlClientPath = Join-Path $ReplicaRoot 'mysql-8.0.46-winx64\bin\mysql.exe'
$MySqlConfigPath = Join-Path $ReplicaRoot 'my.ini'

function Write-JsonFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value
    )
    [IO.File]::WriteAllText(
        $Path,
        (($Value | ConvertTo-Json -Depth 8) + [Environment]::NewLine),
        (New-Object Text.UTF8Encoding($false))
    )
}

function Invoke-MySql {
    param([Parameter(Mandatory = $true)][string]$Sql)

    $SavedErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $Output = @(
            & $MySqlClientPath `
                --protocol=TCP `
                --host=127.0.0.1 `
                --port=3306 `
                "--user=$script:DbaUser" `
                --batch `
                --raw `
                --skip-column-names `
                "--execute=$Sql" 2>&1
        )
        $ExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $SavedErrorActionPreference
    }
    if ($ExitCode -ne 0) {
        $Message = @($Output | ForEach-Object { [string]$_ }) -join [Environment]::NewLine
        throw "mysql.exe failed with exit code $ExitCode. $Message"
    }
    return @($Output | ForEach-Object { [string]$_ })
}

function Get-FirstMySqlValue {
    param([Parameter(Mandatory = $true)][string]$Sql)
    $Rows = @(Invoke-MySql -Sql $Sql)
    if ($Rows.Count -lt 1) {
        return ''
    }
    return [string]$Rows[0]
}

if ($env:COMPUTERNAME -cne $ExpectedComputerName) {
    throw "Safety stop: expected $ExpectedComputerName, got $env:COMPUTERNAME."
}
$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$Principal = New-Object Security.Principal.WindowsPrincipal($Identity)
if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator privileges are required on the laptop.'
}
foreach ($Path in @($CredentialPath, $MySqlClientPath, $MySqlConfigPath)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required file not found: $Path"
    }
}

Add-Type -AssemblyName System.Security
$ProtectedSecretBytes = [IO.File]::ReadAllBytes($CredentialPath)
$PlainSecretBytes = [Security.Cryptography.ProtectedData]::Unprotect(
    $ProtectedSecretBytes,
    $null,
    [Security.Cryptography.DataProtectionScope]::LocalMachine
)
$SecretText = [Text.Encoding]::UTF8.GetString($PlainSecretBytes)
$SecretParts = $SecretText -split [char]10, 2
if ($SecretParts.Count -ne 2) {
    throw 'The laptop DBA credential has an unexpected format.'
}
$script:DbaUser = $SecretParts[0].Trim()
$DbaPassword = $SecretParts[1].Trim()
$PreviousMySqlPassword = $env:MYSQL_PWD
$env:MYSQL_PWD = $DbaPassword

$Timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$ConfigBackupPath = Join-Path $QuarantineRoot "my.ini-before-file-position-$Timestamp"
$ManifestPath = Join-Path $QuarantineRoot "file-position-preparation-$Timestamp.json"
$ConfigBackupCreated = $false
$RuntimeChanged = $false
$ExecutionSucceeded = $false
$Manifest = [ordered]@{
    computer = $env:COMPUTERNAME
    started_at = (Get-Date).ToString('o')
    status = 'preflight'
    mysql_config = $MySqlConfigPath
}

try {
    $Variables = @(Invoke-MySql -Sql (
        "SELECT CONCAT(@@hostname,'|',VERSION(),'|',@@server_id,'|'," +
        "@@gtid_mode,'|',@@enforce_gtid_consistency,'|',@@read_only,'|'," +
        "@@super_read_only,'|',@@log_bin,'|',@@log_replica_updates);"
    ))
    $GtidMode = Get-FirstMySqlValue -Sql 'SELECT @@global.gtid_mode;'
    $EnforceGtidConsistency = Get-FirstMySqlValue -Sql (
        'SELECT @@global.enforce_gtid_consistency;'
    )
    $GtidExecuted = Get-FirstMySqlValue -Sql (
        "SELECT REPLACE(@@global.gtid_executed, CHAR(10), '');"
    )
    $GtidPurged = Get-FirstMySqlValue -Sql (
        "SELECT REPLACE(@@global.gtid_purged, CHAR(10), '');"
    )
    $GtidOwnedState = Get-FirstMySqlValue -Sql (
        "SELECT IF(@@global.gtid_owned='', 'EMPTY', 'BUSY');"
    )
    $ReplicaChannelCount = [int](Get-FirstMySqlValue -Sql (
        'SELECT COUNT(*) FROM performance_schema.replication_connection_status;'
    ))
    $TableCount = [int](Get-FirstMySqlValue -Sql (
        "SELECT COUNT(*) FROM information_schema.TABLES " +
        "WHERE TABLE_SCHEMA='takealot_ops' AND TABLE_TYPE='BASE TABLE';"
    ))
    $BinaryLogs = @(Invoke-MySql -Sql 'SHOW BINARY LOGS;')

    if ($GtidMode -cne 'ON' -or $EnforceGtidConsistency -cne 'ON') {
        throw "Safety stop: expected GTID ON/ON, got $GtidMode/$EnforceGtidConsistency."
    }
    if ($GtidOwnedState -cne 'EMPTY') {
        throw 'Safety stop: the laptop owns an active GTID transaction.'
    }
    if ($ReplicaChannelCount -ne 0) {
        throw 'Safety stop: the laptop already has a replication channel.'
    }
    if ($TableCount -le 0) {
        throw 'Safety stop: takealot_ops has no imported tables.'
    }

    $ConfigText = [IO.File]::ReadAllText($MySqlConfigPath)
    $GtidConfigCount = (
        $ConfigText.Split(@('gtid-mode=ON'), [StringSplitOptions]::None).Count - 1
    )
    $EnforceConfigCount = (
        $ConfigText.Split(
            @('enforce-gtid-consistency=ON'),
            [StringSplitOptions]::None
        ).Count - 1
    )
    if ($GtidConfigCount -ne 1 -or $EnforceConfigCount -ne 1) {
        throw 'Safety stop: expected exactly one ON setting for each GTID option.'
    }
    if ($ConfigText -match '(?im)^replicate[-_]same[-_]server[-_]id\s*=') {
        throw 'Safety stop: replicate-same-server-id is configured.'
    }

    $Manifest.mysql_variables = $Variables
    $Manifest.gtid_executed_before = $GtidExecuted
    $Manifest.gtid_purged_before = $GtidPurged
    $Manifest.binary_logs_before = $BinaryLogs
    $Manifest.replica_channels_before = $ReplicaChannelCount
    $Manifest.table_count = $TableCount
    $Manifest.config_sha256_before = (
        Get-FileHash -LiteralPath $MySqlConfigPath -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if (-not $Execute) {
        $Manifest.status = 'preflight_ok'
        $Manifest | ConvertTo-Json -Depth 8
        exit 0
    }

    New-Item -ItemType Directory -Path $QuarantineRoot -Force | Out-Null
    Copy-Item -LiteralPath $MySqlConfigPath -Destination $ConfigBackupPath
    $ConfigBackupCreated = $true
    $Manifest.mysql_config_backup = $ConfigBackupPath
    $Manifest.mysql_config_backup_sha256 = (
        Get-FileHash -LiteralPath $ConfigBackupPath -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    $Manifest.status = 'starting'
    Write-JsonFile -Path $ManifestPath -Value $Manifest

    Invoke-MySql -Sql 'RESET MASTER;' | Out-Null
    $RuntimeChanged = $true
    if ((Get-FirstMySqlValue -Sql (
        "SELECT REPLACE(@@global.gtid_executed, CHAR(10), '');"
    )) -ne '') {
        throw 'RESET MASTER did not clear gtid_executed.'
    }

    Invoke-MySql -Sql 'SET @@global.gtid_mode=ON_PERMISSIVE;' | Out-Null
    Invoke-MySql -Sql 'SET @@global.gtid_mode=OFF_PERMISSIVE;' | Out-Null
    $OwnedDeadline = (Get-Date).AddSeconds(30)
    do {
        $GtidOwnedState = Get-FirstMySqlValue -Sql (
            "SELECT IF(@@global.gtid_owned='', 'EMPTY', 'BUSY');"
        )
        if ($GtidOwnedState -eq 'EMPTY') {
            break
        }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $OwnedDeadline)
    if ($GtidOwnedState -ne 'EMPTY') {
        throw 'gtid_owned did not become empty within 30 seconds.'
    }
    Invoke-MySql -Sql 'SET @@global.gtid_mode=OFF;' | Out-Null
    Invoke-MySql -Sql 'SET @@global.enforce_gtid_consistency=OFF;' | Out-Null

    $UpdatedConfigText = $ConfigText.Replace('gtid-mode=ON', 'gtid-mode=OFF')
    $UpdatedConfigText = $UpdatedConfigText.Replace(
        'enforce-gtid-consistency=ON',
        'enforce-gtid-consistency=OFF'
    )
    [IO.File]::WriteAllText(
        $MySqlConfigPath,
        $UpdatedConfigText,
        (New-Object Text.UTF8Encoding($false))
    )

    $FinalMode = Get-FirstMySqlValue -Sql 'SELECT @@global.gtid_mode;'
    $FinalEnforce = Get-FirstMySqlValue -Sql (
        'SELECT @@global.enforce_gtid_consistency;'
    )
    $FinalExecuted = Get-FirstMySqlValue -Sql (
        "SELECT REPLACE(@@global.gtid_executed, CHAR(10), '');"
    )
    $FinalChannelCount = [int](Get-FirstMySqlValue -Sql (
        'SELECT COUNT(*) FROM performance_schema.replication_connection_status;'
    ))
    $FinalConfigText = [IO.File]::ReadAllText($MySqlConfigPath)
    if (
        $FinalMode -cne 'OFF' -or
        $FinalEnforce -cne 'OFF' -or
        $FinalExecuted -ne '' -or
        $FinalChannelCount -ne 0 -or
        -not $FinalConfigText.Contains('gtid-mode=OFF') -or
        -not $FinalConfigText.Contains('enforce-gtid-consistency=OFF')
    ) {
        throw 'The laptop did not reach the expected clean file-position state.'
    }

    $Manifest.status = 'success'
    $Manifest.completed_at = (Get-Date).ToString('o')
    $Manifest.gtid_mode_after = $FinalMode
    $Manifest.enforce_gtid_consistency_after = $FinalEnforce
    $Manifest.gtid_executed_after = $FinalExecuted
    $Manifest.binary_logs_after = @(Invoke-MySql -Sql 'SHOW BINARY LOGS;')
    $Manifest.config_sha256_after = (
        Get-FileHash -LiteralPath $MySqlConfigPath -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    Write-JsonFile -Path $ManifestPath -Value $Manifest
    $ExecutionSucceeded = $true

    [pscustomobject]@{
        computer = $env:COMPUTERNAME
        status = 'success'
        gtid_mode = $FinalMode
        enforce_gtid_consistency = $FinalEnforce
        gtid_executed_empty = ($FinalExecuted -eq '')
        table_count = $TableCount
        manifest = $ManifestPath
    } | ConvertTo-Json -Depth 6
}
catch {
    $Failure = $_
    if ($ConfigBackupCreated) {
        try {
            Copy-Item -LiteralPath $ConfigBackupPath -Destination $MySqlConfigPath -Force
        }
        catch {
        }
    }
    if ($RuntimeChanged) {
        try {
            Invoke-MySql -Sql 'SET @@global.enforce_gtid_consistency=ON;' | Out-Null
            $CurrentMode = Get-FirstMySqlValue -Sql 'SELECT @@global.gtid_mode;'
            if ($CurrentMode -eq 'OFF') {
                Invoke-MySql -Sql 'SET @@global.gtid_mode=OFF_PERMISSIVE;' | Out-Null
                $CurrentMode = 'OFF_PERMISSIVE'
            }
            if ($CurrentMode -eq 'OFF_PERMISSIVE') {
                Invoke-MySql -Sql 'SET @@global.gtid_mode=ON_PERMISSIVE;' | Out-Null
                $CurrentMode = 'ON_PERMISSIVE'
            }
            if ($CurrentMode -eq 'ON_PERMISSIVE') {
                Invoke-MySql -Sql 'SET @@global.gtid_mode=ON;' | Out-Null
            }
        }
        catch {
        }
    }
    try {
        $Manifest.status = 'failed'
        $Manifest.failed_at = (Get-Date).ToString('o')
        $Manifest.failure = $Failure.Exception.Message
        Write-JsonFile -Path $ManifestPath -Value $Manifest
    }
    catch {
    }
    throw $Failure
}
finally {
    if ($null -eq $PreviousMySqlPassword) {
        Remove-Item Env:\MYSQL_PWD -ErrorAction SilentlyContinue
    }
    else {
        $env:MYSQL_PWD = $PreviousMySqlPassword
    }
    $DbaPassword = $null
    $SecretText = $null
    if ($null -ne $PlainSecretBytes) {
        [Array]::Clear($PlainSecretBytes, 0, $PlainSecretBytes.Length)
    }
    if (-not $ExecutionSucceeded -and $Execute) {
        Write-Error "Laptop file-position preparation failed; inspect $ManifestPath."
    }
}
