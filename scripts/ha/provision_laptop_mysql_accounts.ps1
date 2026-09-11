[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [switch]$Execute,

    [Parameter(Mandatory = $false)]
    [switch]$ReadPasswordsFromStdin
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ExpectedComputerName = 'LAPTOP-2T5MN8EU'
$ReplicaRoot = 'D:\TakealotMySQLReplica'
$CredentialPath = Join-Path $ReplicaRoot 'secrets\mysql-dba.dpapi'
$MySqlPath = Join-Path $ReplicaRoot 'mysql-8.0.46-winx64\bin\mysql.exe'
$QuarantineRoot = Join-Path $ReplicaRoot 'quarantine'

function Invoke-MySql {
    param(
        [Parameter(Mandatory = $true)][string]$User,
        [Parameter(Mandatory = $true)][string]$Password,
        [Parameter(Mandatory = $true)][string]$Sql
    )

    $PreviousPassword = $env:MYSQL_PWD
    $env:MYSQL_PWD = $Password
    $SavedErrorActionPreference = $ErrorActionPreference
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
        $Message = @($Output | ForEach-Object { [string]$_ }) -join [Environment]::NewLine
        throw "mysql.exe failed with exit code $ExitCode. $Message"
    }
    return @($Output | ForEach-Object { [string]$_ })
}

function Invoke-SecretSql {
    param([Parameter(Mandatory = $true)][string]$Sql)

    $StartInfo = New-Object Diagnostics.ProcessStartInfo
    $StartInfo.FileName = $MySqlPath
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
        throw 'Failed to start mysql.exe for account provisioning.'
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

if ($env:COMPUTERNAME -cne $ExpectedComputerName) {
    throw "Safety stop: expected $ExpectedComputerName, got $env:COMPUTERNAME."
}
$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$Principal = New-Object Security.Principal.WindowsPrincipal($Identity)
if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator privileges are required on the laptop.'
}
foreach ($Path in @($CredentialPath, $MySqlPath)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required file not found: $Path"
    }
}

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
$script:DbaUser = $DbaParts[0].Trim()
$script:DbaPassword = $DbaParts[1].Trim()

