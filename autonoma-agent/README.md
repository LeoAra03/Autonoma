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
# Conversación con memoria: retomar la última sesión, o una concreta
python -m autonoma --resume
python -m autonoma --resume 2026-09-23-181530-0a3e
# Empezar con archivos ya leídos en el contexto (repetible)
python -m autonoma --attach informe.md --attach datos.csv "resume esto"
# Una pasada sin dejar rastro en disco, o con más pasos por turno
python -m autonoma --no-persist "…"
python -m autonoma --max-steps 60 "revisa todo el paquete y arregla lo que falle"
```

| Comando | Función |
| --- | --- |
| `/help` | Ayuda |
| `/key`, `/brave` | Entrada oculta de claves; no las escribas como argumento del comando |
| `/status` | Estado de claves sin mostrar fragmentos, modelo, notas y listener |
| `/kb` | Notas recientes |
| `/clear` | Borrar la conversación de memoria, no los archivos de notas |
| `/sessions` | Lista las sesiones guardadas (id, turnos, antigüedad) |
| `/resume [id]` | Cambiar la ventana de historial a otra sesión guardada |
| `/attach <ruta>` | Meter un archivo en el contexto de la sesión actual |
| `/forget [id]` | Borrar el historial visible **y** el archivo de esa sesión |
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

## Qué puede hacer

El modelo decide, pero sólo con las herramientas del contrato —todas tipadas y validadas antes de ejecutarse:

| Herramienta | Para qué |
| --- | --- |
| `web_search`, `fetch_url` | Buscar y leer la web. `fetch_url` devuelve una **ventana** con aviso de cuántos caracteres quedan: se continúa con `start_char` sin volver a descargar, y `save=true` la deja en `knowledge_base/`. |
| `read_file` | Lectura acotada; con `start_line`/`max_lines` recorre archivos grandes sin pedirlos enteros. |
| `edit_file` | Sustitución exacta de un fragmento único. Si hay más de una coincidencia y no pasas `all=true`, **rechaza** la edición y el archivo queda intacto. |
| `write_file`, `append_file`, `mkdir`, `copy_path`, `move_path`, `delete_path` | Escritos y movimientos, atómicos, dentro de la política de rutas. |
| `search_files` | `grep -n` sobre el árbol: salta binarios, symlinks y carpetas de artefactos. |
| `save_knowledge`, `read_knowledge`, `list_knowledge` | Memoria de trabajo reutilizable. |
| `run_command` | Shell local de un solo golpe, con `--allow-commands` y aprobación por comando. |
| `spawn_command`, `job_status`, `job_output`, `kill_job` | Lo que no termina en segundos (servidores, compilaciones, `npm test` largo): se lanza en segundo plano, la salida va a un archivo y se lee por trozos. |

Cualquier herramienta que escriba o lance procesos pide aprobación humana explícita (`SI`). No hay sandbox simulado: lo que el
agente puede hacer es lo que puedes hacer tú, y por eso se pregunta.

### Usar un modelo local, sin clave

Autonoma habla OpenAI-compatible, así que Ollama, LM Studio, llama.cpp o vLLM valen. En `config.json` o el entorno:

```bash
export NOTRACK_BASE_URL="http://127.0.0.1:11434/v1"   # Ollama
export NOTRACK_MODEL="qwen2.5:14b"
unset NOTRACK_API_KEY                                   # opcional en loopback
```

`http` (sin TLS) **sólo** se acepta si el host es de esta máquina (`localhost`, `127.0.0.1`, `::1`, `*.localhost`):
es la firma de un servidor propio, no un permiso para bajar la guardia. Cualquier otro endpoint sin HTTPS se rechaza,
igual que antes. El `--doctor` lo distingue: en loopback dice que no hace falta clave en lugar de pedirte una.

### Recuperar la cookie NDUS (opcional)

La cookie NDUS es **opcional**: sin ella Autonoma funciona igual. Si la usas, el problema
práctico es que vive en tu navegador, y en un entorno sin navegador (un sandbox, una VM, CI)
no hay de dónde sacarla. Tres caminos, del más rápido al más lento:

```bash
# 1. Pegarla: valor suelto, `ndus=...` o la cabecera Cookie completa del inspector.
autonoma --ndus "Cookie: ubid-acb=abc; ndus=TU-VALOR; session-token=xyz"
autonoma --ndus "TU-VALOR"                       # también vale el valor a secas
NDUS=TU-VALOR autonoma "tu prompt"               # o como variable de entorno

# 2. Importar un archivo: `cookies.txt` exportado por una extensión, o la cabecera pegada.
autonoma --ndus-file ~/Downloads/cookies.txt

# 3. Que la busque él: lee los almacenes de cookies de Firefox, Chrome, Edge, Brave…
autonoma --ndus-auto

