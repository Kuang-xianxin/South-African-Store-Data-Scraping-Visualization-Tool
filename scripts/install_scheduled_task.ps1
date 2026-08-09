param(
    [Parameter(Mandatory = $false)]
    [string]$ProjectPath = (Get-Location).Path,

    [Parameter(Mandatory = $false)]
    [ValidatePattern('^(?:[01]\d|2[0-3]):[0-5]\d$')]
    [string]$MorningAt = '10:05',

    [Parameter(Mandatory = $false)]
    [ValidatePattern('^(?:[01]\d|2[0-3]):[0-5]\d$')]
    [string]$EveningAt = '18:00',

    [Parameter(Mandatory = $false)]
    [ValidatePattern('^(?:[01]\d|2[0-3]):[0-5]\d$')]
    [string]$PreCloseAt = '09:00',

    [Parameter(Mandatory = $false)]
    [ValidatePattern('^(?:[01]\d|2[0-3]):[0-5]\d$')]
    [string]$DeadlineAt = '18:30',

    [Parameter(Mandatory = $false)]
    [ValidatePattern('^(?:[01]\d|2[0-3]):[0-5]\d$')]
    [string]$CompetitorCollectionAt = '09:00'
)

$ErrorActionPreference = 'Stop'
$ResolvedProjectPath = (Resolve-Path -LiteralPath $ProjectPath).Path
$PythonPath = Join-Path $ResolvedProjectPath '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Project Python environment not found: $PythonPath"
}

$EscapedProjectPath = $ResolvedProjectPath.Replace("'", "''")
$EscapedPythonPath = $PythonPath.Replace("'", "''")
$TaskSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5)

function New-TakealotTaskAction {
    param([Parameter(Mandatory = $true)][string]$CliArguments)
    $Command = "`$env:TAKEALOT_PROJECT_ROOT='$EscapedProjectPath'; " +
        "Set-Location -LiteralPath '$EscapedProjectPath'; " +
        "& '$EscapedPythonPath' -m takealot_ops.cli $CliArguments; " +
        "exit `$LASTEXITCODE"
    $ActionArguments = "-NoProfile -NonInteractive -WindowStyle Hidden -Command `"$Command`""
    return New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $ActionArguments
}

$ChineseDailyUpdate = -join [char[]](
    0x5E97, 0x94FA, 0x6570, 0x636E, 0x6BCF, 0x65E5, 0x66F4, 0x65B0
)
$ChineseEveningReview = -join [char[]](
    0x8FD0, 0x8425, 0x65E5, 0x62A5, 0x665A, 0x95F4, 0x590D, 0x6838
)
$ChinesePreCloseUpdate = -join [char[]](
    0x8FD0, 0x8425, 0x65E5, 0x62A5, 0x5468, 0x671F, 0x672B, 0x66F4, 0x65B0
)
$ChineseDeadline = -join [char[]](
    0x8FD0, 0x8425, 0x65E5, 0x62A5, 0x5F85, 0x529E, 0x5FEB, 0x7167
)
$ChineseCompetitorCollection = -join [char[]](
    0x7ADE, 0x54C1, 0x96F7, 0x8FBE,
    0x6BCF, 0x65E5, 0x91C7, 0x96C6
)
$ChineseObsoleteFollowerTracking = -join [char[]](
    0x81EA, 0x6709, 0x5546, 0x54C1, 0x8DDF,
    0x5356, 0x81EA, 0x52A8, 0x8FFD, 0x8E2A
)

$TaskDefinitions = @(
    @{
        Name = "Takealot $ChineseDailyUpdate"
        At = $MorningAt
        Arguments = 'daily-report-run --slot morning --all-stores'
        Description = 'Morning collection and immutable operations report capture.'
    },
    @{
        Name = "Takealot $ChineseEveningReview"
        At = $EveningAt
        Arguments = 'daily-report-run --slot evening --all-stores'
        Description = 'Evening collection and immutable operations report capture.'
    },
    @{
        Name = "Takealot $ChinesePreCloseUpdate"
        At = $PreCloseAt
        Arguments = 'daily-report-run --slot pre_close --all-stores'
        Description = 'Pre-close collection near the end of the operations report cycle.'
    },
    @{
        Name = "Takealot $ChineseDeadline"
        At = $DeadlineAt
        Arguments = 'daily-report-deadline --all-stores'
        Description = 'Snapshot unresolved report items and export confirmed work.'
    },
    @{
        Name = "Takealot $ChineseCompetitorCollection"
        At = $CompetitorCollectionAt
        Arguments = 'trigger-competitor-collection'
        Description = 'Start the same visible shared competitor batch as the ERP Start button.'
    }
)

$ObsoleteTaskName = "Takealot $ChineseObsoleteFollowerTracking"
if (Get-ScheduledTask -TaskName $ObsoleteTaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $ObsoleteTaskName -Confirm:$false
    Write-Host "Removed obsolete hidden crawler task $ObsoleteTaskName"
}

foreach ($Definition in $TaskDefinitions) {
    $Action = New-TakealotTaskAction -CliArguments $Definition.Arguments
    $Trigger = New-ScheduledTaskTrigger -Daily -At $Definition.At
    Register-ScheduledTask `
        -TaskName $Definition.Name `
        -Description $Definition.Description `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $TaskSettings `
        -Force
    Write-Host "Installed $($Definition.Name) at $($Definition.At)"
}
