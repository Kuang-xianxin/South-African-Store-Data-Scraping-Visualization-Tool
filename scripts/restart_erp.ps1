[CmdletBinding()]
param(
    [int]$HealthTimeoutSeconds = 60,
    [int]$PrepareTimeoutSeconds = 900,
    [int]$DrainTimeoutSeconds = 600,
    [switch]$AllowLegacyDrain,
    [string]$SourceRoot = ''
)

$ErrorActionPreference = "Stop"
$restartMutex = New-Object System.Threading.Mutex(
    $false,
    'Local\TakealotErpStartup'
)
$restartMutexAcquired = $false
$bridgeActive = $false
$standby = $null
$tunnel = $null
$releaseLease = $null
$bridgeState = $null
$retainStandby = $false
$legacyProcessHandle = [IntPtr]::Zero
try {
    try {
        $restartMutexAcquired = $restartMutex.WaitOne([TimeSpan]::FromSeconds(120))
    }
    catch [System.Threading.AbandonedMutexException] {
        $restartMutexAcquired = $true
    }
    if (-not $restartMutexAcquired) {
        throw 'Another ERP startup or restart did not finish within 120 seconds.'
    }

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "Project Python not found: $pythonPath"
}

function Import-UserEnvironmentVariableIfMissing {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    $processValue = [Environment]::GetEnvironmentVariable($Name, "Process")
    if (-not [string]::IsNullOrWhiteSpace($processValue)) {
        return
    }

    $userValue = [Environment]::GetEnvironmentVariable($Name, "User")
    if (-not [string]::IsNullOrWhiteSpace($userValue)) {
        [Environment]::SetEnvironmentVariable($Name, $userValue, "Process")
    }
}

# A long-running launcher may predate a Windows user-variable change. Import
# only the two optional search-model keys, without printing or persisting values.
Import-UserEnvironmentVariableIfMissing -Name "DASHSCOPE_API_KEY"
Import-UserEnvironmentVariableIfMissing -Name "ARK_API_KEY"

