@echo off
setlocal
cd /d "%~dp0.." || exit /b 1

set "PYTHON=%CD%\src\full\.venv\Scripts\python.exe"
if exist "%PYTHON%" goto check_dependencies

where py >nul 2>&1
if errorlevel 1 goto check_python
py -3.12 -c "import sys" >nul 2>&1
if not errorlevel 1 goto create_with_312
py -3 -c "import sys; sys.exit(0 if (3, 9) <= sys.version_info[:2] <= (3, 12) else 1)" >nul 2>&1
if not errorlevel 1 goto create_with_launcher

:check_python
where python >nul 2>&1
if errorlevel 1 goto python_missing
python -c "import sys; sys.exit(0 if (3, 9) <= sys.version_info[:2] <= (3, 12) else 1)" >nul 2>&1
if errorlevel 1 goto python_version
python -m venv "%CD%\src\full\.venv"
if errorlevel 1 goto setup_failed
goto check_dependencies

:create_with_312
py -3.12 -m venv "%CD%\src\full\.venv"
if errorlevel 1 goto setup_failed
goto check_dependencies

:create_with_launcher
py -3 -m venv "%CD%\src\full\.venv"
if errorlevel 1 goto setup_failed

:check_dependencies
if not exist "%PYTHON%" goto setup_failed
"%PYTHON%" -c "import sys; sys.exit(0 if (3, 9) <= sys.version_info[:2] <= (3, 12) else 1)" >nul 2>&1
if errorlevel 1 goto python_version

"%PYTHON%" -c "import numpy, PySide6, rapidocr_onnxruntime, onnxruntime, cv2" >nul 2>&1
if errorlevel 1 goto install_dependencies
goto launch

:install_dependencies
echo Installing Full dependencies...
"%PYTHON%" -m pip install -r src\full\requirements-ocr.txt
if errorlevel 1 goto install_failed
"%PYTHON%" -c "import numpy, PySide6, rapidocr_onnxruntime, onnxruntime, cv2"
if errorlevel 1 goto dependencies_failed

:launch
"%PYTHON%" -m src.full.echo_gui %*
if errorlevel 1 goto launch_failed
exit /b 0

:python_missing
echo Python 3.9-3.12 was not found. Install Python and try again.
goto failed

:python_version
echo Full requires Python 3.9-3.12. Use a compatible interpreter for src\full\.venv.
goto failed

:setup_failed
echo Failed to create the Full virtual environment.
goto failed

:install_failed
echo Failed to install Full dependencies.
goto failed

:dependencies_failed
echo Full dependencies could not be loaded. See the error above.
goto failed

:launch_failed
echo The Full GUI did not start successfully.

:failed
pause
exit /b 1
