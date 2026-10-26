@echo off
setlocal
cd /d "%~dp0"

for %%I in ("..") do set "ROOT=%%~fI"
set "VENV_PY=%ROOT%\.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo [ERROR] Virtual environment not found.
    echo Run install.bat first.
    pause
    exit /b 1
)

"%VENV_PY%" -c "import cv2, numpy, PIL, img2pdf, pikepdf, pypdfium2, flask, waitress" 2>nul
if errorlevel 1 (
    echo [ERROR] Dependencies are missing or broken.
    echo Run install.bat to repair the environment.
    pause
    exit /b 1
)

if not defined PORT set "PORT=18000"

echo Starting PDF Crusher with:
echo %VENV_PY%
echo.

"%VENV_PY%" app.py

pause