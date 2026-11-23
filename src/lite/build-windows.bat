@echo off
setlocal
chcp 65001 >nul
for %%I in ("%~dp0.") do set "EDITION_DIR=%%~fI"
cd /d "%EDITION_DIR%\..\.." || exit /b 1

if not "%~1"=="" goto invalid_arguments

set "PYTHON=%EDITION_DIR%\.venv\Scripts\python.exe"
if exist "%PYTHON%" goto install_build_requirements

where py >nul 2>&1
if not errorlevel 1 goto create_with_launcher
where python >nul 2>&1
if errorlevel 1 goto python_missing

python -m venv "%EDITION_DIR%\.venv"
if errorlevel 1 goto setup_failed
goto install_build_requirements

:create_with_launcher
py -3 -m venv "%EDITION_DIR%\.venv"
if errorlevel 1 goto setup_failed

:install_build_requirements
if not exist "%PYTHON%" goto setup_failed
"%PYTHON%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
if errorlevel 1 goto python_version
if not exist "%CD%\weight-presets.json" goto presets_missing
"%PYTHON%" -m pip install -r "%EDITION_DIR%\requirements-build.txt"
if errorlevel 1 goto install_failed

"%PYTHON%" -c "from pathlib import Path; from src.echo_score import DEFAULT_STATS, DEFAULT_WEIGHTS, load_json; from src.echo_weight_presets import WeightPresetStore; WeightPresetStore(Path('weight-presets.json'), load_json(DEFAULT_STATS), load_json(DEFAULT_WEIGHTS))"
if errorlevel 1 goto presets_invalid

set "PYINSTALLER_CONFIG_DIR=%EDITION_DIR%\build\pyinstaller"
"%PYTHON%" -m PyInstaller --noconfirm --clean --workpath "%EDITION_DIR%\build" --distpath "%EDITION_DIR%\dist" "%EDITION_DIR%\WWEcho.spec"
if errorlevel 1 goto build_failed

echo.
echo Lite build complete: "%EDITION_DIR%\dist\WWEcho声骸工作台.exe"
echo Presets are saved next to the .exe. Use a writable folder.
pause
exit /b 0

:invalid_arguments
echo Usage: src\lite\build-windows.bat
goto failed

:python_missing
echo Python 3.9 or newer was not found. Install Python and try again.
goto failed

:setup_failed
echo Failed to create the project virtual environment.
goto failed

:python_version
echo This project requires Python 3.9 or newer to build.
goto failed

:presets_missing
echo weight-presets.json is required as the initial preset library. Run bin\run-lite.bat to create it, or export a library to this path.
goto failed

:presets_invalid
echo weight-presets.json is not a valid preset library. Fix it and try again.
goto failed

:install_failed
echo Failed to install build dependencies.
goto failed

:build_failed
echo Failed to build WWEcho声骸工作台.exe.

:failed
pause
exit /b 1
