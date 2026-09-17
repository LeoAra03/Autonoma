# Auditoría técnica y UX — Autonoma

> Este informe conserva la evaluación de la primera etapa. Consulta [PROGRESO.md](PROGRESO.md) para los cambios y métricas posteriores de la versión 1.1.

Fecha: 17 de septiembre de 2026. Alcance: código del ZIP original y versión corregida en `autonoma-agent/`. Rama de trabajo: `arena/01a0adf9-autonoma`.

## Resumen ejecutivo

El proyecto original es un prototipo de agente Python para terminal, con una separación inicial razonable por módulos, pero con riesgos altos al ejecutar acciones locales decididas por un modelo. Se aplicaron controles de aprobación humana, correcciones de procesos y configuración, defensas de red y mejoras de accesibilidad, acompañados por 38 pruebas locales satisfactorias. Todavía no corresponde calificarlo 10/10 ni certificarlo para producción: faltan aislamiento real, más cobertura, validación multiplataforma y evaluación con usuarios.

## Método y límites

- Inspección de los siete módulos originales, cliente HTTP, esquema de herramientas, scripts de empaquetado, documentación y cuatro pruebas originales.
- Extracción del código para permitir revisión, modificaciones y CI; el ZIP permanece intacto como referencia, no como distribución actualizada.
- Verificación local en Linux, Python 3.11.2: 38 pruebas offline y compilación del paquete; comprobación de `--help`.
- Cobertura medida con pytest-cov: **40% de líneas** (1540 sentencias, 929 sin ejecutar). Tener pruebas exitosas no demuestra ausencia de vulnerabilidades.
- Sin claves reales, sin solicitudes a NoTrack/Brave, sin navegador gráfico ni pruebas de usuario. La evaluación UX se basa en código de terminal, no screenshots ni observación de uso.
- No hay frontend web ni base de datos SQL: no se observó una superficie propia de SQLi o XSS de navegador. Los riesgos relevantes son ejecución local, exposición de archivos, SSRF, prompt injection y presentación de texto no confiable en terminal.

## Puntos críticos de fallo

Referencias por archivo y símbolo, para evitar números de línea obsoletos tras las correcciones.

| Prioridad | Evidencia original | Consecuencia | Acción y estado |
| --- | --- | --- | --- |
| Crítica | `agent.py:Agent._dispatch` → `filesystem.py:run_command`, `shell=True`, sin confirmación externa | El LLM podía ejecutar comandos con los permisos del usuario; una instrucción web maliciosa podía influir en esa decisión | Aprobación independiente por operación local y denegación por defecto. **Mitigado**, no aislado: shell aprobado sigue siendo poderoso |
| Alta | `filesystem.py:_assert_writable` permitía `force` + coincidencia textual | Mencionar una ruta no equivale a autorizar una modificación; la política era eludible | Eliminada la excepción de rutas protegidas para CRUD |
| Alta | `filesystem.py:run_command` esperaba salida del proceso antes de consumir dos PIPE | Al llenar stdout/stderr, el hijo se bloqueaba y acababa en timeout aunque el trabajo fuera válido | Lectores concurrentes, colas acotadas y reloj monotónico; prueba con 200 000 caracteres por canal |
| Alta | `search_engine.py:read_note` aceptaba cualquier archivo existente, incluso fuera de las notas | Lectura de secretos mediante una herramienta aparentemente limitada a conocimiento | Confinamiento por ruta resuelta; rechazo de traversal y symlinks fuera del directorio |
| Alta | `search_engine.py:fetch_url` aceptaba URLs arbitrarias y seguía redirecciones | Acceso a servicios locales o metadatos de infraestructura desde el agente | Solo HTTP(S) público y puertos estándar; sin credenciales ni redirecciones. **Mitigación parcial**: DNS rebinding y navegador requieren control de egreso |
| Alta | `agent.py:_run_inner` introducía notas web en un mensaje `system` | Contenido persistido no confiable adquiría prioridad de instrucciones | Contexto separado como datos en mensaje de usuario e instrucción explícita de no obedecer notas; no es una solución completa a prompt injection |
| Media | `cli.py:run_prompt/repl` síncronos; documentación recomendaba `/panic` cuando el listener fallaba | El comando de cancelación no se podía escribir durante la ejecución; Ctrl+C no se gestionaba en el turno | Ctrl+C gestionado con limpieza y retorno al prompt; ayuda corregida, cancelación cooperativa |
| Media | `config.py:load_settings` solo cargaba un ajuste numérico, mutaba el entorno y toleraba JSON corrupto | Ajustes ignorados, recarga obsoleta y fallos silenciosos | Carga de todos los ajustes numéricos con límites finitos; JSON inválido produce error; entorno sin mutación por lectura |
| Media | `config.py:_write_dotenv_file` reescribía cuatro claves sin permisos explícitos | Pérdida de ajustes personalizados, exposición local y escritura parcial | Preservación de claves, validación contra saltos de línea, reemplazo atómico, 0600 POSIX y entorno actualizado después de persistir |
| Media | `config.py:project_root`, combinado con binario onefile | `.env` junto al ejecutable no coincidía con ruta temporal del paquete | Ruta basada en `sys.executable` cuando está congelado; prueba simulada, build real pendiente |
| Media | `search_engine.py:fetch_url` sin `raise_for_status`, cuerpo completo antes de truncar | Una página 404 podía tratarse como información; uso excesivo de memoria | Estado HTTP validado y lectura en streaming con máximo de 2 MB para texto |
| Media | `Agent.history` crecía sin límite aunque se enviaba solo una ventana | Consumo de memoria creciente en sesiones largas | Ventana también aplicada a almacenamiento; prueba con 20 turnos |
| Media | `Session.rebuild_agent` registraba nuevos callbacks sin quitar anteriores | Retención de clientes antiguos y limpiezas duplicadas | Desregistro de callbacks y cierre completo del buscador antes de reconstruir |
| Baja | `save_note/save_findings` usaban timestamp a segundos | Dos notas homónimas en el mismo segundo se sobrescribían | Timestamp con microsegundos; queda pendiente exclusividad atómica para concurrencia extrema |
| Media | `repl(once=...)` devolvía 0 incluso ante error de clave/API | Automatizaciones interpretaban fallos como éxito | Propagación de resultado no cero de `run_prompt` |

