"""Diagnóstico offline; no inicia clientes, listener ni comandos del modelo."""
from __future__ import annotations
import ctypes
import importlib.util
import json
import os
import platform
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from autonoma.config import load_settings, project_root
from autonoma.presentation import safe_text


def is_elevated() -> bool | None:
    try:
        if os.name == 'nt':
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return os.geteuid() == 0
    except (AttributeError, OSError):
        return None


def writable_directory(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=path) as stream:
            stream.write(b'autonoma diagnostic')
        return True
    except OSError:
        return False


def collect_diagnostics() -> dict[str, Any]:
    checks: list[dict[str, str]] = []
    def add(name: str, status: str, message: str) -> None:
        checks.append({'name': name, 'status': status, 'message': message})
    elevated = is_elevated()
    add('privileges', 'warning' if elevated is not False else 'ok',
        'Elevado: los comandos tendrían permisos de administrador; usa una terminal normal.' if elevated else
        'No se pudo determinar elevación.' if elevated is None else 'Sin elevación detectada.')
    add('host_access', 'warning', 'Modo host: sin aislamiento ni elevación automática; aprobación por operación.')
    add('stdin', 'ok' if sys.stdin.isatty() else 'warning',
        'TTY disponible.' if sys.stdin.isatty() else 'Sin TTY: operaciones locales denegadas.')
    try:
        settings = load_settings()
    except (ValueError, OSError):
        add('configuration', 'error', 'Configuración ilegible o inválida; revisa config.json, .env y permisos.')
    else:
        add('configuration', 'ok', 'Configuración válida.')
        for name, path in [('data_directory', project_root()), ('knowledge_directory', settings.knowledge_path()),
                           ('log_directory', settings.log_path())]:
            writable = writable_directory(path)
            add(name, 'ok' if writable else 'error', f'{safe_text(str(path))}: ' + ('escritura comprobada.' if writable else 'sin escritura.'))
        add('notrack_key', 'ok' if settings.has_notrack_key else 'warning',
            'Configurada; validez no comprobada.' if settings.has_notrack_key else 'Falta clave; configura /key antes de conversar.')
        parsed = urlsplit(settings.notrack_base_url)
        https_ok = (parsed.scheme == 'https' and bool(parsed.hostname) and not
                    (parsed.username or parsed.password or parsed.query or parsed.fragment))
        add('notrack_url', 'ok' if https_ok else 'error',
            'HTTPS configurado; conectividad no comprobada.' if https_ok else 'URL incompatible con la política HTTPS.')
    available = importlib.util.find_spec('pynput') is not None
    add('global_hotkey', 'ok' if available else 'warning',
        'pynput instalado; listener no probado. Ctrl+C siempre es el mecanismo de terminal.' if available else
        'pynput no instalado; usa Ctrl+C. El listener global es opcional.')
    return {'platform': platform.system(), 'python': platform.python_version(),
            'frozen': bool(getattr(sys, 'frozen', False)), 'checks': checks,
            'local_checks_passed': not any(c['status'] == 'error' for c in checks), 'network_tested': False}


def run_diagnostics(as_json: bool = False) -> int:
    try:
        report = collect_diagnostics()
    except (ValueError, OSError):
        report = {'local_checks_passed': False, 'network_tested': False, 'checks': [
            {'name': 'diagnostic', 'status': 'error', 'message': 'No se pudo completar el diagnóstico local.'}]}
    if as_json:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    else:
        print('Autonoma — diagnóstico local (sin comprobar servicios externos)')
        for check in report['checks']:
            print(safe_text(f"[{check['status'].upper()}] {check['name']}: {check['message']}"))
    return 0 if report['local_checks_passed'] else 1
