#!/usr/bin/env bash
set -euo pipefail

# 无论从哪个目录启动，都以项目根目录作为模块搜索路径。
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$ROOT"

PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    printf '%s\n' '启动失败：未找到项目虚拟环境的 Python。请先创建 .venv 并安装 requirements.txt 中的依赖。' >&2
    exit 1
fi

exec "$PYTHON" -m src.echo_gui "$@"