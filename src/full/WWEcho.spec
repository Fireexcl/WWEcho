# -*- mode: python ; coding: utf-8 -*-

from importlib.metadata import distribution
from pathlib import Path
import sys
from PIL import Image

from PyInstaller.utils.hooks import get_package_paths


EDITION_DIR = Path(SPECPATH).resolve()
ROOT = EDITION_DIR.parent.parent
IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"
if not (IS_MACOS or IS_WINDOWS):
    raise SystemExit(f"Unsupported build platform: {sys.platform}")
APP_NAME = "WWEcho声骸工作台" + ("-Full" if IS_MACOS else "")
sys.path.insert(0, str(ROOT))
from src.full.echo_ocr import MODEL_FILES

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

if IS_WINDOWS:
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
    (str(EDITION_DIR / "licenses" / "RapidOCR-LICENSE.txt"), "licenses"),
    (str(EDITION_DIR / "licenses" / "OCR-NOTICE.txt"), "licenses"),
]
_, ocr_dir = get_package_paths("rapidocr_onnxruntime")
for relative in ("config.yaml", *(f"models/{name}" for name in MODEL_FILES.values())):
    asset = Path(ocr_dir) / relative
    if not asset.is_file():
        raise SystemExit(f"Required OCR resource missing: {asset}")
    datas.append((str(asset), str(Path("rapidocr_onnxruntime") / Path(relative).parent)))

# ONNX Runtime and tqdm ship notices inside the package rather than dist-info.
ocr_distributions = (
    "onnxruntime", "opencv-python", "numpy", "PyYAML", "pyclipper", "Shapely",
    "Pillow", "six", "tqdm", "coloredlogs", "humanfriendly", "flatbuffers",
    "packaging", "protobuf", "sympy", "mpmath",
) + (("colorama",) if IS_WINDOWS else ())
for package in ocr_distributions:
    wheel = distribution(package)
    for path in wheel.files or ():
        if not path.name.lower().startswith(("licen", "copying", "notice", "thirdpartynotice")):
            continue
        if path.parts[0].endswith(".dist-info") or len(path.parts) == 1 or (
                len(path.parts) == 2 and path.parts[0] in ("onnxruntime", "tqdm")):
            datas.append((str(wheel.locate_file(path)), f"licenses/{package}"))

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
    a.binaries if IS_WINDOWS else [],
    a.datas if IS_WINDOWS else [],
    [],
    exclude_binaries=IS_MACOS,
    name=APP_NAME,
    icon=APP_ICON,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="arm64" if IS_MACOS else None,
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
        bundle_identifier="com.fireexcl.wwecho.full",
        info_plist={
            "CFBundleDisplayName": APP_NAME,
            "NSHighResolutionCapable": True,
            "LSMultipleInstancesProhibited": True,
        },
    )
