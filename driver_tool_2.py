import ctypes
import os
import sys
import subprocess
import re
import threading
import time
import logging
import shutil
import json
import glob
import traceback
import winreg
import queue
from datetime import datetime

BUILD_NUMBER = 88

try:
    import webview
except ImportError:
    print("HIBA: pywebview nem található! Telepítsd: pip install pywebview")
    sys.exit(1)

# pywebview 6.x deprecation compat
try:
    _FOLDER_DIALOG = webview.FileDialog.FOLDER
    _OPEN_DIALOG = webview.FileDialog.OPEN
except AttributeError:
    _FOLDER_DIALOG = webview.FOLDER_DIALOG
    _OPEN_DIALOG = webview.OPEN_DIALOG

# WebView2 init state (watchdog)
_webview_ready = threading.Event()
_webview_error = threading.Event()

# WebView2 minimum verzió ellenőrzés (ICoreWebView2Environment10 interface min v109 kell)
MIN_WEBVIEW2_MAJOR = 109

def check_webview2_runtime():
    """
    Ellenőrzi, hogy a WebView2 Runtime telepítve van-e és megfelelő verzió-e.
    Visszatérési értékek:
        (True, verzió_string) - OK
        (False, hibaüzenet) - Hiba
    """
    version = None
    
    # 1. Önálló WebView2 Runtime telepítések (EdgeUpdate registry)
    edgeupdate_paths = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
    ]
    for hive, path in edgeupdate_paths:
        try:
            with winreg.OpenKey(hive, path) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
                if version and version != "0.0.0.0":
                    break
        except (FileNotFoundError, OSError):
            continue
    
    # 2. Edge beépített WebView2 (Windows 11 / Edge-be integrált)
    if not version:
        edge_webview_paths = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeWebView\BLBeacon"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeWebView\BLBeacon"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\EdgeWebView\BLBeacon"),
        ]
        for hive, path in edge_webview_paths:
            try:
                with winreg.OpenKey(hive, path) as key:
                    version, _ = winreg.QueryValueEx(key, "version")
                    if version:
                        break
            except (FileNotFoundError, OSError):
                continue
    
    # 3. Edge böngésző verzió (fallback - ha WebView2 nincs külön regisztrálva)
    if not version:
        edge_paths = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Edge\BLBeacon"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Edge\BLBeacon"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Edge\BLBeacon"),
        ]
        for hive, path in edge_paths:
            try:
                with winreg.OpenKey(hive, path) as key:
                    version, _ = winreg.QueryValueEx(key, "version")
                    if version:
                        break
            except (FileNotFoundError, OSError):
                continue
    
    # 4. Utolsó esély: GetAvailableCoreWebView2BrowserVersionString (ha van WebView2Loader.dll)
    if not version:
        try:
            wv2_loader = ctypes.windll.LoadLibrary("WebView2Loader.dll")
            buf = ctypes.create_unicode_buffer(256)
            hr = wv2_loader.GetAvailableCoreWebView2BrowserVersionString(None, ctypes.byref(buf))
            if hr == 0 and buf.value:
                version = buf.value
        except Exception as e:
            logging.debug(e)
    
    if not version:
        return (False, "WebView2 Runtime nem található!\n\n"
                       "A program működéséhez telepíteni kell:\n"
                       "https://go.microsoft.com/fwlink/p/?LinkId=2124703\n\n"
                       "(Evergreen Bootstrapper)")
    
    # Verzió parsing: pl. "109.0.1518.61" -> 109
    try:
        major = int(version.split('.')[0])
    except (ValueError, IndexError):
        major = 0
    
    if major < MIN_WEBVIEW2_MAJOR:
        return (False, f"WebView2 Runtime túl régi! (v{version})\n\n"
                       f"Minimum v{MIN_WEBVIEW2_MAJOR}.x szükséges.\n\n"
                       "Frissítsd itt:\n"
                       "https://go.microsoft.com/fwlink/p/?LinkId=2124703")
    
    return (True, version)


def show_webview2_error(message):
    """MessageBox megjelenítése WebView2 hibáról, majd program kilépés."""
    try:
        import webbrowser
        MB_ICONERROR = 0x10
        MB_TOPMOST = 0x40000
        result = ctypes.windll.user32.MessageBoxW(
            None,
            message + "\n\nMegnyissam a letöltési oldalt?",
            "DriverDoktor - WebView2 hiba",
            0x4 | MB_ICONERROR | MB_TOPMOST  # MB_YESNO
        )
        if result == 6:  # IDYES
            webbrowser.open("https://go.microsoft.com/fwlink/p/?LinkId=2124703")
    except Exception as e:
        logging.debug(e)
    sys.exit(1)


# Suppress noisy PIL/Pillow debug logging
logging.getLogger('PIL').setLevel(logging.WARNING)
logging.getLogger('PIL.PngImagePlugin').setLevel(logging.WARNING)




def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False


def resource_path(relative_path):
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


