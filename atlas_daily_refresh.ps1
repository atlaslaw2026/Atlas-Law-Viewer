$ErrorActionPreference = 'Stop'

$baseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$bundledPython = Join-Path $baseDir 'runtime\python\python.exe'
$venvPython = Join-Path $baseDir '.venv\Scripts\python.exe'
$pythonCandidates = @(
    $bundledPython,
    $venvPython,
    (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
    (Get-Command py -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue)
)
$pythonCandidates = $pythonCandidates | Where-Object { $_ }

function Test-PythonCandidate {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Exe
    )

    if (-not (Test-Path $Exe)) {
        return $false
    }

    $prevErrPref = $ErrorActionPreference
    $prevNativeErr = $PSNativeCommandUseErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $PSNativeCommandUseErrorActionPreference = $false
    try {
        & $Exe -c "import requests, bs4; print('ok')" *> $null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
    finally {
        $ErrorActionPreference = $prevErrPref
        $PSNativeCommandUseErrorActionPreference = $prevNativeErr
    }
}

foreach ($candidate in $pythonCandidates) {
    if (Test-PythonCandidate -Exe $candidate) {
        $python = $candidate
        break
    }
}

if (-not $python) {
    throw "No usable Python interpreter found with required packages (requests, bs4)."
}

# App-triggered refresh can inherit PYTHONWARNINGS=error from parent process.
# Force a non-fatal warnings mode so generator SyntaxWarnings do not abort the refresh.
$env:PYTHONWARNINGS = 'default'
# App-triggered no-window runs can default to cp1252; force UTF-8 so unicode logs do not crash.
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$logDir = Join-Path $baseDir 'logs'
if (-not (Test-Path $logDir)) {
    New-Item -Path $logDir -ItemType Directory | Out-Null
}

$timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$logPath = Join-Path $logDir ("atlas_refresh_" + $timestamp + ".log")
$summaryPath = Join-Path $logDir 'atlas_refresh_last_summary.json'
$baseDirPy = $baseDir.Replace('\\', '/')

function Get-OpinionCounts {
    $jsonOut = & $python -c "import json, os; b=r'$baseDirPy'; paths={'ninth':'opinions_data.json','supreme':'supreme_opinions_data.json','central':'central_opinions_data.json'}; out={};
for k,p in paths.items():
    fp=os.path.join(b,p)
    try:
        with open(fp, encoding='utf-8') as f:
            out[k]=len(json.load(f))
    except Exception:
        out[k]=0
print(json.dumps(out))"

    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($jsonOut)) {
        return @{ ninth = 0; supreme = 0; central = 0 }
    }

    try {
        return ($jsonOut | ConvertFrom-Json)
    }
    catch {
        return @{ ninth = 0; supreme = 0; central = 0 }
    }
}

function Invoke-Step {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [string]$ScriptPath
    )

    Write-Host "==> $Name"
    $safeName = ($Name -replace '[^A-Za-z0-9]+', '_').Trim('_')
    $stepLog = Join-Path $logDir ("atlas_refresh_step_" + $timestamp + "_" + $safeName + ".log")
    if (Test-Path $stepLog) { Remove-Item $stepLog -Force -ErrorAction SilentlyContinue }

    $cmdLine = '"{0}" "{1}" 1>>"{2}" 2>>&1' -f $python, $ScriptPath, $stepLog
    & cmd.exe /d /c $cmdLine
    $exitCode = $LASTEXITCODE

    if ($exitCode -ne 0) {
        if (Test-Path $stepLog) {
            $tail = Get-Content -Path $stepLog -Tail 80 -ErrorAction SilentlyContinue
            if ($tail) {
                Write-Host "--- Step log tail: $stepLog ---"
                $tail | ForEach-Object { Write-Host $_ }
                Write-Host "--- End step log tail ---"
            }
        }
        throw "Step failed: $Name (exit code $exitCode). See $stepLog"
    }
}

Start-Transcript -Path $logPath -Force | Out-Null
try {
    Set-Location $baseDir
    $env:ATLAS_NO_BROWSER = '1'
    $before = Get-OpinionCounts

    Invoke-Step -Name 'Build U.S. Supreme Court viewer' -ScriptPath (Join-Path $baseDir 'supreme_court_viewer.py')
    Invoke-Step -Name 'Build Ninth Circuit viewer' -ScriptPath (Join-Path $baseDir 'atlas_law_v1.py')
    Invoke-Step -Name 'Build Central District (C.D. Cal.) viewer' -ScriptPath (Join-Path $baseDir 'central_district_viewer.py')
    Write-Host '==> Verification summary'
    & $python -c "import json, os; b=r'$baseDirPy'; d=json.load(open(os.path.join(b,'opinions_data.json'),encoding='utf-8')); sc=json.load(open(os.path.join(b,'supreme_opinions_data.json'),encoding='utf-8')); cd=json.load(open(os.path.join(b,'central_opinions_data.json'),encoding='utf-8')); print('std_count',len(d)); print('supreme_count',len(sc)); print('central_count',len(cd)); print('std_max_date',max((x.get('issue_date') or '') for x in d)); print('supreme_max_date',max((x.get('issue_date') or '') for x in sc)); print('central_max_date',max((x.get('issue_date') or '') for x in cd))"
    if ($LASTEXITCODE -ne 0) {
        throw 'Verification step failed'
    }

    $after = Get-OpinionCounts
    $addedNinth = [Math]::Max(0, [int]$after.ninth - [int]$before.ninth)
    $addedSupreme = [Math]::Max(0, [int]$after.supreme - [int]$before.supreme)
    $addedCentral = [Math]::Max(0, [int]$after.central - [int]$before.central)
    $addedTotal = $addedNinth + $addedSupreme + $addedCentral

    $summary = [ordered]@{
        started_at = $timestamp
        finished_at = (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss')
        before = @{
            ninth = [int]$before.ninth
            supreme = [int]$before.supreme
            central = [int]$before.central
        }
        after = @{
            ninth = [int]$after.ninth
            supreme = [int]$after.supreme
            central = [int]$after.central
        }
        added = @{
            ninth = $addedNinth
            supreme = $addedSupreme
            central = $addedCentral
            total = $addedTotal
        }
        total_added = $addedTotal
    }
    ($summary | ConvertTo-Json -Depth 6) | Set-Content -Path $summaryPath -Encoding UTF8

    Write-Host "Refresh completed successfully. Log: $logPath"
}
catch {
    Write-Error $_
    exit 1
}
finally {
    Stop-Transcript | Out-Null
}
