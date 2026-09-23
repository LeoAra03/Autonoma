"""Validación defensiva de destinos web; no sustituye un firewall de salida.

Devuelve el destino resuelto (`PublicTarget`) para que el llamador registre qué se
autorizó, y estrecha la política: el puerto debe ser coherente con el esquema
(443 sólo en HTTPS, 80 sólo en HTTP).
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final
from urllib.parse import urlsplit

from autonoma.errors import NetworkPolicyError

__all__ = ["PublicTarget", "validate_public_url"]

_ALLOWED_PORTS: Final[MappingProxyType[str, frozenset[int | None]]] = MappingProxyType(
    {"https": frozenset({None, 443}), "http": frozenset({None, 80})}
)


@dataclass(frozen=True, slots=True)
class PublicTarget:
    """URL admitida, con las direcciones públicas a las que resuelve hoy."""

    url: str
    scheme: str
    host: str
    port: int
    addresses: tuple[str, ...]

    @property
    def address_count(self) -> int:
        return len(self.addresses)

    def as_dict(self) -> dict[str, object]:
        return {
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
            "addresses": list(self.addresses),
        }


def validate_public_url(url: str, *, allow_private: bool = False) -> PublicTarget:
    """Rechaza destinos no HTTP(S), con credenciales, puertos sueltos o privados.

    `allow_private` es la única forma de abrir la red local (servidores de desarrollo,
    APIs internas, `localhost`): opt-in explícito del usuario, no un favor al modelo.
    Con él activado también se admiten puertos arbitrarios, porque un servicio local
    casi nunca escucha en 80/443.
    """
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise NetworkPolicyError("URL mal formada") from exc
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_PORTS or not parsed.hostname:
        raise NetworkPolicyError("Solo se permiten URLs HTTP(S) públicas")
    if parsed.username or parsed.password:
        raise NetworkPolicyError("La URL no puede llevar credenciales")
    try:
        port = parsed.port
    except ValueError as exc:
        raise NetworkPolicyError("Puerto inválido en la URL") from exc
    if port not in _ALLOWED_PORTS[scheme] and not allow_private:
        raise NetworkPolicyError(
            "Puerto no permitido para este esquema",
            context={"scheme": scheme, "port": str(port)},
        )
    effective_port = port or (443 if scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(parsed.hostname, effective_port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise NetworkPolicyError("No se pudo resolver el destino") from exc
    addresses = tuple(sorted({str(item[4][0]) for item in infos}))
    if not addresses:
        raise NetworkPolicyError("El destino no resolvió ninguna dirección")
    private = tuple(address for address in addresses if not _is_global(address))
    if private and not allow_private:
        # No se enumera el host completo en el mensaje: basta con saber que es local.
        raise NetworkPolicyError(
            "Destino de red privado o reservado bloqueado",
            context={"blocked_count": str(len(private))},
        )
    return PublicTarget(
        url=url,
        scheme=scheme,
        host=parsed.hostname,
        port=effective_port,
        addresses=addresses,
    )


def _is_global(address: str) -> bool:
    try:
        return bool(ipaddress.ip_address(address).is_global)
    except ValueError:
        return False
