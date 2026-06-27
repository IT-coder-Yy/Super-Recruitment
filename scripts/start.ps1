$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
}

$healthUrl = "http://127.0.0.1:8000/health"
try {
    $healthResponse = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
    if ($healthResponse.status -eq "ok") {
        Write-Host "The application is already running at http://127.0.0.1:8000"
        exit 0
    }
}
catch {
    # No healthy application is currently responding on port 8000.
}

$portOwner = netstat -ano -p tcp |
    Select-String -Pattern "^\s*TCP\s+127\.0\.0\.1:8000\s+.*LISTENING\s+(\d+)\s*$" |
    Select-Object -First 1

if ($portOwner) {
    $processId = $portOwner.Matches[0].Groups[1].Value
    throw "Port 8000 is occupied by process $processId. Stop it or use another port."
}

conda run -n testin python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
if ($LASTEXITCODE -ne 0) {
    throw "The application failed to start."
}
