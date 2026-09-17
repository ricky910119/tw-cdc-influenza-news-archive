<#
.SYNOPSIS
    Convenience wrapper around the CLI using the repository's own virtualenv.

.EXAMPLE
    .\run.ps1 doctor
    .\run.ps1 crawl --start-date 2023-01-01 --end-date 2026-12-31 --provider serpapi-google-news
    .\run.ps1 resume --run-id run-20260917T101500-a1b2c3
    .\run.ps1 report --run-id run-20260917T101500-a1b2c3

.DESCRIPTION
    Loads .env (if present) into this process only, then runs the CLI with
    .venv\Scripts\python.exe. Nothing is exported to the machine, and no
    value from .env is echoed.
#>

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CliArgs
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $RepoRoot 'pyproject.toml'))) { $RepoRoot = $PSScriptRoot }
$VenvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $VenvPython)) {
    Write-Host 'No virtualenv found. Run setup first:' -ForegroundColor Red
    Write-Host '    powershell -ExecutionPolicy Bypass -File .\setup.ps1'
    exit 1
}

# Load .env into THIS process only. Values are never printed.
$EnvFile = Join-Path $RepoRoot '.env'
if (Test-Path $EnvFile) {
    foreach ($line in Get-Content $EnvFile) {
        $trimmed = $line.Trim()
        if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }
        $split = $trimmed.IndexOf('=')
        if ($split -lt 1) { continue }
        $name = $trimmed.Substring(0, $split).Trim()
        $value = $trimmed.Substring($split + 1).Trim().Trim('"').Trim("'")
        Set-Item -Path "Env:$name" -Value $value
    }
    Write-Host "loaded $EnvFile ($((Get-Content $EnvFile | Where-Object { $_ -match '^\s*[^#].*=' }).Count) variables)" -ForegroundColor DarkGray
}

if (-not $CliArgs -or $CliArgs.Count -eq 0) { $CliArgs = @('--help') }

& $VenvPython -m tw_cdc_influenza_news @CliArgs
exit $LASTEXITCODE
