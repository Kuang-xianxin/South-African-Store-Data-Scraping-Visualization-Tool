param(
    [Parameter(Mandatory = $false)]
    [string]$ProjectPath = (Get-Location).Path,

    [Parameter(Mandatory = $false)]
    [string]$Repository = 'sftp:takealot-backup-laptop:/D:/TakealotOffsiteBackup/repository',

    [Parameter(Mandatory = $false)]
    [string]$RecoveryKeyPath = ''
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

function Protect-PathForCurrentUser {
    param([Parameter(Mandatory = $true)][string]$Path)

    $CurrentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $Item = Get-Item -LiteralPath $Path
    $CurrentUserGrant = if ($Item.PSIsContainer) {
        '*' + $CurrentSid + ':(OI)(CI)F'
    } else {
        '*' + $CurrentSid + ':F'
    }
    $SystemGrant = if ($Item.PSIsContainer) {
        '*S-1-5-18:(OI)(CI)F'
    } else {
        '*S-1-5-18:F'
    }
    $AdministratorsGrant = if ($Item.PSIsContainer) {
        '*S-1-5-32-544:(OI)(CI)F'
    } else {
        '*S-1-5-32-544:F'
    }
    & icacls.exe $Path /inheritance:r | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to disable inherited permissions: $Path"
    }
    & icacls.exe $Path /grant:r `
        $CurrentUserGrant `
        $SystemGrant `
        $AdministratorsGrant | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to restrict permissions: $Path"
    }
}

$ResolvedProjectPath = (Resolve-Path -LiteralPath $ProjectPath).Path
if ((Get-Item -LiteralPath $ResolvedProjectPath).PSDrive.Name -ne 'D') {
    throw 'The formal project must remain on drive D:.'
}

$ResticPath = Resolve-ResticExecutable
$ConfigRoot = Join-Path $env:LOCALAPPDATA 'TakealotOffsiteBackup'
$PasswordPath = Join-Path $ConfigRoot 'restic-password.clixml'
$RepositoryPath = Join-Path $ConfigRoot 'repository.txt'
if ([string]::IsNullOrWhiteSpace($RecoveryKeyPath)) {
    $RecoveryKeyPath = Join-Path $env:USERPROFILE `
        'Documents\Takealot-Offsite-Backup-Recovery-Key.txt'
}

New-Item -ItemType Directory -Path $ConfigRoot -Force | Out-Null
Protect-PathForCurrentUser -Path $ConfigRoot

$CreatedPassword = $false
if (-not (Test-Path -LiteralPath $PasswordPath -PathType Leaf)) {
    $RandomBytes = New-Object byte[] 48
    $RandomGenerator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $RandomGenerator.GetBytes($RandomBytes)
    }
    finally {
        $RandomGenerator.Dispose()
    }
    $PlainPassword = [Convert]::ToBase64String($RandomBytes).
        TrimEnd('=').
        Replace('+', '-').
        Replace('/', '_')
    ConvertTo-SecureString -String $PlainPassword -AsPlainText -Force |
        Export-Clixml -LiteralPath $PasswordPath

    $RecoveryDirectory = Split-Path -Parent $RecoveryKeyPath
    New-Item -ItemType Directory -Path $RecoveryDirectory -Force | Out-Null
    $RecoveryText = @(
        'Takealot encrypted offsite backup recovery key'
        "Created: $((Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz'))"
        "Repository: $Repository"
        ''
        $PlainPassword
        ''
        'Store this key in a password manager or offline USB. Do not send it in chat.'
        'The automated DPAPI copy works only for this Windows user on this computer.'
    ) -join [Environment]::NewLine
    [System.IO.File]::WriteAllText(
        $RecoveryKeyPath,
        $RecoveryText,
        (New-Object System.Text.UTF8Encoding($true))
    )
    Protect-PathForCurrentUser -Path $RecoveryKeyPath
    $CreatedPassword = $true
}

[System.IO.File]::WriteAllText(
    $RepositoryPath,
    $Repository + [Environment]::NewLine,
    (New-Object System.Text.UTF8Encoding($false))
)

$SecurePassword = Import-Clixml -LiteralPath $PasswordPath
$PasswordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecurePassword)
try {
    $env:RESTIC_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
        $PasswordPointer
    )
    $PreviousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $ResticPath -r $Repository cat config 2>$null | Out-Null
        $RepositoryProbeExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }
    if ($RepositoryProbeExitCode -ne 0) {
        Write-Host 'Initializing the encrypted Restic repository on the laptop...'
        & $ResticPath -r $Repository init --repository-version 2
        if ($LASTEXITCODE -ne 0) {
            throw 'Restic repository initialization failed.'
        }
    }

    & $ResticPath -r $Repository snapshots
    if ($LASTEXITCODE -ne 0) {
        throw 'The initialized Restic repository is not readable.'
    }
}
finally {
    Remove-Item Env:RESTIC_PASSWORD -ErrorAction SilentlyContinue
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($PasswordPointer)
}

Write-Host "Repository ready: $Repository"
Write-Host "Protected automatic key: $PasswordPath"
if ($CreatedPassword) {
    Write-Host "Recovery key copy: $RecoveryKeyPath"
    Write-Warning 'Copy the recovery key to a password manager or offline USB.'
}