class DriverToolApi:
    def __init__(self):
        logging.info("[INIT] DriverToolApi inicializálás...")
        self._window = None
        self.target_os_path = None
        self.sys_drive = os.environ.get('SystemDrive', 'C:') + '\\'
        self.hw_updates_pool = []
        self._hw_installed_devs = []
        self._hw_scanning = False
        self._hw_loaded = False
        self.wu_api_mode = True
        self._cancel_flag = False  # Flag for cancelling long-running tasks
        self._si = subprocess.STARTUPINFO()
        self._si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        self._nw = subprocess.CREATE_NO_WINDOW
        logging.info(f"[INIT] sys_drive={self.sys_drive}")
        logging.info("[INIT] DriverToolApi kész.")

    def set_window(self, window):
        logging.info("[WINDOW] WebView ablak beállítása...")
        self._window = window
        # Wait for WebView2 DOM to be ready (max 12s, watchdog: 15s)
        dom_ready = False
        for i in range(120):  # 120 * 0.1s = 12s
            try:
                if self._window and self._window.evaluate_js('1+1') == 2:
                    logging.info(f"[WINDOW] WebView2 DOM kész ({i+1} próba után, {(i+1)*0.1:.1f}s)")
                    dom_ready = True
                    _webview_ready.set()
                    break
            except Exception as e:
                if i == 119:
                    logging.warning(f"[WINDOW] WebView2 DOM nem reagál: {e}")
            time.sleep(0.1)
        if not dom_ready:
            logging.error("[WINDOW] WebView2 init sikertelen, watchdog átveszi...")
            _webview_error.set()

    def emit(self, event, data=None):
        # Log minden emit event-et
        try:
            if isinstance(data, dict):
                log_msg = data.get('log') or data.get('status') or data.get('error') or data.get('phase')
                if log_msg:
                    logging.info(f"[EMIT:{event}] {str(log_msg).strip()}")
                else:
                    # Log egyéb data mezőket is
                    logging.debug(f"[EMIT:{event}] data={json.dumps(data, ensure_ascii=False, default=str)[:200]}")
            else:
                logging.debug(f"[EMIT:{event}] data={data}")
        except Exception as e:
            logging.warning(f"[EMIT] Logging hiba: {e}")

        if self._window:
            payload = None
            try:
                payload = json.dumps({"event": event, "data": data}, ensure_ascii=False, default=str)
                self._window.evaluate_js(f'window.handlePyEvent({payload})')
            except Exception as e:
                if 'NoneType' in str(e) and payload:
                    logging.warning(f"[EMIT:{event}] Window None, újrapróbálás...")
                    time.sleep(0.5)
                    try:
                        self._window.evaluate_js(f'window.handlePyEvent({payload})')
                    except Exception as e2:
                        logging.error(f"[EMIT:{event}] Újrapróbálás sikertelen: {e2}")
                elif payload is None:
                    logging.error(f"[EMIT:{event}] JSON serializálási hiba: {e}")
                else:
                    logging.error(f"[EMIT:{event}] Hiba: {e}")

    def _run(self, cmd, **kwargs):
        # Log minden parancs futtatását
        cmd_str = cmd if isinstance(cmd, str) else ' '.join(str(c) for c in cmd)
        logging.debug(f"[CMD] Futtatás: {cmd_str[:300]}")
        start = time.time()
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, errors='replace',
                                  startupinfo=self._si, creationflags=self._nw, **kwargs)
            elapsed = time.time() - start
            # Log eredmény
            if result.returncode != 0:
                logging.warning(f"[CMD] Visszatérési kód: {result.returncode} ({elapsed:.1f}s)")
                if result.stderr:
                    logging.warning(f"[CMD] stderr: {result.stderr[:4000]}")
            else:
                logging.debug(f"[CMD] OK ({elapsed:.1f}s)")
            
            # Log teljes kimenet 4000 karakterig
            if result.stdout:
                out_txt = result.stdout.strip()
                if len(out_txt) > 4000: out_txt = out_txt[:4000] + '... [TRUNCATED]'
                logging.debug(f"[CMD] stdout: {out_txt}")
            return result
        except Exception as e:
            logging.error(f"[CMD] Kivétel: {e}")
            raise

    def _safe_thread(self, task, target):
        def wrapper():
            logging.info(f"[THREAD:{task}] Háttérszál indul...")
            start_time = time.time()
            try:
                target()
                elapsed = time.time() - start_time
                logging.info(f"[THREAD:{task}] Befejezve ({elapsed:.1f}s)")
            except Exception as e:
                elapsed = time.time() - start_time
                logging.error(f"[THREAD:{task}] HIBA ({elapsed:.1f}s): {e}")
                logging.error(f"[THREAD:{task}] Traceback:\n{traceback.format_exc()}")
                self.emit('task_error', {'task': task, 'error': str(e)})
                self.emit('task_complete', {'task': task, 'status': f'❌ Hiba: {e}'})
        threading.Thread(target=wrapper, daemon=True).start()

    # ================================================================
    # GENERAL
    # ================================================================

    def js_log(self, level, msg):
        # UI-bol jovo nyers JavaScript logok kozvetitess
        level = str(level).upper()
        if level == 'ERROR': log_lvl = logging.ERROR
        elif level == 'WARN' or level == 'WARNING': log_lvl = logging.WARNING
        elif level == 'DEBUG': log_lvl = logging.DEBUG
        else: log_lvl = logging.INFO
        logging.log(log_lvl, f"[JS_UI] {msg}")

    def get_init_data(self):
        logging.info(f"[API] get_init_data() hívás - build={BUILD_NUMBER}, target={self.target_os_path}")
        return {'build': BUILD_NUMBER, 'sys_drive': self.sys_drive, 'target_os': self.target_os_path}

    def reboot_system(self):
        logging.info("[API] reboot_system() - Felhasználó újraindítást kért")
        self._run(['shutdown', '/r', '/t', '0', '/f'])
        return True

    def cancel_task(self):
        """API hívás a hosszan tartó műveletek (pl. törlés) megszakítására."""
        logging.warning("[API] cancel_task() — Felhasználó megszakítást kért!")
        self._cancel_flag = True
        self.emit('toast', {'message': '⚠️ Megszakítás kérve...', 'type': 'warning'})
        return True

    def _check_cancel(self):
        """Ellenőrzi, hogy a felhasználó megszakította-e a műveletet."""
        if self._cancel_flag:
            logging.info("[CANCEL] Megszakítás flag aktiv!")
            return True
        return False

    def change_target_os(self):
        logging.info("[API] change_target_os() hívás")
        result = self._window.create_file_dialog(_FOLDER_DIALOG, allow_multiple=False)
        if result and len(result) > 0:
            d = os.path.abspath(result[0]).replace("/", "\\")
            has_win = os.path.exists(os.path.join(d, "Windows"))
            logging.info(f"[API] change_target_os: kiválasztva={d}, has_windows={has_win}")
            return {'path': d, 'has_windows': has_win}
        logging.info("[API] change_target_os: mégse")
        return None

    def apply_target_os(self, path):
        logging.info(f"[API] apply_target_os({path})")
        self.target_os_path = path
        return True

    def reset_target_os(self):
        logging.info("[API] reset_target_os() - visszatérés jelenlegi rendszerre")
        self.target_os_path = None
        return True

    def select_directory(self, title='Válassz mappát'):
        logging.info(f"[API] select_directory(title={title})")
        result = self._window.create_file_dialog(_FOLDER_DIALOG, allow_multiple=False)
        if result and len(result) > 0:
            logging.info(f"[API] select_directory: kiválasztva={result[0]}")
            return result[0]
        logging.info("[API] select_directory: mégse")
        return None

    def select_file(self, title='Válassz fájlt', file_types=''):
        logging.info(f"[API] select_file(title={title}, types={file_types})")
        ft = (file_types.split('|')[0],) if file_types else ()
        result = self._window.create_file_dialog(_OPEN_DIALOG, allow_multiple=False, file_types=ft)
        if result and len(result) > 0:
            logging.info(f"[API] select_file: kiválasztva={result[0]}")
            return result[0]
        logging.info("[API] select_file: mégse")
        return None

    # ================================================================
    # DRIVER LISTING
    # ================================================================
    def load_drivers(self, all_drivers=False):
        logging.info(f"[API] load_drivers(all_drivers={all_drivers})")
        def worker():
            self.emit('drivers_loading')
            start = time.time()
            try:
                if self.target_os_path:
                    logging.info(f"[DRIVERS] Offline mód: {self.target_os_path}")
                    drivers = self._get_offline_drivers(all_drivers)
                elif all_drivers:
                    logging.info("[DRIVERS] Összes driver lekérdezés (élő rendszer)")
                    drivers = self._get_all_drivers()
                else:
                    logging.info("[DRIVERS] Third-party driverek lekérdezés")
                    drivers = self._get_third_party_drivers()
                elapsed = time.time() - start
                logging.info(f"[DRIVERS] Betöltve: {len(drivers)} driver ({elapsed:.1f}s)")
                self.emit('drivers_loaded', {'drivers': drivers, 'elapsed': round(elapsed, 1)})
            except Exception as e:
                logging.error(f"[DRIVERS] Betöltési hiba: {e}")
                logging.error(traceback.format_exc())
                self.emit('drivers_loaded', {'drivers': [], 'elapsed': 0, 'error': str(e)})
        threading.Thread(target=worker, daemon=True).start()

    def _get_third_party_drivers(self):
        logging.debug("[DRIVERS] dism /English /Online /Get-Drivers futtatása...")
        res = self._run(['dism', '/English', '/Online', '/Get-Drivers'])
        drivers = []
        current = {}
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                if current and "published" in current:
                    drivers.append(current)
                    current = {}
                continue
            parts = line.split(":", 1)
            if len(parts) == 2:
                key, val = parts[0].strip(), parts[1].strip()
                if "Published Name" in key:
                    current["published"] = val
                elif "Original File Name" in key:
                    current["original"] = val
                elif "Provider Name" in key:
                    current["provider"] = val
                elif "Class Name" in key:
                    current["class"] = val
                elif "Date and Version" in key:
                    current["version"] = val
        if current and "published" in current:
            drivers.append(current)
        return drivers

    def _get_all_drivers(self):
        logging.debug("[DRIVERS] _get_all_drivers() indult")
        cmd = ['powershell', '-NoProfile', '-Command',
               '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Get-WindowsDriver -Online -All | Select-Object ProviderName, ClassName, Version, Driver, OriginalFileName | ConvertTo-Json -Depth 2 -WarningAction SilentlyContinue']
        res = self._run(cmd, encoding='utf-8')
        out = res.stdout.strip()
        if not out:
            logging.debug("[DRIVERS] _get_all_drivers: üres kimenet")
            return []
        data = json.loads(out)
        if isinstance(data, dict):
            data = [data]
        parsed_drivers = [{"published": d.get("Driver", ""), "original": d.get("OriginalFileName", ""),
                 "provider": d.get("ProviderName", ""), "class": d.get("ClassName", ""),
                 "version": d.get("Version", "")} for d in data]

        # Filter ghosts (force-deleted inbox drivers)
        valid_drivers = []
        rep = os.path.join(os.environ.get('SYSTEMROOT', r'C:\Windows'), "System32", "DriverStore", "FileRepository")
        for d in parsed_drivers:
            pub = d.get("published", "")
            if not pub:
                continue
            if pub.lower().startswith("oem"):
                valid_drivers.append(d)
                continue
            if glob.glob(os.path.join(rep, f"{pub}_*")):
                valid_drivers.append(d)

        logging.debug(f"[DRIVERS] _get_all_drivers: {len(valid_drivers)} valid driver")
        return valid_drivers

    def _get_offline_drivers(self, all_drivers=False):
        logging.debug(f"[DRIVERS] _get_offline_drivers(all_drivers={all_drivers})")
        cmd = ['dism', '/English', f'/Image:{self.target_os_path}', '/Get-Drivers']
        if all_drivers:
            cmd.append('/all')
        res = self._run(cmd)
        drivers = []
        current = {}
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                if current and "published" in current:
                    drivers.append(current)
                    current = {}
                continue
            parts = line.split(":", 1)
            if len(parts) == 2:
                key, val = parts[0].strip(), parts[1].strip()
                if "Published Name" in key:
                    current["published"] = val
                elif "Original File Name" in key:
                    current["original"] = val
                elif "Provider Name" in key:
                    current["provider"] = val
                elif "Class Name" in key:
                    current["class"] = val
                elif "Date and Version" in key:
                    current["version"] = val
        if current and "published" in current:
            drivers.append(current)

        # Filter ghosts (force-deleted inbox drivers)
        valid_drivers = []
        rep = os.path.join(self.target_os_path, "Windows", "System32", "DriverStore", "FileRepository")
        for d in drivers:
            pub = d.get("published", "")
            if not pub:
                continue
            if pub.lower().startswith("oem"):
                valid_drivers.append(d)
                continue
            if glob.glob(os.path.join(rep, f"{pub}_*")):
                valid_drivers.append(d)

        logging.debug(f"[DRIVERS] _get_offline_drivers: {len(valid_drivers)} valid driver")
        return valid_drivers

    # ================================================================
    # BCD REPAIR (boot loader javítás offline restore után)
    # ================================================================
    def _repair_bcd(self, target_drive):
        """BCD újraépítése offline restore után - megakadályozza a boot hibákat."""
        logging.info(f"[BCD] BCD javítás indítása: {target_drive}")
        self.emit('task_progress', {'task': 'restore', 'log': '\n--- BOOT LOADER (BCD) JAVÍTÁS ---'})
        
        target_drive = target_drive.rstrip('\\') + '\\'
        windows_path = os.path.join(target_drive, 'Windows')
        
        if not os.path.exists(windows_path):
            self.emit('task_progress', {'task': 'restore', 'log': f'⚠️ Windows mappa nem található: {windows_path}'})
            return False
            
        success = False
        
        # 1. Próbáljuk a legegyszerűbb módszert (ALL)
        self.emit('task_progress', {'task': 'restore', 'log': f'bcdboot {target_drive}Windows /f ALL'})
        res = self._run(['bcdboot', f'{target_drive}Windows', '/f', 'ALL'])
        if res.returncode == 0:
            success = True
            self.emit('task_progress', {'task': 'restore', 'log': '✅ BCD sikeresen újraépítve (ALL)!'})
        else:
            err_msg = res.stderr.strip() if res.stderr else res.stdout.strip() if res.stdout else f'Exit code: {res.returncode}'
            self.emit('task_progress', {'task': 'restore', 'log': f'⚠️ bcdboot hiba (0x{res.returncode:X}): {err_msg[:300]}'})
            
        # 2. bootrec parancsok (ha a bcdboot nem sikerült teljesen)
        if not success:
            self.emit('task_progress', {'task': 'restore', 'log': 'bootrec parancsok futtatása...'})
            for cmd in ['/fixmbr', '/fixboot', '/rebuildbcd']:
                res = self._run(['bootrec', cmd])
                if res.returncode == 0:
                    self.emit('task_progress', {'task': 'restore', 'log': f'  bootrec {cmd}: ✅'})
                else:
                    self.emit('task_progress', {'task': 'restore', 'log': f'  bootrec {cmd}: ⚠️ (nem elérhető)'})
        
        logging.info(f"[BCD] Javítás befejezve, success={success}")
        return success

    # ================================================================
    # DRIVER DELETION
    # ================================================================
    def delete_drivers(self, published_names, list_all=False, reboot=False):
        logging.info(f"[API] delete_drivers() - {len(published_names)} driver, list_all={list_all}, reboot={reboot}")
        logging.info(f"[DELETE] Törlendő driverek: {published_names}")
        self._cancel_flag = False
        def worker():
            total = len(published_names)
            success = 0
            fail = 0
            logging.info(f"[DELETE] Törlés indulása: {total} db driver")
            self.emit('task_start', {'task': 'delete', 'title': f'Törlés folyamatban... ({total} driver)'})
            self.emit('task_progress', {'task': 'delete', 'log': f'Kijelölt driverek törlése indult ({total} db)'})

            cancelled = False
            for i, pub in enumerate(published_names):
                if self._cancel_flag:
                    self.emit('task_progress', {'task': 'delete', 'log': '❗ Törlés megszakítva a felhasználó által!'})
                    self.emit('task_progress', {'status': '❗ Megszakítva!', 'counter': f'{i} / {total}'})
                    cancelled = True
                    break
                
                self.emit('task_progress', {
                    'task': 'delete', 'current': i, 'total': total,
                    'status': f'Törlés: {pub}', 'counter': f'{i+1} / {total}',
                    'log': f'🗑 Törlés: {pub}'
                })
                try:
                    is_offline = bool(self.target_os_path)
                    is_oem = pub.lower().startswith("oem")

                    if is_offline and is_oem:
                        res = self._run(['dism', f'/Image:{self.target_os_path}', '/Remove-Driver', f'/Driver:{pub}'])
                    elif not is_offline:
                        res = self._run(['pnputil', '/delete-driver', pub, '/uninstall', '/force'])
                    else:
                        class DummyRes:
                            returncode = 1
                            stdout = ""
                        res = DummyRes()

                    if res.returncode == 0 or any(k in res.stdout for k in ["Deleted", "törölve", "successfully"]):
                        success += 1
                        self.emit('task_progress', {'task': 'delete', 'log': f'  ✅ {pub} törölve'})
                    else:
                        if list_all and not is_oem:
                            if is_offline:
                                rep = os.path.join(self.target_os_path, "Windows", "System32", "DriverStore", "FileRepository")
                                inf_dir = os.path.join(self.target_os_path, "Windows", "INF")
                            else:
                                rep = os.path.join(os.environ.get('SYSTEMROOT', r'C:\Windows'), "System32", "DriverStore", "FileRepository")
                                inf_dir = os.path.join(os.environ.get('SYSTEMROOT', r'C:\Windows'), "INF")
                            dirs = glob.glob(os.path.join(rep, f"{pub}_*"))
                            
                            found_any = False
                            if dirs:
                                for d in dirs:
                                    self._run(f'takeown /f "{d}" /r /d y', shell=True)
                                    self._run(f'icacls "{d}" /grant *S-1-5-32-544:F /t', shell=True)
                                    shutil.rmtree(d, ignore_errors=True)
                                    self._run(f'rmdir /s /q "{d}"', shell=True)
                                found_any = True

                            bname = os.path.splitext(pub)[0]
                            for ext in ['.in', '.pn', '.INF', '.PNF']:
                                fpath = os.path.join(inf_dir, bname + ext)
                                if os.path.exists(fpath):
                                    self._run(f'takeown /f "{fpath}" /A', shell=True)
                                    self._run(f'icacls "{fpath}" /grant *S-1-5-32-544:F', shell=True)
                                    try:
                                        os.remove(fpath)
                                        found_any = True
                                    except OSError:
                                        self._run(f'del /f /q "{fpath}"', shell=True)
                                        found_any = True

                            if found_any:
                                success += 1
                                self.emit('task_progress', {'task': 'delete', 'log': f'  ✅ {pub} törölve (force)'})
                            else:
                                fail += 1
                                self.emit('task_progress', {'task': 'delete', 'log': f'  ❌ {pub} sikertelen (nem található)'})
                        else:
                            fail += 1
                            self.emit('task_progress', {'task': 'delete', 'log': f'  ❌ {pub} sikertelen'})
                except Exception as e:
                    fail += 1
                    self.emit('task_progress', {'task': 'delete', 'log': f'  ❌ {pub} hiba: {e}'})

            # Post-delete scan
            is_offline = bool(self.target_os_path)
            is_pe = os.environ.get('SystemDrive', 'C:') == 'X:'
            if not is_offline and not is_pe and success > 0:
                self.emit('task_progress', {'task': 'delete', 'log': 'Hardverek újraszkennelése...', 'status': 'Hardverek újraszkennelése...'})
                self._run(['pnputil', '/scan-devices'])
                time.sleep(3)
                self.emit('task_progress', {'task': 'delete', 'log': '✅ Hardverek frissítve!'})

            if cancelled:
                self.emit('task_progress', {'task': 'delete', 'log': f'\n--- MEGSZAKÍTVA! Sikeres: {success}, Sikertelen: {fail} ---', 'current': i, 'total': total})
                self.emit('task_complete', {'task': 'delete', 'success': success, 'fail': fail,
                                            'counter': '❗ Megszakítva',
                                            'status': f'❗ Megszakítva! Sikeres: {success}, Sikertelen: {fail}'})
            else:
                self.emit('task_progress', {'task': 'delete', 'log': f'\n--- Sikeres: {success}, Sikertelen: {fail} ---', 'current': total, 'total': total})
                self.emit('task_complete', {'task': 'delete', 'success': success, 'fail': fail,
                                            'counter': f'✅ {success} / ❌ {fail}',
                                            'status': f'Kész! Sikeres: {success}, Sikertelen: {fail}'})
                
                # Újraindítás ha kérték
                if reboot and success > 0:
                    self.emit('task_progress', {'task': 'delete', 'log': '\n🔄 Újraindítás 5 másodperc múlva...'})
                    time.sleep(5)
                    self._run(['shutdown', '/r', '/t', '0', '/f'])

        self._safe_thread('delete', worker)

    # ================================================================
    # HARDWARE SCAN
    # ================================================================
    def start_hw_scan(self, run_sync=False, target_task='hw_scan'):
        logging.info("[API] start_hw_scan() hívás")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: Hardver keresés csak Élő rendszeren működik!', 'type': 'error'})
            self.emit('hw_scan_result', {'pool': [], 'installed': [], 'sys_info': '❌ Offline módban nem elérhető', 'time': ''})
            return

        if self._hw_scanning:
            logging.warning("[HW_SCAN] Már fut egy scan!")
            return
        self._hw_scanning = True
        logging.info("[HW_SCAN] Hardver scan indítása...")

        def worker():
            try:
                _start = time.time()
                
                # Hardver változások frissítése szkennelés előtt
                logging.info("[HW_SCAN] Eszközök újra-szkennelése (PnP)...")
                self.emit('hw_scan_progress', {'status': '⏳ Hardver változások keresése...'})
                self._run(['pnputil', '/scan-devices'])
                time.sleep(2)
                
                sys_info_text = "Ismeretlen PC / Laptop"
                logging.info("[HW_SCAN] Rendszer info lekérdezése...")
                self.emit('hw_scan_progress', {'status': '⏳ Rendszer információk lekérdezése...'})

                # System info
                try:
                    ps_cmd = (
                        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                        "$cs = Get-WmiObject Win32_ComputerSystem | Select-Object Manufacturer, Model, PCSystemType; "
                        "$bb = Get-WmiObject Win32_BaseBoard | Select-Object Manufacturer, Product; "
                        "$enc = Get-WmiObject Win32_SystemEnclosure | Select-Object ChassisTypes; "
                        "@{CS=$cs; BB=$bb; ENC=$enc} | ConvertTo-Json -Depth 3"
                    )
                    res = self._run(["powershell", "-NoProfile", "-Command", ps_cmd], encoding='utf-8')
                    if res.stdout.strip():
                        data = json.loads(res.stdout.strip())
                        cs = data.get("CS", {}) or {}
                        bb = data.get("BB", {}) or {}
                        enc = data.get("ENC", {}) or {}

                        man = (cs.get("Manufacturer") or "").strip()
                        mod = (cs.get("Model") or "").strip()
                        pct = cs.get("PCSystemType", -1)

                        # Fallback: ha OEM placeholder, használjuk az alaplap infót
                        oem_junk = {"to be filled by o.e.m.", "default string", "system manufacturer",
                                    "system product name", "not applicable", ""}
                        if man.lower() in oem_junk:
                            man = (bb.get("Manufacturer") or "").strip()
                        if mod.lower() in oem_junk:
                            mod = (bb.get("Product") or "").strip()
                        if man.lower() in oem_junk:
                            man = "Ismeretlen gyártó"
                        if mod.lower() in oem_junk:
                            mod = "Ismeretlen modell"

                        # Chassis-alapú laptop/desktop detekció (pontosabb mint PCSystemType)
                        chassis = enc.get("ChassisTypes", []) or []
                        if isinstance(chassis, int):
                            chassis = [chassis]
                        laptop_chassis = {8, 9, 10, 11, 14, 30, 31, 32}  # Portable, Laptop, Notebook, Sub Notebook, etc.
                        is_laptop = pct == 2 or any(c in laptop_chassis for c in chassis)
                        prefix = "💻 Laptop" if is_laptop else "🖥️ Asztali (Desktop)"

                        sys_info_text = f"{prefix} | {man} - {mod}"
                except Exception as e:
                    logging.debug(e)
                self.emit('hw_scan_progress', {'sys_info': sys_info_text, 'status': '⏳ PnP eszközök lekérdezése...'})

                # PnP devices
                ignored_classes = ['Volume', 'VolumeSnapshot', 'DiskDrive', 'CDROM', 'Monitor', 'Battery',
                                   'SoftwareDevice', 'SoftwareComponent', 'Processor', 'Computer',
                                   'LegacyDriver', 'Endpoint', 'AudioEndpoint', 'PrintQueue', 'Printer', 'WPD']

                pnp_data = []
                try:
                    cmd_pnp = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Get-WmiObject Win32_PnPEntity | Where-Object { $_.Present -eq $true } | Select-Object Name, PNPClass, PNPDeviceID | ConvertTo-Json -Compress"
                    res = self._run(["powershell", "-NoProfile", "-Command", cmd_pnp], encoding='utf-8')
                    if res.stdout:
                        out = json.loads(res.stdout)
                        pnp_data = out if isinstance(out, list) else [out]
                except Exception as ex:
                    logging.error(f"PNP Query error: {ex}")

                self.emit('hw_scan_progress', {'status': f'📋 {len(pnp_data)} PnP eszköz szűrése...'})

                seen_hwids = set()
                devices_to_check = []
                for d in pnp_data:
                    n = d.get("Name") or "Ismeretlen Eszköz"
                    pid = d.get("PNPDeviceID") or ""
                    pclass = d.get("PNPClass") or ""
                    if not pid:
                        continue
                    if "virtual" in n.lower() or "pseudo" in n.lower() or "vmware" in n.lower():
                        continue
                    if pid.upper().startswith("ROOT\\"):
                        continue
                    if pclass in ignored_classes:
                        continue
                    hwid_clean = self._extract_hwid(pid)
                    if not hwid_clean:
                        continue
                    if hwid_clean in seen_hwids:
                        continue
                    seen_hwids.add(hwid_clean)
                    if pclass == "Display": cat = "🎮 Videókártya (VGA)"
                    elif pclass == "Media": cat = "🎵 Hangkártya (Audio)"
                    elif pclass == "Net": cat = "🌐 Hálózat (LAN/Wi-Fi)"
                    elif pclass == "Bluetooth": cat = "🔵 Bluetooth"
                    elif pclass == "System": cat = "⚙️ Rendszereszköz"
                    elif pclass == "USB": cat = "🔌 USB Vezérlő"
                    elif pclass in ("Camera", "Image"): cat = "📷 Webkamera"
                    elif pclass in ("Mouse", "Keyboard", "HIDClass"): cat = "🖱️ Periféria"
                    elif pclass == "Biometric": cat = "🔒 Ujjlenyomat / Biometria"
                    else: cat = f"🔧 Egyéb ({pclass})"
                    devices_to_check.append({"cat": cat, "name": n, "id": hwid_clean, "pnp_id": pid})

                logging.info(f"PnP szürés: {len(devices_to_check)} eszköz átment")
                total_devs = len(devices_to_check)
                self.emit('hw_scan_progress', {'status': f'✅ {total_devs} eszköz azonosítva, WU keresés indul...',
                                               'sys_info': f'{sys_info_text} | ⏳ Driver keresés...'})

                # WU COM API search
                self.hw_updates_pool = []
                self._hw_installed_devs = []
                self.wu_api_mode = True
                wu_results = self._search_wu_api()
                wu_api_success = wu_results is not None
                if wu_results is None:
                    wu_results = []

                self.emit('hw_scan_progress', {'status': '📋 Eredmények feldolgozása...'})

                matched_hwids = set()
                if wu_results:
                    for wu in wu_results:
                        wu_hwid_raw = (wu.get('HardwareID') or '').upper()
                        wu_title = wu.get('Title', '')
                        for dev in devices_to_check:
                            if dev['id'] in matched_hwids:
                                continue
                            dev_hwid = dev['id'].upper()
                            dev_pnp = dev.get('pnp_id', '').upper()
                            if (dev_hwid and dev_hwid in wu_hwid_raw) or (wu_hwid_raw and wu_hwid_raw in dev_pnp):
                                matched_hwids.add(dev['id'])
                                self.hw_updates_pool.append({
                                    "name": dev['name'], "cat": dev['cat'], "hwid": dev['id'],
                                    "wu_title": wu_title, "pnp_id": dev.get('pnp_id', '')
                                })
                                break
                    # Unmatched WU updates kihagyása a ghost eszközök miatt
                    for wu in wu_results:
                        wu_hwid_raw = (wu.get('HardwareID') or '').upper()
                        if not wu_hwid_raw:
                            continue
                        already = any(dev['id'].upper() in wu_hwid_raw or wu_hwid_raw in dev.get('pnp_id', '').upper()
                                      for dev in devices_to_check)
                        if not already:
                            logging.debug(f"[WU_API] Ghost / Unmatched eszköz kihagyva: {wu.get('Title')}")
                            # Eltávolítva: self.hw_updates_pool.append(...)

                self._hw_installed_devs = [dev for dev in devices_to_check if dev['id'] not in matched_hwids]

                # Catalog fallback if WU API failed
                if not self.hw_updates_pool and not wu_api_success:
                    self.wu_api_mode = False
                    self.emit('hw_scan_progress', {'status': f'🌐 WU API hiba, katalógus keresés ({total_devs} eszköz)...'})
                    self._catalog_search(devices_to_check)

                elapsed = int(time.time() - _start)
                _m, _s = divmod(elapsed, 60)
                time_str = f"{_m} perc {_s} mp" if _m else f"{_s} mp"
                mode = "WU API" if self.wu_api_mode else "Katalógus"
                found = len(self.hw_updates_pool)
                final_sys = f"{sys_info_text} | ✅ Kész ({mode})! {found} frissítés ({total_devs} eszköz)"

                self.emit('hw_scan_result', {
                    'pool': self.hw_updates_pool, 'installed': self._hw_installed_devs,
                    'sys_info': final_sys, 'time': time_str
                })
                self._hw_loaded = True
            except Exception as e:
                logging.error(f"hw_scan crash: {e}")
                logging.error(traceback.format_exc())
                self.emit('hw_scan_progress', {'status': '❌ Hiba történt!'})
                self.emit('hw_scan_result', {'pool': [], 'installed': [], 'sys_info': '❌ Scan hiba', 'time': ''})
            finally:
                self._hw_scanning = False

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception as e:
            logging.error(f"[HW_SCAN] Thread indítási hiba: {e}")
            self._hw_scanning = False
            self.emit('hw_scan_result', {'pool': [], 'installed': [], 'sys_info': '❌ Thread hiba', 'time': ''})

    def _extract_hwid(self, pnp_id):
        if not pnp_id:
            return None
        m = re.search(r'(HDAUDIO\\FUNC_[0-9A-F]+&VEN_[0-9A-F]+&DEV_[0-9A-F]+)', pnp_id, re.I)
        if m:
            logging.debug(f"[HWID] {pnp_id} -> {m.group(1)}")
            return m.group(1)
        m = re.search(r'(VEN_[0-9A-F]+&DEV_[0-9A-F]+)', pnp_id, re.I)
        if m:
            logging.debug(f"[HWID] {pnp_id} -> {m.group(1)}")
            return m.group(1)
        m = re.search(r'(HID\\VID_[0-9A-F]+&PID_[0-9A-F]+)', pnp_id, re.I)
        if m:
            logging.debug(f"[HWID] {pnp_id} -> {m.group(1)}")
            return m.group(1)
        m = re.search(r'(USB\\VID_[0-9A-F]+&PID_[0-9A-F]+)', pnp_id, re.I)
        if m:
            logging.debug(f"[HWID] {pnp_id} -> {m.group(1)}")
            return m.group(1)
        m = re.search(r'(VID_[0-9A-F]+&PID_[0-9A-F]+)', pnp_id, re.I)
        if m:
            logging.debug(f"[HWID] {pnp_id} -> {m.group(1)}")
            return m.group(1)
        m = re.search(r'(ACPI\\[A-Z0-9_]+)', pnp_id, re.I)
        if m:
            logging.debug(f"[HWID] {pnp_id} -> {m.group(1)}")
            return m.group(1)
        m = re.search(r'(DISPLAY\\[A-Z0-9]+)', pnp_id, re.I)
        if m:
            logging.debug(f"[HWID] {pnp_id} -> {m.group(1)}")
            return m.group(1)
        logging.debug(f"[HWID] {pnp_id} -> None (no match)")
        return None

    def _search_wu_api(self):
        logging.info("[WU_API] _search_wu_api() indult...")
        try:
            ps_cmd = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
