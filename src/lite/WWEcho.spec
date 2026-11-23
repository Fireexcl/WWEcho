# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys

from PIL import Image


EDITION_DIR = Path(SPECPATH).resolve()
ROOT = EDITION_DIR.parent.parent
IS_MACOS = sys.platform == "darwin"
APP_NAME = "WWEcho声骸工作台" + ("-Lite" if IS_MACOS else "")
SOURCE_ICON = ROOT / "icon.png"
if not SOURCE_ICON.is_file():
    raise SystemExit(f"Required application icon missing: {SOURCE_ICON}")

BUILD_ICON_DIR = EDITION_DIR / "build" / "generated-icons"
BUILD_ICON_DIR.mkdir(parents=True, exist_ok=True)
with Image.open(SOURCE_ICON) as source:
    icon = source.convert("RGBA")
    icon.thumbnail((512, 512), Image.Resampling.LANCZOS)
    square = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    square.alpha_composite(icon, ((512 - icon.width) // 2, (512 - icon.height) // 2))

if sys.platform == "win32":
    APP_ICON = str(BUILD_ICON_DIR / "WWEcho.ico")
    square.save(
        APP_ICON,
        format="ICO",
        sizes=((16, 16), (24, 24), (32, 32), (48, 48),
               (64, 64), (128, 128), (256, 256)),
    )
elif IS_MACOS:
    APP_ICON = str(BUILD_ICON_DIR / "WWEcho.icns")
    square.save(APP_ICON, format="ICNS")
else:
    raise SystemExit(f"Unsupported build platform: {sys.platform}")

datas = [
    (str(SOURCE_ICON), "."),
    (str(ROOT / "config" / "activation-costs.json"), "config"),
    (str(ROOT / "config" / "echo-stats.json"), "config"),
    (str(ROOT / "config" / "score-weights.json"), "config"),
    (str(ROOT / "weight-presets.json"), "."),
]

a = Analysis(
    [str(EDITION_DIR / "echo_gui.py")],
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
    icon=APP_ICON,
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
        icon=APP_ICON,
        bundle_identifier="com.fireexcl.wwecho.lite",
        info_plist={
            "CFBundleDisplayName": APP_NAME,
            "NSHighResolutionCapable": True,
            "LSMultipleInstancesProhibited": True,
        },
    )