# Dentro del REPL, lo mismo sin salir de la conversación:
/ndus              # estado y pegado oculto
/ndus auto         # buscar en los navegadores de esta máquina
/ndus archivo RUTA # importar de un cookies.txt
/ndus ver          # ver el valor enmascarado      /ndus borrar  quitarlo del txt personal
```

Se guarda en el txt personal (`credenciales.txt`, `mis_claves.txt`…), creándolo con permisos
0600 si no existe, y reemplazando sólo la línea de la cookie: el resto de tus claves queda
intacto. El valor **nunca se imprime**: en pantalla verás `VALO…7890 (23 caracteres)`.

Cómo copiarla del navegador: `F12` → Application (Chrome/Edge) o Storage (Firefox) →
Cookies → el dominio → valor de `ndus` (o copia la cabecera `Cookie:` completa, también sirve).

Detalles que importan:

- Firefox guarda la cookie **en claro** y se lee sin dependencias. Chromium/Chrome/Edge/Brave
  la cifran: en Windows se intenta con DPAPI y en macOS/Linux con el llavero del sistema;
  para el cifrado AES hace falta `pip install cryptography`. Si no se puede abrir, se dice
  que está cifrada (y cómo pegarla a mano) en lugar de fingir que no existía.
- Los almacenes del navegador sólo se **leen**, nunca se escriben, y se copian a un temporal
  antes de consultarlos para no chocar con el navegador abierto.
- En un entorno sin navegador `--ndus-auto` no falla en silencio: lista lo que miró y recuerda
  que puedes pegarla con `--ndus` o `NDUS=`.

## Modo "hasta que yo pare"

`--supervise` acotado está bien para una tanda concreta (12 turnos / 90 min por defecto, hasta 200
turnos / 24 h). Pero cuando lo que quieres es **dar una instrucción y que siga hasta que lo pares**,
el presupuesto es un estorbo. Con `--hasta-que-pare` desaparecen el tope de turnos y la ventana de
tiempo:

```bash
autonoma --supervise --hasta-que-pare \
  --allow-commands --approval-policy approval.json "tu objetivo"
```

El lazo sigue indefinidamente y **sólo se detiene** por una de estas:

| Motivo | Cómo |
| --- | --- |
| Trabajo terminado | el modelo responde `DONE:` |
| Tú lo paras | `Ctrl+C` en su terminal |
| Tú lo paras desde fuera | crea el archivo `supervisor/STOP` desde otra terminal |
| Freno: fallos seguidos | N turnos seguidos con el proveedor caído / clave rota / sin red |
| Freno: sin avance | N turnos seguidos sin línea de control (`NEXT:`/`DONE:`/`BLOCKED:`) |

Los dos frenos existen para que una avería externa no deje el lazo consumiendo sin avanzar: no son
un presupuesto, son una red. El de fallos se ajusta con `--tope-fallos N` (por defecto 10) y el de
turnos sin avance con `SUPERVISOR_MAX_UNPARSABLE` (por defecto 25 en este modo).

**Cómo convivir con él mientras corre** (todo en `supervisor/`, junto al informe):

- `turns.jsonl` — registro continuo: se reescribe en cada turno con el historial completo.
- `estado.json` — objetivo, turno y paso pendiente. Si se corta (Ctrl+C, cierre, reinicio), retómalo
  con `autonoma --supervise --continuar --hasta-que-pare`: sigue en el turno siguiente y recupera el
  paso donde iba, sin repetir lo hecho.
- `steer.txt` — escribe ahí una instrucción nueva y el lazo la consume en el siguiente turno
  **sin pararse**. Es como decirle «ahora revisa esto otro» en mitad del trabajo.
- `STOP` — créalo y el lazo termina limpio al empezar el siguiente turno.

## Arranque: en qué repositorio y con qué chat

Cuando el lazo va a trabajar solo, arranca preguntando **dónde** y **con qué conversación**:

```
$ autonoma --supervise --hasta-que-pare "deja la tanda verde"

¿Sobre qué repositorio trabajo?
  1  autonoma      /home/usuario/Autonoma             (actual)
  2  mi-web        /home/usuario/proyectos/mi-web     (git)
  3  api-vieja     /home/usuario/proyectos/api-vieja  (registro)
  4  otra ruta…
Elige [1]: 2

¿Chat nuevo o uno existente?
  1  chat nuevo
  2  20261012-010101-0001 · 12 turnos · hace 3 h · "deja la tanda verde…"
Elige [1]: 2

