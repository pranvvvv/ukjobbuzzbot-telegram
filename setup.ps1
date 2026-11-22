# JobPilot one-command setup for Windows (PowerShell 5.1+).
# Usage (from the project folder):  powershell -ExecutionPolicy Bypass -File .\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

# Warn early: OneDrive syncing .venv / .git / SQLite causes locks and conflict copies.
if ($PSScriptRoot -match "OneDrive|Dropbox|Google Drive|iCloud") {
    Write-Warning "This folder is inside a cloud-synced folder:`n  $PSScriptRoot"
    Write-Warning "Recommended: move the project to e.g. C:\dev\jobpilot and run setup again."
    $answer = Read-Host "Continue here anyway? (y/N)"
    if ($answer -notin @("y", "Y")) { exit 1 }
}

Step "Checking for uv (Python package manager)"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv from astral.sh ..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
uv --version

Step "Installing Python 3.13 (if needed) and dependencies"
uv sync

Step "Preparing .env"
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example. Fill in TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS and LLM_API_KEY."
    Start-Process notepad.exe ".env" -Wait
} else {
    Write-Host ".env already exists; leaving it untouched."
}

Step "Finding your Telegram user ID"
if (Select-String -Path ".env" -Pattern "^ALLOWED_USER_IDS=\s*$" -Quiet) {
    # whoami prints your bot's name and waits up to 2 minutes for your /start.
    uv run python -m jobpilot whoami --save
} else {
    Write-Host "ALLOWED_USER_IDS is already set."
}

Step "Running doctor"
uv run python -m jobpilot doctor
if ($LASTEXITCODE -ne 0) {
    Write-Host "`nFix the FAIL rows (see docs\SETUP.md), then run:  uv run python -m jobpilot doctor" -ForegroundColor Yellow
    exit $LASTEXITCODE
}

Write-Host "`nAll set. Start the bot with:  uv run python -m jobpilot run" -ForegroundColor Green
