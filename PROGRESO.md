# Autonoma 1.1 — segunda etapa de mejora

> Actualización posterior: el usuario eligió Windows con acceso al host. Ver [WINDOWS_PRODUCCION.md](WINDOWS_PRODUCCION.md) para el nuevo alcance y las pruebas de la tercera etapa.

17 de septiembre de 2026 · Rama `arena/01a0adf9-autonoma`

## Resultado ejecutivo

Se completó una segunda etapa centrada en contratos, seguridad por defecto, experiencia de confirmación y distribución verificable. La suite pasa de 38 a **114 pruebas offline**; la cobertura de líneas sube del 40% al **63,2%**. El proyecto todavía no merece 10/10: no cuenta con aislamiento del sistema operativo ni con validación real de proveedores, tecnologías asistivas y plataformas de escritorio.

Este documento actualiza la [auditoría inicial](AUDITORIA.md), que se conserva como registro histórico.

## Cambios aplicados

### Arquitectura

- `tool_contracts.py`: contrato compartido entre el esquema enviado al modelo y la validación local. Rechaza parámetros desconocidos, tipos incorrectos, JSON inválido, cadenas vacías para rutas/consultas, NUL, tamaños excesivos y números fuera de rango/no finitos. Un booleano ya no puede pasar como entero.
- `tool_registry.py`: registro de adaptadores separado del bucle de conversación; una prueba comprueba correspondencia exacta entre herramientas registradas y anunciadas.
- `knowledge.py`: almacenamiento independiente de notas, lecturas acotadas y creación exclusiva con nombres UUID para no sobrescribir notas homónimas. SearchEngine mantiene compatibilidad mediante herencia; la composición y la separación de backends web siguen pendientes.
- `presentation.py`: presentación de controles de terminal y previsualización de operaciones separadas de Rich/CLI.
- `pyproject.toml`: paquete instalable, comando `autonoma`, extras de teclado, navegador, build y pruebas. Se validó la construcción wheel/sdist y el entrypoint de la wheel instalada fuera del checkout.

### Seguridad y calidad

- **Shell deshabilitado por defecto**. `--allow-commands` lo habilita únicamente para esa sesión, conservando aprobación positiva por comando. Configuración y contenido generado por el modelo no pueden activarlo.
- Herramientas inválidas se rechazan antes de solicitar aprobación o ejecutarse; no se convierten silenciosamente sus argumentos.
- NoTrack exige HTTPS sin credenciales embebidas/query/fragmento, no sigue redirecciones y desactiva proxies implícitos del entorno. Los errores muestran recomendaciones para 401/403/429 sin devolver cuerpos remotos potencialmente sensibles.
- Respuesta JSON de NoTrack limitada a 2 MB, validación de choices/message/tool_calls, IDs duplicados y número máximo de herramientas por respuesta.
- Streaming SSE de texto con límites de tamaño, eventos completos, validación de deltas y detección de cierre faltante. Sigue siendo una API de texto: la CLI del agente aún no transmite respuestas con herramientas en streaming.
- Copiar/mover exige destino nuevo y rechaza destino dentro del origen. Copias recursivas con symlinks se bloquean; escritura/borrado mediante rutas simbólicas y borrado/movimiento de ancestros de rutas protegidas también se bloquean.
- Procesos admiten argv explícito para pruebas portables. La limpieza POSIX envía SIGKILL al grupo incluso si el padre ya salió; Windows intenta capturar descendientes antes de matar al padre. No se promete contención de procesos que escapen del grupo.
- Ubicación de datos escribible para la instalación como paquete y override `AUTONOMA_HOME`; se conserva compatibilidad con checkout y ejecutable congelado.

### UX y accesibilidad

- Confirmaciones muestran advertencias según el impacto: privacidad de lecturas, shell sin aislamiento y operaciones destructivas.
- Escritura muestra diff local acotado antes de confirmar; se avisa cuando es parcial o no puede calcularse. Los argumentos completos siguen visibles y el diff no escribe ni autoriza nada.
- La tecla P no se procesa como cancelación mientras se introduce la confirmación; Ctrl+C cancela ese diálogo. Fuera del diálogo, el riesgo de activación accidental del listener global sigue pendiente.
- Controles ESC, retorno de carro, bidi y otros caracteres de control se representan visiblemente en texto remoto, diffs, eventos y listados cubiertos, en vez de ejecutarse en la terminal. Se conserva Unicode legible.
- Se informa duración total del turno; es medición local, no un benchmark ni un objetivo de latencia garantizado.
- Se mantienen modos simple, movimiento reducido y compacto de la etapa anterior.

