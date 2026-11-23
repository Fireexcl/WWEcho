#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$ROOT"

PYTHON="$ROOT/src/full/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    if ! command -v python3 >/dev/null 2>&1; then
        printf '%s\n' '启动失败：请先安装原生架构的 Python 3.9～3.12。' >&2
        exit 1
    fi
    python3 -c 'import sys; sys.exit(0 if (3, 9) <= sys.version_info[:2] <= (3, 12) else "启动失败：Full 需要 Python 3.9～3.12。")'
    python3 -m venv "$ROOT/src/full/.venv"
fi

"$PYTHON" -c 'import sys; sys.exit(0 if (3, 9) <= sys.version_info[:2] <= (3, 12) else "启动失败：Full 需要 Python 3.9～3.12。")'
if ! "$PYTHON" -c 'import numpy, PySide6, rapidocr_onnxruntime, onnxruntime, cv2' >/dev/null 2>&1; then
    printf '%s\n' '正在安装 Full 运行依赖...'
    "$PYTHON" -m pip install -r "$ROOT/src/full/requirements-ocr.txt"
    "$PYTHON" -c 'import numpy, PySide6, rapidocr_onnxruntime, onnxruntime, cv2'
fi

exec "$PYTHON" -m src.full.echo_gui "$@"
