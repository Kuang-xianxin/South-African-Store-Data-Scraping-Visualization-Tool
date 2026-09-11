[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [switch]$ReadSourcePasswordFromStdin,

    [Parameter(Mandatory = $false)]
    [switch]$Execute
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ExpectedComputerName = 'LAPTOP-2T5MN8EU'
$ReplicaRoot = 'D:\TakealotMySQLReplica'
$CredentialPath = Join-Path $ReplicaRoot 'secrets\mysql-dba.dpapi'
$MySqlClientPath = Join-Path $ReplicaRoot 'mysql-8.0.46-winx64\bin\mysql.exe'
$SeedManifestPath = Join-Path $ReplicaRoot (
    'seed\takealot-20260904-013752-015723.sql.gz.json'
)
$QuarantineRoot = Join-Path $ReplicaRoot 'quarantine'
$SourceHost = '100.70.103.11'
$SourcePort = 13306
$SourceUser = 'takealot_backup'
$CatchUpTimeoutSeconds = 600

function Write-JsonFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value
    )
    [IO.File]::WriteAllText(
        $Path,
        (($Value | ConvertTo-Json -Depth 10) + [Environment]::NewLine),
        (New-Object Text.UTF8Encoding($false))
    )
}

function Invoke-MySqlWithPassword {
    param(
        [Parameter(Mandatory = $true)][string]$Password,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    $PreviousPassword = $env:MYSQL_PWD
    $env:MYSQL_PWD = $Password
    $SavedErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $Output = @(& $MySqlClientPath @Arguments 2>&1)
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
        $Message = @($Output | ForEach-Object { [string]$_ }) -join [Environment]::NewLine
        throw "mysql.exe failed with exit code $ExitCode. $Message"
    }
    return @($Output | ForEach-Object { [string]$_ })
}

function Invoke-LocalMySql {
    param(
        [Parameter(Mandatory = $true)][string]$Sql,
        [Parameter(Mandatory = $false)][switch]$IncludeColumnNames
    )

    $Arguments = @(
        '--protocol=TCP',
        '--host=127.0.0.1',
        '--port=3306',
        "--user=$script:DbaUser",
        '--batch',
        '--raw'
    )
    if (-not $IncludeColumnNames) {
        $Arguments += '--skip-column-names'
    }
    $Arguments += "--execute=$Sql"
    return @(
        Invoke-MySqlWithPassword -Password $script:DbaPassword -Arguments $Arguments
    )
}

function Invoke-SourceMySql {
    param([Parameter(Mandatory = $true)][string]$Sql)

    $Arguments = @(
        '--protocol=TCP',
        "--host=$SourceHost",
        "--port=$SourcePort",
        '--ssl-mode=REQUIRED',
        '--connect-timeout=10',
        "--user=$SourceUser",
        '--batch',
        '--raw',
        '--skip-column-names',
        "--execute=$Sql"
    )
    return @(
        Invoke-MySqlWithPassword -Password $script:SourcePassword -Arguments $Arguments
    )
}

function Get-FirstValue {
    param([Parameter(Mandatory = $true)][string[]]$Rows)
    if ($Rows.Count -lt 1) {
        return ''
    }
    return [string]$Rows[0]
}

function Get-ReplicaStatus {
    $Lines = @(Invoke-LocalMySql -Sql 'SHOW REPLICA STATUS;' -IncludeColumnNames)
    if ($Lines.Count -lt 2) {
        return $null
    }
    $Headers = $Lines[0].Split([char]9)
    $Values = $Lines[1].Split([char]9)
    $Status = [ordered]@{}
    for ($Index = 0; $Index -lt $Headers.Count; $Index++) {
        $Value = if ($Index -lt $Values.Count) { $Values[$Index] } else { '' }
        $Status[$Headers[$Index]] = $Value
    }
    return [pscustomobject]$Status
}

function Invoke-LocalSqlFromStandardInput {
    param([Parameter(Mandatory = $true)][string]$Sql)

    $StartInfo = New-Object Diagnostics.ProcessStartInfo
    $StartInfo.FileName = $MySqlClientPath
    $StartInfo.Arguments = (
        '--protocol=TCP --host=127.0.0.1 --port=3306 ' +
        "--user=$script:DbaUser --batch --raw"
    )
    $StartInfo.UseShellExecute = $false
    $StartInfo.CreateNoWindow = $true
    $StartInfo.RedirectStandardInput = $true
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true
    $StartInfo.EnvironmentVariables['MYSQL_PWD'] = $script:DbaPassword

    $Process = New-Object Diagnostics.Process
    $Process.StartInfo = $StartInfo
    if (-not $Process.Start()) {
        throw 'Failed to start mysql.exe for standard-input configuration.'
    }
    try {
        $Process.StandardInput.WriteLine($Sql)
        $Process.StandardInput.Close()
        $StandardOutput = $Process.StandardOutput.ReadToEnd()
        $StandardError = $Process.StandardError.ReadToEnd()
        $Process.WaitForExit()
        if ($Process.ExitCode -ne 0) {
            throw "mysql.exe failed with exit code $($Process.ExitCode). $StandardError"
        }
        return $StandardOutput
    }
    finally {
        $Process.Dispose()
    }
}