trabajando en /home/usuario/proyectos/mi-web · retomando 20261012-010101-0001
```

Y a partir de ahí ya no pregunta más: se queda trabajando (con `--supervise`, o sin topes con
`--hasta-que-pare`).

- **De dónde salen los repos**: la carpeta actual, tu registro `repos.json` (en la raíz de datos)
  y un escaneo de carpetas con `.git` bajo `AUTONOMA_REPOS` o `--repos-dir RUTA`. El escaneo es
  corto a propósito (dos niveles): es para una carpeta madre de proyectos, no para un disco entero.
- **Recordar rutas**: si eliges «otra ruta…» y dices que sí, se guarda en `repos.json` y aparece
  la próxima vez marcada como `(registro)`.
- **Saltarse la pregunta**: `--repo RUTA` trabaja ahí sin preguntar. `--elegir` la fuerza incluso
  sin terminal interactiva.
- **Elegir repo mueve el proceso** a esa carpeta (`chdir`): las rutas relativas del agente, los
  comandos que ejecuta y las raíces relativas de tu `approval.json` se refieren al repo elegido.
  Tus claves, sesiones e informes siguen en la raíz de datos (`--data-dir`), nunca dentro del repo:
  trabajar en un proyecto ajeno no debe dejar tu `credenciales.txt` en su carpeta.

## Verlo, dirigirlo y pararlo desde el celular

Sin instalar nada: el propio Autonoma sirve una página para que la abras desde el navegador del
teléfono. Ves por dónde va el lazo, le escribes una instrucción nueva y lo paras.

```bash
# sólo en este equipo (abre la URL que te imprime)
autonoma --supervise --hasta-que-pare --panel "tu objetivo"

# visible en tu wifi: ábrelo desde el celular con la URL que imprime (lleva el token)
autonoma --supervise --hasta-que-pare --panel 0.0.0.0 "tu objetivo"
```

Al arrancar imprime algo como `panel: http://192.168.1.5:8787/?token=…`. Esa dirección es la de tu
PC en tu red; ábrela en el celular estando en el mismo wifi. La página se refresca cada 10 s.

Lo que puedes hacer desde ahí es exactamente lo mismo que harías en el disco:

| En la página | En disco |
| --- | --- |
| Ver estado, turno, resumen y último avance | `supervisor/turns.jsonl` y `supervisor/estado.json` |
| Escribir una instrucción nueva | `supervisor/steer.txt` |
| Parar el lazo | `supervisor/STOP` |

Cómo está cerrado, y por qué:

- **Sólo local por defecto.** `--panel` a secas escucha en `127.0.0.1`: desde fuera de tu PC nadie
  ve nada. Para verlo desde el celular hay que escribirlo (`--panel 0.0.0.0`), y entonces se avisa
  de que eso es visible en la red.
- **Token nuevo en cada arranque.** Va en la URL y se exige en todas las rutas; sin él, 401.
- **No se registran las peticiones.** El token viaja en la URL, así que un log con la URL sería un
  log con la llave.
- **Nada se ejecuta desde la web.** Lo único que se puede escribir es `steer.txt` y lo único que se
  puede crear es `STOP`. No hay botón de comandos ni de shell.
- **Lo que muestra el modelo se escapa y los secretos conocidos se enmascaran**: esa página se mira
  desde un teléfono.

Un detalle de Windows: la primera vez que escuche en `0.0.0.0` el firewall preguntará si permitirlo.
Concédenlo **sólo en redes privadas**, nunca en públicas.

## Un solo comando: `execute`

```bash
./execute "deja la tanda verde"                      # lazo con el presupuesto por defecto
./execute "refactoriza el modulo de auth" --hasta-que-pare
./execute "refactoriza el modulo de auth" --hasta-que-pare --max 15
./execute "sigue donde ibamos" --hasta-que-pare --continuar
```

`execute` (y `execute.bat` en Windows) es el atajo del lazo supervisor: le pasas el enunciado entre
comillas y él hace el resto. Por dentro es `--supervise` más lo que le pidas.

| Flag | Qué hace |
| --- | --- |
| `--hasta-que-pare` | sin tope de turnos ni de tiempo: sigue hasta DONE, STOP o Ctrl+C |
| `--max N` | tope de iteraciones (atajo de `--turns N`) |
| `--continuar` | retoma la corrida anterior donde se cortó |
| `--panel 0.0.0.0` | panel para verlo, dirigirlo y pararlo desde el celular |
| `--sin-panel` | no abrir el panel |
| `--repo RUTA` | trabajar en ese repositorio sin que pregunte |

**Cómo se para, sin romper nada**: `Ctrl+C` termina al acabar el turno en curso (no a mitad);
`supervisor/STOP` desde otra terminal hace lo mismo; `supervisor/steer.txt` le da otra instrucción
**sin pararlo**. En los tres casos el trabajo queda registrado y se retoma con `--continuar`.

## Consumo: el proceso es liviano por diseño

Autonoma no corre modelos: habla con un proveedor por HTTP. Así que su gasto en tu
máquina es el de un script de consola, y se mantiene ahí con tres decisiones:

