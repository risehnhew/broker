$ErrorActionPreference = "Stop"

$candidates = @(
    "C:\Jts",
    "C:\Program Files\IB Gateway",
    "C:\Program Files\Trader Workstation",
    "C:\Program Files (x86)\IB Gateway",
    "C:\Program Files (x86)\Trader Workstation"
)

Write-Host "Searching common IBKR install paths..." -ForegroundColor Cyan

foreach ($path in $candidates) {
    if (Test-Path $path) {
        Write-Host ""
        Write-Host $path -ForegroundColor Green
        Get-ChildItem $path -Recurse -ErrorAction SilentlyContinue |
            Select-Object -First 40 FullName
    }
}