### Dependencias y automatización

- `requirements-lock.txt`: versiones fijadas y hashes para entorno base + pruebas en Linux/Python 3.11. Se instaló utilizando `--require-hashes`.
- Una alerta del escáner sobre la versión de pytest inicialmente resuelta se corrigió actualizando la restricción y regenerando el lock.
- La auditoría final del lock con `pip-audit --require-hashes` devolvió **«No known vulnerabilities found»**. Esto solo significa que el escáner no detectó vulnerabilidades conocidas en ese conjunto en el momento de la consulta; no cubre código propio ni extras opcionales.
- CI configurada para Linux/Windows y Python 3.10/3.12, más Linux/3.11 con lock, auditoría de dependencias, build y umbral de cobertura combinada del 60%. **Estos jobs remotos aún no han sido observados**; no equivalen a pruebas realizadas en Windows.
- Comprobación Ruff de errores F y compilación Python satisfactorias. No se afirma que haya type checking estricto o todos los estilos de lint habilitados.

## Evidencia local

Entorno: Linux, Python 3.11.2. Sin claves ni llamadas reales a NoTrack/Brave, sin interfaz gráfica.

| Comprobación | Resultado |
| --- | --- |
| Pruebas | 114 satisfactorias |
| Cobertura de líneas | 63,2% |
| Cobertura de ramas | 56,7% |
| Cobertura combinada líneas/ramas | 61,65%; supera umbral inicial de 60% |
| Contratos de herramientas, combinada | 100% |
| Cliente NoTrack, combinada | 88,0% |
| Almacenamiento de notas, combinada | 92,2% |
| Orquestador, combinada | 82,9% |
| Ruff `--select F` y compileall | Sin errores |
| Build wheel y sdist 1.1.0 | Correcto |
| Entry point de wheel instalada | `Autonoma 1.1.0` |
| Auditoría de dependencias del lock | Sin vulnerabilidades conocidas detectadas |

Las métricas de módulos son del conjunto local de pruebas, no garantías sobre su seguridad. Las pruebas de HTTP usan MockTransport; las de procesos lanzan procesos Python pequeños sin efectos externos deliberados.

## Cambios de comportamiento que requieren atención

1. Para ejecutar comandos se necesita `--allow-commands`; aun habilitado, el shell tiene los permisos de la cuenta y no está aislado.
2. Copias/movimientos ya no fusionan ni sobrescriben destinos existentes: el usuario debe elegir una ruta nueva explícita.
3. `fetch_url` ya no duplica contenido guardándolo automáticamente como nota; para persistirlo existe `save_knowledge`.
4. Argumentos que antes se convertían permisivamente ahora se rechazan. NoTrack con URL HTTP o proxy implícito deja de funcionar por diseño.
5. El lock suministrado no es universal: otros Python/SO y extras opcionales resuelven sus propias dependencias desde pyproject; necesitan fijación y auditoría propias antes de distribuir.

## Calificación provisional

| Categoría | Tras primera etapa | Ahora | Condición pendiente para aspirar a 10 |
| --- | ---: | ---: | --- |
| Arquitectura | 6 | 7 | Separación de backends, interfaces tipadas y ciclo de vida de recursos más simple |
| Calidad y seguridad | 6 | 7 | Sandbox/egreso real, cierre de carreras de archivos, más cobertura e integración real |
| Usabilidad | 7 | 7 | Validar tareas y comprensión de permisos con usuarios, no solo por inspección |
| Dinamismo | 6 | 6 | Streaming del agente, caché con TTL, concurrencia limitada y latencia medida con proveedor |
| Personalización/accesibilidad | 7 | 7 | Preferencias guiadas/persistentes y pruebas con lector de pantalla/contraste |

No se aumentan las notas UX solo por añadir funcionalidades sin observar su uso.

## Próximas puertas de aceptación hacia 10/10

