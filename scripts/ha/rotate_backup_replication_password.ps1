[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$ProjectPath = '',

    [Parameter(Mandatory = $false)]
    [switch]$Execute,

    [Parameter(Mandatory = $false)]
    [switch]$AtomicProbeOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ExpectedComputerName = 'DESKTOP-NTRMANG'
$ExpectedUser = 'takealot_backup'
$ExpectedHost = '127.0.0.1'
$ExpectedPort = 3306
$ExpectedDatabase = 'takealot_ops'
$PasswordLength = 32
$EnvironmentKey = 'TAKEALOT_BACKUP_DATABASE_URL'
$MySqlPath = 'C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe'
$QuarantineRoot = 'D:\TakealotHA\quarantine'

function Get-EnvironmentAssignment {
    param(
        [Parameter(Mandatory = $true)][string]$Text,
        [Parameter(Mandatory = $true)][string]$Key
    )

    $Pattern = "(?m)^(?<prefix>\s*$([Regex]::Escape($Key))\s*=\s*)(?<value>[^\r\n]*)\r?$"
    $Matches = [Regex]::Matches($Text, $Pattern)
    if ($Matches.Count -ne 1) {
        throw "Expected exactly one $Key assignment, found $($Matches.Count)."
    }
    $Match = $Matches[0]
    $RawValue = $Match.Groups['value'].Value
    $LeadingLength = $RawValue.Length - $RawValue.TrimStart().Length
    $TrailingLength = $RawValue.Length - $RawValue.TrimEnd().Length
    $Leading = $RawValue.Substring(0, $LeadingLength)
    $Trailing = if ($TrailingLength -gt 0) {
        $RawValue.Substring($RawValue.Length - $TrailingLength)
    }
    else {
        ''
    }
    $Value = $RawValue.Trim()
    $Quote = ''
    if (
        $Value.Length -ge 2 -and
        (($Value[0] -eq [char]34 -and $Value[$Value.Length - 1] -eq [char]34) -or
        ($Value[0] -eq [char]39 -and $Value[$Value.Length - 1] -eq [char]39))
    ) {
        $Quote = [string]$Value[0]
        $Value = $Value.Substring(1, $Value.Length - 2)
    }
    return [pscustomobject]@{
        Match = $Match
        Url = $Value
        Quote = $Quote
        Leading = $Leading
        Trailing = $Trailing
    }
}

function Get-FileEncoding {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    if ($Bytes.Length -ge 3 -and $Bytes[0] -eq 0xEF -and $Bytes[1] -eq 0xBB -and $Bytes[2] -eq 0xBF) {
        return New-Object Text.UTF8Encoding($true)
    }
    if ($Bytes.Length -ge 2 -and $Bytes[0] -eq 0xFF -and $Bytes[1] -eq 0xFE) {
        return New-Object Text.UnicodeEncoding($false, $true)
    }
    if ($Bytes.Length -ge 2 -and $Bytes[0] -eq 0xFE -and $Bytes[1] -eq 0xFF) {
        return New-Object Text.UnicodeEncoding($true, $true)
    }
    return New-Object Text.UTF8Encoding($false)
}

function Get-CryptoRandomInt {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateRange(1, 2147483647)]
        [int]$MaximumExclusive
    )

    $Generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    $Buffer = New-Object byte[] 4
    try {
        $Limit = [uint64]::MaxValue
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

function New-StrongPassword {
    param([Parameter(Mandatory = $true)][int]$Length)

    if ($Length -lt 16) {
        throw 'Password length must be at least 16.'
    }
    $Upper = 'ABCDEFGHJKLMNPQRSTUVWXYZ'
    $Lower = 'abcdefghijkmnopqrstuvwxyz'
    $Digits = '23456789'
    $Special = '!#$%&()*+,-./:;<=>?@[]^_{|}~'
    $All = $Upper + $Lower + $Digits + $Special
    $Characters = New-Object Collections.Generic.List[char]
    foreach ($Group in @($Upper, $Lower, $Digits, $Special)) {
        $Characters.Add($Group[(Get-CryptoRandomInt -MaximumExclusive $Group.Length)])
    }
    while ($Characters.Count -lt $Length) {
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
        [Parameter(Mandatory = $true)][string]$Password,
        [Parameter(Mandatory = $true)][string]$Sql
    )

    $PreviousPassword = $env:MYSQL_PWD
    $env:MYSQL_PWD = $Password
    $SavedErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $Output = @(
            & $MySqlPath `
                --protocol=TCP `
                "--host=$ExpectedHost" `
                "--port=$ExpectedPort" `
                "--user=$ExpectedUser" `
                --batch `
                --raw `
                --skip-column-names `
                "--execute=$Sql" 2>&1
        )
        $ExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $SavedErrorActionPreference
        if ($null -eq $PreviousPassword) {
            Remove-Item Env:\MYSQL_PWD -ErrorAction SilentlyContinue
        }
        else {
            $env:MYSQL_PWD = $PreviousPassword
        }
    }
    if ($ExitCode -ne 0) {
        $Message = @($Output | ForEach-Object { [string]$_ }) -join [Environment]::NewLine
        throw "mysql.exe failed with exit code $ExitCode. $Message"
    }
    return @($Output | ForEach-Object { [string]$_ })
}

