#!/bin/sh
# Arranca Autonoma en Linux/macOS sin Node. Ej.: ./run-autonoma.sh "resume mis notas"
set -e
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BOOT="$HERE/scripts/bootstrap.py"
[ -f "$BOOT" ] || { echo "autonoma: falta scripts/bootstrap.py en $HERE" >&2; exit 127; }
for candidate in ${AUTONOMA_PYTHON:-} python3 python; do
  [ -n "$candidate" ] || continue
  if command -v "$candidate" >/dev/null 2>&1; then
    exec "$candidate" "$BOOT" run "$@"
  fi
done
echo "autonoma: necesito Python 3.10+ (https://www.python.org/downloads/) o el ejecutable portable." >&2
echo "autonoma: en Linux/macOS también puedes usar dist/Autonoma (ver INSTALL.md)." >&2
exit 127
