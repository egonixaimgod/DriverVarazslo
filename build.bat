@echo off
chcp 65001 > nul
REM A szkript mappajabol dolgozunk (a bump_build.py relativ utvonalon nyitja a driver_tool.py-t).
cd /d "%~dp0"

echo ==========================================
echo    DriverVarazslo Build
echo ==========================================
echo.

REM --- Fut-e meg a program? (2026-11-26, a kiado szkript mintajara.) Ha igen, a
REM     dist\DriverVarazslo.exe zarolva van, es a PyInstaller a build VEGEN
REM     "PermissionError: [WinError 5] Access is denied"-del all meg (terepen igy tortent).
REM     A build szam emelese ELOTT nezzuk, hogy egy be nem zart program miatt ne vesszen
REM     el szam. goto-val, mert ebben a szkriptben nincs delayed expansion (a "[!]" es a
REM     "KESZ!" feliratokbol eltunne a felkialtojel). A program UAC-vel rendszergazdakent
REM     fut: ha a sima taskkill nem eleg, emelt joggal (UAC ablak) probaljuk ujra, es
REM     mindig visszaolvasunk. A ping = varakozas (a `timeout` ^< nul alatt nem mukodik).
tasklist /fi "imagename eq DriverVarazslo.exe" 2>nul | find /i "DriverVarazslo.exe" >nul
if errorlevel 1 goto :nincs_futo
echo [FIGYELEM] A DriverVarazslo.exe fut, ezert a dist mappa nem irhato felul.
set "VALASZ="
set /p VALASZ="Bezarjam most? [i/n] "
if /i not "%VALASZ%"=="i" goto :nem_zarjuk
taskkill /f /t /im "DriverVarazslo.exe" >nul 2>&1
ping -n 3 127.0.0.1 >nul
tasklist /fi "imagename eq DriverVarazslo.exe" 2>nul | find /i "DriverVarazslo.exe" >nul
if errorlevel 1 goto :bezarva
echo      A program rendszergazdakent fut - a bezarashoz UAC ablak jon, nyomj Igent.
powershell -NoProfile -Command "Start-Process taskkill -ArgumentList '/f','/t','/im','DriverVarazslo.exe' -Verb RunAs -Wait -WindowStyle Hidden" >nul 2>&1
ping -n 3 127.0.0.1 >nul
tasklist /fi "imagename eq DriverVarazslo.exe" 2>nul | find /i "DriverVarazslo.exe" >nul
if errorlevel 1 goto :bezarva
echo [!] A DriverVarazslo.exe NEM zarult be (UAC elutasitva?). Zard be kezzel, es inditsd ujra ezt a szkriptet.
pause
exit /b 1
:nem_zarjuk
echo Zard be a programot, es inditsd ujra ezt a szkriptet.
pause
exit /b 1
:bezarva
echo      Bezarva.
echo.
:nincs_futo

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
