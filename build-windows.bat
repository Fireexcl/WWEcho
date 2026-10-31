@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "PYTHON=%CD%\.venv\Scripts\python.exe"
if exist "%PYTHON%" goto install_build_requirements

where py >nul 2>&1
if not errorlevel 1 goto create_with_launcher
where python >nul 2>&1
if errorlevel 1 goto python_missing

python -m venv .venv
if errorlevel 1 goto setup_failed
goto install_build_requirements

:create_with_launcher
py -3 -m venv .venv
if errorlevel 1 goto setup_failed

:install_build_requirements
if not exist "%PYTHON%" goto setup_failed
"%PYTHON%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
if errorlevel 1 goto python_version
"%PYTHON%" -m pip install -r requirements-build.txt
if errorlevel 1 goto install_failed

"%PYTHON%" -m PyInstaller --noconfirm --clean WWEcho.spec
if errorlevel 1 goto build_failed

echo.
echo Build complete: %CD%\dist\WWEcho声骸工作台.exe
pause
exit /b 0

:python_missing
echo Python 3.9 or newer was not found. Install Python and try again.
goto failed

:setup_failed
echo Failed to create the project virtual environment.
goto failed

:python_version
echo This project requires Python 3.9 or newer to build.
goto failed

:install_failed
echo Failed to install build dependencies.
goto failed

:build_failed
echo Failed to build WWEcho声骸工作台.exe.

:failed
pause
exit /b 1
