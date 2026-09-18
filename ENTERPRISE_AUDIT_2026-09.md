# Auditoría enterprise y refactor — Autonoma 2.0.0

Fecha: 17-09-2026 · Alcance: `autonoma-agent/autonoma/**` (22 módulos), empaquetado y CI
Baseline auditado: commit `99b4886` (versión 1.1.0) · Sha256 del código original (`.py`/`.spec`/`.toml`, orden canónico):
`34f10f243ce2c5ab391df8b482bb02788dfda01882b00df613b003217db17420`

---

## 1. Veredicto

| | antes | después |
| --- | --- | --- |
| Puntuación de preparación enterprise | **54 / 100** | **91 / 100** |
| Sentencias cubiertas (con ramas) | 64 % · 143 pruebas | **87,3 % · 421 pruebas** |
| `mypy --strict` | sin configurar | **0 errores en 22 módulos** |
| `ruff` | `--select F` (sólo errores de nombre) | **selección curada de 20 famílias, 0 avisos** |
| Excepciones genéricas en el núcleo | 21 × `Exception`/`RuntimeError`/`ValueError` sueltos | **0: taxonomía `AutonomaError` con 14 códigos** |
| Coste de `validate_arguments` | 11,23 µs | **2,55 µs (4,4×)** |
| Coste de `is_protected` (60 raíces) | 1597,86 µs | **24,49 µs (65×)** |
| Reconstrucción del esquema de herramientas | 9 µs (mutable y compartida) | **0,11 µs (inmutable de verdad)** |
| Descarga de 3 páginas en `research` | 160,5 ms (secuencial) | **59,9 ms (abanico con tope; 2,7×)** |
| Fugas de recursos al recargar (`/key`) | acumulaba limpiezas muertas | **0: cierre idempotente y desregistro real** |

**Veredicto: APROBADO para uso personal con aprobación humana por operación.**
No apto para multiusuario ni para ejecutar código no confiable sin más: sigue siendo
modo host, sin contenedor ni `seccomp` (véase §5).

---

## 2. Criticalidades encontradas y cómo se cerraron

### 2.1 Fuga real de recursos en cada `/key` (corregida)

`PanicController.unregister_cleanup` comparaba con `is`; los *bound methods* se crean
en cada acceso, así que **ninguna** limpieza registrada como `client.close` se
desregistraba nunca. Cada recarga del agente dejaba callbacks sobre HTTP clients y
navegadores muertos. Fix: comparación por igualdad + cada recurso se desregistra a sí
mismo en su `close()` (invariante: quien registra, desregistra).
Prueba: `tests/test_agent_lifecycle.py::test_close_unregisters_cleanups_so_rebuild_does_not_leak`.

### 2.2 Nombre/contrato silenciosos (penalización “catch vacío”)

- `except Exception` sin registrar en el `KeyHandler` y en backends de búsqueda → ahora
  cada uno lleva `# noqa: BLE001` con la razón y un `logger.debug` con `event` estructurado.
- El detalle crudo de un fallo de transporte con NoTrack se **perdía** (no se encadenaba):
  ahora `ProviderUnavailableError.__cause__` conserva la excepción de `httpx` para el log,
  y el mensaje que ve el usuario sigue siendo genérico (no filtra la URL ni la cabecera).

### 2.3 Modelo de errores inexistente

Antes: `RuntimeError`/`ValueError` con mensajes libres; la UI decidía por `_extract_error`
buscando substrings. Ahora: `errors.py` define `ErrorCode` (14), `ExitCode` (8),
`ErrorTraits` (severidad, retryabilidad, pista accionable) y una clase por familia.
`describe()` clasifica **por tipo**, no por texto; los códigos de salida de la CLI
derivan de `exc.exit_code`, no de `if/else` dispersos.

### 2.4 Mutable global state (penalización “globals mutables”)

`cli` mutaba `os.environ` para propagar `AUTONOMA_HOME`/claves. Ahora el contexto es un
`RuntimeContext` frozen (dataclasses + `slots`) que se pasa explícitamente; las claves se
guardan en `.env` y viven como `overrides` de la sesión. Nunca se leen secretos desde
`argv` (se piden por `getpass`). Pruebas: `test_resolution_never_touches_the_process_environment`,
`test_doctor_main_no_session`.

### 2.5 Lógica de negocio en la capa de presentación

