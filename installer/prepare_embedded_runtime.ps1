param(
    [string]$PythonVersion = '3.11.9'
)

$ErrorActionPreference = 'Stop'

$baseDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$runtimeDir = Join-Path $baseDir 'runtime\python'
$requirements = Join-Path $baseDir 'requirements.txt'
$tempDir = Join-Path $baseDir 'runtime\_build_tmp'

if (-not (Test-Path $requirements)) {
    throw "requirements.txt not found in $baseDir"
}

$zipName = "python-$PythonVersion-embed-amd64.zip"
$zipUrl = "https://www.python.org/ftp/python/$PythonVersion/$zipName"
$zipPath = Join-Path $tempDir $zipName
$getPipPath = Join-Path $tempDir 'get-pip.py'

Write-Host "[Atlas Installer] Preparing embedded Python runtime $PythonVersion..."

New-Item -ItemType Directory -Path $tempDir -Force | Out-Null
if (Test-Path $runtimeDir) {
    Remove-Item -Recurse -Force $runtimeDir
}
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null

Write-Host "[Atlas Installer] Downloading embeddable Python..."
Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath

Write-Host "[Atlas Installer] Extracting runtime..."
Expand-Archive -Path $zipPath -DestinationPath $runtimeDir -Force

$pthFile = Get-ChildItem $runtimeDir -Filter 'python*._pth' | Select-Object -First 1
if (-not $pthFile) {
    throw "Could not locate python*._pth in embedded runtime."
}

$stdlibZip = Get-ChildItem $runtimeDir -Filter 'python*.zip' | Select-Object -First 1
if (-not $stdlibZip) {
    throw "Could not locate python*.zip in embedded runtime."
}

$pthLines = @(
    $stdlibZip.Name,
    '.',
    'Lib\site-packages',
    'import site'
)
$pthText = ($pthLines -join "`r`n") + "`r`n"
Set-Content -Path $pthFile.FullName -Value $pthText -Encoding ASCII

$pythonExe = Join-Path $runtimeDir 'python.exe'
if (-not (Test-Path $pythonExe)) {
    throw "Embedded python.exe not found after extraction."
}

Write-Host "[Atlas Installer] Installing pip into embedded runtime..."
Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile $getPipPath
& $pythonExe $getPipPath
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install pip in embedded runtime."
}

Write-Host "[Atlas Installer] Installing Atlas dependencies into embedded runtime..."
& $pythonExe -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "Failed to upgrade pip in embedded runtime."
}

& $pythonExe -m pip install -r $requirements
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install requirements in embedded runtime."
}

Write-Host "[Atlas Installer] Validating runtime imports..."
& $pythonExe -c "import requests, bs4, cloudscraper, fitz, PyPDF2; print('ok')"
if ($LASTEXITCODE -ne 0) {
    throw "Embedded runtime validation failed."
}

if (Test-Path $tempDir) {
    Remove-Item -Recurse -Force $tempDir
}

Write-Host "[Atlas Installer] Embedded runtime ready at: $runtimeDir"
Write-Host "[Atlas Installer] Build the installer next with installer/AtlasLawViewer.iss"
