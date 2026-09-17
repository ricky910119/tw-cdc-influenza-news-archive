<#
.SYNOPSIS
    Create the project virtualenv and install pinned dependencies.

.DESCRIPTION
    Run from anywhere:
        powershell -ExecutionPolicy Bypass -File .\setup.ps1

    What it does, and nothing else:
      1. finds a Python 3.12 interpreter
      2. creates .venv INSIDE the repository
      3. installs the pinned dependencies from requirements.lock
      4. installs this package in editable mode
      5. downloads the Playwright Chromium build into the user cache

    It never installs into the system Python, never needs administrator
    rights, and stops at the first failure with an actionable message.
#>

[CmdletBinding()]
param(
    [switch]$SkipPlaywright,
    [string]$PythonExe
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $RepoRoot 'pyproject.toml'))) {
    # Allow the script to live at the repo root as well as in scripts/.
    $RepoRoot = $PSScriptRoot
}
$VenvDir = Join-Path $RepoRoot '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$LockFile = Join-Path $RepoRoot 'requirements.lock'

function Fail([string]$Message) {
    Write-Host ''
    Write-Host "SETUP FAILED: $Message" -ForegroundColor Red
    exit 1
}

function Step([string]$Message) {
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

# --- 1. locate Python 3.12 --------------------------------------------------
Step 'Looking for Python 3.12'

$Candidates = @()
if ($PythonExe) { $Candidates += $PythonExe }
$Candidates += 'py -3.12'
$Candidates += 'python3.12'
$Candidates += 'python'

$Python = $null
foreach ($candidate in $Candidates) {
    $parts = $candidate -split ' '
    $exe = $parts[0]
    $args = @()
    if ($parts.Length -gt 1) { $args = $parts[1..($parts.Length - 1)] }

    $resolved = Get-Command $exe -ErrorAction SilentlyContinue
    if ($null -eq $resolved) { continue }

    try {
        $version = & $exe @args -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    } catch {
        continue
    }
    if ($version -eq '3.12') {
        $Python = @{ Exe = $exe; Args = $args }
        Write-Host "    found Python $version via '$candidate'"
        break
    }
}

if ($null -eq $Python) {
    Fail @'
No Python 3.12 interpreter was found.

Install it from https://www.python.org/downloads/release/python-3120/ or
with winget:

    winget install Python.Python.3.12

Then re-run this script. If 3.12 is installed somewhere unusual, pass it
explicitly:

    powershell -ExecutionPolicy Bypass -File .\setup.ps1 -PythonExe "C:\Path\To\python.exe"
'@
}

# --- 2. create the virtualenv ----------------------------------------------
Step "Creating virtualenv at $VenvDir"

if (Test-Path $VenvPython) {
    Write-Host '    .venv already exists; reusing it'
} else {
    & $Python.Exe @($Python.Args) -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { Fail "python -m venv failed with exit code $LASTEXITCODE" }
}
if (-not (Test-Path $VenvPython)) { Fail "expected interpreter not found at $VenvPython" }

# --- 3. install dependencies ------------------------------------------------
Step 'Upgrading pip inside the virtualenv'
& $VenvPython -m pip install --upgrade pip --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Fail "pip upgrade failed with exit code $LASTEXITCODE" }

if (-not (Test-Path $LockFile)) { Fail "requirements.lock not found at $LockFile" }

Step 'Installing pinned dependencies from requirements.lock'
& $VenvPython -m pip install --require-virtualenv -r $LockFile
if ($LASTEXITCODE -ne 0) {
    Fail @"
Installing the pinned dependencies failed (exit code $LASTEXITCODE).

If a pin cannot be resolved on this machine, regenerate the lock file:

    $VenvPython -m pip install -e ".[dev]"
    $VenvPython -m pip freeze --exclude-editable > requirements.lock
"@
}

Step 'Installing the project in editable mode'
& $VenvPython -m pip install --require-virtualenv -e "$RepoRoot"
if ($LASTEXITCODE -ne 0) { Fail "editable install failed with exit code $LASTEXITCODE" }

# --- 4. playwright chromium --------------------------------------------------
if ($SkipPlaywright) {
    Write-Host ''
    Write-Host '    skipping Playwright Chromium (-SkipPlaywright)' -ForegroundColor Yellow
} else {
    Step 'Installing Playwright Chromium'
    & $VenvPython -m playwright install chromium
    if ($LASTEXITCODE -ne 0) {
        Fail @"
Playwright could not download Chromium (exit code $LASTEXITCODE).

Chromium is only needed for JavaScript-rendered article pages. To continue
without it, re-run with -SkipPlaywright and set
extraction.playwright.enabled to false in your config.
"@
    }
}

# --- 5. next steps -------------------------------------------------------------
Write-Host ''
Write-Host 'Setup complete.' -ForegroundColor Green
Write-Host ''
Write-Host 'Next:'
Write-Host '    Copy-Item .env.example .env'
Write-Host '    notepad .env                       # fill in CRAWLER_CONTACT_EMAIL (and SERPAPI_API_KEY if you have one)'
Write-Host '    .\.venv\Scripts\python.exe -m tw_cdc_influenza_news doctor'
Write-Host ''
