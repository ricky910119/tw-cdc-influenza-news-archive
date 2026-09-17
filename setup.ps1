# Thin wrapper so the documented install flow works from the repository root.
# The implementation lives in scripts\setup.ps1.
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'scripts\setup.ps1') @args
exit $LASTEXITCODE