- **Aislamiento:** decidir plataforma soportada para ejecutar comandos en contenedor/VM sin privilegios, con montajes y red mínimos; comprobar escapes, hardlinks, symlinks y procesos descendientes. La aprobación humana no sustituye esta capa.
- **Red:** controlar egreso por conexión real, incluida resolución DNS y subrecursos Playwright. La validación previa actual sigue expuesta a DNS rebinding/TOCTOU.
- **Calidad:** elevar progresivamente cobertura global y alcanzar al menos 90% de líneas/85% de ramas en componentes críticos, añadiendo casos de fallo y cancelación, no pruebas vacías para subir porcentajes.
- **Integraciones/plataformas:** observar CI remota, probar Windows/macOS, listener global, ejecutables y cuentas reales de proveedores en un entorno de pruebas; auditar/lockear extras.
- **Experiencia:** streaming compatible con herramientas sin ejecutarlas antes de terminar su validación; caché acotada con TTL y métricas por fase; pruebas observadas de tareas y cancelación.
- **Accesibilidad y privacidad:** validación con lector de pantalla, redacción sistemática de logs, almacén de claves del SO y ajustes de presentación persistentes mediante UI.

**Estado:** segunda etapa implementada y verificada localmente; plan completo hacia 10/10 todavía en curso. No se crearon commits, push ni PR.

## Reproducción

Desde la raíz, Python 3.11/Linux:

```bash
python -m venv .venv
.venv/bin/python -m pip install --require-hashes -r autonoma-agent/requirements-lock.txt
.venv/bin/python -m pytest autonoma-agent/tests --cov=autonoma --cov-branch --cov-fail-under=60
.venv/bin/python -m compileall -q autonoma-agent/autonoma
# Herramientas de mantenimiento, fuera del lock de aplicación/pruebas:
.venv/bin/python -m pip install ruff pip-audit build
.venv/bin/ruff check autonoma-agent/autonoma autonoma-agent/tests --select F
.venv/bin/pip-audit --require-hashes -r autonoma-agent/requirements-lock.txt
.venv/bin/python -m build autonoma-agent
```


---

## Tercera etapa — refactor enterprise (versión 2.0.0, 17-09-2026)

Informe completo: [ENTERPRISE_AUDIT_2026-09.md](ENTERPRISE_AUDIT_2026-09.md). Resumen:

| Comprobación | Resultado |
| --- | --- |
| Pruebas | 423 satisfactorias (1 omitida por requerir NTFS real) |
| Cobertura combinada líneas/ramas | 87,3% (umbral exigido 80%) |
| `mypy --strict` | 0 errores en los 22 módulos del paquete |
| `ruff check` (selección amplia) | 0 avisos |
| `validate_arguments` | 11,23 µs → 2,55 µs (4,4×) |
| `is_protected` con 60 raíces | 1597,86 µs → 24,49 µs (65×) |
| `tool_schemas()` | 9,01 µs y mutable → 0,11 µs e inmutable (`MappingProxyType` recursivo) |
| `research()` con 3 páginas | 159,3 ms secuencial → 59,9 ms en abanico (2,7×) |
| Uso de memoria por turno | estable (p95 25 MiB, `growth_ratio` 0,94× sobre 12 turnos) |

Cambios de comportamiento que conviene conocer:

1. `Settings` es inmutable; `run_command` devuelve `CommandResult` tipado.
2. Los errores del agente son `AutonomaError` con `ErrorCode` y `ExitCode`; la UI ya no
   deduce el fallo buscando substrings.
3. `/key` y `/brave` escriben en `.env` y aplican overrides locales: **el entorno del
   proceso no se modifica** (dejar de exportar `AUTONOMA_HOME` globalmente).
4. Cada recarga cierra los recursos del agente anterior (había acumulación de limpiezas
   huérfanas); `close()` es idempotente y `unregister_cleanup` coincide por igualdad.
5. Nuevos: `--selftest`, `/status` con métricas y percentiles, `logs/autonoma.jsonl` con
   `trace_id`, `autonoma/py.typed`, `scripts/bench.py` como puerta de regresión.
6. Empaquetado: `autonoma.spec` unificado (onefile en cualquier SO), `build_windows.ps1`,
   `build_portable.sh` con fallback zipapp, artefactos y Release en CI.
7. Distribución en un comando: `scripts/bootstrap.py` (npm start, `Run-Autonoma.bat`,
   `./run-autonoma.sh`) y `scripts/make_bundle.py` (ZIP portable con LEEME y `.sha256`);
   ver [INSTALL.md](INSTALL.md).
