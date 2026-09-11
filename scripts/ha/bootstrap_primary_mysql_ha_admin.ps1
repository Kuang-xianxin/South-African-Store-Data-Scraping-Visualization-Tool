[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$VerifiedBackupPath,

    [Parameter(Mandatory = $false)]
    [switch]$Execute,

    [Parameter(Mandatory = $false)]
    [string]$FencedMarkerPath = '',

    [Parameter(Mandatory = $false)]
    [string]$ContinueSignalPath = '',

    [Parameter(Mandatory = $false)]
    [ValidateRange(30, 900)]
    [int]$TakeoverWaitSeconds = 300
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ExpectedComputerName = 'DESKTOP-NTRMANG'
$ServiceName = 'MySQL80'
$ExpectedServiceAccount = 'NT AUTHORITY\NetworkService'
$MySqlServerPath = 'C:\Program Files\MySQL\MySQL Server 8.0\bin\mysqld.exe'
$MySqlClientPath = 'C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe'
$MySqlConfigPath = 'C:\ProgramData\MySQL\MySQL Server 8.0\my.ini'
$HaRoot = 'D:\TakealotHA'
$BootstrapRoot = Join-Path $HaRoot 'bootstrap'
$SecretsRoot = Join-Path $HaRoot 'secrets'
$QuarantineRoot = Join-Path $HaRoot 'quarantine'
$HandoffRoot = Join-Path $HaRoot 'handoff'
$CredentialPath = Join-Path $SecretsRoot 'primary-ha-admin.dpapi'
$HaUser = 'takealot_ha_admin'
$ErpPort = 8501
$OperatorAccount = 'DESKTOP-NTRMANG\Mayn'
$OperatorSid = (
    New-Object Security.Principal.NTAccount($OperatorAccount)
).Translate([Security.Principal.SecurityIdentifier]).Value

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

function Protect-Directory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $false)][switch]$AllowNetworkServiceRead
    )

    $CurrentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $Grants = @(
        ('*' + $OperatorSid + ':(OI)(CI)F'),
        '*S-1-5-18:(OI)(CI)F',
        '*S-1-5-32-544:(OI)(CI)F'
    )
    if ($CurrentSid -cne $OperatorSid) {
        $Grants += ('*' + $CurrentSid + ':(OI)(CI)F')
    }
    if ($AllowNetworkServiceRead) {
        $Grants += '*S-1-5-20:(OI)(CI)RX'
    }
    $GrantArguments = @($Path, '/grant:r') + $Grants + @('/C', '/Q')
    & icacls.exe @GrantArguments | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to protect $Path"
    }
    & icacls.exe $Path /inheritance:r /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to disable inherited permissions on $Path"
    }
}

function Protect-File {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $false)][switch]$AllowNetworkServiceRead
    )

    $CurrentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $Grants = @(
        ('*' + $OperatorSid + ':F'),
        '*S-1-5-18:F',
        '*S-1-5-32-544:F'
    )
    if ($CurrentSid -cne $OperatorSid) {
        $Grants += ('*' + $CurrentSid + ':F')
    }
    if ($AllowNetworkServiceRead) {
        $Grants += '*S-1-5-20:R'
    }
    $GrantArguments = @($Path, '/grant:r') + $Grants + @('/C', '/Q')
    & icacls.exe @GrantArguments | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to protect $Path"
    }
    & icacls.exe $Path /inheritance:r /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to disable inherited permissions on $Path"
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

function Read-Utf8JsonWithRetry {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $false)][int]$Attempts = 5
    )

    $LastFailure = $null
    for ($Attempt = 1; $Attempt -le $Attempts; $Attempt++) {
        try {
            $Content = [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8)
            return $Content | ConvertFrom-Json
        }
        catch {
            $LastFailure = $_
            if ($Attempt -lt $Attempts) {
                Start-Sleep -Milliseconds 250
            }
        }
    }
    throw "Unable to read a stable UTF-8 JSON snapshot from $Path`: $($LastFailure.Exception.Message)"
}