## 1. Arquitectura y diseño del código

**Fortalezas:** módulos reconocibles, dataclasses para configuración/resultados, dependencia de recursos inyectada en `Agent`, cancelación central y nombres de dominio claros. No es un proyecto enteramente spaghetti ni necesita una reescritura de framework.

**Deuda:** `SearchEngine` combina tres transportes, scraping, almacenamiento y recuperación; `FileSystemManager` mezcla política, E/S y procesos; el despachador y el esquema duplican el contrato de herramientas. Se respeta parcialmente responsabilidad única e inversión de dependencias, pero añadir herramientas exige modificar múltiples bloques y los clientes concretos limitan sustitución/pruebas de contrato.

**Aplicado:** código extraído y navegable, política de URL separada, callback de aprobación inyectable, CI offline y documentación de límites. **Pendiente:** interfaces pequeñas para buscadores y repositorio de notas, registro declarativo de herramientas con validación tipada, cierre de recursos encapsulado y paquete instalable con extras/lock de dependencias. Evitar una abstracción por cada función: separar donde existan cambios o pruebas independientes.

## 2. Calidad de programación y seguridad

Los nombres suelen ser comprensibles (`knowledge_dir`, `PanicController`, `ResearchBundle`); abreviaturas como `s`, `fn` o `n` no son el problema principal. Son más importantes los contratos permissivos: convertir argumentos con `str()` o `bool()` oculta tipos erróneos, y `tool_calls`/respuestas HTTP no tienen validación integral de esquema.

Los `try/except` son razonables cuando permiten limpieza y fallback, pero varios `except Exception` silencian eventos o errores de UI. Deben distinguirse cancelación, validación, transporte y fallos internos; no conviene reintentar herramientas destructivas automáticamente. Se corrigieron límites numéricos, `force` booleano estricto y fallos HTTP en extracción; queda endurecer contratos de NoTrack, streaming SSE y errores sin datos sensibles.

Persisten límites importantes: comandos aprobados heredan permisos/entorno; las operaciones recursivas, symlinks y carreras de archivos no están contenidas por una sandbox; la política DNS tiene TOCTOU; Playwright puede cargar subrecursos no cubiertos por `fetch_url`. No ejecutar este agente con privilegios elevados ni presentarlo como aislado. La rotación de logs limita disco, pero no implementa una política completa de redacción/retención.