$State = @(
    Invoke-MySql `
        -User $script:DbaUser `
        -Password $script:DbaPassword `
        -Sql (
            "SELECT CONCAT(CURRENT_USER(),'|',@@hostname,'|',@@server_id,'|'," +
            "@@read_only,'|',@@super_read_only,'|',@@log_bin,'|'," +
            "@@skip_name_resolve);"
        )
)[0]
$StateParts = $State.Split('|')
if (
    $StateParts.Count -ne 7 -or
    $StateParts[0] -cne 'takealot_dba@127.0.0.1' -or
    $StateParts[1] -cne $ExpectedComputerName -or
    $StateParts[2] -cne '2' -or
    $StateParts[3] -cne '1' -or
    $StateParts[4] -cne '1' -or
    $StateParts[5] -cne '1'
) {
    throw "Safety stop: unexpected laptop MySQL state: $State"
}
$TableCount = [int]@(
    Invoke-MySql `
        -User $script:DbaUser `
        -Password $script:DbaPassword `
        -Sql (
            "SELECT COUNT(*) FROM information_schema.TABLES " +
            "WHERE TABLE_SCHEMA='takealot_ops' AND TABLE_TYPE='BASE TABLE';"
        )
)[0]
if ($TableCount -ne 60) {
    throw "Safety stop: expected 60 business tables, found $TableCount."
}
$ExistingAccounts = @(
    Invoke-MySql `
        -User $script:DbaUser `
        -Password $script:DbaPassword `
        -Sql (
            "SELECT CONCAT(User,'@',Host) FROM mysql.user " +
            "WHERE User IN ('takealot_app','takealot_backup') ORDER BY User,Host;"
        )
)

$Preflight = [ordered]@{
    computer = $env:COMPUTERNAME
    status = 'preflight_ok'
    mysql_state = $State
    business_table_count = $TableCount
    existing_accounts = $ExistingAccounts
    read_only_will_be_restored = $true
    sql_log_bin_for_account_setup = 0
    password_recorded = $false
}
if (-not $Execute) {
    $Preflight | ConvertTo-Json -Depth 8
    exit 0
}
if (-not $ReadPasswordsFromStdin) {
    throw 'Passwords must be supplied through standard input.'
}

$Payload = [Console]::In.ReadToEnd() -split '\r?\n'
$Payload = @($Payload | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($Payload.Count -ne 2) {
    throw 'Expected two base64-encoded passwords on standard input.'
}
$script:AppPassword = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String($Payload[0])
)
$script:BackupPassword = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String($Payload[1])
)
foreach ($Password in @($script:AppPassword, $script:BackupPassword)) {
    if ([string]::IsNullOrWhiteSpace($Password) -or $Password -match "['\\\r\n]") {
        throw 'A password contains characters unsupported by this guarded path.'
    }
}
if ($script:BackupPassword.Length -gt 32) {
    throw 'The backup password exceeds the MySQL replication limit of 32 characters.'
}

$Timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$ManifestPath = Join-Path $QuarantineRoot "account-provisioning-$Timestamp.json"
$Manifest = [ordered]@{
    computer = $env:COMPUTERNAME
    started_at = (Get-Date).ToString('o')
    status = 'starting'
    accounts = @(
        'takealot_app@localhost',
        'takealot_app@127.0.0.1',
        'takealot_backup@localhost',
        'takealot_backup@127.0.0.1'
    )
    password_recorded = $false
    sql_log_bin = 0
    initial_read_only = 1
    initial_super_read_only = 1
}
$ReadOnlyDisabled = $false

try {
    New-Item -ItemType Directory -Path $QuarantineRoot -Force | Out-Null
    Write-JsonFile -Path $ManifestPath -Value $Manifest
    Invoke-MySql `
        -User $script:DbaUser `
        -Password $script:DbaPassword `
        -Sql 'SET GLOBAL super_read_only=OFF; SET GLOBAL read_only=OFF;' | Out-Null
    $ReadOnlyDisabled = $true

    $Sql = @"
SET SESSION sql_log_bin=0;
CREATE USER IF NOT EXISTS 'takealot_app'@'localhost' IDENTIFIED BY '$script:AppPassword';
ALTER USER 'takealot_app'@'localhost' IDENTIFIED BY '$script:AppPassword';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, REFERENCES, INDEX, ALTER, LOCK TABLES, SHOW VIEW, TRIGGER ON takealot_ops.* TO 'takealot_app'@'localhost';
CREATE USER IF NOT EXISTS 'takealot_app'@'127.0.0.1' IDENTIFIED BY '$script:AppPassword';
ALTER USER 'takealot_app'@'127.0.0.1' IDENTIFIED BY '$script:AppPassword';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, REFERENCES, INDEX, ALTER, LOCK TABLES, SHOW VIEW, TRIGGER ON takealot_ops.* TO 'takealot_app'@'127.0.0.1';
CREATE USER IF NOT EXISTS 'takealot_backup'@'localhost' IDENTIFIED BY '$script:BackupPassword';
ALTER USER 'takealot_backup'@'localhost' IDENTIFIED BY '$script:BackupPassword';
GRANT RELOAD, REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO 'takealot_backup'@'localhost';
GRANT SELECT, SHOW VIEW, TRIGGER ON takealot_ops.* TO 'takealot_backup'@'localhost';
CREATE USER IF NOT EXISTS 'takealot_backup'@'127.0.0.1' IDENTIFIED BY '$script:BackupPassword';
ALTER USER 'takealot_backup'@'127.0.0.1' IDENTIFIED BY '$script:BackupPassword';
GRANT RELOAD, REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO 'takealot_backup'@'127.0.0.1';
GRANT SELECT, SHOW VIEW, TRIGGER ON takealot_ops.* TO 'takealot_backup'@'127.0.0.1';
"@
    Invoke-SecretSql -Sql $Sql | Out-Null
    $Sql = $null
}
finally {
    if ($ReadOnlyDisabled) {
        Invoke-MySql `
            -User $script:DbaUser `
            -Password $script:DbaPassword `
            -Sql 'SET GLOBAL read_only=ON; SET GLOBAL super_read_only=ON;' | Out-Null
    }
}

try {
    $FinalState = @(
        Invoke-MySql `
            -User $script:DbaUser `
            -Password $script:DbaPassword `
            -Sql 'SELECT CONCAT(@@read_only,''|'',@@super_read_only,''|'',@@log_bin);'
    )[0]
    if ($FinalState -cne '1|1|1') {
        throw "Laptop read-only restoration failed: $FinalState"
    }
    $AppProbe = @(
        Invoke-MySql `
            -User 'takealot_app' `
            -Password $script:AppPassword `
            -Sql (
                "SELECT CONCAT(CURRENT_USER(),'|',(SELECT COUNT(*) " +
                "FROM information_schema.TABLES WHERE TABLE_SCHEMA='takealot_ops' " +
                "AND TABLE_TYPE='BASE TABLE'));"
            )
    )[0]
    $BackupProbe = @(
        Invoke-MySql `
            -User 'takealot_backup' `
            -Password $script:BackupPassword `
            -Sql "SELECT CONCAT(CURRENT_USER(),'|',@@hostname,'|',@@server_id);"
    )[0]
    if ($AppProbe -cne 'takealot_app@127.0.0.1|60') {
        throw "App account probe failed: $AppProbe"
    }
    if ($BackupProbe -cne 'takealot_backup@127.0.0.1|LAPTOP-2T5MN8EU|2') {
        throw "Backup account probe failed: $BackupProbe"
    }

    $Manifest.status = 'success'
    $Manifest.completed_at = (Get-Date).ToString('o')
    $Manifest.final_read_only = 1
    $Manifest.final_super_read_only = 1
    $Manifest.app_probe = $AppProbe
    $Manifest.backup_probe = $BackupProbe
    Write-JsonFile -Path $ManifestPath -Value $Manifest

    [pscustomobject]@{
        computer = $env:COMPUTERNAME
        status = 'success'
        app_identity = 'takealot_app@127.0.0.1'
        backup_identity = 'takealot_backup@127.0.0.1'
        source_event_compatibility_accounts = @(
            'takealot_app@localhost',
            'takealot_backup@localhost'
        )
        business_tables = 60
        read_only = 1
        super_read_only = 1
        sql_logged = 0
        password_recorded = $false
        manifest = $ManifestPath
    } | ConvertTo-Json -Depth 8
}
catch {
    $Failure = $_
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
    $script:AppPassword = $null
    $script:BackupPassword = $null
    $script:DbaPassword = $null
    $DbaSecret = $null
    if ($null -ne $PlainBytes) {
        [Array]::Clear($PlainBytes, 0, $PlainBytes.Length)
    }
}
