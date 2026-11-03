#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
    printf '%s\n' 'Build failed: macOS is required to build the .app bundle.' >&2
    exit 1
fi

PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    if ! command -v python3 >/dev/null 2>&1; then
        printf '%s\n' 'Build failed: install Python 3.9 or newer first.' >&2
        exit 1
    fi
    python3 -m venv "$ROOT/.venv"
fi
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else "Build failed: Python 3.9 or newer is required.")'

if [[ ! -f "$ROOT/weight-presets.json" ]]; then
    printf '%s\n' 'Build failed: weight-presets.json is required as the initial preset library. Run ./run.sh to create it, or export a library to this path.' >&2
    exit 1
fi

"$PYTHON" -m pip install -r requirements-build.txt
"$PYTHON" -c 'from pathlib import Path; from src.echo_score import DEFAULT_STATS, DEFAULT_WEIGHTS, load_json; from src.echo_weight_presets import WeightPresetStore; WeightPresetStore(Path("weight-presets.json"), load_json(DEFAULT_STATS), load_json(DEFAULT_WEIGHTS))'
"$PYTHON" -m PyInstaller --noconfirm --clean WWEcho.spec

printf '\nBuild complete: %s/dist/WWEcho声骸工作台.app\n' "$ROOT"
printf '%s\n' 'Presets are saved next to the .app. Keep both together in a writable folder.'
printf '%s\n' 'This is a local, ad-hoc-signed build, not a Developer ID signed/notarized release.'
