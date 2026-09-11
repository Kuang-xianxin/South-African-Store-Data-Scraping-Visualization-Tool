[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ExpectedSourceLogFile,

    [Parameter(Mandatory = $true)]
    [long]$ExpectedSourceLogPosition,

    [Parameter(Mandatory = $true)]
    [int]$ExpectedWitnessEpoch,

    [Parameter(Mandatory = $false)]
    [switch]$Execute
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ExpectedComputerName = 'LAPTOP-2T5MN8EU'
$ExpectedSourceHost = '100.70.103.11'
$ExpectedSourcePort = 13306
$ReplicaRoot = 'D:\TakealotMySQLReplica'
$CredentialPath = Join-Path $ReplicaRoot 'secrets\mysql-dba.dpapi'
$MySqlPath = Join-Path $ReplicaRoot 'mysql-8.0.46-winx64\bin\mysql.exe'
$WitnessTaskName = 'Takealot HA Laptop Lease Guard'
$WitnessStatusPath = 'D:\TakealotHA\witness\laptop-guard-status.json'
$ErpTaskName = 'Takealot HA ERP'
$ErpPort = 8501
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

function Get-ReplicaStatus {
    $Lines = @(Invoke-MySql -Sql 'SHOW REPLICA STATUS;')
    if ($Lines.Count -lt 2) {
        throw 'Laptop replica status is empty.'
    }
    $Headers = $Lines[0] -split "`t"
    $Values = $Lines[1] -split "`t", -1
    $Status = @{}
    for ($Index = 0; $Index -lt [Math]::Min($Headers.Count, $Values.Count); $Index++) {
        $Status[$Headers[$Index]] = $Values[$Index]
    }
    return $Status
}

function Get-BinaryLogStatus {
    try {
        $Lines = @(Invoke-MySql -Sql 'SHOW BINARY LOG STATUS;')
    }
    catch {
        $Lines = @(Invoke-MySql -Sql 'SHOW MASTER STATUS;')
    }
    if ($Lines.Count -lt 2) {
        throw 'Laptop binary log status is empty.'
    }
    $Headers = $Lines[0] -split "`t"
    $Values = $Lines[1] -split "`t", -1
    $Status = @{}
    for ($Index = 0; $Index -lt [Math]::Min($Headers.Count, $Values.Count); $Index++) {
        $Status[$Headers[$Index]] = $Values[$Index]
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
    if (-not (Test-Path -LiteralPath $WitnessStatusPath -PathType Leaf)) {
        throw 'Safety stop: laptop witness guard status is missing.'
    }
    $Status = Get-Content -LiteralPath $WitnessStatusPath -Raw -Encoding UTF8 |
        ConvertFrom-Json
    if (
        [string]$Status.state -cne 'holding' -or
        [string]$Status.node -cne 'laptop-2t5mn8eu' -or
        [int]$Status.epoch -ne $ExpectedWitnessEpoch
    ) {
        throw 'Safety stop: laptop does not hold the expected witness epoch.'
    }
    $HeartbeatAt = [DateTimeOffset]::Parse([string]$Status.heartbeat_at)
    $HeartbeatAge = [DateTimeOffset]::UtcNow - $HeartbeatAt.ToUniversalTime()
    if ($HeartbeatAge.TotalSeconds -lt 0 -or $HeartbeatAge.TotalSeconds -gt 10) {
        throw "Safety stop: laptop witness heartbeat is stale: $($HeartbeatAge.TotalSeconds) seconds."
    }
    return [pscustomobject]@{
        Epoch = [int]$Status.epoch
        HeartbeatAt = $HeartbeatAt.ToString('o')
        HeartbeatAgeSeconds = [math]::Round($HeartbeatAge.TotalSeconds, 2)
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
if ($ExpectedSourceLogFile -notmatch '^DESKTOP-NTRMANG-bin\.[0-9]{6}$') {
    throw 'Safety stop: unexpected main binary log file name.'
}
if ($ExpectedSourceLogPosition -le 0) {
    throw 'Safety stop: expected main binary log position must be positive.'
}
$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$Principal = [Security.Principal.WindowsPrincipal]::new($Identity)
if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator or SYSTEM rights are required for laptop promotion.'
}
foreach ($Path in @($CredentialPath, $MySqlPath)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required file not found: $Path"
    }
}
if (Get-NetTCPConnection -LocalPort $ErpPort -State Listen -ErrorAction SilentlyContinue) {
    throw 'Safety stop: laptop ERP is already listening before promotion.'
}
if ([string](Get-ScheduledTask -TaskName $ErpTaskName -ErrorAction Stop).State -eq 'Running') {
    throw 'Safety stop: laptop HA ERP task is already running before promotion.'
}

$ProtectedBytes = $null
$PlainBytes = $null
$DbaSecret = $null
$script:DbaPassword = $null
$ReplicaStopped = $false
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
    $Replica = Get-ReplicaStatus
    if (
        [string]$Replica['Replica_IO_Running'] -cne 'Yes' -or
        [string]$Replica['Replica_SQL_Running'] -cne 'Yes' -or
        [string]$Replica['Seconds_Behind_Source'] -cne '0' -or
        [string]$Replica['Source_Host'] -cne $ExpectedSourceHost -or
        [int]$Replica['Source_Port'] -ne $ExpectedSourcePort -or
        [string]$Replica['Source_Log_File'] -cne $ExpectedSourceLogFile -or
        [string]$Replica['Relay_Source_Log_File'] -cne $ExpectedSourceLogFile -or
        [long]$Replica['Read_Source_Log_Pos'] -ne $ExpectedSourceLogPosition -or
        [long]$Replica['Exec_Source_Log_Pos'] -ne $ExpectedSourceLogPosition -or
        -not [string]::IsNullOrEmpty([string]$Replica['Last_IO_Error']) -or
        -not [string]::IsNullOrEmpty([string]$Replica['Last_SQL_Error'])
    ) {
        throw 'Safety stop: laptop replica is not exactly caught up to the fenced main position.'
    }
    $LaptopBinaryLog = Get-BinaryLogStatus
    if ([string]::IsNullOrWhiteSpace($LaptopBinaryLog.File) -or $LaptopBinaryLog.Position -le 0) {
        throw 'Safety stop: laptop binary log promotion boundary is invalid.'
    }

    $Preflight = [ordered]@{
        computer = $env:COMPUTERNAME
        status = 'preflight_ok'
        execute = [bool]$Execute
        witness_epoch = $Witness.Epoch
        witness_heartbeat_age_seconds = $Witness.HeartbeatAgeSeconds
        main_source_file = $ExpectedSourceLogFile
        main_source_position = $ExpectedSourceLogPosition
        laptop_binary_log_file = $LaptopBinaryLog.File
        laptop_binary_log_position = $LaptopBinaryLog.Position
        replica_io = [string]$Replica['Replica_IO_Running']
        replica_sql = [string]$Replica['Replica_SQL_Running']
        lag_seconds = [int]$Replica['Seconds_Behind_Source']
        read_only = 1
        super_read_only = 1
        erp_listening = $false
    }
    if (-not $Execute) {
        $Preflight | ConvertTo-Json -Depth 6
        exit 0
    }

    New-Item -ItemType Directory -Path $QuarantineRoot -Force | Out-Null
    $Timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $ManifestPath = Join-Path $QuarantineRoot "laptop-promotion-$Timestamp.json"
    $Manifest = [ordered]@{
        computer = $env:COMPUTERNAME
        started_at = (Get-Date).ToString('o')
        status = 'starting'
        witness_epoch = $Witness.Epoch
        main_source_file = $ExpectedSourceLogFile
        main_source_position = $ExpectedSourceLogPosition
        laptop_binary_log_file = $LaptopBinaryLog.File
        laptop_binary_log_position = $LaptopBinaryLog.Position
    }
    Write-JsonFile -Path $ManifestPath -Value $Manifest

    Invoke-MySql -Sql 'STOP REPLICA;' | Out-Null
    $ReplicaStopped = $true
    $Witness = Assert-FreshWitnessLease
    Invoke-MySql -Sql 'SET GLOBAL super_read_only=OFF; SET GLOBAL read_only=OFF;' |
        Out-Null
    $WritableEnabled = $true
    $WritableState = @(Invoke-MySql -Sql (
        "SELECT CONCAT(@@hostname,'|',@@server_id,'|'," +
        "@@global.read_only,'|',@@global.super_read_only);"
    ))[1]
    if ($WritableState -cne 'LAPTOP-2T5MN8EU|2|0|0') {
        throw "Laptop writable promotion verification failed: $WritableState"
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
    $Manifest.health_url = $HealthUrl
    $Manifest.witness_heartbeat_at = $Witness.HeartbeatAt
    Write-JsonFile -Path $ManifestPath -Value $Manifest

    [pscustomobject]@{
        computer = $env:COMPUTERNAME
        status = 'promoted'
        witness_epoch = $Witness.Epoch
        main_source_file = $ExpectedSourceLogFile
        main_source_position = $ExpectedSourceLogPosition
        laptop_binary_log_file = $LaptopBinaryLog.File
        laptop_binary_log_position = $LaptopBinaryLog.Position
        read_only = 0
        super_read_only = 0
        erp_health = [string]$Health.status
        health_url = $HealthUrl
        manifest = $ManifestPath
    } | ConvertTo-Json -Depth 6
}
catch {
    $Failure = $_
    try {
        if ([string](Get-ScheduledTask -TaskName $ErpTaskName -ErrorAction SilentlyContinue).State -eq 'Running') {
            Stop-ScheduledTask -TaskName $ErpTaskName -ErrorAction SilentlyContinue
        }
        $ReleaseDeadline = (Get-Date).AddSeconds(20)
        while (
            (Get-Date) -lt $ReleaseDeadline -and
            (Get-NetTCPConnection -LocalPort $ErpPort -State Listen -ErrorAction SilentlyContinue)
        ) {
            Start-Sleep -Milliseconds 250
        }
    }
    catch {
    }
    try {
        if ($null -ne $script:DbaPassword) {
            Invoke-MySql -Sql 'SET GLOBAL read_only=ON; SET GLOBAL super_read_only=ON;' |
                Out-Null
            if ($ReplicaStopped -and -not $ErpStarted) {
                Invoke-MySql -Sql 'START REPLICA;' | Out-Null
            }
        }
    }
    catch {
    }
    if ($null -ne $Manifest -and $null -ne $ManifestPath) {
        try {
            $Manifest.status = 'failed'
            $Manifest.failed_at = (Get-Date).ToString('o')
            $Manifest.failure = $Failure.Exception.Message
            $Manifest.replica_restart_safe = ($ReplicaStopped -and -not $ErpStarted)
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
