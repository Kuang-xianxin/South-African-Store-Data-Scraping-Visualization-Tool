[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^takealot-laptop-bin\.[0-9]{6}$')]
    [string]$ExpectedLaptopLogFile,

    [Parameter(Mandatory = $true)]
    [long]$ExpectedLaptopLogPosition,

    [Parameter(Mandatory = $true)]
    [int]$ExpectedWitnessEpoch,

    [Parameter(Mandatory = $false)]
    [switch]$Execute
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ExpectedComputerName = 'LAPTOP-2T5MN8EU'
$ExpectedStoppedSourceHost = '100.70.103.11'
$ExpectedStoppedSourcePort = 13306
$ReplicaRoot = 'D:\TakealotMySQLReplica'
$CredentialPath = Join-Path $ReplicaRoot 'secrets\mysql-dba.dpapi'
$MySqlPath = Join-Path $ReplicaRoot 'mysql-8.0.46-winx64\bin\mysql.exe'
$WitnessTaskName = 'Takealot HA Laptop Lease Guard'
$WitnessStatusPath = 'D:\TakealotHA\witness\laptop-guard-status.json'
$ErpTaskName = 'Takealot HA ERP'
$ErpPort = 8501
$CheckpointPath = 'D:\南非店铺数据抓取\logs\competitor-scheduled-batch.json'
$ExpectedCheckpointHash = '059248841f208e36dffb4df91251bd25b2cc8a8463919de2ca3f86b02fedeaad'
$QuarantineRoot = 'D:\TakealotHA\quarantine'

function Invoke-MySql {
    param([Parameter(Mandatory = $true)][string]$Sql)

    $PreviousPassword = $env:MYSQL_PWD
    $SavedErrorActionPreference = $ErrorActionPreference
    $env:MYSQL_PWD = $script:DbaPassword
    $ErrorActionPreference = 'Continue'
    try {
        $Output = @(
            & $MySqlPath `
                --protocol=TCP `
                --host=127.0.0.1 `
                --port=3306 `
                "--user=$script:DbaUser" `
                --batch `
                --raw `
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
        throw "mysql.exe failed with exit code $ExitCode."
    }
    return @($Output | ForEach-Object { [string]$_ })
}

function Get-TabularStatus {
    param([Parameter(Mandatory = $true)][string]$Sql)

    $Lines = @(Invoke-MySql -Sql $Sql)
    if ($Lines.Count -lt 2) {
        throw "MySQL status output is incomplete for: $Sql"
    }
    $Headers = $Lines[0].Split([char]9)
    $Values = $Lines[1].Split([char]9)
    $Status = @{}
    for ($Index = 0; $Index -lt [Math]::Min($Headers.Count, $Values.Count); $Index++) {
        $Status[$Headers[$Index]] = $Values[$Index]
    }
    return $Status
}

function Get-BinaryLogStatus {
    try {
        $Status = Get-TabularStatus -Sql 'SHOW BINARY LOG STATUS;'
    }
    catch {
        $Status = Get-TabularStatus -Sql 'SHOW MASTER STATUS;'
    }
    return [pscustomobject]@{
        File = [string]$Status['File']
        Position = [long]$Status['Position']
    }
}

function Assert-FreshWitnessLease {
    $Task = Get-ScheduledTask -TaskName $WitnessTaskName -ErrorAction Stop
    if ([string]$Task.State -cne 'Running') {
        throw 'Safety stop: laptop witness guard task is not Running.'
    }
    $Status = Get-Content `
        -LiteralPath $WitnessStatusPath `
        -Raw `
        -Encoding UTF8 |
        ConvertFrom-Json
    if (
        [string]$Status.state -cne 'holding' -or
        [string]$Status.node -cne 'laptop-2t5mn8eu' -or
        [int]$Status.epoch -ne $ExpectedWitnessEpoch
    ) {
        throw 'Safety stop: laptop does not hold the expected witness epoch.'
    }
    $HeartbeatAt = [DateTimeOffset]::Parse([string]$Status.heartbeat_at)
    $Age = [DateTimeOffset]::UtcNow - $HeartbeatAt.ToUniversalTime()
    if ($Age.TotalSeconds -lt 0 -or $Age.TotalSeconds -gt 10) {
        throw "Safety stop: laptop witness heartbeat is stale: $($Age.TotalSeconds) seconds."
    }
    return [pscustomobject]@{
        Epoch = [int]$Status.epoch
        HeartbeatAt = $HeartbeatAt.ToString('o')
        HeartbeatAgeSeconds = [math]::Round($Age.TotalSeconds, 2)
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
        [Text.UTF8Encoding]::new($false)
    )
}

if ($env:COMPUTERNAME -cne $ExpectedComputerName) {
    throw "Safety stop: expected $ExpectedComputerName, got $env:COMPUTERNAME."
}
if ($ExpectedLaptopLogPosition -le 0) {
    throw 'Safety stop: expected laptop binary log position must be positive.'
}
$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$Principal = [Security.Principal.WindowsPrincipal]::new($Identity)
if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator or SYSTEM rights are required to resume the laptop primary.'
}
foreach ($Path in @($CredentialPath, $MySqlPath, $WitnessStatusPath, $CheckpointPath)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required file not found: $Path"
    }
}
if (Get-NetTCPConnection -LocalPort $ErpPort -State Listen -ErrorAction SilentlyContinue) {
    throw 'Safety stop: laptop ERP is already listening before primary resume.'
}
if ([string](Get-ScheduledTask -TaskName $ErpTaskName).State -eq 'Running') {
    throw 'Safety stop: laptop ERP task is already running before primary resume.'
}
$CheckpointHash = (
    Get-FileHash -LiteralPath $CheckpointPath -Algorithm SHA256
).Hash.ToLowerInvariant()
if ($CheckpointHash -cne $ExpectedCheckpointHash) {
    throw "Safety stop: corrected crawler checkpoint hash changed: $CheckpointHash"
}

$ProtectedBytes = $null
$PlainBytes = $null
$DbaSecret = $null
$script:DbaPassword = $null
$WritableEnabled = $false
$ErpStarted = $false
$Manifest = $null
$ManifestPath = $null
try {
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

    $Witness = Assert-FreshWitnessLease
    $ServerState = @(Invoke-MySql -Sql (
        "SELECT CONCAT(CURRENT_USER(),'|',@@hostname,'|',@@server_id,'|'," +
        "@@global.read_only,'|',@@global.super_read_only,'|',@@global.log_bin,'|'," +
        "@@global.log_replica_updates);"
    ))[1]
    if ($ServerState -cne 'takealot_dba@127.0.0.1|LAPTOP-2T5MN8EU|2|1|1|1|1') {
        throw "Safety stop: unexpected laptop MySQL state: $ServerState"
    }
    $StoppedReplica = Get-TabularStatus -Sql 'SHOW REPLICA STATUS;'
    if (
        [string]$StoppedReplica['Replica_IO_Running'] -cne 'No' -or
        [string]$StoppedReplica['Replica_SQL_Running'] -cne 'No' -or
        [string]$StoppedReplica['Source_Host'] -cne $ExpectedStoppedSourceHost -or
        [int]$StoppedReplica['Source_Port'] -ne $ExpectedStoppedSourcePort
    ) {
        throw 'Safety stop: the laptop legacy replica channel is not safely stopped.'
    }
    $LaptopBinaryLog = Get-BinaryLogStatus
    if (
        $LaptopBinaryLog.File -cne $ExpectedLaptopLogFile -or
        $LaptopBinaryLog.Position -ne $ExpectedLaptopLogPosition
    ) {
        throw (
            'Safety stop: laptop binary log is not exactly at the position already ' +
            'confirmed on the main replica.'
        )
    }

    $Preflight = [ordered]@{
        computer = $env:COMPUTERNAME
        status = 'preflight_ok'
        execute = [bool]$Execute
        witness_epoch = $Witness.Epoch
        witness_heartbeat_age_seconds = $Witness.HeartbeatAgeSeconds
        laptop_binary_log_file = $LaptopBinaryLog.File
        laptop_binary_log_position = $LaptopBinaryLog.Position
        checkpoint_sha256 = $CheckpointHash
        read_only = 1
        super_read_only = 1
        legacy_replica_io = [string]$StoppedReplica['Replica_IO_Running']
        legacy_replica_sql = [string]$StoppedReplica['Replica_SQL_Running']
        legacy_replica_source_host = [string]$StoppedReplica['Source_Host']
        legacy_replica_source_port = [int]$StoppedReplica['Source_Port']
    }
    if (-not $Execute) {
        $Preflight | ConvertTo-Json -Depth 6
        exit 0
    }

    New-Item -ItemType Directory -Path $QuarantineRoot -Force | Out-Null
    $Timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $ManifestPath = Join-Path $QuarantineRoot "laptop-primary-resume-$Timestamp.json"
    $Manifest = [ordered]@{
        computer = $env:COMPUTERNAME
        started_at = (Get-Date).ToString('o')
        status = 'starting'
        witness_epoch = $Witness.Epoch
        laptop_binary_log_file = $LaptopBinaryLog.File
        laptop_binary_log_position = $LaptopBinaryLog.Position
        checkpoint_sha256 = $CheckpointHash
        legacy_replica_source_host = [string]$StoppedReplica['Source_Host']
        legacy_replica_source_port = [int]$StoppedReplica['Source_Port']
    }
    Write-JsonFile -Path $ManifestPath -Value $Manifest

    $Witness = Assert-FreshWitnessLease
    Invoke-MySql -Sql 'SET GLOBAL super_read_only=OFF; SET GLOBAL read_only=OFF;' |
        Out-Null
    $WritableEnabled = $true
    $WritableState = @(Invoke-MySql -Sql (
        "SELECT CONCAT(@@hostname,'|',@@server_id,'|'," +
        "@@global.read_only,'|',@@global.super_read_only);"
    ))[1]
    if ($WritableState -cne 'LAPTOP-2T5MN8EU|2|0|0') {
        throw "Laptop writable resume verification failed: $WritableState"
    }
    $Witness = Assert-FreshWitnessLease

    Start-ScheduledTask -TaskName $ErpTaskName
    $ErpStarted = $true
    $HealthUrl = "http://127.0.0.1:$ErpPort/api/health"
    $HealthDeadline = (Get-Date).AddSeconds(90)
    $Health = $null
    while ((Get-Date) -lt $HealthDeadline) {
        try {
            $Health = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 3
            if (
                [string]$Health.status -ceq 'ok' -and
                [string]$Health.application -ceq 'takealot-erp'
            ) {
                break
            }
        }
        catch {
        }
        Start-Sleep -Milliseconds 500
    }
    if (
        $null -eq $Health -or
        [string]$Health.status -cne 'ok' -or
        [string]$Health.application -cne 'takealot-erp'
    ) {
        throw 'Laptop ERP did not become healthy within 90 seconds.'
    }
    $Witness = Assert-FreshWitnessLease

    $Manifest.status = 'success'
    $Manifest.completed_at = (Get-Date).ToString('o')
    $Manifest.witness_heartbeat_at = $Witness.HeartbeatAt
    $Manifest.health_url = $HealthUrl
    Write-JsonFile -Path $ManifestPath -Value $Manifest

    [pscustomobject]@{
        computer = $env:COMPUTERNAME
        status = 'resumed'
        witness_epoch = $Witness.Epoch
        laptop_binary_log_file = $LaptopBinaryLog.File
        laptop_binary_log_position = $LaptopBinaryLog.Position
        checkpoint_sha256 = $CheckpointHash
        read_only = 0
        super_read_only = 0
        erp_health = [string]$Health.status
        manifest = $ManifestPath
    } | ConvertTo-Json -Depth 6
}
catch {
    $Failure = $_
    try {
        if ([string](Get-ScheduledTask -TaskName $ErpTaskName -ErrorAction SilentlyContinue).State -eq 'Running') {
            Stop-ScheduledTask -TaskName $ErpTaskName -ErrorAction SilentlyContinue
        }
    }
    catch {
    }
    try {
        if ($WritableEnabled -and $null -ne $script:DbaPassword) {
            Invoke-MySql -Sql 'SET GLOBAL read_only=ON; SET GLOBAL super_read_only=ON;' |
                Out-Null
        }
    }
    catch {
    }
    if ($null -ne $Manifest -and $null -ne $ManifestPath) {
        try {
            $Manifest.status = 'failed'
            $Manifest.failed_at = (Get-Date).ToString('o')
            $Manifest.failure = $Failure.Exception.Message
            $Manifest.writable_was_enabled = $WritableEnabled
            $Manifest.erp_was_started = $ErpStarted
            Write-JsonFile -Path $ManifestPath -Value $Manifest
        }
        catch {
        }
    }
    throw $Failure
}
finally {
    $DbaSecret = $null
    $script:DbaPassword = $null
    if ($null -ne $PlainBytes) {
        [Array]::Clear($PlainBytes, 0, $PlainBytes.Length)
    }
    if ($null -ne $ProtectedBytes) {
        [Array]::Clear($ProtectedBytes, 0, $ProtectedBytes.Length)
    }
}
