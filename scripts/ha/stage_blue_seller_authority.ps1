param([Parameter(Mandatory=$true)][string]$PackageDirectory)
$ErrorActionPreference = 'Stop'
$blueRoot = [IO.Path]::GetFullPath('D:\TakealotBlue')
$packageRoot = (Resolve-Path -LiteralPath $PackageDirectory).Path
$manifest = Get-Content -LiteralPath (Join-Path $packageRoot 'manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.version -notin @(1,2,3) -or $manifest.kind -ne 'blue-ha-foundation-disabled') { throw 'Unexpected package' }
$allowed = @('blue_seller_authority.py','blue_seller_ledger.py','blue_seller_client.py','blue_seller_runtime.py','blue_seller_status.py',
    'blue_branch_merge.py','blue_branch_capture.py','provision_blue_seller_identity.py')
if ($manifest.version -ge 2) { $allowed += @('blue_branch_apply.py','blue_branch_review.py') }
if ($manifest.version -ge 3) { $allowed += 'blue_branch_journal.py' }
$node = Get-Content -LiteralPath (Join-Path $blueRoot 'node.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($node.computer -ne $env:COMPUTERNAME -or $node.node -notin @('main','laptop')) { throw 'Wrong BLUE node' }
if (Test-Path -LiteralPath (Join-Path $blueRoot 'state\seller-api-enabled.json')) { throw 'Enabled runtime requires a drained upgrade' }
$files = @($manifest.files.PSObject.Properties)
if ($files.Count -ne $allowed.Count -or @($files.Name | Where-Object {$_ -notin $allowed}).Count) { throw 'Unexpected file inventory' }
foreach ($entry in $files) {
    $source = Join-Path $packageRoot $entry.Name
    if ((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.Value) { throw 'Package hash mismatch' }
    $target = Join-Path $blueRoot $entry.Name
    if (Test-Path -LiteralPath $target) {
        $existingHash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($existingHash -ne $entry.Value -and (!$manifest.previous_files -or $manifest.previous_files.($entry.Name) -ne $existingHash)) {
            throw "Existing runtime differs; preserve and review it before replacement: $($entry.Name)"
        }
    }
}
# The protected BLUE directory already restricts the credential-owning identities.
# This stage never creates the enabling marker or changes a task/MySQL setting.
foreach ($entry in $files) {
    $target = Join-Path $blueRoot $entry.Name
    $alreadyCurrent = (Test-Path -LiteralPath $target) -and ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() -eq $entry.Value)
    if (-not $alreadyCurrent) {
        $temporary = Join-Path $blueRoot ($entry.Name + '.' + [guid]::NewGuid().ToString('N') + '.stage')
        Copy-Item -LiteralPath (Join-Path $packageRoot $entry.Name) -Destination $temporary
        if ((Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.Value) { throw 'Staged hash mismatch' }
        if (Test-Path -LiteralPath $target) {
            $backup = Join-Path $blueRoot ('staging\' + $entry.Name + '.' + [guid]::NewGuid().ToString('N') + '.before-seller')
            [IO.File]::Replace($temporary,$target,$backup)
        } else {
            Move-Item -LiteralPath $temporary -Destination $target
        }
    }
}
$receiptRoot = Join-Path $blueRoot 'state\seller-api'
New-Item -ItemType Directory -Path $receiptRoot -Force | Out-Null
[ordered]@{node=$node.node;files=$files.Count;seller_enabled=$false;mysql_roles_changed=$false;services_restarted=$false} | ConvertTo-Json -Compress
