[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ExpectedLaptopSourceLogFile,

    [Parameter(Mandatory = $true)]
    [long]$ExpectedLaptopSourceLogPosition,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-f0-9]{32}$')]
    [string]$SessionId,

    [Parameter(Mandatory = $false)]
    [switch]$Execute
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ExpectedComputerName = 'DESKTOP-NTRMANG'
$ExpectedLaptopHostName = 'LAPTOP-2T5MN8EU'
$ExpectedSourceUser = 'takealot_backup'
$SourceHost = '127.0.0.1'
$SourcePort = 13307
$MainPort = 3306
$ErpPort = 8501
$MySqlPath = 'C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe'
$CurlPath = Join-Path $env:SystemRoot 'System32\curl.exe'
$HaRoot = 'D:\TakealotHA'
$PrimaryCredentialPath = Join-Path $HaRoot 'secrets\primary-ha-admin.dpapi'
$SourceCredentialPath = Join-Path $HaRoot 'secrets\laptop-replication-source.dpapi'
$HandoffRoot = Join-Path $HaRoot 'handoff'
$QuarantineRoot = Join-Path $HaRoot 'quarantine'
$ResultPath = Join-Path $HandoffRoot "main-replica-$SessionId.json"
$ManagedTasks = @(
    'Takealot 店铺数据每日更新',
    'Takealot 竞品雷达每日采集',
    'Takealot 运营日报待办快照',
    'Takealot 运营日报晚间复核',
    'Takealot 运营日报周期末更新',
    'Takealot Encrypted Offsite Backup',
    'Takealot ERP 登录后自动启动',
    'Takealot ERP 健康守护'
)

