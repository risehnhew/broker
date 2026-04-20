$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$installer = Join-Path $root "downloads\ibgateway-stable-standalone-windows-x64.exe"

if (-not (Test-Path $installer)) {
    throw "Installer not found: $installer. Run scripts/download_ibgateway.ps1 first."
}

Write-Host "Launching IB Gateway installer..." -ForegroundColor Cyan
Start-Process -FilePath $installer
