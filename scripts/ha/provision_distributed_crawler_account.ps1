[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$LaptopSshTarget = 'takealot-admin-laptop',

    [Parameter(Mandatory = $false)]
    [switch]$Execute,

    [Parameter(Mandatory = $false)]
    [switch]$CleanupOrphanedAccount
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ExpectedComputerName = 'DESKTOP-NTRMANG'
$ExpectedAdminUser = 'takealot_ha_admin'
$CrawlerUser = 'takealot_crawler'
$CrawlerHost = 'localhost'
$MySqlPath = 'C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe'
$AdminCredentialPath = 'D:\TakealotHA\secrets\primary-ha-admin.dpapi'
$LaptopProtectorPath = 'D:\TakealotHA\bin\protect_distributed_crawler_secret.ps1'

function Get-CryptoRandomInt {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateRange(1, 2147483647)]
        [int]$MaximumExclusive
    )

    $Generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    $Buffer = New-Object byte[] 4
    try {
        $Limit = [uint64][uint32]::MaxValue + 1
        $AcceptBelow = $Limit - ($Limit % [uint64]$MaximumExclusive)
        do {
            $Generator.GetBytes($Buffer)
            $Value = [uint64][BitConverter]::ToUInt32($Buffer, 0)
        } while ($Value -ge $AcceptBelow)
        return [int]($Value % [uint64]$MaximumExclusive)
    }
    finally {
        $Generator.Dispose()
        [Array]::Clear($Buffer, 0, $Buffer.Length)
    }
}

function New-CrawlerPassword {
    $Upper = 'ABCDEFGHJKLMNPQRSTUVWXYZ'
    $Lower = 'abcdefghijkmnopqrstuvwxyz'
    $Digits = '23456789'
    $All = $Upper + $Lower + $Digits + '-_'
    $Characters = New-Object Collections.Generic.List[char]
    foreach ($Group in @($Upper, $Lower, $Digits)) {
        $Characters.Add($Group[(Get-CryptoRandomInt -MaximumExclusive $Group.Length)])
    }
    while ($Characters.Count -lt 48) {
        $Characters.Add($All[(Get-CryptoRandomInt -MaximumExclusive $All.Length)])
    }
    for ($Index = $Characters.Count - 1; $Index -gt 0; $Index--) {
        $SwapIndex = Get-CryptoRandomInt -MaximumExclusive ($Index + 1)
        $Temporary = $Characters[$Index]
        $Characters[$Index] = $Characters[$SwapIndex]
        $Characters[$SwapIndex] = $Temporary
    }
    return -join $Characters
}

function Invoke-MySql {
    param(
        [Parameter(Mandatory = $true)][string]$Sql,
        [Parameter(Mandatory = $false)][switch]$SecretSql
    )

    $StartInfo = New-Object Diagnostics.ProcessStartInfo
    $StartInfo.FileName = $MySqlPath
    $StartInfo.Arguments = (
        '--protocol=TCP --host=127.0.0.1 --port=3306 ' +
        "--user=$script:AdminUser --batch --raw --skip-column-names"
    )
    $StartInfo.UseShellExecute = $false
    $StartInfo.CreateNoWindow = $true
    $StartInfo.RedirectStandardInput = $true
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true
    $StartInfo.EnvironmentVariables['MYSQL_PWD'] = $script:AdminPassword

    $Process = New-Object Diagnostics.Process
    $Process.StartInfo = $StartInfo
    if (-not $Process.Start()) {
        throw 'Failed to start mysql.exe for crawler account provisioning.'
    }
    try {
        $Process.StandardInput.WriteLine($Sql)
        $Process.StandardInput.Close()
        $StandardOutput = $Process.StandardOutput.ReadToEnd()
        $StandardError = $Process.StandardError.ReadToEnd()
        $Process.WaitForExit()
        if ($Process.ExitCode -ne 0) {
            if ($SecretSql) {
                throw "mysql.exe rejected the guarded account change with exit code $($Process.ExitCode)."
            }
            throw "mysql.exe failed with exit code $($Process.ExitCode). $StandardError"
        }
        return @($StandardOutput -split '\r?\n' | Where-Object { $_ -ne '' })
    }
    finally {
        $Process.Dispose()
    }
}

