#!/usr/bin/env bash
# Compila el binario portable para esta plataforma (Linux/macOS) con PyInstaller.
#
# Dos salidas posibles, en este orden de preferencia:
#   1) dist/Autonoma          binario onefile (necesita libpython compartido)
#   2) dist/autonoma.pyz      zipapp universal: un sólo archivo, corre con cualquier
#                             Python >= 3.10 (`python autonoma.pyz`), sin dependencias
#                             instaladas en el destino salvo httpx/rich/bs4/lxml/psutil.
#
# Uso:  scripts/build_portable.sh [--skip-smoke] [--zipapp]
#   --skip-smoke  no ejecuta pruebas ni autoensayos (rápido, para CI que ya los corrió)
#   --zipapp      fuerza el zipapp universal, saltando PyInstaller
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python3
SKIP_SMOKE=0
ZIPAPP_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --skip-smoke) SKIP_SMOKE=1 ;;
    --zipapp) ZIPAPP_ONLY=1 ;;
    *) echo "aviso: argumento ignorado: $arg" >&2 ;;
  esac
done

bundle() {
  # ZIP "copiar y usar": binario + plantilla de .env + LEEME generado (scripts/make_bundle.py).
  local bundle_script="$ROOT/../scripts/make_bundle.py"
  [[ -f "$bundle_script" ]] || { echo "aviso: sin $bundle_script; no se arma el ZIP portable" >&2; return 0; }
  "$PY" "$bundle_script" --platform linux --dist-dir "$ROOT/dist" || echo "aviso: no se pudo armar el ZIP portable" >&2
}

build_zipapp() {
  echo "-- construyendo zipapp portable (dist/autonoma.pyz)"
  rm -rf build/pyz
  mkdir -p build/pyz/autonoma build/pyz/vendor
  cp -R autonoma/. build/pyz/autonoma/
  find build/pyz -name '__pycache__' -type d -prune -exec rm -rf {} +
  # Sin dependencias embebidas: el zipapp exige `pip install autonoma-agent` en el
  # destino o PYTHONPATH con los wheels; se documenta en el aviso final.
  cat > build/pyz/__main__.py <<'PYMAIN'
import sys
from autonoma.cli import main

if __name__ == "__main__":
    sys.exit(main())
PYMAIN
  "$PY" -m zipapp build/pyz -o dist/autonoma.pyz -p "/usr/bin/env python3" -c
  chmod +x dist/autonoma.pyz
  sha256sum dist/autonoma.pyz > dist/autonoma.pyz.sha256
  echo "-- dist/autonoma.pyz listo (requiere las dependencias en el entorno destino)"
}

if [[ "$SKIP_SMOKE" == "0" ]]; then
  if "$PY" -c 'import pytest' >/dev/null 2>&1; then
    echo "-- verificación previa: suite de pruebas"
    "$PY" -m pytest -q tests
  else
    # Falta el extra [test]: advertir, no abortar — construir el paquete no lo necesita.
    echo "aviso: sin pytest en $PY; se omite la verificación previa (pip install \".[test]\")" >&2
  fi
fi

mkdir -p dist build
if [[ "$ZIPAPP_ONLY" == "1" ]]; then
  echo "-- zipapp forzado"
  build_zipapp
  exit 0
fi

echo "-- PyInstaller (onefile)"
if "$PY" -m PyInstaller --noconfirm --clean autonoma.spec 2> build/pyinstaller.err; then
  BIN="dist/Autonoma"
  [[ -f "$BIN" ]] || { echo "no se encontró $BIN"; exit 1; }
  chmod +x "$BIN"
  sha256sum "$BIN" > "$BIN.sha256"
  if [[ "$SKIP_SMOKE" == "0" ]]; then
    echo "-- autoensayo del binario"
    "$BIN" --version
    AUTONOMA_HOME="$(mktemp -d)" "$BIN" --selftest --json | "$PY" -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d["ok"] else 1)'
    AUTONOMA_HOME="$(mktemp -d)" "$BIN" --doctor --json | "$PY" -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d["local_checks_passed"] and not d["network_tested"] else 1)'
  fi
  echo "-- listo: $BIN ($(du -h "$BIN" | cut -f1)) · hash en $BIN.sha256"
  bundle
  exit 0
fi

echo "   PyInstaller no pudo compilar ($(grep -m1 -o 'libpython[^ ]*' build/pyinstaller.err || echo 'ver build/pyinstaller.err'))"
build_zipapp
bundle
