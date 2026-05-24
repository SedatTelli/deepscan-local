@echo off
:: ============================================================
:: DeepScan Local — Build Script
:: Designer: Sedat Telli | sedattelli.com
::
:: Requirements:
::   pip install -r requirements.txt   (run once before building)
::
:: Output:
::   dist\DeepScanLocal\DeepScanLocal.exe
:: ============================================================

title DeepScan Local — Build

echo.
echo  =============================================
echo   DeepScan Local  ^|  PyInstaller Build
echo   Designer: Sedat Telli ^| sedattelli.com
echo  =============================================
echo.

:: Verify Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found in PATH.
    echo  Install Python 3.10+ and add it to PATH.
    pause & exit /b 1
)

:: Verify PyInstaller
pyinstaller --version >nul 2>&1
if errorlevel 1 (
    echo  [INFO] Installing PyInstaller...
    pip install pyinstaller==6.3.0
)

:: Clean previous build artefacts
echo  [1/3] Cleaning previous build...
if exist build   rmdir /s /q build
if exist dist    rmdir /s /q dist

:: Run PyInstaller
echo  [2/3] Running PyInstaller (this takes ~1-3 minutes)...
pyinstaller deepscan.spec --clean --noconfirm

if errorlevel 1 (
    echo.
    echo  [ERROR] PyInstaller failed. Check output above.
    pause & exit /b 1
)

:: Copy icon into dist if it was generated on a prior run
if exist "%LOCALAPPDATA%\DeepScanLocal\icon.ico" (
    copy /y "%LOCALAPPDATA%\DeepScanLocal\icon.ico" "dist\DeepScanLocal\" >nul
    echo  [INFO] Icon copied from AppData.
)

echo.
echo  [3/3] Done!
echo.
echo  Executable:  %~dp0dist\DeepScanLocal\DeepScanLocal.exe
echo.
echo  To distribute: copy the entire dist\DeepScanLocal\ folder.
echo  The .exe must stay in that folder alongside its _internal\ directory.
echo.
pause
