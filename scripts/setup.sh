#!/usr/bin/env bash
# Create the project virtualenv and install pinned dependencies (Linux/macOS).
#
#   ./setup.sh [--skip-playwright] [--python /path/to/python3.12]
#
# Installs nothing into the system Python, needs no root, and stops at the
# first failure with an actionable message.

set -Eeuo pipefail

SKIP_PLAYWRIGHT=0
PYTHON_EXE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --skip-playwright) SKIP_PLAYWRIGHT=1; shift ;;
    --python) PYTHON_EXE="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
if [ ! -f "$REPO_ROOT/pyproject.toml" ]; then
  REPO_ROOT="$SCRIPT_DIR"
fi
VENV_DIR="$REPO_ROOT/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
LOCK_FILE="$REPO_ROOT/requirements.lock"

fail() { printf '\nSETUP FAILED: %s\n' "$1" >&2; exit 1; }
step() { printf '\n==> %s\n' "$1"; }

# --- 1. locate Python 3.12 --------------------------------------------------
step "Looking for Python 3.12"
PYTHON=""
for candidate in "$PYTHON_EXE" python3.12 python3 python; do
  [ -n "$candidate" ] || continue
  command -v "$candidate" >/dev/null 2>&1 || continue
  version="$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
  if [ "$version" = "3.12" ]; then
    PYTHON="$candidate"
    echo "    found Python $version at $(command -v "$candidate")"
    break
  fi
done

if [ -z "$PYTHON" ]; then
  fail "No Python 3.12 interpreter found.

Install it (pyenv install 3.12, brew install python@3.12, or your distro's
package), then re-run. To point at a specific interpreter:

    ./setup.sh --python /path/to/python3.12"
fi

# --- 2. virtualenv -----------------------------------------------------------
step "Creating virtualenv at $VENV_DIR"
if [ -x "$VENV_PYTHON" ]; then
  echo "    .venv already exists; reusing it"
else
  "$PYTHON" -m venv "$VENV_DIR" || fail "python -m venv failed"
fi
[ -x "$VENV_PYTHON" ] || fail "expected interpreter not found at $VENV_PYTHON"

# --- 3. dependencies ----------------------------------------------------------
step "Upgrading pip inside the virtualenv"
"$VENV_PYTHON" -m pip install --upgrade pip --disable-pip-version-check \
  || fail "pip upgrade failed"

[ -f "$LOCK_FILE" ] || fail "requirements.lock not found at $LOCK_FILE"

step "Installing pinned dependencies from requirements.lock"
"$VENV_PYTHON" -m pip install --require-virtualenv -r "$LOCK_FILE" || fail \
"Installing the pinned dependencies failed.

If a pin cannot be resolved on this machine, regenerate the lock file:

    $VENV_PYTHON -m pip install -e '.[dev]'
    $VENV_PYTHON -m pip freeze --exclude-editable > requirements.lock"

step "Installing the project in editable mode"
"$VENV_PYTHON" -m pip install --require-virtualenv -e "$REPO_ROOT" \
  || fail "editable install failed"

# --- 4. playwright --------------------------------------------------------------
if [ "$SKIP_PLAYWRIGHT" -eq 1 ]; then
  printf '\n    skipping Playwright Chromium (--skip-playwright)\n'
else
  step "Installing Playwright Chromium"
  "$VENV_PYTHON" -m playwright install chromium || fail \
"Playwright could not download Chromium.

Chromium is only needed for JavaScript-rendered article pages. To continue
without it, re-run with --skip-playwright and set
extraction.playwright.enabled to false in your config."
fi

# --- 5. next steps ----------------------------------------------------------------
cat <<'NEXT'

Setup complete.

Next:
    cp .env.example .env
    $EDITOR .env                       # fill in CRAWLER_CONTACT_EMAIL (and SERPAPI_API_KEY if you have one)
    ./.venv/bin/python -m tw_cdc_influenza_news doctor
NEXT
