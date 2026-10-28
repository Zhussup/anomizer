# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir build of the Tkinter desktop application."""

import sys
from pathlib import Path

SPEC_DIR = Path(SPECPATH).resolve()
sys.path.insert(0, str(SPEC_DIR))
from spec_common import (  # noqa: E402
    ROOT,
    ffmpeg_binary,
    model_data,
    version,
)

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=[(str(ffmpeg_binary()), "bin")],
    datas=[model_data()],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["imageio_ffmpeg", "pytest", "ruff"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Anomizer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Anomizer",
)
