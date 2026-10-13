# Autonoma

Agente local de terminal Python. El código mantenible está en [`autonoma-agent/`](autonoma-agent/README.md).
El ZIP original se conserva únicamente como referencia histórica: **no contiene las correcciones**.

- [Perfil Windows/host: controles y validación de producción](WINDOWS_PRODUCCION.md)
- [Avance de la segunda etapa: pruebas y pendientes](PROGRESO.md)
- [Auditoría inicial, calificaciones y plan de mejora](AUDITORIA.md)
- [Auditoría enterprise y refactor 2.0.0 (rendimiento, errores, empaquetado)](ENTERPRISE_AUDIT_2026-09.md)
- [Instalación y arranque en un comando (`.exe`, `npm start`, doble clic)](INSTALL.md)
- [Instalación, uso y límites de seguridad](autonoma-agent/README.md)

## Arrancarlo en un comando

```bash
npm start                     # crea .venv, instala lo que falte y abre el agente
npm start -- "resume mis notas"
```

O sin Node: doble clic en `Run-Autonoma.bat` (Windows), `./run-autonoma.sh` (Linux/macOS),
o usa el ejecutable portable `Autonoma.exe` (no necesita Python en la máquina destino).
Detalle y resolución de problemas en [INSTALL.md](INSTALL.md).

## Verificación local

```bash
python -m venv .venv
# Windows: sustituye .venv/bin/python por .venv\Scripts\python.exe
.venv/bin/python -m pip install -r autonoma-agent/requirements-test.txt
.venv/bin/python -m pytest autonoma-agent/tests -q
```

El ejecutable portable (`.exe` de un archivo para Windows, binario o zipapp en Linux/macOS) se
construye con los scripts de `autonoma-agent/scripts/` y se autoverifica con `--selftest --json`;
sus artefactos salen de CI en la job `windows-executable`.

La aplicación necesita una clave propia de NoTrack para conversaciones reales; las pruebas no usan APIs.
Si prefieres que nada salga de tu máquina, apunta el agente a un modelo local (`NOTRACK_BASE_URL=http://127.0.0.1:11434/v1`)
y la clave deja de ser obligatoria.

Puede trabajar solo: `--supervise` encadena turnos sobre un objetivo, y con `--hasta-que-pare` sigue
hasta que lo paras (`supervisor/STOP`) o el modelo dice `DONE`, con reanudación mediante
`--continuar`. Al arrancar pregunta **en qué repositorio** trabaja y **si abre un chat nuevo o sigue
uno existente**, y con `--panel` lo ves, lo diriges y lo paras desde el navegador de tu celular.
Detalles en [Modo hasta que yo pare](autonoma-agent/README.md#modo-hasta-que-yo-pare),
[Arranque](autonoma-agent/README.md#arranque-en-qué-repositorio-y-con-qué-chat) y
[desde el celular](autonoma-agent/README.md#verlo-dirigirlo-y-pararlo-desde-el-celular).

## Un solo comando

```bash
./execute "deja la tanda verde" --hasta-que-pare        # Linux/macOS
execute "deja la tanda verde" --hasta-que-pare          # Windows
```

`execute` es un atajo del lazo supervisor: recibe el enunciado entre comillas, arranca el lazo y
se queda trabajando solo. `--hasta-que-pare` le quita el tope de turnos y de tiempo.
Atajos propios: `--max N` (= `--turns N`), `--sin-panel` y `--help`. Lo demás pasa tal cual.

Se para con `Ctrl+C` (termina al acabar el turno en curso), creando `supervisor/STOP`, o desde el
panel del celular. Se retoma con `--continuar`. Detalle en
[`execute`](autonoma-agent/README.md#un-solo-comando-execute).

La cookie NDUS es **opcional**. Si la usas y no la tienes a mano (por ejemplo, trabajando dentro de un
entorno sin navegador), recupérala sin salir del programa:
`autonoma --ndus "<valor o cabecera Cookie>"`, `autonoma --ndus-file cookies.txt`, `autonoma --ndus-auto`
o `/ndus` dentro del REPL. Detalle en
[Recuperar la cookie NDUS](autonoma-agent/README.md#recuperar-la-cookie-ndus-opcional).

## Qué puede hacer hoy

Investigar en la web y leer páginas largas por partes, recorrer y **editar** archivos sin reescribirlos enteros,
buscar en disco, dejar notas en `knowledge_base/`, lanzar procesos que siguen vivos en segundo plano y consultar su
salida, y recordar la conversación entre ejecuciones (`--resume`, `/sessions`). Puede trabajar **solo** un rato:
`npm run lazo -- --approval-policy approval.json "objetivo"` encadena turnos hasta terminar, con una allow-list que
escribes tú y un presupuesto que corta el exceso. Los límites son configurables sin dejar de ser límites: la
aprobación (`SI` propio o política delegada) sigue siendo obligatoria para cualquier escritura o comando, y no hay
sandbox simulado. Detalle en [`autonoma-agent/README.md`](autonoma-agent/README.md) y en
[INSTALAR § modo desatendido](INSTALL.md#5-dejarlo-correr-solo-modo-desatendido).

## Consumo

Autonoma no corre modelos en tu equipo: habla con un proveedor por HTTP, así que su gasto
es el de un script de consola. Arranca en **~0,20 s y 26 MiB** (antes: 0,28 s y 38 MiB) (las dependencias pesadas se
cargan sólo cuando se usan), no hay bucles de espera que consuman CPU, y cuando lo dejas
trabajando solo respira: `--supervise --hasta-que-pare` hace una pausa de 2 s entre turnos
(`--pausa 0` la quita). Al terminar te dice el pico de memoria del proceso. Detalle en
[Consumo: el proceso es liviano por diseño](autonoma-agent/README.md#consumo-el-proceso-es-liviano-por-diseño).

## Asistente web para arena.ai (en construcción)

`arena-web/` usa **tu propio Brave** por CDP, con tu sesión ya iniciada, para que el lazo trabaje en
arena.ai mientras tú haces otra cosa: sin tocar tu ratón, una sola pestaña y con ritmo humano.
Se construye por etapas y cada una corre sola; la **1** ya está: `cd arena-web && ./execute --probe`
se conecta, recorre el DOM y te dice qué selector funciona y cuál no, para corregirlo en
`selectors.toml`. **No envía ningún prompt.** Detalle y nota legal en
[`arena-web/README.md`](arena-web/README.md).
