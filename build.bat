@echo off
chcp 65001 > nul

echo ==========================================
echo    DriverVarazslo Build
echo ==========================================
echo.

python -m PyInstaller --clean DriverVarazslo.spec
if %ERRORLEVEL% neq 0 (
    echo.
    echo [!] Hiba a build soran! Megszakitjuk a folyamatot.
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo ==========================================
echo    BUILD KESZ!
echo ==========================================
pause