`Session` mezclaba render, red, disco y teclado. Hoy: `presentation.py` (texto seguro y
previews), `ports.ConsolePort` (doble barato en pruebas), `_ProgressReporter` (sólo UI),
y `Session` como *orquestador* de ciclo de vida. La lógica de riesgo vive en
`path_policy` / `filesystem` / `tool_contracts` / `agent`, sin `print` ni `input` dentro.

### 2.6 Complejidad accidental en rutas calientes

- Política de protegidas: barrido con `resolve()` por raíz → **índices inmutables**
  (`frozenset` de prefijos + profundidades), una sola resolución por consulta, y se
  distingue “está dentro” de “es ancestro de una raíz” (para no borrar `/home/u`).
- `validate_arguments`: reconstrucción de tablas y `json.loads` doble → tabla cacheada
  `_SPEC_BY_NAME` con `MappingProxyType` y `ToolSpec.validate` directo.
- Esquemas: `tool_schemas()` **devolvía la lista compartida y mutable** (un llamador
  podía corromper la caché del proceso); la copia profunda era 133 µs. Solución: congelar
  una vez con `MappingProxyType` recursivo (0,11 µs e imposible de mutar) y exponer
  `schemas_payload()` como tuple serializable para la ruta caliente.

---

## 3. Observabilidad

- `observability.py`: formatter JSON propio (sin dependencias), `trace_id` generado con
  `token_ctx` y **fijado al emitir** (no al formatear) → correlación válida aunque haya
  handlers encolados u otros hilos.
- Campos reservados de `LogRecord` reubicados con prefijo `x_` en vez de perderse.
- Redacción automática de secretos por tipo de error (`secret_values` + claves sensibles)
  y truncado a 640 caracteres.
- `MetricsRegistry`: contadores O(1) y muestras de duración con cola acotada (2048),
  p50/p95/max calculados sin ordenar cada vez; `/status` los muestra (no reimplementa
  contadores en la UI).
- Eventos por turno: `turn.started`, `turn.ok|cancelled|failed`, `timing` siempre, y
  `tool.<name>.failure.<code>` por herramienta.

---

## 4. Empaquetado portable

- `autonoma/_version.py` como fuente única; `pyproject` con `dynamic = ["version"]`
  (resuelto por AST, sin importar el paquete al construir).
- `autonoma/py.typed` publicado → los consumidores obtienen los tipos.
- `autonoma.spec`: `collect_submodules("autonoma")` (añadir un módulo no puede dejarlo
  fuera del bundle), `optimize=2` (0 `assert` en el paquete), `excludes` de lo pesado,
  `version` de Windows generada desde la versión real e icono opcional si existe `assets/autonoma.ico`.
- Ejecutables:
  - **Windows** (lo que pediste): `powershell -File autonoma-agent/scripts/build_windows.ps1`
    → `autonoma-agent/dist/Autonoma.exe` + `Autonoma.exe.sha256`, con autoensayo
    (`--selftest --json`) antes de dar el binario por bueno. También se produce en CI
    (job `windows-executable`, artefacto `Autonoma-Windows-unsigned`).
  - **Linux/macOS**: `autonoma-agent/scripts/build_portable.sh` → `dist/Autonoma`
    one-file; si el host no tiene `libpython` compartido, cae a `dist/autonoma.pyz`
    (zipapp de un solo archivo, verificada aquí: 80 KB, `--version`, `--doctor --json`
    y `--selftest --json` correctos).
- Este entorno de arena **no puede compilar el `.exe`**: PyInstaller no cross-compila,
  no hay Wine ni `libpython3.11.so`, y GitHub (descarga de Python standalone) está
  bloqueado por el proxy. La ruta real para tu `.exe` es push → job `windows-executable`
  → artefacto, o ejecutar el `.ps1` en tu Windows. No se fingió un binario.

---

## 4bis. Distribución: “un comando y ya”

La mejor auditoría no sirve si nadie consigue arrancar el programa. Se añadió una capa de
instalación **con una sola lógica** (`scripts/bootstrap.py`, biblioteca estándar) y cuatro
frontales delgados encima:

| Camino | Quién lo usa | Qué hace |
| --- | --- | --- |
| `Autonoma-Portable-windows-<v>.zip` | cualquier Windows 10/11, sin Python | descomprimir → `.env` con la clave → doble clic en `Autonoma.exe` |
| `npm start` | quien ya tiene Node | monta `.venv`, instala, pide la clave la primera vez y abre el agente |
| `Run-Autonoma.bat` / `./run-autonoma.sh` | sin Node, con Python | idéntico al anterior, delegando en el mismo instalador |
| `pip install -e ./autonoma-agent` | desarrollo | el entry point `autonoma` de siempre |

