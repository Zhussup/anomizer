# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onefile build of the local web interface for Windows.

One file is easier to send to another person: the executable unpacks its
runtime into a temporary directory on each start and opens the browser.
"""

import sys
from pathlib import Path

SPEC_DIR = Path(SPECPATH).resolve()
sys.path.insert(0, str(SPEC_DIR))
from spec_common import (  # noqa: E402
    ROOT,
    web_binaries,
    web_datas,
    web_excludes,
    web_hiddenimports,
)

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
    a.binaries,
    a.datas,
    [],
    name="Anomizer-Web",
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