try {
    $Session = New-Object -ComObject Microsoft.Update.Session
    $Searcher = $Session.CreateUpdateSearcher()
    try {
        $SM = New-Object -ComObject Microsoft.Update.ServiceManager
        $SM.AddService2("7971f918-a847-4430-9279-4a52d1efe18d", 7, "") | Out-Null
    } catch {}
    $Searcher.ServerSelection = 3
    $Searcher.ServiceID = "7971f918-a847-4430-9279-4a52d1efe18d"
    $Result = $Searcher.Search("IsInstalled=0 and Type='Driver'")
    $updates = @()
    foreach ($U in $Result.Updates) {
        $updates += [PSCustomObject]@{
            Title = $U.Title; DriverModel = $U.DriverModel; HardwareID = $U.DriverHardwareID
            DriverClass = $U.DriverClass; DriverProvider = $U.DriverProvider
            UpdateID = $U.Identity.UpdateID; Size = $U.MaxDownloadSize
        }
    }
    if ($updates.Count -eq 0) { Write-Output "[]" }
    else { $updates | ConvertTo-Json -Depth 2 -Compress }
} catch { Write-Error $_.Exception.Message }
"""
            res = self._run(["powershell", "-NoProfile", "-Command", ps_cmd], timeout=300, encoding='utf-8')
            out = res.stdout.strip()
            if not out and res.stderr:
                logging.warning(f"[WU_API] Stderr: {res.stderr[:200]}")
                return None
            if out:
                data = json.loads(out)
                if isinstance(data, dict):
                    data = [data]
                logging.info(f"[WU_API] Talált frissítések: {len(data) if isinstance(data, list) else 0}")
                return data if isinstance(data, list) else None
        except subprocess.TimeoutExpired:
            logging.error("[WU_API] WU API timeout (300s)")
        except Exception as e:
            logging.error(f"[WU_API] WU API error: {e}")
        return None

    def _catalog_search(self, devices_to_check):
        logging.info(f"[CATALOG] _catalog_search() - {len(devices_to_check)} eszköz ellenőrzése...")
        import urllib.request, urllib.parse, ssl
        ssl_ctx = ssl.create_default_context()
        lock = threading.Lock()

        def check_one(item):
            try:
                url = 'https://www.catalog.update.microsoft.com/Search.aspx?q=' + urllib.parse.quote(item['id'])
                logging.debug(f"[CATALOG] Keresés: {item['name']} ({item['id']})")
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                html = urllib.request.urlopen(req, context=ssl_ctx, timeout=30).read().decode('utf-8')
                match_ids = re.findall(r"id=['\"]([a-fA-F0-9\-]+)_link['\"]", html)
                if match_ids:
                    best_id = match_ids[0]
                    dl_body = f'updateIDs=[{{"size":0,"languages":"","uidInfo":"{best_id}","updateID":"{best_id}"}}]'
                    dl_req = urllib.request.Request(
                        'https://www.catalog.update.microsoft.com/DownloadDialog.aspx',
                        data=dl_body.encode('utf-8'),
                        headers={'User-Agent': 'Mozilla/5.0', 'Content-Type': 'application/x-www-form-urlencoded'})
                    dl_html = urllib.request.urlopen(dl_req, context=ssl_ctx, timeout=30).read().decode('utf-8')
                    cab_link = re.search(r'downloadInformation\[0\]\.files\[0\]\.url\s*=\s*[\"\']([^\"\']+)[\"\']', dl_html)
                    if cab_link:
                        logging.debug(f"[CATALOG] Találat: {item['name']} - {cab_link.group(1)[:50]}...")
                        with lock:
                            self.hw_updates_pool.append({
                                "name": item['name'], "cat": item['cat'], "hwid": item['id'],
                                "url": cab_link.group(1), "pnp_id": item.get('pnp_id', ''),
                                "wu_title": f"MS Katalógus: {item['name']}"
                            })
            except Exception as e:
                logging.debug(f"[CATALOG] Hiba: {item['name']} - {e}")
                pass

        q = queue.Queue()
        for dev in devices_to_check:
            q.put(dev)

        def cat_worker():
            while not q.empty():
                try:
                    dev = q.get_nowait()
                except Exception:
                    break
                check_one(dev)
                q.task_done()

        threads = [threading.Thread(target=cat_worker, daemon=True) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        catalog_hwids = {drv['hwid'] for drv in self.hw_updates_pool}
        self._hw_installed_devs = [dev for dev in devices_to_check if dev['id'] not in catalog_hwids]
        logging.info(f"[CATALOG] Kész - {len(self.hw_updates_pool)} találat, {len(self._hw_installed_devs)} nem elérhető")

    # ================================================================
    # WU DRIVER INSTALL
    # ================================================================
    def install_selected_wu(self, selected_indices):
        logging.info(f"[API] install_selected_wu() - {len(selected_indices)} index kiválasztva")
        logging.debug(f"[WU_INSTALL] Indexek: {selected_indices}")
        self._cancel_flag = False  # Reset cancel flag
        selected_pool = [self.hw_updates_pool[i] for i in selected_indices if 0 <= i < len(self.hw_updates_pool)]
        if not selected_pool:
            logging.warning("[WU_INSTALL] Nincs érvényes driver kiválasztva!")
            self.emit('toast', {'message': '⚠️ Nincs érvényes driver kiválasztva!', 'type': 'warning'})
            return
        logging.info(f"[WU_INSTALL] {len(selected_pool)} driver telepítése, mód={'WU API' if self.wu_api_mode else 'Katalógus'}")

        if self.wu_api_mode:
            self._install_wu_api(selected_pool)
        else:
            self._install_catalog(selected_pool)

    def _install_wu_api(self, selected_pool):
        logging.info(f"[WU_API] WU API telepítés indítása: {len(selected_pool)} driver")
        def worker():
            self.emit('task_start', {'task': 'wu_install', 'title': f'Driver Telepítés WU Szerverekről ({len(selected_pool)} db)'})
            self.emit('task_progress', {'task': 'wu_install', 'log': 'Windows Update szervereiről történő telepítés indítása...', 'indeterminate': True})

            pool_hwids = [drv.get('hwid', '').upper() for drv in selected_pool if drv.get('hwid')]
            hwid_list_ps = ','.join(f'"{h}"' for h in pool_hwids)

            ps_script = '$TargetHWIDs = @(' + hwid_list_ps + ')\n' + r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
try {
    Write-Output "INIT: Windows Update Session létrehozása..."
    $Session = New-Object -ComObject Microsoft.Update.Session
    $Searcher = $Session.CreateUpdateSearcher()
    try { $SM = New-Object -ComObject Microsoft.Update.ServiceManager; $SM.AddService2("7971f918-a847-4430-9279-4a52d1efe18d", 7, "") | Out-Null } catch {}
    $Searcher.ServerSelection = 3
    $Searcher.ServiceID = "7971f918-a847-4430-9279-4a52d1efe18d"
    Write-Output "SEARCH: Driver frissítések keresése..."
    $Result = $Searcher.Search("IsInstalled=0 and Type='Driver'")
    if ($Result.Updates.Count -eq 0) { Write-Output "EMPTY: Nem található elérhető driver frissítés."; return }
    $ToInstall = New-Object -ComObject Microsoft.Update.UpdateColl
    foreach ($U in $Result.Updates) {
        $matchFound = $false
        if ($TargetHWIDs.Count -eq 0) { $matchFound = $true } else {
            foreach ($hwid in $U.DriverHardwareID) {
                $hUpper = $hwid.ToUpper()
                foreach ($target in $TargetHWIDs) { if ($hUpper.Contains($target) -or $target.Contains($hUpper)) { $matchFound = $true; break } }
                if ($matchFound) { break }
            }
        }
        if (-not $matchFound) { Write-Output "SKIP: $($U.Title)"; continue }
        if (-not $U.EulaAccepted) { $U.AcceptEula() }
        $ToInstall.Add($U) | Out-Null
        Write-Output "FOUND: $($U.Title)"
    }
    if ($ToInstall.Count -eq 0) { Write-Output "EMPTY: Nem található egyező driver."; return }
    $total = $ToInstall.Count; Write-Output "TOTAL: $total"
    $s = 0; $f = 0
    for ($i = 0; $i -lt $total; $i++) {
        $U = $ToInstall.Item($i); $t = $U.Title; $idx = $i + 1
        Write-Output "DLONE: $idx/$total $t"
        $SC = New-Object -ComObject Microsoft.Update.UpdateColl; $SC.Add($U) | Out-Null
        $DL = $Session.CreateUpdateDownloader(); $DL.Updates = $SC
        try { $DR = $DL.Download() } catch { Write-Output "FAIL: [LETÖLTÉS HIBA] $t"; $f++; continue }
        if ($DR.ResultCode -ne 2 -and $DR.ResultCode -ne 3) { Write-Output "FAIL: [LETÖLTÉS HIBA kód=$($DR.ResultCode)] $t"; $f++; continue }
        Write-Output "INSTONE: $idx/$total $t"
        $Inst = $Session.CreateUpdateInstaller(); $Inst.Updates = $SC
        try { $IR = $Inst.Install() } catch { Write-Output "FAIL: [TELEPÍTÉS HIBA] $t"; $f++; continue }
        $rc = $IR.GetUpdateResult(0).ResultCode
        switch ($rc) { 2 { Write-Output "OK: $t"; $s++ } 3 { Write-Output "OK: $t"; $s++ } default { Write-Output "FAIL: [kód=$rc] $t"; $f++ } }
    }
    Write-Output "DONE: Sikeres=$s, Sikertelen=$f"
} catch { Write-Output "ERROR: $($_.Exception.Message)" }
"""
            process = subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                startupinfo=self._si, creationflags=self._nw)

            success = 0
            fail = 0
            install_total = 0

            for line in process.stdout:
                if self._check_cancel():
                    process.terminate()
                    process.wait()  # Prevent zombie process
                    self.emit('task_progress', {'task': 'wu_install', 'log': '\n❗ Megszakítva!'})
                    self.emit('task_complete', {'task': 'wu_install', 'status': '❗ Megszakítva!', 'success': success, 'fail': fail})
                    return
                line = line.strip()
                if not line:
                    continue
                if line.startswith("INIT:") or line.startswith("SEARCH:"):
                    self.emit('task_progress', {'task': 'wu_install', 'status': line.split(":", 1)[1].strip(), 'log': line})
                elif line.startswith("FOUND:"):
                    self.emit('task_progress', {'task': 'wu_install', 'log': f'  📦 {line[6:].strip()}'})
                elif line.startswith("SKIP:"):
                    self.emit('task_progress', {'task': 'wu_install', 'log': f'  ⏭ {line[5:].strip()}'})
                elif line.startswith("TOTAL:"):
                    m = re.search(r'(\d+)', line)
                    if m:
                        install_total = int(m.group(1))
                    self.emit('task_progress', {'task': 'wu_install', 'log': f'Összesen {install_total} driver telepítése...',
                                                'total': install_total, 'current': 0, 'counter': f'0 / {install_total}'})
                elif line.startswith("DLONE:"):
                    self.emit('task_progress', {'task': 'wu_install', 'status': f'⬇ Letöltés: {line[6:].strip()}', 'log': f'  ⬇ {line[6:].strip()}'})
                elif line.startswith("INSTONE:"):
                    self.emit('task_progress', {'task': 'wu_install', 'status': f'⚙ Telepítés: {line[8:].strip()}', 'log': f'  ⚙ {line[8:].strip()}'})
                elif line.startswith("OK:"):
                    success += 1
                    done = success + fail
                    self.emit('task_progress', {'task': 'wu_install', 'log': f'  ✅ {line[3:].strip()}',
                                                'current': done, 'total': install_total, 'counter': f'{done}/{install_total} (✅{success} ❌{fail})'})
                elif line.startswith("FAIL:"):
                    fail += 1
                    done = success + fail
                    self.emit('task_progress', {'task': 'wu_install', 'log': f'  ❌ {line[5:].strip()}',
                                                'current': done, 'total': install_total, 'counter': f'{done}/{install_total} (✅{success} ❌{fail})'})
                elif line.startswith("DONE:"):
                    self.emit('task_progress', {'task': 'wu_install', 'log': f'\n--- {line[5:].strip()} ---'})
                elif line.startswith("EMPTY:"):
                    self.emit('task_progress', {'task': 'wu_install', 'log': line[6:].strip()})
                elif line.startswith("ERROR:"):
                    self.emit('task_progress', {'task': 'wu_install', 'log': f'❌ HIBA: {line[6:].strip()}'})
                else:
                    self.emit('task_progress', {'task': 'wu_install', 'log': line})
            process.wait()

            if success > 0:
                self.emit('task_progress', {'task': 'wu_install', 'log': 'Eszközök újraszkennelése...', 'status': 'Aktiválás...'})
                self._run(['pnputil', '/scan-devices'])
                self.emit('task_progress', {'task': 'wu_install', 'log': '✅ Eszközök frissítve!'})

            msg = f'Sikeres: {success}, Sikertelen: {fail}'
            self.emit('task_complete', {'task': 'wu_install', 'success': success, 'fail': fail,
                                        'status': msg, 'counter': msg})

        self._safe_thread('wu_install', worker)

    def _install_catalog(self, selected_pool):
        logging.info(f"[CATALOG_INSTALL] _install_catalog() - {len(selected_pool)} driver")
        def worker():
            logging.info("[CATALOG_INSTALL] Worker indult...")
            import urllib.request, ssl
            ssl_ctx = ssl.create_default_context()
            total = len(selected_pool)
            self.emit('task_start', {'task': 'wu_install', 'title': f'Katalógus Driver Telepítés ({total} db)'})

            temp_dir = os.path.join(os.environ.get('TEMP', 'C:\\Temp'), 'driverdoktor_wu')
            os.makedirs(temp_dir, exist_ok=True)
            logging.debug(f"[CATALOG_INSTALL] Temp dir: {temp_dir}")
            success = 0
            fail = 0
            skipped = 0

            try:
                for i, drv in enumerate(selected_pool):
                    if self._check_cancel():
                        logging.warning("[CATALOG_INSTALL] Megszakítva!")
                        self.emit('task_progress', {'task': 'wu_install', 'log': '\n❗ Megszakítva!'})
                        self.emit('task_complete', {'task': 'wu_install', 'status': '❗ Megszakítva!', 'success': success, 'fail': fail})
                        return
                    name = drv['name']
                    url = drv.get('url', '')
                    logging.info(f"[CATALOG_INSTALL] [{i+1}/{total}] {name}")
                    if not url:
                        logging.warning(f"[CATALOG_INSTALL] Kihagyás - nincs URL: {name}")
                        self.emit('task_progress', {'task': 'wu_install', 'log': f'  [KIHAGYÁS] {name} - nincs link'})
                        skipped += 1
                        continue

                    cab_path = os.path.join(temp_dir, f"drv_{i}.cab")
                    ext_path = os.path.join(temp_dir, f"drv_ext_{i}")
                    self.emit('task_progress', {'task': 'wu_install', 'current': i, 'total': total,
                                                'status': f'Letöltés: {name}', 'counter': f'{i+1}/{total}',
                                                'log': f'-> {name} letöltése...'})
                    try:
                        logging.debug(f"[CATALOG_INSTALL] Letöltés: {url[:80]}...")
                        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
                        with urllib.request.urlopen(req, context=ssl_ctx) as resp, open(cab_path, 'wb') as f:
                            shutil.copyfileobj(resp, f)
                        logging.debug(f"[CATALOG_INSTALL] Letöltve: {cab_path}")
                    except Exception as e:
                        logging.error(f"[CATALOG_INSTALL] Letöltési hiba: {e}")
                        self.emit('task_progress', {'task': 'wu_install', 'log': f'  [HIBA] Letöltés: {e}'})
                        fail += 1
                        continue

                    os.makedirs(ext_path, exist_ok=True)
                    self._run(['expand', cab_path, '-F:*', ext_path])
                    for inner_cab in glob.glob(os.path.join(ext_path, '*.cab')):
                        inner_ext = inner_cab + '_ext'
                        os.makedirs(inner_ext, exist_ok=True)
                        self._run(['expand', inner_cab, '-F:*', inner_ext])

                    self.emit('task_progress', {'task': 'wu_install', 'status': f'Telepítés: {name}', 'log': '  Telepítés...'})
                    is_offline = bool(self.target_os_path)
                    if is_offline:
                        cmd = ['dism', f'/Image:{self.target_os_path}', '/Add-Driver', f'/Driver:{ext_path}', '/Recurse', '/ForceUnsigned']
                    else:
                        cmd = ['pnputil', '/add-driver', f"{ext_path}\\*.in", '/subdirs', '/install']
                    res = self._run(cmd)
                    if res.returncode == 0 or any(k in res.stdout for k in ["Added", "sikeres", "successfully"]):
                        success += 1
                        logging.info(f"[CATALOG_INSTALL] ✅ {name} telepítve!")
                        self.emit('task_progress', {'task': 'wu_install', 'log': f'  ✅ {name} telepítve!'})
                    else:
                        fail += 1
                        logging.error(f"[CATALOG_INSTALL] ❌ {name} hiba: {res.stdout[:100]}")
                        self.emit('task_progress', {'task': 'wu_install', 'log': f'  ❌ {name} hiba: {res.stdout[:100]}'})

                if success > 0 and not self.target_os_path:
                    self.emit('task_progress', {'task': 'wu_install', 'log': 'Eszközök újraszkennelése...'})
                    self._run(['pnputil', '/scan-devices'])
            finally:
                logging.debug(f"[CATALOG_INSTALL] Temp dir törlése: {temp_dir}")
                shutil.rmtree(temp_dir, ignore_errors=True)

            logging.info(f"[CATALOG_INSTALL] Kész - Sikeres: {success}/{total}, Sikertelen: {fail}, Kihagyott: {skipped}")
            self.emit('task_progress', {'task': 'wu_install', 'current': total, 'total': total,
                                        'log': f'\n--- Sikeres: {success}, Sikertelen: {fail}, Kihagyott: {skipped} ---'})
            self.emit('task_complete', {'task': 'wu_install', 'success': success, 'fail': fail,
                                        'status': f'Kész! Sikeres: {success}, Sikertelen: {fail}' + (f', Kihagyott: {skipped}' if skipped else '')})

        self._safe_thread('wu_install', worker)

    # ================================================================
    # WU MANAGEMENT
    # ================================================================
    def check_wu_status(self):
        logging.info("[API] check_wu_status()")
        if self.target_os_path:
            return {'status': 'Offline (Nem olvasható)', 'color': 'unknown'}
        try:
            policy_disabled = False
            search_disabled = False
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate", 0, winreg.KEY_READ) as key:
                    val, _ = winreg.QueryValueEx(key, "ExcludeWUDriversInQualityUpdate")
                    if val == 1: policy_disabled = True
                    logging.debug(f"[WU_STATUS] ExcludeWUDriversInQualityUpdate = {val}")
            except FileNotFoundError:
                logging.debug("[WU_STATUS] ExcludeWUDriversInQualityUpdate kulcs nem létezik")
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching", 0, winreg.KEY_READ) as key:
                    val, _ = winreg.QueryValueEx(key, "SearchOrderConfig")
                    if val == 0: search_disabled = True
                    logging.debug(f"[WU_STATUS] SearchOrderConfig = {val}")
            except FileNotFoundError:
                logging.debug("[WU_STATUS] SearchOrderConfig kulcs nem létezik")

            if policy_disabled and search_disabled:
                result = {'status': 'Teljesen LETILTVA', 'color': 'disabled'}
            elif policy_disabled:
                result = {'status': 'Házirend által LETILTVA', 'color': 'disabled'}
            elif search_disabled:
                result = {'status': 'Eszközbeállításokban LETILTVA', 'color': 'disabled'}
            else:
                result = {'status': 'Driver frissítés ENGEDÉLYEZVE', 'color': 'enabled'}
            logging.info(f"[WU_STATUS] Eredmény: {result['status']}")
            return result
        except Exception as e:
            logging.error(f"[WU_STATUS] Hiba: {e}")
            return {'status': 'Ismeretlen', 'color': 'unknown'}

    def run_autofix(self):
        logging.info("[API] run_autofix() indítása")
        if self.target_os_path:
            self.emit('toast', {'message': 'Az 1 kattintásos fix csak az Élő (jelenlegi) rendszeren futtatható le biztonságosan!', 'type': 'error'})
            return

        def worker():
            import ctypes
            import sys
            
            # --- KONZOL ALLOKALASA ---
            try:
                ctypes.windll.kernel32.AllocConsole()
                sys.stdout = open("CONOUT$", "w", encoding="utf-8")
                sys.stderr = open("CONOUT$", "w", encoding="utf-8")
                print("==================================================")
                print(" DRIVERDOKTOR 1-KATTINTASOS FIX (KONZOL)")
                print("==================================================")
                print("FIGYELEM: Ha az ablak kifeheredik a videokartya driver torlesekor,")
                print("a folyamat itt a hatterben zavartalanul tavabb fut!\n")
            except Exception as alloc_e:
                logging.error(f"[AUTOFIX] Konzol hiba: {alloc_e}")

            def c_print(msg, p_type='log', **kwargs):
                # Prints to console
                try:
                    clean_msg = msg.replace('✅', '[OK]').replace('⚠️', '[FIGYELMEZTETES]').replace('🗑', '[TORLES]').replace('🎉', '[KESZ]')
                    print(clean_msg)
                except:
                    pass
                
                # Emits to UI
                kwargs[p_type] = msg
                kwargs['task'] = 'autofix'
                self.emit('task_progress', kwargs)

            self.emit('task_start', {'task': 'autofix', 'title': '1 Kattintásos Driver Javítás és Frissítés'})
            try:
                import datetime
                
                # 1. Rendszer visszaállítása
                desc = "DriverDoktor AutoFix - " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                c_print('[1/4] Registry Mentés (Restore Point) készítése folyamatban...', phase='Registry/Rendszer Mentés', indeterminate=True)
                
                self._run(["powershell", "-NoProfile", "-Command", 'Enable-ComputerRestore -Drive "$($env:SystemDrive)\\" -ErrorAction SilentlyContinue'])
                self._run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore', '/v', 'SystemRestorePointCreationFrequency', '/t', 'REG_DWORD', '/d', '0', '/f'])
                ps_cmd = f'Checkpoint-Computer -Description "{desc}" -RestorePointType "MODIFY_SETTINGS" -ErrorAction SilentlyContinue'
                res1 = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd], encoding='utf-8')
                if res1.returncode == 0:
                    c_print('✅ Registry mentés / Visszaállítási pont elkészült.\n')
                else:
                    c_print('⚠️ Visszaállítási pont elutasítva a rendszer által. (Rendszervédelem talán nincs bekapcsolva a C: meghajtón) - FOLYTATÁS...\n')
                
                if self._cancel_flag: raise Exception("Magyar_Megszakit_Flag")

                # 2. WU Letiltása
                c_print('[2/4] Windows automata driver frissítések letiltása a Registryben...', phase='Windows Update Letiltása', indeterminate=True)
                reg_cmd = ['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching', '/v', 'SearchOrderConfig', '/t', 'REG_DWORD', '/d', '0', '/f']
                self._run(reg_cmd)
                c_print('✅ Automatikus driver telepítés letiltva.\n')
                
                if self._cancel_flag: raise Exception("Magyar_Megszakit_Flag")

                # 3. Third party driverek törlése
                c_print('[3/4] Third-party driverek összegyűjtése és törlése...', phase='Driverek Eltávolítása', indeterminate=True)
                drivers = self._get_third_party_drivers()
                total = len(drivers)
                if total > 0:
                    c_print(f'{total} db third-party driver eltávolítása...\n')
                    for i, drv in enumerate(drivers):
                        if self._cancel_flag: raise Exception("Magyar_Megszakit_Flag")
                        
                        name = drv.get('published', '')
                        if not name: continue
                        
                        c_print(f'🗑 Törlés ({i+1}/{total}): {name}', current=i+1, total=total)
                        self._run(['pnputil', '/delete-driver', name, '/uninstall', '/force'])
                    c_print('✅ Driverek eltávolítva.\n')
                else:
                    c_print('✅ Nincs third-party driver a rendszerben.\n')
                
                if self._cancel_flag: raise Exception("Magyar_Megszakit_Flag")

                # 4. Keresés és visszaépítés
                c_print('[4/4] Új eszközök szkennelése PnP Util-lal...', phase='Új Driverek Keresése', indeterminate=True)
                self._run(['pnputil', '/scan-devices'])
                time.sleep(3)
                
                c_print('Hivatalos driverek keresése és telepítése (Windows Update). Ez percekig is eltarthat...')
                ps_script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
