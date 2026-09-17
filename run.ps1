# Thin wrapper so the documented flow works from the repository root.
# The implementation lives in scripts\run.ps1.
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'scripts\run.ps1') @args
exit $LASTEXITCODE
