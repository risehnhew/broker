$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root ".env"

Write-Host "== IBKR Preflight ==" -ForegroundColor Cyan
Write-Host "Project: $root"

if (Test-Path $envFile) {
    Write-Host ""
    Write-Host ".env found:" -ForegroundColor Green
    Get-Content $envFile | Where-Object { $_ -match '^(IB_HOST|IB_PORT|IB_CLIENT_ID)=' }
} else {
    Write-Host ""
    Write-Host ".env not found" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Processes:" -ForegroundColor Green
$procs = Get-Process | Where-Object { $_.ProcessName -match 'tws|ibgateway|gateway|javaw|java' } |
    Select-Object ProcessName, Id, MainWindowTitle
if ($procs) {
    $procs | Format-Table -AutoSize
} else {
    Write-Host "No obvious TWS/IB Gateway process found." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Ports:" -ForegroundColor Green
$ports = 7497, 4002, 7496, 4001
$results = foreach ($p in $ports) {
    $r = Test-NetConnection 127.0.0.1 -Port $p -WarningAction SilentlyContinue
    [pscustomobject]@{
        Port = $p
        Listening = $r.TcpTestSucceeded
    }
}
$results | Format-Table -AutoSize

Write-Host ""
Write-Host "Quick verdict:" -ForegroundColor Green
if ($results.Listening -contains $true) {
    Write-Host "At least one IBKR socket port is listening. The Python app should be able to connect." -ForegroundColor Green
} else {
    Write-Host "No IBKR socket port is listening. Start TWS or IB Gateway, log in, then enable API." -ForegroundColor Yellow
}