try {
    $PresentHWIDs = @()
    Get-WmiObject Win32_PnPEntity | Where-Object { $_.Present -eq $true } | ForEach-Object {
        if ($_.HardwareID) { foreach ($hid in $_.HardwareID) { $PresentHWIDs += $hid.ToUpper() } }
        if ($_.PNPDeviceID) { $PresentHWIDs += $_.PNPDeviceID.ToUpper() }
    }

    $Session = New-Object -ComObject Microsoft.Update.Session
    $Searcher = $Session.CreateUpdateSearcher()
    try { $SM = New-Object -ComObject Microsoft.Update.ServiceManager; $SM.AddService2("7971f918-a847-4430-9279-4a52d1efe18d", 7, "") | Out-Null } catch {}
    $Searcher.ServerSelection = 3
    $Searcher.ServiceID = "7971f918-a847-4430-9279-4a52d1efe18d"
    
    Write-Output "--- KERESÉS ---"
    $Result = $Searcher.Search("IsInstalled=0 and Type='Driver'")
    
    $ToInstall = New-Object -ComObject Microsoft.Update.UpdateColl
    foreach ($U in $Result.Updates) {
        $matchFound = $false
        foreach ($hwid in $U.DriverHardwareID) {
            $hUpper = $hwid.ToUpper()
            foreach ($target in $PresentHWIDs) {
                if ($hUpper.Contains($target) -or $target.Contains($hUpper)) { $matchFound = $true; break }
            }
            if ($matchFound) { break }
        }
        if ($matchFound) {
            if (-not $U.EulaAccepted) { $U.AcceptEula() }
            $ToInstall.Add($U) | Out-Null
        } else {
            Write-Output "❌ GHOST ESZKÖZ KIHAGYVA: $($U.Title)"
        }
    }

    if ($ToInstall.Count -eq 0) { Write-Output "✅ Szerveren nincs újabb valós illesztőprogram ehhez a géphez."; exit }
    
    $Count = $ToInstall.Count
    Write-Output "✅ Telepítendő driverek száma: $Count"
    
    Write-Output "--- LETÖLTÉS ---"
    $Downloader = $Session.CreateUpdateDownloader()
    $Downloader.Updates = $ToInstall
    $Downloader.Download() | Out-Null
    
    Write-Output "--- TELEPÍTÉS ---"
    $Installer = $Session.CreateUpdateInstaller()
    
    $s = 0; $f = 0
    for ($i = 0; $i -lt $Count; $i++) {
        $U = $ToInstall.Item($i)
        $Title = $U.Title
        Write-Output "▶ Telepítés alatt: $Title"
        
        $SingleUpdateColl = New-Object -ComObject Microsoft.Update.UpdateColl
        $SingleUpdateColl.Add($U) | Out-Null
        
        $Installer.Updates = $SingleUpdateColl
        try { 
            $IR = $Installer.Install()
            $RC = $IR.ResultCode 
            if ($RC -eq 2 -or $RC -eq 3) {
                Write-Output "  ✅ SIKERES: $Title"
                $s++
            } else {
                Write-Output "  ⚠️ SIKERTELEN: $Title"
                $f++
            }
        } catch {
            Write-Output "  ⚠️ Hiba történt: $Title"
            $f++
        }
    }
    Write-Output "--- RENDES ÖSSZEGZÉS ---"
    Write-Output "Összesen telepítve: $s sikeres, $f hibás."
} catch { Write-Output "⚠️ Nem sikerült a WU szinkronizálása $_" }
"""
                res_wu = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script], encoding='utf-8')
                for line in res_wu.stdout.splitlines():
                    if line.strip():
                        c_print(line.strip())
                
                c_print('\n🎉 MINDEN LÉPÉS KÉSZ!')
                
                try:
                    print("\n==================================================")
                    print(" A FOLYAMAT SIKERESEN BEFEJEZŐDÖTT!")
                    print("==================================================")
                    
                    import os
                    for cd in range(20, 0, -1):
                        sys.stdout.write(f"\rA gep {cd} masodperc mulva ujraindul... (Megszakitashoz zard be ezt az ablakot)")
                        sys.stdout.flush()
                        time.sleep(1)
                    print("\nUjrainditas inditasa...")
                    
                    os.system("shutdown /r /t 0 /f")
                    ctypes.windll.kernel32.FreeConsole()
                except:
                    pass

                self.emit('task_complete', {'task': 'autofix', 'status': 'Befejezve (Újraindítás Vár)'})
                time.sleep(1)
                self.emit('ask_reboot', None)

            except Exception as e:
                if str(e) == "Magyar_Megszakit_Flag":
                    self.emit('task_error', {'task': 'autofix', 'error': 'Felhasználó által megszakítva.'})
                else:
                    logging.error(f"[AUTOFIX] Hiba: {e}")
                    self.emit('task_error', {'task': 'autofix', 'error': str(e)})
                    
        self._safe_thread('autofix', worker)

    def disable_wu(self):
        logging.info("[API] disable_wu()")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: A Windows Update beállítások csak Élő rendszeren módosíthatók!', 'type': 'error'})
            return
        def worker():
            logging.info("[WU] WU driver letiltás indítása...")
            self.emit('task_start', {'task': 'disable_wu', 'title': 'WU Driver Letiltás'})
            self.emit('task_progress', {'task': 'disable_wu', 'log': 'WU driver letiltás...', 'indeterminate': True})
            try:
                with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate", 0, winreg.KEY_WRITE) as key:
                    winreg.SetValueEx(key, "ExcludeWUDriversInQualityUpdate", 0, winreg.REG_DWORD, 1)
                self.emit('task_progress', {'task': 'disable_wu', 'log': '✅ ExcludeWUDriversInQualityUpdate = 1'})
            except Exception as e:
                self.emit('task_progress', {'task': 'disable_wu', 'log': f'⚠ {e}'})
            self._run(['reg', 'add', r'HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate',
                       '/v', 'ExcludeWUDriversInQualityUpdate', '/t', 'REG_DWORD', '/d', '1', '/f'])
            try:
                with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching", 0, winreg.KEY_WRITE) as key:
                    winreg.SetValueEx(key, "SearchOrderConfig", 0, winreg.REG_DWORD, 0)
                self.emit('task_progress', {'task': 'disable_wu', 'log': '✅ SearchOrderConfig = 0'})
            except Exception as e:
                self.emit('task_progress', {'task': 'disable_wu', 'log': f'⚠ {e}'})
            self._run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching',
                       '/v', 'SearchOrderConfig', '/t', 'REG_DWORD', '/d', '0', '/f'])
            self._run('net stop wuauserv & net start wuauserv', shell=True)
            self.emit('task_progress', {'task': 'disable_wu', 'log': '✅ WU szolgáltatás újraindítva'})
            self.emit('task_complete', {'task': 'disable_wu', 'status': '✅ WU driver letiltás kész!'})
        self._safe_thread('disable_wu', worker)

    def enable_wu(self):
        logging.info("[API] enable_wu()")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: A Windows Update beállítások csak Élő rendszeren módosíthatók!', 'type': 'error'})
            return
        def worker():
            logging.info("[WU_ENABLE] Worker indult - WU engedélyezés és reset...")
            self.emit('task_start', {'task': 'enable_wu', 'title': 'WU Driver Engedélyezés + Reset'})
            self.emit('task_progress', {'task': 'enable_wu', 'log': 'WU driver engedélyezés + teljes reset...', 'indeterminate': True})

            # Delete policy
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate", 0, winreg.KEY_WRITE) as key:
                    winreg.DeleteValue(key, "ExcludeWUDriversInQualityUpdate")
                logging.info("[WU_ENABLE] ExcludeWUDrivers policy törölve")
                self.emit('task_progress', {'task': 'enable_wu', 'log': '✅ ExcludeWUDrivers policy törölve'})
            except FileNotFoundError:
                logging.debug("[WU_ENABLE] Policy nem létezett")
                self.emit('task_progress', {'task': 'enable_wu', 'log': '  Policy nem létezett'})
            except Exception as e:
                logging.warning(f"[WU_ENABLE] Policy törlés hiba: {e}")
                self.emit('task_progress', {'task': 'enable_wu', 'log': f'⚠ {e}'})
                try:
                    with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate", 0, winreg.KEY_WRITE) as key:
                        winreg.SetValueEx(key, "ExcludeWUDriversInQualityUpdate", 0, winreg.REG_DWORD, 0)
                except Exception as e:
                    logging.debug(e)

            # SearchOrderConfig = 1
            try:
                with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching", 0, winreg.KEY_WRITE) as key:
                    winreg.SetValueEx(key, "SearchOrderConfig", 0, winreg.REG_DWORD, 1)
                logging.info("[WU_ENABLE] SearchOrderConfig = 1")
                self.emit('task_progress', {'task': 'enable_wu', 'log': '✅ SearchOrderConfig = 1'})
            except Exception as e:
                logging.warning(f"[WU_ENABLE] SearchOrderConfig hiba: {e}")
                self.emit('task_progress', {'task': 'enable_wu', 'log': f'⚠ {e}'})

            self._run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching',
                       '/v', 'SearchOrderConfig', '/t', 'REG_DWORD', '/d', '1', '/f'])
            self._run(['reg', 'delete', r'HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate',
                       '/v', 'ExcludeWUDriversInQualityUpdate', '/f'])

            # Stop services
            logging.info("[WU_ENABLE] Szolgáltatások leállítása...")
            for svc in ['wuauserv', 'bits', 'cryptsvc']:
                self._run(f'net stop {svc} /y', shell=True)
            time.sleep(2)

            # Delete SoftwareDistribution
            sysroot = os.environ.get('SYSTEMROOT', r'C:\Windows')
            sw_dist = os.path.join(sysroot, 'SoftwareDistribution')
            logging.info(f"[WU_ENABLE] SoftwareDistribution törlése: {sw_dist}")
            self.emit('task_progress', {'task': 'enable_wu', 'log': 'SoftwareDistribution törlése...'})
            for _ in range(3):
                try:
                    if os.path.exists(sw_dist):
                        shutil.rmtree(sw_dist, ignore_errors=False)
                        logging.info("[WU_ENABLE] SoftwareDistribution törölve")
                        self.emit('task_progress', {'task': 'enable_wu', 'log': '  ✅ Törölve'})
                        break
                except Exception as e:
                    logging.warning(f"[WU_ENABLE] SoftwareDistribution törlés újrapróbálás: {e}")
                    self.emit('task_progress', {'task': 'enable_wu', 'log': f'  ⚠ Újrapróbálás: {e}'})
                    time.sleep(3)

            # Rename catroot2
            catroot2 = os.path.join(sysroot, 'System32', 'catroot2')
            bak = catroot2 + '.bak'
            try:
                if os.path.exists(bak):
                    shutil.rmtree(bak, ignore_errors=True)
                if os.path.exists(catroot2):
                    os.rename(catroot2, bak)
                    logging.info("[WU_ENABLE] catroot2 átnevezve")
                    self.emit('task_progress', {'task': 'enable_wu', 'log': '✅ catroot2 átnevezve'})
            except Exception as e:
                logging.warning(f"[WU_ENABLE] catroot2 hiba: {e}")
                self.emit('task_progress', {'task': 'enable_wu', 'log': f'⚠ catroot2: {e}'})

            # Re-register DLLs
            logging.info("[WU_ENABLE] WU DLL-ek újraregisztrálása...")
            sys32 = os.path.join(sysroot, 'System32')
            for dll in ['wuaueng.dll', 'wuapi.dll', 'wups.dll', 'wups2.dll', 'wuwebv.dll', 'wucltux.dll']:
                fp = os.path.join(sys32, dll)
                if os.path.exists(fp):
                    self._run(f'regsvr32.exe /s "{fp}"', shell=True)
            self.emit('task_progress', {'task': 'enable_wu', 'log': '✅ WU DLL-ek újraregisztrálva'})

            # Winsock reset
            logging.info("[WU_ENABLE] Winsock reset...")
            self._run('netsh winsock reset', shell=True)

            # Start services
            logging.info("[WU_ENABLE] Szolgáltatások indítása...")
            for svc in ['cryptsvc', 'bits', 'wuauserv']:
                for _ in range(3):
                    res = self._run(f'net start {svc}', shell=True)
                    if res.returncode == 0 or 'already' in (res.stdout + res.stderr).lower():
                        break
                    time.sleep(3)

            self._run('wuauclt.exe /resetauthorization /detectnow', shell=True)
            self._run('UsoClient.exe StartScan', shell=True)
            logging.info("[WU_ENABLE] Kész!")
            self.emit('task_progress', {'task': 'enable_wu', 'log': '✅ Frissítés-keresés elindítva'})
            self.emit('task_complete', {'task': 'enable_wu', 'status': '✅ WU engedélyezés + reset kész!'})

        self._safe_thread('enable_wu', worker)

    def restart_wu(self):
        logging.info("[API] restart_wu()")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: A Windows Update szolgáltatások csak Élő rendszeren indíthatók újra!', 'type': 'error'})
            return
        def worker():
            logging.info("[WU_RESTART] Worker indult - szolgáltatások újraindítása...")
            self.emit('task_start', {'task': 'restart_wu', 'title': 'WU Szolgáltatások Újraindítása'})
            self.emit('task_progress', {'task': 'restart_wu', 'log': 'WU szolgáltatások újraindítása...', 'indeterminate': True})

            logging.info("[WU_RESTART] Szolgáltatások leállítása...")
            for svc in ['wuauserv', 'bits', 'cryptsvc', 'msiserver']:
                self._run(f'net stop {svc} /y', shell=True)
                self.emit('task_progress', {'task': 'restart_wu', 'log': f'  stop {svc}'})
            time.sleep(2)
            logging.info("[WU_RESTART] Szolgáltatások indítása...")
            for svc in ['rpcss', 'cryptsvc', 'bits', 'msiserver', 'wuauserv']:
                for _ in range(3):
                    res = self._run(f'net start {svc}', shell=True)
                    if res.returncode == 0 or 'already' in (res.stdout + res.stderr).lower():
                        break
                    time.sleep(3)
                self.emit('task_progress', {'task': 'restart_wu', 'log': f'  start {svc}'})
            self._run('wuauclt.exe /resetauthorization /detectnow', shell=True)
            self._run('UsoClient.exe StartScan', shell=True)
            logging.info("[WU_RESTART] Kész!")
            self.emit('task_progress', {'task': 'restart_wu', 'log': '✅ Frissítés-keresés elindítva'})
            self.emit('task_complete', {'task': 'restart_wu', 'status': '✅ WU szolgáltatások újraindítva!'})

        self._safe_thread('restart_wu', worker)

    # ================================================================
    # BACKUP / RESTORE
    # ================================================================
    def backup_third_party(self):
        logging.info("[API] backup_third_party()")
        dest = self.select_directory('Válassz mappát a driverek kimentéséhez')
        if not dest:
            logging.info("[BACKUP] Mégse - nincs mappa kiválasztva")
            return
        logging.info(f"[BACKUP] Third-party backup indítása -> {dest}")
        self._cancel_flag = False

        def worker():
            folder = os.path.join(dest, f"DriverDoktor_Export_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            logging.info(f"[BACKUP] Célmappa létrehozása: {folder}")
            os.makedirs(folder, exist_ok=True)
            self.emit('task_start', {'task': 'backup', 'title': 'Driver Exportálás'})
            self.emit('task_progress', {'task': 'backup', 'log': f'Célmappa: {folder}\nExportálás indítása...', 'indeterminate': True})

            logging.info("[BACKUP] DISM export-driver futtatása...")
            dism_cmd = ['dism', f'/Image:{self.target_os_path}', '/export-driver', f'/destination:{folder}'] if self.target_os_path else ['dism', '/online', '/export-driver', f'/destination:{folder}']
            process = subprocess.Popen(
                dism_cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                startupinfo=self._si, creationflags=self._nw, errors='replace')

            cancelled = False
            for line in process.stdout:
                if self._check_cancel():
                    process.terminate()
                    process.wait()  # Prevent zombie process
                    cancelled = True
                    break
                line = line.strip()
                if not line:
                    continue
                logging.debug(f"[BACKUP] DISM: {line[:100]}")
                m = re.search(r'(\d+)\s*(?:/|of)\s*(\d+)', line, re.I)
                if m:
                    self.emit('task_progress', {'task': 'backup', 'current': int(m.group(1)), 'total': int(m.group(2)),
                                                'counter': f'{m.group(1)}/{m.group(2)}', 'status': line[:60]})
                self.emit('task_progress', {'task': 'backup', 'log': line})
            process.wait()

            if cancelled:
                self.emit('task_complete', {'task': 'backup', 'status': '❗ Megszakítva!', 'log': '\n--- MEGSZAKÍTVA! ---'})
                return

            success = process.returncode == 0
            logging.info(f"[BACKUP] DISM befejezve, returncode={process.returncode}")
            self.emit('task_complete', {'task': 'backup',
                                        'status': f'{"✅ Sikeres export!" if success else "❌ Hiba!"} Mappa: {folder}',
                                        'log': f'\n--- {"Sikeres" if success else "Hibás"} export: {folder} ---'})
        self._safe_thread('backup', worker)

    def backup_all(self):
        logging.info("[API] backup_all()")
        dest = self.select_directory('Válassz mappát az ÖSSZES driver kimentéséhez')
        if not dest:
            logging.info("[BACKUP_ALL] Mégse - nincs mappa kiválasztva")
            return
        logging.info(f"[BACKUP_ALL] Összes driver backup indítása -> {dest}")
        self._cancel_flag = False

        def worker():
            folder = os.path.join(dest, f"DriverDoktor_FullExport_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            os.makedirs(folder, exist_ok=True)
            self.emit('task_start', {'task': 'backup', 'title': 'ÖSSZES Driver Exportálása'})
            self.emit('task_progress', {'task': 'backup', 'log': 'Driver lista lekérdezése...', 'indeterminate': True})

            success = 0
            fail = 0
            cancelled = False

            if self.target_os_path:
                self.emit('task_progress', {'task': 'backup', 'log': 'DISM export indítása a kiválasztott rendszerből...'})
                res = self._run(['dism', f'/Image:{self.target_os_path}', '/export-driver', f'/destination:{folder}'])
                if res.returncode == 0:
                    success += 1
                else:
                    fail += 1
            else:
                enum_res = self._run(['pnputil', '/enum-drivers'])
                all_infs = re.findall(r'(oem\d+\.inf)', enum_res.stdout, re.I)
                self.emit('task_progress', {'task': 'backup', 'log': f'OEM driverek: {len(all_infs)} db'})

                for i, inf in enumerate(all_infs):
                    if self._check_cancel():
                        cancelled = True
                        break
                    inf_folder = os.path.join(folder, inf.replace('.in', ''))
                    os.makedirs(inf_folder, exist_ok=True)
                    res = self._run(['pnputil', '/export-driver', inf, inf_folder])
                    if res.returncode == 0:
                        success += 1
                    else:
                        fail += 1
                    self.emit('task_progress', {'task': 'backup', 'current': i + 1, 'total': len(all_infs),
                                                'counter': f'{i+1}/{len(all_infs)}', 'status': f'Export: {inf}'})

            if cancelled:
                self.emit('task_complete', {'task': 'backup', 'status': f'❗ Megszakítva! OEM: {success} db exportálva',
                                            'log': f'\n--- MEGSZAKÍTVA! Sikeres: {success}, Sikertelen: {fail} ---'})
                return

            # Copy inbox drivers (FileRepository + INF)
            if self._check_cancel():
                self.emit('task_complete', {'task': 'backup', 'status': '❗ Megszakítva!', 'log': '\n--- MEGSZAKÍTVA! ---'})
                return
            self.emit('task_progress', {'task': 'backup', 'log': 'Windows inbox driverek másolása (FileRepository)...', 'indeterminate': True})
            windows_dir = os.path.join(self.target_os_path, 'Windows') if self.target_os_path else os.environ.get('SYSTEMROOT', r'C:\Windows')
            driverstore = os.path.join(windows_dir, 'System32', 'DriverStore', 'FileRepository')
            inbox_folder = os.path.join(folder, '_Windows_Inbox_Drivers')
            os.makedirs(inbox_folder, exist_ok=True)
            self._run(['robocopy', driverstore, inbox_folder, '/E', '/R:0', '/W:0', '/NFL', '/NDL', '/NJH', '/NJS', '/NC', '/NS', '/NP'])

            if self._check_cancel():
                self.emit('task_complete', {'task': 'backup', 'status': '❗ Megszakítva!', 'log': '\n--- MEGSZAKÍTVA! ---'})
                return
            self.emit('task_progress', {'task': 'backup', 'log': 'Windows INF mappa másolása...'})
            inf_src = os.path.join(windows_dir, 'INF')
            inbox_inf_folder = os.path.join(folder, '_Windows_Inbox_INF')
            os.makedirs(inbox_inf_folder, exist_ok=True)
            self._run(['robocopy', inf_src, inbox_inf_folder, '/E', '/R:0', '/W:0', '/NFL', '/NDL', '/NJH', '/NJS', '/NC', '/NS', '/NP'])

            total_size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fns in os.walk(folder) for f in fns
                             if os.path.exists(os.path.join(dp, f)))
            size_mb = total_size / (1024 * 1024)
            self.emit('task_complete', {'task': 'backup',
                                        'status': f'✅ Kész! OEM: {"Sikeres" if success else "Sikertelen"}, Inbox másolva. Méret: {size_mb:.0f} MB',
                                        'log': f'\n--- Export kész: {folder} ({size_mb:.0f} MB) | Sikeres: {success}, Sikertelen: {fail} ---'})
        self._safe_thread('backup', worker)

    def create_restore_point(self):
        logging.info("[API] create_restore_point()")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: Visszaállítási pont csak Élő rendszeren készíthető!', 'type': 'error'})
            return
        def worker():
            logging.info("[RESTORE_POINT] Worker indult - visszaállítási pont létrehozása...")
            desc = f"DriverDoktor_Backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            logging.info(f"[RESTORE_POINT] Név: {desc}")
            self.emit('task_start', {'task': 'rp', 'title': 'Visszaállítási Pont'})
            self.emit('task_progress', {'task': 'rp', 'log': 'Rendszervédelem engedélyezése...', 'indeterminate': True})

            # 1) Enable System Restore on C: (force enable even if disabled)
            logging.info("[RESTORE_POINT] Rendszervédelem engedélyezése...")
            enable_ps = '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; try { Enable-ComputerRestore -Drive "$($env:SystemDrive)\\" -ErrorAction Stop; Write-Output "OK" } catch { Write-Output "FAIL: $($_.Exception.Message)" }'
            enable_res = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", enable_ps], encoding='utf-8')
            enable_out = (enable_res.stdout or '').strip()
            if 'FAIL' in enable_out:
                logging.warning(f"[RESTORE_POINT] Enable-ComputerRestore hiba: {enable_out}")
                # Try via registry + vssadmin as fallback
                self.emit('task_progress', {'task': 'rp', 'log': f'⚠ Enable-ComputerRestore hiba: {enable_out}\nRegistry + vssadmin fallback...'})
                self._run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore', '/v', 'DisableSR', '/t', 'REG_DWORD', '/d', '0', '/f'])
                self._run(['vssadmin', 'resize', 'shadowstorage', f'/for={os.environ.get("SystemDrive", "C:")}', f'/on={os.environ.get("SystemDrive", "C:")}', '/maxsize=5%'])
                # Retry enable
                enable_res2 = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", enable_ps], encoding='utf-8')
                enable_out2 = (enable_res2.stdout or '').strip()
                if 'FAIL' in enable_out2:
                    logging.error(f"[RESTORE_POINT] Rendszervédelem nem kapcsolható be: {enable_out2}")
                    self.emit('task_complete', {'task': 'rp', 'status': f'❌ Rendszervédelem nem kapcsolható be: {enable_out2}'})
                    return
                logging.info("[RESTORE_POINT] Rendszervédelem bekapcsolva (fallback)")
                self.emit('task_progress', {'task': 'rp', 'log': '✅ Rendszervédelem bekapcsolva (fallback)'})
            else:
                logging.info("[RESTORE_POINT] Rendszervédelem bekapcsolva")
                self.emit('task_progress', {'task': 'rp', 'log': '✅ Rendszervédelem bekapcsolva'})

            # 2) Disable 24-hour frequency limit
            logging.info("[RESTORE_POINT] 24 órás limit feloldása...")
            self._run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore', 
                       '/v', 'SystemRestorePointCreationFrequency', '/t', 'REG_DWORD', '/d', '0', '/f'])

            # 3) Create restore point
            logging.info("[RESTORE_POINT] Checkpoint-Computer futtatása...")
            self.emit('task_progress', {'task': 'rp', 'log': f'Visszaállítási pont: {desc}', 'status': 'Pont létrehozása...'})
            create_ps = f'[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; try {{ Checkpoint-Computer -Description "{desc}" -RestorePointType "MODIFY_SETTINGS" -ErrorAction Stop; Write-Output "OK" }} catch {{ Write-Output "FAIL: $($_.Exception.Message)" }}'
            res = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", create_ps], encoding='utf-8')
            create_out = (res.stdout or '').strip()
            logging.debug(f"[RESTORE_POINT] Checkpoint result: {create_out}")

            # 4) Verify
            logging.info("[RESTORE_POINT] Ellenőrzés...")
            verify_ps = f'[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; (Get-ComputerRestorePoint | Where-Object {{ $_.Description -eq "{desc}" }}).Description'
            verify_res = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", verify_ps], encoding='utf-8')
            verified = desc in (verify_res.stdout or '')
            logging.debug(f"[RESTORE_POINT] Verified: {verified}")

            if 'OK' in create_out and verified:
                logging.info(f"[RESTORE_POINT] Sikeresen létrehozva: {desc}")
                self.emit('task_complete', {'task': 'rp', 'status': f'✅ Visszaállítási pont létrehozva: {desc}'})
            elif 'OK' in create_out:
                logging.warning("[RESTORE_POINT] Lefutott de nem ellenőrizhető (késleltetett létrehozás?)")
                self.emit('task_complete', {'task': 'rp', 'status': '⚠ Visszaállítási pont létrehozás elindítva (ellenőrzés később)'})
            else:
                logging.error(f"[RESTORE_POINT] Hiba: {create_out}")
                self.emit('task_complete', {'task': 'rp', 'status': f'❌ Hiba: {create_out}'})
        self._safe_thread('rp', worker)

    def repair_bcd_standalone(self):
        """Önálló BCD javítás - a felhasználó kiválasztja a meghajtót."""
        logging.info("[API] repair_bcd_standalone()")
        target = self.select_directory('Válaszd ki a HALOTT WINDOWS meghajtóját (ahol a Windows mappa van)')
        if not target:
            logging.info("[BCD] Mégse - nincs cél kiválasztva")
            return
        target = os.path.splitdrive(os.path.abspath(target))[0] + "\\"
        logging.info(f"[BCD] Standalone BCD javítás: {target}")
        
        def worker():
            self.emit('task_start', {'task': 'bcd', 'title': 'BCD Boot Hiba Javítása'})
            self.emit('task_progress', {'task': 'bcd', 'log': f'Kiválasztott meghajtó: {target}\n', 'indeterminate': True})
            
            # Ellenőrzés - van-e Windows mappa
            windows_path = os.path.join(target, 'Windows')
            if not os.path.exists(windows_path):
                self.emit('task_progress', {'task': 'bcd', 'log': f'❌ Hiba: Windows mappa nem található!\n   Elérési út: {windows_path}'})
                self.emit('task_complete', {'task': 'bcd', 'status': '❌ Windows mappa nem található!'})
                return
            
            # BCD javítás (ugyanaz a kód mint a restore után)
            self._repair_bcd_for_task(target, 'bcd')
            
            self.emit('task_progress', {'task': 'bcd', 'log': '\n==== BCD JAVÍTÁS BEFEJEZVE ===='})
            self.emit('task_complete', {'task': 'bcd', 'status': '✅ BCD javítás befejezve!'})
        
        self._safe_thread('bcd', worker)
    
    def _repair_bcd_for_task(self, target_drive, task_name):
        """BCD javítás közös logika - használható restore-ból vagy önállóan is."""
        target_drive = target_drive.rstrip('\\') + '\\'
        target_letter = target_drive[0].upper()
        
        self.emit('task_progress', {'task': task_name, 'log': '\n--- BOOT LOADER (BCD) JAVÍTÁS ---'})
        self.emit('task_progress', {'task': task_name, 'log': f'Cél Windows meghajtó: {target_drive}'})
        self.emit('task_progress', {'task': task_name, 'log': 'A Windows meghajtó lemezének azonosítása...'})
        
        disk_number = None
        efi_letter = None
        efi_partition = None
        
        try:
            # Volume-ok listázása
            res = self._run(['diskpart'], input='list volume\n', timeout=30)
            
            if res.returncode == 0 and res.stdout:
                lines = res.stdout.splitlines()
                target_volume = None
                
                # Windows volume keresése
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 3:
                        for i, p in enumerate(parts):
                            if p.upper() == target_letter and i >= 1:
                                try:
                                    target_volume = int(parts[1])
                                except (ValueError, IndexError):
                                    pass
                                break
                
                if target_volume is not None:
                    self.emit('task_progress', {'task': task_name, 'log': f'Windows volume: {target_volume}'})
                    
                    # Disk azonosítása
                    res2 = self._run(['diskpart'], input=f'select volume {target_volume}\ndetail volume\n', timeout=30)
                    
                    if res2.returncode == 0 and res2.stdout:
                        for line in res2.stdout.splitlines():
                            if 'Disk' in line and '#' not in line:
                                parts = line.split()
                                for p in parts:
                                    if p.isdigit():
                                        disk_number = int(p)
                                        break
                                if disk_number is not None:
                                    break
                    
                    if disk_number is not None:
                        self.emit('task_progress', {'task': task_name, 'log': f'Lemez: Disk {disk_number}'})
                        
                        # EFI partíció keresése ezen a lemezen
                        res3 = self._run(['diskpart'], input=f'select disk {disk_number}\nlist partition\n', timeout=30)
                        
                        if res3.returncode == 0 and res3.stdout:
                            for line in res3.stdout.splitlines():
                                line_upper = line.upper()
                                if 'SYSTEM' in line_upper or 'EFI' in line_upper:
                                    parts = line.split()
                                    for i, p in enumerate(parts):
                                        if p.isdigit() and i >= 1:
                                            efi_partition = int(p)
                                            break
                                    if efi_partition:
                                        break
                        
                        if efi_partition:
                            self.emit('task_progress', {'task': task_name, 'log': f'EFI partíció: Partition {efi_partition}'})
                            
                            # Szabad betűjel keresése
                            used_letters = set()
                            for line in lines:
                                parts = line.split()
                                for p in parts:
                                    if len(p) == 1 and p.isalpha():
                                        used_letters.add(p.upper())
                            
                            free_letter = None
                            for c in 'STUVWXYZ':
                                if c not in used_letters:
                                    free_letter = c
                                    break
                            
                            if free_letter:
                                res4 = self._run(['diskpart'], 
                                    input=f'select disk {disk_number}\nselect partition {efi_partition}\nassign letter={free_letter}\n',
                                    timeout=30)
                                if res4.returncode == 0:
                                    efi_letter = free_letter + ':'
                                    self.emit('task_progress', {'task': task_name, 'log': f'EFI betűjel hozzárendelve: {efi_letter}'})
        except Exception as e:
            logging.warning(f"[BCD] Diskpart hiba: {e}")
            self.emit('task_progress', {'task': task_name, 'log': f'⚠️ Lemez azonosítási hiba: {e}'})
        
        # bcdboot futtatása
        success = False
        
        if efi_letter:
            self.emit('task_progress', {'task': task_name, 'log': f'bcdboot {target_drive}Windows /s {efi_letter} /f UEFI'})
            res = self._run(['bcdboot', f'{target_drive}Windows', '/s', efi_letter, '/f', 'UEFI'])
            if res.returncode == 0:
                success = True
                self.emit('task_progress', {'task': task_name, 'log': '✅ BCD sikeresen újraépítve (UEFI)!'})
            else:
                self.emit('task_progress', {'task': task_name, 'log': '⚠️ UEFI bcdboot hiba, fallback...'})
            
            # EFI betűjel eltávolítása
            try:
                self._run(['diskpart'], 
                    input=f'select disk {disk_number}\nselect partition {efi_partition}\nremove letter={efi_letter[0]}\n',
                    timeout=30)
            except Exception as e:
                logging.debug(e)
        
        if not success:
            # Fallback: bcdboot /s nélkül - automatikusan megkeresi a system partíciót
            self.emit('task_progress', {'task': task_name, 'log': f'bcdboot {target_drive}Windows /f ALL'})
            res = self._run(['bcdboot', f'{target_drive}Windows', '/f', 'ALL'])
            if res.returncode == 0:
                success = True
                self.emit('task_progress', {'task': task_name, 'log': '✅ BCD sikeresen újraépítve (ALL)!'})
            else:
                err_msg = res.stderr.strip() if res.stderr else res.stdout.strip() if res.stdout else f'Exit code: {res.returncode}'
                self.emit('task_progress', {'task': task_name, 'log': f'⚠️ bcdboot hiba (0x{res.returncode:X}): {err_msg[:300]}'})
        
        if not success:
            self.emit('task_progress', {'task': task_name, 'log': 'bootrec parancsok futtatása...'})
            for cmd in ['/fixmbr', '/fixboot', '/rebuildbcd']:
                res = self._run(['bootrec', cmd])
                status = '✅' if res.returncode == 0 else '⚠️ (nem elérhető)'
                self.emit('task_progress', {'task': task_name, 'log': f'  bootrec {cmd}: {status}'})
        
        return success

    def restore_online(self):
        logging.info("[API] restore_online()")
        source = self.select_directory('ÉLŐ MÓD: Válassz kimentett driver mappát')
        if not source:
            logging.info("[RESTORE] Mégse - nincs forrás kiválasztva")
            return
        logging.info(f"[RESTORE] Online restore indítása: source={source}")
        self._run_restore(online=True, source=source, target=None)

    def restore_offline(self):
        logging.info("[API] restore_offline()")
        target = self.select_directory('OFFLINE MÓD: 1. Válaszd ki a HALOTT WINDOWS meghajtóját')
        if not target:
            logging.info("[RESTORE] Mégse - nincs cél kiválasztva")
            return
        target = os.path.splitdrive(os.path.abspath(target))[0] + "\\"
        logging.info(f"[RESTORE] Offline target: {target}")
        source = self.select_directory('OFFLINE MÓD: 2. Válassz kimentett driver mappát')
        if not source:
            logging.info("[RESTORE] Mégse - nincs forrás kiválasztva")
            return
        logging.info(f"[RESTORE] Offline restore indítása: source={source}, target={target}")
        self._run_restore(online=False, source=source, target=target)

    def _run_restore(self, online, source, target):
        logging.info(f"[RESTORE] _run_restore: online={online}, source={source}, target={target}")
        self._cancel_flag = False
        def worker():
            mode = 'Élő' if online else 'Offline'
            logging.info(f"[RESTORE] Worker indult - {mode} mód")
            self.emit('task_start', {'task': 'restore', 'title': f'Driver Visszaállítás ({mode})'})
            self.emit('task_progress', {'task': 'restore', 'log': f'=== {mode.upper()} RESTORE ===\nForrás: {source}\nCél: {target or "jelenlegi rendszer"}\n', 'indeterminate': True})

            norm_source = os.path.normpath(source)
            norm_target = os.path.normpath(target) if target else None
            logging.debug(f"[RESTORE] norm_source={norm_source}, norm_target={norm_target}")

            # Detect source type
            is_wim_extract = not online and "Windows_Gyari_Alap_Driverek" in norm_source
            inbox_subfolder = os.path.join(norm_source, "_Windows_Inbox_Drivers") if not online else None
            has_inbox_subfolder = inbox_subfolder and os.path.isdir(inbox_subfolder)
            logging.info(f"[RESTORE] Típus detektálás: is_wim_extract={is_wim_extract}, has_inbox_subfolder={has_inbox_subfolder}")

            def force_copy(src, dst):
                """Robocopy-based forced copy with fallback for inbox/system drivers."""
                logging.debug(f"[RESTORE] force_copy: {src} -> {dst}")
                if not os.path.exists(src):
                    logging.warning(f"[RESTORE] Forrás nem létezik: {src}")
                    return
                os.makedirs(dst, exist_ok=True)
                self.emit('task_progress', {'task': 'restore', 'log': f'\n  Robocopy indul: {os.path.basename(src)} -> {os.path.basename(dst)}\n  (Backup mód - Windows jogosultságok megkerülése)'})
                cmd = ['robocopy', src, dst, '/E', '/ZB', '/R:1', '/W:1', '/COPY:DAT', '/NC', '/NS', '/NFL', '/NDL', '/NP']
                res = self._run(cmd)

                if res.returncode < 8:
                    logging.info(f"[RESTORE] Robocopy sikeres, returncode={res.returncode}")
                    self.emit('task_progress', {'task': 'restore', 'log': f'  ✅ Sikeres robocopy kényszerítés ({res.returncode})'})
                else:
                    self.emit('task_progress', {'task': 'restore', 'log': f'  ⚠️ Robocopy hiba ({res.returncode}), végső tartalék: mappánkénti jogszerzés (lassabb)...'})
                    for root, _, files in os.walk(src):
                        if self._cancel_flag: return
                        rel = os.path.relpath(root, src)
                        target_dir = os.path.join(dst, rel) if rel != '.' else dst
                        os.makedirs(target_dir, exist_ok=True)

                        for f in files:
                            if self._cancel_flag: return
                            sfile = os.path.join(root, f)
                            dfile = os.path.join(target_dir, f)
                            if os.path.exists(dfile):
                                self._run(f'takeown /f "{dfile}" /A', shell=True)
                                self._run(f'icacls "{dfile}" /grant *S-1-5-32-544:F', shell=True)
                                self._run(f'attrib -R "{dfile}"', shell=True)
                            try:
                                shutil.copy2(sfile, dfile)
                            except Exception as e:
                                self.emit('task_progress', {'task': 'restore', 'log': f'❌ Hiba ({f}): {e}'})
                    self.emit('task_progress', {'task': 'restore', 'log': '  ✅ Fallback másolás befejeződött.'})

            def run_dism_add_driver(driver_path, label=""):
                """Run DISM /Add-Driver on a folder with /Recurse. Returns (returncode, cancelled)."""
                scratch = os.path.join(norm_target, "Scratch")
                os.makedirs(scratch, exist_ok=True)
                cmd = ['dism', f'/Image:{norm_target}', '/Add-Driver', f'/Driver:{driver_path}', '/Recurse', '/ForceUnsigned', f'/ScratchDir:{scratch}']
                self.emit('task_progress', {'task': 'restore', 'log': f'{label}Parancs: {" ".join(cmd)}'})
                process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                           startupinfo=self._si, creationflags=self._nw, errors='replace')
                cancelled = False
                for line in process.stdout:
                    if self._check_cancel():
                        process.terminate()
                        cancelled = True
                        break
                    stripped = line.strip()
                    if stripped:
                        self.emit('task_progress', {'task': 'restore', 'log': stripped})
                process.wait()
                if not cancelled:
                    self.emit('task_progress', {'task': 'restore', 'log': f'Return code: {process.returncode}'})
                return (process.returncode, cancelled)

            if online:
                cmd = ['pnputil', '/add-driver', f"{norm_source}\\*.inf", '/subdirs', '/install']
                self.emit('task_progress', {'task': 'restore', 'log': f'Parancs: {" ".join(cmd)}'})
                process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                           startupinfo=self._si, creationflags=self._nw, errors='replace')
                cancelled = False
                for line in process.stdout:
                    if self._check_cancel():
                        process.terminate()
                        cancelled = True
                        break
                    self.emit('task_progress', {'task': 'restore', 'log': line.strip()})
                process.wait()
                if cancelled:
                    self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                    return
                self.emit('task_progress', {'task': 'restore', 'log': f'\nReturn code: {process.returncode}'})
            elif is_wim_extract:
                # WIM-ből kimentett driverek (Windows_Gyari_Alap_Driverek_*)
                # Ezek FileRepository + INF formátumban vannak
                self.emit('task_progress', {'task': 'restore', 'log': 'WIM-ből kimentett gyári driverek visszaállítása...'})
                new_format_repo = os.path.join(norm_source, "FileRepository")
                new_format_inf = os.path.join(norm_source, "INF")
                target_repo = os.path.join(norm_target, "Windows", "System32", "DriverStore", "FileRepository")
                target_inf = os.path.join(norm_target, "Windows", "INF")

                try:
                    if os.path.exists(new_format_repo):
                        self.emit('task_progress', {'task': 'restore', 'log': '1/2 FileRepository és INF fizikai másolása...'})
                        force_copy(new_format_repo, target_repo)
                        if self._check_cancel():
                            self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                            return
                        if os.path.exists(new_format_inf):
                            force_copy(new_format_inf, target_inf)
                            if self._check_cancel():
                                self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                                return
                    else:
                        self.emit('task_progress', {'task': 'restore', 'log': '1/2 DriverStore fizikai másolása...'})
                        force_copy(norm_source, target_repo)
                        if self._check_cancel():
                            self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                            return

                    self.emit('task_progress', {'task': 'restore', 'log': '✅ Fizikai másolás kész!'})
                except Exception as e:
                    err_msg = str(e)
                    if len(err_msg) > 300: err_msg = err_msg[:300] + "..."
                    self.emit('task_progress', {'task': 'restore', 'log': f'⚠️ Másolási hiba: {err_msg}'})

                # DISM regisztrálás a fizikai másolás után
                self.emit('task_progress', {'task': 'restore', 'log': '\n2/2 DISM driver regisztrálás (inbox drivereknél sok hiba normális)...'})
                _, dism_cancelled = run_dism_add_driver(norm_source, "")
                if dism_cancelled:
                    self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                    return
                self.emit('task_progress', {'task': 'restore', 'log': '✅ A fizikai másolás + DISM regisztrálás kész. Az inbox driverek a másolásnak köszönhetően elérhetőek.'})

            elif has_inbox_subfolder:
                # DriverDoktor_FullExport / ALL_Driver_Backup formátum: _Windows_Inbox_Drivers + oem almappák
                self.emit('task_progress', {'task': 'restore', 'log': 'Teljes export formátum észlelve (DriverDoktor_FullExport / ALL_Driver_Backup).\n'
                                            'Az inbox drivereket fizikailag másoljuk (DISM nem tudja telepíteni őket),\n'
                                            'az OEM drivereket DISM-mel regisztráljuk.\n'})

                # 1) Inbox driverek fizikai másolása (FileRepository + INF)
                target_repo = os.path.join(norm_target, "Windows", "System32", "DriverStore", "FileRepository")
                target_inf = os.path.join(norm_target, "Windows", "INF")
                inbox_inf_subfolder = os.path.join(norm_source, "_Windows_Inbox_INF")
                self.emit('task_progress', {'task': 'restore', 'log': '--- 1. LÉPÉS: Inbox driverek fizikai másolása a DriverStore-ba ---'})
                try:
                    force_copy(inbox_subfolder, target_repo)
                    if self._check_cancel():
                        self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                        return
                    if os.path.isdir(inbox_inf_subfolder):
                        self.emit('task_progress', {'task': 'restore', 'log': 'Windows INF mappa visszamásolása (új formátumú backup)...'})
                        force_copy(inbox_inf_subfolder, target_inf)
                        if self._check_cancel():
                            self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                            return
                    else:
                        # Régi backup: nincs _Windows_Inbox_INF, ezért a FileRepository almappáiból
                        # kiszedjük az .inf fájlokat és bemásoljuk a Windows\INF-be
                        self.emit('task_progress', {'task': 'restore', 'log': 'Régi backup formátum: _Windows_Inbox_INF nem található.\n'
                                                    'INF fájlok kinyerése a FileRepository almappáiból...'})
                        os.makedirs(target_inf, exist_ok=True)
                        inf_count = 0
                        for repo_dir in os.listdir(inbox_subfolder):
                            repo_path = os.path.join(inbox_subfolder, repo_dir)
                            if not os.path.isdir(repo_path):
                                continue
                            for fname in os.listdir(repo_path):
                                if fname.lower().endswith('.in'):
                                    src_inf = os.path.join(repo_path, fname)
                                    dst_inf = os.path.join(target_inf, fname)
                                    try:
                                        shutil.copy2(src_inf, dst_inf)
                                        inf_count += 1
                                    except Exception as e:
                                        logging.debug(e)
                        self.emit('task_progress', {'task': 'restore', 'log': f'✅ {inf_count} db .inf fájl kinyerve a Windows\\INF mappába (.pnf-eket a Windows legenerálja bootoláskor).'})
                    self.emit('task_progress', {'task': 'restore', 'log': '✅ Inbox driverek fizikai másolása kész!'})
                except Exception as e:
                    err_msg = str(e)
                    if len(err_msg) > 300: err_msg = err_msg[:300] + "..."
                    self.emit('task_progress', {'task': 'restore', 'log': f'⚠️ Inbox másolási hiba: {err_msg}'})

                # 2) OEM driverek DISM-mel (almappák, amik nem _Windows_Inbox_Drivers)
                oem_folders = []
                for item in os.listdir(norm_source):
                    item_path = os.path.join(norm_source, item)
                    if os.path.isdir(item_path) and item not in ("_Windows_Inbox_Drivers", "_Windows_Inbox_INF"):
                        # Check if folder contains any .inf files (directly or in subfolders)
                        has_inf = any(f.lower().endswith('.in') for _, _, fns in os.walk(item_path) for f in fns)
                        if has_inf:
                            oem_folders.append(item_path)

                if oem_folders:
                    self.emit('task_progress', {'task': 'restore', 'log': f'\n--- 2. LÉPÉS: {len(oem_folders)} db OEM driver mappa DISM regisztrálása ---'})
                    for i, oem_path in enumerate(oem_folders):
                        if self._check_cancel():
                            self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                            return
                        self.emit('task_progress', {'task': 'restore', 'log': f'\n[{i+1}/{len(oem_folders)}] {os.path.basename(oem_path)}:'})
                        _, dism_cancelled = run_dism_add_driver(oem_path, "  ")
                        if dism_cancelled:
                            self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                            return
                    self.emit('task_progress', {'task': 'restore', 'log': '\n✅ OEM driverek DISM regisztrálása kész!'})
                else:
                    self.emit('task_progress', {'task': 'restore', 'log': '\nNincs OEM driver mappa a backup-ban.'})

            else:
                # Egyéb mappa (pl. DriverDoktor_Export / Driver_Backup third-party export) — tisztán DISM
                _, dism_cancelled = run_dism_add_driver(norm_source, "")
                if dism_cancelled:
                    self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                    return

            # Post-install
            if online:
                is_pe = os.environ.get('SystemDrive', 'C:') == 'X:'
                if not is_pe:
                    self.emit('task_progress', {'task': 'restore', 'log': 'Hardverváltozások keresése...'})
                    time.sleep(1.5)
                    self._run(['pnputil', '/scan-devices'])
                    time.sleep(3.5)
                    self.emit('task_progress', {'task': 'restore', 'log': '✅ Scan kész!'})
            else:
                # === BCD JAVÍTÁS (boot loader) ===
                self._repair_bcd(norm_target)
                
                # Automata PnP rescan beállítása az asztal betöltésére
                self.emit('task_progress', {'task': 'restore', 'log': '\nElső bejelentkezési rescan script beállítása...'})
                startup_dir = os.path.join(target, "ProgramData", "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
                os.makedirs(startup_dir, exist_ok=True)
                bat_path = os.path.join(startup_dir, "auto_pnputil_scan.bat")
                bat_content = (
                    '@echo off\n'
                    'set LOGFILE="%SystemDrive%\\Users\\Public\\driver_startup_log.txt"\n'
                    'echo [%DATE% %TIME%] Boot rescan indult... >> %LOGFILE%\n'
                    'pnputil /scan-devices >> %LOGFILE% 2>&1\n'
                    'echo [%DATE% %TIME%] Kesz! >> %LOGFILE%\n'
                    'ping 127.0.0.1 -n 3 > nul\n'
                    '(goto) 2>nul & del "%~f0"\n'
                )
                try:
                    with open(bat_path, 'w', encoding='utf-8') as f:
                        f.write(bat_content)
                    self.emit('task_progress', {'task': 'restore', 'log': '✅ Startup script elhelyezve.'})
                except Exception as e:
                    self.emit('task_progress', {'task': 'restore', 'log': f'⚠ Script írási hiba: {e}'})

            self.emit('task_progress', {'task': 'restore', 'log': '\n==== BEFEJEZVE ===='})
            self.emit('task_complete', {'task': 'restore', 'status': '✅ Visszaállítás befejezve!'})

        self._safe_thread('restore', worker)

    def extract_wim(self):
        logging.info("[API] extract_wim()")
        wim_path = self.select_file('Válaszd ki az install.wim fájlt', 'WIM fájlok (*.wim)|*.wim')
        if not wim_path:
            logging.info("[WIM] Mégse - nincs WIM kiválasztva")
            return
        logging.info(f"[WIM] WIM fájl: {wim_path}")
        if wim_path.lower().endswith(".esd"):
            logging.error("[WIM] ESD fájl nem támogatott!")
            self.emit('alert', {'title': 'Hiba', 'message': 'ESD fájl nem támogatott. Kérlek, használj install.wim fájlt!'})
            return
        dest = self.select_directory('Válassz ideiglenes mappát a kicsomagoláshoz')
        if not dest:
            logging.info("[WIM] Mégse - nincs célmappa kiválasztva")
            return
        logging.info(f"[WIM] Célmappa: {dest}")
        self._cancel_flag = False

        def worker():
            logging.info("[WIM] Worker indult - WIM kinyerés...")
            self.emit('task_start', {'task': 'wim', 'title': 'WIM Driver Kinyerés'})
            wim = os.path.abspath(wim_path).replace("/", "\\")
            # A WIM csatolási mappának a C: meghajtón kell lennie (NTFS), mert a cserélhető meghajtókat (USB) a DISM visszautasítja
            sys_temp = os.environ.get('TEMP', 'C:\\Temp')
            mount_dir = os.path.join(sys_temp, f"WIM_Mount_Temp_{int(time.time())}")
            target_folder = os.path.join(dest, f"Windows_Gyari_Alap_Driverek_{datetime.now().strftime('%Y%m%d_%H%M')}")
            logging.info(f"[WIM] Mount dir: {mount_dir}")
            logging.info(f"[WIM] Target folder: {target_folder}")

            if os.path.exists(mount_dir):
                logging.debug("[WIM] Régi mount dir törlése...")
                shutil.rmtree(mount_dir, ignore_errors=True)
            os.makedirs(mount_dir, exist_ok=True)
            os.makedirs(target_folder, exist_ok=True)

            try:
                # Cancel check before mount
                if self._check_cancel():
                    self.emit('task_complete', {'task': 'wim', 'status': '❗ Megszakítva!'})
                    return

                logging.info("[WIM] DISM Mount-Image futtatása...")
                self.emit('task_progress', {'task': 'wim', 'log': 'WIM csatolás (ez 4-5 perc)...', 'indeterminate': True,
                                            'counter': '1/3', 'status': 'Képfájl csatolása...'})
                res = self._run(["dism", "/Mount-Image", f"/ImageFile:{wim}", "/Index:1", f"/MountDir:{mount_dir}", "/ReadOnly"])
                if res.returncode != 0:
                    logging.error(f"[WIM] DISM Mount hiba: {res.stdout} {res.stderr}")
                    raise Exception(f"DISM Mount hiba: {res.stdout} {res.stderr}")
                
                # Cancel check after mount (will unmount in except)
                if self._check_cancel():
                    raise Exception("Megszakítva a felhasználó által")
                
                logging.info("[WIM] WIM csatolva, fájlok másolása...")

                self.emit('task_progress', {'task': 'wim', 'log': 'Fájlok másolása...', 'counter': '2/3', 'status': 'Gyári driverek másolása...'})
                
                driverstore = os.path.join(mount_dir, "Windows", "System32", "DriverStore", "FileRepository")
                target_repo = os.path.join(target_folder, "FileRepository")
                if os.path.exists(driverstore):
                    logging.info(f"[WIM] FileRepository másolása: {driverstore} -> {target_repo}")
                    shutil.copytree(driverstore, target_repo, dirs_exist_ok=True)
                else:
                    logging.error("[WIM] FileRepository nem található!")
                    raise Exception("FileRepository nem található a WIM-ben!")

                inf_dir = os.path.join(mount_dir, "Windows", "INF")
                target_inf = os.path.join(target_folder, "INF")
                if os.path.exists(inf_dir):
                    logging.info(f"[WIM] INF mappa másolása: {inf_dir} -> {target_inf}")
                    shutil.copytree(inf_dir, target_inf, dirs_exist_ok=True)

                logging.info("[WIM] WIM leválasztása...")
                self.emit('task_progress', {'task': 'wim', 'log': 'WIM leválasztása...', 'counter': '3/3', 'status': 'Takarítás...'})
                self._run(["dism", "/Unmount-Image", f"/MountDir:{mount_dir}", "/Discard"])
                shutil.rmtree(mount_dir, ignore_errors=True)

                logging.info(f"[WIM] Kész! Kimenet: {target_folder}")
                self.emit('task_complete', {'task': 'wim', 'status': f'✅ Gyári driverek kimentve: {target_folder}',
                                            'log': f'\n✅ Kész! Mappa: {target_folder}'})
            except Exception as e:
                logging.error(f"[WIM] Hiba: {e}")
                logging.error(traceback.format_exc())
                self._run(["dism", "/Unmount-Image", f"/MountDir:{mount_dir}", "/Discard"])
                shutil.rmtree(mount_dir, ignore_errors=True)
                self.emit('task_error', {'task': 'wim', 'error': str(e)})
                self.emit('task_complete', {'task': 'wim', 'status': f'❌ Hiba: {e}'})

        self._safe_thread('wim', worker)


# ================================================================
# CLI MÓD - Teljes funkcionalitás (GUI tükör)
# ================================================================
class CliApi:
    """CLI verzió API - ugyanazokat a funkciókat hívja mint a GUI, de konzolra ír."""
    
    def __init__(self):
        self.target_os_path = None
        self.sys_drive = os.environ.get('SystemDrive', 'C:') + '\\'
        self._si = subprocess.STARTUPINFO()
        self._si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        self._nw = subprocess.CREATE_NO_WINDOW
        self._cancel_flag = False
    
    def _run(self, cmd, **kwargs):
        """Parancs futtatás (CLI verzió)."""
        cmd_str = cmd if isinstance(cmd, str) else ' '.join(str(c) for c in cmd)
        logging.debug(f"[CMD_CLI] Futtatás: {cmd_str[:300]}")
        start = time.time()
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, errors='replace',
                                  startupinfo=self._si, creationflags=self._nw, **kwargs)
            elapsed = time.time() - start
            if result.returncode != 0:
                logging.warning(f"[CMD_CLI] Visszatérési kód: {result.returncode} ({elapsed:.1f}s)")
                if result.stderr:
                    logging.warning(f"[CMD_CLI] stderr: {result.stderr[:4000]}")
            else:
                logging.debug(f"[CMD_CLI] OK ({elapsed:.1f}s)")
            
            if result.stdout:
                out_txt = result.stdout.strip()
                if len(out_txt) > 4000: out_txt = out_txt[:4000] + '... [TRUNCATED]'
                logging.debug(f"[CMD_CLI] stdout: {out_txt}")
            return result
        except Exception as e:
            logging.error(f"[CMD_CLI] Kivétel: {e}")
            class DummyRes:
                returncode = 1
                stdout = ""
                stderr = str(e)
            return DummyRes()
    
    def _print_progress(self, msg, end='\n'):
        """Progress kiírás."""
        print(msg, end=end, flush=True)
    
    # ================================================================
    # DRIVER KEZELÉS
    # ================================================================
    def get_third_party_drivers(self):
        """Third-party driverek listája."""
        self._print_progress("📋 Third-party driverek lekérdezése...")
        res = self._run(['pnputil', '/enum-drivers'])
        drivers = []
        current = {}
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                if current and "published" in current:
                    drivers.append(current)
                    current = {}
                continue
            parts = line.split(":", 1)
            if len(parts) == 2:
                key, val = parts[0].strip(), parts[1].strip()
                if "Published Name" in key or "Közzétett név" in key:
                    current["published"] = val
                elif "Original Name" in key or "Eredeti név" in key:
                    current["original"] = val
                elif "Provider Name" in key or "Szolgáltató neve" in key:
                    current["provider"] = val
                elif "Class Name" in key or "Osztály neve" in key:
                    current["class"] = val
                elif "Driver Version" in key or "Illesztőprogram verziója" in key:
                    current["version"] = val
        if current and "published" in current:
            drivers.append(current)
        return drivers
    
    def get_all_drivers(self):
        """Összes driver listája (veszélyes mód)."""
        self._print_progress("📋 Összes driver lekérdezése (PowerShell)...")
        cmd = ['powershell', '-NoProfile', '-Command',
               '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Get-WindowsDriver -Online -All | Select-Object ProviderName, ClassName, Version, Driver, OriginalFileName | ConvertTo-Json -Depth 2 -WarningAction SilentlyContinue']
        res = self._run(cmd, encoding='utf-8')
        out = res.stdout.strip()
        if not out:
            return []
        try:
            data = json.loads(out)
            if isinstance(data, dict):
                data = [data]
            return [{"published": d.get("Driver", ""), "original": d.get("OriginalFileName", ""),
                     "provider": d.get("ProviderName", ""), "class": d.get("ClassName", ""),
                     "version": d.get("Version", "")} for d in data]
        except Exception:
            return []
    
    def get_offline_drivers(self, all_drivers=False):
        """Offline OS driverek listája."""
        self._print_progress(f"📋 Offline driverek lekérdezése: {self.target_os_path}...")
        cmd = ['dism', f'/Image:{self.target_os_path}', '/Get-Drivers']
        if all_drivers:
            cmd.append('/all')
        res = self._run(cmd)
        drivers = []
        current = {}
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                if current and "published" in current:
                    drivers.append(current)
                    current = {}
                continue
            parts = line.split(":", 1)
            if len(parts) == 2:
                key, val = parts[0].strip(), parts[1].strip()
                if "Published Name" in key or "Közzétett név" in key or "Published name" in key:
                    current["published"] = val
                elif "Original File Name" in key or "Eredeti fájlnév" in key or "Original name" in key:
                    current["original"] = val
                elif "Provider Name" in key or "Szolgáltató neve" in key or "Provider" in key:
                    current["provider"] = val
                elif "Class Name" in key or "Osztálynév" in key:
                    current["class"] = val
                elif "Date and Version" in key or "Dátum és verzió" in key:
                    current["version"] = val
        if current and "published" in current:
            drivers.append(current)
            
        valid_drivers = []
        rep = os.path.join(self.target_os_path, "Windows", "System32", "DriverStore", "FileRepository")
        for d in drivers:
            pub = d.get("published", "")
            if not pub:
                continue
            if pub.lower().startswith("oem"):
                valid_drivers.append(d)
                continue
            if glob.glob(os.path.join(rep, f"{pub}_*")):
                valid_drivers.append(d)
                
        return valid_drivers
    
    def list_drivers(self, all_drivers=False):
        """Driver lista megjelenítése."""
        if self.target_os_path:
            drivers = self.get_offline_drivers(all_drivers)
        elif all_drivers:
            drivers = self.get_all_drivers()
        else:
            drivers = self.get_third_party_drivers()
        
        if not drivers:
            print("❌ Nincs találat vagy hiba történt.")
            return []
        
        mode = "ÖSSZES" if all_drivers else "Third-party"
        loc = f" ({self.target_os_path})" if self.target_os_path else ""
        print(f"\n{'='*60}")
        print(f"  {mode} driverek{loc}: {len(drivers)} db")
        print(f"{'='*60}")
        print(f"{'#':>4}  {'Published':<18} {'Provider':<25} {'Class':<15}")
        print("-" * 70)
        for i, d in enumerate(drivers, 1):
            pub = d.get('published', '?')[:17]
            prov = d.get('provider', '?')[:24]
            cls = d.get('class', '?')[:14]
            print(f"{i:4}  {pub:<18} {prov:<25} {cls:<15}")
        print("-" * 70)
        return drivers
    
    def delete_drivers(self, drivers, reboot=False):
        """Driverek törlése."""
        total = len(drivers)
        print(f"\n🗑️  {total} driver törlése indul...")
        print("-" * 50)
        
        success = 0
        fail = 0
        is_offline = bool(self.target_os_path)
        
        for i, drv in enumerate(drivers, 1):
            pub = drv.get('published', '?')
            print(f"  [{i}/{total}] {pub}... ", end="", flush=True)
            
            is_oem = pub.lower().startswith("oem")
            
            if is_offline and is_oem:
                res = self._run(['dism', f'/Image:{self.target_os_path}', '/Remove-Driver', f'/Driver:{pub}'])
            elif not is_offline:
                res = self._run(['pnputil', '/delete-driver', pub, '/uninstall', '/force'])
            else:
                class DummyRes:
                    returncode = 1
                    stdout = ""
                res = DummyRes()
            
            if res.returncode == 0 or any(k in res.stdout.lower() for k in ['deleted', 'törölve', 'successfully']):
                print("✅")
                success += 1
            else:
                if not is_oem:
                    found_any = False
                    if is_offline:
                        rep = os.path.join(self.target_os_path, "Windows", "System32", "DriverStore", "FileRepository")
                        inf_dir = os.path.join(self.target_os_path, "Windows", "INF")
                    else:
                        rep = os.path.join(os.environ.get('SYSTEMROOT', r'C:\Windows'), "System32", "DriverStore", "FileRepository")
                        inf_dir = os.path.join(os.environ.get('SYSTEMROOT', r'C:\Windows'), "INF")
                    
                    dirs = glob.glob(os.path.join(rep, f"{pub}_*"))
                    if dirs:
                        for d in dirs:
                            self._run(f'takeown /f "{d}" /r /d y', shell=True)
                            self._run(f'icacls "{d}" /grant *S-1-5-32-544:F /t', shell=True)
                            shutil.rmtree(d, ignore_errors=True)
                            self._run(f'rmdir /s /q "{d}"', shell=True)
                        found_any = True
                        
                    bname = os.path.splitext(pub)[0]
                    for ext in ['.in', '.pn', '.INF', '.PNF']:
                        fpath = os.path.join(inf_dir, bname + ext)
                        if os.path.exists(fpath):
                            self._run(f'takeown /f "{fpath}" /A', shell=True)
                            self._run(f'icacls "{fpath}" /grant *S-1-5-32-544:F', shell=True)
                            try:
                                os.remove(fpath)
                                found_any = True
                            except OSError:
                                self._run(f'del /f /q "{fpath}"', shell=True)
                                found_any = True
                    
                    if found_any:
                        print("✅ (force)")
                        success += 1
                    else:
                        print("❌")
                        fail += 1
                else:
                    print("❌")
                    fail += 1
        
        print("-" * 50)
        print(f"✅ Sikeres: {success}  |  ❌ Sikertelen: {fail}")
        
        # Post-delete scan
        if not is_offline and success > 0:
            print("\n🔄 Hardverek újraszkennelése...")
            self._run(['pnputil', '/scan-devices'])
            time.sleep(2)
            print("✅ Kész!")
            
            if reboot:
                print("\n🔄 Újraindítás 5 másodperc múlva...")
                time.sleep(5)
                self._run(['shutdown', '/r', '/t', '0', '/f'])
        
        return success, fail
    
    # ================================================================
    # MENTÉS ÉS VISSZAÁLLÍTÁS
    # ================================================================
    def backup_third_party(self, dest_folder):
        """Third-party driverek mentése."""
        folder = os.path.join(dest_folder, f"DriverDoktor_Export_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        os.makedirs(folder, exist_ok=True)
        print("\n💾 Third-party driverek mentése...")
        print(f"   Cél: {folder}")
        print("-" * 50)
        
        if self.target_os_path:
            res = self._run(['dism', f'/Image:{self.target_os_path}', '/export-driver', f'/destination:{folder}'])
        else:
            res = self._run(['dism', '/online', '/export-driver', f'/destination:{folder}'])
        
        if res.returncode == 0:
            print("✅ Mentés sikeres!")
            return folder
        else:
            print(f"❌ Hiba: {res.stderr[:200] if res.stderr else 'Ismeretlen hiba'}")
            return None
    
    def backup_all(self, dest_folder):
        """Összes driver mentése (OEM + inbox)."""
        folder = os.path.join(dest_folder, f"DriverDoktor_FullExport_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        os.makedirs(folder, exist_ok=True)
        print("\n💾 ÖSSZES driver mentése...")
        print(f"   Cél: {folder}")
        print("-" * 50)
        
        success = 0
        # OEM driverek
        print("1/3 OEM driverek exportálása...")
        if self.target_os_path:
            res = self._run(['dism', f'/Image:{self.target_os_path}', '/export-driver', f'/destination:{folder}'])
            if res.returncode == 0:
                print("   ✅ OEM export sikeres (DISM offline)")
                success = 1
            else:
                print("   ❌ OEM export hiba")
        else:
            enum_res = self._run(['pnputil', '/enum-drivers'])
            all_infs = re.findall(r'(oem\d+\.inf)', enum_res.stdout, re.I)
            
            for i, inf in enumerate(all_infs, 1):
                print(f"  [{i}/{len(all_infs)}] {inf}... ", end="", flush=True)
                inf_folder = os.path.join(folder, inf.replace('.in', ''))
                os.makedirs(inf_folder, exist_ok=True)
                res = self._run(['pnputil', '/export-driver', inf, inf_folder])
                if res.returncode == 0:
                    print("✅")
                    success += 1
                else:
                    print("❌")
            
            print(f"   OEM: {success}/{len(all_infs)} exportálva")
        
        # FileRepository (inbox)
        print("2/3 Windows inbox driverek (FileRepository) másolása...")
        windows_dir = os.path.join(self.target_os_path, 'Windows') if self.target_os_path else os.environ.get('SYSTEMROOT', r'C:\Windows')
        driverstore = os.path.join(windows_dir, 'System32', 'DriverStore', 'FileRepository')
        inbox_folder = os.path.join(folder, '_Windows_Inbox_Drivers')
        os.makedirs(inbox_folder, exist_ok=True)
        self._run(['robocopy', driverstore, inbox_folder, '/E', '/R:0', '/W:0', '/NFL', '/NDL', '/NJH', '/NJS', '/NC', '/NS', '/NP'])
        print("   ✅ FileRepository másolva")
        
        # INF mappa
        print("3/3 Windows INF mappa másolása...")
        inf_src = os.path.join(windows_dir, 'INF')
        inbox_inf = os.path.join(folder, '_Windows_Inbox_INF')
        os.makedirs(inbox_inf, exist_ok=True)
        self._run(['robocopy', inf_src, inbox_inf, '/E', '/R:0', '/W:0', '/NFL', '/NDL', '/NJH', '/NJS', '/NC', '/NS', '/NP'])
        print("   ✅ INF mappa másolva")
        
        # Összegzés
        total_size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fns in os.walk(folder) for f in fns if os.path.exists(os.path.join(dp, f)))
        print("-" * 50)
        print(f"✅ Mentés kész! Méret: {total_size / (1024*1024):.0f} MB")
        return folder
    
    def restore_drivers(self, source_folder, online=True):
        """Driverek visszaállítása."""
        print(f"\n{'♻️'} Driverek visszaállítása...")
        print(f"   Forrás: {source_folder}")
        if not online:
            print(f"   Cél: {self.target_os_path}")
        print("-" * 50)
        
        if online and not self.target_os_path:
            # Online mód - pnputil
            print("🔄 pnputil /add-driver futtatása...")
            res = self._run(['pnputil', '/add-driver', f"{source_folder}\\*.in", '/subdirs', '/install'])
            if res.returncode == 0:
                print("✅ Visszaállítás sikeres!")
            else:
                print("⚠️  Részleges siker vagy hiba. Részletek:")
                print(res.stdout[:500] if res.stdout else res.stderr[:500])
            
            print("\n🔄 Hardverek újraszkennelése...")
            self._run(['pnputil', '/scan-devices'])
            time.sleep(3)
            print("✅ Kész!")
        else:
            # Offline mód - DISM
            target = self.target_os_path or input("Cél OS meghajtó (pl: D:\\): ").strip()
            if not target:
                print("❌ Nincs cél megadva!")
                return False
            
            print(f"🔄 DISM /Add-Driver futtatása ({target})...")
            scratch = os.path.join(target, "Scratch")
            os.makedirs(scratch, exist_ok=True)
            res = self._run(['dism', f'/Image:{target}', '/Add-Driver', f'/Driver:{source_folder}', '/Recurse', '/ForceUnsigned', f'/ScratchDir:{scratch}'])
            
            if res.returncode == 0:
                print("✅ Visszaállítás sikeres!")
            else:
                print("⚠️  Részleges siker vagy hiba. Néhány inbox driver nem telepíthető DISM-mel.")
                print(res.stdout[:300] if res.stdout else "")
            
            # === BCD JAVÍTÁS (boot loader) ===
            self._repair_bcd_cli(target)
        
        return True
    
    def _repair_bcd_cli(self, target_drive):
        """BCD újraépítése CLI módban - megkeresi a megfelelő lemezen az EFI-t."""
        print("\n" + "-" * 50)
        print("🔧 BOOT LOADER (BCD) JAVÍTÁS")
        print("-" * 50)
        
        target_drive = target_drive.rstrip('\\') + '\\'
        target_letter = target_drive[0].upper()
        windows_path = os.path.join(target_drive, 'Windows')
        
        if not os.path.exists(windows_path):
            print(f"⚠️  Windows mappa nem található: {windows_path}")
            return False
        
        print(f"Cél Windows meghajtó: {target_drive}")
        
        # 1. Megkeressük melyik DISK-en van a Windows partíció
        print("A Windows meghajtó lemezének azonosítása...")
        
        disk_number = None
        efi_letter = None
        efi_partition = None
        
        try:
            # Volume-ok listázása
            res = self._run(['diskpart'], input='list volume\n', timeout=30)
            
            if res.returncode == 0 and res.stdout:
                lines = res.stdout.splitlines()
                target_volume = None
                
                # Windows volume keresése
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 3:
                        for i, p in enumerate(parts):
                            if p.upper() == target_letter and i >= 1:
                                try:
                                    target_volume = int(parts[1])
                                except (ValueError, IndexError):
                                    pass
                                break
                
                if target_volume is not None:
                    print(f"Windows volume: {target_volume}")
                    
                    # Disk azonosítása
                    res2 = self._run(['diskpart'], input=f'select volume {target_volume}\ndetail volume\n', timeout=30)
                    
                    if res2.returncode == 0 and res2.stdout:
                        for line in res2.stdout.splitlines():
                            if 'Disk' in line and '#' not in line:
                                parts = line.split()
                                for p in parts:
                                    if p.isdigit():
                                        disk_number = int(p)
                                        break
                                if disk_number is not None:
                                    break
                    
                    if disk_number is not None:
                        print(f"Lemez: Disk {disk_number}")
                        
                        # EFI partíció keresése ezen a lemezen
                        res3 = self._run(['diskpart'], input=f'select disk {disk_number}\nlist partition\n', timeout=30)
                        
                        if res3.returncode == 0 and res3.stdout:
                            for line in res3.stdout.splitlines():
                                line_upper = line.upper()
                                if 'SYSTEM' in line_upper or 'EFI' in line_upper:
                                    parts = line.split()
                                    for i, p in enumerate(parts):
                                        if p.isdigit() and i >= 1:
                                            efi_partition = int(p)
                                            break
                                    if efi_partition:
                                        break
                        
                        if efi_partition:
                            print(f"EFI partíció: Partition {efi_partition}")
                            
                            # Szabad betűjel keresése
                            used_letters = set()
                            for line in lines:
                                parts = line.split()
                                for p in parts:
                                    if len(p) == 1 and p.isalpha():
                                        used_letters.add(p.upper())
                            
                            free_letter = None
                            for c in 'STUVWXYZ':
                                if c not in used_letters:
                                    free_letter = c
                                    break
                            
                            if free_letter:
                                res4 = self._run(['diskpart'], 
                                    input=f'select disk {disk_number}\nselect partition {efi_partition}\nassign letter={free_letter}\n',
                                    timeout=30)
                                if res4.returncode == 0:
                                    efi_letter = free_letter + ':'
                                    print(f"EFI betűjel: {efi_letter}")
        except Exception as e:
            print(f"⚠️  Lemez azonosítási hiba: {e}")
        
        # 2. bcdboot futtatása
        success = False
        
        if efi_letter:
            print(f"bcdboot {target_drive}Windows /s {efi_letter} /f UEFI")
            res = self._run(['bcdboot', f'{target_drive}Windows', '/s', efi_letter, '/f', 'UEFI'])
            if res.returncode == 0:
                success = True
                print("✅ BCD sikeresen újraépítve (UEFI)!")
            else:
                print("⚠️  UEFI bcdboot hiba, fallback...")
            
            # EFI betűjel eltávolítása
            try:
                self._run(['diskpart'], 
                    input=f'select disk {disk_number}\nselect partition {efi_partition}\nremove letter={efi_letter[0]}\n',
                    timeout=30)
            except Exception as e:
                logging.debug(e)
        
        if not success:
            # Fallback: /s nélkül
            print(f"bcdboot {target_drive}Windows /f ALL")
            res = self._run(['bcdboot', f'{target_drive}Windows', '/f', 'ALL'])
            if res.returncode == 0:
                success = True
                print("✅ BCD sikeresen újraépítve (ALL)!")
            else:
                print(f"⚠️  bcdboot hiba (0x{res.returncode:X}), bootrec parancsok...")
        
        if not success:
            print("bootrec parancsok...")
            for cmd in ['/fixmbr', '/fixboot', '/rebuildbcd']:
                print(f"  bootrec {cmd}... ", end="", flush=True)
                res = self._run(['bootrec', cmd])
                print("✅" if res.returncode == 0 else "⚠️")
        
        print("-" * 50)
        print("✅ BCD javítás befejezve!")
        return True

    def extract_wim(self, wim_path, dest_folder):
        """WIM-ből gyári driverek kinyerése."""
        print("\n📀 WIM driver kinyerés...")
        print(f"   WIM: {wim_path}")
        print(f"   Cél: {dest_folder}")
        print("-" * 50)
        
        sys_temp = os.environ.get('TEMP', 'C:\\Temp')
        mount_dir = os.path.join(sys_temp, f"WIM_Mount_Temp_{int(time.time())}")
        target_folder = os.path.join(dest_folder, f"Windows_Gyari_Alap_Driverek_{datetime.now().strftime('%Y%m%d_%H%M')}")
        
        if os.path.exists(mount_dir):
            shutil.rmtree(mount_dir, ignore_errors=True)
        os.makedirs(mount_dir, exist_ok=True)
        os.makedirs(target_folder, exist_ok=True)
        
        try:
            print("1/3 WIM csatolása (ez 3-5 perc)...")
            res = self._run(["dism", "/Mount-Image", f"/ImageFile:{wim_path}", "/Index:1", f"/MountDir:{mount_dir}", "/ReadOnly"])
            if res.returncode != 0:
                raise Exception(f"Mount hiba: {res.stderr}")
            
            print("2/3 FileRepository + INF másolása...")
            driverstore = os.path.join(mount_dir, "Windows", "System32", "DriverStore", "FileRepository")
            target_repo = os.path.join(target_folder, "FileRepository")
            if os.path.exists(driverstore):
                shutil.copytree(driverstore, target_repo, dirs_exist_ok=True)
            
            inf_dir = os.path.join(mount_dir, "Windows", "INF")
            target_inf = os.path.join(target_folder, "INF")
            if os.path.exists(inf_dir):
                shutil.copytree(inf_dir, target_inf, dirs_exist_ok=True)
            
            print("3/3 WIM leválasztása...")
            self._run(["dism", "/Unmount-Image", f"/MountDir:{mount_dir}", "/Discard"])
            shutil.rmtree(mount_dir, ignore_errors=True)
            
            print("-" * 50)
            print(f"✅ Gyári driverek kimentve: {target_folder}")
            return target_folder
            
        except Exception as e:
            print(f"❌ Hiba: {e}")
            self._run(["dism", "/Unmount-Image", f"/MountDir:{mount_dir}", "/Discard"])
            shutil.rmtree(mount_dir, ignore_errors=True)
            return None
    
    def create_restore_point(self):
        """Visszaállítási pont létrehozása."""
        if self.target_os_path:
            print("\n❌ Hiba: Visszaállítási pont csak Élő rendszeren készíthető!")
            return False
            
        desc = f"DriverDoktor_Backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        print("\n🛡️  Visszaállítási pont létrehozása...")
        print(f"   Név: {desc}")
        print("-" * 50)
        
        # Enable System Restore
        print("1/2 Rendszervédelem engedélyezése...")
        self._run(["powershell", "-NoProfile", "-Command", 'Enable-ComputerRestore -Drive "$($env:SystemDrive)\\" -ErrorAction SilentlyContinue'])
        self._run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore',
                   '/v', 'SystemRestorePointCreationFrequency', '/t', 'REG_DWORD', '/d', '0', '/f'])
        
        # Create restore point
        print("2/2 Visszaállítási pont létrehozása...")
        ps_cmd = f'Checkpoint-Computer -Description "{desc}" -RestorePointType "MODIFY_SETTINGS" -ErrorAction Stop'
        res = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd], encoding='utf-8')
        
        if res.returncode == 0:
            print("✅ Visszaállítási pont létrehozva!")
            return True
        else:
            print(f"❌ Hiba: {res.stderr[:200] if res.stderr else 'Ismeretlen hiba'}")
            return False
    
    def repair_bcd_standalone_cli(self):
        """Önálló BCD javítás CLI módban."""
        print("\n🔧 BCD BOOT HIBA JAVÍTÁSA")
        print("-" * 50)
        
        target = self.target_os_path
        if not target:
            target = input("Add meg a HALOTT Windows meghajtóját (pl: D:\\): ").strip()
            
        if not target:
            print("❌ Nincs meghajtó megadva!")
            return False
        
        target = target.rstrip('\\') + '\\'
        windows_path = os.path.join(target, 'Windows')
        
        if not os.path.exists(windows_path):
            print(f"❌ Windows mappa nem található: {windows_path}")
            return False
        
        return self._repair_bcd_cli(target)
    
    # ================================================================
    # WINDOWS UPDATE
    # ================================================================
    def check_wu_status(self):
        """WU driver frissítés állapota."""
        policy_disabled = False
        search_disabled = False
        
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate", 0, winreg.KEY_READ) as key:
                val, _ = winreg.QueryValueEx(key, "ExcludeWUDriversInQualityUpdate")
                if val == 1:
                    policy_disabled = True
        except (FileNotFoundError, OSError):
            pass
        
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching", 0, winreg.KEY_READ) as key:
                val, _ = winreg.QueryValueEx(key, "SearchOrderConfig")
                if val == 0:
                    search_disabled = True
        except (FileNotFoundError, OSError):
            pass
        
        if policy_disabled and search_disabled:
            return "⛔ LETILTVA (policy + eszközbeállítások)"
        elif policy_disabled:
            return "⛔ LETILTVA (policy)"
        elif search_disabled:
            return "⛔ LETILTVA (eszközbeállítások)"
        else:
            return "✅ ENGEDÉLYEZVE"
    
    def disable_wu_drivers(self):
        """WU driver frissítések letiltása."""
        if self.target_os_path:
            print("\n❌ Hiba: A Windows Update beállítások csak Élő rendszeren módosíthatók!")
            return
            
        print("\n⛔ WU driver frissítések letiltása...")
        print("-" * 50)
        
        try:
            with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate", 0, winreg.KEY_WRITE) as key:
                winreg.SetValueEx(key, "ExcludeWUDriversInQualityUpdate", 0, winreg.REG_DWORD, 1)
            print("  ✅ ExcludeWUDriversInQualityUpdate = 1")
        except Exception as e:
            print(f"  ⚠️  {e}")
        
        self._run(['reg', 'add', r'HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate',
                   '/v', 'ExcludeWUDriversInQualityUpdate', '/t', 'REG_DWORD', '/d', '1', '/f'])
        
        try:
            with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching", 0, winreg.KEY_WRITE) as key:
                winreg.SetValueEx(key, "SearchOrderConfig", 0, winreg.REG_DWORD, 0)
            print("  ✅ SearchOrderConfig = 0")
        except Exception as e:
            print(f"  ⚠️  {e}")
        
        self._run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching',
                   '/v', 'SearchOrderConfig', '/t', 'REG_DWORD', '/d', '0', '/f'])
        
        print("  🔄 WU szolgáltatás újraindítása...")
        self._run('net stop wuauserv & net start wuauserv', shell=True)
        
        print("-" * 50)
        print("✅ WU driver letiltás kész!")
    
    def enable_wu_drivers(self):
        """WU driver frissítések engedélyezése + teljes reset."""
        if self.target_os_path:
            print("\n❌ Hiba: A Windows Update beállítások csak Élő rendszeren módosíthatók!")
            return
            
        print("\n✅ WU driver frissítések engedélyezése + reset...")
        print("-" * 50)
        
        # Policy törlés
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate", 0, winreg.KEY_WRITE) as key:
                winreg.DeleteValue(key, "ExcludeWUDriversInQualityUpdate")
            print("  ✅ Policy törölve")
        except FileNotFoundError:
            print("  ℹ️  Policy nem létezett")
        except Exception as e:
            print(f"  ⚠️  {e}")
        
        # SearchOrderConfig = 1
        try:
            with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching", 0, winreg.KEY_WRITE) as key:
                winreg.SetValueEx(key, "SearchOrderConfig", 0, winreg.REG_DWORD, 1)
            print("  ✅ SearchOrderConfig = 1")
        except Exception as e:
            print(f"  ⚠️  {e}")
        
        # Szolgáltatások
        print("  🔄 WU szolgáltatások újraindítása...")
        for svc in ['wuauserv', 'bits', 'cryptsvc']:
            self._run(f'net stop {svc} /y', shell=True)
        time.sleep(2)
        
        # SoftwareDistribution törlés
        sw_dist = os.path.join(os.environ.get('SYSTEMROOT', r'C:\Windows'), 'SoftwareDistribution')
        if os.path.exists(sw_dist):
            print("  🗑️  SoftwareDistribution törlése...")
            shutil.rmtree(sw_dist, ignore_errors=True)
        
        for svc in ['cryptsvc', 'bits', 'wuauserv']:
            self._run(f'net start {svc}', shell=True)
        
        self._run('wuauclt.exe /resetauthorization /detectnow', shell=True)
        self._run('UsoClient.exe StartScan', shell=True)
        
        print("-" * 50)
        print("✅ WU engedélyezés + reset kész!")
    
    def restart_wu_services(self):
        """WU szolgáltatások újraindítása."""
        if self.target_os_path:
            print("\n❌ Hiba: A Windows Update beállítások csak Élő rendszeren módosíthatók!")
            return
            
        print("\n🔄 WU szolgáltatások újraindítása...")
        print("-" * 50)
        
        for svc in ['wuauserv', 'bits', 'cryptsvc', 'msiserver']:
            print(f"  stop {svc}...", end=" ", flush=True)
            self._run(f'net stop {svc} /y', shell=True)
            print("✅")
        
        time.sleep(2)
        
        for svc in ['rpcss', 'cryptsvc', 'bits', 'msiserver', 'wuauserv']:
            print(f"  start {svc}...", end=" ", flush=True)
            self._run(f'net start {svc}', shell=True)
            print("✅")
        
        self._run('wuauclt.exe /resetauthorization /detectnow', shell=True)
        self._run('UsoClient.exe StartScan', shell=True)
        
        print("-" * 50)
        print("✅ WU szolgáltatások újraindítva!")
    
    # ================================================================
    # AUTOFIX (1 kattintásos driver fix)
    # ================================================================
    def autofix(self):
        """Teljes automatikus driver fix (mint a GUI-ban)."""
        if self.target_os_path:
            print("\n❌ Hiba: Az 1 Kattintásos Fix (Autofix) csak Élő (Online) rendszeren futtatható!")
            return
            
        print("\n" + "=" * 60)
        print("  ⚡ 1 KATTINTÁSOS AUTOMATIKUS DRIVER FIX")
        print("=" * 60)
        print("""
