#!/usr/bin/env bash
# Thin wrapper so the documented install flow works from the repository root.
# The implementation lives in scripts/setup.sh.
set -Eeuo pipefail
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/setup.sh" "$@"
