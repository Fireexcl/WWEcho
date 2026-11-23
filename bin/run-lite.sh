#!/usr/bin/env bash
set -euo pipefail

# 无论从哪个目录启动，都以项目根目录作为模块搜索路径。
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$ROOT"

PYTHON="$ROOT/src/lite/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    if ! command -v python3 >/dev/null 2>&1; then
        printf '%s\n' '启动失败：请先安装 Python 3.9 或更高版本。' >&2
        exit 1
    fi
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else "启动失败：Lite 需要 Python 3.9 或更高版本。")'
    python3 -m venv "$ROOT/src/lite/.venv"
fi

"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else "启动失败：Lite 需要 Python 3.9 或更高版本。")'
if ! "$PYTHON" -c 'import numpy, PySide6' >/dev/null 2>&1; then
    printf '%s\n' '正在安装 Lite 运行依赖...'
    "$PYTHON" -m pip install -r "$ROOT/requirements.txt"
    "$PYTHON" -c 'import numpy, PySide6'
fi

exec "$PYTHON" -m src.lite.echo_gui "$@"