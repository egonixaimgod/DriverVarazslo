@echo off
setlocal enabledelayedexpansion
chcp 65001 > nul
set NEW_BUILD=999
echo --- teszt: gh release view egy NEM letezo cimkere ---
gh release view "build-%NEW_BUILD%" >nul 2>&1
echo errorlevel a view utan: %errorlevel%
if %errorlevel%==0 (
    echo AG: mar letezik
) else (
    echo AG: letrehozas - a kovetkezo sor az, ami a valodi szkriptben fut:
    echo.gh release create "build-%NEW_BUILD%" "dist/DriverVarazslo.exe" ^
        --title "DriverVarazslo - build %NEW_BUILD%" ^
        --notes "Automatikus kiadas. Build %NEW_BUILD%."
)
echo --- vege ---