Lépések:
  1️⃣  Windows Update driver keresés LETILTÁSA
  2️⃣  Összes third-party driver TÖRLÉSE
  3️⃣  Hardver újraszkennelés
  4️⃣  WU driver telepítés (friss driverek)
  5️⃣  Újraindítás
""")
        
        confirm = input("Biztosan elindítod? (igen/nem): ").strip().lower()
        if confirm not in ['igen', 'i', 'yes', 'y']:
            print("❌ Megszakítva.")
            return
        
        start_time = time.time()
        
        # FÁZIS 1: WU letiltás
        print("\n" + "=" * 50)
        print("  FÁZIS 1: WU driver letiltás")
        print("=" * 50)
        self.disable_wu_drivers()
        
        # FÁZIS 2: Third-party driverek törlése
        print("\n" + "=" * 50)
        print("  FÁZIS 2: Third-party driverek törlése")
        print("=" * 50)
        drivers = self.get_third_party_drivers()
        if drivers:
            print(f"Talált: {len(drivers)} db third-party driver")
            self.delete_drivers(drivers, reboot=False)
        else:
            print("Nincs third-party driver.")
        
        # FÁZIS 3: Hardver scan
        print("\n" + "=" * 50)
        print("  FÁZIS 3: Hardver újraszkennelés")
        print("=" * 50)
        print("🔄 pnputil /scan-devices...")
        self._run(['pnputil', '/scan-devices'])
        time.sleep(5)
        print("✅ Kész!")
        
        # FÁZIS 4: WU driver telepítés
        print("\n" + "=" * 50)
        print("  FÁZIS 4: WU driver telepítés")
        print("=" * 50)
        print("🔄 Driver frissítések keresése és telepítése...")
        print("   (Ez akár 5-10 percig is tarthat)")
        
        ps_script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
try {
    $Session = New-Object -ComObject Microsoft.Update.Session
    $Searcher = $Session.CreateUpdateSearcher()
    try { $SM = New-Object -ComObject Microsoft.Update.ServiceManager; $SM.AddService2("7971f918-a847-4430-9279-4a52d1efe18d", 7, "") | Out-Null } catch {}
    $Searcher.ServerSelection = 3; $Searcher.ServiceID = "7971f918-a847-4430-9279-4a52d1efe18d"
    $Result = $Searcher.Search("IsInstalled=0 and Type='Driver'")
    if ($Result.Updates.Count -eq 0) { Write-Output "EMPTY"; exit }
    $ToInstall = New-Object -ComObject Microsoft.Update.UpdateColl
    foreach ($U in $Result.Updates) {
        if (-not $U.EulaAccepted) { $U.AcceptEula() }
        $ToInstall.Add($U) | Out-Null
        Write-Output "FOUND: $($U.Title)"
    }
    Write-Output "TOTAL: $($ToInstall.Count)"
    $s = 0; $f = 0
    for ($i = 0; $i -lt $ToInstall.Count; $i++) {
        $U = $ToInstall.Item($i)
        $SC = New-Object -ComObject Microsoft.Update.UpdateColl; $SC.Add($U) | Out-Null
        Write-Output "DL: $($U.Title)"
        $DL = $Session.CreateUpdateDownloader(); $DL.Updates = $SC
        try { $DR = $DL.Download() } catch { Write-Output "FAIL: $($U.Title)"; $f++; continue }
        if ($DR.ResultCode -ne 2 -and $DR.ResultCode -ne 3) { Write-Output "FAIL: $($U.Title)"; $f++; continue }
        Write-Output "INST: $($U.Title)"
        $Inst = $Session.CreateUpdateInstaller(); $Inst.Updates = $SC
        try { $IR = $Inst.Install() } catch { Write-Output "FAIL: $($U.Title)"; $f++; continue }
        $rc = $IR.GetUpdateResult(0).ResultCode
        if ($rc -eq 2 -or $rc -eq 3) { Write-Output "OK: $($U.Title)"; $s++ } else { Write-Output "FAIL: $($U.Title)"; $f++ }
    }
    Write-Output "DONE: s=$s f=$f"
} catch { Write-Output "ERROR: $($_.Exception.Message)" }
"""
        process = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
            startupinfo=self._si, creationflags=self._nw)
        
        install_success = 0
        install_fail = 0
        
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            if line.startswith("FOUND:"):
                print(f"  📦 {line[6:].strip()}")
            elif line.startswith("TOTAL:"):
                print(f"\n  Összesen {line[6:].strip()} driver telepítése...")
            elif line.startswith("DL:"):
                print(f"  ⬇ {line[3:].strip()}")
            elif line.startswith("INST:"):
                print(f"  ⚙ {line[5:].strip()}")
            elif line.startswith("OK:"):
                install_success += 1
                print(f"  ✅ {line[3:].strip()}")
            elif line.startswith("FAIL:"):
                install_fail += 1
                print(f"  ❌ {line[5:].strip()}")
            elif line == "EMPTY":
                print("  ℹ️  Nincs elérhető driver frissítés.")
            elif line.startswith("ERROR:"):
                print(f"  ❌ HIBA: {line[6:].strip()}")
            elif line.startswith("DONE:"):
                print(f"\n  Telepítés kész: ✅ {install_success} sikeres, ❌ {install_fail} sikertelen")
        
        process.wait()
        
        if install_success > 0:
            print("\n🔄 Eszközök újraszkennelése...")
            self._run(['pnputil', '/scan-devices'])
        
        # Összegzés
        elapsed = int(time.time() - start_time)
        print("\n" + "=" * 60)
        print(f"  ⚡ AUTOFIX KÉSZ! (Idő: {elapsed // 60} perc {elapsed % 60} mp)")
        print("=" * 60)
        
        # FÁZIS 5: Újraindítás
        if install_success > 0 or len(drivers) > 0:
            print("\n🔄 Újraindítás 30 másodperc múlva...")
            print("   (Ctrl+C a megszakításhoz)")
            try:
                for i in range(30, 0, -1):
                    print(f"\r   {i} másodperc...", end="", flush=True)
                    time.sleep(1)
                print("\n🔄 Újraindítás MOST!")
                self._run(['shutdown', '/r', '/t', '0', '/f'])
            except KeyboardInterrupt:
                print("\n❌ Újraindítás megszakítva.")
        else:
            print("\nNem történt változás - újraindítás nem szükséges.")


