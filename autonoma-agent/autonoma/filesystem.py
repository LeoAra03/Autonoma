"""Manipulación de archivos y ejecución de procesos con capa de seguridad."""

from __future__ import annotations

import logging
import os
import shutil
import signal
import shlex
import math
import time
import subprocess
import threading
from pathlib import Path
from typing import Any

from autonoma.path_policy import is_redirected, has_redirected_component, validate_windows_path
from autonoma.key_handler import PanicController

logger = logging.getLogger(__name__)


class FileSystemError(RuntimeError):
    """Error de E/S o de política de seguridad."""


def _win_env_path(name: str) -> Path | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return Path(raw).resolve()
    except OSError:
        return None


def _default_protected() -> list[Path]:
    paths: list[Path] = []
    if os.name == "nt":
        candidates = [
            Path(r"C:\Windows"),
            Path(r"C:\Windows\System32"),
            Path(r"C:\Windows\SysWOW64"),
            Path(r"C:\Program Files"),
            Path(r"C:\Program Files (x86)"),
            Path(r"C:\ProgramData"),
            Path(r"C:\Recovery"),
            Path(r"C:\$Recycle.Bin"),
            Path(r"C:\System Volume Information"),
            Path(r"C:\Boot"),
            Path(r"C:\EFI"),
        ]
        for env_name in ("WINDIR", "SYSTEMROOT", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA"):
            extra = _win_env_path(env_name)
            if extra:
                candidates.append(extra)
        paths.extend(candidates)
    else:
        candidates = [
            Path("/"),
            Path("/etc"),
            Path("/usr"),
            Path("/bin"),
            Path("/sbin"),
            Path("/lib"),
            Path("/lib64"),
            Path("/boot"),
            Path("/dev"),
            Path("/proc"),
            Path("/sys"),
            Path("/root"),
            Path("/run"),
            Path("/snap"),
            Path("/System"),
            Path("/Library"),
            Path("/private/etc"),
            Path("/private/var"),
            Path("/Applications"),
            Path("/usr/local/bin"),  # only as prefix of itself, see is_protected
        ]
        paths.extend(candidates)
    resolved: list[Path] = []
    for p in paths:
        try:
            resolved.append(p.resolve())
        except OSError:
            resolved.append(p)
    return resolved


class FileSystemManager:
    """Leer, escribir, copiar, mover, borrar y lanzar procesos con protecciones."""

    def __init__(
        self,
        panic: PanicController,
        extra_protected: list[str] | None = None,
        command_timeout: float = 60.0,
    ) -> None:
        self.panic = panic
        self.command_timeout = command_timeout
        self._protected = _default_protected()
        if extra_protected:
            for raw in extra_protected:
                try:
                    self._protected.append(Path(raw).expanduser().resolve())
                except OSError:
                    self._protected.append(Path(raw))
        self._procs: list[subprocess.Popen[str]] = []
        self._proc_lock = threading.Lock()
        panic.register_cleanup(self.kill_all)

    # ------------------------------------------------------------------ safety
    def resolve(self, path: str | Path) -> Path:
        if os.name == "nt":
            validate_windows_path(str(path))
        return Path(path).expanduser().resolve()

    def is_protected(self, path: str | Path) -> bool:
        """True si la ruta es (o está dentro de) un directorio crítico del SO."""
        try:
            target = self.resolve(path)
        except OSError:
            target = Path(path).expanduser()

        # En Unix, bloquear "/" como destino de delete/move, pero no todo lo que cuelga de /.
        unix_root_only = {Path("/")}
        for protected in self._protected:
            try:
                prot = protected.resolve()
            except OSError:
                prot = protected
            if os.name != "nt" and prot in unix_root_only:
                if target == prot:
                    return True
                continue
            if target == prot:
                return True
            try:
                if target.is_relative_to(prot):
                    return True
            except (ValueError, TypeError, OSError):
                try:
                    target.relative_to(prot)
                    return True
                except ValueError:
                    continue
        return False

    def _assert_writable(self, path: str | Path, *, force: bool, user_prompt: str) -> Path:
        target = self.resolve(path)
        if not self.is_protected(target):
            if has_redirected_component(Path(path)):
                raise FileSystemError("Las operaciones mediante enlaces simbólicos/reparse points están bloqueadas")
            return target
        raise FileSystemError(f"Ruta protegida del sistema operativo: {target}. Operación bloqueada.")

    def _assert_not_protected_delete(self, path: str | Path, *, force: bool, user_prompt: str) -> Path:
        target = self._assert_writable(path, force=force, user_prompt=user_prompt)
        if any(protected.is_relative_to(target) for protected in self._protected):
            raise FileSystemError("La ruta contiene una ubicación protegida")
        return target

    def _assert_plain_tree(self, source: Path) -> None:
        if has_redirected_component(source):
            raise FileSystemError("No se permiten enlaces simbólicos en copias recursivas")
        if source.is_dir():
            for parent, directories, files in os.walk(source, followlinks=False):
                self.panic.check()
                if any(is_redirected(Path(parent) / name) for name in directories + files):
                    raise FileSystemError("No se permiten enlaces simbólicos en copias recursivas")

    # ------------------------------------------------------------------- CRUD
    def read_file(self, path: str, max_chars: int = 80_000) -> str:
        self.panic.check()
        target = self.resolve(path)
        if not target.exists():
            raise FileSystemError(f"No existe: {target}")
        if target.is_dir():
            raise FileSystemError(f"Es un directorio, no un archivo: {target}")
        try:
            with target.open(encoding="utf-8", errors="replace") as stream:
                data = stream.read(max_chars + 1)
        except OSError as exc:
            raise FileSystemError(f"No se pudo leer {target}: {exc}") from exc
        if len(data) > max_chars:
            return data[:max_chars] + f"\n\n…[truncado, más de {max_chars} caracteres]"
        return data

    def write_file(self, path: str, content: str, *, force: bool = False, user_prompt: str = "") -> str:
        self.panic.check()
        target = self._assert_writable(path, force=force, user_prompt=user_prompt)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise FileSystemError(f"No se pudo escribir {target}: {exc}") from exc
        return f"Escrito {target} ({len(content)} caracteres)"

    def append_file(self, path: str, content: str, *, force: bool = False, user_prompt: str = "") -> str:
        self.panic.check()
        target = self._assert_writable(path, force=force, user_prompt=user_prompt)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as fh:
                fh.write(content)
        except OSError as exc:
            raise FileSystemError(f"No se pudo anexar {target}: {exc}") from exc
        return f"Anexado a {target}"

    def copy_path(self, src: str, dst: str, *, force: bool = False, user_prompt: str = "") -> str:
        self.panic.check()
        self._assert_plain_tree(Path(src).expanduser())
        source = self.resolve(src)
        dest = self._assert_writable(dst, force=force, user_prompt=user_prompt)
        if not source.exists():
            raise FileSystemError(f"Origen no existe: {source}")
        if dest.exists():
            raise FileSystemError("El destino ya existe; elige una ruta nueva explícita")
        if dest.is_relative_to(source):
            raise FileSystemError("El destino no puede estar dentro del origen")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, dest)
            else:
                shutil.copy2(source, dest)
        except OSError as exc:
            raise FileSystemError(f"Copia falló: {exc}") from exc
        return f"Copiado {source} → {dest}"

    def move_path(self, src: str, dst: str, *, force: bool = False, user_prompt: str = "") -> str:
        self.panic.check()
        source = self._assert_not_protected_delete(src, force=force, user_prompt=user_prompt)
        dest = self._assert_writable(dst, force=force, user_prompt=user_prompt)
        if not source.exists():
            raise FileSystemError(f"Origen no existe: {source}")
        if dest.exists():
            raise FileSystemError("El destino ya existe; elige una ruta nueva explícita")
        if dest.is_relative_to(source):
            raise FileSystemError("El destino no puede estar dentro del origen")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(dest))
        except OSError as exc:
            raise FileSystemError(f"Movimiento falló: {exc}") from exc
        return f"Movido {source} → {dest}"

    def delete_path(self, path: str, *, force: bool = False, user_prompt: str = "") -> str:
        self.panic.check()
        target = self._assert_not_protected_delete(path, force=force, user_prompt=user_prompt)
        if not target.exists():
            raise FileSystemError(f"No existe: {target}")
        try:
            if target.is_dir():
                shutil.rmtree(target)
                return f"Directorio eliminado: {target}"
            target.unlink()
            return f"Archivo eliminado: {target}"
        except OSError as exc:
            raise FileSystemError(f"No se pudo eliminar {target}: {exc}") from exc

    def list_dir(self, path: str, max_entries: int = 400) -> str:
        self.panic.check()
        target = self.resolve(path)
        if not target.exists():
            raise FileSystemError(f"No existe: {target}")
        if not target.is_dir():
            raise FileSystemError(f"No es un directorio: {target}")
        try:
            entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            raise FileSystemError(f"No se pudo listar {target}: {exc}") from exc
        lines: list[str] = [f"{target} ({len(entries)} entradas)"]
        for item in entries[:max_entries]:
            kind = "dir " if item.is_dir() else "file"
            size = ""
            try:
                if item.is_file():
                    size = f" {item.stat().st_size}B"
            except OSError:
                size = ""
            lines.append(f"  [{kind}] {item.name}{size}")
        if len(entries) > max_entries:
            lines.append(f"  … +{len(entries) - max_entries} más")
        return "\n".join(lines)

    def mkdir(self, path: str, *, force: bool = False, user_prompt: str = "") -> str:
        self.panic.check()
        target = self._assert_writable(path, force=force, user_prompt=user_prompt)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise FileSystemError(f"No se pudo crear directorio {target}: {exc}") from exc
        return f"Directorio listo: {target}"

    # --------------------------------------------------------------- processes
    def run_command(
        self,
        command: str | list[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        shell: bool = True,
    ) -> dict[str, Any]:
        """Ejecuta un programa externo. Cancelable con la tecla P."""
        self.panic.check()
        timeout = timeout if timeout is not None else self.command_timeout
        if not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise FileSystemError("timeout debe estar entre 0 y 300 segundos")
        if isinstance(command, list):
            if shell or not command or any(not isinstance(arg, str) or "\x00" in arg for arg in command):
                raise FileSystemError("argv requiere una lista válida y shell=False")
        elif not isinstance(command, str) or not command.strip():
            raise FileSystemError("Comando vacío")
        cwd_path = self.resolve(cwd) if cwd else Path.cwd()
        if not cwd_path.is_dir():
            raise FileSystemError(f"cwd inválido: {cwd_path}")

        kwargs: dict[str, Any] = {
            "args": command if shell or isinstance(command, list) else shlex.split(command, posix=os.name != "nt"),
            "cwd": str(cwd_path),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "shell": shell,
        }
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            kwargs["creationflags"] = flags
        else:
            kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(**kwargs)
        except OSError as exc:
            raise FileSystemError(f"No se pudo lanzar el proceso: {exc}") from exc

        with self._proc_lock:
            self._procs.append(proc)

        # Drenar ambos pipes concurrentemente evita el deadlock de wait()+PIPE.
        # Conservar solo la cola limita el uso de memoria, incluso con salida continua.
        tails = {"stdout": "", "stderr": ""}
        def drain(stream: Any, key: str, limit: int) -> None:
            try:
                while chunk := stream.read(4096):
                    tails[key] = (tails[key] + chunk)[-limit:]
            finally:
                stream.close()
        readers = [threading.Thread(target=drain, args=(proc.stdout, "stdout", 12000), daemon=True),
                   threading.Thread(target=drain, args=(proc.stderr, "stderr", 8000), daemon=True)]
        for reader in readers:
            reader.start()
        try:
            deadline = time.monotonic() + timeout
            while proc.poll() is None or any(reader.is_alive() for reader in readers):
                self.panic.check()
                if time.monotonic() >= deadline:
                    raise FileSystemError(f"Timeout ({timeout}s) ejecutando comando")
                self.panic.wait(0.05)
        finally:
            self._kill_proc(proc)
            proc.wait(timeout=5)
            for reader in readers:
                reader.join(timeout=2)
            with self._proc_lock:
                if proc in self._procs:
                    self._procs.remove(proc)
        stdout, stderr = tails["stdout"], tails["stderr"]

        out = (stdout or "")[-12_000:]
        err = (stderr or "")[-8_000:]
        return {
            "command": command,
            "returncode": proc.returncode,
            "stdout": out,
            "stderr": err,
        }

    def _kill_proc(self, proc: subprocess.Popen[str]) -> None:
        if os.name == "nt":
            # Capturar descendientes antes de terminar el padre.
            try:
                import psutil
                children = psutil.Process(proc.pid).children(recursive=True)
                for child in reversed(children):
                    try:
                        child.kill()
                    except psutil.Error:
                        pass
            except ImportError:
                logger.warning("psutil no disponible: limpieza de descendientes limitada")
            except Exception as exc:
                logger.debug("Limpieza de descendientes: %s", type(exc).__name__)
            if proc.poll() is None:
                proc.kill()
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                pass
            # El padre puede salir antes que hijos que ignoran SIGTERM.
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            logger.debug("No se pudo terminar grupo: %s", type(exc).__name__)
            if proc.poll() is None:
                proc.kill()

    def kill_all(self) -> None:
        with self._proc_lock:
            procs = list(self._procs)
        for proc in procs:
            self._kill_proc(proc)

    def format_command_result(self, result: dict[str, Any]) -> str:
        parts = [
            f"$ {result.get('command')}",
            f"exit={result.get('returncode')}",
        ]
        if result.get("stdout"):
            parts.append("--- stdout ---\n" + str(result["stdout"]))
        if result.get("stderr"):
            parts.append("--- stderr ---\n" + str(result["stderr"]))
        return "\n".join(parts)
