@echo off
cd /d "%~dp0"
echo ========================================
echo  Joe Monolith - Windows Installer
echo ========================================
echo.

:: Check for Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python is not installed or not in PATH.
    echo Download from https://www.python.org/downloads/
    pause
    exit /b 1
)

:: Check for uv
uv --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: uv is not installed or not in PATH.
    echo Install with: pip install uv
    echo Or see: https://docs.astral.sh/uv/getting-started/installation/
    pause
    exit /b 1
)

:: Install dependencies
echo Installing Python dependencies via uv...
set UV_PYTHON_PREFERENCE=only-system
uv sync
if %errorlevel% neq 0 (
    echo ERROR: uv sync failed.
    pause
    exit /b 1
)

echo.
echo ========================================
echo  Installation complete!
echo ========================================
echo.
echo Usage:
echo   Double-click Joe.vbs or run: uv run python joe.py
echo.
echo Say "Hi/Hey/Aye Joe/John" to activate, Ctrl+T to stop.
echo.
echo For CUDA GPU detection, optionally install PyTorch:
echo   uv pip install torch --index-url https://download.pytorch.org/whl/cu124
echo.
pause
