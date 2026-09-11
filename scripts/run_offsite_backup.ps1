param(
    [Parameter(Mandatory = $false)]
    [string]$ProjectPath = (Get-Location).Path,

    [Parameter(Mandatory = $false)]
    [string]$Repository = 'sftp:takealot-backup-laptop:/D:/TakealotOffsiteBackup/repository',

    [Parameter(Mandatory = $false)]
    [ValidateRange(0, 1048576)]
    [int]$UploadLimitKiB = 8192,

    [Parameter(Mandatory = $false)]
    [ValidateRange(0, 168)]
    [int]$MinimumIntervalHours = 6,

    [Parameter(Mandatory = $false)]
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

function Resolve-ResticExecutable {
    $Command = Get-Command 'restic.exe' -ErrorAction SilentlyContinue
    if ($null -ne $Command) {
        return $Command.Source
    }

    $PackageRoot = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
    $Candidate = Get-ChildItem `
        -LiteralPath $PackageRoot `
        -Filter 'restic*.exe' `
        -Recurse `
        -File `
        -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1
    if ($null -eq $Candidate) {
        throw 'Restic is not installed. Install winget package restic.restic first.'
    }
    return $Candidate.FullName
}

$ResolvedProjectPath = (Resolve-Path -LiteralPath $ProjectPath).Path
if ((Get-Item -LiteralPath $ResolvedProjectPath).PSDrive.Name -ne 'D') {
    throw 'The formal project must remain on drive D:.'
}

$ConfigRoot = Join-Path $env:LOCALAPPDATA 'TakealotOffsiteBackup'
$PasswordPath = Join-Path $ConfigRoot 'restic-password.clixml'
$CacheDirectory = 'D:\TakealotOffsiteCache'
$ExcludeFile = Join-Path $ResolvedProjectPath 'config\offsite-backup-excludes.txt'
$PythonPath = Join-Path $ResolvedProjectPath '.venv\Scripts\python.exe'
$StatusPath = Join-Path $ConfigRoot 'status.json'
$LogPath = Join-Path $ConfigRoot 'offsite-backup.log'
$LatestDatabaseBackup = $null
$SnapshotId = $null
$StartedAt = Get-Date
$ExitCode = 0

New-Item -ItemType Directory -Path $ConfigRoot -Force | Out-Null
New-Item -ItemType Directory -Path $CacheDirectory -Force | Out-Null

function Write-BackupLog {
    param([Parameter(Mandatory = $true)][string]$Message)
    $Line = "[$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz'))] $Message"
    Add-Content -LiteralPath $LogPath -Value $Line -Encoding UTF8
    Write-Host $Line
}

function Write-OffsiteStatus {
    param(
        [Parameter(Mandatory = $true)][string]$State,
        [Parameter(Mandatory = $false)][string]$ErrorMessage = ''
    )
    $Document = [ordered]@{
        format_version = 1
        state = $State
        computer_name = $env:COMPUTERNAME
        repository = $Repository
        project_path = $ResolvedProjectPath
        started_at = $StartedAt.ToUniversalTime().ToString('o')
        updated_at = (Get-Date).ToUniversalTime().ToString('o')
        latest_database_backup = if ($null -eq $LatestDatabaseBackup) {
            $null
        } else {
            $LatestDatabaseBackup.Name
        }
        snapshot_id = $SnapshotId
        error = if ([string]::IsNullOrWhiteSpace($ErrorMessage)) {
            $null
        } else {
            $ErrorMessage
        }
    }
    if ($State -eq 'completed') {
        $Document['completed_at'] = (Get-Date).ToUniversalTime().ToString('o')
    }
    $PartialPath = "$StatusPath.part"
    [System.IO.File]::WriteAllText(
        $PartialPath,
        (($Document | ConvertTo-Json -Depth 4) + [Environment]::NewLine),
        (New-Object System.Text.UTF8Encoding($false))
    )
    Move-Item -LiteralPath $PartialPath -Destination $StatusPath -Force
}

function Invoke-LoggedCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$CommandArguments
    )
    Write-BackupLog -Message "Starting: $Label"
    $PreviousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $Output = & $FilePath @CommandArguments 2>&1
        $CommandExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }
    foreach ($Line in $Output) {
        Add-Content -LiteralPath $LogPath -Value ([string]$Line) -Encoding UTF8
        Write-Host ([string]$Line)
    }
    if ($CommandExitCode -ne 0) {
        throw "$Label failed with exit code $CommandExitCode."
    }
    Write-BackupLog -Message "Completed: $Label"
    return $Output
}

$CreatedNew = $false
$Mutex = [System.Threading.Mutex]::new(
    $true,
    'Local\TakealotOffsiteBackup',
    [ref]$CreatedNew
)
if (-not $CreatedNew) {
    $Mutex.Dispose()
    Write-BackupLog -Message 'Another offsite backup is already running; this run is skipped.'
    exit 0
}

$PasswordPointer = [IntPtr]::Zero
try {
    if (-not $Force -and $MinimumIntervalHours -gt 0 -and
        (Test-Path -LiteralPath $StatusPath -PathType Leaf)) {
        $PreviousStatus = Get-Content -LiteralPath $StatusPath -Raw | ConvertFrom-Json
        if ($PreviousStatus.state -eq 'completed' -and $PreviousStatus.completed_at) {
            $PreviousCompletion = [datetimeoffset]::Parse(
                [string]$PreviousStatus.completed_at
            )
            if ([datetimeoffset]::UtcNow - $PreviousCompletion -lt
                [timespan]::FromHours($MinimumIntervalHours)) {
                Write-BackupLog -Message 'A recent successful snapshot exists; this run is skipped.'
                exit 0
            }
        }
    }

    foreach ($RequiredPath in @($PasswordPath, $ExcludeFile, $PythonPath)) {
        if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
            throw "Required backup file is missing: $RequiredPath"
        }
    }

    Write-OffsiteStatus -State 'running'
    $ResticPath = Resolve-ResticExecutable
    $LatestDatabaseBackup = Get-ChildItem `
        -LiteralPath (Join-Path $ResolvedProjectPath 'backups') `
        -Filter 'takealot-*.sql.gz' `
        -File |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1

    if ($null -eq $LatestDatabaseBackup -or
        [datetime]::UtcNow - $LatestDatabaseBackup.LastWriteTimeUtc -gt
        [timespan]::FromHours(20)) {
        Invoke-LoggedCommand `
            -Label 'create verified local MySQL backup' `
            -FilePath $PythonPath `
            -CommandArguments @('-m', 'takealot_ops.cli', 'backup-local') | Out-Null
        $LatestDatabaseBackup = Get-ChildItem `
            -LiteralPath (Join-Path $ResolvedProjectPath 'backups') `
            -Filter 'takealot-*.sql.gz' `
            -File |
            Sort-Object LastWriteTimeUtc -Descending |
            Select-Object -First 1
    }

    if ($null -eq $LatestDatabaseBackup) {
        throw 'No verified local MySQL backup was found.'
    }

    Invoke-LoggedCommand `
        -Label 'verify latest local MySQL backup' `
        -FilePath $PythonPath `
        -CommandArguments @(
            '-m',
            'takealot_ops.cli',
            'backup-verify',
            $LatestDatabaseBackup.FullName
        ) | Out-Null
    Invoke-LoggedCommand `
        -Label 'verify local binlog archive coverage' `
        -FilePath $PythonPath `
        -CommandArguments @('-m', 'takealot_ops.cli', 'binlog-archive-status') | Out-Null

    $SecurePassword = Import-Clixml -LiteralPath $PasswordPath
    $PasswordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR(
        $SecurePassword
    )
    $env:RESTIC_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
        $PasswordPointer
    )

    $ResticBaseArguments = @(
        '-r', $Repository,
        '--cache-dir', $CacheDirectory,
        '--retry-lock', '15m',
        '--cleanup-cache'
    )
    $BackupArguments = $ResticBaseArguments + @(
        '--limit-upload', [string]$UploadLimitKiB,
        'backup',
        $ResolvedProjectPath,
        '--exclude-file', $ExcludeFile,
        '--exclude-caches',
        '--host', $env:COMPUTERNAME,
        '--tag', 'takealot-offsite',
        '--skip-if-unchanged'
    )
    Invoke-LoggedCommand `
        -Label 'encrypted incremental upload to laptop' `
        -FilePath $ResticPath `
        -CommandArguments $BackupArguments | Out-Null

    Invoke-LoggedCommand `
        -Label 'repository metadata and pack check' `
        -FilePath $ResticPath `
        -CommandArguments ($ResticBaseArguments + @('check')) | Out-Null

    Invoke-LoggedCommand `
        -Label 'apply offsite snapshot retention' `
        -FilePath $ResticPath `
        -CommandArguments ($ResticBaseArguments + @(
            'forget',
            '--host', $env:COMPUTERNAME,
            '--tag', 'takealot-offsite',
            '--keep-last', '3',
            '--keep-daily', '14',
            '--keep-weekly', '8',
            '--keep-monthly', '12',
            '--prune'
        )) | Out-Null

    $PreviousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $SnapshotJson = & $ResticPath @ResticBaseArguments `
            snapshots --host $env:COMPUTERNAME --tag takealot-offsite --latest 1 --json `
            2>> $LogPath
        $SnapshotQueryExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }
    if ($SnapshotQueryExitCode -ne 0) {
        throw 'Unable to read the latest Restic snapshot after backup.'
    }
    $Snapshots = @($SnapshotJson | ConvertFrom-Json)
    if ($Snapshots.Count -eq 0) {
        throw 'Restic completed without returning a snapshot.'
    }
    $SnapshotId = [string]$Snapshots[0].id
    Write-OffsiteStatus -State 'completed'
    Write-BackupLog -Message "Offsite backup completed. Snapshot: $SnapshotId"
}
catch {
    $ExitCode = 1
    Write-BackupLog -Message "Offsite backup failed: $($_.Exception.Message)"
    Write-OffsiteStatus -State 'failed' -ErrorMessage $_.Exception.Message
}
finally {
    Remove-Item Env:RESTIC_PASSWORD -ErrorAction SilentlyContinue
    if ($PasswordPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($PasswordPointer)
    }
    $Mutex.ReleaseMutex()
    $Mutex.Dispose()
}

exit $ExitCode
