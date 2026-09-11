[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ExpectedComputerName = 'LAPTOP-2T5MN8EU'
$TaskName = 'Takealot HA ERP'
$ExpectedHash = '059248841f208e36dffb4df91251bd25b2cc8a8463919de2ca3f86b02fedeaad'
$ExpectedBatch = 'scheduled-20260903-8ceabc01284c'
$StagedPath = 'D:\TakealotHA\handoff\main-final-competitor-scheduled-batch-20260904-142614.json'
$ActivePath = 'D:\南非店铺数据抓取\logs\competitor-scheduled-batch.json'
$QuarantineRoot = 'D:\TakealotHA\quarantine'

if ($env:COMPUTERNAME -cne $ExpectedComputerName) {
    throw "Safety stop: expected $ExpectedComputerName, got $env:COMPUTERNAME."
}
if (Get-NetTCPConnection -State Listen -LocalPort 8501 -ErrorAction SilentlyContinue) {
    throw 'Safety stop: laptop ERP port 8501 is still listening.'
}
$Task = Get-ScheduledTask -TaskName $TaskName
if ([string]$Task.State -eq 'Running') {
    throw 'Safety stop: laptop ERP task is still running.'
}
foreach ($Path in @($StagedPath, $ActivePath)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required checkpoint is missing: $Path"
    }
}

$StagedHash = (
    Get-FileHash -LiteralPath $StagedPath -Algorithm SHA256
).Hash.ToLowerInvariant()
if ($StagedHash -cne $ExpectedHash) {
    throw "Safety stop: staged checkpoint hash mismatch: $StagedHash"
}
$StagedJournal = Get-Content `
    -LiteralPath $StagedPath `
    -Raw `
    -Encoding UTF8 |
    ConvertFrom-Json
if (
    [string]$StagedJournal.batch_id -cne $ExpectedBatch -or
    [string]$StagedJournal.run_status -cne 'running' -or
    [int]$StagedJournal.active_item.index -ne 1390 -or
    @($StagedJournal.results).Count -ne 1303 -or
    @($StagedJournal.errors).Count -ne 87 -or
    @($StagedJournal.queue).Count -ne 1071
) {
    throw 'Safety stop: staged checkpoint content does not match the verified main checkpoint.'
}

$OldJournal = Get-Content `
    -LiteralPath $ActivePath `
    -Raw `
    -Encoding UTF8 |
    ConvertFrom-Json
$OldHash = (
    Get-FileHash -LiteralPath $ActivePath -Algorithm SHA256
).Hash.ToLowerInvariant()
New-Item -ItemType Directory -Path $QuarantineRoot -Force | Out-Null
$Timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$BackupPath = Join-Path `
    $QuarantineRoot `
    "laptop-pre-checkpoint-correction-$Timestamp.json"
$IncomingPath = "$ActivePath.incoming-$Timestamp"
Copy-Item -LiteralPath $StagedPath -Destination $IncomingPath
$IncomingHash = (
    Get-FileHash -LiteralPath $IncomingPath -Algorithm SHA256
).Hash.ToLowerInvariant()
if ($IncomingHash -cne $ExpectedHash) {
    throw 'Incoming checkpoint hash mismatch before atomic replacement.'
}

[IO.File]::Replace($IncomingPath, $ActivePath, $BackupPath, $true)
$InstalledHash = (
    Get-FileHash -LiteralPath $ActivePath -Algorithm SHA256
).Hash.ToLowerInvariant()
if ($InstalledHash -cne $ExpectedHash) {
    throw 'Installed checkpoint hash mismatch.'
}

Start-ScheduledTask -TaskName $TaskName
$Healthy = $false
$Deadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $Deadline) {
    Start-Sleep -Milliseconds 500
    try {
        $Health = Invoke-RestMethod `
            -Uri 'http://127.0.0.1:8501/api/health' `
            -TimeoutSec 3
        if (
            [string]$Health.status -ceq 'ok' -and
            [string]$Health.application -ceq 'takealot-erp'
        ) {
            $Healthy = $true
            break
        }
    }
    catch {
    }
}
if (-not $Healthy) {
    throw 'Laptop ERP did not become healthy after checkpoint correction.'
}

$PostJournal = $null
for ($Attempt = 1; $Attempt -le 12; $Attempt++) {
    try {
        $PostJournal = Get-Content `
            -LiteralPath $ActivePath `
            -Raw `
            -Encoding UTF8 |
            ConvertFrom-Json
        break
    }
    catch {
        Start-Sleep -Milliseconds 250
    }
}
if ($null -eq $PostJournal) {
    throw 'Resumed journal could not be parsed.'
}
$Listener = Get-NetTCPConnection `
    -State Listen `
    -LocalPort 8501 `
    -ErrorAction SilentlyContinue |
    Select-Object -First 1

[pscustomobject]@{
    status = 'success'
    backup = $BackupPath
    backup_sha256 = (
        Get-FileHash -LiteralPath $BackupPath -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    previous_sha256 = $OldHash
    previous_active_index = $OldJournal.active_item.index
    previous_result_count = @($OldJournal.results).Count
    installed_checkpoint_sha256 = $InstalledHash
    resumed_active_index = $PostJournal.active_item.index
    resumed_next_queue_index = @($PostJournal.queue)[0].index
    resumed_result_count = @($PostJournal.results).Count
    resumed_error_count = @($PostJournal.errors).Count
    erp_pid = if ($null -ne $Listener) { $Listener.OwningProcess } else { $null }
    task_state = [string](Get-ScheduledTask -TaskName $TaskName).State
} | ConvertTo-Json -Depth 4
