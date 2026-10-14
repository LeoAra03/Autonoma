"""Etapa 1: conectarse por CDP y diagnosticar el DOM **sin enviar ni un prompt**.

    python probe.py                 # usa config.toml y selectors.toml de al lado
    python probe.py --json          # salida para máquinas
    python probe.py --config ../mi-config.toml

Qué hace, en orden:

1. Se adjunta al Brave que ya corre.
2. Busca la pestaña de arena.ai (si no hay, la abre en segundo plano).
3. Prueba TODAS las estrategias de `selectors.toml` y te dice cuál resolvió.
4. Te imprime los elementos reales de la página, para corregir el TOML mirando.

No manda nada, no hace clic, no mueve el foco. Es un diagnóstico.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import cdp
import probing
import registro

AQUI = Path(__file__).resolve().parent
__all__ = ["cargar_toml", "principal", "sondeo"]


def cargar_toml(ruta: Path) -> dict[str, Any]:
    """TOML del sistema (3.11+) o de `tomli` (3.10). Nada más: sin dependencias raras."""
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:  # pragma: no cover — depende de la versión
        try:
            import tomli as tomllib
        except ModuleNotFoundError as exc:
            raise SystemExit("Este Python no trae `tomllib` (hace falta 3.11+, o `pip install tomli`).") from exc
    try:
        with ruta.open("rb") as handle:
            datos: dict[str, Any] = tomllib.load(handle)
    except OSError as exc:
        raise SystemExit(f"No pude leer {ruta}: {exc}") from exc
    except Exception as exc:
        raise SystemExit(f"{ruta} no es TOML válido: {exc}") from exc
    return datos


def _titulo(pagina: Any) -> str:
    """El título, si se puede. Un fallo aquí no arruina el diagnóstico."""
    try:
        titulo = pagina.title()
        if callable(titulo):
            titulo = titulo()
        return str(titulo or "")
    except Exception:  # noqa: BLE001
        return ""


def sondeo(config: dict[str, Any], selectores: dict[str, Any], *, con_inventario: bool = True) -> dict[str, Any]:
    """Conecta, localiza la pestaña y prueba los selectores. Devuelve el informe."""
    url_cdp = str(config.get("cdp", {}).get("url", "http://127.0.0.1:9222"))
    timeout_ms = int(config.get("cdp", {}).get("conexion_ms", 10_000))
    url = str(config.get("arena", {}).get("url", "https://arena.ai"))
    patron = str(config.get("arena", {}).get("patron_url", "arena.ai"))

    gestor, navegador = cdp.conectar(url_cdp, timeout_ms=timeout_ms)
    try:
        contexto = navegador.contexts[0]
        pagina: Any = cdp.localizar_pestana(contexto, patron)
        nueva = pagina is None
        if nueva:
            pagina = cdp.abrir_pestana_fondo(navegador, contexto, url)

        grupos = probing.cargar_grupos(selectores)
        resultados: list[probing.Resultado] = []
        for nombre, datos in grupos.items():
            resultados.extend(probing.probar_grupo(pagina, nombre, datos))

        candidatos = probing.inventario(pagina) if con_inventario else []
        return {
            "url_cdp": url_cdp,
            "url": pagina.url,
            "titulo": _titulo(pagina),
            "pestana_nueva": nueva,
            "resultados": resultados,
            "candidatos": candidatos,
        }
    finally:
        gestor.stop()


def _pendientes(informe: dict[str, Any]) -> list[str]:
    """Grupos en los que **ninguna** estrategia resolvió (un grupo con una que
    funcione ya está bien: esas son las alternativas)."""
    resultados: list[probing.Resultado] = informe["resultados"]
    todos = {r.grupo for r in resultados}
    resueltos = {r.grupo for r in resultados if r.ok}
    return sorted(todos - resueltos)


def _resumen(informe: dict[str, Any]) -> str:
    resultados: list[probing.Resultado] = informe["resultados"]
    grupos = {r.grupo for r in resultados}
    return (
        f"{sum(1 for r in resultados if r.ok)} de {len(resultados)} estrategias resuelven · "
        f"{len(grupos) - len(_pendientes(informe))} de {len(grupos)} grupos tienen al menos una"
    )


def _texto(informe: dict[str, Any]) -> str:
    lineas = [
        f"Brave   : conectado en {informe['url_cdp']}",
        f"Pestaña : {informe['url']}"
        + ("  (recién abierta, en segundo plano)" if informe["pestana_nueva"] else "  (ya estaba abierta)"),
        f"Título  : {informe['titulo']}",
        "",
        "SELECTORES",
        probing.formatear_tabla(informe["resultados"]),
        "",
        "CANDIDATOS REALES (para corregir selectors.toml sin tocar Python)",
        probing.formatear_inventario(informe["candidatos"]),
        "",
        f"Resumen : {_resumen(informe)}",
    ]
    pendientes = _pendientes(informe)
    if not pendientes:
        lineas.append("Todo resuelve. Etapa 1 lista.")
    else:
        lineas.append("Sin resolver: " + ", ".join(pendientes))
        lineas.append("Arréglalos en selectors.toml y vuelve a correr ./execute --probe.")
    return "\n".join(lineas)


def principal(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="probe",
        description="Diagnostica el DOM de arena.ai por CDP. No envía ningún prompt.",
    )
    parser.add_argument("--config", type=Path, default=AQUI / "config.toml", help="ruta de config.toml")
    parser.add_argument(
        "--selectores", type=Path, default=None, help="ruta de selectors.toml (por defecto, el de config)"
    )
    parser.add_argument("--json", action="store_true", help="salida en JSON, para máquinas")
    parser.add_argument("--sin-inventario", action="store_true", help="no listar candidatos")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="diagnosticar el DOM y salir (por ahora es la única acción; se acepta por comodidad)",
    )
    args = parser.parse_args(argv)

    config = cargar_toml(args.config)
    ruta_selectores = args.selectores
    if ruta_selectores is None:
        nombre = str(config.get("selectores", {}).get("archivo", "selectors.toml"))
        ruta_selectores = (args.config.parent / nombre) if not Path(nombre).is_absolute() else Path(nombre)
    selectores = cargar_toml(ruta_selectores)

    try:
        informe = sondeo(config, selectores, con_inventario=not args.sin_inventario)
    except cdp.ErrorCDP as exc:
        print(f"probe: {exc}", file=sys.stderr)
        return 2

    # El log es el expediente: queda constancia de cada sondeo.
    logs = config.get("logs", {})
    registro.registrar(
        registro.ruta_log(args.config.parent / str(logs.get("directorio", "logs")), "probe.jsonl"),
        "probe",
        url=informe["url"],
        estrategias=len(informe["resultados"]),
        resueltas=sum(1 for r in informe["resultados"] if r.ok),
        grupos_sin_resolver=_pendientes(informe),
    )

    if args.json:
        print(
            json.dumps(
                {
                    "url": informe["url"],
                    "titulo": informe["titulo"],
                    "pestana_nueva": informe["pestana_nueva"],
                    "resultados": [
                        {
                            "grupo": r.grupo,
                            "estrategia": r.estrategia.descripcion(),
                            "resuelve": r.ok,
                            "encontrados": r.encontrados,
                            "visible": r.visible,
                            "error": r.error,
                        }
                        for r in informe["resultados"]
                    ],
                    "candidatos": informe["candidatos"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(_texto(informe))
    return 0


if __name__ == "__main__":
    raise SystemExit(principal())
