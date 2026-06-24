$ErrorActionPreference = 'Stop'

$baseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $baseDir

$bundledPython = Join-Path $baseDir 'runtime\python\python.exe'
$venvPython = Join-Path $baseDir '.venv\Scripts\python.exe'
$pythonCandidates = @(
    $bundledPython,
    $venvPython,
    (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
    (Get-Command py -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
    'C:\Python310\python.exe',
    'C:\Python311\python.exe',
    'C:\Python312\python.exe',
    'C:\Python313\python.exe'
) | Where-Object { $_ } | Select-Object -Unique

$pythonExe = $null
foreach ($candidate in $pythonCandidates) {
    if (-not (Test-Path $candidate)) {
        continue
    }

    $prevErrPref = $ErrorActionPreference
    $prevNativeErr = $PSNativeCommandUseErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $PSNativeCommandUseErrorActionPreference = $false
    try {
        & $candidate -c "import sys" *> $null
        if ($LASTEXITCODE -eq 0) {
            $pythonExe = $candidate
            break
        }
    }
    finally {
        $ErrorActionPreference = $prevErrPref
        $PSNativeCommandUseErrorActionPreference = $prevNativeErr
    }
}

if (-not $pythonExe) {
    throw "No usable Python executable found for Atlas server startup."
}

$env:HOST = '0.0.0.0'
$env:PORT = '8080'

$connections = Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue
if ($connections) {
    $pids = $connections | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($procId in $pids) {
        try {
            Stop-Process -Id $procId -Force -ErrorAction Stop
            Write-Host "Stopped existing process on port 8080 (PID $procId)"
        }
        catch {
            Write-Warning "Could not stop PID ${procId}: $($_.Exception.Message)"
        }
    }
    Start-Sleep -Milliseconds 400
}

Write-Host "Starting Atlas server on http://127.0.0.1:8080 ..."
try {
    $lanIp = Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1' -and $_.PrefixOrigin -ne 'WellKnown' } |
        Select-Object -First 1 -ExpandProperty IPAddress
    if ($lanIp) {
        Write-Host "Phone URL (same Wi-Fi): http://$lanIp:8080/"
    }
}
catch {
}
& $pythonExe (Join-Path $baseDir 'atlas_law_server.py')