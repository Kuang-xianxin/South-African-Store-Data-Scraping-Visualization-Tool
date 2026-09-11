[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [switch]$ReadCredentialFromStdin,

    [Parameter(Mandatory = $false)]
    [switch]$Rotate
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ExpectedComputerName = 'LAPTOP-2T5MN8EU'
$ExpectedUser = 'takealot_crawler'
$SecretRoot = 'D:\TakealotHA\secrets'
$CredentialPath = Join-Path $SecretRoot 'distributed-crawler-primary.dpapi'

if ($env:COMPUTERNAME -cne $ExpectedComputerName) {
    throw "Safety stop: expected $ExpectedComputerName, got $env:COMPUTERNAME."
}
$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$Principal = [Security.Principal.WindowsPrincipal]::new($Identity)
if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator rights are required to protect the crawler credential.'
}

if (-not $ReadCredentialFromStdin) {
    [pscustomobject]@{
        computer = $env:COMPUTERNAME
        status = 'preflight_ok'
        credential_path = $CredentialPath
        credential_exists = Test-Path -LiteralPath $CredentialPath -PathType Leaf
        overwrite_requires_rotate = $true
        password_recorded = $false
    } | ConvertTo-Json -Depth 4
    exit 0
}
if ((Test-Path -LiteralPath $CredentialPath -PathType Leaf) -and -not $Rotate) {
    throw 'The crawler credential already exists. Use -Rotate only for an intentional rotation.'
}

$RawPayload = [Console]::In.ReadToEnd()
if ([string]::IsNullOrWhiteSpace($RawPayload)) {
    throw 'Expected one credential JSON object on standard input.'
}
try {
    $Payload = $RawPayload | ConvertFrom-Json -ErrorAction Stop
}
catch {
    throw 'Credential input is not valid JSON.'
}
$User = [string]$Payload.user
$Password = [string]$Payload.password
if ($User -cne $ExpectedUser) {
    throw "Credential user must be exactly $ExpectedUser."
}
if (
    $Password.Length -lt 32 -or
    $Password.Length -gt 128 -or
    $Password -notmatch '[A-Z]' -or
    $Password -notmatch '[a-z]' -or
    $Password -notmatch '[0-9]' -or
    $Password -match '[\x00-\x20\x7f]'
) {
    throw 'Credential password does not meet the guarded length and character policy.'
}

Add-Type -AssemblyName System.Security
$PlainBytes = [Text.Encoding]::UTF8.GetBytes("$User`n$Password")
$ProtectedBytes = $null
$RoundTripProtected = $null
$RoundTripPlain = $null
$RoundTripText = $null
$TemporaryPath = Join-Path $SecretRoot ".distributed-crawler-primary.$PID.tmp"
try {
    New-Item -ItemType Directory -Path $SecretRoot -Force | Out-Null
    $ProtectedBytes = [Security.Cryptography.ProtectedData]::Protect(
        $PlainBytes,
        $null,
        [Security.Cryptography.DataProtectionScope]::LocalMachine
    )
    [IO.File]::WriteAllBytes($TemporaryPath, $ProtectedBytes)

    $Acl = New-Object Security.AccessControl.FileSecurity
    $Acl.SetAccessRuleProtection($true, $false)
    foreach ($Account in @(
        $Identity.Name,
        'NT AUTHORITY\SYSTEM',
        'BUILTIN\Administrators'
    )) {
        $Rule = New-Object Security.AccessControl.FileSystemAccessRule(
            $Account,
            [Security.AccessControl.FileSystemRights]::FullControl,
            [Security.AccessControl.AccessControlType]::Allow
        )
        $Acl.AddAccessRule($Rule) | Out-Null
    }
    Set-Acl -LiteralPath $TemporaryPath -AclObject $Acl
    Move-Item -LiteralPath $TemporaryPath -Destination $CredentialPath -Force

    $RoundTripProtected = [IO.File]::ReadAllBytes($CredentialPath)
    $RoundTripPlain = [Security.Cryptography.ProtectedData]::Unprotect(
        $RoundTripProtected,
        $null,
        [Security.Cryptography.DataProtectionScope]::LocalMachine
    )
    $RoundTripText = [Text.Encoding]::UTF8.GetString($RoundTripPlain)
    if ($RoundTripText -cne "$User`n$Password") {
        throw 'Protected crawler credential failed its local round-trip check.'
    }

    [pscustomobject]@{
        computer = $env:COMPUTERNAME
        status = 'success'
        credential_user = $ExpectedUser
        credential_path = $CredentialPath
        scope = 'LocalMachine'
        password_recorded = $false
    } | ConvertTo-Json -Depth 4
}
finally {
    Remove-Item -LiteralPath $TemporaryPath -Force -ErrorAction SilentlyContinue
    $RawPayload = $null
    $Payload = $null
    $Password = $null
    $RoundTripText = $null
    if ($null -ne $PlainBytes) {
        [Array]::Clear($PlainBytes, 0, $PlainBytes.Length)
    }
    if ($null -ne $ProtectedBytes) {
        [Array]::Clear($ProtectedBytes, 0, $ProtectedBytes.Length)
    }
    if ($null -ne $RoundTripProtected) {
        [Array]::Clear($RoundTripProtected, 0, $RoundTripProtected.Length)
    }
    if ($null -ne $RoundTripPlain) {
        [Array]::Clear($RoundTripPlain, 0, $RoundTripPlain.Length)
    }
}
