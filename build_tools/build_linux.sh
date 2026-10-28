#!/usr/bin/env bash
# Build standalone Linux bundles.  Run this on Linux; PyInstaller cannot
# cross-compile.  Usage: ./build_tools/build_linux.sh [all|web|desktop]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

TARGET="${1:-all}"
case "$TARGET" in
  all|web|desktop) ;;
  *) echo "Использование: $0 [all|web|desktop]" >&2; exit 2 ;;
esac

PYTHON="${PYTHON:-python3}"
VENV="${ANOMIZER_BUILD_VENV:-$ROOT/.build/venv-linux}"
DIST="$ROOT/dist"
BUILD="$ROOT/build"
STAMP="$VENV/.anomizer-build-deps"

if [ ! -x "$VENV/bin/python" ]; then
  echo "==> Создаю сборочное окружение: $VENV"
  "$PYTHON" -m venv "$VENV"
fi

if [ ! -f "$STAMP" ] || [ "$ROOT/requirements-build.txt" -nt "$STAMP" ]; then
  echo "==> Устанавливаю сборочные зависимости"
  "$VENV/bin/python" -m pip install --upgrade pip
  "$VENV/bin/python" -m pip install -r "$ROOT/requirements-build.txt"
  touch "$STAMP"
fi

if [ "$TARGET" = "all" ] || [ "$TARGET" = "desktop" ]; then
  if ! "$VENV/bin/python" -c "import tkinter" >/dev/null 2>&1; then
    echo "Ошибка: для сборки desktop-версии нужен Tkinter." >&2
    echo "Debian/Ubuntu: sudo apt install python3-tk" >&2
    exit 1
  fi
fi

mkdir -p "$DIST" "$BUILD"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/anomizer/__init__.py")"
if [ -z "$VERSION" ]; then
  echo "Не удалось определить версию." >&2
  exit 1
fi
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64) ARCH="x86_64" ;;
  aarch64|arm64) ARCH="aarch64" ;;
esac
PLATFORM="linux-$ARCH"

copy_docs() {
  local folder="$1"
  cp "$ROOT/THIRD_PARTY_NOTICES.md" "$folder/"
  cp "$ROOT/README.md" "$folder/"
  cp "$ROOT/build_tools/README-WEB.txt" "$folder/README-WEB.txt"
  mkdir -p "$folder/models"
  cp "$ROOT/models/README.md" "$folder/models/README.md"
}

build_web() {
  echo "==> Собираю веб-версию"
  "$VENV/bin/python" -m PyInstaller \
    --noconfirm --clean \
    --distpath "$DIST" --workpath "$BUILD/web" \
    "$ROOT/build_tools/anomizer_web.spec"
  copy_docs "$DIST/Anomizer-Web"
  chmod +x "$DIST/Anomizer-Web/anomizer-web"
  tar -C "$DIST" -czf "$DIST/Anomizer-Web-$VERSION-$PLATFORM.tar.gz" "Anomizer-Web"
}

build_desktop() {
  echo "==> Собираю desktop-версию"
  "$VENV/bin/python" -m PyInstaller \
    --noconfirm --clean \
    --distpath "$DIST" --workpath "$BUILD/desktop" \
    "$ROOT/build_tools/anomizer_desktop.spec"
  copy_docs "$DIST/Anomizer"
  chmod +x "$DIST/Anomizer/Anomizer"
  tar -C "$DIST" -czf "$DIST/Anomizer-$VERSION-$PLATFORM.tar.gz" "Anomizer"
}

if [ "$TARGET" = "all" ] || [ "$TARGET" = "web" ]; then
  build_web
fi
if [ "$TARGET" = "all" ] || [ "$TARGET" = "desktop" ]; then
  build_desktop
fi

(
  shopt -s nullglob
  cd "$DIST"
  archives=( ./*.tar.gz ./*.zip )
  if [ ${#archives[@]} -gt 0 ]; then
    sha256sum "${archives[@]}" > SHA256SUMS
  fi
)

echo
echo "Готово. Архивы и контрольные суммы:"
ls -lh "$DIST"/*.tar.gz "$DIST/SHA256SUMS"
echo
echo "Проверка веб-сборки без окна браузера:"
echo "  $DIST/Anomizer-Web/anomizer-web --no-browser --port 8765"
