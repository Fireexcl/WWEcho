@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=%CD%\.venv\Scripts\python.exe"
if exist "%PYTHON%" goto check_dependencies

where py >nul 2>&1
if not errorlevel 1 goto create_with_launcher
where python >nul 2>&1
if errorlevel 1 goto python_missing

python -m venv .venv
if errorlevel 1 goto setup_failed
goto check_dependencies

:create_with_launcher
py -3 -m venv .venv
if errorlevel 1 goto setup_failed

:check_dependencies
if not exist "%PYTHON%" goto setup_failed
"%PYTHON%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
if errorlevel 1 goto python_version

"%PYTHON%" -c "import numpy, PySide6" >nul 2>&1
if errorlevel 1 goto install_dependencies
goto launch

:install_dependencies
echo Installing project dependencies...
"%PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 goto install_failed

:launch
"%PYTHON%" -m src.echo_gui %*
if errorlevel 1 goto launch_failed
exit /b 0

:python_missing
echo Python 3.9 or newer was not found. Install Python and try again.
goto failed

:python_version
echo This project requires Python 3.9 or newer.
goto failed

:setup_failed
echo Failed to create the project virtual environment.
goto failed

:install_failed
echo Failed to install project dependencies.
goto failed

:launch_failed
echo The GUI did not start successfully.

:failed
pause
exit /b 1
