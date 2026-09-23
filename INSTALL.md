# Instalar y usar Autonoma (de más fácil a más control)

Hay **cuatro** formas de arrancarlo. Todas llegan al mismo programa; elige la que te incomode menos.

---

## 1) El ejecutable portable — no se instala nada

Ideal para un USB, un PC prestado o para quien no quiere ver una terminal de configuración.

1. Baja el ZIP del Release (o del artefacto `Autonoma-Windows-unsigned` de Actions):
   `Autonoma-Portable-windows-<versión>.zip`.
2. Descomprime donde quieras. Queda algo así:

   ```
   Autonoma/
     Autonoma.exe          ← el agente completo, con todo dentro
     Autonoma.exe.sha256   ← para verificar que no se corrompió
     .env.example          ← plantilla de configuración
     LEEME.txt             ← estas instrucciones, resumidas
   ```

3. Renombra `.env.example` a `.env` y pega tu clave en `NOTRACK_API_KEY=`
   (se crea en <https://notrack.ai/api-keys>). Sin clave igual puedes leer y editar tus notas.
4. **Doble clic en `Autonoma.exe`** y escribe lo que quieras. O en una terminal:

   ```powershell
   .\Autonoma.exe "resume las notas de esta semana y guarda un resumen"
   .\Autonoma.exe --doctor --json        # qué está configurado y qué no
   ```

En Linux/macOS el equivalente es `dist/Autonoma` (binario) o `dist/autonoma.pyz` (corre con
cualquier Python 3.10+: `./autonoma.pyz "tu instrucción"`).

> El binario no está firmado: Windows mostrará el aviso de SmartScreen la primera vez
> («Más información» → «Ejecutar de todos modos»). Verifica el `.sha256` si te interesa.

---

## 2) `npm start` — si ya tienes Node

```bash
npm start                              # instala (una sola vez) y abre el agente
npm start -- "busca X y anótalo"        # un turno y sale
npm start -- --plain "resume mis notas" # sin animaciones (SSH, terminal vieja)
```

La primera vez, si no hay clave, te la pide por pantalla (no se ve mientras escribes) y la guarda
en `autonoma-agent/.env`. La segunda vez arranca en menos de un segundo: el entorno `.venv`
del repo se reutiliza.

| Comando npm | Qué hace |
| --- | --- |
| `npm start` | instalar lo que falte y abrir el agente (o correr el prompt que le pases) |
| `npm run setup` | sólo preparar el entorno (útil en CI o en un equipo nuevo) |
| `npm run key` | cambiar/guardar la `NOTRACK_API_KEY` |
| `npm run status` | JSON con python, entorno, versión y estado de la clave |
| `npm run doctor` | diagnóstico local (config, carpetas, dependencias, hotkey) |
| `npm run selftest` | autoensayo del paquete (`--selftest --json`) |
| `npm test` / `npm run lint` / `npm run typecheck` | suite, ruff y mypy estricto |
| `npm run bench` | microbenchmarks; con `--baseline` es puerta anti-regresión |
| `npm run build` | PyInstaller: `autonoma-agent/dist/Autonoma` / `Autonoma.exe` |
| `npm run clean` | borra el `.venv` para empezar de cero |
| `npm run update` | trae la última versión (git) y re-sincroniza el entorno; en un bundle portable dice qué bajar |

Si prefieres decirle qué Python usar (varios instalados, o uno viejo en el PATH):

```bash
npm start -- --python C:\\Python312\\python.exe
```

---

## 3) Sin Node: el lanzador del repo

```bat
Run-Autonoma.bat                    :: Windows, doble clic
Run-Autonoma.bat "resume mis notas" :: con instrucciones
```

```bash
./run-autonoma.sh "resume mis notas"   # Linux/macOS
python scripts/bootstrap.py run        # o directo, con lo que quieras pasarle
```

Los tres son la misma lógica: `scripts/bootstrap.py` (sólo biblioteca estándar de Python, sin
dependencias, sin `pip install` global). Comandos disponibles:
`setup · run · key · status · doctor · selftest · test · lint · format · types · bench · build · clean`
y flags `--python`, `--venv`, `--no-input`, `--dry-run`.

---

## 4) Instalación normal de Python (para desarrollar)

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e "./autonoma-agent[test,check]"     # agrega [keyboard] para el hotkey global
autonoma "tu instrucción"
```

---

## Qué pasa cuando dices «listo»

- El agente vive en una sola ventana: escribes, él propone acciones sobre tus archivos o la web, y
  **cada operación local pide aprobación explícita escribiendo `SI`**.
- Tus notas quedan en `knowledge_base/` y el registro local en `logs/autonoma.jsonl`
- La conversación se guarda en `sessions/<id>.jsonl` (un turno por línea, permisos 0600) y la salida de los procesos en
  segundo plano en `jobs/`. `--no-persist` evita que se escriba nada y `/forget` borra una sesión concreta
  (una línea JSON por evento, con `trace_id` para atar todo un turno).
- `Ctrl+C` cancela lo que esté haciendo; `--global-hotkey` añade la tecla `P` como botón de pánico.
- `/help` lista los comandos (`/kb`, `/status`, `/key`, `/brave`, `/exit`).
- Nada sale de tu equipo salvo las llamadas a NoTrack.ai y a la búsqueda que tú pidas.
- Ejecutar comandos del sistema está **apagado por defecto**: `--allow-commands` lo enciende y
  corre con tus permisos, sin sandbox. Léelo antes de activarlo
  ([límites](autonoma-agent/README.md#seguridad-y-privacidad)).

## Dónde vive tu información

| Qué | Dónde |
| --- | --- |
| Tus notas (`knowledge_base/`), el registro (`logs/autonoma.jsonl`), las sesiones (`sessions/`) y la salida de los trabajos (`jobs/`) | en la raíz de datos activa: junto al `.exe` (modo portable) o en `autonoma-agent/` (checkout); con permisos privados y fuera de git |
| La clave | `autonoma-agent/.env` (o `.env` al lado del `.exe`); nunca en la línea de órdenes ni en el registro |
| Cambiar la raíz para una sesión | `--data-dir <carpeta>` (o la variable `AUTONOMA_HOME`) |

`npm run update` es el único comando que toca el código: en un checkout hace `fetch` + `merge --ff-only` y re-instala el
entorno; si hay cambios locales sin commitear se niega a tocar nada (pasa `--allow-dirty` sólo si sabes qué haces). En un
bundle portable no se reescribe a sí mismo: dice qué ZIP bajar y deja `.env`, `knowledge_base/`, `sessions/` y `logs/`
intactos.

`npm run status` (o `autonoma --doctor --json`) te enseña exactamente qué raíz está usando y
de dónde viene (`flag`, `env`, `frozen_executable`, `checkout` o `user_config`).

## Si algo no arranca

| Síntoma | Solución |
| --- | --- |
| `necesito Python 3.10+ y no lo encontré` | instálalo (<https://www.python.org/downloads/>, marca *Add python.exe to PATH*) o pasa `--python <ruta>`; o usa el `.exe` del punto 1 |
| `Fallo instalando el paquete` con algo de PyPI | red corporativa bloqueando PyPI: usa el ejecutable portable |
| SmartScreen bloquea el `.exe` | aviso por binario sin firmar; verifica el `.sha256` y ejecuta de todos modos |
| `pynput no instalado` / listener no disponible | es opcional; `Ctrl+C` hace el mismo trabajo |
| Doble clic y la ventana se cierra | el `.exe` espera un Enter al salir; si lo lanzas por script, pon `AUTONOMA_NO_PAUSE=1` |
| `autonoma: error: no hay TTY interactiva` | sin terminal no se aprueban operaciones locales (por diseño); corre `--doctor --json` para ver el detalle |

Diagnóstico de una, siempre: `npm run doctor` o `.\Autonoma.exe --doctor --json`.
