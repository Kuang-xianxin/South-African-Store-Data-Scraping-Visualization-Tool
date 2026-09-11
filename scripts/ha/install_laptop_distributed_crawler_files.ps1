[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$StagingPath,

    [Parameter(Mandatory = $true)]
    [string]$QuarantinePath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ExpectedComputerName = 'LAPTOP-2T5MN8EU'
$ExpectedOriginalModelsNormalizedSha256 = (
    '6c50240f81f90f30f9f14ca2db43a0a3c63d16246b5b40ff293df0add1c41d38'
)
$OperationalBin = 'D:\TakealotHA\bin'
$OperationalProtector = Join-Path $OperationalBin 'protect_distributed_crawler_secret.ps1'
$Mappings = @(
    [pscustomobject]@{
        stage = 'models.py'
        destination = 'src\takealot_ops\storage\models.py'
        existing = 'baseline-only'
    },
    [pscustomobject]@{
        stage = 'distributed_queue.py'
        destination = 'src\takealot_ops\competitors\distributed_queue.py'
        existing = 'refuse'
    },
    [pscustomobject]@{
        stage = 'distributed_worker.py'
        destination = 'src\takealot_ops\competitors\distributed_worker.py'
        existing = 'refuse'
    },
    [pscustomobject]@{
        stage = 'target_sync.py'
        destination = 'src\takealot_ops\competitors\target_sync.py'
        existing = 'refuse'
    },
    [pscustomobject]@{
        stage = 'protect_distributed_crawler_secret.ps1'
        destination = 'scripts\ha\protect_distributed_crawler_secret.ps1'
        existing = 'refuse'
    },
    [pscustomobject]@{
        stage = 'run_laptop_distributed_worker.ps1'
        destination = 'scripts\ha\run_laptop_distributed_worker.ps1'
        existing = 'refuse'
    }
)

function Get-NormalizedSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    $Text = [IO.File]::ReadAllText($Path)
    $Normalized = $Text.Replace("`r`n", "`n").Replace("`r", "`n")
    $Bytes = [Text.Encoding]::UTF8.GetBytes($Normalized)
    $Hasher = [Security.Cryptography.SHA256]::Create()
    try {
        return -join ($Hasher.ComputeHash($Bytes) | ForEach-Object {
            $_.ToString('x2')
        })
    }
    finally {
        $Hasher.Dispose()
        [Array]::Clear($Bytes, 0, $Bytes.Length)
    }
}

if ($env:COMPUTERNAME -cne $ExpectedComputerName) {
    throw "Safety stop: expected $ExpectedComputerName, got $env:COMPUTERNAME."
}
$ProjectCandidates = @(
    Get-ChildItem -LiteralPath 'D:\' -Directory -ErrorAction Stop |
        Where-Object {
            (Test-Path -LiteralPath (Join-Path $_.FullName 'pyproject.toml') -PathType Leaf) -and
            (Test-Path -LiteralPath (
                Join-Path $_.FullName 'src\takealot_ops\storage\models.py'
            ) -PathType Leaf) -and
            (Test-Path -LiteralPath (
                Join-Path $_.FullName 'src\takealot_ops\competitors\service.py'
            ) -PathType Leaf)
        }
)
if ($ProjectCandidates.Count -ne 1) {
    throw "Expected exactly one Takealot project on D:, found $($ProjectCandidates.Count)."
}
$ProjectRoot = $ProjectCandidates[0].FullName
foreach ($Path in @($StagingPath, $QuarantinePath)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "Required deployment directory not found: $Path"
    }
}

$ModelsPath = Join-Path $ProjectRoot 'src\takealot_ops\storage\models.py'
$ModelsHash = Get-NormalizedSha256 -Path $ModelsPath
if ($ModelsHash -cne $ExpectedOriginalModelsNormalizedSha256) {
    throw 'Laptop models.py is not the verified pre-deployment baseline.'
}
foreach ($Mapping in $Mappings) {
    $StagePath = Join-Path $StagingPath $Mapping.stage
    if (-not (Test-Path -LiteralPath $StagePath -PathType Leaf)) {
        throw "Staged deployment file not found: $StagePath"
    }
    $DestinationPath = Join-Path $ProjectRoot $Mapping.destination
    if ($Mapping.existing -ceq 'refuse' -and (Test-Path -LiteralPath $DestinationPath)) {
        throw "Refusing to overwrite unexpected laptop file: $DestinationPath"
    }
}
if (Test-Path -LiteralPath $OperationalProtector) {
    throw "Refusing to overwrite unexpected operational file: $OperationalProtector"
}

$ModelsBackup = Join-Path $QuarantinePath 'models.py.before-distributed-crawler'
Copy-Item -LiteralPath $ModelsPath -Destination $ModelsBackup
$CopiedNewFiles = New-Object Collections.Generic.List[string]
$OperationalProtectorCreated = $false
try {
    foreach ($Mapping in $Mappings) {
        $StagePath = Join-Path $StagingPath $Mapping.stage
        $DestinationPath = Join-Path $ProjectRoot $Mapping.destination
        Copy-Item -LiteralPath $StagePath -Destination $DestinationPath -Force
        if ($Mapping.existing -ceq 'refuse') {
            $CopiedNewFiles.Add($DestinationPath)
        }
    }
    New-Item -ItemType Directory -Path $OperationalBin -Force | Out-Null
    Copy-Item -LiteralPath (
        Join-Path $ProjectRoot 'scripts\ha\protect_distributed_crawler_secret.ps1'
    ) -Destination $OperationalProtector
    $OperationalProtectorCreated = $true
    $Files = foreach ($Mapping in $Mappings) {
        $DestinationPath = Join-Path $ProjectRoot $Mapping.destination
        [pscustomobject]@{
            path = $Mapping.destination
            sha256 = (Get-FileHash -LiteralPath $DestinationPath -Algorithm SHA256).Hash
        }
    }
    [pscustomobject]@{
        computer = $env:COMPUTERNAME
        status = 'success'
        project_root = $ProjectRoot
        models_backup = $ModelsBackup
        operational_protector = $OperationalProtector
        files = $Files
    } | ConvertTo-Json -Depth 6
}
catch {
    $Failure = $_
    Copy-Item -LiteralPath $ModelsBackup -Destination $ModelsPath -Force `
        -ErrorAction SilentlyContinue
    foreach ($Path in $CopiedNewFiles) {
        Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    }
    if ($OperationalProtectorCreated) {
        Remove-Item -LiteralPath $OperationalProtector -Force `
            -ErrorAction SilentlyContinue
    }
    throw $Failure
}
