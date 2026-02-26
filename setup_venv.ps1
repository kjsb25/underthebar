# Sets up a virtual environment using the Python version in .python-version
# and installs dependencies from requirements.txt.
# Requires: uv (https://github.com/astral-sh/uv)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

# Refresh PATH from the machine/user environment in case uv was just installed
$env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" +
            [System.Environment]::GetEnvironmentVariable("PATH", "User")

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "Error: 'uv' is not installed.`nInstall it with: powershell -c `"irm https://astral.sh/uv/install.ps1 | iex`"`nOr see: https://github.com/astral-sh/uv"
    exit 1
}

$PythonVersion = (Get-Content .python-version).Trim()
Write-Host "Python version: $PythonVersion"

Write-Host "Creating virtual environment..."
uv venv --python $PythonVersion .venv

Write-Host "Installing dependencies from requirements.txt..."
uv pip install --python .venv\Scripts\python.exe -r requirements.txt

Write-Host ""
Write-Host "Done. Activate with:"
Write-Host "  .venv\Scripts\Activate.ps1"
