#!/usr/bin/env bash
# Сборка Flowza под Linux. PyInstaller не умеет кросс-компиляцию,
# поэтому сборка идёт в Linux-контейнере.
set -euo pipefail

IMAGE="${IMAGE:-python:3.11-slim-bookworm}"
PYINSTALLER_VERSION="${PYINSTALLER_VERSION:-6.22.3}"

docker run --rm \
  -v "$PWD":/src \
  -w /src \
  "$IMAGE" \
  bash -lc "pip install --no-cache-dir -r requirements.txt pyinstaller==${PYINSTALLER_VERSION} \
    && pyinstaller Flowza.spec --noconfirm --clean"

echo "Готово: dist/Flowza"
