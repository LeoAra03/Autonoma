#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m pip install ".[build,keyboard]"
python3 -m PyInstaller --noconfirm --clean autonoma.spec
echo "Binario en dist/Autonoma"
