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
