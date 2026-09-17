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
from app.common import CMD_TIMEOUT_RETURNCODE
from app.common import CommandResult
from app.common import spawn_failed
from app.common import ps_force_utf8
from app.common import _FOLDER_DIALOG
from app.common import _OPEN_DIALOG
from app.common import _app_data_dir
from app.common import _app_exe_path
from app.common import _webview_error
from app.common import _webview_ready
from app import drivers_core
from app import update_core
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
        # Az AutoFix indító dialógusán bejelölt "tárolóvezérlő-driverek is" választás. A
        # lábak külön processzek, ezért a nyomtató-flaghez hasonlóan CSAK az ütemezett
        # feladat argumentumában él tovább (lásd _schedule_autofix_resume). Alapértelmezés:
        # KI - tárolódriver csak kifejezett engedéllyel mehet fel felügyelet nélkül.
        self.allow_storage_drivers = '--allow-storage-drivers' in sys.argv
        # Firmware-frissítések (UEFI/BIOS, SSD, TPM) engedélyezése - a tárolótól KÜLÖN
        # kapcsoló, mert más a kockázat: a firmware-írás visszafordíthatatlan, és egy
        # megszakadt flash hardveresen teszi tönkre az eszközt. Alapértelmezés: KI.
        self.allow_firmware_updates = '--allow-firmware' in sys.argv
        # "Wi-Fi-s telepítés": a gép vezeték nélkül lóg a hálózaton, ezért (a) a
        # CSATLAKOZOTT Wi-Fi adapter driverét a törlési fázis megtartja, és (b) minden
        # újraindítás után hosszabban várunk a hálózat felállására. A lábak külön
        # processzek, ezért a többi dialógus-választáshoz hasonlóan ez is CSAK az
        # ütemezett feladat argumentumában él tovább (lásd _schedule_autofix_resume).
        self.wifi_mode = '--wifi-mode' in sys.argv
        # Wi-Fi driver TELJES újraépítése (törlés + visszatelepítés a mentett példányból).
        # Csak Wi-Fi módban van értelme: vezetékesen az adapter drivere amúgy is a normál
        # törlési fázisban megy el, és a WU rakja vissza. Lásd wu_core.rebuild_wifi_driver.
        self.rebuild_wifi_driver = '--rebuild-wifi-driver' in sys.argv
        # A ZÁRÓ Windows Update-szüneteltetés (~10 év) kérése. Az alapértelmezés BE, ezért
        # - a többi kapcsolóval ellentétben - a TILTÁS utazik jelzőként: így egy régi
        # ütemezett feladat argumentuma (amiben még nincs ilyen kapcsoló) is a megszokott,
        # szüneteltető viselkedést adja. A lábak külön processzek, ezért ez is csak az
        # ütemezett feladat argumentumában él tovább (lásd _schedule_autofix_resume).
        self.no_wu_pause = '--no-wu-pause' in sys.argv
        # GYORS MÓD: a Microsoft Update Catalog kihagyása (explicit user decision,
        # 2026-09-02). Alapból BE van kapcsolva a katalógus, ezért - a wu-pause-hoz
        # hasonlóan - a TILTÁS utazik jelzőként: egy régebbi ütemezett feladat
        # argumentuma (amiben ez a kapcsoló még nincs) így a megszokott, teljes
        # keresést adja. MÉRT ÁR: a katalógus kihagyása 20-25 percet spórol egy
        # lábakra bontott láncon, DE azon a Dell-en, amiről ez a mérés készült, a WU
        # keresés időtúllépésbe futott, és a 16 driver MIND a katalógusból jött volna -
        # gyors módban az a gép 0 drivert kapott volna. Lásd CLAUDE.md.
        self.no_catalog = '--no-catalog' in sys.argv
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
        except Exception as e:
            logging.debug(f"[INIT] Régi .exe.old törlése sikertelen (zárolt fájl?): {e}")

        # Ha egy korábbi Stabilitás Teszt indítás letiltotta a képernyő-kikapcsolást/alvó
        # módot, itt, a program (újra)indulásakor állítjuk vissza az akkor elmentett eredeti
        # energiagazdálkodási beállításokat (lásd _lock_power_for_stress/_restore_power_after_stress).
        self._restore_power_after_stress()

        # A LEGUTÓBBI EXE-CSERE KIMENETELE A NAPLÓBA. A cserét egy külön .bat végzi a
        # program kilépése UTÁN, tehát a fő napló eddig semmit nem tudott a
        # legkritikusabb lépésről: lecserélődött-e egyáltalán az exe. Ha valaki azt
        # jelenti, hogy "frissítettem, mégis a régi van fent", ez a pár sor a válasz.
        update_core.read_update_report()

        # Ugyanez a logika a másik "ideiglenes rendszerállapot" esetre: ha egy AutoFix lánc
        # a telepítő lábon szakadt meg (BSOD/áramszünet), ott maradhat a NoAutoUpdate=1
        # csoportházirend, ami a TELJES Windows Update-et letiltja. Lásd a metódus
        # docstringjét (app/gui/autofix.py) - szűk, bizonyíték-alapú feltétellel takarít.
        self._cleanup_leftover_autofix_policy()

        # Harmadik "félbehagyott munka" eset: az AutoFix UTOLSÓ lábában elhalasztott
        # INF-kivezetés (lásd _finish_deferred_inf_cleanup). A lánc közbeni halasztásokat
        # a következő láb intézi, de ha a halasztás az utolsó lábban történt, nincs
        # következő láb - ilyenkor itt, a legközelebbi induláskor fejezzük be (ekkor a
        # gép már réges-rég újraindult, tehát a kötések eldőltek). Háttérszálon fut és
        # csak akkor csinál bármit, ha a fájl létezik: normál induláskor egy fájl-
        # létezés-vizsgálat a teljes költsége, a WMI-lekérdezés (~15-25 mp) nem
        # késleltetheti az ablak megjelenését.
        try:
            if os.path.exists(self._deferred_inf_cleanup_path()):
                threading.Thread(target=self._deferred_inf_cleanup_at_startup,
                                 name='inf-cleanup', daemon=True).start()
        except Exception as e:
            logging.debug(f"[INIT] Elhalasztott INF-kivezetés indítása sikertelen: {e}")

        logging.info("[INIT] DriverToolApi kész.")

    def _deferred_inf_cleanup_at_startup(self):
        """Az induláskori pótló kivezetés burkolója. Resume lábon NEM fut: ott a lánc
        maga intézi a megfelelő ponton (a törlési fázis után), és két párhuzamos
        pnputil-törlés-sorozat egymásnak esne. Csendes: a felületre nem emit-el
        (indulásnál nincs is még hova), csak a naplóba dolgozik."""
        try:
            if getattr(self, 'resume_mode', False) or getattr(self, 'resume_step1', False):
                logging.debug("[INIT] Resume láb - az elhalasztott INF-kivezetést a lánc végzi el.")
                return
            logging.info("[INIT] Elhalasztott INF-kivezetés található - befejezés a háttérben.")
            self._finish_deferred_inf_cleanup(task_id='')
        except Exception as e:
            logging.warning(f"[INIT] Az elhalasztott INF-kivezetés nem fejeződött be: {e}")

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
                    # A felület bizonyítottan él: a kockázatos natív szakasz (pythonnet +
                    # WebView2 betöltés) túl van, tehát az összeomlás-jelző törölhető.
                    # Lásd common.gui_attempt_begin / gui_crash_check.
                    common.gui_attempt_succeeded()
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

    def _run(self, cmd, *, ok_codes=(0,), **kwargs):
        # Log minden parancs futtatását.
        # ok_codes: a hívó által VÁRT (nem hibának számító) visszatérési kódok - pl. a
        # "reg delete" 1-es kódja nemlétező kulcsnál, vagy a pnputil 3010-e (siker, de
        # reboot kell). Ezek DEBUG-on logolódnak "várt kód" jelöléssel, hogy a WARNING
        # szint tényleg csak a valódi anomáliákat tartalmazza. A visszatérési értéket
        # nem befolyásolja, csak a log-szintet.
        cmd_str = cmd if isinstance(cmd, str) else ' '.join(str(c) for c in cmd)
        logging.debug(f"[CMD] Futtatás: {cmd_str[:300]}")
        # PowerShell -Command: a kimenet UTF-8-ra kényszerítése + UTF-8 dekódolás.
        # Lásd common.ps_force_utf8 - enélkül a PS az OEM kódlappal (magyar Windowson
        # cp852) ír a pipe-ra, és minden ékezetes visszaadott érték (nyomtatónév,
        # eszköznév, SSID) U+FFFD-vel romlik el MÉG A MEMÓRIÁBAN. A logba szándékosan
        # az EREDETI parancsot írjuk (fent): az előtag állandó és dokumentált, a 300
        # karakteres levágásból viszont pont a lényeget enné el.
        cmd, want_utf8 = ps_force_utf8(cmd)
        if want_utf8:
            kwargs.setdefault('encoding', 'utf-8')
        # stdin alapból DEVNULL: egyik parancsunk sem olvas stdin-t, VISZONT a stressz-teszt
        # automatizálás AttachConsole/FreeConsole hívásai után a folyamat örökölt stdin
        # handle-je érvénytelenné válik, és az örökölt-stdin + capture_output kombináció
        # ettől kezdve MINDEN parancsindítást "[WinError 6] A leíró érvénytelen" hibával
        # buktatna el (terepen bizonyított: stressz teszt után a taskkill sem futott le).
        kwargs.setdefault('stdin', subprocess.DEVNULL)
        start = time.monotonic()
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, errors='replace',
                                  startupinfo=self._si, creationflags=self._nw, **kwargs)
            elapsed = time.monotonic() - start
            # Log eredmény
            if spawn_failed(result):
                # A folyamat el sem indult - lásd common.STATUS_DLL_INIT_FAILED. Külön,
                # greppelhető ERROR: ez sosem "a parancs nem sikerült", hanem rendszerszintű baj.
                logging.error(f"[CMD] A FOLYAMAT EL SEM INDULT (0xC0000142 / STATUS_DLL_INIT_FAILED, {elapsed:.1f}s) - "
                              f"a Windows session nem tud új processzt indítani (leállás alatt / szétesett eszközverem). Parancs: {cmd_str[:200]}")
            elif result.returncode not in ok_codes:
                logging.warning(f"[CMD] Visszatérési kód: {result.returncode} ({elapsed:.1f}s)")
                if result.stderr:
                    logging.warning(f"[CMD] stderr: {result.stderr[:4000]}")
            elif result.returncode != 0:
                logging.debug(f"[CMD] OK - várt kód: {result.returncode} ({elapsed:.1f}s)")
            else:
                logging.debug(f"[CMD] OK ({elapsed:.1f}s)")
            
            # Log teljes kimenet 4000 karakterig
            # A DriverStore-t módosító parancsok eldobják a csomaglista-gyorsítótárat.
            # ITT, EGY HELYEN - egy hívási helyenkénti érvénytelenítést előbb-utóbb
            # elfelejtenénk valahol, és onnantól egy elavult lista alapján döntene a lánc
            # (lásd app/gui/drivers.py: _get_third_party_drivers).
            try:
                if drivers_core.mutates_driver_store(cmd_str):
                    inv = getattr(self, 'invalidate_driver_cache', None)
                    if inv:
                        inv()
            except Exception as _e:
                logging.debug(f"[CMD] A gyorsítótár érvénytelenítése nem sikerült: {_e}")

            if result.stdout:
                out_txt = result.stdout.strip()
                if len(out_txt) > 4000: out_txt = out_txt[:4000] + '... [TRUNCATED]'
                logging.debug(f"[CMD] stdout: {out_txt}")
            return result
        except subprocess.TimeoutExpired as e:
            # timeout= kwarg-gal hívott parancs túllépte az időt. A subprocess.run ilyenkor
            # már kilőtte a gyereket; mi CommandResult-ot adunk vissza (nem kivételt), hogy
            # egy beragadt segédprogram (terepen: pnputil /delete-driver egy nem válaszoló
            # eszközön 143 mp-ig) ne akassza meg az egész AutoFix lábat.
            elapsed = time.monotonic() - start
            logging.error(f"[CMD] IDŐTÚLLÉPÉS ({elapsed:.1f}s, limit={kwargs.get('timeout')}s): {cmd_str[:200]}")
            partial = (e.stdout or b'') if isinstance(e.stdout, (bytes, bytearray)) else (e.stdout or '')
            if isinstance(partial, (bytes, bytearray)):
                partial = partial.decode('utf-8', errors='replace')
            return CommandResult(CMD_TIMEOUT_RETURNCODE, partial, 'IDŐTÚLLÉPÉS')
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
            start_time = time.monotonic()
            try:
                target()
                elapsed = time.monotonic() - start_time
                logging.info(f"[THREAD:{task}] Befejezve ({elapsed:.1f}s)")
            except Exception as e:
                elapsed = time.monotonic() - start_time
                logging.error(f"[THREAD:{task}] HIBA ({elapsed:.1f}s): {e}")
                logging.error(f"[THREAD:{task}] Traceback:\n{traceback.format_exc()}")
                self.emit('task_error', {'task': task, 'error': str(e)})
                self.emit('task_complete', {'task': task, 'status': f'❌ Hiba: {e}'})
            finally:
                self._task_busy = None
        threading.Thread(target=wrapper, daemon=True, name=f"task-{task}").start()

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
        # A FUTÓ rendszer meghajtó-gyökere nem lehet offline cél: a DISM a futó
        # Windowsra /Image:-ként 87-es hibával elszáll ("The /Image option ... points
        # to a running Windows installation"), és minden lista üresen ("0 driver")
        # jönne vissza - terepi logból (Build 208) azonosított eset, amikor a "Másik
        # lemez kiválasztása" gombbal a C:\-t választották. Ilyenkor élő módban
        # maradunk, ami pont ugyanazt a rendszert kezeli, csak a helyes (/Online) úton.
        try:
            sys_root = os.path.normcase(os.path.normpath(os.environ.get('SystemDrive', 'C:') + '\\'))
            if os.path.normcase(os.path.normpath(path)) == sys_root:
                self.target_os_path = None
                self.emit('toast', {'message': 'ℹ️ Ez a most futó rendszer meghajtója - Élő módban kezeljük (az offline mód csak másik/nem bootolt Windowsra való).', 'type': 'info'})
                logging.info("[API] apply_target_os: a futó rendszer gyökere lett kiválasztva -> élő mód marad.")
                return False
        except Exception as e:
            logging.debug(f"[API] apply_target_os élő-meghajtó ellenőrzés kihagyva: {e}")
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

    def _check_internet(self, require_dns=False, quiet=False):
        """Megbízható TCP port alapú internet ellenőrzés.

        `quiet=True`: a sikertelen próbák NEM naplózódnak soronként. A `_wait_for_internet`
        ciklusa hívja így, a legelső próba kivételével - lásd ott az indoklást (a napló
        egy 45 mp-es várakozásra ~50 azonos DEBUG sort kapott, ami a Rule 0 saját
        ellen-szabályába ütközik: maximális INFORMÁCIÓ kell, nem maximális sor).

        A NÉVFELOLDÁS AZ ELSŐ PRÓBA, ÉS EZ NEM SORRENDI ÍZLÉS - EZ MAGA A LÉNYEG
        (2026-09-01, terepen mérve, Dell Latitude 5580, Build 288). A régi sorrend a
        nyers `8.8.8.8:53` IP-próbával kezdett, ami **definíció szerint nem igényel
        DNS-t**, és sikerre futva azonnal True-t adott - a hosztneves próba tehát SOHA
        nem futott le. A napló pontosan ezt mutatja: a driver-törlés utáni lábon
        `_check_internet -> True (0.20s)`, majd ugyanabban a futásban **22 db
        `[Errno 11001] getaddrinfo failed`** a katalógus-letöltéseknél, és a WU-keresés
        a teljes 300 mp-es időkorlátig futott, majd `IDŐTÚLLÉPÉS`. Vagyis: a kapcsolat
        élt, a NÉVFELOLDÁS viszont még nem - a lánc pedig "van internet"-et látott, és
        vakon nekiment egy 5 perces WUA-keresésnek, aminek a szerverét fel sem tudta
        oldani. Minden későbbi hálózati művelet (WUA, Microsoft-katalógus, gyártói
        oldalak) hosztnévvel dolgozik, tehát a DNS a valódi feltétel, nem a nyers TCP.

        `require_dns=True` esetén CSAK a névfeloldásos próba számít. Alapból False, hogy
        a "van-e egyáltalán kapcsolat" jellegű hívók (netdrv-visszaállítás döntése)
        változatlanul működjenek - de ilyenkor a DNS hiányát WARNING-gal naplózzuk, mert
        az ezután következő letöltések ettől még el fognak hasalni, és e nélkül a sor
        nélkül az ok megint láthatatlan maradna.

        Az időkorlát SZÁNDÉKOSAN hívásonként megy (create_connection timeout=), NEM
        socket.setdefaulttimeout()-tal: az utóbbi a TELJES PROCESSZRE állítja be az
        alapértelmezett socket-időkorlátot, és sosem áll vissza. Emiatt az első
        internet-ellenőrzés után minden olyan hálózati művelet, ami nem ad meg saját
        időkorlátot, 3 másodperc után elhasalna - egy nagy letöltésnél (NVIDIA driver,
        stresstools.zip) ez egyetlen lassú másodperc miatt megszakadó letöltést jelent.
        Ma minden hívónk ad explicit timeout-ot, tehát ez nem sült el; a globális
        mellékhatást viszont ne hozzuk vissza."""
        import socket
        # 1) Névfeloldást IGÉNYLŐ próba. Ez az igazi kérdés: fel tudjuk-e oldani a
        #    hosztneveket, amikkel a WU és a katalógus dolgozik.
        for host, port in (("www.microsoft.com", 80), ("dns.google", 443)):
            try:
                with socket.create_connection((host, port), timeout=3.0):
                    return True
            except Exception as e:
                if not quiet:
                    logging.debug(f"[NET] Névfeloldásos internet-ellenőrzés sikertelen ({host}:{port}): {e}")
        if require_dns:
            return False
        # 2) Nyers IP-próba: DNS nélkül is elárulja, hogy van-e egyáltalán kapcsolat.
        try:
            with socket.create_connection(("8.8.8.8", 53), timeout=3.0):
                logging.warning("[NET] Van hálózati kapcsolat, de a NÉVFELOLDÁS nem működik "
                                "(a hosztneves próbák elbuktak, a nyers IP elérhető). A most "
                                "következő letöltések és a WU-keresés emiatt elhasalhatnak.")
                return True
        except Exception as e:
            if not quiet:
                logging.debug(f"[NET] Internet-ellenőrzés sikertelen (8.8.8.8:53): {e}")
        return False

    def _wait_for_internet(self, timeout, task_id=None, reason=''):
        """Megvárja, amíg a hálózat feláll - a `_check_internet` EGYSZERI próbája helyett.

        MIÉRT: a `_check_internet` egy 3 mp-es TCP-próba, és a lánc közvetlenül a
        bejelentkezés után futtatja. Kábelnél a link már bootkor él, Wi-Finél viszont a
        WLAN szolgáltatás indulása + asszociáció + hitelesítés + DHCP együtt 15-45 mp -
        vagyis az AutoFix "nincs internet"-et látott olyankor is, amikor fél perc múlva
        lett volna. Ez volt a "wifivel nem megy" panasz első számú oka (2026-08-05).
        Vezetékesen is hasznos: lassú switch/DHCP mellett eddig szintén elhasalhatott.

        Azonnal tér vissza, ha már az első próba sikerül (a tipikus eset) - a várakozás
        csak akkor kerül bármibe, ha tényleg nincs még hálózat. Kb. 2 mp-enként próbál
        újra, és 10 mp-enként visszajelez a felületre, hogy ne tűnjön fagyottnak.
        Megszakítható: a _cancel_flag-et minden körben nézi.

        A VÁRAKOZÁS A NÉVFELOLDÁSRA IS VONATKOZIK (2026-09-01): a cikluson belül
        `require_dns=True`-val próbálkozunk, mert a driver-törlés utáni bootnál a
        kapcsolat előbb áll fel, mint a DNS - és a lánc minden következő lépése
        (WUA-keresés, katalógus-letöltés) hosztnévvel dolgozik. Terepen mérve ez az 5
        perces WU-időtúllépés OKA volt, nem a következménye: a régi próba a nyers
        `8.8.8.8`-cal azonnal True-t adott, a WUA pedig utána a teljes időkorlátig
        próbálta feloldani a szerverét. A keret LEJÁRTAKOR viszont elfogadjuk a
        DNS nélküli kapcsolatot is: az "eddig vártunk, most már próbáljuk meg" mindig
        jobb, mint hamisan azt állítani, hogy nincs hálózat - a lánc no-net ága ilyenkor
        fölöslegesen állítaná vissza a NIC-mentést.

        Visszatérés: True, ha lett internet a határidőn belül."""
        deadline = time.monotonic() + max(0, timeout)
        attempt = 0
        announced = False
        while True:
            if getattr(self, '_cancel_flag', False):
                logging.info("[NET] A hálózat-várakozást megszakították.")
                return False
            # CSAK AZ ELSŐ PRÓBA NAPLÓZ SORONKÉNT: abból kiderül, MI a hiba (getaddrinfo,
            # timeout, unreachable), a további ~20-25 azonos sor viszont már csak zaj -
            # terepen egyetlen 45 mp-es várakozás ~50 sort írt, kétszer egymás után. A
            # végeredményt a ciklus végi összefoglaló sorok mondják ki (siker/kudarc,
            # eltelt idő, próbák száma), ami pontosan az az egy sor, amit olvasni kell.
            if self._check_internet(require_dns=True, quiet=bool(attempt)):
                if attempt:
                    waited = int(timeout - max(0, deadline - time.monotonic()))
                    logging.info(f"[NET] Internet {waited} mp várakozás után elérhető ({attempt + 1}. próba).")
                    if task_id:
                        self.emit('task_progress', {'task': task_id, 'log': f'✅ Hálózat feláll ({waited} mp után).'})
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # A keret lejárt. Ha van kapcsolat, csak DNS nincs, akkor is True-t adunk:
                # jobb megpróbálni a letöltéseket, mint a no-net ágra menni (az fölöslegesen
                # visszaállítaná a NIC-mentést). A hiányzó DNS-t a _check_internet naplózza.
                if self._check_internet():
                    logging.warning(f"[NET] {timeout} mp alatt sem lett NÉVFELOLDÁS, de a kapcsolat él "
                                    f"- továbbmegyünk ({attempt + 1} próba, ok: {reason or 'n/a'}).")
                    if task_id:
                        self.emit('task_progress', {'task': task_id, 'log': '⚠️ A hálózat él, de a névfeloldás (DNS) nem áll - a letöltések elhasalhatnak.'})
                    return True
                logging.warning(f"[NET] {timeout} mp alatt sem lett internet ({attempt + 1} próba, ok: {reason or 'n/a'}).")
                return False
            if not announced:
                announced = True
                logging.info(f"[NET] Még nincs internet - várakozás max. {timeout} mp ({reason or 'n/a'}).")
                if task_id:
                    self.emit('task_progress', {'task': task_id, 'log': f'⏳ Várakozás a hálózatra (max. {timeout} mp){(" - " + reason) if reason else ""}...'})
            elif attempt % 5 == 0 and task_id:
                self.emit('task_progress', {'task': task_id, 'log': f'⏳ Még nincs hálózat... (hátra: {int(remaining)} mp)'})
            attempt += 1
            time.sleep(min(2.0, max(0.1, remaining)))

    def open_file(self, path):
        logging.info(f"[API] open_file: {path}")
        try:
            os.startfile(path)
            return True
        except Exception as e:
            logging.error(f"Cannot open file: {e}")
            return False
