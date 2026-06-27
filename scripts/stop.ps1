$ErrorActionPreference = "Stop"

$listeners = netstat -ano -p tcp |
    Select-String -Pattern "^\s*TCP\s+127\.0\.0\.1:8000\s+.*LISTENING\s+(\d+)\s*$"

if (-not $listeners) {
    Write-Host "No application is listening on port 8000."
    exit 0
}

$processIds = $listeners | ForEach-Object {
    $_.Matches[0].Groups[1].Value
} | Sort-Object -Unique

foreach ($processId in $processIds) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $processId"
    if (-not $process) {
        continue
    }

    $isThisApplication = (
        $process.Name -match "^python" -and
        $process.CommandLine -match "uvicorn\s+app\.main:app"
    )
    if (-not $isThisApplication) {
        throw "Port 8000 is used by another application (PID $processId). It was not stopped."
    }

    Get-Process -Id $processId | Stop-Process -Force
    Write-Host "Stopped the application process $processId."
}
