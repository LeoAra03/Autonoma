# Perfil de producción: Windows con acceso al equipo

## Decisión del usuario y límites

Se prioriza Windows de escritorio con acceso al host completo. Esto **no es un perfil aislado**:
los comandos aprobados pueden acceder a archivos y red con los permisos efectivos de la cuenta.
La selección del perfil no constituye aprobación anticipada de operaciones destructivas ni
permiso para elevar privilegios. Se mantiene aprobación humana por operación y shell opt-in.

No se implementa elevación automática ni solicitud automática de UAC. Se recomienda una terminal
normal, no «Ejecutar como administrador». Las protecciones CRUD de rutas del SO siguen activas;
un shell aprobado puede eludirlas. Malware, instrucciones hostiles o procesos concurrentes no
quedan contenidos por estas validaciones.

## Qué se implementó en esta etapa

- `--doctor` y `--doctor --json`: diagnóstico offline de configuración, política HTTPS, directorios
  escribibles, elevación, disponibilidad de TTY y dependencia opcional del listener. No muestra
  claves ni comprueba su validez. Solo comprueba presencia, no inicia el listener.
- `--data-dir`: elección explícita del directorio de datos/configuración antes de inicializar la app.
- `--global-hotkey`: P global pasa a ser **opt-in**. Ctrl+C es el mecanismo de terminal por defecto;
  evita que escribir una P en otra aplicación detenga la tarea inesperadamente por defecto.
- Política compartida para symlinks y reparse points, incluidos junctions NTFS. Escrituras y
  operaciones destructivas por estas rutas se rechazan; el preview no las sigue y las copias
  recursivas las detectan antes de descender.
- Validación Windows de dispositivos reservados, rutas de dispositivo, alternate data streams,
  rutas relativas a unidad y componentes con punto/espacio final. Es conservadora: puede rechazar
  ubicaciones legítimas redirigidas, por ejemplo ciertas carpetas administradas por OneDrive.
- La interfaz y el contexto del agente identifican **cmd.exe** como shell de Windows. Ejecutar
  PowerShell requiere invocarlo explícitamente; no se promete interpretar sintaxis PowerShell
  directamente en cmd.exe.
- Limpieza del listener y recursos registrados si falla la inicialización de sesión.
- CI adicional en Windows: prueba nativa de junction, build PyInstaller, smoke tests del `.exe`
  (`--version`, diagnóstico JSON) y artefacto **sin firma**, acompañado de SHA-256. UPX desactivado.

La detección previa de reparse points no cierra carreras TOCTOU; los hardlinks y procesos hostiles
siguen requiriendo controles adicionales. Un checksum detecta cambios respecto al archivo generado,
pero no acredita identidad del editor ni sustituye una firma Authenticode.

## Instalación de desarrollo en Windows (PowerShell)

