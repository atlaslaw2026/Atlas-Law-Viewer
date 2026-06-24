param(
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'

$baseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$bundledPython = Join-Path $baseDir 'runtime\python\python.exe'
$venvDir = Join-Path $baseDir '.venv'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
$requirements = Join-Path $baseDir 'requirements.txt'

function Write-Info {
    param([string]$Message)
    if (-not $Quiet) { Write-Host $Message }
}

function Resolve-SystemPython {
    $candidates = @(
        (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
        (Get-Command py -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
        'C:\Python313\python.exe',
        'C:\Python312\python.exe',
        'C:\Python311\python.exe',
        'C:\Python310\python.exe'
    ) | Where-Object { $_ } | Select-Object -Unique

    foreach ($candidate in $candidates) {
        if (-not (Test-Path $candidate)) { continue }
        try {
            & $candidate -c "import sys; print(sys.version)" *> $null
            if ($LASTEXITCODE -eq 0) { return $candidate }
        }
        catch {
        }
    }

    return $null
}

function Test-AtlasRuntime {
    param([Parameter(Mandatory = $true)][string]$Exe)

    if (-not (Test-Path $Exe)) { return $false }
    try {
        & $Exe -c "import requests, bs4, cloudscraper, fitz, PyPDF2" *> $null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
}

function Install-AtlasRequirements {
    param([Parameter(Mandatory = $true)][string]$Exe)

    & $Exe -m pip --version *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Python runtime at '$Exe' does not have pip."
    }

    Write-Info "[Atlas] Installing/updating dependencies..."
    & $Exe -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to upgrade pip for '$Exe'."
    }

    & $Exe -m pip install -r $requirements
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install dependencies from requirements.txt using '$Exe'."
    }
}

if (-not (Test-Path $requirements)) {
    throw "Missing requirements.txt in $baseDir"
}

if (Test-Path $bundledPython) {
    Write-Info "[Atlas] Bundled runtime detected."
    if (-not (Test-AtlasRuntime -Exe $bundledPython)) {
        try {
            Install-AtlasRequirements -Exe $bundledPython
        }
        catch {
            Write-Info "[Atlas] Bundled pip install failed, continuing anyway: $_"
        }
    }
    # Accept bundled runtime even if optional packages are missing — server handles them gracefully.
    Write-Info "[Atlas] Bootstrap complete (bundled runtime)."
    return
}

if (-not (Test-Path $venvPython)) {
    $pythonExe = Resolve-SystemPython
    if (-not $pythonExe) {
        throw "No usable Python executable found. Install Python 3.10+ and re-run."
    }

    Write-Info "[Atlas] Creating virtual environment..."
    & $pythonExe -m venv $venvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython)) {
        throw "Failed to create virtual environment."
    }
}

Install-AtlasRequirements -Exe $venvPython

Write-Info "[Atlas] Bootstrap complete."