- **Nada pesado se importa al arrancar.** `httpx`, `bs4`/`lxml` y `rich`/`markdown-it`
  se cargan dentro de la función que los necesita. `import autonoma.cli` pasó de
  **315 ms y 40 MiB a 188 ms y 26 MiB**, y `autonoma --version` de 0,28 s y 38 MiB a 0,20 s y 26 MiB.
  Comandos como `--version`, `--help` o `--doctor` no cargan ninguna dependencia
  externa (hay una prueba que lo verifica en un intérprete limpio).
- **Nada espera dando vueltas.** No hay *busy-wait*: las esperas son `Event.wait` y
  `poll()` con intervalo, así que el proceso duerme en vez de consumir CPU.
- **Nada se paraleliza más que la máquina.** Las herramientas de sólo lectura pueden
  ir en paralelo, pero el pool se queda en el mínimo entre lo que pidió el modelo, lo
  que permite `MAX_PARALLEL_TOOL_CALLS` (4) y los núcleos que tengas.

Y una más, para cuando lo dejas solo mucho rato:

| Ajuste | Qué hace |
|--------|----------|
| `--pausa SEGUNDOS` | respiro entre turnos del lazo (0-3600). `--pausa 0` lo quita. |
| `--hasta-que-pare` | sin tope de turnos ni de tiempo; **le aplica una pausa de 2 s por defecto** para no tener el proceso y la red clavados durante horas. |
| `SUPERVISOR_IDLE_SECONDS` | lo mismo, en el entorno, `.env` o `config.json`. Si ya lo fijaste tú, eso manda y no se te impone ningún valor. |

Al terminar, el lazo dice cuánto pesó: `lazo: detenido por el archivo STOP · 3 turnos ·
pico 35 MiB`. Es la memoria máxima del proceso, medida con `psutil` (y sin él, con
`resource`).

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

Límites (todos configurables, ninguno inventado por el modelo): iteraciones por turno 1–200 (24), llamadas por vuelta
1–32 (16), ésas en paralelo 1–8 (4), mensajes en ventana 2–512 (64), timeout HTTP 1–300 s, timeout de comando 1–1800 s,
resultados 1–20, páginas precargadas 0–5, ventana de lectura 1 000–400 000 caracteres (80 000), tope de una página
100 000–16 000 000 bytes y texto devuelto por `fetch_url` 1 000–200 000 caracteres.
También se aceptan `MAX_TOOL_ITERATIONS`, `MAX_TOOL_CALLS_PER_TURN`, `MAX_PARALLEL_TOOL_CALLS`, `MAX_HISTORY_MESSAGES`,
`HTTP_TIMEOUT`, `COMMAND_TIMEOUT`, `SEARCH_RESULTS`, `FETCH_PAGES`, `READ_LIMIT_CHARS`, `FETCH_CHAR_LIMIT`,
`PAGE_MAX_BYTES`, `KNOWLEDGE_DIR`, `LOG_DIR`, `SESSIONS_DIR`, `JOBS_DIR`, `SUPERVISOR_DIR`, `NOTRACK_BASE_URL`,
`NOTRACK_MODEL`, `APPROVAL_POLICY`, `SYSTEM_PROMPT_FILE`, `SUPERVISOR_GOAL_FILE`, `SUPERVISOR_MAX_TURNS` (1-200),
`SUPERVISOR_MAX_MINUTES` (1-1440), `SUPERVISOR_IDLE_SECONDS`, `SUPERVISOR_STOP_ON_BLOCK`, `APPROVAL_POLICY_MAX_BYTES`
(1 KiB-1 MiB) y las dos API keys. Cuatro interruptores de seguridad tienen nombre propio: `SESSION_PERSIST` (por
defecto `true`; si se apaga, nada queda en disco), `ALLOW_PRIVATE_NETWORK` (por defecto `false`; si se enciende,
`fetch_url` y la investigación pueden dirigirse a la red local, lo que abre el paso a metadatos de nubes y servicios
internos — úsalo sólo a sabiendas), `ALLOW_SHELL_COMMANDS` (por defecto `false`; la política delegada autoriza `argv`
—programa y argumentos literales—, y sólo con este flag puede autorizar `command`, que es una línea que interpreta un
shell) y `SUPERVISOR_STOP_ON_BLOCK` (por defecto `true`; un `BLOCKED` corta el lazo en lugar de seguir reintentando).
Configuraciones numéricas o JSON inválidas producen un error explícito, incluida una `APPROVAL_POLICY` mal escrita:
una política rota nunca se ignora en silencio.

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
- `approval.py`: política de aprobación delegada (allow-list, presupuesto por turno y techo duro de comandos).
- `supervisor.py`: lazo `--supervise` — protocolo `NEXT`/`DONE`/`BLOCKED`, topes e interrupción humana.
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
