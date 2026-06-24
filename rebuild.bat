@echo off
setlocal enabledelayedexpansion
chcp 65001 > nul

echo ==========================================
echo    DriverVarazslo Auto Rebuild ^& Release
echo ==========================================
echo.

echo [1/3] Build szam novelese...
python -c "import re; f=open('driver_tool.py','r',encoding='utf-8'); c=f.read(); f.close(); m=re.search(r'^BUILD_NUMBER\s*=\s*(\d+)', c, re.M); nb=int(m.group(1))+1; c=c[:m.start(1)]+str(nb)+c[m.end(1):]; f=open('driver_tool.py','w',encoding='utf-8'); f.write(c); f.close(); print(nb)" > temp_build.txt
set /p NEW_BUILD=<temp_build.txt
del temp_build.txt
echo Uj Build verzio: %NEW_BUILD%

echo.
echo [2/3] Program leforditasa (PyInstaller)...
python -m PyInstaller --clean DriverVarazslo.spec
if %ERRORLEVEL% neq 0 (
    echo.
    echo [!] Hiba a build soran! Megszakitjuk a folyamatot.
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo [3/3] Feltoltes a GitHubra (Git Push)...
git add .
git add -f dist/DriverVarazslo.exe
git commit -m "Release: Build %NEW_BUILD% (Auto-Build)"
git push

echo.
echo ==========================================
echo    SIKERES KIADAS: Build %NEW_BUILD%
echo ==========================================
pause
