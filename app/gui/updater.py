"""DriverVarázsló GUI - In-app auto-updater: BUILD_NUMBER ellenőrzés GitHubról + exe csere."""

# === AUTO-IMPORTS ===
import os
import subprocess
import re
import time
import logging
import winreg
from app import common
from app.common import _app_exe_path
# === /AUTO-IMPORTS ===


class GuiUpdaterMixin:
    """In-app auto-updater: BUILD_NUMBER ellenőrzés GitHubról + exe csere. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def check_for_updates(self):
        """Update-ellenőrzés a GitHub-on lévő driver_tool.py BUILD_NUMBER-je alapján.
        Legfeljebb UPDATE_CHECK_ATTEMPTS-szor próbálkozik, próbálkozások közt
        UPDATE_CHECK_RETRY_SEC másodperc szünettel - a raw.githubusercontent.com egy
        Fastly CDN mögött fut, aminek edge-cache-e egy friss push után még percekig a
        RÉGI tartalmat adhatja vissza (a lekérésbe rakott ?t=<timestamp> csak a
        kliens/böngésző-oldali cache-t kerüli meg, a CDN edge-cache-ét nem) - emiatt
        közvetlenül egy rebuild.bat futtatása utáni azonnali indításkor az első próbálkozás
        könnyen a még nem frissült verziót kaphatja vissza. Ez a retry nem garantált fix
        (a CDN-lag néha percekben mérhető, ennél tovább a felhasználót nem várakoztatjuk
        induláskor), de a gyakori pár-másodperces eseteket lefedi."""
        logging.info("[UPDATE] check_for_updates()")
        import urllib.request
        import ssl
        ssl_ctx = ssl.create_default_context()
        UPDATE_CHECK_ATTEMPTS = 3
        UPDATE_CHECK_RETRY_SEC = 3
        for attempt in range(1, UPDATE_CHECK_ATTEMPTS + 1):
            try:
                # Bypassing GitHub cache with a timestamp
                url = f"https://raw.githubusercontent.com/egonixaimgod/DriverVarazslo/main/driver_tool.py?t={int(time.time())}"
                logging.info(f"[UPDATE] Update ellenőrzése erről a címről ({attempt}/{UPDATE_CHECK_ATTEMPTS}. próbálkozás): {url}")
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
                with urllib.request.urlopen(req, context=ssl_ctx, timeout=10) as resp:
                    content = resp.read().decode('utf-8')
                m = re.search(r'^BUILD_NUMBER\s*=\s*(\d+)', content, re.MULTILINE)
                if m:
                    new_build = int(m.group(1))
                    logging.info(f"[UPDATE] Letöltött BUILD_NUMBER: {new_build}, Helyi: {common.BUILD_NUMBER}")
                    if new_build > common.BUILD_NUMBER:
                        logging.info(f"[UPDATE] Új verzió elérhető: {new_build} (Jelenlegi: {common.BUILD_NUMBER})")
                        return {'has_update': True, 'new_version': new_build}
                    else:
                        logging.info("[UPDATE] Nincs újabb verzió.")
                else:
                    logging.error("[UPDATE] Nem található BUILD_NUMBER a letöltött fájlban!")
            except Exception:
                logging.error(f"[UPDATE] Ellenőrzési hiba ({attempt}/{UPDATE_CHECK_ATTEMPTS}. próbálkozás):", exc_info=True)
            if attempt < UPDATE_CHECK_ATTEMPTS:
                time.sleep(UPDATE_CHECK_RETRY_SEC)
        return {'has_update': False}

    def perform_update(self):
        logging.info("[UPDATE] perform_update indítása...")
        def worker():
            try:
                self.emit('task_start', {'task': 'update', 'title': 'Program Frissítése'})
                self.emit('task_progress', {'task': 'update', 'log': 'Új verzió letöltése GitHubról...', 'indeterminate': True})
                import tempfile
                import urllib.request
                import ssl
                import shutil
                ssl_ctx = ssl.create_default_context()

                exe_url = f"https://raw.githubusercontent.com/egonixaimgod/DriverVarazslo/main/dist/DriverVarazslo.exe?t={int(time.time())}"
                # WinPE-ben a %TEMP% az X: RAM-diskre mutat - a letöltött exe-t a valódi C: meghajtóra tesszük.
                is_pe = os.environ.get('SystemDrive', 'C:') == 'X:'
                if is_pe:
                    temp_dir = r'C:\DV_Temp'
                    os.makedirs(temp_dir, exist_ok=True)
                else:
                    temp_dir = tempfile.gettempdir()
                new_exe = os.path.join(temp_dir, f"DriverVarazslo_Update_{int(time.time())}.exe")
                
                logging.info(f"[UPDATE] EXE letöltése innen: {exe_url}")
                logging.info(f"[UPDATE] Cél fájl: {new_exe}")
                
                req = urllib.request.Request(exe_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
                with urllib.request.urlopen(req, context=ssl_ctx, timeout=60) as resp, open(new_exe, 'wb') as f:
                    shutil.copyfileobj(resp, f)
                    
                downloaded_size = os.path.getsize(new_exe)
                logging.info(f"[UPDATE] EXE letöltve. Fájlméret: {downloaded_size} byte.")
                if downloaded_size < 1000000: # Ha kevesebb mint 1 MB, valószínűleg nem jó a letöltés
                    logging.warning("[UPDATE] A letöltött fájl gyanúsan kicsi! Lehet, hogy hiba történt vagy 404 oldalt töltött le.")
                
                self.emit('task_progress', {'task': 'update', 'log': '✅ Letöltés kész! A program frissítése és újraindítása következik...'})
                time.sleep(2)
                
                current_exe = _app_exe_path()
                logging.info(f"[UPDATE] Jelenlegi futtatható fájl: {current_exe}")
                
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as key:
                        desktop_dir, _ = winreg.QueryValueEx(key, "Desktop")
                except Exception:
                    desktop_dir = os.path.join(os.environ.get('USERPROFILE', 'C:\\'), 'Desktop')
                    
                desktop_exe = os.path.join(desktop_dir, "DriverVarazslo.exe")
                logging.info(f"[UPDATE] Asztali elérési út: {desktop_exe}")
                
                bat_path = os.path.join(temp_dir, f"dv_update_{int(time.time())}.bat")
                # A ".old" biztonsági másolatokat sosem olvassuk vissza (nincs rollback funkció),
                # kizárólag a következő indításkori törlésre szolgálnak - ha viszont a program
                # legközelebb egy MÁSIK elérési útról indul (pl. nem az asztalról), a __init__-beli
                # takarítás sosem találja meg és törli őket, és örökre a lemezen maradnak. Ezért itt,
                # helyben (és csak sikeres másolás esetén) rögtön eltávolítjuk mindkettőt.
                bat_content = f"""@echo off