## 3. Usabilidad y UX

La CLI tiene comandos descubribles y mensajes en español, entrada oculta de claves y un estado consultable. Antes faltaban un fallback fiable de cancelación, señales estáticas de progreso y explicaciones precisas de lo que protege el agente; el spinner se detenía tras la primera herramienta sin progreso posterior.

La investigación realiza búsquedas y descargas secuenciales, con hasta tres mecanismos de fallback; puede sentirse lenta y consumir varias llamadas por turno. No hay streaming visible de respuesta final ni caché explícita con caducidad. Se limitaron páginas, salida de procesos y cuerpos HTTP, pero no se han medido percentiles de latencia reales.

### Cinco sugerencias concretas UX/UI

1. **Permisos comprensibles:** mostrar operación y argumentos completos y pedir confirmación positiva; aplicado. Pendiente: resumen de impacto y diff para sobrescrituras, sin esconder argumentos relevantes.
2. **Cancelación fiable y honesta:** Ctrl+C durante el turno y ayuda que explique la limitación de `/panic`; aplicado. Pendiente: pruebas reales de cancelación en navegador y procesos de Windows.
3. **Progreso sin animación obligatoria:** modo simple y progreso estático por paso, también después de la primera herramienta; aplicado. Pendiente: indicador de backend efectivo y duración por fase.
4. **Errores accionables:** ajustes inválidos y falta de clave con mensaje y código de salida no cero; aplicado. Pendiente: guía específica para 401, 429 y red caída, sin exponer cuerpos sensibles.
5. **Control del ruido:** salida compacta y sin color, sin ocultar la aprobación de operaciones; aplicado. Pendiente: búsqueda/paginación interactiva de notas y prueba con lector de pantalla.

## 4. Dinamismo y experiencia de usuario

El flujo sigue siendo síncrono: prompt → razonamiento → herramientas → resumen. Se mantuvo para no introducir carreras de historia, cancelación y confirmación al mismo tiempo; la mejora prioritaria fue hacerlo explícito y seguro, no añadir animaciones.

Aplicado: continuidad de mensajes de estado tras el spinner, errores de ejecución observables, cancelación al turno y recursos liberados al reconstruir. Pendiente: streaming compatible con tool calls, caché web con TTL, concurrencia limitada de descargas y mediciones de latencia/cancelación. El botón global P tiene riesgo de activación accidental al escribir en otra ventana; cambiarlo requiere validación multiplataforma, no solo sustituir un carácter.

## 5. Comodidad y personalización

Originalmente había colores fijos y ajustes técnicos parcialmente ignorados; no preferencias de interacción. Se implementaron tres opciones de bajo acoplamiento:

- **Presentación simple/sin color:** `--plain`, `AUTONOMA_PLAIN=1`, `NO_COLOR`.
- **Movimiento reducido:** `--reduced-motion`, `AUTONOMA_REDUCED_MOTION=1`.
- **Detalle compacto:** `--quiet`, `AUTONOMA_QUIET=1`.

Se pueden conservar en la configuración del shell; no se añadió un panel de preferencias ni se promete persistencia automática de flags. La consola Rich ya no interpreta por defecto cadenas arbitrarias como markup; las respuestas siguen renderizando Markdown intencionalmente. Queda validar escapes de control, contraste y lectura de paneles con tecnologías asistivas; WCAG web no se aplica directamente a esta CLI.

## Código ejemplo: autorización fuera del modelo

Antes, `_dispatch` enviaba el comando al sistema directamente. La corrección implementada es una frontera de aprobación dentro del agente:

```python
if name in local_tools:
    if self.approve is None or not self.approve(name, dict(args)):
        return "ERROR: operación local denegada; requiere aprobación humana."
    self.panic.check()
```

La CLI muestra JSON completo de argumentos y exige `SI` desde una terminal interactiva. Un texto del modelo como «el usuario autorizó» no activa este callback; la aprobación no sustituye una sandbox del SO.

## Plan para aspirar a 10/10 y aplicación

No se define 10/10 como «cero bugs garantizados», sino como cumplimiento verificable de criterios de producción para un alcance declarado.

