# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir build of the local web interface."""

import sys
from pathlib import Path

SPEC_DIR = Path(SPECPATH).resolve()
sys.path.insert(0, str(SPEC_DIR))
from spec_common import (  # noqa: E402
    ROOT,
    version,
    web_binaries,
    web_datas,
    web_excludes,
    web_hiddenimports,
)

VERSION = version()

a = Analysis(
    [str(ROOT / "web.py")],
    pathex=[str(ROOT)],
    binaries=web_binaries(),
    datas=web_datas(),
    hiddenimports=web_hiddenimports(),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=web_excludes(),
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="anomizer-web",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
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
    name="Anomizer-Web",
)
