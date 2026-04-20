$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$downloadDir = Join-Path $root "downloads"
$installer = Join-Path $downloadDir "ibgateway-stable-standalone-windows-x64.exe"
$url = "https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-windows-x64.exe"

New-Item -ItemType Directory -Force -Path $downloadDir | Out-Null

Write-Host "Downloading IB Gateway installer..." -ForegroundColor Cyan
Write-Host "Source: $url"
Write-Host "Target: $installer"

Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $installer

Write-Host "Download complete." -ForegroundColor Green
Get-Item $installer | Select-Object FullName, Length, LastWriteTime | Format-List