function Invoke-PasswordChange {
    param(
        [Parameter(Mandatory = $true)][string]$LoginPassword,
        [Parameter(Mandatory = $true)][string]$NewPassword
    )

    if ($NewPassword -match "['\\\r\n]") {
        throw 'Password contains characters unsupported by the guarded rotation path.'
    }
    $StartInfo = New-Object Diagnostics.ProcessStartInfo
    $StartInfo.FileName = $MySqlPath
    $StartInfo.Arguments = (
        "--protocol=TCP --host=$ExpectedHost --port=$ExpectedPort " +
        "--user=$ExpectedUser --batch --raw"
    )
    $StartInfo.UseShellExecute = $false
    $StartInfo.CreateNoWindow = $true
    $StartInfo.RedirectStandardInput = $true
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true
    $StartInfo.EnvironmentVariables['MYSQL_PWD'] = $LoginPassword

    $Process = New-Object Diagnostics.Process
    $Process.StartInfo = $StartInfo
    if (-not $Process.Start()) {
        throw 'Failed to start mysql.exe for password rotation.'
    }
    try {
        $Process.StandardInput.WriteLine(
            "ALTER USER USER() IDENTIFIED BY '$NewPassword';"
        )
        $Process.StandardInput.Close()
        $StandardOutput = $Process.StandardOutput.ReadToEnd()
        $StandardError = $Process.StandardError.ReadToEnd()
        $Process.WaitForExit()
        if ($Process.ExitCode -ne 0) {
            throw "mysql.exe failed with exit code $($Process.ExitCode). $StandardError"
        }
        return $StandardOutput
    }
    finally {
        $Process.Dispose()
    }
}

function Write-AtomicText {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Text,
        [Parameter(Mandatory = $true)][Text.Encoding]$Encoding
    )

    $TemporaryPath = Join-Path (
        Split-Path -Parent $Path
    ) ('.' + (Split-Path -Leaf $Path) + ".rotation-$PID.tmp")
    $BackupPath = Join-Path (
        Split-Path -Parent $Path
    ) ('.' + (Split-Path -Leaf $Path) + ".rotation-$PID.bak")
    try {
        $TargetAcl = Get-Acl -LiteralPath $Path
        [IO.File]::WriteAllText($TemporaryPath, $Text, $Encoding)
        [IO.File]::Replace(
            $TemporaryPath,
            $Path,
            $BackupPath
        )
        $UpdatedAcl = Get-Acl -LiteralPath $Path
        if ($UpdatedAcl.Sddl -cne $TargetAcl.Sddl) {
            throw 'Atomic replacement changed the protected file ACL.'
        }
    }
    finally {
        Remove-Item -LiteralPath $TemporaryPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $BackupPath -Force -ErrorAction SilentlyContinue
    }
}

function Write-JsonFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value
    )

    [IO.File]::WriteAllText(
        $Path,
        (($Value | ConvertTo-Json -Depth 8) + [Environment]::NewLine),
        (New-Object Text.UTF8Encoding($false))
    )
}

if ($env:COMPUTERNAME -cne $ExpectedComputerName) {
    throw "Safety stop: expected $ExpectedComputerName, got $env:COMPUTERNAME."
}
if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
    $ProjectPath = Join-Path $PSScriptRoot '..\..'
}
$ResolvedProjectPath = (Resolve-Path -LiteralPath $ProjectPath).Path
$EnvironmentPath = Join-Path $ResolvedProjectPath '.env'
foreach ($Path in @($EnvironmentPath, $MySqlPath)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required file not found: $Path"
    }
}

