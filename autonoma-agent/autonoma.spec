# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — genera Autonoma.exe (o binario Unix)."""

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hidden = []
hidden += collect_submodules("pynput")
hidden += collect_submodules("rich")
hidden += [
    "pynput.keyboard._win32",
    "pynput.mouse._win32",
    "pynput.keyboard._xorg",
    "pynput.mouse._xorg",
    "pynput.keyboard._darwin",
    "pynput.mouse._darwin",
    "pynput.keyboard._uinput",
    "bs4",
    "lxml",
    "httpx",
    "httpcore",
    "anyio",
    "psutil",
    "autonoma",
    "autonoma.cli",
    "autonoma.agent",
    "autonoma.config",
    "autonoma.key_handler",
    "autonoma.notrack_client",
    "autonoma.search_engine",
    "autonoma.filesystem",
]

datas = []
datas += collect_data_files("rich")
datas += collect_data_files("lxml")

a = Analysis(
    ["run_autonoma.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["playwright", "selenium"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Autonoma",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
