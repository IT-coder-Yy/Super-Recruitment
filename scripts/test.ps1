$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

Write-Host "Running syntax check..."
if (Get-Command conda -ErrorAction SilentlyContinue) {
    conda run -n testin python -m compileall app tests scripts
    if ($LASTEXITCODE -ne 0) { throw "Syntax check failed." }

    Write-Host "Running pytest in conda env: testin..."
    conda run -n testin python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "Tests failed." }
} else {
    Write-Host "Conda not found; falling back to current Python."
    python -m compileall app tests scripts
    if ($LASTEXITCODE -ne 0) { throw "Syntax check failed." }

    python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "Tests failed." }
}

Write-Host "All checks passed."
