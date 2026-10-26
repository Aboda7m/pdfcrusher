 @echo off
setlocal
cd /d "%~dp0"

for %%I in ("..") do set "ROOT=%%~fI"
set "VENV=%ROOT%\.venv"
set "VENV_PY=%VENV%\Scripts\python.exe"

echo ==========================================
echo PDF Crusher - Environment Setup
echo ==========================================
echo Project: %ROOT%
echo.

if exist "%VENV_PY%" (
    echo [INFO] Existing virtual environment found.
    "%VENV_PY%" --version
    echo.
    echo If this environment is broken or uses the wrong Python version,
    echo fix it manually rather than overwriting it automatically.
    choice /C YN /M "Continue installing requirements into this environment"
    if errorlevel 2 exit /b 0
) else (
    echo [INFO] Creating virtual environment with Python 3.12...
    py -3.12 -m venv "%VENV%"
    if errorlevel 1 (
        echo.
        echo [ERROR] Could not create the virtual environment.
        echo Check installed Python versions with: py -0p
        pause
        exit /b 1
    )
)

echo.
echo [INFO] Upgrading pip inside the virtual environment...
"%VENV_PY%" -m pip install --upgrade pip
if errorlevel 1 goto :error

echo.
echo [INFO] Installing project requirements...
"%VENV_PY%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto :error

echo.
echo [INFO] Verifying imports...
"%VENV_PY%" -c "import cv2, numpy, PIL, img2pdf, pikepdf, pypdfium2, flask, waitress; print('All required imports succeeded'); print('OpenCV:', cv2.__version__); print('NumPy:', numpy.__version__)"
if errorlevel 1 goto :error

echo.
echo [SUCCESS] PDF Crusher environment is ready.
echo Python: %VENV_PY%
echo.
pause
exit /b 0

:error
echo.
echo [ERROR] Setup failed. Read the error above before retrying.
pause
exit /b 1