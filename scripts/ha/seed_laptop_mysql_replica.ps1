[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ArchivePath,

    [Parameter(Mandatory = $false)]
    [switch]$ReplaceTakealotOps,

    [Parameter(Mandatory = $false)]
    [switch]$Execute
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ExpectedComputerName = 'LAPTOP-2T5MN8EU'
$ReplicaRoot = 'D:\TakealotMySQLReplica'
$SeedRoot = Join-Path $ReplicaRoot 'seed'
$QuarantineRoot = Join-Path $ReplicaRoot 'quarantine'
$CredentialPath = Join-Path $ReplicaRoot 'secrets\mysql-dba.dpapi'
$MySqlRoot = Join-Path $ReplicaRoot 'mysql-8.0.46-winx64'
$MySqlClientPath = Join-Path $MySqlRoot 'bin\mysql.exe'
$MySqlCheckPath = Join-Path $MySqlRoot 'bin\mysqlcheck.exe'
$DatabaseName = 'takealot_ops'

function Get-NormalizedPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return [IO.Path]::GetFullPath($Path).TrimEnd('\')
}

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
    param(
        [Parameter(Mandatory = $true)][string]$Sql,
        [Parameter(Mandatory = $false)][string]$Database,
        [Parameter(Mandatory = $false)][string]$InitCommand
    )

    $Arguments = @(
        '--protocol=TCP',
        '--host=127.0.0.1',
        '--port=3306',
        "--user=$script:DbaUser",
        '--batch',
        '--raw',
        '--skip-column-names'
    )
    if ($Database) {
        $Arguments += "--database=$Database"
    }
    if ($InitCommand) {
        $Arguments += "--init-command=$InitCommand"
    }
    $Arguments += "--execute=$Sql"

    $SavedErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $Output = @(& $MySqlClientPath @Arguments 2>&1)
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

if ($env:COMPUTERNAME -cne $ExpectedComputerName) {
    throw "Safety stop: expected $ExpectedComputerName, got $env:COMPUTERNAME."
}
$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$Principal = New-Object Security.Principal.WindowsPrincipal($Identity)
if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator privileges are required on the laptop.'
}

