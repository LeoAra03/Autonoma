# Autonoma — agente local con aprobación de herramientas

CLI Python 3.10+ para conversar con NoTrack, investigar con Brave y operar sobre archivos.
No es una aplicación web. El perfil prioritario es [Windows con acceso al host](../WINDOWS_PRODUCCION.md). Consulta la [auditoría](../AUDITORIA.md) antes de habilitar operaciones locales.

## Instalación

**La vía corta** (no requiere leer nada): desde la raíz del repo, `npm start` —o doble clic en
`Run-Autonoma.bat` / `./run-autonoma.sh`— crea el entorno, instala el paquete, te pide la clave la
primera vez y abre el agente. El ejecutable portable (`Autonoma.exe`, sin Python en el destino) se
arma con `npm run build` o `scripts/build_windows.ps1`. Todo eso está detallado en
[../INSTALL.md](../INSTALL.md).

Desde este directorio, a mano:

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows: .venv\Scripts\activate
pip install ".[keyboard]"
cp .env.example .env
# Edita .env localmente; nunca lo publiques.
python -m autonoma
```

Para un artefacto portable (un solo archivo, sin Python instalado en el destino):

```powershell
# Windows → dist\Autonoma.exe
powershell -ExecutionPolicy Bypass -File autonoma-agent\scripts\build_windows.ps1
# (-SkipSmoke omite el autoensayo; el binario siempre se construye onefile)
```

```bash
# Linux/macOS → dist/Autonoma (o dist/autonoma.pyz si faltan herramientas de compilación)
autonoma-agent/scripts/build_portable.sh
```

Ambos scripts ejecutan `--selftest --json` sobre el artefacto y generan `.sha256`. El `.exe`
se construye y publica también desde CI (job `windows-executable`); no está firmado.

Playwright es opcional: `pip install ".[browser]"` y `playwright install chromium` instalan el soporte.
Sin Brave API, se intenta Playwright y después HTML. El paquete define las dependencias y extras en `pyproject.toml`; `requirements.txt` ofrece una
alternativa base y `requirements-test.txt` añade pruebas. Para Linux/Python 3.11 hay un entorno
fijado con hashes en `requirements-lock.txt` (incluye herramientas de prueba).

## Uso y accesibilidad

```bash
python -m autonoma --plain "Resume mis notas"
python -m autonoma --reduced-motion --quiet
python -m autonoma --help
python -m autonoma --doctor
python -m autonoma --doctor --json
python -m autonoma --selftest --json   # autoensayo del paquete/ejecutable, sin red ni claves
# Opcional: P global (puede cancelar al escribir en otras aplicaciones)
python -m autonoma --global-hotkey
# Solo si aceptas ejecutar shell sin aislamiento:
python -m autonoma --allow-commands
```

| Comando | Función |
| --- | --- |
| `/help` | Ayuda |
| `/key`, `/brave` | Entrada oculta de claves; no las escribas como argumento del comando |
| `/status` | Estado de claves sin mostrar fragmentos, modelo, notas y listener |
| `/kb` | Notas recientes |
| `/clear` | Borrar la conversación de memoria, no los archivos de notas |
| `/panic` | Activar cancelación entre tareas; no puede introducirse durante una tarea síncrona |
| `/quit` | Cerrar la sesión |

**Ctrl+C** cancela la tarea y permite volver al prompt. El listener global **P** está desactivado
por defecto: requiere `--global-hotkey` y que el sistema permita `pynput`; en terminales remotas usa Ctrl+C. P también puede activarse mientras
escribes en otra aplicación. La cancelación es cooperativa, no promete interrumpir instantáneamente
una operación de disco, DNS o navegador.

Tres preferencias sencillas, también persistibles en el entorno de tu shell:

1. Texto simple: `--plain` o `AUTONOMA_PLAIN=1`; `NO_COLOR=1` elimina color sin quitar paneles.
2. Movimiento reducido: `--reduced-motion` o `AUTONOMA_REDUCED_MOTION=1`.
3. Menos detalle: `--quiet` o `AUTONOMA_QUIET=1` oculta vistas previas de resultados, no aprobaciones.

No se ha certificado compatibilidad con lectores de pantalla: el modo simple facilita su evaluación.

## Seguridad y privacidad

- Las herramientas locales del **Agent** requieren confirmar sus argumentos completos escribiendo `SI`.
  Sin callback de aprobación o sin TTY, se deniegan. La confirmación no se deduce de una respuesta del LLM.
- El shell está **deshabilitado por defecto**. Solo `--allow-commands` lo habilita durante esa sesión;
  incluso entonces, cada comando requiere aprobación humana. No hay activación desde notas ni config.json.
- La confirmación de escritura incluye un diff de texto limitado; los argumentos completos siguen visibles.
- Una lectura aprobada puede enviar su contenido a NoTrack. No apruebes lectura de secretos.
- Escritura, borrado y movimiento CRUD sobre rutas protegidas no se habilitan con `force`.
  Se rechazan destinos simbólicos y operaciones destructivas sobre ancestros de rutas protegidas.
  Copiar/mover exige un destino nuevo: no se fusionan carpetas ni se sobrescribe implícitamente.
  Copias recursivas con symlinks se rechazan. Esto no resuelve carreras con otros procesos o hardlinks.
- **Esto no es un sandbox**: un comando de shell aprobado tiene los permisos de tu cuenta y puede
  eludir las protecciones CRUD. Las clases de bajo nivel son APIs internas, no una frontera de seguridad.
  Usa una cuenta sin privilegios y un contenedor/VM sin secretos ni montajes sensibles.
- `fetch_url` valida destinos públicos, bloquea redirecciones y limita texto a 2 MB.
  La validación DNS no evita por sí sola DNS rebinding y no cubre todos los subrecursos de Playwright.
  Para exposición a entradas hostiles hace falta un firewall/proxy de salida.
- Las notas se restringen a `knowledge_base`; no se cargan symlinks en el listado de contexto.
  Contenido web y notas siguen siendo datos no confiables.
- NoTrack exige HTTPS, no sigue redirecciones y no usa proxies implícitos del entorno; los errores
  HTTP no muestran cuerpos remotos. Esto puede requerir adaptación en redes corporativas con proxy.
- `.env` se reemplaza atómicamente con permisos POSIX 0600; configura ACL privadas en Windows.
  No hay cifrado ni almacén de credenciales del SO. Los logs rotan (2 MB, tres copias), pero pueden
  contener consultas, rutas y errores. No los publiques sin revisarlos.

## Configuración

Precedencia: entorno del proceso > `.env` > `config.json` > valores por defecto.
`config.json` es local y no debe contener secretos. Ejemplo:

```json
{
  "max_tool_iterations": 14,
  "http_timeout": 120,
  "command_timeout": 60,
  "search_results": 5,
  "fetch_pages": 3
}
```

Límites: pasos 1–50; timeouts de configuración 1–300 s; resultados 1–20; páginas 0–5.
También se aceptan `MAX_TOOL_ITERATIONS`, `HTTP_TIMEOUT`, `COMMAND_TIMEOUT`, `SEARCH_RESULTS`,
`FETCH_PAGES`, `KNOWLEDGE_DIR`, `LOG_DIR`, `NOTRACK_BASE_URL`, `NOTRACK_MODEL` y las dos API keys.
Configuraciones numéricas o JSON inválidas producen un error explícito.

En un checkout, notas/logs/configuración están en este directorio; en PyInstaller, junto al ejecutable.
`--data-dir RUTA` tiene prioridad sobre `AUTONOMA_HOME` durante la ejecución.
Instalado como paquete: `%APPDATA%/autonoma` en Windows o `$XDG_CONFIG_HOME/autonoma`
(`~/.config/autonoma` por defecto). `AUTONOMA_HOME` permite elegir explícitamente otro directorio.
Ese directorio debe ser escribible por el usuario. Se devuelve código no cero ante fallos de ejecución
visibles para la CLI; una respuesta del modelo que describa un fallo de herramienta no implica por sí
sola un código de salida no cero.

## Arquitectura y pruebas

- `agent.py`: orquestación y aprobación.
- `tool_contracts.py`: esquemas y validación estricta compartida.
- `tool_registry.py`: adaptadores/registro de ejecución, con correspondencia comprobada contra los esquemas.
- `knowledge.py`: almacenamiento de notas, archivos exclusivos con UUID y lecturas limitadas.
- `presentation.py`: diff local y representación visible de controles de terminal.
- `filesystem.py`: CRUD protegido y procesos con salida limitada.
- `network.py`: política de destinos web.
- `search_engine.py`: búsqueda y extracción; mantiene una fachada compatible heredando el almacén de notas.
- `notrack_client.py`: API compatible con OpenAI.
- `key_handler.py`: cancelación y recursos.
- `config.py`: configuración y persistencia local.
- `cli.py`: interacción y presentación.

```bash
pip install -r requirements-test.txt
python -m pytest tests -q
```

La CI está configurada para Linux y Windows con Python 3.10 y 3.12, más un job de dependencias
fijadas en Linux/3.11 y un umbral de cobertura combinada del 60%. La ejecución remota aún no se ha
observado; localmente se probaron Linux/3.11 y el build wheel/sdist. No valida cuentas reales,
listener gráfico ni ejecutables Windows/macOS. Para empaquetar: instala `pip install ".[build,keyboard]"`
y ejecuta `python -m PyInstaller --noconfirm --clean autonoma.spec` desde este directorio.
Playwright está excluido del binario. El empaquetado debe validarse en cada SO antes de distribuir.


## Entorno fijado y auditoría de dependencias

Desde la raíz del repositorio, con Python 3.11/Linux:

```bash
python -m pip install --require-hashes -r autonoma-agent/requirements-lock.txt
python -m pytest autonoma-agent/tests --cov=autonoma --cov-branch --cov-fail-under=60
```

El lock fue generado con pip-tools a partir del extra `test` de `pyproject.toml`.
No es un lock universal: Python 3.10/Windows y los extras browser/keyboard/build necesitan
resolución propia. La auditoría local del lock no encontró vulnerabilidades conocidas al
momento de ejecutarla; no constituye una garantía ni cubre los extras opcionales.

Cambios de compatibilidad de 1.1: shell opt-in; parámetros de herramientas inválidos se rechazan
sin conversiones silenciosas; copiar/mover no sobrescribe destinos existentes; fetch_url ya no
guarda automáticamente una segunda nota (usa save_knowledge); streaming de texto exige cierre SSE
completo y rechaza llamadas de herramientas. El agente todavía usa chat no streaming para herramientas.

La tercera etapa añade un job CI de build/smoke del ejecutable Windows, todavía no observado en
esta sesión. Consulta la guía Windows para interpretar el diagnóstico y los riesgos del perfil host.