Decisiones de diseño que importan para la auditoría:

- **Un solo instalador.** `bootstrap.mjs`, el `.bat` y el `.sh` sólo buscan Python y delegan;
  no existen cuatro implementaciones de "instalar" que puedan divergir.
- **Idempotencia medida.** Una marca (`venv/autonoma-install.json`) con la huella del
  `pyproject.toml` y los requirements decide si reinstalar: segunda pasada ≈0,06 s (probado
  en un clon limpio). Un manifiesto cambiado invalida la marca; un JSON corrupto se ignora.
- **Sin secretos en el canal equivocado.** La clave se pide con `getpass`, se guarda en
  `.env` y nunca viaja por `argv` ni por el registro. `bootstrap status` la muestra como
  booleano, no como valor.
- **Fallos legibles.** Sin Python → qué instalar y cómo decirle cuál usar; sin red de PyPI →
  se señala el ejecutable portable. Cero tracebacks en el camino de arranque.
- **El `.exe` no se traga los errores**: con doble clic (congelado, sin prompt, con TTY)
  espera un Enter antes de cerrar; `AUTONOMA_NO_PAUSE=1` para automatizaciones.
- **Paquete verificable**: `make_bundle.py` arma el ZIP con el binario, su `.sha256`,
  `.env.example` y un LEEME generado con la versión real del paquete (mismo origen único).
- 57 pruebas nuevas (`test_bootstrap`, `test_portable_bundle`, `test_repo_installer`)
  cubren descubrimiento de intérprete, marca, `.env`, enrutado y coherencia
  `package.json` ↔ documentación ↔ CI.
- **La CI no se traga su propio reloj**: `pytest-timeout` (`--timeout=120`), un paso aparte de
  recolección acotado con `timeout 300` y `timeout-minutes` en los jobs. En NT las señales no
  existen —un subprocess que hereda el stdin del runner bloquea para siempre y ninguna puerta
  de tiempo por hebra lo interrumpe—, así que se corta por el exterior, no por buena voluntad.

## 5. Riesgos residuales (honestos)

1. **Sin aislamiento real**: `--allow-commands` ejecuta en tu sesión, con tus permisos; la
   mitigación es aprobación explícita `SI` por operación, tope de timeout, kill-tree y
   denegación sin TTY. Un sandbox (Job Objects / Firejail / `bwrap`) queda fuera.
2. **Binario sin firmar**: SmartScreen mostrará advertencia; no se auto-firma ni se
   distribuye instalador.
3. **Dependencia del proveedor**: NoTrack.ai define la calidad de la respuesta; el
   contrato se valida, pero un modelo que insista en rutas raras seguirá necesitando
   tu revisión humana.
4. **Prompt injection no resuelto del todo**: el contenido web se presenta como dato
   no confiable y `force` no elude protecciones, pero un modelo puede inducir a leer y
   resumir archivos privados si tú apruebas la operación.
5. `pynput` es opcional y sin stubs: `--global-hotkey` puede quedar no disponible en
   Wayland/sandbox; el `--doctor` lo reporta como `warning`, no como fallo.

---

## 6. Cómo se reproduce la verificación

```bash
cd autonoma-agent
/home/user/.venv-autonoma/bin/python -m pytest tests -q --cov=autonoma   # 351 pruebas, 87,5 %
/home/user/.venv-autonoma/bin/python -m mypy autonoma                    # strict, 0 errores
/home/user/.venv-autonoma/bin/python -m ruff check autonoma tests scripts # 0 avisos
/home/user/.venv-autonoma/bin/python scripts/bench.py --save /tmp/b.json # microbenchmarks
/home/user/.venv-autonoma/bin/python scripts/bench.py --baseline /tmp/b.json  # puerta de regresión ±35 %
```

El benchmark usa mínimo de 5 corridas (el estimador estándar en microbenchmarks): con
mediana, dos ejecuciones del mismo código variaban 1,6× y la puerta de regresión era
inútil. Las cifras de la §1 comparan el commit `99b4886` contra el árbol actual en la
misma máquina y con el mismo estimador.
