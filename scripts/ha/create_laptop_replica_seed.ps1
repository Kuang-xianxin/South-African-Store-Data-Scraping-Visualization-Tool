[CmdletBinding()]
param(
    [string]$SourceHost = '192.168.110.180',

    [ValidateRange(1, 65535)]
    [int]$SourcePort = 3306,

    [string]$OutputDirectory = 'D:\TakealotHA\staging',

    [string]$ExistingDumpPath,

    [string]$ExpectedComputerName = 'LAPTOP-2T5MN8EU',

    [ValidateRange(1, 4294967295)]
    [long]$ExpectedReplicaServerId = 2,

    [string]$ExpectedSourceHostName = 'DESKTOP-NTRMANG',

    [ValidateRange(1, 4294967295)]
    [long]$ExpectedSourceServerId = 1,

    [string]$MySqlExe = 'D:\TakealotMySQLReplica\mysql-8.0.46-winx64\bin\mysql.exe',

    [string]$MySqlDumpExe = 'D:\TakealotMySQLReplica\mysql-8.0.46-winx64\bin\mysqldump.exe',

    [string]$DbaCredentialPath = 'D:\TakealotMySQLReplica\secrets\mysql-dba.dpapi'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

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

foreach ($requiredPath in @($MySqlExe, $MySqlDumpExe, $DbaCredentialPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Required file not found: $requiredPath"
    }
}

$sourceAddress = $null
if (-not [Net.IPAddress]::TryParse($SourceHost, [ref]$sourceAddress)) {
    throw 'SourceHost must be a literal IPv4 or IPv6 address.'
}
$normalizedSourceHost = $sourceAddress.ToString()

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
$dumpPath = $null

try {
    $guardOutput = @(
        Invoke-MySql `
            -User $dbaUser `
            -Password $dbaPassword `
            -Sql 'SELECT @@hostname,@@server_id,@@read_only,@@super_read_only;' `
            -SkipColumnNames
    )
    $guard = $guardOutput[0] -split "`t"
    if (
        $guard.Count -ne 4 -or
        $guard[0] -ne $ExpectedComputerName -or
        [long]$guard[1] -ne $ExpectedReplicaServerId -or
        $guard[2] -ne '1' -or
        $guard[3] -ne '1'
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

    $identityOutput = @(
        Invoke-MySql `
            -User $sourceUser `
            -Password $sourcePassword `
            -Sql "SELECT @@hostname,@@server_id,@@read_only,CURRENT_USER(); SHOW STATUS LIKE 'Ssl_cipher';" `
            -HostName $normalizedSourceHost `
            -Port $SourcePort `
            -SkipColumnNames `
            -RequireTls
    )
    $identity = $identityOutput[0] -split "`t"
    $tls = $identityOutput[1] -split "`t", 2
    if (
        $identity.Count -ne 4 -or
        $identity[0] -ne $ExpectedSourceHostName -or
        [long]$identity[1] -ne $ExpectedSourceServerId -or
        $identity[2] -ne '0' -or
        $tls.Count -ne 2 -or
        $tls[0] -ne 'Ssl_cipher' -or
        [string]::IsNullOrWhiteSpace($tls[1])
    ) {
        throw "Source identity or TLS guard failed: $($identityOutput -join ' | ')"
    }

    if ($ExistingDumpPath) {
        $dumpPath = (Resolve-Path -LiteralPath $ExistingDumpPath -ErrorAction Stop).Path
        if ([IO.Path]::GetExtension($dumpPath) -ne '.sql') {
            throw "Existing seed must be a .sql file: $dumpPath"
        }
    }
    else {
        [void](New-Item -ItemType Directory -Path $OutputDirectory -Force)
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $dumpPath = Join-Path $OutputDirectory "takealot-ops-replica-seed-$stamp.sql"
        if (Test-Path -LiteralPath $dumpPath) {
            throw "Refusing to overwrite existing seed: $dumpPath"
        }

        $previousPassword = $env:MYSQL_PWD
        $env:MYSQL_PWD = $sourcePassword
        try {
            $dumpArguments = @(
                "--host=$normalizedSourceHost",
                "--port=$SourcePort",
                "--user=$sourceUser",
                '--ssl-mode=REQUIRED',
                '--single-transaction',
                '--quick',
                '--source-data=2',
                '--set-gtid-purged=OFF',
                '--default-character-set=utf8mb4',
                '--hex-blob',
                '--no-tablespaces',
                '--skip-triggers',
                '--max-allowed-packet=1G',
                '--column-statistics=0',
                "--result-file=$dumpPath",
                'takealot_ops'
            )
            & $MySqlDumpExe @dumpArguments
            if ($LASTEXITCODE -ne 0) {
                throw "mysqldump failed with exit code $LASTEXITCODE"
            }
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

    $coordinateMatchInfo = Select-String `
        -LiteralPath $dumpPath `
        -Pattern '^-- CHANGE .* (?:SOURCE|MASTER)_LOG_FILE=' `
        -List
    if (-not $coordinateMatchInfo) {
        throw "Seed is missing source coordinates: $dumpPath"
    }
    $coordinateLine = $coordinateMatchInfo.Line
    $coordinateMatch = [regex]::Match(
        $coordinateLine,
        "(?:SOURCE|MASTER)_LOG_FILE='(?<file>[^']+)',\s+(?:SOURCE|MASTER)_LOG_POS=(?<pos>[0-9]+)"
    )
    if (-not $coordinateMatch.Success) {
        throw "Unable to parse source coordinates: $coordinateLine"
    }
    $dumpTail = Get-Content -LiteralPath $dumpPath -Tail 8
    if (-not ($dumpTail -match '^-- Dump completed on ')) {
        throw "Seed is missing the mysqldump completion marker: $dumpPath"
    }

    $dump = Get-Item -LiteralPath $dumpPath
    $hash = Get-FileHash -Algorithm SHA256 -LiteralPath $dumpPath
    [ordered]@{
        status = if ($ExistingDumpPath) { 'seed-validated' } else { 'seed-created' }
        path = $dump.FullName
        size_bytes = $dump.Length
        sha256 = $hash.Hash
        source_log_file = $coordinateMatch.Groups['file'].Value
        source_log_pos = [long]$coordinateMatch.Groups['pos'].Value
        source_endpoint = "${normalizedSourceHost}:$SourcePort"
        source_current_user = $identity[3]
        tls = 'required-and-verified'
    } | ConvertTo-Json -Compress
}
finally {
    if ($plainCredential) {
        [Array]::Clear($plainCredential, 0, $plainCredential.Length)
    }
    $dbaPassword = $null
    $sourcePassword = $null
    if ($sourceMetadata -and $sourceMetadata.Count -ge 2) {
        $sourceMetadata[1] = $null
    }
}