$EnvironmentBytes = [IO.File]::ReadAllBytes($EnvironmentPath)
$EnvironmentEncoding = Get-FileEncoding -Bytes $EnvironmentBytes
$EnvironmentText = $EnvironmentEncoding.GetString($EnvironmentBytes)
if (
    $EnvironmentBytes.Length -ge 3 -and
    $EnvironmentBytes[0] -eq 0xEF -and
    $EnvironmentBytes[1] -eq 0xBB -and
    $EnvironmentBytes[2] -eq 0xBF
) {
    $EnvironmentText = $EnvironmentText.TrimStart([char]0xFEFF)
}
$Assignment = Get-EnvironmentAssignment -Text $EnvironmentText -Key $EnvironmentKey
$DatabaseUrl = [Uri]$Assignment.Url
$UserInfoParts = $DatabaseUrl.UserInfo -split ':', 2
if ($UserInfoParts.Count -ne 2) {
    throw 'Backup database URL does not contain a username and password.'
}
$CurrentUser = [Uri]::UnescapeDataString($UserInfoParts[0])
$CurrentPassword = [Uri]::UnescapeDataString($UserInfoParts[1])
if (
    $CurrentUser -cne $ExpectedUser -or
    $DatabaseUrl.Host -cne $ExpectedHost -or
    $DatabaseUrl.Port -ne $ExpectedPort -or
    $DatabaseUrl.AbsolutePath.Trim('/') -cne $ExpectedDatabase
) {
    throw 'Safety stop: backup database URL identity is not the expected local account.'
}
if ($CurrentPassword -match "['\\\r\n]") {
    throw 'Current password contains characters unsupported by the guarded rollback path.'
}

$Probe = @(
    Invoke-MySql -Password $CurrentPassword -Sql (
        "SELECT CONCAT(CURRENT_USER(),'|',@@hostname,'|',VERSION(),'|'," +
        "@@server_id,'|',@@read_only,'|',@@global.general_log);"
    )
)
$ProbeParts = $Probe[0].Split('|')
if (
    $ProbeParts.Count -ne 6 -or
    $ProbeParts[0] -cne 'takealot_backup@localhost' -or
    $ProbeParts[1] -cne $ExpectedComputerName -or
    $ProbeParts[3] -cne '1' -or
    $ProbeParts[4] -cne '0' -or
    $ProbeParts[5] -cne '0'
) {
    throw "Safety stop: unexpected MySQL source state: $($Probe[0])"
}
$Grants = @(Invoke-MySql -Password $CurrentPassword -Sql 'SHOW GRANTS FOR CURRENT_USER();')
$GrantText = $Grants -join [Environment]::NewLine
foreach ($RequiredGrant in @('REPLICATION SLAVE', 'REPLICATION CLIENT')) {
    if ($GrantText -notmatch [Regex]::Escape($RequiredGrant)) {
        throw "Safety stop: backup account lacks $RequiredGrant."
    }
}

$Preflight = [ordered]@{
    computer = $env:COMPUTERNAME
    status = 'preflight_ok'
    mysql_identity = $Probe[0]
    account = 'takealot_backup@localhost'
    current_password_length = $CurrentPassword.Length
    target_password_length = $PasswordLength
    environment_path = $EnvironmentPath
    mysql_restart_required = $false
    erp_restart_required = $false
    password_recorded = $false
}
if (-not $Execute -and -not $AtomicProbeOnly) {
    $Preflight | ConvertTo-Json -Depth 8
    exit 0
}

New-Item -ItemType Directory -Path $QuarantineRoot -Force | Out-Null
$AtomicProbePath = Join-Path $QuarantineRoot "atomic-replace-probe-$PID.txt"
try {
    $ProbeEncoding = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($AtomicProbePath, 'before', $ProbeEncoding)
    Write-AtomicText -Path $AtomicProbePath -Text 'after' -Encoding $ProbeEncoding
    if ([IO.File]::ReadAllText($AtomicProbePath, $ProbeEncoding) -cne 'after') {
        throw 'Atomic replacement probe returned unexpected content.'
    }
}
finally {
    Remove-Item -LiteralPath $AtomicProbePath -Force -ErrorAction SilentlyContinue
}
if ($AtomicProbeOnly) {
    [pscustomobject]@{
        computer = $env:COMPUTERNAME
        status = 'atomic_probe_ok'
        account_changed = $false
        environment_changed = $false
        mysql_restarted = $false
        erp_restarted = $false
    } | ConvertTo-Json -Depth 8
    exit 0
}

