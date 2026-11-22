$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Get-Command conda -ErrorAction SilentlyContinue)) {
    throw "Conda was not found. Install Anaconda or Miniconda first."
}

$envExists = conda env list | Select-String -Pattern "^\s*testin\s+"
if (-not $envExists) {
    conda create -n testin python=3.11 -y
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create the testin Conda environment."
    }
}

conda run -n testin python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install Python dependencies."
}

. (Join-Path $PSScriptRoot 'initialize_env.ps1')

Write-Host "Setup completed. Run .\scripts\start.ps1 to start the application."