if ($env:COMPUTERNAME -cne $ExpectedComputerName) {
    throw "Safety stop: expected $ExpectedComputerName, got $env:COMPUTERNAME."
}
$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$Principal = New-Object Security.Principal.WindowsPrincipal($Identity)
if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator privileges are required on the laptop.'
}
foreach ($Path in @($CredentialPath, $MySqlClientPath, $SeedManifestPath)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required file not found: $Path"
    }
}
if (-not $ReadSourcePasswordFromStdin) {
    throw 'The source password must be supplied through standard input.'
}
$script:SourcePassword = [Console]::In.ReadToEnd().TrimEnd([char]13, [char]10)
if ([string]::IsNullOrWhiteSpace($script:SourcePassword)) {
    throw 'The source password received on standard input is empty.'
}
if ($script:SourcePassword.Length -gt 32) {
    throw 'The source password exceeds the MySQL replication limit of 32 characters.'
}
if ($script:SourcePassword -match "['\\\r\n]") {
    throw 'The source password contains characters unsupported by this bootstrap path.'
}

Add-Type -AssemblyName System.Security
$ProtectedDbaBytes = [IO.File]::ReadAllBytes($CredentialPath)
$PlainDbaBytes = [Security.Cryptography.ProtectedData]::Unprotect(
    $ProtectedDbaBytes,
    $null,
    [Security.Cryptography.DataProtectionScope]::LocalMachine
)
$DbaSecret = [Text.Encoding]::UTF8.GetString($PlainDbaBytes)
$DbaParts = $DbaSecret -split [char]10, 2
if ($DbaParts.Count -ne 2) {
    throw 'The laptop DBA credential has an unexpected format.'
}
$script:DbaUser = $DbaParts[0].Trim()
$script:DbaPassword = $DbaParts[1].Trim()

$SeedManifest = Get-Content -LiteralPath $SeedManifestPath -Raw | ConvertFrom-Json
$SourceLogFile = [string]$SeedManifest.binlog.file
$SourceLogPosition = [long]$SeedManifest.binlog.position
$Timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$ManifestPath = Join-Path $QuarantineRoot "replication-configuration-$Timestamp.json"
$ChannelConfigured = $false
$ExecutionSucceeded = $false
$Manifest = [ordered]@{
    computer = $env:COMPUTERNAME
    started_at = (Get-Date).ToString('o')
    status = 'preflight'
    source_host = $SourceHost
    source_port = $SourcePort
    source_user = $SourceUser
    source_ssl = $true
    source_log_file = $SourceLogFile
    source_log_position = $SourceLogPosition
    password_recorded = $false
}