$NewPassword = New-StrongPassword -Length $PasswordLength
$AuthorityStart = $Assignment.Url.IndexOf('://') + 3
$AtIndex = $Assignment.Url.IndexOf('@', $AuthorityStart)
if ($AuthorityStart -lt 3 -or $AtIndex -le $AuthorityStart) {
    throw 'Backup database URL authority could not be parsed safely.'
}
$RawUserInfo = $Assignment.Url.Substring($AuthorityStart, $AtIndex - $AuthorityStart)
$ColonIndex = $RawUserInfo.IndexOf(':')
if ($ColonIndex -lt 1) {
    throw 'Backup database URL user information could not be parsed safely.'
}
$NewUrl = (
    $Assignment.Url.Substring(0, $AuthorityStart) +
    $RawUserInfo.Substring(0, $ColonIndex + 1) +
    [Uri]::EscapeDataString($NewPassword) +
    $Assignment.Url.Substring($AtIndex)
)
$NewRawValue = (
    $Assignment.Leading + $Assignment.Quote + $NewUrl +
    $Assignment.Quote + $Assignment.Trailing
)
$NewEnvironmentText = (
    $EnvironmentText.Substring(0, $Assignment.Match.Groups['value'].Index) +
    $NewRawValue +
    $EnvironmentText.Substring(
        $Assignment.Match.Groups['value'].Index +
        $Assignment.Match.Groups['value'].Length
    )
)

$PasswordChanged = $false
$EnvironmentChanged = $false
$RollbackSucceeded = $false
$Timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$ManifestPath = Join-Path $QuarantineRoot "backup-replication-password-$Timestamp.json"
$Manifest = [ordered]@{
    computer = $env:COMPUTERNAME
    started_at = (Get-Date).ToString('o')
    status = 'starting'
    account = 'takealot_backup@localhost'
    old_password_length = $CurrentPassword.Length
    new_password_length = $NewPassword.Length
    password_recorded = $false
    mysql_restart_performed = $false
    erp_restart_performed = $false
}

try {
    Write-JsonFile -Path $ManifestPath -Value $Manifest

    Invoke-PasswordChange `
        -LoginPassword $CurrentPassword `
        -NewPassword $NewPassword | Out-Null
    $PasswordChanged = $true

    Write-AtomicText `
        -Path $EnvironmentPath `
        -Text $NewEnvironmentText `
        -Encoding $EnvironmentEncoding
    $EnvironmentChanged = $true

    $Verification = @(
        Invoke-MySql -Password $NewPassword -Sql (
            "SELECT CONCAT(CURRENT_USER(),'|',@@hostname,'|',@@server_id);"
        )
    )
    if ($Verification.Count -ne 1 -or $Verification[0] -cne 'takealot_backup@localhost|DESKTOP-NTRMANG|1') {
        throw 'New backup account credential did not pass the independent login check.'
    }

    $SavedText = [IO.File]::ReadAllText($EnvironmentPath, $EnvironmentEncoding)
    $SavedAssignment = Get-EnvironmentAssignment -Text $SavedText -Key $EnvironmentKey
    $SavedUri = [Uri]$SavedAssignment.Url
    $SavedParts = $SavedUri.UserInfo -split ':', 2
    if (
        $SavedParts.Count -ne 2 -or
        [Uri]::UnescapeDataString($SavedParts[0]) -cne $ExpectedUser -or
        [Uri]::UnescapeDataString($SavedParts[1]) -cne $NewPassword
    ) {
        throw 'The updated environment file did not pass the independent credential check.'
    }

    $Manifest.status = 'success'
    $Manifest.completed_at = (Get-Date).ToString('o')
    $Manifest.rollback_required = $false
    Write-JsonFile -Path $ManifestPath -Value $Manifest

    [pscustomobject]@{
        computer = $env:COMPUTERNAME
        status = 'success'
        account = 'takealot_backup@localhost'
        password_length = $NewPassword.Length
        environment_updated = $true
        mysql_restarted = $false
        erp_restarted = $false
        manifest = $ManifestPath
    } | ConvertTo-Json -Depth 8
}
catch {
    $Failure = $_
    if ($PasswordChanged) {
        try {
            Invoke-PasswordChange `
                -LoginPassword $NewPassword `
                -NewPassword $CurrentPassword | Out-Null
            $RollbackSucceeded = $true
            if ($EnvironmentChanged) {
                Write-AtomicText `
                    -Path $EnvironmentPath `
                    -Text $EnvironmentText `
                    -Encoding $EnvironmentEncoding
            }
        }
        catch {
            $RollbackSucceeded = $false
        }
    }
    try {
        $Manifest.status = 'failed'
        $Manifest.failed_at = (Get-Date).ToString('o')
        $Manifest.failure = $Failure.Exception.Message
        $Manifest.rollback_succeeded = $RollbackSucceeded
        Write-JsonFile -Path $ManifestPath -Value $Manifest
    }
    catch {
    }
    throw $Failure
}
finally {
    $CurrentPassword = $null
    $NewPassword = $null
    $DatabaseUrl = $null
    $NewUrl = $null
    $EnvironmentText = $null
    $NewEnvironmentText = $null
    if ($null -ne $EnvironmentBytes) {
        [Array]::Clear($EnvironmentBytes, 0, $EnvironmentBytes.Length)
    }
}