$env:TAKEALOT_PROJECT_ROOT = $projectRoot
$activeSourcePath = Join-Path $projectRoot 'data\active-erp-source.json'
if ([string]::IsNullOrWhiteSpace($SourceRoot) -and (Test-Path -LiteralPath $activeSourcePath)) {
    $SourceRoot = (Get-Content -LiteralPath $activeSourcePath -Raw | ConvertFrom-Json).source_root
}
if ([string]::IsNullOrWhiteSpace($SourceRoot)) { $SourceRoot = $projectRoot }
$SourceRoot = (Resolve-Path -LiteralPath $SourceRoot).Path
if (-not (Test-Path -LiteralPath (Join-Path $SourceRoot 'src\takealot_ops\erp\web.py'))) {
    throw 'Reviewed ERP source root is missing.'
}
$env:TAKEALOT_RELEASE_SOURCE_ROOT = $SourceRoot
$env:PYTHONPATH = Join-Path $SourceRoot 'src'
Push-Location -LiteralPath $projectRoot
try {
    $portOutput = @(
        & $pythonPath -c "from pathlib import Path; from takealot_ops.settings import DashboardSettings; print(DashboardSettings.from_env(Path.cwd()).dashboard_port)" 2>&1
    )
    $portExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
if ($portExitCode -ne 0) {
    $portText = $portOutput -join ' '
    throw "Unable to read the ERP port: $portText"
}
$port = [int]($portOutput | Select-Object -Last 1)

# A healthy formal GREEN is retired only after the next code namespace has
# complete radar projections. Cold recovery must still start when MySQL returns.
$wasHealthy = $false
try {
    $oldHealth = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 3
    $wasHealthy = $oldHealth.status -eq 'ok'
}
catch { }
if ($port -eq 8501 -and $wasHealthy) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $releaseDir = Join-Path $projectRoot "logs\prepared-release-$stamp"
    New-Item -ItemType Directory -Path $releaseDir | Out-Null
    $readyPath = Join-Path $releaseDir 'ready.json'
    $bridgeState = Join-Path $releaseDir 'bridge.json'
    $bridgeHelper = Join-Path $SourceRoot 'scripts\green_release_bridge.py'
    $releaseLease = Join-Path $projectRoot 'logs\erp-release.lease'
    if (Get-NetTCPConnection -State Listen -LocalPort 8511 -ErrorAction SilentlyContinue) {
        throw 'Release standby port 8511 is occupied; existing process was not stopped.'
    }
    $savedWebOnly = $env:TAKEALOT_WEB_ONLY
    $savedReadOnly = $env:TAKEALOT_READ_ONLY_TEST_MODE
    $savedReady = $env:TAKEALOT_RELEASE_READY
    try {
        $env:TAKEALOT_WEB_ONLY = '1'
        $env:TAKEALOT_READ_ONLY_TEST_MODE = '1'
        $env:TAKEALOT_RELEASE_READY = $readyPath
        $standby = Start-Process -FilePath $pythonPath -WorkingDirectory $projectRoot `
            -ArgumentList @('-m', 'uvicorn', 'takealot_ops.erp.restart_standby:create_standby', '--factory', '--host', '127.0.0.1', '--port', '8511', '--no-access-log') `
            -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $releaseDir 'standby.stdout.log') `
            -RedirectStandardError (Join-Path $releaseDir 'standby.stderr.log')
    }
    finally {
        $env:TAKEALOT_WEB_ONLY = $savedWebOnly
        $env:TAKEALOT_READ_ONLY_TEST_MODE = $savedReadOnly
        $env:TAKEALOT_RELEASE_READY = $savedReady
    }
    Write-Output "Preparing the replacement while the current ERP serves users: $releaseDir"
    $deadline = (Get-Date).AddSeconds($PrepareTimeoutSeconds)
    while (-not (Test-Path -LiteralPath $readyPath)) {
        if ($standby.HasExited -or (Get-Date) -gt $deadline) {
            throw "Replacement did not become ready; current ERP retained. See $releaseDir"
        }
        Start-Sleep -Milliseconds 500
    }
    $ready = Get-Content -LiteralPath $readyPath -Raw | ConvertFrom-Json
    $currentNamespace = & $pythonPath -c "from pathlib import Path; from takealot_ops.erp.radar_code_version import materialized_code_fingerprint; print(materialized_code_fingerprint(Path.cwd()))"
    if ($LASTEXITCODE -ne 0 -or $currentNamespace -ne $ready.namespace) { throw 'Source changed after preparation; current ERP retained.' }
    $candidate = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8511/api/health' -TimeoutSec 10
    if ($candidate.Headers['X-ERP-Release-Bridge'] -ne '1') { throw 'Wrong standby identity.' }
    $readerVerifier = Join-Path $SourceRoot 'scripts\verify_release_reader.py'
    & $pythonPath $readerVerifier --base 'http://127.0.0.1:8511' --ready $readyPath --output (Join-Path $releaseDir 'reader-local.json')
    if ($LASTEXITCODE -ne 0) { throw 'Standby authenticated reader failed; current ERP retained.' }
    $sshKey = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.ssh\takealot_tencent_witness_ed25519'
    $tunnel = Start-Process -FilePath 'ssh.exe' -ArgumentList @('-N', '-T', '-i', $sshKey,
        '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
        '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
        '-R', '127.0.0.1:18507:127.0.0.1:8511', 'ubuntu@119.91.117.232') `
        -WindowStyle Hidden -PassThru -RedirectStandardError (Join-Path $releaseDir 'tunnel.stderr.log')
    Start-Sleep -Seconds 2
    if ($tunnel.HasExited) { throw 'Release tunnel failed; current ERP retained.' }
    $bridgeActive = $true
    & $pythonPath $bridgeHelper activate --state $bridgeState
    if ($LASTEXITCODE -ne 0) { throw 'Public read bridge activation failed; current ERP retained.' }
    $bridgeActive = $true
    # nginx reload acknowledges the signal before every new worker is serving.
    # Require observed public readiness, allowing bounded propagation time.
    $bridgeConfirmed = $false
    $bridgeDeadline = (Get-Date).AddSeconds(20)
    do {
        $public = Invoke-WebRequest -UseBasicParsing -DisableKeepAlive -Uri 'https://119.91.117.232/api/health' -TimeoutSec 10
        if ($public.Headers['X-ERP-Release-Bridge'] -eq '1') { $bridgeConfirmed = $true; break }
        Start-Sleep -Milliseconds 300
    } while ((Get-Date) -lt $bridgeDeadline)
    if (-not $bridgeConfirmed) { throw 'Public bridge identity check failed; old ERP retained.' }
    & $pythonPath $readerVerifier --base 'https://119.91.117.232' --ready $readyPath --output (Join-Path $releaseDir 'reader-public.json')
    if ($LASTEXITCODE -ne 0) { throw 'Public authenticated reader failed; current ERP retained.' }
    Write-Output 'Public reads now use the prepared standby; waiting for safe collection/HTTP boundary.'
    $batchPath = Join-Path $projectRoot 'logs\competitor-scheduled-batch.json'
    $queuePath = Join-Path $projectRoot 'logs\competitor-batch-queue.json'
    $legacyOwner = @(Get-NetTCPConnection -State Listen -LocalPort $port).OwningProcess | Select-Object -Unique
    if ($AllowLegacyDrain) {
        if (@($legacyOwner).Count -ne 1) { throw 'Legacy boundary requires exactly one ERP listener.' }
        $legacyProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $legacyOwner"
        if ($legacyProcess.CommandLine -notlike '*takealot_ops.erp.web:app*') { throw 'Unexpected legacy listener.' }
        if (-not ('ErpReleaseNative' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class ErpReleaseNative {
 [DllImport("kernel32.dll", SetLastError=true)] public static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
 [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr handle);
 [DllImport("ntdll.dll")] public static extern int NtSuspendProcess(IntPtr handle);
 [DllImport("ntdll.dll")] public static extern int NtResumeProcess(IntPtr handle);
}
'@
        }
    }
    $deadline = (Get-Date).AddSeconds($DrainTimeoutSeconds)
    do {
        Set-Content -LiteralPath $releaseLease -Value $stamp -Encoding UTF8
        $drained = $false
        $legacyDrain = $false
        try {
            $status = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/internal/release-status" -TimeoutSec 3
            $drained = $status.draining -and $status.active_requests -eq 0 -and -not $status.background_busy
        }
        catch {
            $legacyStatus = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
            if (-not $AllowLegacyDrain -or $legacyStatus -notin @(401, 404)) { throw 'Old service cannot prove drain readiness; no process stopped.' }
            $drained = $true
            $legacyDrain = $true
        }
        $batch = if (Test-Path -LiteralPath $batchPath) { Get-Content -LiteralPath $batchPath -Raw | ConvertFrom-Json } else { $null }
        if ($drained -and ($null -eq $batch -or $null -eq $batch.active_item)) {
            if ($legacyDrain) {
                # One-time migration: freeze the verified old process, then
                # reread its persisted boundary. If it already began an item,
                # resume immediately and wait; never stop an active item.
                $legacyProcessHandle = [ErpReleaseNative]::OpenProcess(0x0800, $false, $legacyOwner)
                if ($legacyProcessHandle -eq [IntPtr]::Zero) { throw 'Cannot freeze legacy checkpoint safely.' }
                if ([ErpReleaseNative]::NtSuspendProcess($legacyProcessHandle) -ne 0) { throw 'Legacy checkpoint freeze failed.' }
                $batch = if (Test-Path -LiteralPath $batchPath) { Get-Content -LiteralPath $batchPath -Raw | ConvertFrom-Json } else { $null }
                if ($null -ne $batch -and $null -ne $batch.active_item) {
                    [ErpReleaseNative]::NtResumeProcess($legacyProcessHandle) | Out-Null
                    [ErpReleaseNative]::CloseHandle($legacyProcessHandle) | Out-Null
                    $legacyProcessHandle = [IntPtr]::Zero
                    Start-Sleep -Milliseconds 200
                    continue
                }
            }
            break
        }
        if ((Get-Date) -gt $deadline) { throw 'Safe drain boundary timed out; current ERP retained.' }
        Start-Sleep -Milliseconds 200
    } while ($true)
    if ($null -ne $batch) { Copy-Item -LiteralPath $batchPath -Destination (Join-Path $releaseDir 'batch-before.json') }
    if (Test-Path -LiteralPath $queuePath) { Copy-Item -LiteralPath $queuePath -Destination (Join-Path $releaseDir 'queue-before.json') }
}

$listeners = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
foreach ($listener in $listeners) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
    if ($null -eq $process -or $process.CommandLine -notlike "*takealot_ops.erp.web:app*") {
        throw "Port $port is owned by a process outside this ERP; restart aborted."
    }
    Stop-Process -Id $listener.OwningProcess
    Wait-Process -Id $listener.OwningProcess -Timeout 10 -ErrorAction SilentlyContinue
}

