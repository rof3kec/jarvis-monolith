@echo off
echo ========================================
echo  Jarvis Monolith - Windows Installer
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
echo   uv run python jarvis.py
echo.
echo Hold Ctrl+T to record, release to transcribe and paste.
echo.
echo For CUDA acceleration, also install PyTorch with CUDA:
echo   pip install torch --index-url https://download.pytorch.org/whl/cu121
echo.
pause
