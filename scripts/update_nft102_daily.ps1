param(
    [string]$TemplatePath,
    [datetime]$ReportDate = (Get-Date),
    [switch]$SkipCollect
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$outputRoot = Join-Path $projectRoot 'outputs\nft102-daily'

if (-not (Test-Path -LiteralPath $python)) {
    throw "未找到项目 Python 环境：$python"
}

if ([string]::IsNullOrWhiteSpace($TemplatePath)) {
    $wxworkRoot = Join-Path $env:USERPROFILE 'Documents\WXWork'
    $template = Get-ChildItem -LiteralPath $wxworkRoot -Filter '*takealot*.xlsx' -Recurse -File |
        Where-Object { $_.Name -notmatch '_NFT102_\d{4}-\d{2}-\d{2}' } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($null -eq $template) {
        throw '未找到 Takealot 访客表，请用 -TemplatePath 指定源文件。'
    }
    $TemplatePath = $template.FullName
}

$TemplatePath = (Resolve-Path -LiteralPath $TemplatePath).Path
$reportDay = $ReportDate.Date
$salesDay = $reportDay.AddDays(-1)
$dateText = $reportDay.ToString('yyyy-MM-dd')
$salesDateText = $salesDay.ToString('yyyy-MM-dd')
$dayOutput = Join-Path $outputRoot $dateText
New-Item -ItemType Directory -Path $dayOutput -Force | Out-Null

$salesComplete = $false
if (-not $SkipCollect) {
    Write-Host "正在采集 NFT102 当前 Offer 和 $salesDateText 销售数据..."
    & $python -m takealot_ops.cli collect --start $salesDateText --end $salesDateText
    if ($LASTEXITCODE -ne 0) {
        throw 'Takealot 数据采集失败，未生成表格。'
    }
    $salesComplete = $true
}

$baseName = [IO.Path]::GetFileNameWithoutExtension($TemplatePath)
# The previous day's generated workbook is the next day's baseline. Remove its
# generated suffix so filenames do not grow by another date on every run.
$baseName = [regex]::Replace(
    $baseName,
    '_NFT102_\d{4}-\d{2}-\d{2}(?:_\d+)?$',
    '',
    [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
)
$outputPath = Join-Path $dayOutput "${baseName}_NFT102_${dateText}.xlsx"
$suffix = 2
while (Test-Path -LiteralPath $outputPath) {
    $outputPath = Join-Path $dayOutput "${baseName}_NFT102_${dateText}_$suffix.xlsx"
    $suffix++
}
$auditPath = [IO.Path]::ChangeExtension($outputPath, '.核对报告.json')
$auditTextPath = [IO.Path]::ChangeExtension($outputPath, '.核对报告.txt')

$payloadArgs = @(
    (Join-Path $projectRoot 'scripts\build_nft102_payload.py'),
    '--template', $TemplatePath,
    '--report-date', $dateText,
    '--output-json', $auditPath
)
if ($salesComplete) { $payloadArgs += '--sales-complete' }
& $python @payloadArgs
if ($LASTEXITCODE -ne 0) {
    throw 'NFT102 字段匹配失败，未生成表格。'
}

$payload = Get-Content -LiteralPath $auditPath -Raw -Encoding UTF8 | ConvertFrom-Json
& $python (Join-Path $projectRoot 'scripts\write_nft102_workbook.py') `
    --source $TemplatePath `
    --output $outputPath `
    --payload $auditPath
if ($LASTEXITCODE -ne 0) {
    if (Test-Path -LiteralPath $outputPath) { Remove-Item -LiteralPath $outputPath -Force }
    throw 'NFT102 工作表写入失败。'
}

$summary = $payload.summary
Write-Host ''
Write-Host 'NFT102 日报已生成：' -ForegroundColor Green
Write-Host $outputPath
Write-Host "表格日期：$dateText；订单日期：$salesDateText"
Write-Host "商品列：$($summary.product_columns)；成功匹配：$($summary.matched_active_columns)"
Write-Host "订单件数：$($summary.ordered_units_mapped)；未匹配SKU列：$($summary.unmatched_sku_columns)"
Write-Host "无SKU列：$($summary.columns_without_sku)；跳过重复SKU列：$($summary.duplicate_sku_columns_skipped)"
Write-Host "核对报告：$auditPath"
Write-Host "运营核对说明：$auditTextPath"