Desde la raíz del checkout, con Python 3.12 instalado:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install ".\autonoma-agent[test,build,keyboard]"
.\.venv\Scripts\python.exe -m pytest .\autonoma-agent\tests -q
$env:AUTONOMA_HOME = Join-Path $env:LOCALAPPDATA 'Autonoma'
.\.venv\Scripts\autonoma.exe --doctor
.\.venv\Scripts\autonoma.exe --allow-commands
```

Configura la clave únicamente mediante `/key` en la aplicación o editando el archivo local `.env`.
No la pases como argumento de PowerShell ni la publiques en capturas, diagnósticos o tickets.

Los extras Windows todavía no tienen un lock auditado propio; no uses el lock Linux/Python 3.11
como si fuera universal. El build actual no equivale a una distribución reproducible y firmada.

## Compilar y comprobar el ejecutable

```powershell
Push-Location .\autonoma-agent
..\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean .\autonoma.spec
Pop-Location
$env:AUTONOMA_HOME = Join-Path $env:LOCALAPPDATA 'Autonoma'
.\autonoma-agent\dist\Autonoma.exe --doctor --json
.\autonoma-agent\dist\Autonoma.exe --allow-commands
```

Alternativa sin variable de entorno:

```powershell
.\autonoma-agent\dist\Autonoma.exe --data-dir "$env:LOCALAPPDATA\Autonoma" --doctor
```

`--data-dir` prevalece sobre `AUTONOMA_HOME` durante esa ejecución; no guarda esa preferencia en
el sistema. Sin override, el ejecutable sigue usando su directorio como ubicación de datos por
compatibilidad: podría no ser escribible si se instala en Program Files. Usa el override mostrado.

El diagnóstico crea los directorios configurados si faltan y utiliza un archivo temporal que se
elimina para comprobar escritura. `local_checks_passed=true`/exit 0 significa **sin errores en las
comprobaciones locales implementadas**, no certificación de producción: puede haber advertencias
por clave ausente, falta de TTY o elevación. `network_tested=false` es intencional.

## Evidencia y autoevaluación de esta entrega

Comprobado en Linux/Python 3.11.2 (estado de la versión 2.0.0):

- **351 pruebas pasan; 1 se omite** porque necesita Windows/NTFS real (junction de `mklink /J`).
- Ruff con selección curada (`E, W, F, I, UP, B, C4, PERF, SIM, RET, PIE, RUF, ANN, TRY, EM, BLE, ARG, SLF, PLC, PLE, PLW0603, PLR0124, PLR1714, PLR1722, PTH, S, DTZ, INP, PT, FBT`),
  `mypy --strict` sin errores y compileall.
- Cobertura combinada líneas/ramas: **87,5%**, umbral exigido 80%.
- Puerta anti-regresión de rendimiento: `python scripts/bench.py --baseline b.json` (mínimo de
  5 corridas; falla si una métrica empeora más de 1,35×).
- El diagnóstico distingue explícitamente configuración local de disponibilidad del proveedor.

## Autoensayo del ejecutable portable

El bundle debe demostrarse a sí mismo sin credenciales ni red:

```powershell
autonoma-agent\dist\Autonoma.exe --selftest --json
```

Devuelve `ok`, `frozen`, `checks[]` (import de todos los módulos del paquete, raíz de datos
escribible, presencia de dependencias, `pynput` como advertencia opcional) y
`local_checks_passed`. `network_tested=false` es intencional: el autoensayo nunca llama a
proveedores. Los scripts `autonoma-agent/scripts/build_windows.ps1` (onefile; `-SkipSmoke` para omitir el ensayo) y
`build_portable.sh` (PyInstaller o zipapp como fallback) ejecutan ese autoensayo sobre el
artefacto antes de darlo por bueno y publican un `.sha256`. CI construye el `.exe` en la job
`windows-executable` y lo sube a un Release cuando se etique `v*`; el binario **no** está
firmado, así que SmartScreen advertirá la primera vez.

La prueba nativa omitida crea una junction real con `mklink /J` y verifica que no se modifique el
archivo destino a través de ella. Está incluida en CI Windows, pero **ese job no se ha ejecutado ni
observado en esta sesión**. Tampoco se ha construido aquí un `.exe` Windows ni probado un listener
gráfico. No se hizo push, commit ni PR.

## ¿Es 10/10?

**No hay evidencia suficiente para afirmarlo todavía.** Se corrigieron los problemas encontrados
al revisar esta etapa, pero el criterio de salida del perfil Windows sigue abierto:

| Requisito | Estado |
| --- | --- |
| Consentimiento por operación y sin elevación automática | Implementado; no es sandbox |
| Diagnóstico offline y mensajes de limitaciones | Implementado y probado localmente |
| Reparse points/junctions | Política y simulación probadas; NTFS real pendiente |
| Build y smoke test de ejecutable Windows | Automatizados en CI; ejecución pendiente |
| Cancelación de árbol de procesos Windows | Implementación previa; validación adversaria pendiente |
| Unicode, espacios en rutas y cmd/PowerShell reales | Cobertura parcial; validación nativa pendiente |
| Persistencia de secretos con DPAPI/Credential Manager | Pendiente; `.env` sigue siendo texto plano |
| Dependencias Windows fijadas/auditadas y firma Authenticode | Pendiente |
| Uso con cuenta estándar y elevada/UAC | Diagnóstico implementado; escenarios reales pendientes |
| NVDA, contraste, teclado y tareas observadas | Pendiente |
| Cobertura objetivo de componentes críticos | Pendiente; no se rebaja el objetivo para aprobar |

La sandbox deja de ser un requisito de este perfil, por decisión explícita del usuario, pero el
riesgo de acceso completo debe permanecer visible. La siguiente validación decisiva es ejecutar
los tests y el build nativos en Windows, antes de etiquetar un binario como listo para producción.
