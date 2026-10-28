"""Locate bundled resources and keep child processes quiet on Windows.

A source checkout and a PyInstaller build have different layouts:

* source checkout: the project root contains ``models/``, ``bin/`` and the
  ``anomizer`` package;
* frozen build: PyInstaller extracts/copies data under ``sys._MEIPASS`` (the
  application directory for a onedir build) with the same relative layout.

Nothing in this module contacts the network or writes inside the application
bundle.  A downloaded YuNet model goes to the user's data directory instead.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

APP_DIR_NAME = "anomizer"
_BUNDLE_ENV = "ANOMIZER_BUNDLE_DIR"
_DATA_ENV = "ANOMIZER_DATA_DIR"


def is_frozen() -> bool:
    """True when running from a PyInstaller (or similar) bundle."""
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """Directory that holds bundled read-only resources."""
    override = os.environ.get(_BUNDLE_ENV)
    if override:
        return Path(override).expanduser().resolve()
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass).resolve()
    return Path(__file__).resolve().parent.parent


def project_root() -> Path:
    """Source-project root, even when the code is frozen.

    This is only meaningful for diagnostics; never write here in a build.
    """
    return Path(__file__).resolve().parent.parent


def resource_path(*parts: str | os.PathLike[str]) -> Path:
    return bundle_root().joinpath(*parts)


def is_bundled_path(path: Path) -> bool:
    """Return True when ``path`` is inside the read-only application bundle."""
    try:
        Path(path).resolve().relative_to(bundle_root())
    except (OSError, ValueError):
        return False
    return True


def static_dir() -> Path:
    """Directory with ``index.html``, ``app.js`` and ``style.css``."""
    candidate = resource_path("anomizer", "web", "static")
    if candidate.is_dir():
        return candidate
    return Path(__file__).resolve().parent / "web" / "static"


def bundled_model_path(filename: str) -> Path:
    return resource_path("models", filename)


def user_data_dir() -> Path:
    """Writable per-user directory for a downloaded detector model.

    The directory is created lazily by the caller.  Environment override is
    intended for portable builds and tests.
    """
    override = os.environ.get(_DATA_ENV)
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "Anomizer"
    xdg = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return root / APP_DIR_NAME


def bundled_ffmpeg_path() -> Path | None:
    """Return the FFmpeg executable shipped next to the application.

    PyInstaller keeps the original basename from imageio-ffmpeg (for example
    ``ffmpeg-linux-x86_64-v7.0.2``), so match the ``ffmpeg`` prefix instead of
    assuming a fixed name.  The Windows build ships ``ffmpeg.exe``.
    """
    binary_dir = resource_path("bin")
    if not binary_dir.is_dir():
        return None
    preferred = [
        binary_dir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg"),
        binary_dir / "ffmpeg",
        binary_dir / "ffmpeg.exe",
    ]
    for candidate in preferred:
        if candidate.is_file():
            return candidate
    for candidate in sorted(binary_dir.glob("ffmpeg*")):
        if candidate.is_file():
            return candidate
    return None


def no_console_kwargs() -> dict[str, object]:
    """``subprocess`` kwargs that avoid a flashing console on Windows."""
    if os.name != "nt":
        return {}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return {"creationflags": flags} if flags else {}


def detached_process_kwargs() -> dict[str, object]:
    """Run an audio helper outside this process group.

    ``start_new_session`` is POSIX-only, so it must never be passed on
    Windows.  There the useful part is suppressing the console window.
    """
    if os.name == "nt":
        return no_console_kwargs()
    return {"start_new_session": True}


def ffmpeg_text_kwargs() -> dict[str, object]:
    """Consistent decoding of FFmpeg's UTF-8 diagnostics on every locale."""
    return {
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        **no_console_kwargs(),
    }


def configure_console() -> None:
    """Make Russian console messages safe and readable on Windows.

    A frozen Windows app gets ``sys.stdout`` with the legacy ANSI code page
    (cp1251/cp866/cp1252), so printing Cyrillic can raise UnicodeEncodeError.
    Switch the console to UTF-8 and make encoding errors non-fatal.  On other
    platforms this is a no-op beyond a harmless stream reconfiguration.
    """
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleOutputCP(65001)
            kernel32.SetConsoleCP(65001)
        except (AttributeError, OSError, ValueError):
            pass
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
