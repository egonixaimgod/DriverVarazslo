@echo off
chcp 65001 > nul
REM A szkript mappajabol dolgozunk (a bump_build.py relativ utvonalon nyitja a driver_tool.py-t).
cd /d "%~dp0"

echo ==========================================
echo    DriverVarazslo Build
echo ==========================================
echo.

REM --- Build szam novelese (+1). UGYANAZ a bump_build.py, mint a kiado szkriptben: a helyi,
REM     a GitHubon publikalt es a kiadas-cimkekbol olvasott szam kozul a legnagyobbat emeli,
REM     tehat a szam SOHA nem csokkenhet (az auto-updater csak nagyobb szamra frissit). ---
echo [1/2] Build szam novelese...
python bump_build.py > temp_build.txt
if errorlevel 1 (
    del temp_build.txt 2>nul
    echo [!] A build szam noveles nem sikerult.
    pause
    exit /b 1
)
set /p NEW_BUILD=<temp_build.txt
del temp_build.txt
echo      Uj Build verzio: %NEW_BUILD%
echo.

REM A CLI felulete (rich + questionary, 2026-10-13). Ha hianyzik, a PyInstaller hiba nelkul
REM epitene egy olyan exet, aminek a CLI modja nem indul - ezert elotte ellenorizzuk/telepitjuk.
python -c "import rich, questionary" 2>nul || python -m pip install rich==14.1.0 questionary==2.1.1
python -c "import rich, questionary" || (
    echo [!] A rich / questionary csomag nem telepitheto - a build megall.
    pause
    exit /b 1
)

echo [2/2] Program leforditasa (PyInstaller)...
python -m PyInstaller --clean DriverVarazslo.spec
if %ERRORLEVEL% neq 0 (
    echo.
    echo [!] Hiba a build soran! Megszakitjuk a folyamatot.
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo ==========================================
echo    BUILD KESZ! (Build %NEW_BUILD%)
echo    Nincs commit, push es GitHub-kiadas - azt a
echo    rebuild_verzioszam_novelessel_es_github_pushal.bat csinalja.
echo ==========================================
pause