# The old collector is gone. The new process may resume the original checkpoint.
if ($releaseLease -and (Test-Path -LiteralPath $releaseLease)) { Remove-Item -LiteralPath $releaseLease }

$releaseDeadline = (Get-Date).AddSeconds(10)
while (
    (Get-Date) -lt $releaseDeadline -and
    (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
) {
    Start-Sleep -Milliseconds 250
}
if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
    throw "The old ERP stopped, but port $port was not released within 10 seconds."
}

$logDirectory = Join-Path $projectRoot "logs"
if ($bridgeActive) {
    & $pythonPath (Join-Path $SourceRoot 'scripts\install_prepared_radar.py') --ready $readyPath --port $port
    if ($LASTEXITCODE -ne 0) { throw 'Prepared cache installation failed; public reader retained.' }
}
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$stdoutPath = Join-Path $logDirectory "erp.stdout.log"
$stderrPath = Join-Path $logDirectory "erp.stderr.log"
$launcher = Start-Process `
    -FilePath $pythonPath `
    -ArgumentList @("-m", "takealot_ops.cli", "dashboard") `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath `
    -PassThru

$healthUrl = "http://127.0.0.1:$port/api/health"
$healthDeadline = (Get-Date).AddSeconds($HealthTimeoutSeconds)
$healthy = $false
while ((Get-Date) -lt $healthDeadline) {
    if ($launcher.HasExited) {
        break
    }
    try {
        $response = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
        if ($response.status -eq "ok") {
            $healthy = $true
            break
        }
    }
    catch {
        Start-Sleep -Milliseconds 500
    }
}

