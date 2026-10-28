"""Headless checks used to verify packaged desktop builds."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def desktop_selftest() -> int:
    """Verify bundled Tcl/Tk, FFmpeg, YuNet and image libraries.

    This intentionally does not create a ``Tk`` root window: it is safe to run
    over SSH or in a build smoke test without opening anything on the desktop.
    """
    import tkinter

    import cv2
    from PIL import Image, ImageTk  # noqa: F401 - verifies the Tk image plugin

    from .faces import ensure_model
    from .ffmpeg_utils import find_ffmpeg

    tcl = tkinter.Tcl()
    patchlevel = tcl.eval("info patchlevel")
    lines = [
        f"Tcl: {patchlevel}",
        f"Tk: {tkinter.TkVersion}",
        f"OpenCV: {cv2.__version__}",
        f"Pillow: {Image.__version__}",
        f"FFmpeg: {find_ffmpeg()}",
        f"YuNet: {ensure_model(allow_download=False)}",
    ]
    if sys.stdout is not None:
        for line in lines:
            print(line)
    log_path = os.environ.get("ANOMIZER_SELFTEST_LOG")
    if log_path:
        Path(log_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0
