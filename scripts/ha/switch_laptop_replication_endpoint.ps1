[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$TargetHost,

    [Parameter(Mandatory = $true)]
    [ValidateRange(1, 65535)]
    [int]$TargetPort,

    [switch]$Apply,

    [ValidateRange(1, 60)]
    [int]$ValidationDelaySeconds = 10,

    [string]$ExpectedComputerName = 'LAPTOP-2T5MN8EU',

    [ValidateRange(1, 4294967295)]
    [long]$ExpectedReplicaServerId = 2,

    [string]$ExpectedSourceHostName = 'DESKTOP-NTRMANG',

    [ValidateRange(1, 4294967295)]
    [long]$ExpectedSourceServerId = 1,

    [string]$MySqlExe = 'D:\TakealotMySQLReplica\mysql-8.0.46-winx64\bin\mysql.exe',

    [string]$DbaCredentialPath = 'D:\TakealotMySQLReplica\secrets\mysql-dba.dpapi'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function ConvertTo-SqlStringLiteral {
    param([Parameter(Mandatory = $true)][string]$Value)

    return "'" + $Value.Replace("'", "''") + "'"
}

function Invoke-MySql {
    param(
        [Parameter(Mandatory = $true)][string]$User,
        [Parameter(Mandatory = $true)][string]$Password,
        [Parameter(Mandatory = $true)][string]$Sql,
        [string]$HostName = '127.0.0.1',
        [int]$Port = 3306,
        [switch]$SkipColumnNames,
        [switch]$RequireTls
    )

    $previousPassword = $env:MYSQL_PWD
    $env:MYSQL_PWD = $Password
    try {
        $arguments = @(
            "--host=$HostName",
            "--port=$Port",
            "--user=$User",
            '--connect-timeout=8',
            '--batch',
            '--raw'
        )
        if ($SkipColumnNames) {
            $arguments += '--skip-column-names'
        }
        if ($RequireTls) {
            $arguments += '--ssl-mode=REQUIRED'
        }
        $arguments += @('-e', $Sql)

        $output = & $MySqlExe @arguments
        if ($LASTEXITCODE -ne 0) {
            throw "mysql client failed with exit code $LASTEXITCODE"
        }
        return @($output)
    }
    finally {
        if ($null -eq $previousPassword) {
            Remove-Item Env:MYSQL_PWD -ErrorAction SilentlyContinue
        }
        else {
            $env:MYSQL_PWD = $previousPassword
        }
    }
}

if (-not (Test-Path -LiteralPath $MySqlExe -PathType Leaf)) {
    throw "mysql client not found: $MySqlExe"
}
if (-not (Test-Path -LiteralPath $DbaCredentialPath -PathType Leaf)) {
    throw "DBA credential not found: $DbaCredentialPath"
}

$parsedTargetAddress = $null
if (-not [Net.IPAddress]::TryParse($TargetHost, [ref]$parsedTargetAddress)) {
    throw 'TargetHost must be a literal IPv4 or IPv6 address.'
}
$normalizedTargetHost = $parsedTargetAddress.ToString()

Add-Type -AssemblyName System.Security
$protectedCredential = [IO.File]::ReadAllBytes($DbaCredentialPath)
$plainCredential = [Security.Cryptography.ProtectedData]::Unprotect(
    $protectedCredential,
    $null,
    [Security.Cryptography.DataProtectionScope]::LocalMachine
)
$credentialText = [Text.Encoding]::UTF8.GetString($plainCredential)
$credentialParts = $credentialText -split "`r?`n", 2
if ($credentialParts.Count -ne 2) {
    throw 'DBA credential payload is invalid.'
}

$dbaUser = $credentialParts[0].Trim()
$dbaPassword = $credentialParts[1].Trim()
$sourcePassword = $null
$sourceMetadata = $null

try {
    $replicaGuardOutput = @(
        Invoke-MySql `
            -User $dbaUser `
            -Password $dbaPassword `
            -Sql 'SELECT @@hostname,@@server_id,@@read_only,@@super_read_only;' `
            -SkipColumnNames
    )
    $replicaGuard = $replicaGuardOutput[0] -split "`t"
    if (
        $replicaGuard.Count -ne 4 -or
        $replicaGuard[0] -ne $ExpectedComputerName -or
        [long]$replicaGuard[1] -ne $ExpectedReplicaServerId -or
        $replicaGuard[2] -ne '1' -or
        $replicaGuard[3] -ne '1'
    ) {
        throw "Replica safety guard rejected target: $($replicaGuard -join ',')"
    }

    $sourceMetadataOutput = @(
        Invoke-MySql `
            -User $dbaUser `
            -Password $dbaPassword `
            -Sql "SELECT User_name,User_password,Host,Port FROM mysql.slave_master_info WHERE Channel_name='';" `
            -SkipColumnNames
    )
    $sourceMetadata = $sourceMetadataOutput[0] -split "`t", 4
    if ($sourceMetadata.Count -ne 4 -or -not $sourceMetadata[0] -or -not $sourceMetadata[1]) {
        throw 'Stored replication credential metadata is incomplete.'
    }

    $sourceUser = $sourceMetadata[0]
    $sourcePassword = $sourceMetadata[1]
    $originalHost = $sourceMetadata[2]
    $originalPort = [int]$sourceMetadata[3]

    $currentStatusRaw = Invoke-MySql `
        -User $dbaUser `
        -Password $dbaPassword `
        -Sql 'SHOW REPLICA STATUS'
    $currentStatus = $currentStatusRaw | ConvertFrom-Csv -Delimiter "`t"
    $safeToSwitch = (
        $currentStatus.Replica_IO_Running -eq 'Yes' -and
        $currentStatus.Replica_SQL_Running -eq 'Yes' -and
        $currentStatus.Last_IO_Errno -eq '0' -and
        $currentStatus.Last_SQL_Errno -eq '0' -and
        $currentStatus.Seconds_Behind_Source -eq '0' -and
        $currentStatus.Source_Log_File -eq $currentStatus.Relay_Source_Log_File -and
        $currentStatus.Read_Source_Log_Pos -eq $currentStatus.Exec_Source_Log_Pos -and
        -not [string]::IsNullOrWhiteSpace($currentStatus.Relay_Source_Log_File) -and
        [long]$currentStatus.Exec_Source_Log_Pos -gt 4
    )

    $targetIdentity = @(
        Invoke-MySql `
            -User $sourceUser `
            -Password $sourcePassword `
            -Sql "SELECT @@hostname,@@server_id,@@read_only,CURRENT_USER(); SHOW STATUS LIKE 'Ssl_cipher';" `
            -HostName $normalizedTargetHost `
            -Port $TargetPort `
            -SkipColumnNames `
            -RequireTls
    )
    $identityParts = $targetIdentity[0] -split "`t"
    $tlsParts = $targetIdentity[1] -split "`t", 2
    if (
        $identityParts.Count -ne 4 -or
        $identityParts[0] -ne $ExpectedSourceHostName -or
        [long]$identityParts[1] -ne $ExpectedSourceServerId -or
        $identityParts[2] -ne '0' -or
        $tlsParts.Count -ne 2 -or
        $tlsParts[0] -ne 'Ssl_cipher' -or
        [string]::IsNullOrWhiteSpace($tlsParts[1])
    ) {
        throw "Target source identity or TLS guard failed: $($targetIdentity -join ' | ')"
    }

    $plan = [ordered]@{
        computer = $replicaGuard[0]
        replica_server_id = [long]$replicaGuard[1]
        original_endpoint = "${originalHost}:$originalPort"
        target_endpoint = "${normalizedTargetHost}:$TargetPort"
        source_user = $sourceUser
        target_current_user = $identityParts[3]
        tls = 'required-and-verified'
        safe_source_log_file = $currentStatus.Relay_Source_Log_File
        safe_source_log_pos = [long]$currentStatus.Exec_Source_Log_Pos
        safe_to_switch = $safeToSwitch
        apply = [bool]$Apply
    }
    if (-not $Apply) {
        $plan | ConvertTo-Json -Compress
        return
    }
    if (-not $safeToSwitch) {
        throw "Replica is not at a fully caught-up switch point: IO=$($currentStatus.Replica_IO_Running), SQL=$($currentStatus.Replica_SQL_Running), behind=$($currentStatus.Seconds_Behind_Source), read=$($currentStatus.Source_Log_File):$($currentStatus.Read_Source_Log_Pos), exec=$($currentStatus.Relay_Source_Log_File):$($currentStatus.Exec_Source_Log_Pos)"
    }

    $targetHostLiteral = ConvertTo-SqlStringLiteral -Value $normalizedTargetHost
    $safeLogFileLiteral = ConvertTo-SqlStringLiteral -Value $currentStatus.Relay_Source_Log_File
    $safeLogPos = [long]$currentStatus.Exec_Source_Log_Pos
    $switchSql = "STOP REPLICA; RESET REPLICA; CHANGE REPLICATION SOURCE TO SOURCE_HOST=$targetHostLiteral, SOURCE_PORT=$TargetPort, SOURCE_LOG_FILE=$safeLogFileLiteral, SOURCE_LOG_POS=$safeLogPos; START REPLICA;"
    [void](Invoke-MySql -User $dbaUser -Password $dbaPassword -Sql $switchSql)
    Start-Sleep -Seconds $ValidationDelaySeconds

    $replicaStatusRaw = Invoke-MySql -User $dbaUser -Password $dbaPassword -Sql 'SHOW REPLICA STATUS'
    $replicaStatus = $replicaStatusRaw | ConvertFrom-Csv -Delimiter "`t"
    $switchHealthy = (
        $replicaStatus.Source_Host -eq $normalizedTargetHost -and
        [int]$replicaStatus.Source_Port -eq $TargetPort -and
        $replicaStatus.Replica_IO_Running -eq 'Yes' -and
        $replicaStatus.Replica_SQL_Running -eq 'Yes' -and
        $replicaStatus.Last_IO_Errno -eq '0' -and
        $replicaStatus.Last_SQL_Errno -eq '0'
    )
    if (-not $switchHealthy) {
        throw "Replica endpoint switch did not become healthy: IO=$($replicaStatus.Replica_IO_Running), SQL=$($replicaStatus.Replica_SQL_Running), IO errno=$($replicaStatus.Last_IO_Errno), SQL errno=$($replicaStatus.Last_SQL_Errno)"
    }

    $plan.status = 'switched-and-healthy'
    $plan.io = $replicaStatus.Replica_IO_Running
    $plan.sql = $replicaStatus.Replica_SQL_Running
    $plan.seconds_behind_source = $replicaStatus.Seconds_Behind_Source
    $plan.read_source_log_pos = $replicaStatus.Read_Source_Log_Pos
    $plan.exec_source_log_pos = $replicaStatus.Exec_Source_Log_Pos
    $plan | ConvertTo-Json -Compress
}
catch {
    throw
}
finally {
    if ($plainCredential) {
        [Array]::Clear($plainCredential, 0, $plainCredential.Length)
    }
    $credentialText = $null
    $dbaPassword = $null
    $sourcePassword = $null
    if ($sourceMetadata -and $sourceMetadata.Count -ge 2) {
        $sourceMetadata[1] = $null
    }
}
