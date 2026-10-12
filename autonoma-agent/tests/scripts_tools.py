"""Puente fino entre los tests y los scripts stdlib de la raíz del repo.

Mantiene una sola carga del módulo (`load_repo_script`) y reexporta sus símbolos, para que
los tests importen `from scripts_tools import ...` sin repetir el baile de `importlib`.
"""

from __future__ import annotations

from types import ModuleType

from conftest import load_repo_script

make_bundle: ModuleType = load_repo_script("make_bundle")

BundleError = make_bundle.BundleError
build_bundle = make_bundle.build_bundle
collect_files = make_bundle.collect_files
package_version = make_bundle.package_version
readme_text = make_bundle.readme_text
sha256_of = make_bundle.sha256_of
version_from_source = make_bundle.version_from_source

__all__ = [
    "BundleError",
    "build_bundle",
    "collect_files",
    "make_bundle",
    "package_version",
    "readme_text",
    "sha256_of",
    "version_from_source",
]
