[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$DumpPath,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Fa-f0-9]{64}$')]
    [string]$ExpectedSha256,

    [string]$SourceHost = '192.168.110.180',

    [ValidateRange(1, 65535)]
    [int]$SourcePort = 3306,

    [ValidateRange(1, 10000)]
    [int]$ExpectedTableCount = 62,

    [string]$BlueTaskName = 'Takealot Blue Read Only ERP',

    [ValidateRange(1, 65535)]
    [int]$BluePort = 8502,

    [switch]$Apply,

    [string]$ExpectedComputerName = 'LAPTOP-2T5MN8EU',

    [ValidateRange(1, 4294967295)]
    [long]$ExpectedReplicaServerId = 2,

    [string]$ExpectedSourceHostName = 'DESKTOP-NTRMANG',

    [ValidateRange(1, 4294967295)]
    [long]$ExpectedSourceServerId = 1,

    [string]$ExpectedDataDirectory = 'D:\TakealotMySQLReplica\data\',

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

function Wait-BluePortClosed {
    param([int]$Port, [int]$TimeoutSeconds = 20)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if (-not (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Wait-BluePortOpen {
    param([int]$Port, [int]$TimeoutSeconds = 30)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    return $false
}

foreach ($requiredPath in @($MySqlExe, $DbaCredentialPath, $DumpPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Required file not found: $requiredPath"
    }
}

$sourceAddress = $null
if (-not [Net.IPAddress]::TryParse($SourceHost, [ref]$sourceAddress)) {
    throw 'SourceHost must be a literal IPv4 or IPv6 address.'
}
$normalizedSourceHost = $sourceAddress.ToString()
$resolvedDumpPath = (Resolve-Path -LiteralPath $DumpPath).Path
if ([IO.Path]::GetExtension($resolvedDumpPath) -ne '.sql') {
    throw "DumpPath must be a .sql file: $resolvedDumpPath"
}

$actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $resolvedDumpPath).Hash
if ($actualHash -ne $ExpectedSha256.ToUpperInvariant()) {
    throw "Seed hash mismatch: expected $ExpectedSha256, actual $actualHash"
}
$dumpTail = Get-Content -LiteralPath $resolvedDumpPath -Tail 8
if (-not ($dumpTail -match '^-- Dump completed on ')) {
    throw "Seed is missing the mysqldump completion marker: $resolvedDumpPath"
}
$coordinateMatchInfo = Select-String `
    -LiteralPath $resolvedDumpPath `
    -Pattern '^-- CHANGE .* (?:SOURCE|MASTER)_LOG_FILE=' `
    -List
if (-not $coordinateMatchInfo) {
    throw "Seed is missing source coordinates: $resolvedDumpPath"
}
$coordinateMatch = [regex]::Match(
    $coordinateMatchInfo.Line,
    "(?:SOURCE|MASTER)_LOG_FILE='(?<file>[^']+)',\s+(?:SOURCE|MASTER)_LOG_POS=(?<pos>[0-9]+)"
)
if (-not $coordinateMatch.Success) {
    throw "Unable to parse source coordinates: $($coordinateMatchInfo.Line)"
}
$sourceLogFile = $coordinateMatch.Groups['file'].Value
$sourceLogPos = [long]$coordinateMatch.Groups['pos'].Value

Add-Type -AssemblyName System.Security
$protectedCredential = [IO.File]::ReadAllBytes($DbaCredentialPath)
$plainCredential = [Security.Cryptography.ProtectedData]::Unprotect(
    $protectedCredential,
    $null,
    [Security.Cryptography.DataProtectionScope]::LocalMachine
)
$credentialParts = [Text.Encoding]::UTF8.GetString($plainCredential) -split "`r?`n", 2
if ($credentialParts.Count -ne 2) {
    throw 'DBA credential payload is invalid.'
}

$dbaUser = $credentialParts[0].Trim()
$dbaPassword = $credentialParts[1].Trim()
$sourceMetadata = $null
$sourcePassword = $null
$blueWasRunning = $false
$blueStopped = $false
$writeModeEnabled = $false
$schemaDropped = $false
$importSucceeded = $false
$replicationStarted = $false

try {
    $guardOutput = @(
        Invoke-MySql `
            -User $dbaUser `
            -Password $dbaPassword `
            -Sql 'SELECT @@hostname,@@server_id,@@datadir,@@read_only,@@super_read_only;' `
            -SkipColumnNames
    )
    $guard = $guardOutput[0] -split "`t"
    if (
        $guard.Count -ne 5 -or
        $guard[0] -ne $ExpectedComputerName -or
        [long]$guard[1] -ne $ExpectedReplicaServerId -or
        $guard[2] -ne $ExpectedDataDirectory -or
        $guard[3] -ne '1' -or
        $guard[4] -ne '1'
    ) {
        throw "Replica safety guard rejected target: $($guard -join ',')"
    }

    $sourceMetadataOutput = @(
        Invoke-MySql `
            -User $dbaUser `
            -Password $dbaPassword `
            -Sql "SELECT User_name,User_password FROM mysql.slave_master_info WHERE Channel_name='';" `
            -SkipColumnNames
    )
    $sourceMetadata = $sourceMetadataOutput[0] -split "`t", 2
    if ($sourceMetadata.Count -ne 2 -or $sourceMetadata[0] -ne 'takealot_backup' -or -not $sourceMetadata[1]) {
        throw 'Stored replication backup credential is incomplete.'
    }
    $sourceUser = $sourceMetadata[0]
    $sourcePassword = $sourceMetadata[1]

    $sourceCheck = @(
        Invoke-MySql `
            -User $sourceUser `
            -Password $sourcePassword `
            -Sql "SELECT @@hostname,@@server_id,@@read_only,CURRENT_USER(); SHOW STATUS LIKE 'Ssl_cipher'; SHOW BINARY LOGS;" `
            -HostName $normalizedSourceHost `
            -Port $SourcePort `
            -SkipColumnNames `
            -RequireTls
    )
    $sourceIdentity = $sourceCheck[0] -split "`t"
    $sourceTls = $sourceCheck[1] -split "`t", 2
    $matchingBinlog = @($sourceCheck | Where-Object { $_ -like "$sourceLogFile`t*" })
    if (
        $sourceIdentity.Count -ne 4 -or
        $sourceIdentity[0] -ne $ExpectedSourceHostName -or
        [long]$sourceIdentity[1] -ne $ExpectedSourceServerId -or
        $sourceIdentity[2] -ne '0' -or
        $sourceTls.Count -ne 2 -or
        $sourceTls[0] -ne 'Ssl_cipher' -or
        [string]::IsNullOrWhiteSpace($sourceTls[1]) -or
        $matchingBinlog.Count -ne 1 -or
        [long](($matchingBinlog[0] -split "`t")[1]) -lt $sourceLogPos
    ) {
        throw 'Source identity, TLS, or seed binlog availability guard failed.'
    }

    $blueTask = Get-ScheduledTask -TaskName $BlueTaskName -ErrorAction Stop
    $blueWasRunning = $blueTask.State -eq 'Running'
    $plan = [ordered]@{
        computer = $guard[0]
        replica_server_id = [long]$guard[1]
        data_directory = $guard[2]
        dump_path = $resolvedDumpPath
        dump_sha256 = $actualHash
        source_endpoint = "${normalizedSourceHost}:$SourcePort"
        source_log_file = $sourceLogFile
        source_log_pos = $sourceLogPos
        source_current_user = $sourceIdentity[3]
        blue_task = $BlueTaskName
        blue_was_running = $blueWasRunning
        destructive_target = 'takealot_ops on LAPTOP-2T5MN8EU only'
        apply = [bool]$Apply
    }
    if (-not $Apply) {
        $plan | ConvertTo-Json -Compress
        return
    }

    if ($blueWasRunning) {
        Stop-ScheduledTask -TaskName $BlueTaskName
        if (-not (Wait-BluePortClosed -Port $BluePort)) {
            throw "Blue port $BluePort remained open; refusing to drop the replica schema."
        }
        $blueStopped = $true
    }
    elseif (Get-NetTCPConnection -State Listen -LocalPort $BluePort -ErrorAction SilentlyContinue) {
        throw "Blue port $BluePort is open outside the expected scheduled task."
    }

    [void](Invoke-MySql -User $dbaUser -Password $dbaPassword -Sql 'STOP REPLICA; SET GLOBAL super_read_only=OFF; SET GLOBAL read_only=OFF;')
    $writeModeEnabled = $true
    [void](Invoke-MySql -User $dbaUser -Password $dbaPassword -Sql "DROP DATABASE IF EXISTS takealot_ops; CREATE DATABASE takealot_ops CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;")
    $schemaDropped = $true

    $previousPassword = $env:MYSQL_PWD
    $env:MYSQL_PWD = $dbaPassword
    try {
        $processInfo = [Diagnostics.ProcessStartInfo]::new()
        $processInfo.FileName = $MySqlExe
        $processInfo.Arguments = "--host=127.0.0.1 --port=3306 --user=$dbaUser --binary-mode=1 --max-allowed-packet=1G --database=takealot_ops"
        $processInfo.UseShellExecute = $false
        $processInfo.CreateNoWindow = $true
        $processInfo.RedirectStandardInput = $true
        $processInfo.RedirectStandardOutput = $true
        $processInfo.RedirectStandardError = $true

        $importProcess = [Diagnostics.Process]::new()
        $importProcess.StartInfo = $processInfo
        [void]$importProcess.Start()
        $stdoutTask = $importProcess.StandardOutput.ReadToEndAsync()
        $stderrTask = $importProcess.StandardError.ReadToEndAsync()
        $dumpStream = [IO.File]::OpenRead($resolvedDumpPath)
        try {
            $dumpStream.CopyTo($importProcess.StandardInput.BaseStream)
            $importProcess.StandardInput.Close()
            $importProcess.WaitForExit()
        }
        finally {
            $dumpStream.Dispose()
        }
        $importStdout = $stdoutTask.Result
        $importStderr = $stderrTask.Result
        if ($importProcess.ExitCode -ne 0) {
            throw "Seed import failed with exit code $($importProcess.ExitCode): $importStderr"
        }
        $importProcess.Dispose()
    }
    finally {
        if ($null -eq $previousPassword) {
            Remove-Item Env:MYSQL_PWD -ErrorAction SilentlyContinue
        }
        else {
            $env:MYSQL_PWD = $previousPassword
        }
    }
    $importSucceeded = $true

    $tableCountOutput = @(
        Invoke-MySql `
            -User $dbaUser `
            -Password $dbaPassword `
            -Sql "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='takealot_ops' AND table_type='BASE TABLE';" `
            -SkipColumnNames
    )
    $tableCount = [int]$tableCountOutput[0]
    if ($tableCount -ne $ExpectedTableCount) {
        throw "Imported table count mismatch: expected $ExpectedTableCount, actual $tableCount"
    }

    $sourceHostLiteral = ConvertTo-SqlStringLiteral -Value $normalizedSourceHost
    $sourceLogFileLiteral = ConvertTo-SqlStringLiteral -Value $sourceLogFile
    $replicationSql = "RESET REPLICA; CHANGE REPLICATION SOURCE TO SOURCE_HOST=$sourceHostLiteral, SOURCE_PORT=$SourcePort, SOURCE_LOG_FILE=$sourceLogFileLiteral, SOURCE_LOG_POS=$sourceLogPos; SET GLOBAL read_only=ON; SET GLOBAL super_read_only=ON; START REPLICA;"
    [void](Invoke-MySql -User $dbaUser -Password $dbaPassword -Sql $replicationSql)
    $writeModeEnabled = $false
    $replicationStarted = $true
    Start-Sleep -Seconds 15

    $replicaStatusRaw = Invoke-MySql -User $dbaUser -Password $dbaPassword -Sql 'SHOW REPLICA STATUS'
    $replicaStatus = $replicaStatusRaw | ConvertFrom-Csv -Delimiter "`t"
    if (
        $replicaStatus.Source_Host -ne $normalizedSourceHost -or
        [int]$replicaStatus.Source_Port -ne $SourcePort -or
        $replicaStatus.Replica_IO_Running -ne 'Yes' -or
        $replicaStatus.Replica_SQL_Running -ne 'Yes' -or
        $replicaStatus.Last_IO_Errno -ne '0' -or
        $replicaStatus.Last_SQL_Errno -ne '0'
    ) {
        throw "Reseeded replica is unhealthy: IO=$($replicaStatus.Replica_IO_Running), SQL=$($replicaStatus.Replica_SQL_Running), IO errno=$($replicaStatus.Last_IO_Errno), SQL errno=$($replicaStatus.Last_SQL_Errno)"
    }

    if ($blueWasRunning) {
        Start-ScheduledTask -TaskName $BlueTaskName
        if (-not (Wait-BluePortOpen -Port $BluePort)) {
            throw "Replica recovered but blue port $BluePort did not reopen."
        }
        $blueStopped = $false
    }

    $plan.status = 'reseeded-and-replication-healthy'
    $plan.imported_table_count = $tableCount
    $plan.replica_io = $replicaStatus.Replica_IO_Running
    $plan.replica_sql = $replicaStatus.Replica_SQL_Running
    $plan.seconds_behind_source = $replicaStatus.Seconds_Behind_Source
    $plan.read_source_log_pos = $replicaStatus.Read_Source_Log_Pos
    $plan.exec_source_log_pos = $replicaStatus.Exec_Source_Log_Pos
    $plan.blue_port_open = [bool](Get-NetTCPConnection -State Listen -LocalPort $BluePort -ErrorAction SilentlyContinue)
    $plan | ConvertTo-Json -Compress
}
finally {
    if ($writeModeEnabled) {
        try {
            [void](Invoke-MySql -User $dbaUser -Password $dbaPassword -Sql 'SET GLOBAL read_only=ON; SET GLOBAL super_read_only=ON;')
        }
        catch {
            Write-Error "Failed to restore replica read-only mode: $($_.Exception.Message)"
        }
    }
    if ($plainCredential) {
        [Array]::Clear($plainCredential, 0, $plainCredential.Length)
    }
    $dbaPassword = $null
    $sourcePassword = $null
    if ($sourceMetadata -and $sourceMetadata.Count -ge 2) {
        $sourceMetadata[1] = $null
    }
    if ($schemaDropped -and -not $importSucceeded) {
        Write-Warning 'Laptop takealot_ops was dropped but the seed import did not finish. Keep blue stopped and rerun this script with the same validated seed.'
    }
    if ($importSucceeded -and -not $replicationStarted) {
        Write-Warning 'Seed import completed, but replication did not start. Keep blue stopped and repair replication before serving it.'
    }
    if ($blueStopped) {
        Write-Warning 'Blue ERP remains stopped because recovery did not reach the healthy endpoint.'
    }
}
