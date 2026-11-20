# -*- mode: python ; coding: utf-8 -*-

from importlib.metadata import distribution
from pathlib import Path
import os
import sys

from PyInstaller.utils.hooks import get_package_paths


ROOT = Path(SPECPATH)
IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"
MODE = os.environ.get("WWECHO_BUILD_MODE", "macos" if IS_MACOS else "onefile")
if MODE not in (("macos",) if IS_MACOS else ("onefile", "onedir") if IS_WINDOWS else ()):
    raise SystemExit(f"Unsupported build mode/platform: {MODE}/{sys.platform}")
IS_ONEFILE = MODE == "onefile"
APP_NAME = "WWEcho声骸工作台"
sys.path.insert(0, str(ROOT))
from src.echo_ocr import MODEL_FILES

datas = [
    (str(ROOT / "config" / "activation-costs.json"), "config"),
    (str(ROOT / "config" / "echo-stats.json"), "config"),
    (str(ROOT / "config" / "score-weights.json"), "config"),
    (str(ROOT / "weight-presets.json"), "."),
    (str(ROOT / "licenses" / "RapidOCR-LICENSE.txt"), "licenses"),
    (str(ROOT / "licenses" / "OCR-NOTICE.txt"), "licenses"),
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
    a.binaries if IS_ONEFILE else [],
    a.datas if IS_ONEFILE else [],
    [],
    exclude_binaries=not IS_ONEFILE,
    name=APP_NAME,
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

if not IS_ONEFILE:
    collection = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name=APP_NAME,
    )
if IS_MACOS:
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