if (-not $healthy) {
    $stderrTail = if (Test-Path -LiteralPath $stderrPath) {
        (Get-Content -LiteralPath $stderrPath -Tail 20) -join [Environment]::NewLine
    }
    else {
        "No stderr log was created."
    }
    throw "ERP did not pass its health check after restart: $healthUrl`n$stderrTail"
}

Write-Output "ERP restarted and passed health check: $healthUrl (launcher PID $($launcher.Id))"
$activeSourceTemporary = "$activeSourcePath.tmp"
@{source_root=$SourceRoot; updated_at=(Get-Date).ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $activeSourceTemporary -Encoding UTF8
Move-Item -LiteralPath $activeSourceTemporary -Destination $activeSourcePath -Force
if ($bridgeActive) {
    $currentStatus = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/internal/release-status" -TimeoutSec 10
    if ($currentStatus.namespace -ne $ready.namespace -or $currentStatus.web_only) { throw 'Replacement runtime identity does not match prepared code.' }
    & $pythonPath $bridgeHelper restore --state $bridgeState
    if ($LASTEXITCODE -ne 0) { throw 'Public route restoration failed; prepared bridge retained.' }
    $bridgeActive = $false
    $readerDeadline = (Get-Date).AddSeconds($DrainTimeoutSeconds)
    do {
        $public = Invoke-WebRequest -UseBasicParsing -DisableKeepAlive -Uri 'https://119.91.117.232/api/health' -TimeoutSec 10
        $readerStatus = Invoke-RestMethod -Uri 'http://127.0.0.1:8511/api/health' -TimeoutSec 5
        if (-not $public.Headers['X-ERP-Release-Bridge'] -and $readerStatus.active_requests -eq 0) { break }
        if ((Get-Date) -gt $readerDeadline) {
            $retainStandby = $true
            throw 'Replacement serves traffic, but old reader still has active requests; reader retained.'
        }
        Start-Sleep -Milliseconds 300
    } while ($true)
    if (Test-Path -LiteralPath $batchPath) { Copy-Item -LiteralPath $batchPath -Destination (Join-Path $releaseDir 'batch-after.json') }
    if (Test-Path -LiteralPath $queuePath) { Copy-Item -LiteralPath $queuePath -Destination (Join-Path $releaseDir 'queue-after.json') }
    Write-Output "Prepared public cutover completed: $releaseDir"
}
}
finally {
    if ($legacyProcessHandle -ne [IntPtr]::Zero) {
        [ErpReleaseNative]::NtResumeProcess($legacyProcessHandle) | Out-Null
        [ErpReleaseNative]::CloseHandle($legacyProcessHandle) | Out-Null
    }
    if ($releaseLease -and (Test-Path -LiteralPath $releaseLease)) { Remove-Item -LiteralPath $releaseLease }
    if ($bridgeState -and -not (Test-Path -LiteralPath $bridgeState) -and
        -not (Test-Path -LiteralPath ([System.IO.Path]::ChangeExtension($bridgeState, 'restored.json')))) { $bridgeActive = $false }
    # On a failed restart retain the prepared public reader. On a preflight
    # failure restore the healthy old entry; never overwrite a concurrent edit.
    if ($bridgeState -and (Test-Path -LiteralPath $bridgeState)) {
        try {
            $check = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 3
            if ($check.status -eq 'ok') {
                & $pythonPath $bridgeHelper restore --state $bridgeState
                if ($LASTEXITCODE -eq 0) { $bridgeActive = $false }
            }
        }
        catch { }
    }
    if (-not $bridgeActive -and -not $retainStandby) {
        if ($tunnel -and -not $tunnel.HasExited) { Stop-Process -Id $tunnel.Id -ErrorAction SilentlyContinue }
        # The venv launcher can have a child Python listener. Stop only the
        # exact standby module; unrelated listeners are never touched.
        if ($standby) {
            Get-CimInstance Win32_Process | Where-Object {
                $_.CommandLine -like '*uvicorn takealot_ops.erp.restart_standby:create_standby*'
            } | ForEach-Object { Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }
        }
    }
    if ($restartMutexAcquired) {
        $restartMutex.ReleaseMutex()
    }
    $restartMutex.Dispose()
}
