$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

function Read-EnvValue($key) {
    if (-not (Test-Path ".env")) {
        return $null
    }
    foreach ($line in Get-Content ".env") {
        if ($line.StartsWith("$key=")) {
            return $line.Substring($key.Length + 1)
        }
    }
    return $null
}

$databaseUrl = Read-EnvValue "DATABASE_URL"
if ([string]::IsNullOrWhiteSpace($databaseUrl)) {
    Write-Host "DATABASE_URL was not found in backend\.env" -ForegroundColor Red
    exit 1
}

$uri = [Uri]($databaseUrl -replace "^postgresql\+psycopg://", "postgresql://")
$userInfo = $uri.UserInfo.Split(":", 2)
$dbUser = [Uri]::UnescapeDataString($userInfo[0])
$dbPassword = if ($userInfo.Count -gt 1) { [Uri]::UnescapeDataString($userInfo[1]) } else { "" }
$dbHost = $uri.Host
$dbPort = if ($uri.Port -gt 0) { $uri.Port } else { 5432 }
$dbName = $uri.AbsolutePath.TrimStart("/")

$pgDump = Get-Command pg_dump -ErrorAction SilentlyContinue
if (-not $pgDump) {
    $candidates = Get-ChildItem "C:\Program Files\PostgreSQL" -Recurse -Filter pg_dump.exe -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending
    if ($candidates) {
        $pgDump = $candidates[0]
    }
}
if (-not $pgDump) {
    Write-Host "pg_dump.exe was not found. Install PostgreSQL client tools or add PostgreSQL bin to PATH." -ForegroundColor Red
    exit 1
}
$pgDumpPath = if ($pgDump.Source) { $pgDump.Source } else { $pgDump.FullName }

$backupDir = Join-Path (Split-Path $PSScriptRoot -Parent) "backups"
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$outFile = Join-Path $backupDir "$dbName-$stamp.dump"

$env:PGPASSWORD = $dbPassword
& $pgDumpPath -h $dbHost -p $dbPort -U $dbUser -d $dbName -F c -f $outFile
if ($LASTEXITCODE -ne 0) {
    Write-Host "Backup failed." -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host "Backup created: $outFile" -ForegroundColor Green
