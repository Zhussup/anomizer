#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if ! python3 -c "import tkinter" >/dev/null 2>&1; then
  echo "Не найден Tkinter. В Debian/Ubuntu: sudo apt install python3-tk" >&2
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  python3 -m venv --system-site-packages .venv
fi

if ! .venv/bin/python -c "import cv2, PIL, imageio_ffmpeg" >/dev/null 2>&1; then
  .venv/bin/python -m pip install -r requirements.txt
fi

exec .venv/bin/python app.py