if ($env:COMPUTERNAME -ne $ExpectedComputerName) {
    throw "Safety stop: current computer is $env:COMPUTERNAME, expected $ExpectedComputerName."
}
$IsAdministrator = Test-IsAdministrator
if ($Execute -and -not $IsAdministrator) {
    throw 'Safety stop: an elevated administrator token is required.'
}

$ProjectRoot = Get-NormalizedPath -Path (Join-Path $PSScriptRoot '..\..')
if ((Get-Item -LiteralPath $ProjectRoot).PSDrive.Name -cne 'D') {
    throw "Safety stop: project root is not on drive D: $ProjectRoot"
}
foreach ($ProjectFile in @(
    (Join-Path $ProjectRoot 'AGENT.md'),
    (Join-Path $ProjectRoot '.venv\Scripts\python.exe'),
    (Join-Path $ProjectRoot 'scripts\restart_erp.ps1')
)) {
    if (-not (Test-Path -LiteralPath $ProjectFile -PathType Leaf)) {
        throw "Safety stop: required project file not found: $ProjectFile"
    }
}
Assert-ExactPath -Actual $HaRoot -Expected 'D:\TakealotHA' -Label 'HA root'

foreach ($RequiredFile in @($MySqlServerPath, $MySqlClientPath, $MySqlConfigPath)) {
    if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
        throw "Required file not found: $RequiredFile"
    }
}