function Invoke-LaptopCredentialProtection {
    param([Parameter(Mandatory = $true)][string]$CredentialJson)

    $StartInfo = New-Object Diagnostics.ProcessStartInfo
    $StartInfo.FileName = (Get-Command ssh.exe -ErrorAction Stop).Source
    $StartInfo.Arguments = (
        "$LaptopSshTarget powershell.exe -NoProfile -NonInteractive " +
        "-ExecutionPolicy Bypass -File `"$LaptopProtectorPath`" " +
        '-ReadCredentialFromStdin'
    )
    $StartInfo.UseShellExecute = $false
    $StartInfo.CreateNoWindow = $true
    $StartInfo.RedirectStandardInput = $true
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true

    $Process = New-Object Diagnostics.Process
    $Process.StartInfo = $StartInfo
    if (-not $Process.Start()) {
        throw 'Failed to start the guarded laptop credential transfer.'
    }
    try {
        $Process.StandardInput.Write($CredentialJson)
        $Process.StandardInput.Close()
        $StandardOutput = $Process.StandardOutput.ReadToEnd()
        $StandardError = $Process.StandardError.ReadToEnd()
        $Process.WaitForExit()
        if ($Process.ExitCode -ne 0) {
            throw "Laptop credential protection failed with exit code $($Process.ExitCode). $StandardError"
        }
        $Result = $StandardOutput | ConvertFrom-Json -ErrorAction Stop
        if (
            [string]$Result.computer -cne 'LAPTOP-2T5MN8EU' -or
            [string]$Result.status -cne 'success' -or
            [string]$Result.credential_user -cne $CrawlerUser
        ) {
            throw 'Laptop credential protection returned an unexpected identity.'
        }
        return $Result
    }
    finally {
        $Process.Dispose()
    }
}

if ($env:COMPUTERNAME -cne $ExpectedComputerName) {
    throw "Safety stop: expected $ExpectedComputerName, got $env:COMPUTERNAME."
}
foreach ($RequiredPath in @($MySqlPath, $AdminCredentialPath)) {
    if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
        throw "Required file not found: $RequiredPath"
    }
}

Add-Type -AssemblyName System.Security
$ProtectedAdminBytes = [IO.File]::ReadAllBytes($AdminCredentialPath)
$PlainAdminBytes = [Security.Cryptography.ProtectedData]::Unprotect(
    $ProtectedAdminBytes,
    $null,
    [Security.Cryptography.DataProtectionScope]::LocalMachine
)
$AdminSecret = [Text.Encoding]::UTF8.GetString($PlainAdminBytes)
$AdminParts = $AdminSecret -split "`n", 2
if ($AdminParts.Count -ne 2 -or $AdminParts[0].Trim() -cne $ExpectedAdminUser) {
    throw 'The protected primary HA administrator credential is invalid.'
}
$script:AdminUser = $AdminParts[0].Trim()
$script:AdminPassword = $AdminParts[1].Trim()

try {
    $State = @(Invoke-MySql -Sql (
        "SELECT CONCAT(CURRENT_USER(),'|',@@hostname,'|',@@server_id,'|'," +
        "@@global.read_only,'|',@@global.super_read_only,'|',@@have_ssl);"
    ))[0]
    $StateParts = $State.Split('|')
    if (
        $StateParts.Count -ne 6 -or
        $StateParts[0] -cne 'takealot_ha_admin@localhost' -or
        $StateParts[1] -cne $ExpectedComputerName -or
        $StateParts[2] -cne '1' -or
        $StateParts[3] -cne '0' -or
        $StateParts[4] -cne '0' -or
        $StateParts[5] -cne 'YES'
    ) {
        throw "Safety stop: unexpected writable-primary state: $State"
    }
    $ExistingAccount = @(
        Invoke-MySql -Sql (
            "SELECT CONCAT(User,'@',Host) FROM mysql.user " +
            "WHERE User='$CrawlerUser' AND Host='$CrawlerHost';"
        )
    )
    $GrantableDmlCount = [int]@(
        Invoke-MySql -Sql @"
SELECT COUNT(DISTINCT CONCAT(TABLE_NAME,':',PRIVILEGE_TYPE))
FROM information_schema.TABLE_PRIVILEGES
WHERE GRANTEE='''takealot_ha_admin''@''localhost'''
  AND TABLE_SCHEMA='takealot_ops'
  AND IS_GRANTABLE='YES'
  AND CONCAT(TABLE_NAME,':',PRIVILEGE_TYPE) IN (
    'competitor_targets:INSERT',
    'competitor_targets:UPDATE',
    'competitor_target_audits:INSERT',
    'competitor_link_health:INSERT',
    'competitor_link_health:UPDATE',
    'competitor_snapshots:INSERT',
    'competitor_variant_snapshots:INSERT',
    'competitor_reviews:INSERT',
    'competitor_reviews:UPDATE',
    'competitor_collection_jobs:INSERT',
    'competitor_collection_jobs:UPDATE',
    'competitor_worker_heartbeats:INSERT',
    'competitor_worker_heartbeats:UPDATE'
  );
"@
    )[0]
    $ActiveCrawlerSessions = [int]@(
        Invoke-MySql -Sql (
            "SELECT COUNT(*) FROM information_schema.PROCESSLIST " +
            "WHERE USER='$CrawlerUser';"
        )
    )[0]
    $CrawlerHeartbeatRows = [int]@(
        Invoke-MySql -Sql (
            "SELECT COUNT(*) FROM takealot_ops.competitor_worker_heartbeats " +
            "WHERE worker_id='laptop-2t5mn8eu-clash';"
        )
    )[0]
    $RemoteProbeOutput = @(
        & ssh.exe $LaptopSshTarget powershell.exe -NoProfile -NonInteractive `
            -ExecutionPolicy Bypass -File "`"$LaptopProtectorPath`"" 2>&1
    )
    if ($LASTEXITCODE -ne 0) {
        throw "Laptop protector preflight failed: $($RemoteProbeOutput -join ' ')"
    }
    $RemoteProbe = ($RemoteProbeOutput -join [Environment]::NewLine) |
        ConvertFrom-Json -ErrorAction Stop
    if (
        [string]$RemoteProbe.computer -cne 'LAPTOP-2T5MN8EU' -or
        [string]$RemoteProbe.status -cne 'preflight_ok'
    ) {
        throw 'Laptop protector preflight returned an unexpected identity.'
    }

    $Preflight = [ordered]@{
        computer = $env:COMPUTERNAME
        status = 'preflight_ok'
        mysql_identity = $State
        account = "$CrawlerUser@$CrawlerHost"
        account_exists = $ExistingAccount.Count -eq 1
        grantable_required_dml = $GrantableDmlCount
        required_dml = 13
        active_crawler_sessions = $ActiveCrawlerSessions
        crawler_heartbeat_rows = $CrawlerHeartbeatRows
        laptop = [string]$RemoteProbe.computer
        laptop_credential_exists = [bool]$RemoteProbe.credential_exists
        execute_will_restart_mysql = $false
        execute_will_restart_erp = $false
        password_recorded = $false
    }
    if (-not $Execute) {
        $Preflight | ConvertTo-Json -Depth 6
        exit 0
    }
    if ($CleanupOrphanedAccount) {
        if (
            $ExistingAccount.Count -ne 1 -or
            [bool]$RemoteProbe.credential_exists -or
            $ActiveCrawlerSessions -ne 0 -or
            $CrawlerHeartbeatRows -ne 0
        ) {
            throw 'Safety stop: the crawler account is not a verified unused orphan.'
        }
        Invoke-MySql -Sql "DROP USER '$CrawlerUser'@'$CrawlerHost';" | Out-Null
        $Remaining = [int]@(
            Invoke-MySql -Sql (
                "SELECT COUNT(*) FROM mysql.user WHERE User='$CrawlerUser' " +
                "AND Host='$CrawlerHost';"
            )
        )[0]
        if ($Remaining -ne 0) {
            throw 'Orphaned crawler account cleanup did not pass verification.'
        }
        [pscustomobject]@{
            computer = $env:COMPUTERNAME
            status = 'orphan_cleanup_success'
            account = "$CrawlerUser@$CrawlerHost"
            active_sessions_before = $ActiveCrawlerSessions
            heartbeat_rows_before = $CrawlerHeartbeatRows
            laptop_credential_existed = [bool]$RemoteProbe.credential_exists
            mysql_restarted = $false
            erp_restarted = $false
        } | ConvertTo-Json -Depth 4
        exit 0
    }
    if ($GrantableDmlCount -ne 13) {
        throw (
            'HA administrator cannot yet delegate all 13 required table DML ' +
            'privileges; account creation was not attempted.'
        )
    }
    if ($ExistingAccount.Count -ne 0) {
        throw 'Crawler account already exists; this initial provisioning path refuses to rotate it.'
    }
    if ([bool]$RemoteProbe.credential_exists) {
        throw 'Laptop crawler credential already exists; this initial provisioning path refuses to overwrite it.'
    }

    $CrawlerPassword = New-CrawlerPassword
    $AccountCreated = $false
    try {
        $CreateAccountSql = (
            "CREATE USER '$CrawlerUser'@'$CrawlerHost' IDENTIFIED BY " +
            "'$CrawlerPassword' REQUIRE SSL;"
        )
        Invoke-MySql -Sql $CreateAccountSql -SecretSql | Out-Null
        $AccountCreated = $true

        $AccountSql = @"
GRANT SELECT ON takealot_ops.erp_stores TO '$CrawlerUser'@'$CrawlerHost';
GRANT SELECT ON takealot_ops.offer_current TO '$CrawlerUser'@'$CrawlerHost';
GRANT SELECT, INSERT, UPDATE ON takealot_ops.competitor_targets TO '$CrawlerUser'@'$CrawlerHost';
GRANT INSERT ON takealot_ops.competitor_target_audits TO '$CrawlerUser'@'$CrawlerHost';
GRANT SELECT, INSERT, UPDATE ON takealot_ops.competitor_link_health TO '$CrawlerUser'@'$CrawlerHost';
GRANT SELECT, INSERT ON takealot_ops.competitor_snapshots TO '$CrawlerUser'@'$CrawlerHost';
GRANT SELECT, INSERT ON takealot_ops.competitor_variant_snapshots TO '$CrawlerUser'@'$CrawlerHost';
GRANT SELECT, INSERT, UPDATE ON takealot_ops.competitor_reviews TO '$CrawlerUser'@'$CrawlerHost';
GRANT SELECT, INSERT, UPDATE ON takealot_ops.competitor_collection_jobs TO '$CrawlerUser'@'$CrawlerHost';
GRANT SELECT, INSERT, UPDATE ON takealot_ops.competitor_worker_heartbeats TO '$CrawlerUser'@'$CrawlerHost';
"@
        Invoke-MySql -Sql $AccountSql -SecretSql | Out-Null

        $Grants = @(Invoke-MySql -Sql "SHOW GRANTS FOR '$CrawlerUser'@'$CrawlerHost';")
        $GrantText = $Grants -join [Environment]::NewLine
        foreach ($RequiredGrant in @(
            'REQUIRE SSL',
            'competitor_collection_jobs',
            'competitor_worker_heartbeats',
            'competitor_snapshots',
            'competitor_reviews'
        )) {
            if ($GrantText -notmatch [Regex]::Escape($RequiredGrant)) {
                throw "Crawler account verification is missing $RequiredGrant."
            }
        }

        $CredentialJson = @{
            user = $CrawlerUser
            password = $CrawlerPassword
        } | ConvertTo-Json -Compress
        $LaptopResult = Invoke-LaptopCredentialProtection -CredentialJson $CredentialJson

        [pscustomobject]@{
            computer = $env:COMPUTERNAME
            status = 'success'
            account = "$CrawlerUser@$CrawlerHost"
            tls_required = $true
            laptop = [string]$LaptopResult.computer
            laptop_credential_path = [string]$LaptopResult.credential_path
            mysql_restarted = $false
            erp_restarted = $false
            password_recorded = $false
        } | ConvertTo-Json -Depth 6
    }
    catch {
        $Failure = $_
        if ($AccountCreated) {
            try {
                Invoke-MySql -Sql "DROP USER IF EXISTS '$CrawlerUser'@'$CrawlerHost';" | Out-Null
            }
            catch {
            }
        }
        throw $Failure
    }
    finally {
        $AccountSql = $null
        $CreateAccountSql = $null
        $CredentialJson = $null
        $CrawlerPassword = $null
    }
}
finally {
    $script:AdminPassword = $null
    $AdminSecret = $null
    $AdminParts = $null
    if ($null -ne $ProtectedAdminBytes) {
        [Array]::Clear($ProtectedAdminBytes, 0, $ProtectedAdminBytes.Length)
    }
    if ($null -ne $PlainAdminBytes) {
        [Array]::Clear($PlainAdminBytes, 0, $PlainAdminBytes.Length)
    }
}
