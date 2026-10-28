"""Shared values for the PyInstaller spec files.

PyInstaller cannot cross-compile: run the Linux spec on Linux and the Windows
spec on Windows.  The FFmpeg executable is taken from imageio-ffmpeg for the
host platform, so this module deliberately does not hard-code a filename.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "anomizer" / "web" / "static"
MODEL_DIR = ROOT / "models"
MODEL_FILENAME = "face_detection_yunet_2023mar.onnx"


def version() -> str:
    text = (ROOT / "anomizer" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if match is None:
        raise SystemExit("Не удалось определить версию Anomizer.")
    return match.group(1)


def ffmpeg_binary() -> Path:
    """Return the host FFmpeg shipped inside the application bundle."""
    try:
        import imageio_ffmpeg
    except ImportError as exc:  # pragma: no cover - build environment error
        raise SystemExit(
            "Для сборки нужен imageio-ffmpeg: pip install -r requirements-build.txt"
        ) from exc
    exe = Path(imageio_ffmpeg.get_ffmpeg_exe())
    if not exe.is_file():  # pragma: no cover - broken wheel
        raise SystemExit(f"imageio-ffmpeg не нашёл FFmpeg: {exe}")
    return exe


def model_data() -> tuple[str, str]:
    model = MODEL_DIR / MODEL_FILENAME
    if not model.is_file():
        raise SystemExit(
            f"Нет модели детектора: {model}. Соберите проект из полного архива."
        )
    return str(model), "models"


def web_datas() -> list[tuple[str, str]]:
    if not (STATIC_DIR / "index.html").is_file():
        raise SystemExit(f"Нет файлов веб-интерфейса: {STATIC_DIR}")
    return [(str(STATIC_DIR), "anomizer/web/static"), model_data()]


def web_binaries() -> list[tuple[str, str]]:
    return [(str(ffmpeg_binary()), "bin")]


def web_hiddenimports() -> list[str]:
    from PyInstaller.utils.hooks import collect_submodules

    # Uvicorn selects its loop/protocol/lifespan implementations by import
    # string at runtime, so static analysis cannot see them.
    return [
        *collect_submodules("uvicorn"),
        *collect_submodules("anyio"),
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
        "email.mime.multipart",
        "email.mime.text",
    ]


def web_excludes() -> list[str]:
    return [
        "tkinter",
        "_tkinter",
        "PIL",
        "pytest",
        "ruff",
        # Its 60+ MB binary is replaced by the explicit bin/ entry above.
        "imageio_ffmpeg",
    ]
