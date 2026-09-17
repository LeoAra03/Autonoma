# Autonoma

Agente local de terminal Python. El código mantenible está en [`autonoma-agent/`](autonoma-agent/README.md).
El ZIP original se conserva únicamente como referencia histórica: **no contiene las correcciones**.

- [Perfil Windows/host: controles y validación de producción](WINDOWS_PRODUCCION.md)
- [Avance de la segunda etapa: pruebas y pendientes](PROGRESO.md)
- [Auditoría inicial, calificaciones y plan de mejora](AUDITORIA.md)
- [Instalación, uso y límites de seguridad](autonoma-agent/README.md)

## Verificación local

```bash
python -m venv .venv
# Windows: sustituye .venv/bin/python por .venv\Scripts\python.exe
.venv/bin/python -m pip install -r autonoma-agent/requirements-test.txt
.venv/bin/python -m pytest autonoma-agent/tests -q
```

La aplicación necesita una clave propia de NoTrack para conversaciones reales; las pruebas no usan APIs.