$Archive = Get-NormalizedPath -Path $ArchivePath
$NormalizedSeedRoot = Get-NormalizedPath -Path $SeedRoot
if (-not $Archive.StartsWith($NormalizedSeedRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw "Safety stop: seed archive is outside $NormalizedSeedRoot."
}
if (-not $Archive.EndsWith('.sql.gz', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Safety stop: seed archive is not a .sql.gz file.'
}
$SidecarManifestPath = "$Archive.json"
foreach ($Path in @($Archive, $SidecarManifestPath, $CredentialPath, $MySqlClientPath, $MySqlCheckPath)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required file not found: $Path"
    }
}

$SidecarManifest = Get-Content -LiteralPath $SidecarManifestPath -Raw | ConvertFrom-Json
if (
    [string]$SidecarManifest.backend -cne 'mysql' -or
    [string]$SidecarManifest.database -cne $DatabaseName -or
    [string]$SidecarManifest.archive -cne [IO.Path]::GetFileName($Archive)
) {
    throw 'The seed manifest does not describe the expected MySQL database archive.'
}
$ArchiveHash = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($ArchiveHash -cne ([string]$SidecarManifest.sha256).ToLowerInvariant()) {
    throw 'The seed archive SHA256 does not match its manifest.'
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

$RawSqlPath = Join-Path $SeedRoot ([IO.Path]::GetFileNameWithoutExtension($Archive))
$Timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$ExecutionManifestPath = Join-Path $QuarantineRoot "seed-import-$Timestamp.json"
$ReadOnlyDisabled = $false
$ImportSucceeded = $false
$ExecutionManifest = [ordered]@{
    computer = $env:COMPUTERNAME
    started_at = (Get-Date).ToString('o')
    status = 'preflight'
    archive = $Archive
    archive_sha256 = $ArchiveHash
    expected_raw_bytes = [long]$SidecarManifest.raw_bytes
    binlog_file = [string]$SidecarManifest.binlog.file
    binlog_position = [long]$SidecarManifest.binlog.position
    database = $DatabaseName
}

try {
    $Variables = @(Invoke-MySql -Sql (
        "SELECT CONCAT(@@hostname,'|',VERSION(),'|',@@server_id,'|'," +
        "@@gtid_mode,'|',@@enforce_gtid_consistency,'|',@@read_only,'|'," +
        "@@super_read_only,'|',@@log_bin,'|',@@log_replica_updates);"
    ))
    $NonSystemSchemas = @(
        Invoke-MySql -Sql (
            "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA " +
            "WHERE SCHEMA_NAME NOT IN ('information_schema','mysql','performance_schema','sys') " +
            'ORDER BY SCHEMA_NAME;'
        )
    )
    $ReplicaChannelCount = [int](@(
        Invoke-MySql -Sql 'SELECT COUNT(*) FROM performance_schema.replication_connection_status;'
    )[0])
    if ($ReplicaChannelCount -ne 0) {
        throw 'Safety stop: the laptop already has a replication channel.'
    }
    $UnexpectedSchemas = @($NonSystemSchemas | Where-Object { $_ -cne $DatabaseName })
    if ($UnexpectedSchemas.Count -gt 0) {
        throw "Safety stop: unexpected laptop databases exist: $($UnexpectedSchemas -join ', ')."
    }
    if (($NonSystemSchemas -contains $DatabaseName) -and -not $ReplaceTakealotOps) {
        throw 'Safety stop: takealot_ops exists; use -ReplaceTakealotOps to replace it explicitly.'
    }

    $ExecutionManifest.mysql_variables = $Variables
    $ExecutionManifest.schemas_before = $NonSystemSchemas
    $ExecutionManifest.replica_channels_before = $ReplicaChannelCount
    if (-not $Execute) {
        $ExecutionManifest.status = 'preflight_ok'
        $ExecutionManifest | ConvertTo-Json -Depth 8
        exit 0
    }

    New-Item -ItemType Directory -Path $QuarantineRoot -Force | Out-Null
    $ExecutionManifest.status = 'starting'
    Write-JsonFile -Path $ExecutionManifestPath -Value $ExecutionManifest

    Invoke-MySql -Sql (
        'SET SESSION sql_log_bin=0; ' +
        'SET GLOBAL super_read_only=OFF; ' +
        'SET GLOBAL read_only=OFF; ' +
        "DROP DATABASE IF EXISTS $DatabaseName; " +
        "CREATE DATABASE $DatabaseName CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
    ) | Out-Null
    $ReadOnlyDisabled = $true

    if (Test-Path -LiteralPath $RawSqlPath) {
        Remove-Item -LiteralPath $RawSqlPath -Force
    }
    $CompressedStream = [IO.File]::OpenRead($Archive)
    try {
        $GzipStream = New-Object IO.Compression.GZipStream(
            $CompressedStream,
            [IO.Compression.CompressionMode]::Decompress
        )
        try {
            $RawStream = [IO.File]::Create($RawSqlPath)
            try {
                $GzipStream.CopyTo($RawStream)
            }
            finally {
                $RawStream.Dispose()
            }
        }
        finally {
            $GzipStream.Dispose()
        }
    }
    finally {
        $CompressedStream.Dispose()
    }
    $ActualRawBytes = (Get-Item -LiteralPath $RawSqlPath).Length
    if ($ActualRawBytes -ne [long]$SidecarManifest.raw_bytes) {
        throw "Decompressed SQL size mismatch: $ActualRawBytes."
    }

    $SourcePath = $RawSqlPath.Replace('\', '/')
    Invoke-MySql `
        -Database $DatabaseName `
        -InitCommand 'SET SESSION sql_log_bin=0' `
        -Sql "source $SourcePath" | Out-Null

    Invoke-MySql -Sql 'SET GLOBAL read_only=ON; SET GLOBAL super_read_only=ON;' | Out-Null
    $ReadOnlyDisabled = $false

    $TableCount = [int](@(
        Invoke-MySql -Sql (
            "SELECT COUNT(*) FROM information_schema.TABLES " +
            "WHERE TABLE_SCHEMA='$DatabaseName' AND TABLE_TYPE='BASE TABLE';"
        )
    )[0])
    if ($TableCount -le 0) {
        throw 'The imported database contains no base tables.'
    }

    $SavedErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $CheckOutput = @(
            & $MySqlCheckPath `
                --protocol=TCP `
                --host=127.0.0.1 `
                --port=3306 `
                "--user=$script:DbaUser" `
                --check `
                --databases $DatabaseName 2>&1
        )
        $CheckExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $SavedErrorActionPreference
    }
    if ($CheckExitCode -ne 0) {
        throw "mysqlcheck failed with exit code $CheckExitCode."
    }

    $ExecutionManifest.status = 'success'
    $ExecutionManifest.completed_at = (Get-Date).ToString('o')
    $ExecutionManifest.actual_raw_bytes = $ActualRawBytes
    $ExecutionManifest.table_count = $TableCount
    $ExecutionManifest.mysqlcheck_exit_code = $CheckExitCode
    $ExecutionManifest.read_only = [int](@(
        Invoke-MySql -Sql 'SELECT @@read_only;'
    )[0])
    $ExecutionManifest.super_read_only = [int](@(
        Invoke-MySql -Sql 'SELECT @@super_read_only;'
    )[0])
    Write-JsonFile -Path $ExecutionManifestPath -Value $ExecutionManifest
    $ImportSucceeded = $true

    [pscustomobject]@{
        computer = $env:COMPUTERNAME
        status = 'success'
        database = $DatabaseName
        table_count = $TableCount
        read_only = $ExecutionManifest.read_only
        super_read_only = $ExecutionManifest.super_read_only
        manifest = $ExecutionManifestPath
    } | ConvertTo-Json -Depth 6
}
catch {
    $Failure = $_
    try {
        $ExecutionManifest.status = 'failed'
        $ExecutionManifest.failed_at = (Get-Date).ToString('o')
        $ExecutionManifest.failure = $Failure.Exception.Message
        Write-JsonFile -Path $ExecutionManifestPath -Value $ExecutionManifest
    }
    catch {
    }
    throw $Failure
}
finally {
    if ($ReadOnlyDisabled) {
        try {
            Invoke-MySql -Sql (
                'SET GLOBAL read_only=ON; SET GLOBAL super_read_only=ON;'
            ) | Out-Null
        }
        catch {
        }
    }
    if (Test-Path -LiteralPath $RawSqlPath -PathType Leaf) {
        Remove-Item -LiteralPath $RawSqlPath -Force -ErrorAction SilentlyContinue
    }
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
    if (-not $ImportSucceeded -and $Execute) {
        Write-Error "Laptop seed import failed; inspect $ExecutionManifestPath."
    }
}
