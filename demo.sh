#!/usr/bin/env bash
#
# Demo: run the Autonoma self-check on Linux via pwsh.
#
# Usage:
#   ./demo.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "== Autonoma demo (pwsh on Linux) =="
if ! command -v pwsh >/dev/null 2>&1; then
    echo "pwsh is not installed. Install PowerShell 7 and re-run." >&2
    exit 1
fi

pwsh -NoProfile -File "$ROOT/tools/verify.ps1"