| Fase | Trabajo y criterio de aceptación | Estado |
| --- | --- | --- |
| P0: repositorio verificable | Código fuera del ZIP, guía de ejecución, exclusión de secretos y CI offline | Aplicado; workflow creado, ejecución remota aún no observada |
| P0: acciones locales | Denegación sin aprobación, confirmación por acción, sin override textual de rutas protegidas, pruebas negativas | Aplicado en Agent/CLI; aislamiento de bajo nivel pendiente |
| P0: estabilidad | Sin deadlock por pipes, salida acotada, timeout finito, limpieza y pruebas de carga local | Aplicado y probado en Linux; procesos descendientes hostiles y Windows pendientes |
| P1: datos/red/config | Notas confinadas, validación HTTP, límites, carga consistente y persistencia atómica de claves | Aplicado con límites documentados; firewall DNS/Playwright y ACL Windows pendientes |
| P1: UX accesible | Tres preferencias, cancelación fallback, errores observables y ayuda coherente | Aplicado; pruebas con usuarios/lectores pendientes |
| P2: arquitectura | Separar backend de búsqueda, notas y procesos; registro tipado de herramientas sin duplicación; contratos API | Pendiente; aceptar cuando cambiar un backend no requiera cambiar UI/agente y contratos tengan pruebas |
| P2: aislamiento real | Contenedor/VM sin privilegios, directorio permitido, egreso filtrado, política para comandos/secretos | Pendiente; requiere decisión de plataforma y pruebas adversarias de escape, rebinding y exfiltración |
| P2: calidad de distribución | Dependencias bloqueadas y auditadas, ≥90% líneas y ≥85% ramas en módulos críticos, integración por SO | Pendiente; cobertura actual global 40%, sin auditoría de CVE ni builds reales |
| P3: experiencia medida | Streaming/caché, latencias por fase, cancelación medida y tareas observadas con usuarios | Pendiente; definir presupuesto de latencia con proveedor y lograr ≥90% de éxito en tareas de referencia |
| P3: accesibilidad/privacidad | Validación con lector de pantalla y teclado, contraste, almacén de claves del SO y retención configurable | Pendiente; no certificar antes de pruebas y revisión externa |

Se aplicó la primera etapa verificable P0/P1 dentro de esta sesión; **el plan completo hacia 10/10 no está finalizado**. Las siguientes fases necesitan decisiones de producto, entorno de despliegue y evidencias que no es honesto inventar.

## Veredicto final

Escala orientativa de madurez, no certificación. El valor posterior solo reconoce cambios revisados y comprobaciones disponibles.

| Categoría | Original | Tras correcciones | Principal barrera para 10 |
| --- | ---: | ---: | --- |
| Arquitectura y diseño | 5/10 | 6/10 | Separación de responsabilidades, contratos y distribución reproducible |
| Calidad y seguridad | 3/10 | 6/10 | Sandbox, cobertura y validación integral de protocolos/errores |
| Usabilidad y UX | 5/10 | 7/10 | Evaluación de tareas con usuarios, permisos por diff y errores contextualizados |
| Dinamismo e interacción | 4/10 | 6/10 | Streaming, latencia medida y cancelación multiplataforma |
| Comodidad y personalización | 3/10 | 7/10 | Accesibilidad validada, preferencias guiadas y privacidad configurable |

**Resultado:** base significativamente más segura y verificable para continuar el desarrollo local; todavía no lista para ejecutar entradas hostiles sin aislamiento externo.

## Reproducir las verificaciones

Desde la raíz:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r autonoma-agent/requirements-test.txt
.venv/bin/python -m pytest autonoma-agent/tests -q
.venv/bin/python -m compileall -q autonoma-agent/autonoma
# Cobertura opcional
.venv/bin/python -m pip install pytest-cov
.venv/bin/python -m pytest autonoma-agent/tests --cov=autonoma --cov-report=term-missing
```

En Windows cambia el ejecutable a `.venv\\Scripts\\python.exe`; las pruebas de procesos usan quoting POSIX y necesitan adaptación antes de habilitar una matriz Windows. No se realizaron commit, push ni PR. La sesión está fijada a `arena/01a0adf9-autonoma` y no permite renombrarla a `Arena.ai/correccion`.
