# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys


ROOT = Path(SPECPATH)
IS_MACOS = sys.platform == "darwin"
APP_NAME = "WWEcho声骸工作台"
datas = [
    (str(ROOT / "config" / "activation-costs.json"), "config"),
    (str(ROOT / "config" / "echo-stats.json"), "config"),
    (str(ROOT / "config" / "score-weights.json"), "config"),
    (str(ROOT / "weight-presets.json"), "."),
]

a = Analysis(
    [str(ROOT / "src" / "echo_gui.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries if not IS_MACOS else [],
    a.datas if not IS_MACOS else [],
    [],
    exclude_binaries=IS_MACOS,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

if IS_MACOS:
    collection = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name=APP_NAME,
    )
    app = BUNDLE(
        collection,
        name=APP_NAME + ".app",
        bundle_identifier="com.fireexcl.wwecho",
        info_plist={
            "CFBundleDisplayName": APP_NAME,
            "NSHighResolutionCapable": True,
            "LSMultipleInstancesProhibited": True,
        },
    )