set _MEIPASS2=
set _MEIPASS=
set _PYIBoot_Pkg_ID=
timeout /t 3 /nobreak > nul

if /I not "{current_exe}"=="{desktop_exe}" (
    move /y "{current_exe}" "{current_exe}.old" > nul 2>&1
    copy /y "{new_exe}" "{current_exe}" > nul 2>&1
    if not errorlevel 1 del /f /q "{current_exe}.old" > nul 2>&1
)

move /y "{desktop_exe}" "{desktop_exe}.old" > nul 2>&1
copy /y "{new_exe}" "{desktop_exe}" > nul 2>&1
if not errorlevel 1 del /f /q "{desktop_exe}.old" > nul 2>&1

start "" "{desktop_exe}"
del "%~f0"
"""
                logging.info(f"[UPDATE] .bat fájl írása: {bat_path}")
                with open(bat_path, 'w', encoding='utf-8') as f:
                    f.write(bat_content)

                env = os.environ.copy()
                keys_to_remove = [k for k in env.keys() if k.startswith('_MEI') or k.startswith('_PYI')]
                for k in keys_to_remove:
                    env.pop(k, None)
                
                logging.info("[UPDATE] .bat fájl elindítása és program bezárása...")
                subprocess.Popen(["cmd.exe", "/c", bat_path],
                                 creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NO_WINDOW,
                                 env=env)
                os._exit(0)
            except Exception as e:
                logging.error(f"[UPDATE] Hiba a letöltés/frissítés során:", exc_info=True)
                self.emit('task_error', {'task': 'update', 'error': str(e)})
        self._safe_thread('update', worker)
