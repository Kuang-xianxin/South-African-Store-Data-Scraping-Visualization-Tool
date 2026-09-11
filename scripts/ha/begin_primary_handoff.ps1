[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$VerifiedBackupPath,

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
$OperatorAccount = 'DESKTOP-NTRMANG\Mayn'
$HaRoot = 'D:\TakealotHA'
$HandoffRoot = Join-Path $HaRoot 'handoff'
$BootstrapScript = Join-Path $PSScriptRoot 'bootstrap_primary_mysql_ha_admin.ps1'
$ConfigureReplicaScript = Join-Path $PSScriptRoot 'configure_main_as_laptop_replica.ps1'
$FencedMarkerPath = Join-Path $HandoffRoot "main-fenced-$SessionId.json"
$ContinueSignalPath = Join-Path $HandoffRoot "laptop-ready-$SessionId.json"
$TaskStatePath = Join-Path $HandoffRoot "main-task-state-$SessionId.json"
$ResultPath = Join-Path $HandoffRoot "primary-bootstrap-result-$SessionId.json"
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

function Protect-HandoffDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)

    $OperatorSid = (
        New-Object Security.Principal.NTAccount($OperatorAccount)
    ).Translate([Security.Principal.SecurityIdentifier]).Value
    $CurrentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $Grants = @(
        ('*' + $OperatorSid + ':(OI)(CI)F'),
        '*S-1-5-18:(OI)(CI)F',
        '*S-1-5-32-544:(OI)(CI)F'
    )
    if ($CurrentSid -cne $OperatorSid) {
        $Grants += ('*' + $CurrentSid + ':(OI)(CI)F')
    }
    & icacls.exe @($Path, '/grant:r') @Grants '/inheritance:r' '/C' '/Q' |
        Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to protect handoff directory: $Path"
    }
}

if ($env:COMPUTERNAME -cne $ExpectedComputerName) {
    throw "Safety stop: expected $ExpectedComputerName, got $env:COMPUTERNAME."
}
foreach ($Path in @($BootstrapScript, $ConfigureReplicaScript, $VerifiedBackupPath)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required file not found: $Path"
    }
}
$TaskSnapshot = foreach ($TaskName in $ManagedTasks) {
    $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    [pscustomobject]@{
        task_name = $TaskName
        state = [string]$Task.State
        enabled = [bool]$Task.Settings.Enabled
    }
}
if (@($TaskSnapshot | Where-Object state -eq 'Running').Count -gt 0) {
    throw 'Safety stop: one or more managed main tasks are currently Running.'
}
if (@($TaskSnapshot | Where-Object { -not $_.enabled }).Count -gt 0) {
    throw 'Safety stop: one or more managed main tasks are unexpectedly disabled.'
}
foreach ($Path in @(
    $FencedMarkerPath,
    $ContinueSignalPath,
    $TaskStatePath,
    $ResultPath
)) {
    if (Test-Path -LiteralPath $Path) {
        throw "Safety stop: handoff artifact already exists: $Path"
    }
}

$BootstrapPreflight = & $BootstrapScript `
    -VerifiedBackupPath $VerifiedBackupPath `
    -FencedMarkerPath $FencedMarkerPath `
    -ContinueSignalPath $ContinueSignalPath `
    -TakeoverWaitSeconds 300 |
    ConvertFrom-Json
$Preflight = [ordered]@{
    computer = $env:COMPUTERNAME
    session_id = $SessionId
    execute = [bool]$Execute
    is_administrator = Test-IsAdministrator
    managed_tasks = $TaskSnapshot
    fenced_marker = $FencedMarkerPath
    continue_signal = $ContinueSignalPath
    task_state = $TaskStatePath
    result = $ResultPath
    bootstrap_preflight = $BootstrapPreflight
}
if (-not $Execute) {
    $Preflight | ConvertTo-Json -Depth 8
    exit 0
}
if (-not $Preflight.is_administrator) {
    throw 'Administrator rights are required to begin the primary handoff.'
}

New-Item -ItemType Directory -Path $HandoffRoot -Force | Out-Null
Protect-HandoffDirectory -Path $HandoffRoot
Write-JsonFile -Path $TaskStatePath -Value ([ordered]@{
    computer = $env:COMPUTERNAME
    session_id = $SessionId
    captured_at = (Get-Date).ToString('o')
    tasks = $TaskSnapshot
})

$TasksDisabled = $false
try {
    foreach ($TaskName in $ManagedTasks) {
        Disable-ScheduledTask -TaskName $TaskName | Out-Null
    }
    $TasksDisabled = $true
    $DisabledState = foreach ($TaskName in $ManagedTasks) {
        $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        [pscustomobject]@{
            task_name = $TaskName
            state = [string]$Task.State
            enabled = [bool]$Task.Settings.Enabled
        }
    }
    if (@($DisabledState | Where-Object enabled).Count -gt 0) {
        throw 'One or more managed main tasks did not become disabled.'
    }

    $BootstrapOutput = & $BootstrapScript `
        -VerifiedBackupPath $VerifiedBackupPath `
        -FencedMarkerPath $FencedMarkerPath `
        -ContinueSignalPath $ContinueSignalPath `
        -TakeoverWaitSeconds 300 `
        -Execute |
        ConvertFrom-Json
    $ContinueSignal = Get-Content -LiteralPath $ContinueSignalPath -Raw -Encoding UTF8 |
        ConvertFrom-Json
    $LaptopSourceLogFile = [string]$ContinueSignal.laptop_source_log_file
    $LaptopSourceLogPosition = [long]$ContinueSignal.laptop_source_log_position
    if ($LaptopSourceLogFile -notmatch '^takealot-laptop-bin\.[0-9]{6}$') {
        throw 'Safety stop: laptop takeover signal has an invalid source log file.'
    }
    if ($LaptopSourceLogPosition -le 0) {
        throw 'Safety stop: laptop takeover signal has an invalid source log position.'
    }
    $MainReplicaOutput = & $ConfigureReplicaScript `
        -ExpectedLaptopSourceLogFile $LaptopSourceLogFile `
        -ExpectedLaptopSourceLogPosition $LaptopSourceLogPosition `
        -SessionId $SessionId `
        -Execute |
        ConvertFrom-Json
    if ([string]$MainReplicaOutput.status -cne 'success') {
        throw 'Main replica configuration did not report success.'
    }
    $Result = [ordered]@{
        computer = $env:COMPUTERNAME
        session_id = $SessionId
        status = 'success'
        completed_at = (Get-Date).ToString('o')
        tasks_disabled = $true
        bootstrap = $BootstrapOutput
        main_replica = $MainReplicaOutput
    }
    Write-JsonFile -Path $ResultPath -Value $Result
    $Result | ConvertTo-Json -Depth 8
}
catch {
    $Failure = $_
    $Result = [ordered]@{
        computer = $env:COMPUTERNAME
        session_id = $SessionId
        status = 'failed'
        failed_at = (Get-Date).ToString('o')
        failure = $Failure.Exception.Message
        tasks_disabled = $TasksDisabled
        fenced_marker_exists = Test-Path -LiteralPath $FencedMarkerPath
        continue_signal_exists = Test-Path -LiteralPath $ContinueSignalPath
    }
    try {
        Write-JsonFile -Path $ResultPath -Value $Result
    }
    catch {
    }
    throw $Failure
}
