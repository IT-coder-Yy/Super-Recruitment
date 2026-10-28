$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if ($env:TALENTFLOW_PYTHON) {
    & $env:TALENTFLOW_PYTHON -B scripts/run_tests.py @args
} elseif (Get-Command conda -ErrorAction SilentlyContinue) {
    conda run -n testin python -B scripts/run_tests.py @args
} else {
    python -B scripts/run_tests.py @args
}
exit $LASTEXITCODE