function Test-IsAdministrator {
    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $Principal = [Security.Principal.WindowsPrincipal]::new($Identity)
    return $Principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

function Read-DpapiCredential {
    param([Parameter(Mandatory = $true)][string]$Path)

    $ProtectedBytes = $null
    $PlainBytes = $null
    $SecretText = $null
    try {
        Add-Type -AssemblyName System.Security
        $ProtectedBytes = [IO.File]::ReadAllBytes($Path)
        $PlainBytes = [Security.Cryptography.ProtectedData]::Unprotect(
            $ProtectedBytes,
            $null,
            [Security.Cryptography.DataProtectionScope]::LocalMachine
        )
        $SecretText = [Text.Encoding]::UTF8.GetString($PlainBytes)
        $Parts = $SecretText -split [char]10, 2
        if ($Parts.Count -ne 2) {
            throw "Credential has an unexpected format: $Path"
        }
        return [pscustomobject]@{
            User = $Parts[0].Trim()
            Password = $Parts[1].Trim()
        }
    }
    finally {
        $SecretText = $null
        if ($null -ne $PlainBytes) {
            [Array]::Clear($PlainBytes, 0, $PlainBytes.Length)
        }
        if ($null -ne $ProtectedBytes) {
            [Array]::Clear($ProtectedBytes, 0, $ProtectedBytes.Length)
        }
    }
}

function ConvertTo-MySqlStringLiteral {
    param([Parameter(Mandatory = $true)][string]$Value)

    $Escaped = $Value.Replace('\', '\\').Replace("'", "''")
    return "'$Escaped'"
}

function Invoke-MySqlStdin {
    param(
        [Parameter(Mandatory = $true)][string]$HostName,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$User,
        [Parameter(Mandatory = $true)][string]$Password,
        [Parameter(Mandatory = $true)][string]$Sql,
        [Parameter(Mandatory = $false)][switch]$IncludeColumnNames
    )

    if ($User -notmatch '^[A-Za-z0-9_]+$') {
        throw 'Safety stop: MySQL user contains unexpected characters.'
    }
    $Arguments = @(
        '--protocol=TCP',
        "--host=$HostName",
        "--port=$Port",
        '--connect-timeout=10',
        "--user=$User",
        '--batch',
        '--raw'
    )
    if (-not $IncludeColumnNames) {
        $Arguments += '--skip-column-names'
    }

    $StartInfo = [Diagnostics.ProcessStartInfo]::new()
    $StartInfo.FileName = $MySqlPath
    $StartInfo.Arguments = $Arguments -join ' '
    $StartInfo.UseShellExecute = $false
    $StartInfo.CreateNoWindow = $true
    $StartInfo.RedirectStandardInput = $true
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true
    $StartInfo.EnvironmentVariables['MYSQL_PWD'] = $Password
    $Process = [Diagnostics.Process]::new()
    $Process.StartInfo = $StartInfo
    try {
        if (-not $Process.Start()) {
            throw 'mysql.exe did not start.'
        }
        $Process.StandardInput.WriteLine($Sql)
        $Process.StandardInput.Close()
        $StandardOutput = $Process.StandardOutput.ReadToEnd()
        $StandardError = $Process.StandardError.ReadToEnd()
        $Process.WaitForExit()
        if ($Process.ExitCode -ne 0) {
            throw "mysql.exe failed with exit code $($Process.ExitCode)."
        }
        return @(
            $StandardOutput -split "`r?`n" |
                Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
        )
    }
    finally {
        if ($null -ne $Process) {
            $Process.Dispose()
        }
        $StandardOutput = $null
        $StandardError = $null
    }
}

function Get-ReplicaStatus {
    param(
        [Parameter(Mandatory = $true)][string]$User,
        [Parameter(Mandatory = $true)][string]$Password
    )

    $Lines = @(
        Invoke-MySqlStdin `
            -HostName '127.0.0.1' `
            -Port $MainPort `
            -User $User `
            -Password $Password `
            -Sql 'SHOW REPLICA STATUS;' `
            -IncludeColumnNames
    )
    if ($Lines.Count -eq 0) {
        return $null
    }
    if ($Lines.Count -lt 2) {
        throw 'Main replica status output is incomplete.'
    }
    $Headers = $Lines[0] -split "`t"
    $Values = $Lines[1] -split "`t", -1
    $Status = @{}
    for ($Index = 0; $Index -lt [Math]::Min($Headers.Count, $Values.Count); $Index++) {
        $Status[$Headers[$Index]] = $Values[$Index]
    }
    return $Status
}

function Get-LaptopBinaryLogStatus {
    param(
        [Parameter(Mandatory = $true)][string]$User,
        [Parameter(Mandatory = $true)][string]$Password
    )

    $Lines = @(
        Invoke-MySqlStdin `
            -HostName $SourceHost `
            -Port $SourcePort `
            -User $User `
            -Password $Password `
            -Sql 'SHOW MASTER STATUS;'
    )
    if ($Lines.Count -lt 1) {
        throw 'Laptop binary log status is empty.'
    }
    $Fields = $Lines[0] -split "`t"
    if ($Fields.Count -lt 2) {
        throw 'Laptop binary log status is incomplete.'
    }
    return [pscustomobject]@{
        File = [string]$Fields[0]
        Position = [long]$Fields[1]
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
if ($ExpectedLaptopSourceLogFile -notmatch '^takealot-laptop-bin\.[0-9]{6}$') {
    throw 'Safety stop: unexpected laptop binary log file name.'
}
if ($ExpectedLaptopSourceLogPosition -le 0) {
    throw 'Safety stop: laptop binary log position must be positive.'
}
if (-not (Test-Path -LiteralPath $MySqlPath -PathType Leaf)) {
    throw "Required file not found: $MySqlPath"
}
if (-not (Test-Path -LiteralPath $SourceCredentialPath -PathType Leaf)) {
    throw "Required credential not found: $SourceCredentialPath"
}
if (Test-Path -LiteralPath $ResultPath) {
    throw "Safety stop: result already exists for this session: $ResultPath"
}

$IsAdministrator = Test-IsAdministrator
$SourceCredential = Read-DpapiCredential -Path $SourceCredentialPath
$PrimaryCredential = $null
$MainFenced = $false
$ReplicationConfigured = $false
$Manifest = $null
$ManifestPath = $null
try {
    if ([string]$SourceCredential.User -cne $ExpectedSourceUser) {
        throw "Safety stop: unexpected laptop source user: $($SourceCredential.User)"
    }
    $LaptopState = @(
        Invoke-MySqlStdin `
            -HostName $SourceHost `
            -Port $SourcePort `
            -User $SourceCredential.User `
            -Password $SourceCredential.Password `
            -Sql (
                "SELECT CONCAT(CURRENT_USER(),'|',@@hostname,'|',@@server_id,'|'," +
                "@@global.read_only,'|',@@global.super_read_only,'|',@@global.log_bin," +
                "'|',@@global.log_replica_updates);"
            )
    )[0]
    $ExpectedLaptopState = (
        "$ExpectedSourceUser@127.0.0.1|$ExpectedLaptopHostName|2|0|0|1|1"
    )
    $LaptopBinaryLog = Get-LaptopBinaryLogStatus `
        -User $SourceCredential.User `
        -Password $SourceCredential.Password
    $LaptopHealth = $null
    $LaptopHealthAttempts = if ($Execute) { 6 } else { 1 }
    for ($Attempt = 1; $Attempt -le $LaptopHealthAttempts; $Attempt++) {
        try {
            $LaptopHealth = Invoke-RestMethod `
                -Uri 'http://100.122.102.37:8501/api/health' `
                -TimeoutSec 5
            if (
                [string]$LaptopHealth.status -ceq 'ok' -and
                [string]$LaptopHealth.application -ceq 'takealot-erp'
            ) {
                break
            }
        }
        catch {
            $LaptopHealth = $null
        }
        if ($null -eq $LaptopHealth -and (Test-Path -LiteralPath $CurlPath -PathType Leaf)) {
            $CurlOutput = @(
                & $CurlPath `
                    --silent `
                    --show-error `
                    --max-time 5 `
                    'http://100.122.102.37:8501/api/health' 2>$null
            )
            if ($LASTEXITCODE -eq 0) {
                try {
                    $LaptopHealth = ($CurlOutput -join "`n") | ConvertFrom-Json
                }
                catch {
                    $LaptopHealth = $null
                }
            }
        }
        if (
            $null -ne $LaptopHealth -and
            [string]$LaptopHealth.status -ceq 'ok' -and
            [string]$LaptopHealth.application -ceq 'takealot-erp'
        ) {
            break
        }
        if ($Attempt -lt $LaptopHealthAttempts) {
            Start-Sleep -Milliseconds 500
        }
    }

    $PrimaryCredentialExists = Test-Path `
        -LiteralPath $PrimaryCredentialPath `
        -PathType Leaf
    $Preflight = [ordered]@{
        computer = $env:COMPUTERNAME
        session_id = $SessionId
        status = if ($PrimaryCredentialExists) { 'preflight_ready' } else { 'awaiting_primary_bootstrap' }
        execute = [bool]$Execute
        is_administrator = $IsAdministrator
        primary_credential_exists = $PrimaryCredentialExists
        laptop_state = $LaptopState
        laptop_binary_log_file = $LaptopBinaryLog.File
        laptop_binary_log_position = $LaptopBinaryLog.Position
        expected_laptop_binary_log_file = $ExpectedLaptopSourceLogFile
        expected_laptop_binary_log_position = $ExpectedLaptopSourceLogPosition
        laptop_erp_health = if ($null -ne $LaptopHealth) { [string]$LaptopHealth.status } else { 'unavailable' }
        source_host = $SourceHost
        source_port = $SourcePort
        result = $ResultPath
    }
    if (-not $Execute) {
        $Preflight | ConvertTo-Json -Depth 6
        exit 0
    }
    if (-not $IsAdministrator) {
        throw 'Administrator rights are required to configure the main replica.'
    }
    if (-not $PrimaryCredentialExists) {
        throw "Primary HA credential does not exist: $PrimaryCredentialPath"
    }
    if ($LaptopState -cne $ExpectedLaptopState) {
        throw "Safety stop: unexpected laptop source state: $LaptopState"
    }
    if (
        $LaptopBinaryLog.File -cne $ExpectedLaptopSourceLogFile -or
        $LaptopBinaryLog.Position -lt $ExpectedLaptopSourceLogPosition
    ) {
        throw 'Safety stop: laptop binary log has not reached the promotion boundary.'
    }
    if (
        $null -eq $LaptopHealth -or
        [string]$LaptopHealth.status -cne 'ok' -or
        [string]$LaptopHealth.application -cne 'takealot-erp'
    ) {
        throw 'Safety stop: laptop ERP is not healthy.'
    }
    if (Get-NetTCPConnection -State Listen -LocalPort $ErpPort -ErrorAction SilentlyContinue) {
        throw 'Safety stop: main ERP is still listening during replica configuration.'
    }
    foreach ($TaskName in $ManagedTasks) {
        $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        if ($Task.Settings.Enabled) {
            throw "Safety stop: main task is still enabled: $TaskName"
        }
    }

    $PrimaryCredential = Read-DpapiCredential -Path $PrimaryCredentialPath
    if ([string]$PrimaryCredential.User -cne 'takealot_ha_admin') {
        throw "Safety stop: unexpected primary HA user: $($PrimaryCredential.User)"
    }
    $MainState = @(
        Invoke-MySqlStdin `
            -HostName '127.0.0.1' `
            -Port $MainPort `
            -User $PrimaryCredential.User `
            -Password $PrimaryCredential.Password `
            -Sql (
                "SELECT CONCAT(CURRENT_USER(),'|',@@hostname,'|',@@server_id,'|'," +
                "@@global.read_only,'|',@@global.super_read_only,'|',@@global.log_bin," +
                "'|',@@global.log_replica_updates);"
            )
    )[0]
    $ExpectedWritableMainState = 'takealot_ha_admin@localhost|DESKTOP-NTRMANG|1|0|0|1|1'
    $ExpectedFencedMainState = 'takealot_ha_admin@localhost|DESKTOP-NTRMANG|1|1|1|1|1'
    if (
        $MainState -cne $ExpectedWritableMainState -and
        $MainState -cne $ExpectedFencedMainState
    ) {
        throw "Safety stop: unexpected main MySQL state before fencing: $MainState"
    }
    $MainWasAlreadyFenced = $MainState -ceq $ExpectedFencedMainState
    if ($null -ne (Get-ReplicaStatus -User $PrimaryCredential.User -Password $PrimaryCredential.Password)) {
        throw 'Safety stop: main already has replica metadata.'
    }

    New-Item -ItemType Directory -Path $HandoffRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $QuarantineRoot -Force | Out-Null
    $Timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $ManifestPath = Join-Path $QuarantineRoot "main-replica-$Timestamp.json"
    $Manifest = [ordered]@{
        computer = $env:COMPUTERNAME
        session_id = $SessionId
        status = 'starting'
        started_at = (Get-Date).ToString('o')
        laptop_source_file = $ExpectedLaptopSourceLogFile
        laptop_source_position = $ExpectedLaptopSourceLogPosition
        source_host = $SourceHost
        source_port = $SourcePort
        main_was_already_fenced = $MainWasAlreadyFenced
    }
    Write-JsonFile -Path $ManifestPath -Value $Manifest

    if (-not $MainWasAlreadyFenced) {
        Invoke-MySqlStdin `
            -HostName '127.0.0.1' `
            -Port $MainPort `
            -User $PrimaryCredential.User `
            -Password $PrimaryCredential.Password `
            -Sql 'SET GLOBAL read_only=ON; SET GLOBAL super_read_only=ON;' |
            Out-Null
    }
    $MainFenced = $true
    $FencedState = @(
        Invoke-MySqlStdin `
            -HostName '127.0.0.1' `
            -Port $MainPort `
            -User $PrimaryCredential.User `
            -Password $PrimaryCredential.Password `
            -Sql "SELECT CONCAT(@@hostname,'|',@@server_id,'|',@@global.read_only,'|',@@global.super_read_only);"
    )[0]
    if ($FencedState -cne 'DESKTOP-NTRMANG|1|1|1') {
        throw "Main read-only fence verification failed: $FencedState"
    }

    $SourceUserLiteral = ConvertTo-MySqlStringLiteral -Value $SourceCredential.User
    $SourcePasswordLiteral = ConvertTo-MySqlStringLiteral -Value $SourceCredential.Password
    $SourceFileLiteral = ConvertTo-MySqlStringLiteral -Value $ExpectedLaptopSourceLogFile
    $ChangeSourceSql = @"
CHANGE REPLICATION SOURCE TO
  SOURCE_HOST='127.0.0.1',
  SOURCE_PORT=$SourcePort,
  SOURCE_USER=$SourceUserLiteral,
  SOURCE_PASSWORD=$SourcePasswordLiteral,
  SOURCE_LOG_FILE=$SourceFileLiteral,
  SOURCE_LOG_POS=$ExpectedLaptopSourceLogPosition,
  SOURCE_CONNECT_RETRY=5,
  GET_SOURCE_PUBLIC_KEY=1;
START REPLICA;
"@
    Invoke-MySqlStdin `
        -HostName '127.0.0.1' `
        -Port $MainPort `
        -User $PrimaryCredential.User `
        -Password $PrimaryCredential.Password `
        -Sql $ChangeSourceSql |
        Out-Null
    $ReplicationConfigured = $true
    $ChangeSourceSql = $null
    $SourcePasswordLiteral = $null

    $Replica = $null
    $Deadline = (Get-Date).AddSeconds(90)
    do {
        $Replica = Get-ReplicaStatus `
            -User $PrimaryCredential.User `
            -Password $PrimaryCredential.Password
        if (
            $null -ne $Replica -and
            [string]$Replica['Replica_IO_Running'] -ceq 'Yes' -and
            [string]$Replica['Replica_SQL_Running'] -ceq 'Yes' -and
            [string]::IsNullOrEmpty([string]$Replica['Last_IO_Error']) -and
            [string]::IsNullOrEmpty([string]$Replica['Last_SQL_Error'])
        ) {
            break
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $Deadline)
    if (
        $null -eq $Replica -or
        [string]$Replica['Replica_IO_Running'] -cne 'Yes' -or
        [string]$Replica['Replica_SQL_Running'] -cne 'Yes' -or
        [string]$Replica['Source_Host'] -cne $SourceHost -or
        [int]$Replica['Source_Port'] -ne $SourcePort -or
        -not [string]::IsNullOrEmpty([string]$Replica['Last_IO_Error']) -or
        -not [string]::IsNullOrEmpty([string]$Replica['Last_SQL_Error'])
    ) {
        throw 'Main replica did not become healthy within 90 seconds.'
    }

    $Manifest.status = 'success'
    $Manifest.completed_at = (Get-Date).ToString('o')
    $Manifest.replica_io = [string]$Replica['Replica_IO_Running']
    $Manifest.replica_sql = [string]$Replica['Replica_SQL_Running']
    $Manifest.seconds_behind_source = [string]$Replica['Seconds_Behind_Source']
    $Manifest.read_only = 1
    $Manifest.super_read_only = 1
    Write-JsonFile -Path $ManifestPath -Value $Manifest
    Write-JsonFile -Path $ResultPath -Value $Manifest
    $Manifest.manifest = $ManifestPath
    $Manifest.result = $ResultPath
    $Manifest | ConvertTo-Json -Depth 6
}
catch {
    $Failure = $_
    if ($null -ne $Manifest -and $null -ne $ManifestPath) {
        try {
            $Manifest.status = 'failed'
            $Manifest.failed_at = (Get-Date).ToString('o')
            $Manifest.failure = $Failure.Exception.Message
            $Manifest.main_fenced = $MainFenced
            $Manifest.replication_configured = $ReplicationConfigured
            Write-JsonFile -Path $ManifestPath -Value $Manifest
        }
        catch {
        }
    }
    throw $Failure
}
finally {
    if ($null -ne $PrimaryCredential) {
        $PrimaryCredential.Password = $null
    }
    if ($null -ne $SourceCredential) {
        $SourceCredential.Password = $null
    }
    $ChangeSourceSql = $null
    $SourcePasswordLiteral = $null
}
