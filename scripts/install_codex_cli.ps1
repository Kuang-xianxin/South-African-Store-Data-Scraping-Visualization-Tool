[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$runtimeRoot = Join-Path $projectRoot "tools\codex-cli"
$packageLock = Join-Path $runtimeRoot "package-lock.json"
if (-not (Test-Path -LiteralPath $packageLock -PathType Leaf)) {
    throw "Pinned Codex CLI package-lock.json is missing: $packageLock"
}

$npm = (Get-Command npm.cmd -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if ([string]::IsNullOrWhiteSpace($npm)) {
    $npm = (Get-Command npm -ErrorAction SilentlyContinue | Select-Object -First 1).Source
}
if ([string]::IsNullOrWhiteSpace($npm)) {
    throw "npm was not found; install Node.js before provisioning the Codex CLI runtime."
}

Push-Location -LiteralPath $runtimeRoot
try {
    & $npm ci --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) {
        throw "npm ci failed for the pinned Codex CLI runtime."
    }
}
finally {
    Pop-Location
}

$codexExecutables = @(
    Get-ChildItem `
        -Path (Join-Path $runtimeRoot "node_modules\@openai\codex-*\vendor\*\bin\codex.exe") `
        -File `
        -ErrorAction SilentlyContinue
)
if ($codexExecutables.Count -ne 1) {
    throw "Pinned Codex CLI executable was not installed uniquely."
}
$codexVersion = (& $codexExecutables[0].FullName --version 2>&1) -join ' '
if ($LASTEXITCODE -ne 0 -or $codexVersion -ne "codex-cli 0.147.0") {
    throw "Codex CLI version verification failed; expected codex-cli 0.147.0."
}

Write-Output "Pinned Codex CLI runtime installed: $codexVersion"