try {
    $LocalVariables = Get-FirstValue -Rows @(
        Invoke-LocalMySql -Sql (
            "SELECT CONCAT(@@hostname,'|',VERSION(),'|',@@server_id,'|'," +
            "@@gtid_mode,'|',@@enforce_gtid_consistency,'|',@@read_only,'|'," +
            "@@super_read_only,'|',@@log_bin,'|',@@log_replica_updates,'|'," +
            "@@bind_address);"
        )
    )
    $LocalParts = $LocalVariables.Split('|')
    if (
        $LocalParts.Count -ne 10 -or
        $LocalParts[0] -cne $ExpectedComputerName -or
        $LocalParts[2] -cne '2' -or
        $LocalParts[3] -cne 'OFF' -or
        $LocalParts[4] -cne 'OFF' -or
        $LocalParts[5] -cne '1' -or
        $LocalParts[6] -cne '1' -or
        $LocalParts[7] -cne '1' -or
        $LocalParts[8] -cne '1' -or
        $LocalParts[9] -cne '127.0.0.1'
    ) {
        throw "Safety stop: unexpected laptop MySQL state: $LocalVariables"
    }
    $LocalChannelCount = [int](Get-FirstValue -Rows @(
        Invoke-LocalMySql -Sql (
            'SELECT COUNT(*) FROM performance_schema.replication_connection_status;'
        )
    ))
    if ($LocalChannelCount -ne 0 -or $null -ne (Get-ReplicaStatus)) {
        throw 'Safety stop: the laptop already has a replication channel.'
    }
    $LocalTableCount = [int](Get-FirstValue -Rows @(
        Invoke-LocalMySql -Sql (
            "SELECT COUNT(*) FROM information_schema.TABLES " +
            "WHERE TABLE_SCHEMA='takealot_ops' AND TABLE_TYPE='BASE TABLE';"
        )
    ))
    if ($LocalTableCount -le 0) {
        throw 'Safety stop: the laptop seed database has no base tables.'
    }

    $SourceIdentity = Get-FirstValue -Rows @(
        Invoke-SourceMySql -Sql (
            "SELECT CONCAT(CURRENT_USER(),'|',@@hostname,'|',VERSION(),'|'," +
            "@@server_id,'|',@@gtid_mode,'|',@@binlog_format);"
        )
    )
    $SourceParts = $SourceIdentity.Split('|')
    if (
        $SourceParts.Count -ne 6 -or
        $SourceParts[0] -cne 'takealot_backup@localhost' -or
        $SourceParts[1] -cne 'DESKTOP-NTRMANG' -or
        $SourceParts[3] -cne '1' -or
        $SourceParts[4] -cne 'OFF' -or
        $SourceParts[5] -cne 'ROW'
    ) {
        throw "Safety stop: unexpected replication source identity: $SourceIdentity"
    }
    $SslCipherRow = Get-FirstValue -Rows @(
        Invoke-SourceMySql -Sql "SHOW STATUS LIKE 'Ssl_cipher';"
    )
    $SslParts = $SslCipherRow.Split([char]9)
    if ($SslParts.Count -lt 2 -or [string]::IsNullOrWhiteSpace($SslParts[1])) {
        throw 'Safety stop: the source probe did not negotiate MySQL TLS.'
    }
    $SourceBinaryLogs = @(Invoke-SourceMySql -Sql 'SHOW BINARY LOGS;')
    $SeedLogRow = @(
        $SourceBinaryLogs | Where-Object {
            $_.Split([char]9)[0] -ceq $SourceLogFile
        }
    )
    if ($SeedLogRow.Count -ne 1) {
        throw "Safety stop: seed binlog is not retained on the source: $SourceLogFile"
    }
    $SeedLogSize = [long]$SeedLogRow[0].Split([char]9)[1]
    if ($SeedLogSize -lt $SourceLogPosition) {
        throw 'Safety stop: seed binlog is shorter than the recorded position.'
    }
    $SourceStatus = Get-FirstValue -Rows @(
        Invoke-SourceMySql -Sql 'SHOW MASTER STATUS;'
    )
    $SourceStatusParts = $SourceStatus.Split([char]9)
    if ($SourceStatusParts.Count -lt 2) {
        throw 'Safety stop: source master status is unavailable.'
    }
    $CatchUpFile = $SourceStatusParts[0]
    $CatchUpPosition = [long]$SourceStatusParts[1]

    $Manifest.local_mysql = $LocalVariables
    $Manifest.local_table_count = $LocalTableCount
    $Manifest.source_identity = $SourceIdentity
    $Manifest.source_ssl_cipher = $SslParts[1]
    $Manifest.source_binlog_count = $SourceBinaryLogs.Count
    $Manifest.catch_up_file = $CatchUpFile
    $Manifest.catch_up_position = $CatchUpPosition
    if (-not $Execute) {
        $Manifest.status = 'preflight_ok'
        $Manifest | ConvertTo-Json -Depth 10
        exit 0
    }

    New-Item -ItemType Directory -Path $QuarantineRoot -Force | Out-Null
    $Manifest.status = 'starting'
    Write-JsonFile -Path $ManifestPath -Value $Manifest

    $ChangeSql = (
        'CHANGE REPLICATION SOURCE TO ' +
        "SOURCE_HOST='$SourceHost', " +
        "SOURCE_PORT=$SourcePort, " +
        "SOURCE_USER='$SourceUser', " +
        "SOURCE_PASSWORD='$script:SourcePassword', " +
        "SOURCE_LOG_FILE='$SourceLogFile', " +
        "SOURCE_LOG_POS=$SourceLogPosition, " +
        'SOURCE_AUTO_POSITION=0, ' +
        'SOURCE_SSL=1, ' +
        'SOURCE_SSL_VERIFY_SERVER_CERT=0, ' +
        'SOURCE_CONNECT_RETRY=10, ' +
        'SOURCE_HEARTBEAT_PERIOD=5; ' +
        'START REPLICA;'
    )
    Invoke-LocalSqlFromStandardInput -Sql $ChangeSql | Out-Null
    $ChangeSql = $null
    $ChannelConfigured = $true

    $WaitResult = Get-FirstValue -Rows @(
        Invoke-LocalMySql -Sql (
            "SELECT SOURCE_POS_WAIT('$CatchUpFile',$CatchUpPosition,$CatchUpTimeoutSeconds);"
        )
    )
    if ($WaitResult -eq '') {
        throw 'SOURCE_POS_WAIT returned NULL before the replica reached the target.'
    }
    if ([long]$WaitResult -lt 0) {
        throw (
            'The replica did not reach the recorded source position within ' +
            "$CatchUpTimeoutSeconds seconds."
        )
    }

    $ReplicaStatus = Get-ReplicaStatus
    if ($null -eq $ReplicaStatus) {
        throw 'SHOW REPLICA STATUS returned no row after START REPLICA.'
    }
    if (
        [string]$ReplicaStatus.Replica_IO_Running -cne 'Yes' -or
        [string]$ReplicaStatus.Replica_SQL_Running -cne 'Yes' -or
        [int]$ReplicaStatus.Last_IO_Errno -ne 0 -or
        [int]$ReplicaStatus.Last_SQL_Errno -ne 0
    ) {
        throw (
            'Replication threads are not healthy. IO=' +
            [string]$ReplicaStatus.Replica_IO_Running +
            ' SQL=' + [string]$ReplicaStatus.Replica_SQL_Running +
            ' LastIO=' + [string]$ReplicaStatus.Last_IO_Error +
            ' LastSQL=' + [string]$ReplicaStatus.Last_SQL_Error
        )
    }

    $Manifest.status = 'success'
    $Manifest.completed_at = (Get-Date).ToString('o')
    $Manifest.source_pos_wait_result = $WaitResult
    $Manifest.replica_io_running = [string]$ReplicaStatus.Replica_IO_Running
    $Manifest.replica_sql_running = [string]$ReplicaStatus.Replica_SQL_Running
    $Manifest.seconds_behind_source = [string]$ReplicaStatus.Seconds_Behind_Source
    $Manifest.source_log_file_current = [string]$ReplicaStatus.Source_Log_File
    $Manifest.read_source_log_pos = [string]$ReplicaStatus.Read_Source_Log_Pos
    $Manifest.relay_source_log_file = [string]$ReplicaStatus.Relay_Source_Log_File
    $Manifest.exec_source_log_pos = [string]$ReplicaStatus.Exec_Source_Log_Pos
    $Manifest.last_io_errno = [int]$ReplicaStatus.Last_IO_Errno
    $Manifest.last_sql_errno = [int]$ReplicaStatus.Last_SQL_Errno
    Write-JsonFile -Path $ManifestPath -Value $Manifest
    $ExecutionSucceeded = $true

    [pscustomobject]@{
        computer = $env:COMPUTERNAME
        status = 'success'
        replica_io_running = $Manifest.replica_io_running
        replica_sql_running = $Manifest.replica_sql_running
        seconds_behind_source = $Manifest.seconds_behind_source
        source_log_file = $Manifest.source_log_file_current
        read_source_log_pos = $Manifest.read_source_log_pos
        relay_source_log_file = $Manifest.relay_source_log_file
        exec_source_log_pos = $Manifest.exec_source_log_pos
        manifest = $ManifestPath
    } | ConvertTo-Json -Depth 8
}
catch {
    $Failure = $_
    try {
        $ReplicaStatus = Get-ReplicaStatus
        $Manifest.status = 'failed'
        $Manifest.failed_at = (Get-Date).ToString('o')
        $Manifest.failure = $Failure.Exception.Message
        if ($null -ne $ReplicaStatus) {
            $Manifest.replica_io_running = [string]$ReplicaStatus.Replica_IO_Running
            $Manifest.replica_sql_running = [string]$ReplicaStatus.Replica_SQL_Running
            $Manifest.last_io_errno = [int]$ReplicaStatus.Last_IO_Errno
            $Manifest.last_io_error = [string]$ReplicaStatus.Last_IO_Error
            $Manifest.last_sql_errno = [int]$ReplicaStatus.Last_SQL_Errno
            $Manifest.last_sql_error = [string]$ReplicaStatus.Last_SQL_Error
        }
        Write-JsonFile -Path $ManifestPath -Value $Manifest
    }
    catch {
    }
    throw $Failure
}
finally {
    $script:SourcePassword = $null
    $script:DbaPassword = $null
    $DbaSecret = $null
    if ($null -ne $PlainDbaBytes) {
        [Array]::Clear($PlainDbaBytes, 0, $PlainDbaBytes.Length)
    }
    if (-not $ExecutionSucceeded -and $Execute -and $ChannelConfigured) {
        Write-Error "Replication configuration failed; inspect $ManifestPath."
    }
}