def run_cli_mode():
    """Parancssoros mód - TELJES funkcionalitás (GUI tükör)."""
    api = CliApi()
    
    def clear_screen():
        os.system('cls' if os.name == 'nt' else 'clear')
    
    def print_header():
        clear_screen()
        print("=" * 60)
        print("  ♻️  DRIVERDOKTOR - CLI MÓD")
        print("  🖥️  Tiszta rendszer (Build " + str(BUILD_NUMBER) + ")")
        print("=" * 60)
        if api.target_os_path:
            print(f"  📌 Offline mód: {api.target_os_path}")
        else:
            print("  📌 Jelenlegi rendszer (online)")
        print("=" * 60)
    
    def main_menu():
        print("""
  FŐMENÜ - Válassz kategóriát:

    💿  1. Driverek kezelése
    💾  2. Mentés és Visszaállítás
    🔄  3. Windows Update
    ⚡  4. 1 Kattintásos Driver Fix
    
    ⚙️   5. Cél OS váltása (offline mód)
    ❌  0. Kilépés
""")
    
    def drivers_menu():
        while True:
            print_header()
            print("""
  💿 DRIVEREK KEZELÉSE

    1. Third-party driverek listázása
    2. ÖSSZES driver listázása (veszélyes!)
    3. Driver(ek) törlése
    4. Hardver újraszkennelés
    
    0. Vissza a főmenübe
""")
            choice = input("Választás: ").strip()
            
            if choice == '0':
                break
            elif choice == '1':
                drivers = api.list_drivers(all_drivers=False)
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '2':
                drivers = api.list_drivers(all_drivers=True)
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '3':
                all_mode = input("Összes driver mód? (i/n): ").strip().lower() == 'i'
                drivers = api.list_drivers(all_drivers=all_mode)
                if not drivers:
                    input("\nNyomj ENTER-t a folytatáshoz...")
                    continue
                
                sel = input("\nTörlendő sorszámok (pl: 1,3,5 vagy 'mind'): ").strip()
                if sel.lower() == 'mind':
                    to_delete = drivers
                else:
                    indices = [int(x.strip())-1 for x in sel.split(',') if x.strip().isdigit()]
                    to_delete = [drivers[i] for i in indices if 0 <= i < len(drivers)]
                
                if to_delete:
                    reboot = input("Törlés után újraindítás? (i/n): ").strip().lower() == 'i'
                    confirm = input(f"Biztosan törölsz {len(to_delete)} drivert? (i/n): ").strip().lower()
                    if confirm == 'i':
                        api.delete_drivers(to_delete, reboot=reboot)
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '4':
                if api.target_os_path:
                    print("❌ Offline módban nem elérhető!")
                else:
                    print("🔄 Hardver újraszkennelés...")
                    api._run(['pnputil', '/scan-devices'])
                    time.sleep(2)
                    print("✅ Kész!")
                input("\nNyomj ENTER-t a folytatáshoz...")
    
    def backup_menu():
        while True:
            print_header()
            print("""
  💾 MENTÉS ÉS VISSZAÁLLÍTÁS

    1. Third-party driverek mentése
    2. ÖSSZES driver mentése (OEM + inbox)
    3. Lementett driverek visszaállítása
    4. WIM-ből gyári driverek kinyerése
    5. Visszaállítási pont létrehozása
    6. BCD boot hiba javítása
    
    0. Vissza a főmenübe
""")
            choice = input("Választás: ").strip()
            
            if choice == '0':
                break
            elif choice == '1':
                dest = input("Mentés célmappája: ").strip()
                if dest:
                    api.backup_third_party(dest)
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '2':
                dest = input("Mentés célmappája: ").strip()
                if dest:
                    api.backup_all(dest)
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '3':
                source = input("Lementett driver mappa: ").strip()
                if source:
                    online = input("Online mód (jelenlegi rendszer)? (i/n): ").strip().lower() == 'i'
                    api.restore_drivers(source, online=online)
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '4':
                wim = input("install.wim fájl elérési útja: ").strip()
                dest = input("Kinyerés célmappája: ").strip()
                if wim and dest:
                    api.extract_wim(wim, dest)
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '5':
                if api.target_os_path:
                    print("❌ Offline módban nem elérhető!")
                else:
                    api.create_restore_point()
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '6':
                api.repair_bcd_standalone_cli()
                input("\nNyomj ENTER-t a folytatáshoz...")
    
    def wu_menu():
        while True:
            print_header()
            status = api.check_wu_status()
            print(f"""
  🔄 WINDOWS UPDATE BEÁLLÍTÁSOK
  
  Jelenlegi állapot: {status}

    1. WU driver letiltás
    2. WU driver engedélyezés + reset
    3. WU szolgáltatások újraindítása
    
    0. Vissza a főmenübe
""")
            choice = input("Választás: ").strip()
            
            if choice == '0':
                break
            elif choice == '1':
                api.disable_wu_drivers()
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '2':
                api.enable_wu_drivers()
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '3':
                api.restart_wu_services()
                input("\nNyomj ENTER-t a folytatáshoz...")
    
    def target_menu():
        print("\n⚙️  CÉL OS VÁLTÁSA")
        print("-" * 40)
        print("Jelenlegi:", api.target_os_path or "Jelenlegi rendszer (online)")
        print()
        path = input("Új cél OS path (üres = visszaállítás jelenlegire): ").strip()
        
        if not path:
            api.target_os_path = None
            print("✅ Visszaállítva: jelenlegi rendszer")
        elif os.path.isdir(os.path.join(path, 'Windows')):
            api.target_os_path = path
            print(f"✅ Cél OS: {api.target_os_path}")
        else:
            print(f"❌ Nem található Windows mappa: {path}")
        
        input("\nNyomj ENTER-t a folytatáshoz...")
    
    # FŐCIKLUS
    while True:
        print_header()
        main_menu()
        
        try:
            choice = input("Választás: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        
        if choice == '0':
            print("\nViszlát! 👋")
            break
        elif choice == '1':
            drivers_menu()
        elif choice == '2':
            backup_menu()
        elif choice == '3':
            wu_menu()
        elif choice == '4':
            print_header()
            api.autofix()
            input("\nNyomj ENTER-t a folytatáshoz...")
        elif choice == '5':
            target_menu()
        else:
            print("❌ Érvénytelen választás!")


# ================================================================
# MAIN
# ================================================================
if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    
    if "--cli" in sys.argv:
        if getattr(sys, "frozen", False):
            # Attach to the parent console if running from cmd in windowed mode
            if ctypes.windll.kernel32.AttachConsole(-1):
                sys.stdout = open("CONOUT$", "w", encoding="utf-8")
                sys.stderr = open("CONOUT$", "w", encoding="utf-8")
                sys.stdin = open("CONIN$", "r", encoding="utf-8")

    # Ha --progress argumentummal indítottuk, csak a progress ablakot nyitjuk meg
    if len(sys.argv) >= 3 and sys.argv[1] == '--progress':
        log_path = sys.argv[2]
        run_progress_window(log_path)
        sys.exit(0)
    
    # CLI mód
    if '--cli' in sys.argv:
        if not is_admin():
            print("❌ Rendszergazdai jogosultság szükséges!")
            print("   Futtasd rendszergazdaként!")
            input("Nyomj ENTER-t a kilépéshez...")
            sys.exit(1)
        run_cli_mode()
        sys.exit(0)
    
    if not is_admin():
        params = ' '.join([f'"{arg}"' for arg in sys.argv[1:]])
        if getattr(sys, 'frozen', False):
            ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        else:
            script = sys.argv[0]
            ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, f'"{script}" {params}', None, 1)
        sys.exit()

    # Logging
    log_filename = os.path.join(os.path.dirname(sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)), "DriverDoktor_debug.log")
    try:
        logging.basicConfig(filename=log_filename, level=logging.DEBUG,
                            format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S', encoding='utf-8')
    except Exception:
        logging.basicConfig(level=logging.DEBUG)

    def global_exception_handler(exc_type, exc_value, exc_traceback):
        err_str = str(exc_value)
        logging.exception("FATÁLIS HIBA:", exc_info=(exc_type, exc_value, exc_traceback))
        # WebView2 hibák detektálása
        if 'WebView2' in err_str or 'ICoreWebView2' in err_str or '.NET' in err_str:
            logging.error("[MAIN] WebView2 hiba detektálva exception handler-ben!")
            _webview_error.set()
    sys.excepthook = global_exception_handler

    def thread_exception_handler(args):
        err_str = str(args.exc_value)
        logging.exception("HÁTTÉRSZÁL HIBA:", exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        if 'WebView2' in err_str or 'ICoreWebView2' in err_str or '.NET' in err_str:
            logging.error("[MAIN] WebView2 hiba detektálva szál exception handler-ben!")
            _webview_error.set()
    threading.excepthook = thread_exception_handler

    logging.info("=" * 50)
    logging.info("DriverDoktor ELINDITVA")
    logging.info(f"Futtatasi konyvtar: {os.getcwd()}")
    logging.info("=" * 50)

    # WebView2 Runtime verzió ellenőrzés - ha túl régi, egyből CLI mód
    wv2_ok, wv2_info = check_webview2_runtime()
    if wv2_ok:
        logging.info(f"[INIT] WebView2 Runtime OK: v{wv2_info}")
    else:
        logging.warning(f"[INIT] WebView2 nem megfelelő: {wv2_info}")
        logging.info("[INIT] WebView2 telepítés felajánlása...")
        
        # MessageBox: telepítsük?
        MB_YESNO = 0x4
        MB_ICONQUESTION = 0x20
        MB_TOPMOST = 0x40000
        IDYES = 6
        
        result = ctypes.windll.user32.MessageBoxW(
            None,
            "A WebView2 Runtime hiányzik vagy túl régi!\n\n"
            "A DriverDoktor GUI-hoz WebView2 v109+ szükséges.\n\n"
            "Telepítsem automatikusan?\n"
            "(~2MB letöltés, pár másodperc)",
            "DriverDoktor - WebView2 telepítés",
            MB_YESNO | MB_ICONQUESTION | MB_TOPMOST
        )
        
        if result == IDYES:
            logging.info("[INIT] Felhasználó elfogadta a WebView2 telepítést")
            
            # Progress MessageBox (nem blokkoló)
            import urllib.request
            import tempfile
            
            try:
                # Letöltés
                logging.info("[INIT] WebView2 Bootstrapper letöltése...")
                bootstrapper_url = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
                temp_dir = tempfile.gettempdir()
                bootstrapper_path = os.path.join(temp_dir, "MicrosoftEdgeWebview2Setup.exe")
                
                # Progress ablak
                ctypes.windll.user32.MessageBoxW(
                    None,
                    "WebView2 telepítése folyamatban...\n\n"
                    "Ez pár másodpercet vesz igénybe.\n"
                    "Kattints OK-ra és várd meg!",
                    "DriverDoktor",
                    0x40 | MB_TOPMOST  # MB_ICONINFORMATION
                )
                
                urllib.request.urlretrieve(bootstrapper_url, bootstrapper_path)
                logging.info(f"[INIT] Bootstrapper letöltve: {bootstrapper_path}")
                
                # Telepítés silent módban
                logging.info("[INIT] WebView2 telepítés indítása (silent)...")
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                result = subprocess.run(
                    [bootstrapper_path, '/silent', '/install'],
                    capture_output=True,
                    startupinfo=si,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    timeout=120
                )
                
                logging.info(f"[INIT] WebView2 telepítés kész, returncode={result.returncode}")
                
                # Törlés
                try:
                    os.remove(bootstrapper_path)
                except Exception as e:
                    logging.debug(e)
                
                # Újraellenőrzés
                wv2_ok2, wv2_info2 = check_webview2_runtime()
                if wv2_ok2:
                    logging.info(f"[INIT] WebView2 telepítés SIKERES! v{wv2_info2}")
                    ctypes.windll.user32.MessageBoxW(
                        None,
                        f"WebView2 sikeresen telepítve!\n\nVerzió: {wv2_info2}\n\n"
                        "A program most újraindul a GUI-val.",
                        "DriverDoktor - Siker",
                        0x40 | MB_TOPMOST
                    )
                    # Program újraindítása
                    os.execv(sys.executable, [sys.executable] + sys.argv)
                else:
                    logging.error(f"[INIT] WebView2 telepítés után még mindig nem OK: {wv2_info2}")
                    ctypes.windll.user32.MessageBoxW(
                        None,
                        "WebView2 telepítés sikertelen vagy újraindítás szükséges.\n\n"
                        "Próbáld meg manuálisan:\n"
                        "https://go.microsoft.com/fwlink/p/?LinkId=2124703\n\n"
                        "Vagy használd a CLI módot.",
                        "DriverDoktor - Hiba",
                        0x10 | MB_TOPMOST  # MB_ICONERROR
                    )
                    
            except Exception as e:
                logging.error(f"[INIT] WebView2 telepítési hiba: {e}")
                ctypes.windll.user32.MessageBoxW(
                    None,
                    f"Hiba a WebView2 telepítésekor:\n{e}\n\n"
                    "Próbáld meg manuálisan:\n"
                    "https://go.microsoft.com/fwlink/p/?LinkId=2124703\n\n"
                    "Vagy használd a CLI módot.",
                    "DriverDoktor - Hiba",
                    0x10 | MB_TOPMOST
                )
        else:
            logging.info("[INIT] Felhasználó elutasította a WebView2 telepítést")
        
        # CLI mód indítása
        logging.info("[INIT] CLI mód indítása...")
        
        # Konzol ablak létrehozása (windowed exe-nél nincs)
        try:
            ctypes.windll.kernel32.AllocConsole()
            sys.stdin = open('CONIN$', 'r')
            sys.stdout = open('CONOUT$', 'w')
            sys.stderr = open('CONOUT$', 'w')
        except Exception as e:
            logging.debug(e)
        
        print("\n" + "=" * 60)
        print("  📋 DRIVERDOKTOR - CLI MÓD")
        print("=" * 60)
        
        run_cli_mode()
        os._exit(0)

    # Hardware rendering (gyors) - az autofix progress külön ablakban jelenik meg

    api = DriverToolApi()
    html_path = resource_path('ui.html')

    window = webview.create_window(
        'DriverDoktor',
        url=html_path,
        js_api=api,
        width=1200, height=780,
        min_size=(900, 600)
    )

    def on_start():
        api.set_window(window)

    # Watchdog: ha 15mp alatt nem indul el a GUI, bezárja az ablakot és CLI-re vált
    def webview_watchdog():
        TIMEOUT = 15  # seconds
        start = time.time()
        while time.time() - start < TIMEOUT:
            if _webview_ready.is_set():
                logging.info("[WATCHDOG] WebView2 sikeresen elindult")
                return  # GUI OK
            if _webview_error.is_set():
                logging.error("[WATCHDOG] WebView2 hiba detektálva, ablak bezárása...")
                time.sleep(0.5)  # Adj időt a log kiírására
                try:
                    window.destroy()
                except Exception as e:
                    logging.debug(e)
                return
            time.sleep(0.25)
        # Timeout
        logging.error(f"[WATCHDOG] {TIMEOUT}s timeout - WebView2 nem válaszol, ablak bezárása...")
        _webview_error.set()
        try:
            window.destroy()
        except Exception as e:
            logging.debug(e)

    watchdog_thread = threading.Thread(target=webview_watchdog, daemon=True)
    watchdog_thread.start()

    gui_failed = False
    try:
        logging.info("[MAIN] webview.start() hívása...")
        webview.start(func=on_start, debug=False)
        # webview.start() visszatért - ellenőrizzük hogy sikeres volt-e
        if not _webview_ready.is_set() or _webview_error.is_set():
            gui_failed = True
            logging.info("[MAIN] GUI nem indult el sikeresen, CLI mód következik...")
    except Exception as e:
        gui_failed = True
        logging.error(f"[MAIN] WebView indítási hiba: {e}")
        logging.error("[MAIN] Automatikus CLI mód indítása...")
    
    if gui_failed:
        # Konzol ablak létrehozása ha nincs (windowed exe-nél)
        try:
            ctypes.windll.kernel32.AllocConsole()
            # Stdin/stdout/stderr átirányítása az új konzolra
            sys.stdin = open('CONIN$', 'r')
            sys.stdout = open('CONOUT$', 'w')
            sys.stderr = open('CONOUT$', 'w')
        except Exception as e:
            logging.debug(e)
        
        print("\n" + "=" * 60)
        print("  ⚠️  GUI nem elérhető - CLI mód automatikusan aktiválva")
        print("  (Telepítsd a WebView2 Runtime-ot a GUI-hoz)")
        print("=" * 60)
        
        run_cli_mode()
    
    os._exit(0)
