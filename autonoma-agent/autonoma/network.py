"""Validación defensiva de destinos web; no sustituye un firewall de salida."""
import ipaddress
import socket
from urllib.parse import urlsplit


def validate_public_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("Solo se permiten URLs HTTP(S) públicas")
    if parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
        raise ValueError("Credenciales o puerto no permitido en URL")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("No se pudo resolver el destino") from exc
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Destino de red privado o reservado bloqueado")
