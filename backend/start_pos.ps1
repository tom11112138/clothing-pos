$ErrorActionPreference = "Stop"

function Write-Step($message) {
    Write-Host ""
    Write-Host "==> $message" -ForegroundColor Cyan
}

function Read-WithDefault($prompt, $default) {
    $value = Read-Host "$prompt [$default]"
    if ([string]::IsNullOrWhiteSpace($value)) {
        return $default
    }
    return $value.Trim()
}

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

Set-Location $PSScriptRoot

Write-Step "Checking Python"
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$venvReady = $false
if (Test-Path ".venv\Scripts\python.exe") {
    & $venvPython --version *> $null
    if ($LASTEXITCODE -eq 0) {
        $venvReady = $true
    } else {
        Write-Host "Existing virtual environment is invalid. Rebuilding it..." -ForegroundColor Yellow
        Remove-Item -LiteralPath ".venv" -Recurse -Force
    }
}
if (-not $venvReady) {
    $pythonPaths = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\python.exe")
    )
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) { $pythonPaths += $pythonCommand.Source }
    $basePython = $pythonPaths | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
    if (-not $basePython) {
        Write-Host "Python was not found. Please install Python 3.10 or newer, then run start.bat again." -ForegroundColor Red
        exit 1
    }
    Write-Step "Preparing virtual environment"
    & $basePython -m venv .venv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $venvReady = $true
}

$requirementsHash = (Get-FileHash -LiteralPath "requirements.txt" -Algorithm SHA256).Hash
$requirementsStamp = Join-Path $PSScriptRoot ".venv\.requirements.sha256"
$installedHash = if (Test-Path $requirementsStamp) { (Get-Content $requirementsStamp -Raw).Trim() } else { "" }
if ($requirementsHash -ne $installedHash) {
    Write-Step "Installing dependencies"
    & $venvPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Dependency installation failed. Please check the error above, then run start.bat again." -ForegroundColor Red
        exit $LASTEXITCODE
    }
    Set-Content -LiteralPath $requirementsStamp -Value $requirementsHash -Encoding ASCII
}

Write-Step "Checking configuration"
if (-not (Test-Path ".env")) {
    Write-Host "First start: please enter database and admin settings. Press Enter to use the default value."
    $dbHost = Read-WithDefault "PostgreSQL host" "127.0.0.1"
    $dbPort = Read-WithDefault "PostgreSQL port" "5432"
    $dbName = Read-WithDefault "PostgreSQL database name" "clothing_pos"
    $dbUser = Read-WithDefault "PostgreSQL username" "postgres"
    $dbPassword = Read-WithDefault "PostgreSQL password" "admin"
    $adminUser = Read-WithDefault "Admin username" "admin"
    do {
        $adminPassword = Read-Host "App admin initial password (at least 10 characters)"
        if ($adminPassword.Length -lt 10) {
            Write-Host "Password must contain at least 10 characters." -ForegroundColor Yellow
        }
    } while ($adminPassword.Length -lt 10)
    $adminName = Read-WithDefault "Admin display name" "Super Admin"
    $encodedUser = [Uri]::EscapeDataString($dbUser)
    $encodedPassword = [Uri]::EscapeDataString($dbPassword)
    $encodedDbName = [Uri]::EscapeDataString($dbName)
    $databaseUrl = "postgresql+psycopg://${encodedUser}:${encodedPassword}@${dbHost}:${dbPort}/${encodedDbName}"

    $envLines = @(
        "DATABASE_URL=$databaseUrl",
        "ADMIN_USERNAME=$adminUser",
        "ADMIN_PASSWORD=$adminPassword",
        "ADMIN_NAME=$adminName",
        "SEED_ADMIN=true",
        "CORS_ORIGINS=http://127.0.0.1:8000,http://localhost:8000",
        "COOKIE_SECURE=false",
        "BUSINESS_TIMEZONE=Asia/Shanghai"
    )
    Set-Content -Path ".env" -Value $envLines -Encoding ASCII
} else {
    $databaseUrl = Read-EnvValue "DATABASE_URL"
    if ($databaseUrl -eq "admin") {
        Write-Host "Your .env has an invalid DATABASE_URL=admin. Please delete backend\.env and run start.bat again." -ForegroundColor Red
        exit 1
    }
}

Write-Step "Preparing PostgreSQL database"
& $venvPython ensure_postgres.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "PostgreSQL preparation failed. Please check .env and make sure PostgreSQL is running." -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Step "Testing database connection"
& $venvPython check_database.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "Database connection failed. Edit backend\.env or delete it and run start.bat again." -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Step "Starting POS system"
$url = "http://127.0.0.1:8000"
Write-Host "Opening browser: $url"
Write-Host "LAN access: http://YOUR-COMPUTER-IP:8000"
Write-Host "Use the administrator account configured during first start."
Start-Process $url
& $venvPython -m uvicorn main:app --host 0.0.0.0 --port 8000
