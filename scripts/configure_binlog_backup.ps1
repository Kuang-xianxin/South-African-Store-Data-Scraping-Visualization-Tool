param(
    [Parameter(Mandatory = $false)]
    [string]$ProjectPath = (Get-Location).Path,

    [Parameter(Mandatory = $false)]
    [string]$MySqlAdministrator = 'root'
)

$ErrorActionPreference = 'Stop'
$ResolvedProjectPath = (Resolve-Path -LiteralPath $ProjectPath).Path
$ProjectDrive = (Get-Item -LiteralPath $ResolvedProjectPath).PSDrive.Name
if ($ProjectDrive -ne 'D') {
    throw 'The formal project and all backup artifacts must be on drive D:.'
}

$MySqlPath = 'C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe'
if (-not (Test-Path -LiteralPath $MySqlPath -PathType Leaf)) {
    throw "MySQL client not found: $MySqlPath"
}

$EnvPath = Join-Path $ResolvedProjectPath '.env'
if (-not (Test-Path -LiteralPath $EnvPath -PathType Leaf)) {
    throw "Project .env not found: $EnvPath"
}

$SecureAdminPassword = Read-Host `
    "Enter the MySQL password for $MySqlAdministrator" `
    -AsSecureString
$AdminPasswordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR(
    $SecureAdminPassword
)
try {
    $AdminPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
        $AdminPasswordPointer
    )
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($AdminPasswordPointer)
}

$RandomBytes = New-Object byte[] 32
$RandomGenerator = [Security.Cryptography.RandomNumberGenerator]::Create()
try {
    $RandomGenerator.GetBytes($RandomBytes)
}
finally {
    $RandomGenerator.Dispose()
}
$BackupPassword = [Convert]::ToBase64String($RandomBytes).TrimEnd('=')
$Sql = @"
CREATE USER 'takealot_backup'@'localhost'
IDENTIFIED BY '$BackupPassword';
GRANT SELECT, SHOW VIEW, TRIGGER
ON ``takealot_ops``.*
TO 'takealot_backup'@'localhost';
GRANT RELOAD, REPLICATION CLIENT, REPLICATION SLAVE
ON *.*
TO 'takealot_backup'@'localhost';
"@

$PreviousMySqlPassword = $env:MYSQL_PWD
$env:MYSQL_PWD = $AdminPassword
try {
    $Sql | & $MySqlPath `
        --protocol=TCP `
        --host=127.0.0.1 `
        --port=3306 `
        --user=$MySqlAdministrator
    if ($LASTEXITCODE -ne 0) {
        throw 'MySQL rejected the backup-account provisioning request.'
    }
}
finally {
    if ($null -eq $PreviousMySqlPassword) {
        Remove-Item Env:\MYSQL_PWD -ErrorAction SilentlyContinue
    }
    else {
        $env:MYSQL_PWD = $PreviousMySqlPassword
    }
    $AdminPassword = $null
}

$EncodedPassword = [Uri]::EscapeDataString($BackupPassword)
$RequiredValues = [ordered]@{
    TAKEALOT_BACKUP_DATABASE_URL = (
        "mysql+pymysql://takealot_backup:$EncodedPassword" +
        "@127.0.0.1:3306/takealot_ops?charset=utf8mb4"
    )
    TAKEALOT_BACKUP_ROOT = "$ResolvedProjectPath\backups"
    TAKEALOT_BINLOG_RETENTION_DAYS = '35'
}
$Lines = [Collections.Generic.List[string]]::new()
foreach ($Line in [IO.File]::ReadAllLines($EnvPath)) {
    $Key = ($Line -split '=', 2)[0]
    if (-not $RequiredValues.Contains($Key)) {
        $Lines.Add($Line)
    }
}
foreach ($Entry in $RequiredValues.GetEnumerator()) {
    $Lines.Add("$($Entry.Key)=$($Entry.Value)")
}
[IO.File]::WriteAllLines(
    $EnvPath,
    $Lines,
    [Text.UTF8Encoding]::new($false)
)
$BackupPassword = $null

& (Join-Path $ResolvedProjectPath 'scripts\install_binlog_archive_task.ps1') `
    -ProjectPath $ResolvedProjectPath
if ($LASTEXITCODE -ne 0) {
    throw 'The backup account was created, but task installation failed.'
}

Write-Host 'Binlog backup account configured without exposing its password.'
Write-Host "All backup artifacts are under $ResolvedProjectPath\backups"