$BackupPath = Get-NormalizedPath -Path $VerifiedBackupPath
$BackupRoot = Get-NormalizedPath -Path (Join-Path $ProjectRoot 'backups')
if (-not $BackupPath.StartsWith($BackupRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw "Safety stop: verified backup is outside the project backup root: $BackupPath"
}
if (-not $BackupPath.EndsWith('.sql.gz', [StringComparison]::OrdinalIgnoreCase)) {
    throw "Safety stop: verified backup is not a .sql.gz archive: $BackupPath"
}
foreach ($BackupFile in @($BackupPath, "$BackupPath.json", "$BackupPath.sha256")) {
    if (-not (Test-Path -LiteralPath $BackupFile -PathType Leaf)) {
        throw "Verified backup component not found: $BackupFile"
    }
}
$BackupManifest = Get-Content -LiteralPath "$BackupPath.json" -Raw | ConvertFrom-Json
$BackupHash = (Get-FileHash -LiteralPath $BackupPath -Algorithm SHA256).Hash.ToLowerInvariant()
$ExpectedHash = [string]$BackupManifest.sha256
if ($BackupHash -cne $ExpectedHash.ToLowerInvariant()) {
    throw "Safety stop: backup SHA256 mismatch. Actual=$BackupHash Expected=$ExpectedHash"
}
if (
    [string]$BackupManifest.backend -cne 'mysql' -or
    [string]$BackupManifest.database -cne 'takealot_ops' -or
    [string]$BackupManifest.archive -cne [IO.Path]::GetFileName($BackupPath)
) {
    throw 'Safety stop: backup manifest does not describe the formal MySQL database archive.'
}
$BackupCreatedAt = [DateTimeOffset]::Parse([string]$BackupManifest.created_at)
$BackupAge = [DateTimeOffset]::UtcNow - $BackupCreatedAt.ToUniversalTime()
if ($BackupAge.TotalHours -lt 0 -or $BackupAge.TotalHours -gt 4) {
    throw "Safety stop: verified backup is not within the last four hours: $BackupCreatedAt"
}

$HasFencedMarker = -not [string]::IsNullOrWhiteSpace($FencedMarkerPath)
$HasContinueSignal = -not [string]::IsNullOrWhiteSpace($ContinueSignalPath)
if ($HasFencedMarker -xor $HasContinueSignal) {
    throw 'Safety stop: fenced marker and continue signal paths must be supplied together.'
}
$UseTakeoverHandshake = $HasFencedMarker -and $HasContinueSignal
if ($UseTakeoverHandshake) {
    $FencedMarkerPath = Get-NormalizedPath -Path $FencedMarkerPath
    $ContinueSignalPath = Get-NormalizedPath -Path $ContinueSignalPath
    $NormalizedHandoffRoot = Get-NormalizedPath -Path $HandoffRoot
    foreach ($HandshakePath in @($FencedMarkerPath, $ContinueSignalPath)) {
        if (
            (Get-NormalizedPath -Path (Split-Path -Parent $HandshakePath)) -ine
            $NormalizedHandoffRoot
        ) {
            throw "Safety stop: handshake file must be a direct child of $NormalizedHandoffRoot"
        }
        if (Test-Path -LiteralPath $HandshakePath) {
            throw "Safety stop: handshake file already exists: $HandshakePath"
        }
    }
    if ($FencedMarkerPath -ieq $ContinueSignalPath) {
        throw 'Safety stop: fenced marker and continue signal paths must be different.'
    }
}

$Service = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
if ($null -eq $Service) {
    throw "Safety stop: Windows service not found: $ServiceName"
}
$ExpectedServiceCommand = (
    '"' + $MySqlServerPath + '" --defaults-file="' +
    $MySqlConfigPath + '" ' + $ServiceName
)
if ([string]$Service.PathName -cne $ExpectedServiceCommand) {
    throw "Safety stop: unexpected MySQL80 command line: $($Service.PathName)"
}
if ([string]$Service.StartName -cne $ExpectedServiceAccount) {
    throw "Safety stop: unexpected MySQL80 service account: $($Service.StartName)"
}
if ([string]$Service.State -cne 'Running' -or [string]$Service.StartMode -cne 'Auto') {
    throw 'Safety stop: MySQL80 is not Running with Automatic startup.'
}
if (Test-Path -LiteralPath $CredentialPath) {
    throw "Safety stop: HA administrator credential already exists: $CredentialPath"
}

$ErpListeners = @(Get-NetTCPConnection -State Listen -LocalPort $ErpPort -ErrorAction Stop)
if ($ErpListeners.Count -ne 1) {
    throw "Safety stop: expected exactly one ERP listener on port $ErpPort."
}
$ErpProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($ErpListeners[0].OwningProcess)"
if ($null -eq $ErpProcess -or [string]$ErpProcess.CommandLine -notlike '*takealot_ops.erp.web:app*') {
    throw "Safety stop: port $ErpPort is not owned by the formal ERP process."
}
$Health = Invoke-RestMethod -Uri "http://127.0.0.1:$ErpPort/api/health" -TimeoutSec 5
if ([string]$Health.status -cne 'ok' -or [string]$Health.application -cne 'takealot-erp') {
    throw 'Safety stop: formal ERP health response is not valid.'
}

$BatchJournalPath = Join-Path $ProjectRoot 'logs\competitor-scheduled-batch.json'
$BatchJournal = if (Test-Path -LiteralPath $BatchJournalPath -PathType Leaf) {
    Read-Utf8JsonWithRetry -Path $BatchJournalPath
}
else {
    $null
}
$Preflight = [ordered]@{
    computer = $env:COMPUTERNAME
    execute = [bool]$Execute
    is_administrator = $IsAdministrator
    project_root = $ProjectRoot
    backup = $BackupPath
    backup_sha256 = $BackupHash
    backup_age_minutes = [math]::Round($BackupAge.TotalMinutes, 1)
    backup_binlog_file = [string]$BackupManifest.binlog.file
    backup_binlog_position = [long]$BackupManifest.binlog.position
    mysql_service_state = [string]$Service.State
    mysql_service_command = [string]$Service.PathName
    mysql_service_account = [string]$Service.StartName
    erp_pid = [int]$ErpProcess.ProcessId
    erp_health = [string]$Health.status
    batch_id = if ($null -ne $BatchJournal) { [string]$BatchJournal.batch_id } else { $null }
    batch_active_index = if ($null -ne $BatchJournal) { $BatchJournal.active_item.index } else { $null }
    batch_result_count = if ($null -ne $BatchJournal) { @($BatchJournal.results).Count } else { 0 }
    planned_account = "$HaUser@localhost"
    planned_erp_action = 'stop_only_then_restart_from_non_elevated_session'
    takeover_handshake = $UseTakeoverHandshake
    fenced_marker = if ($UseTakeoverHandshake) { $FencedMarkerPath } else { $null }
    continue_signal = if ($UseTakeoverHandshake) { $ContinueSignalPath } else { $null }
    takeover_wait_seconds = if ($UseTakeoverHandshake) { $TakeoverWaitSeconds } else { 0 }
}
if (-not $Execute) {
    $Preflight | ConvertTo-Json -Depth 6
    exit 0
}

$Timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$BootstrapSqlPath = Join-Path $BootstrapRoot "primary-ha-admin-$Timestamp.sql"
$ConfigBackupPath = Join-Path $QuarantineRoot "primary-my.ini-$Timestamp"
$ManifestPath = Join-Path $QuarantineRoot "primary-ha-admin-$Timestamp.json"
$ErpStopped = $false
$ConfigChanged = $false
$ConfigBackupHash = $null
$ExecutionSucceeded = $false
$PasswordText = $null
$PlainSecretBytes = $null
$ProtectedSecretBytes = $null
$HandoffToken = if ($UseTakeoverHandshake) { [Guid]::NewGuid().ToString('N') } else { $null }
$PreviousMySqlPassword = $env:MYSQL_PWD
$Manifest = [ordered]@{
    computer = $env:COMPUTERNAME
    started_at = (Get-Date).ToString('o')
    status = 'starting'
    backup = $BackupPath
    backup_sha256 = $BackupHash
    original_service_command = $ExpectedServiceCommand
    bootstrap_method = 'temporary_init_file_option'
    service_command_unchanged = $true
    mysql_config = $MySqlConfigPath
    mysql_config_backup = $ConfigBackupPath
    credential = $CredentialPath
    erp_pid_before = [int]$ErpProcess.ProcessId
    batch_id = $Preflight.batch_id
    batch_active_index = $Preflight.batch_active_index
    batch_result_count = $Preflight.batch_result_count
    takeover_handshake = $UseTakeoverHandshake
    fenced_marker = if ($UseTakeoverHandshake) { $FencedMarkerPath } else { $null }
    continue_signal = if ($UseTakeoverHandshake) { $ContinueSignalPath } else { $null }
}

try {
    foreach ($Directory in @($HaRoot, $BootstrapRoot, $SecretsRoot, $QuarantineRoot)) {
        New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    }
    if ($UseTakeoverHandshake) {
        New-Item -ItemType Directory -Path $HandoffRoot -Force | Out-Null
    }
    Protect-Directory -Path $HaRoot -AllowNetworkServiceRead
    Protect-Directory -Path $BootstrapRoot -AllowNetworkServiceRead
    Protect-Directory -Path $SecretsRoot
    Protect-Directory -Path $QuarantineRoot
    if ($UseTakeoverHandshake) {
        Protect-Directory -Path $HandoffRoot
    }
    Copy-Item -LiteralPath $MySqlConfigPath -Destination $ConfigBackupPath
    Protect-File -Path $ConfigBackupPath
    $ConfigBackupHash = (
        Get-FileHash -LiteralPath $ConfigBackupPath -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    $Manifest.mysql_config_backup_sha256 = $ConfigBackupHash
    Write-JsonFile -Path $ManifestPath -Value $Manifest
    Protect-File -Path $ManifestPath

    $RandomBytes = New-Object byte[] 36
    $RandomGenerator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $RandomGenerator.GetBytes($RandomBytes)
        $PasswordText = [Convert]::ToBase64String($RandomBytes).TrimEnd('=') + 'aA!9'
    }
    finally {
        $RandomGenerator.Dispose()
        [Array]::Clear($RandomBytes, 0, $RandomBytes.Length)
    }

    Add-Type -AssemblyName System.Security
    $SecretText = "$HaUser`n$PasswordText"
    $PlainSecretBytes = [Text.Encoding]::UTF8.GetBytes($SecretText)
    $ProtectedSecretBytes = [Security.Cryptography.ProtectedData]::Protect(
        $PlainSecretBytes,
        $null,
        [Security.Cryptography.DataProtectionScope]::LocalMachine
    )
    [IO.File]::WriteAllBytes($CredentialPath, $ProtectedSecretBytes)
    Protect-File -Path $CredentialPath
    $SecretText = $null

    $BootstrapSql = @"
CREATE USER IF NOT EXISTS '$HaUser'@'localhost' IDENTIFIED WITH caching_sha2_password BY '$PasswordText';
ALTER USER '$HaUser'@'localhost' IDENTIFIED WITH caching_sha2_password BY '$PasswordText';
GRANT SELECT, RELOAD, PROCESS, REPLICATION CLIENT, REPLICATION SLAVE, CREATE USER ON *.* TO '$HaUser'@'localhost' WITH GRANT OPTION;
GRANT SYSTEM_VARIABLES_ADMIN, PERSIST_RO_VARIABLES_ADMIN, REPLICATION_SLAVE_ADMIN, CONNECTION_ADMIN, BACKUP_ADMIN ON *.* TO '$HaUser'@'localhost';
"@
    Write-Utf8NoBom -Path $BootstrapSqlPath -Content $BootstrapSql
    Protect-File -Path $BootstrapSqlPath -AllowNetworkServiceRead

    Stop-Process -Id ([int]$ErpProcess.ProcessId) -ErrorAction Stop
    $ErpStopped = $true
    $ErpReleaseDeadline = (Get-Date).AddSeconds(15)
    while (
        (Get-Date) -lt $ErpReleaseDeadline -and
        (Get-NetTCPConnection -State Listen -LocalPort $ErpPort -ErrorAction SilentlyContinue)
    ) {
        Start-Sleep -Milliseconds 250
    }
    if (Get-NetTCPConnection -State Listen -LocalPort $ErpPort -ErrorAction SilentlyContinue) {
        throw "The formal ERP did not release port $ErpPort."
    }

    if ($UseTakeoverHandshake) {
        $FencedPayload = [ordered]@{
            computer = $env:COMPUTERNAME
            state = 'erp_fenced'
            token = $HandoffToken
            fenced_at = (Get-Date).ToString('o')
            erp_pid_before = [int]$ErpProcess.ProcessId
            batch_id = $Preflight.batch_id
            batch_active_index = $Preflight.batch_active_index
            batch_result_count = $Preflight.batch_result_count
            mysql_service_state = [string](Get-Service -Name $ServiceName).Status
        }
        Write-JsonFile -Path $FencedMarkerPath -Value $FencedPayload
        Protect-File -Path $FencedMarkerPath
        $Manifest.erp_fenced_at = $FencedPayload.fenced_at
        $Manifest.handoff_token = $HandoffToken
        Write-JsonFile -Path $ManifestPath -Value $Manifest
        Protect-File -Path $ManifestPath

        $TakeoverDeadline = (Get-Date).AddSeconds($TakeoverWaitSeconds)
        while (
            (Get-Date) -lt $TakeoverDeadline -and
            -not (Test-Path -LiteralPath $ContinueSignalPath -PathType Leaf)
        ) {
            Start-Sleep -Milliseconds 250
        }
        if (-not (Test-Path -LiteralPath $ContinueSignalPath -PathType Leaf)) {
            throw "Timed out waiting for the laptop takeover signal after $TakeoverWaitSeconds seconds."
        }
        $ContinueSignal = Read-Utf8JsonWithRetry -Path $ContinueSignalPath
        if (
            [string]$ContinueSignal.token -cne $HandoffToken -or
            [string]$ContinueSignal.state -cne 'laptop_healthy'
        ) {
            throw 'Safety stop: laptop takeover signal did not match this fenced session.'
        }
        $Manifest.laptop_takeover_confirmed_at = (Get-Date).ToString('o')
        $Manifest.laptop_health_url = [string]$ContinueSignal.health_url
        Write-JsonFile -Path $ManifestPath -Value $Manifest
        Protect-File -Path $ManifestPath
    }

    Stop-Service -Name $ServiceName -Force
    Wait-ServiceState -Name $ServiceName -State 'Stopped' -TimeoutSeconds 60

    $Utf8Encoding = [Text.UTF8Encoding]::new($false, $true)
    $ConfigText = [IO.File]::ReadAllText($MySqlConfigPath, $Utf8Encoding)
    $MySqlGroup = [regex]::Match(
        $ConfigText,
        '(?im)^[ \t]*\[mysqld\][ \t]*(?:\r?\n|$)'
    )
    if (-not $MySqlGroup.Success) {
        throw 'The MySQL option file does not contain a [mysqld] group.'
    }
    $NewLine = if ($ConfigText.Contains("`r`n")) { "`r`n" } else { "`n" }
    $BootstrapOptionPath = $BootstrapSqlPath.Replace('\', '/')
    $BootstrapConfigEntry = (
        "# Takealot HA one-time bootstrap $Timestamp" + $NewLine +
        "init-file=$BootstrapOptionPath" + $NewLine
    )
    $UpdatedConfigText = $ConfigText.Insert(
        $MySqlGroup.Index + $MySqlGroup.Length,
        $BootstrapConfigEntry
    )
    $ConfigChanged = $true
    [IO.File]::WriteAllText($MySqlConfigPath, $UpdatedConfigText, $Utf8Encoding)
    $Manifest.bootstrap_config_sha256 = (
        Get-FileHash -LiteralPath $MySqlConfigPath -Algorithm SHA256
    ).Hash.ToLowerInvariant()

    Start-Service -Name $ServiceName
    Wait-ServiceState -Name $ServiceName -State 'Running' -TimeoutSeconds 60

    $Ready = $false
    $ReadyDeadline = (Get-Date).AddSeconds(60)
    do {
        $Listener = @(Get-NetTCPConnection -LocalPort 3306 -State Listen -ErrorAction SilentlyContinue)
        if ($Listener.Count -gt 0) {
            $Ready = $true
            break
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $ReadyDeadline)
    if (-not $Ready) {
        throw 'MySQL80 restarted but port 3306 did not become ready.'
    }

    Copy-Item -LiteralPath $ConfigBackupPath -Destination $MySqlConfigPath -Force
    $RestoredConfigHash = (
        Get-FileHash -LiteralPath $MySqlConfigPath -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if ($RestoredConfigHash -cne $ConfigBackupHash) {
        throw 'The MySQL option file did not restore to its original SHA256.'
    }
    $ConfigChanged = $false
    Remove-Item -LiteralPath $BootstrapSqlPath -Force

    $env:MYSQL_PWD = $PasswordText
    $VerificationSql = (
        'SELECT CURRENT_USER(), VERSION(), @@GLOBAL.server_id; ' +
        'SHOW GRANTS FOR CURRENT_USER();'
    )
    $SavedErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $VerificationOutput = @(
            & $MySqlClientPath `
                --protocol=TCP `
                --host=127.0.0.1 `
                --port=3306 `
                --connect-timeout=10 `
                --user=$HaUser `
                --batch `
                --skip-column-names `
                --execute=$VerificationSql 2>&1
        )
        $VerificationExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $SavedErrorActionPreference
    }
    if ($VerificationExitCode -ne 0) {
        throw 'The bootstrapped HA administrator failed its local verification login.'
    }
    $VerificationText = @($VerificationOutput | ForEach-Object { [string]$_ })
    if (
        -not ($VerificationText -match "$HaUser@localhost") -or
        -not ($VerificationText -match 'SYSTEM_VARIABLES_ADMIN')
    ) {
        throw 'The bootstrapped HA administrator is missing its expected identity or privileges.'
    }

    $Manifest.status = 'success'
    $Manifest.completed_at = (Get-Date).ToString('o')
    $Manifest.service_command_restored = $true
    $Manifest.mysql_config_restored = $true
    $Manifest.mysql_config_restored_sha256 = $RestoredConfigHash
    $Manifest.mysql_service_state = [string](Get-Service -Name $ServiceName).Status
    $Manifest.erp_restart_required = $ErpStopped
    Write-JsonFile -Path $ManifestPath -Value $Manifest
    Protect-File -Path $ManifestPath
    $ExecutionSucceeded = $true

    [pscustomobject]@{
        computer = $env:COMPUTERNAME
        status = 'success'
        mysql_service_state = [string](Get-Service -Name $ServiceName).Status
        mysql_service_command = [string](
            Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
        ).PathName
        account = "$HaUser@localhost"
        credential = $CredentialPath
        bootstrap_sql_removed = -not (Test-Path -LiteralPath $BootstrapSqlPath)
        mysql_config_backup = $ConfigBackupPath
        manifest = $ManifestPath
        erp_stopped = $ErpStopped
        erp_restart_required = $ErpStopped
        batch_id = $Preflight.batch_id
        batch_active_index = $Preflight.batch_active_index
        batch_result_count = $Preflight.batch_result_count
    } | ConvertTo-Json -Depth 6
}
catch {
    $Failure = $_
    try {
        $Manifest.status = 'failed'
        $Manifest.failed_at = (Get-Date).ToString('o')
        $Manifest.failure = $Failure.Exception.Message
        $Manifest.erp_restart_required = $ErpStopped
        Write-JsonFile -Path $ManifestPath -Value $Manifest
        Protect-File -Path $ManifestPath
    }
    catch {
    }

    try {
        if ($ConfigChanged) {
            if ([string](Get-Service -Name $ServiceName -ErrorAction SilentlyContinue).Status -ne 'Stopped') {
                Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
                Wait-ServiceState -Name $ServiceName -State 'Stopped' -TimeoutSeconds 30
            }
            Copy-Item -LiteralPath $ConfigBackupPath -Destination $MySqlConfigPath -Force
            $RecoveredConfigHash = (
                Get-FileHash -LiteralPath $MySqlConfigPath -Algorithm SHA256
            ).Hash.ToLowerInvariant()
            if ($RecoveredConfigHash -cne $ConfigBackupHash) {
                throw 'Failed to restore the original MySQL option file after bootstrap failure.'
            }
            $ConfigChanged = $false
        }
        Set-Service -Name $ServiceName -StartupType Automatic
        if ([string](Get-Service -Name $ServiceName).Status -ne 'Running') {
            Start-Service -Name $ServiceName
            Wait-ServiceState -Name $ServiceName -State 'Running' -TimeoutSeconds 60
        }
    }
    catch {
    }
    try {
        if (Test-Path -LiteralPath $BootstrapSqlPath -PathType Leaf) {
            Remove-Item -LiteralPath $BootstrapSqlPath -Force
        }
    }
    catch {
    }
    try {
        $Manifest.mysql_config_restored = -not $ConfigChanged
        $Manifest.mysql_service_state = [string](Get-Service -Name $ServiceName).Status
        $Manifest.mysql_service_command = [string](
            Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
        ).PathName
        Write-JsonFile -Path $ManifestPath -Value $Manifest
        Protect-File -Path $ManifestPath
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
    if ($null -ne $PlainSecretBytes) {
        [Array]::Clear($PlainSecretBytes, 0, $PlainSecretBytes.Length)
    }
    if ($null -ne $ProtectedSecretBytes) {
        [Array]::Clear($ProtectedSecretBytes, 0, $ProtectedSecretBytes.Length)
    }
    $PasswordText = $null
}
