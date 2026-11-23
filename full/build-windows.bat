@echo off
setlocal
chcp 65001 >nul
for %%I in ("%~dp0.") do set "EDITION_DIR=%%~fI"
cd /d "%EDITION_DIR%\.." || exit /b 1

set "MODE=%~1"
if not defined MODE set "MODE=all"
if not "%~2"=="" goto invalid_mode
if /i "%MODE%"=="all" goto setup
if /i "%MODE%"=="onefile" goto setup
if /i "%MODE%"=="onedir" goto setup
goto invalid_mode

:setup
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
"%PYTHON%" -c "import platform, struct, sys; sys.exit(0 if (3, 9) <= sys.version_info[:2] <= (3, 12) and struct.calcsize('P') == 8 and platform.machine().lower() in ('amd64', 'x86_64') else 1)" >nul 2>&1
if errorlevel 1 goto python_version
if not exist "%CD%\weight-presets.json" goto presets_missing
"%PYTHON%" -m pip install -r "%EDITION_DIR%\requirements-build.txt"
if errorlevel 1 goto install_failed

"%PYTHON%" -c "from pathlib import Path; from src.echo_score import DEFAULT_STATS, DEFAULT_WEIGHTS, load_json; from src.echo_weight_presets import WeightPresetStore; WeightPresetStore(Path('weight-presets.json'), load_json(DEFAULT_STATS), load_json(DEFAULT_WEIGHTS))"
if errorlevel 1 goto presets_invalid

if /i not "%MODE%"=="onedir" (
    call :build onefile
    if errorlevel 1 goto build_failed
)
if /i not "%MODE%"=="onefile" (
    call :build onedir
    if errorlevel 1 goto build_failed
)

echo.
echo Requested Full builds complete:
if /i not "%MODE%"=="onedir" echo "%EDITION_DIR%\dist\windows-onefile\WWEcho声骸工作台.exe"
if /i not "%MODE%"=="onefile" echo "%EDITION_DIR%\dist\windows-onedir\WWEcho声骸工作台\"
echo Keep the complete onedir folder. Presets and OCR settings are saved beside the EXE.
pause
exit /b 0

:build
set "WWECHO_BUILD_MODE=%~1"
set "PYINSTALLER_CONFIG_DIR=%EDITION_DIR%\build\pyinstaller-%~1"
"%PYTHON%" -m PyInstaller --noconfirm --clean --distpath "%EDITION_DIR%\dist\windows-%~1" --workpath "%EDITION_DIR%\build\windows-%~1" "%EDITION_DIR%\WWEcho.spec"
exit /b %errorlevel%

:invalid_mode
echo Usage: full\build-windows.bat [all^|onefile^|onedir]
goto failed

:python_missing
echo Install x64 Python 3.9-3.12 for the OCR trial build and try again.
goto failed

:setup_failed
echo Failed to create the project virtual environment.
goto failed

:python_version
echo The OCR trial build requires x64 Python 3.9-3.12. Recreate full\.venv with a compatible interpreter.
goto failed

:presets_missing
echo weight-presets.json is required as the initial preset library. Run run.bat to create it, or export a library to this path.
goto failed

:presets_invalid
echo weight-presets.json is not a valid preset library. Fix it and try again.
goto failed

:install_failed
echo Failed to install build dependencies.
goto failed

:build_failed
echo The "%WWECHO_BUILD_MODE%" build failed. The requested "%MODE%" build is incomplete.

:failed
pause
exit /b 1
