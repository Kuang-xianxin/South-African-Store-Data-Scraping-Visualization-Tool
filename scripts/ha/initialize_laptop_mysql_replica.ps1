[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [switch]$Execute
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ExpectedComputerName = 'LAPTOP-2T5MN8EU'
$ServiceName = 'MySQL80'
$ReplicaRoot = 'D:\TakealotMySQLReplica'
$ArchivePath = Join-Path $ReplicaRoot 'packages\mysql-8.0.46-winx64.zip'
$ExpectedArchiveSha256 = '28e9eda019d88eff4478d811ea2110b83f02a3966be157fe91cc55def3ab0d4d'
$SoftwarePath = Join-Path $ReplicaRoot 'mysql-8.0.46-winx64'
$MySqlServerPath = Join-Path $SoftwarePath 'bin\mysqld.exe'
$MySqlClientPath = Join-Path $SoftwarePath 'bin\mysql.exe'
$ReplicaConfigPath = Join-Path $ReplicaRoot 'my.ini'
$ReplicaDataPath = Join-Path $ReplicaRoot 'data'
$ReplicaBinlogPath = Join-Path $ReplicaRoot 'binlog'
$ReplicaRelayPath = Join-Path $ReplicaRoot 'relay'
$ReplicaLogPath = Join-Path $ReplicaRoot 'logs'
$ReplicaTempPath = Join-Path $ReplicaRoot 'tmp'
$ReplicaSecureFilePath = Join-Path $ReplicaRoot 'secure-file-priv'
$ReplicaBootstrapPath = Join-Path $ReplicaRoot 'bootstrap'
$ReplicaSecretsPath = Join-Path $ReplicaRoot 'secrets'
$DbaCredentialPath = Join-Path $ReplicaSecretsPath 'mysql-dba.dpapi'
$LegacyServerPath = 'C:\Program Files\MySQL\MySQL Server 8.0\bin\mysqld.exe'
$LegacyConfigPath = 'C:\ProgramData\MySQL\MySQL Server 8.0\my.ini'
$ExpectedLegacyDataPath = 'C:\ProgramData\MySQL\MySQL Server 8.0\Data'

function Get-NormalizedPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    return [IO.Path]::GetFullPath($Path.Replace('/', '\')).TrimEnd('\')
}

function Assert-ExactPath {
    param(
        [Parameter(Mandatory = $true)][string]$Actual,
        [Parameter(Mandatory = $true)][string]$Expected,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $NormalizedActual = Get-NormalizedPath -Path $Actual
    $NormalizedExpected = Get-NormalizedPath -Path $Expected
    if ($NormalizedActual -ine $NormalizedExpected) {
        throw "Safety stop: unexpected $Label path. Actual=$NormalizedActual Expected=$NormalizedExpected"
    }
}

function Test-IsAdministrator {
    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $Principal = New-Object Security.Principal.WindowsPrincipal($Identity)
    return $Principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

function Get-ConfiguredDataPath {
    param([Parameter(Mandatory = $true)][string]$ConfigPath)

    $DataPath = $null
    foreach ($Line in [IO.File]::ReadAllLines($ConfigPath)) {
        if ($Line -match '^\s*datadir\s*=\s*["'']?(.+?)["'']?\s*$') {
            $DataPath = $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
    if ([string]::IsNullOrWhiteSpace($DataPath)) {
        throw "No datadir was found in $ConfigPath"
    }
    return Get-NormalizedPath -Path $DataPath
}

function Wait-ServiceState {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$State,
        [Parameter(Mandatory = $false)][int]$TimeoutSeconds = 60
    )

    $Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $Service = Get-Service -Name $Name -ErrorAction Stop
        if ([string]$Service.Status -eq $State) {
            return
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $Deadline)
    throw "Service $Name did not reach $State within $TimeoutSeconds seconds."
}

function Set-ServiceCommandLine {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$PathName,
        [Parameter(Mandatory = $true)][ValidateSet('Automatic', 'Manual')]
        [string]$StartMode
    )

    $Service = Get-CimInstance Win32_Service -Filter "Name='$Name'"
    if ($null -eq $Service) {
        throw "Windows service not found: $Name"
    }
    $Result = Invoke-CimMethod -InputObject $Service -MethodName Change -Arguments @{
        PathName = $PathName
        StartMode = $StartMode
    }
    if ([int]$Result.ReturnValue -ne 0) {
        throw "Failed to update service $Name; Win32 return code $($Result.ReturnValue)."
    }
}

function Grant-PathAccess {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Grant,
        [Parameter(Mandatory = $false)][switch]$Recursive
    )

    $Arguments = @($Path, '/grant', $Grant, '/C', '/Q')
    if ($Recursive) {
        $Arguments += '/T'
    }
    & icacls.exe @Arguments | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to grant access on $Path"
    }
}

function Protect-PathForCurrentUser {
    param([Parameter(Mandatory = $true)][string]$Path)

    $CurrentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $Item = Get-Item -LiteralPath $Path -ErrorAction Stop
    if ($Item.PSIsContainer) {
        $CurrentGrant = '*' + $CurrentSid + ':(OI)(CI)F'
        $SystemGrant = '*S-1-5-18:(OI)(CI)F'
        $AdministratorsGrant = '*S-1-5-32-544:(OI)(CI)F'
    }
    else {
        $CurrentGrant = '*' + $CurrentSid + ':F'
        $SystemGrant = '*S-1-5-18:F'
        $AdministratorsGrant = '*S-1-5-32-544:F'
    }
    & icacls.exe $Path /inheritance:r | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to disable inherited permissions on $Path"
    }
    & icacls.exe $Path /grant:r `
        $CurrentGrant `
        $SystemGrant `
        $AdministratorsGrant | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to protect $Path"
    }
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )

    [IO.File]::WriteAllText(
        $Path,
        $Content,
        (New-Object Text.UTF8Encoding($false))
    )
}

function Write-JsonFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][object]$Value
    )

    [IO.File]::WriteAllText(
        $Path,
        ($Value | ConvertTo-Json -Depth 8),
        (New-Object Text.UTF8Encoding($true))
    )
}

if ($env:COMPUTERNAME -ne $ExpectedComputerName) {
    throw "Safety stop: current computer is $env:COMPUTERNAME, expected $ExpectedComputerName."
}
if (-not (Test-IsAdministrator)) {
    throw 'Safety stop: an elevated administrator token is required.'
}
Assert-ExactPath -Actual $ReplicaRoot -Expected 'D:\TakealotMySQLReplica' -Label 'replica root'
Assert-ExactPath -Actual $LegacyServerPath -Expected 'C:\Program Files\MySQL\MySQL Server 8.0\bin\mysqld.exe' -Label 'legacy server'
Assert-ExactPath -Actual $LegacyConfigPath -Expected 'C:\ProgramData\MySQL\MySQL Server 8.0\my.ini' -Label 'legacy config'

foreach ($RequiredFile in @(
    $ArchivePath,
    $MySqlServerPath,
    $MySqlClientPath,
    $LegacyServerPath,
    $LegacyConfigPath
)) {
    if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
        throw "Required file not found: $RequiredFile"
    }
}

$ArchiveSha256 = (
    Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256
).Hash.ToLowerInvariant()
if ($ArchiveSha256 -ne $ExpectedArchiveSha256) {
    throw "Safety stop: MySQL archive SHA256 mismatch: $ArchiveSha256"
}

$ServerVersion = (& $MySqlServerPath --version 2>&1 | Out-String).Trim()
if ($ServerVersion -notmatch '\b8\.0\.46\b') {
    throw "Safety stop: unexpected replica mysqld version: $ServerVersion"
}
foreach ($BinaryPath in @($MySqlServerPath, $MySqlClientPath)) {
    $Signature = Get-AuthenticodeSignature -LiteralPath $BinaryPath
    if (
        [string]$Signature.Status -ne 'Valid' -or
        $null -eq $Signature.SignerCertificate -or
        $Signature.SignerCertificate.Subject -notmatch 'Oracle'
    ) {
        throw "Safety stop: Oracle signature verification failed for $BinaryPath"
    }
}

$LegacyService = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
if ($null -eq $LegacyService) {
    throw "Safety stop: Windows service not found: $ServiceName"
}
$ExpectedLegacyCommandFragments = @(
    $LegacyServerPath,
    $LegacyConfigPath,
    $ServiceName
)
foreach ($Fragment in $ExpectedLegacyCommandFragments) {
    if ([string]$LegacyService.PathName -notlike "*$Fragment*") {
        throw "Safety stop: $ServiceName command line is not the expected legacy instance."
    }
}

$LegacyDataPath = Get-ConfiguredDataPath -ConfigPath $LegacyConfigPath
Assert-ExactPath -Actual $LegacyDataPath -Expected $ExpectedLegacyDataPath -Label 'legacy data'
if (-not (Test-Path -LiteralPath $LegacyDataPath -PathType Container)) {
    throw "Safety stop: legacy data directory not found: $LegacyDataPath"
}

$ExistingReplicaService = [string]$LegacyService.PathName -like "*$MySqlServerPath*"
if ($ExistingReplicaService) {
    throw 'Safety stop: MySQL80 already points at the managed replica binary.'
}
foreach ($NewPath in @($ReplicaConfigPath, $ReplicaDataPath, $DbaCredentialPath)) {
    if (Test-Path -LiteralPath $NewPath) {
        throw "Safety stop: managed replica target already exists: $NewPath"
    }
}

$MySqlProcesses = @(Get-CimInstance Win32_Process -Filter "Name='mysqld.exe'")
$MySqlProcessIds = @($MySqlProcesses | ForEach-Object { [int]$_.ProcessId })
$ConflictingListeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object {
        $_.LocalPort -in @(3306, 33060) -and
        [int]$_.OwningProcess -notin $MySqlProcessIds
    })
if ($ConflictingListeners.Count -gt 0) {
    throw 'Safety stop: port 3306 or 33060 is owned by a non-MySQL process.'
}

$DriveD = Get-Volume -DriveLetter D
if ([long]$DriveD.SizeRemaining -lt 10GB) {
    throw 'Safety stop: drive D has less than 10 GiB free.'
}

$Preflight = [ordered]@{
    computer = $env:COMPUTERNAME
    execute = [bool]$Execute
    service_name = $ServiceName
    service_state = [string]$LegacyService.State
    legacy_service_command = [string]$LegacyService.PathName
    legacy_data = $LegacyDataPath
    replica_root = $ReplicaRoot
    replica_version = $ServerVersion
    archive_sha256 = $ArchiveSha256
    drive_d_free_bytes = [long]$DriveD.SizeRemaining
    planned_bind_address = '127.0.0.1'
    planned_server_id = 2
    planned_gtid_mode = 'ON'
    planned_read_only = $true
}
if (-not $Execute) {
    $Preflight | ConvertTo-Json -Depth 5
    exit 0
}

$Timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$QuarantineRoot = Join-Path $ReplicaRoot 'quarantine'
$LegacyConfigBackupPath = Join-Path $QuarantineRoot "legacy-my.ini-$Timestamp"
$LegacyDataQuarantinePath = "$LegacyDataPath.pre-takealot-replica-$Timestamp"
$ManifestPath = Join-Path $QuarantineRoot "initialization-$Timestamp.json"
$BootstrapSqlPath = Join-Path $ReplicaBootstrapPath "bootstrap-$Timestamp.sql"
$OriginalServiceCommand = [string]$LegacyService.PathName
$OriginalServiceStartMode = [string]$LegacyService.StartMode
$ReplicaServiceCommand = (
    '"' + $MySqlServerPath + '" --defaults-file="' +
    $ReplicaConfigPath + '" ' + $ServiceName
)
$LegacyDataMoved = $false
$ServiceCommandChanged = $false
$ExecutionSucceeded = $false

$Manifest = [ordered]@{
    computer = $env:COMPUTERNAME
    started_at = (Get-Date).ToString('o')
    status = 'starting'
    legacy_service_command = $OriginalServiceCommand
    legacy_service_start_mode = $OriginalServiceStartMode
    legacy_config = $LegacyConfigPath
    legacy_config_backup = $LegacyConfigBackupPath
    legacy_data = $LegacyDataPath
    legacy_data_quarantine = $LegacyDataQuarantinePath
    replica_service_command = $ReplicaServiceCommand
    replica_config = $ReplicaConfigPath
    replica_data = $ReplicaDataPath
    replica_version = $ServerVersion
    archive_sha256 = $ArchiveSha256
}

try {
    New-Item -ItemType Directory -Path $QuarantineRoot -Force | Out-Null
    Copy-Item -LiteralPath $LegacyConfigPath -Destination $LegacyConfigBackupPath
    Write-JsonFile -Path $ManifestPath -Value $Manifest

    if ([string](Get-Service -Name $ServiceName).Status -ne 'Stopped') {
        Stop-Service -Name $ServiceName -Force
        Wait-ServiceState -Name $ServiceName -State 'Stopped' -TimeoutSeconds 60
    }
    Set-Service -Name $ServiceName -StartupType Manual

    $PortDeadline = (Get-Date).AddSeconds(20)
    do {
        $RemainingListeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalPort -in @(3306, 33060) })
        if ($RemainingListeners.Count -eq 0) {
            break
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $PortDeadline)
    if ($RemainingListeners.Count -gt 0) {
        throw 'MySQL stopped but port 3306 or 33060 is still listening.'
    }

    if (Test-Path -LiteralPath $LegacyDataQuarantinePath) {
        throw "Safety stop: legacy quarantine target already exists: $LegacyDataQuarantinePath"
    }
    Move-Item -LiteralPath $LegacyDataPath -Destination $LegacyDataQuarantinePath
    $LegacyDataMoved = $true

    foreach ($Directory in @(
        $ReplicaDataPath,
        $ReplicaBinlogPath,
        $ReplicaRelayPath,
        $ReplicaLogPath,
        $ReplicaTempPath,
        $ReplicaSecureFilePath,
        $ReplicaBootstrapPath,
        $ReplicaSecretsPath
    )) {
        New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    }

    Grant-PathAccess -Path $ReplicaRoot -Grant '*S-1-5-20:(RX)'
    Grant-PathAccess -Path $SoftwarePath -Grant '*S-1-5-20:(OI)(CI)RX' -Recursive
    foreach ($WritableDirectory in @(
        $ReplicaDataPath,
        $ReplicaBinlogPath,
        $ReplicaRelayPath,
        $ReplicaLogPath,
        $ReplicaTempPath,
        $ReplicaSecureFilePath
    )) {
        Grant-PathAccess -Path $WritableDirectory -Grant '*S-1-5-20:(OI)(CI)M' -Recursive
    }
    Grant-PathAccess -Path $ReplicaBootstrapPath -Grant '*S-1-5-20:(OI)(CI)RX' -Recursive
    Protect-PathForCurrentUser -Path $ReplicaSecretsPath

    $RandomBytes = New-Object byte[] 36
    $RandomGenerator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $RandomGenerator.GetBytes($RandomBytes)
        $DbaPasswordText = [Convert]::ToBase64String($RandomBytes).TrimEnd('=') + 'aA!9'
    }
    finally {
        $RandomGenerator.Dispose()
        [Array]::Clear($RandomBytes, 0, $RandomBytes.Length)
    }

    $ForwardReplicaRoot = $ReplicaRoot.Replace('\', '/')
    $ForwardSoftwarePath = $SoftwarePath.Replace('\', '/')
    $ForwardBootstrapSqlPath = $BootstrapSqlPath.Replace('\', '/')
    $FinalConfig = @"
[client]
protocol=TCP
host=127.0.0.1
port=3306

[mysqld]
basedir=$ForwardSoftwarePath
datadir=$ForwardReplicaRoot/data
port=3306
bind-address=127.0.0.1
mysqlx=0
server-id=2
report-host=LAPTOP-2T5MN8EU
log-bin=$ForwardReplicaRoot/binlog/takealot-laptop-bin
log-replica-updates=ON
binlog-format=ROW
binlog-row-image=FULL
binlog-expire-logs-seconds=604800
relay-log=$ForwardReplicaRoot/relay/takealot-laptop-relay
relay-log-recovery=ON
skip-replica-start=ON
gtid-mode=ON
enforce-gtid-consistency=ON
read-only=ON
super-read-only=ON
sync-binlog=1
innodb-flush-log-at-trx-commit=1
skip-name-resolve=ON
local-infile=OFF
character-set-server=utf8mb4
collation-server=utf8mb4_0900_ai_ci
max-connections=150
innodb-buffer-pool-size=2G
tmpdir=$ForwardReplicaRoot/tmp
secure-file-priv=$ForwardReplicaRoot/secure-file-priv
log-error=$ForwardReplicaRoot/logs/mysql-error.log
pid-file=$ForwardReplicaRoot/logs/mysql.pid
"@
    $BootstrapConfig = (
        $FinalConfig.TrimEnd("`r", "`n") +
        "`r`ninit-file=$ForwardBootstrapSqlPath`r`n"
    )
    $BootstrapSql = @"
SET GLOBAL super_read_only=OFF;
SET GLOBAL read_only=OFF;
ALTER USER 'root'@'localhost' IDENTIFIED WITH caching_sha2_password BY '$DbaPasswordText';
CREATE USER 'takealot_dba'@'127.0.0.1' IDENTIFIED WITH caching_sha2_password BY '$DbaPasswordText';
GRANT ALL PRIVILEGES ON *.* TO 'takealot_dba'@'127.0.0.1' WITH GRANT OPTION;
SET GLOBAL read_only=ON;
SET GLOBAL super_read_only=ON;
"@
    Write-Utf8NoBom -Path $ReplicaConfigPath -Content $FinalConfig
    Write-Utf8NoBom -Path $BootstrapSqlPath -Content $BootstrapSql
    Grant-PathAccess -Path $ReplicaConfigPath -Grant '*S-1-5-20:R'
    Grant-PathAccess -Path $BootstrapSqlPath -Grant '*S-1-5-20:R'

    $SavedErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $InitializeOutput = @(
            & $MySqlServerPath `
                "--defaults-file=$ReplicaConfigPath" `
                --initialize-insecure `
                --console 2>&1
        )
        $InitializeExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $SavedErrorActionPreference
    }
    if ($InitializeExitCode -ne 0) {
        $InitializeText = ($InitializeOutput | Select-Object -Last 20) -join ' '
        throw "MySQL data initialization failed: $InitializeText"
    }

    Write-Utf8NoBom -Path $ReplicaConfigPath -Content $BootstrapConfig
    Grant-PathAccess -Path $ReplicaConfigPath -Grant '*S-1-5-20:R'

    Set-ServiceCommandLine `
        -Name $ServiceName `
        -PathName $ReplicaServiceCommand `
        -StartMode Automatic
    $ServiceCommandChanged = $true

    Start-Service -Name $ServiceName
    Wait-ServiceState -Name $ServiceName -State 'Running' -TimeoutSeconds 90

    $ReadyDeadline = (Get-Date).AddSeconds(60)
    $Ready = $false
    do {
        $Listener = @(Get-NetTCPConnection -LocalPort 3306 -State Listen -ErrorAction SilentlyContinue)
        if ($Listener.Count -gt 0) {
            $Ready = $true
            break
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $ReadyDeadline)
    if (-not $Ready) {
        throw 'Replica service started but port 3306 did not become ready.'
    }

    Add-Type -AssemblyName System.Security
    $DbaSecretText = "takealot_dba`n$DbaPasswordText"
    $DbaSecretBytes = [Text.Encoding]::UTF8.GetBytes($DbaSecretText)
    $ProtectedDbaSecretBytes = $null
    $RoundTripDbaSecretBytes = $null
    try {
        $ProtectedDbaSecretBytes = [Security.Cryptography.ProtectedData]::Protect(
            $DbaSecretBytes,
            $null,
            [Security.Cryptography.DataProtectionScope]::LocalMachine
        )
        [IO.File]::WriteAllBytes($DbaCredentialPath, $ProtectedDbaSecretBytes)
        Protect-PathForCurrentUser -Path $DbaCredentialPath

        $RoundTripDbaSecretBytes = [Security.Cryptography.ProtectedData]::Unprotect(
            $ProtectedDbaSecretBytes,
            $null,
            [Security.Cryptography.DataProtectionScope]::LocalMachine
        )
        if ([Text.Encoding]::UTF8.GetString($RoundTripDbaSecretBytes) -cne $DbaSecretText) {
            throw 'The machine-protected DBA credential failed its round-trip check.'
        }
    }
    finally {
        if ($null -ne $DbaSecretBytes) {
            [Array]::Clear($DbaSecretBytes, 0, $DbaSecretBytes.Length)
        }
        if ($null -ne $ProtectedDbaSecretBytes) {
            [Array]::Clear($ProtectedDbaSecretBytes, 0, $ProtectedDbaSecretBytes.Length)
        }
        if ($null -ne $RoundTripDbaSecretBytes) {
            [Array]::Clear($RoundTripDbaSecretBytes, 0, $RoundTripDbaSecretBytes.Length)
        }
        $DbaSecretText = $null
    }

    Write-Utf8NoBom -Path $ReplicaConfigPath -Content $FinalConfig
    Grant-PathAccess -Path $ReplicaConfigPath -Grant '*S-1-5-20:R'
    Remove-Item -LiteralPath $BootstrapSqlPath -Force

    $PreviousMySqlPassword = $env:MYSQL_PWD
    $env:MYSQL_PWD = $DbaPasswordText
    $VerificationSql = (
        'SELECT VERSION(), @@GLOBAL.server_id, ' +
        '@@GLOBAL.gtid_mode, @@GLOBAL.enforce_gtid_consistency, ' +
        '@@GLOBAL.read_only, @@GLOBAL.super_read_only, ' +
        '@@GLOBAL.log_bin;'
    )
    try {
        $SavedErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $VerificationOutput = @(
                & $MySqlClientPath `
                    --protocol=TCP `
                    --host=127.0.0.1 `
                    --port=3306 `
                    --user=takealot_dba `
                    --batch `
                    --skip-column-names `
                    "--execute=$VerificationSql" 2>&1
            )
            $VerificationExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $SavedErrorActionPreference
        }
        if ($VerificationExitCode -ne 0) {
            throw 'The initialized MySQL instance rejected the DBA verification connection.'
        }
    }
    finally {
        if ($null -eq $PreviousMySqlPassword) {
            Remove-Item Env:\MYSQL_PWD -ErrorAction SilentlyContinue
        }
        else {
            $env:MYSQL_PWD = $PreviousMySqlPassword
        }
        $DbaPasswordText = $null
    }

    $UnsafeListeners = @(Get-NetTCPConnection -LocalPort 3306 -State Listen -ErrorAction Stop |
        Where-Object { $_.LocalAddress -notin @('127.0.0.1', '::1') })
    if ($UnsafeListeners.Count -gt 0) {
        throw 'Replica MySQL is listening outside the loopback interface.'
    }
    $MySqlXListeners = @(Get-NetTCPConnection -LocalPort 33060 -State Listen -ErrorAction SilentlyContinue)
    if ($MySqlXListeners.Count -gt 0) {
        throw 'MySQL X port 33060 is unexpectedly listening.'
    }

    $Manifest.status = 'success'
    $Manifest.completed_at = (Get-Date).ToString('o')
    $Manifest.verification = ($VerificationOutput -join ' ').Trim()
    $Manifest.credential_path = $DbaCredentialPath
    Write-JsonFile -Path $ManifestPath -Value $Manifest
    $ExecutionSucceeded = $true

    [pscustomobject]@{
        computer = $env:COMPUTERNAME
        status = 'success'
        service = $ServiceName
        service_state = [string](Get-Service -Name $ServiceName).Status
        service_command = [string](Get-CimInstance Win32_Service -Filter "Name='$ServiceName'").PathName
        config = $ReplicaConfigPath
        data = $ReplicaDataPath
        legacy_data_quarantine = $LegacyDataQuarantinePath
        legacy_config_backup = $LegacyConfigBackupPath
        credential = $DbaCredentialPath
        verification = ($VerificationOutput -join ' ').Trim()
        manifest = $ManifestPath
    } | ConvertTo-Json -Depth 6
}
catch {
    $Failure = $_
    $Manifest.status = 'failed'
    $Manifest.failed_at = (Get-Date).ToString('o')
    $Manifest.failure = $Failure.Exception.Message
    try {
        Write-JsonFile -Path $ManifestPath -Value $Manifest
    }
    catch {
    }

    try {
        if ([string](Get-Service -Name $ServiceName -ErrorAction SilentlyContinue).Status -ne 'Stopped') {
            Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
            Wait-ServiceState -Name $ServiceName -State 'Stopped' -TimeoutSeconds 30
        }
    }
    catch {
    }

    if ($ServiceCommandChanged) {
        try {
            Set-ServiceCommandLine `
                -Name $ServiceName `
                -PathName $OriginalServiceCommand `
                -StartMode Automatic
        }
        catch {
        }
    }

    if ($LegacyDataMoved) {
        try {
            if (Test-Path -LiteralPath $ReplicaDataPath -PathType Container) {
                $FailedDataPath = "$ReplicaDataPath.failed-$Timestamp"
                if (-not (Test-Path -LiteralPath $FailedDataPath)) {
                    Move-Item -LiteralPath $ReplicaDataPath -Destination $FailedDataPath
                }
            }
            if (
                -not (Test-Path -LiteralPath $LegacyDataPath) -and
                (Test-Path -LiteralPath $LegacyDataQuarantinePath)
            ) {
                Move-Item -LiteralPath $LegacyDataQuarantinePath -Destination $LegacyDataPath
            }
        }
        catch {
        }
    }

    try {
        Set-Service -Name $ServiceName -StartupType Automatic
        Start-Service -Name $ServiceName
        Wait-ServiceState -Name $ServiceName -State 'Running' -TimeoutSeconds 60
    }
    catch {
    }
    throw $Failure
}
finally {
    if (-not $ExecutionSucceeded) {
        Remove-Item Env:\MYSQL_PWD -ErrorAction SilentlyContinue
    }
}
