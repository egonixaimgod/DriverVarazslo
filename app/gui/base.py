"""DriverVarázsló GUI - GUI alap: init, WebView ablak, emit (Python->JS push), _run (subprocess wrapper), háttérszál-kezelés (_safe_thread), cél-OS váltás, fájl-dialógusok."""

# === AUTO-IMPORTS ===
import os
import sys
import subprocess
import threading
import time
import logging
import json
import traceback
from app import common
from app.common import _FOLDER_DIALOG
from app.common import _OPEN_DIALOG
from app.common import _app_data_dir
from app.common import _app_exe_path
from app.common import _webview_error
from app.common import _webview_ready
# === /AUTO-IMPORTS ===


class GuiBaseMixin:
    """GUI alap: init, WebView ablak, emit (Python->JS push), _run (subprocess wrapper), háttérszál-kezelés (_safe_thread), cél-OS váltás, fájl-dialógusok. A DriverToolApi része (összerakás: app/gui/api.py)."""

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
        self._task_busy = None  # None, vagy a jelenleg futó feladat neve (lásd _safe_thread)
        self._stresstools_download_lock = threading.Lock()
        self._console_attach_lock = threading.Lock()
        self._stress_pids = {}  # az általunk indított stressz-programok PID-jei (stop_stress_tests-hez)
        self._last_report_path = None  # a legutóbb generált Rendszer Riport útvonala (print_via_store_printer-hez)
        self.resume_mode = '--resume-autofix' in sys.argv
        self.resume_step1 = '--resume-step1' in sys.argv
        self.skip_printer_drivers = '--skip-printer-drivers' in sys.argv
        self._si = subprocess.STARTUPINFO()
        self._si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        self._nw = subprocess.CREATE_NO_WINDOW
        logging.info(f"[INIT] sys_drive={self.sys_drive}")
        
        # Takarítás: frissítés utáni régi .exe törlése
        try:
            exe_path = _app_exe_path()
            old_path = exe_path + ".old"
            if os.path.exists(old_path):
                os.remove(old_path)
                logging.info("[INIT] Régi verzió (update előtti) törölve.")
        except Exception:
            pass

        # Ha egy korábbi Stabilitás Teszt indítás letiltotta a képernyő-kikapcsolást/alvó
        # módot, itt, a program (újra)indulásakor állítjuk vissza az akkor elmentett eredeti
        # energiagazdálkodási beállításokat (lásd _lock_power_for_stress/_restore_power_after_stress).
        self._restore_power_after_stress()

        logging.info("[INIT] DriverToolApi kész.")

    def set_window(self, window):
        logging.info("[WINDOW] WebView ablak beállítása...")
        self._window = window
        # Wait for WebView2 DOM to be ready (max 12s, watchdog timeout: 60s)
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
                # U+2028/U+2029 a JSON-ban érvényes, de egy JS string-literálba nyers szövegként
                # beillesztve (nem JSON.parse-on át) sor-terminátornak számíthat és megszakíthatja
                # a generált window.handlePyEvent(...) hívást - escape-eljük explicit \uXXXX-ként.
                payload = payload.replace(' ', '\\u2028').replace(' ', '\\u2029')
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
        # stdin alapból DEVNULL: egyik parancsunk sem olvas stdin-t, VISZONT a stressz-teszt
        # automatizálás AttachConsole/FreeConsole hívásai után a folyamat örökölt stdin
        # handle-je érvénytelenné válik, és az örökölt-stdin + capture_output kombináció
        # ettől kezdve MINDEN parancsindítást "[WinError 6] A leíró érvénytelen" hibával
        # buktatna el (terepen bizonyított: stressz teszt után a taskkill sem futott le).
        kwargs.setdefault('stdin', subprocess.DEVNULL)
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
        """Háttérszálon futtatja a target()-et.

        Egyszerre csak EGY ilyen feladat futhat: ha már fut egy másik, ezt elutasítjuk,
        mert két egyidejű feladat egyébként ütközne a közös self._cancel_flag-en (egy
        épp induló új feladat False-ra állítaná a MÁR futó feladat megszakítás-kérését),
        és pl. a hardver-scan/driver-telepítés is ugyanazt a self.hw_updates_pool listát
        írná-olvasná egyszerre. A _cancel_flag reset-jét is itt, a busy-check UTÁN
        végezzük - ha korábban minden hívó metódus saját maga nullázta a flaget MIELŐTT
        idekerült volna, egy elutasított (busy) próbálkozás is csendben visszavonta volna
        a ténylegesen futó feladat megszakítás-kérését.
        """
        if self._task_busy:
            logging.warning(f"[THREAD:{task}] Elutasítva - már fut egy másik feladat ({self._task_busy}).")
            self.emit('toast', {'message': f'⚠️ Már folyamatban van egy másik művelet ({self._task_busy}), várd meg amíg befejeződik!', 'type': 'warning'})
            return
        self._task_busy = task
        self._cancel_flag = False

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
            finally:
                self._task_busy = None
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
        logging.info(f"[API] get_init_data() hívás - build={common.BUILD_NUMBER}, target={self.target_os_path}")
        return {'build': common.BUILD_NUMBER, 'sys_drive': self.sys_drive, 'target_os': self.target_os_path, 'resume_mode': getattr(self, 'resume_mode', False), 'resume_step1': getattr(self, 'resume_step1', False), 'app_data_dir': _app_data_dir()}

    def reboot_system(self):
        logging.info("[API] reboot_system() - Felhasználó újraindítást kért")
        self._run(['shutdown', '/r', '/t', '0', '/f'])
        return True

    def open_defender(self):
        logging.info("[API] open_defender() - Windows Defender megnyitása")
        self._run(['start', 'windowsdefender://threat'], shell=True)
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

    def _check_internet(self):
        """Megbízható TCP port alapú internet ellenőrzés."""
        import socket
        try:
            socket.setdefaulttimeout(3.0)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect(("8.8.8.8", 53))
            return True
        except Exception:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.connect(("www.microsoft.com", 80))
                return True
            except Exception:
                return False

    def open_file(self, path):
        logging.info(f"[API] open_file: {path}")
        try:
            os.startfile(path)
            return True
        except Exception as e:
            logging.error(f"Cannot open file: {e}")
            return False
