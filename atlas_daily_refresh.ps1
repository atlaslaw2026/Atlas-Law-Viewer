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
        & $Exe -c "import requests, bs4, fitz, websockets; print('ok')" *> $null
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
        [string]$ScriptPath,
        [switch]$ContinueOnError
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
        $message = "Step failed: $Name (exit code $exitCode). See $stepLog"
        if ($ContinueOnError) {
            Write-Warning $message
            return [ordered]@{
                name = $Name
                ok = $false
                exit_code = $exitCode
                log = $stepLog
                message = $message
            }
        }
        throw $message
    }

    return [ordered]@{
        name = $Name
        ok = $true
        exit_code = 0
        log = $stepLog
        message = ""
    }
}

function Get-ChromeExecutable {
    $candidates = @(
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
    )

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) {
            return $candidate
        }
    }

    $command = Get-Command chrome.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    return ""
}

function Test-CacdChromeListingReady {
    param(
        [Parameter(Mandatory = $true)]
        [int]$Port
    )

    $previousDebugUrl = $env:JUSTIA_CDP_DEBUG_URL
    $env:JUSTIA_CDP_DEBUG_URL = "http://127.0.0.1:$Port/json"
    try {
        $output = & $python (Join-Path $baseDir 'tools\justia_cdp_helper.py') eval "([...document.links].filter(a=>a.href.startsWith('https://law.justia.com/cases/federal/district-courts/california/cacdce/') && /\/[0-9]+:[0-9]{4}cv[0-9]+\//.test(a.href)).length)" 2>$null
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($output)) {
            return $false
        }
        $joined = ($output -join "`n").Trim()
        $count = 0
        if ([int]::TryParse($joined, [ref]$count)) {
            return ($count -gt 0)
        }
        return $false
    }
    finally {
        $env:JUSTIA_CDP_DEBUG_URL = $previousDebugUrl
    }
}

function Invoke-CacdJustiaChromeScrape {
    Write-Host "==> Refresh Central District Justia listing/PDF links"
    $safeName = 'Refresh_Central_District_Justia_listing_PDF_links'
    $stepLog = Join-Path $logDir ("atlas_refresh_step_" + $timestamp + "_" + $safeName + ".log")
    if (Test-Path $stepLog) { Remove-Item $stepLog -Force -ErrorAction SilentlyContinue }

    $chrome = Get-ChromeExecutable
    if (-not $chrome) {
        $message = "Chrome was not found; using existing Central District listing/PDF link cache."
        Write-Warning $message
        $message | Set-Content -Path $stepLog -Encoding UTF8
        return [ordered]@{
            name = "Refresh Central District Justia listing/PDF links"
            ok = $false
            exit_code = 1
            log = $stepLog
            message = $message
        }
    }

    $port = 9223
    $profileDir = Join-Path $baseDir ".tmp\justia-update-profile-$timestamp"
    New-Item -Path $profileDir -ItemType Directory -Force | Out-Null

    $listingUrl = 'https://law.justia.com/cases/federal/district-courts/california/cacdce/2026/'
    $arguments = @(
        "--remote-debugging-port=$port",
        "--user-data-dir=$profileDir",
        "--no-first-run",
        "--new-window",
        $listingUrl
    )

    $process = $null
    $previousDebugUrl = $env:JUSTIA_CDP_DEBUG_URL
    $env:JUSTIA_CDP_DEBUG_URL = "http://127.0.0.1:$port/json"
    try {
        "Starting Chrome: $chrome $($arguments -join ' ')" | Add-Content -Path $stepLog -Encoding UTF8
        $process = Start-Process -FilePath $chrome -ArgumentList $arguments -PassThru

        $ready = $false
        $deadline = (Get-Date).AddMinutes(4)
        while ((Get-Date) -lt $deadline) {
            if (Test-CacdChromeListingReady -Port $port) {
                $ready = $true
                break
            }
            Start-Sleep -Seconds 3
        }

        if (-not $ready) {
            $message = "Justia listing did not become available in Chrome before timeout; using existing Central District listing/PDF link cache."
            Write-Warning $message
            $message | Add-Content -Path $stepLog -Encoding UTF8
            return [ordered]@{
                name = "Refresh Central District Justia listing/PDF links"
                ok = $false
                exit_code = 2
                log = $stepLog
                message = $message
            }
        }

        $helperScript = Join-Path $baseDir 'tools\justia_cdp_helper.py'
        $seedPath = Join-Path $baseDir 'central_listing_seed.tsv'
        $detailPath = Join-Path $baseDir 'central_case_details_from_chrome.json'

        $listingCmdLine = '"{0}" "{1}" scrape-listing --output "{2}" 1>>"{3}" 2>>&1' -f $python, $helperScript, $seedPath, $stepLog
        & cmd.exe /d /c $listingCmdLine
        if ($LASTEXITCODE -ne 0) {
            throw "CACD Justia listing scrape failed"
        }

        $pdfCmdLine = '"{0}" "{1}" scrape-pdfs --input "{2}" --output "{3}" 1>>"{4}" 2>>&1' -f $python, $helperScript, $seedPath, $detailPath, $stepLog
        & cmd.exe /d /c $pdfCmdLine
        if ($LASTEXITCODE -ne 0) {
            throw "CACD Justia PDF-link scrape failed"
        }

        return [ordered]@{
            name = "Refresh Central District Justia listing/PDF links"
            ok = $true
            exit_code = 0
            log = $stepLog
            message = ""
        }
    }
    catch {
        $message = "Step failed: Refresh Central District Justia listing/PDF links. $_"
        Write-Warning $message
        $message | Add-Content -Path $stepLog -Encoding UTF8
        return [ordered]@{
            name = "Refresh Central District Justia listing/PDF links"
            ok = $false
            exit_code = 1
            log = $stepLog
            message = $message
        }
    }
    finally {
        $env:JUSTIA_CDP_DEBUG_URL = $previousDebugUrl
        if ($process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Seconds 1
        if (Test-Path $profileDir) {
            Remove-Item -LiteralPath $profileDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

Start-Transcript -Path $logPath -Force | Out-Null
try {
    Set-Location $baseDir
    $env:ATLAS_NO_BROWSER = '1'
    $env:CENTRAL_REFRESH_STRICT = '1'
    $before = Get-OpinionCounts

    $stepResults = @()
    $stepResults += Invoke-Step -Name 'Build U.S. Supreme Court viewer' -ScriptPath (Join-Path $baseDir 'supreme_court_viewer.py')
    $stepResults += Invoke-Step -Name 'Build Ninth Circuit viewer' -ScriptPath (Join-Path $baseDir 'atlas_law_v1.py')
    $stepResults += Invoke-CacdJustiaChromeScrape
    $env:CENTRAL_FETCH_LIMIT = '0'
    $env:CENTRAL_PDF_LIMIT = '500'
    $stepResults += Invoke-Step -Name 'Build Central District (C.D. Cal.) viewer' -ScriptPath (Join-Path $baseDir 'central_district_viewer.py') -ContinueOnError
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
    $failedSteps = @($stepResults | Where-Object { -not $_.ok })

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
        partial_failure = ($failedSteps.Count -gt 0)
        warnings = @($failedSteps | ForEach-Object { $_.message })
        steps = $stepResults
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
