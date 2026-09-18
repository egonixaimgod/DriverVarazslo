@echo off
setlocal enabledelayedexpansion
chcp 65001 > nul

cd /d "%~dp0"

echo ==========================================
echo    DriverVarazslo Auto Rebuild ^& Release
echo ==========================================
echo.

REM --- Fut-e meg a program? Ha igen, a dist\DriverVarazslo.exe zarolva van, es a
REM     PyInstaller nem tudja felulirni (a build ilyenkor a REGI exe-t hagyna ott). ---
tasklist /fi "imagename eq DriverVarazslo.exe" 2>nul | find /i "DriverVarazslo.exe" >nul
if %errorlevel%==0 (
    echo [FIGYELEM] A DriverVarazslo.exe fut, ezert a dist mappa nem irhato felul.
    set /p VALASZ="Bezarjam most? [i/n] "
    if /i "!VALASZ!"=="i" (
        taskkill /f /im "DriverVarazslo.exe" >nul 2>&1
        echo      Bezarva.
    ) else (
        echo Zard be a programot, es inditsd ujra ezt a szkriptet.
        pause
        exit /b 1
    )
)

echo [1/4] Build szam novelese...
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
echo [2/4] Program leforditasa (PyInstaller)...
python -m PyInstaller --clean --noconfirm DriverVarazslo.spec
if %ERRORLEVEL% neq 0 (
    echo.
    echo [!] Hiba a build soran! Megszakitjuk a folyamatot.
    pause
    exit /b %ERRORLEVEL%
)
if not exist "dist\DriverVarazslo.exe" (
    echo [!] A build lefutott, de a dist\DriverVarazslo.exe nem jott letre.
    pause
    exit /b 1
)

echo.
echo [3/4] Feltoltes a GitHubra (Git Push)...
REM A push NEM hagyhato ki es nem bukhat el eszrevetlenul: a MAR KIADOTT (Build 310 es
REM alatti) peldanyok kizarolag a raw.githubusercontent.com-ot nezik, vagyis szamukra a
REM driver_tool.py BUILD_NUMBER-e es a dist/DriverVarazslo.exe JELENTI a frissitest.
git add .
git add -f dist/DriverVarazslo.exe
git commit -m "Release: Build %NEW_BUILD% (Auto-Build)"
git push
if %ERRORLEVEL% neq 0 (
    echo.
    echo [!] A git push nem sikerult - a kiadas felbeszakadt.
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo [4/4] GitHub Release keszitese (build-%NEW_BUILD%)...
REM EZ AZ, AMI A FRISSITEST AZONNALIVA TESZI. A program ELOSZOR a GitHub Releases
REM API-t kerdezi (api.github.com), ami a kiadas letrehozasa utan rogton latszik -
REM mig a raw.githubusercontent.com egy Fastly CDN mogott ul `Cache-Control: max-age=300`
REM fejleccel, vagyis a pusholt fajlt akar 5 percig a REGI valtozataban szolgalja ki.
REM
REM EZ A LEPES MAR KIMARADT EGYSZER, ES A SZKRIPT ERROL NEM SZOLT (2026-09-18, meres):
REM a build-314 es a build-315 kiadas nem jott letre, a szkript viszont ugyanugy kiirta
REM a nagy "SIKERES KIADAS" keretet - a `[!]` sor pedig par sorral feljebb elveszett.
REM Terepi kovetkezmeny: egy 313-as gep nem talalta meg a 315-ot. HAROM JAVITAS:
REM   1) a gh kimenetet NEM nyeljuk el (a `>nul 2>&1` csak a letezes-ellenorzesen van),
REM   2) a kiadas utan VISSZAELLENORIZZUK, hogy tenyleg letrejott-e (a gh visszateresi
REM      kodja onmagaban keves - ugyanaz az elv, mint a programban: a verdikt a
REM      visszaolvasas, nem a returncode),
REM   3) a zaro keret az IGAZAT mondja: ha a kiadas kimaradt, azt nagybetuvel kiirjuk.
set "RELEASE_OK=0"
where gh >nul 2>&1
if %errorlevel% neq 0 (
    echo      [HIBA] A "gh" parancs nem talalhato - a kiadas KIMARAD.
    echo          Telepites: winget install GitHub.cli   majd:  gh auth login
    goto ellenorzes
)

REM A bejelentkezes ellenorzese kulon: lejart/elerhetetlen token eseten a `gh release
REM create` olyan hibaval bukik, ami a kimenetben elveszne.
gh auth status >nul 2>&1
if %errorlevel% neq 0 (
    echo      [HIBA] A "gh" nincs bejelentkezve - a kiadas KIMARAD.
    echo          Javitas:  gh auth login
    goto ellenorzes
)

gh release view "build-%NEW_BUILD%" >nul 2>&1
if %errorlevel%==0 (
    echo      A kiadas mar letezik - az exe felulirasa...
    gh release upload "build-%NEW_BUILD%" "dist/DriverVarazslo.exe" --clobber
) else (
    gh release create "build-%NEW_BUILD%" "dist/DriverVarazslo.exe" --title "DriverVarazslo - build %NEW_BUILD%" --notes "Automatikus kiadas. Build %NEW_BUILD%."
)

:ellenorzes
REM VISSZAOLVASAS: tenyleg ott van-e a kiadas a GitHubon? Ez a verdikt, nem a returncode.
where gh >nul 2>&1
if %errorlevel%==0 (
    gh release view "build-%NEW_BUILD%" >nul 2>&1
    if !errorlevel!==0 set "RELEASE_OK=1"
)

echo.
if "!RELEASE_OK!"=="1" (
    echo ==========================================
    echo    SIKERES KIADAS: Build %NEW_BUILD%
    echo    Kiadas: build-%NEW_BUILD%  ^(a frissites azonnal lathato^)
    echo ==========================================
) else (
    echo ==========================================
    echo    Build %NEW_BUILD% FELTOLTVE, DE A KIADAS KIMARADT!
    echo ==========================================
    echo   A kod es az exe fent van a GitHubon ^(a push sikerult^), tehat a
    echo   frissites mukodik - de a "build-%NEW_BUILD%" KIADAS nem jott letre.
    echo.
    echo   Mit jelent ez: a gepek a lassabb, CDN-cache-elt raw uton ertesulnek
    echo   a frissitesrol, tehat ~5 perc keses lehet. A program ezt eszreveszi
    echo   es a naplojaba WARNING-ot ir.
    echo.
    echo   Potlas kezzel:
    echo     gh release create "build-%NEW_BUILD%" "dist/DriverVarazslo.exe" --title "DriverVarazslo - build %NEW_BUILD%" --notes "Automatikus kiadas."
    echo.
    echo   A fenti gh-kimenetben ott a pontos hibauzenet - olvasd el.
)
pause
endlocal
