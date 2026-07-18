"""DriverVarázsló GUI - Stabilitás Teszt: eszközök letöltése/indítása/leállítása, energiagazdálkodás-zár."""

# === AUTO-IMPORTS ===
import ctypes
import ctypes.wintypes
import os
import subprocess
import re
import threading
import time
import logging
import traceback
import winreg
import math
from app.common import _ps_quote
from app.stress_defs import LINPACK_PROMPT_SCRIPT
from app.stress_defs import LINPACK_RAM_OPTIONS
from app.stress_defs import STRESS_CLICK_SEQUENCES
from app.stress_defs import STRESS_KILL_IMAGES
from app.stress_defs import STRESS_POWER_REG_KEY
from app.stress_defs import STRESS_POWER_SETTINGS
from app.stress_defs import STRESS_TOOLS
from app.stress_defs import STRESS_TOOLS_BULK
from app.win32 import SEE_MASK_NOCLOSEPROCESS
from app.win32 import SW_SHOWNORMAL
from app.win32 import _MEMORYSTATUSEX
from app.win32 import _SHELLEXECUTEINFOW
# === /AUTO-IMPORTS ===


class GuiStressMixin:
    """Stabilitás Teszt: eszközök letöltése/indítása/leállítása, energiagazdálkodás-zár. A DriverToolApi része (összerakás: app/gui/api.py)."""

    # ================================================================
    # STABILITÁS TESZT - energiagazdálkodás (képernyő/alvó mód letiltása közben)
    # ================================================================
    def _query_power_setting(self, subgroup, setting):
        """(ac_másodperc, dc_másodperc) lekérdezése egy powercfg alias-párra.

        A SUB_VIDEO/VIDEOIDLE stb. alias-kulcsszavak nyelvfüggetlenek, de a `powercfg
        /query` kimenetének felirat-szövegei lokalizáltak lehetnek - ezért nem szöveges
        mintára illesztünk, hanem a kimenetben szereplő "0x..." hexa értékeket szedjük ki
        POZÍCIÓ szerint (a sorrend - előbb AC, utána DC - nem nyelvfüggő)."""
        try:
            res = self._run(['powercfg', '/query', 'SCHEME_CURRENT', subgroup, setting])
            hexes = re.findall(r'0x[0-9a-fA-F]+', res.stdout or '')
            if len(hexes) >= 2:
                return int(hexes[0], 16), int(hexes[1], 16)
        except Exception as e:
            logging.warning(f"[STRESS_POWER] Lekérdezési hiba ({subgroup}/{setting}): {e}")
        return None, None

    def _set_power_setting(self, subgroup, setting, ac_seconds, dc_seconds):
        self._run(['powercfg', '/setacvalueindex', 'SCHEME_CURRENT', subgroup, setting, str(ac_seconds)])
        self._run(['powercfg', '/setdcvalueindex', 'SCHEME_CURRENT', subgroup, setting, str(dc_seconds)])

    def _lock_power_for_stress(self):
        """Letiltja a kijelző kikapcsolását és az alvó/hibernálás módot (AC és DC is), amíg
        a stressz-teszt programok futnak - enélkül a gép/kijelző elalhatna egy hosszú, több
        órás stabilitás-teszt közben. Az EREDETI értékeket (csak ha még nincs korábbi
        mentés) elmentjük a registrybe, hogy a program legközelebbi indításakor
        (_restore_power_after_stress, hívva __init__-ből) visszaállíthassuk őket."""
        try:
            try:
                already_saved = True
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, STRESS_POWER_REG_KEY, 0, winreg.KEY_READ):
                    pass
            except FileNotFoundError:
                already_saved = False

            if not already_saved:
                with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, STRESS_POWER_REG_KEY, 0, winreg.KEY_WRITE) as key:
                    for subgroup, setting in STRESS_POWER_SETTINGS:
                        ac, dc = self._query_power_setting(subgroup, setting)
                        if ac is not None:
                            winreg.SetValueEx(key, f'{subgroup}_{setting}_AC', 0, winreg.REG_DWORD, ac)
                        if dc is not None:
                            winreg.SetValueEx(key, f'{subgroup}_{setting}_DC', 0, winreg.REG_DWORD, dc)
                logging.info("[STRESS_POWER] Eredeti energiagazdálkodási beállítások elmentve.")

            for subgroup, setting in STRESS_POWER_SETTINGS:
                self._set_power_setting(subgroup, setting, 0, 0)
            self._run(['powercfg', '/setactive', 'SCHEME_CURRENT'])
            logging.info("[STRESS_POWER] Képernyő-kikapcsolás és alvó mód letiltva a stressz teszt idejére.")
        except Exception as e:
            logging.warning(f"[STRESS_POWER] Letiltási hiba: {e}")

    def _restore_power_after_stress(self):
        """A program indulásakor hívva: ha egy korábbi Stabilitás Teszt futás során
        elmentettük az eredeti energiagazdálkodási értékeket, itt visszaállítjuk őket, majd
        töröljük a mentést, hogy egy következő stressz teszt friss eredeti állapotot
        mentsen el (ne egy már egyszer nullázott értéket)."""
        try:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, STRESS_POWER_REG_KEY, 0, winreg.KEY_READ) as key:
                    values = {}
                    i = 0
                    while True:
                        try:
                            name, value, _ = winreg.EnumValue(key, i)
                            values[name] = value
                            i += 1
                        except OSError:
                            break
            except FileNotFoundError:
                return

            restored_any = False
            for subgroup, setting in STRESS_POWER_SETTINGS:
                ac = values.get(f'{subgroup}_{setting}_AC')
                dc = values.get(f'{subgroup}_{setting}_DC')
                if ac is not None and dc is not None:
                    self._set_power_setting(subgroup, setting, ac, dc)
                    restored_any = True
            if restored_any:
                self._run(['powercfg', '/setactive', 'SCHEME_CURRENT'])
                logging.info("[STRESS_POWER] Eredeti energiagazdálkodási beállítások visszaállítva.")

            try:
                winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, STRESS_POWER_REG_KEY)
            except Exception as e:
                logging.debug(f"[STRESS_POWER] Mentett powercfg-kulcs törlése kihagyva (nem is létezett?): {e}")
        except Exception as e:
            logging.warning(f"[STRESS_POWER] Visszaállítási hiba: {e}")

    def _find_stress_tool_exes(self, stress_dir, keys):
        """Megkeresi a kicsomagolt mappában a megadott STRESS_TOOLS kulcsokhoz tartozó
        exe-ket. Egy kulcson belül a STRESS_TOOLS[key][1] filenames-lista SORRENDJE
        prioritást jelent (pl. HWiNFO-nál előbb a 64, majd a 32 bites) - ezért nem az
        os.walk bejárási sorrendjében elsőként talált fájlt fogadjuk el, hanem a teljes
        bejárás után, kulcsonként, a legmagasabb prioritású (legkorábbi) filenames-
        bejegyzést választjuk ki az összes ténylegesen megtalált jelölt közül."""
        candidates = {key: {} for key in keys}
        for root, dirs, files in os.walk(stress_dir):
            for file in files:
                fl = file.lower()
                for key in keys:
                    filenames = STRESS_TOOLS[key][1]
                    if fl in filenames and fl not in candidates[key]:
                        candidates[key][fl] = os.path.join(root, file)

        found = {}
        for key in keys:
            found[key] = None
            for fname in STRESS_TOOLS[key][1]:
                if fname in candidates[key]:
                    found[key] = candidates[key][fname]
                    break
        return found

    def _get_ram_gb(self):
        """(teljes, szabad) fizikai RAM GB-ban. A teljes felfelé kerekítve - a Windows a
        ténylegesen jelentett bájtszámot mindig a "reklámozott" kapacitás alatt adja
        vissza a hardver számára fenntartott tartomány miatt, pl. egy 8GB-os gép gyakran
        ~7.85 GB-ot jelent - felkerekítve ez helyesen 8-cá válik. A szabad érték kerekítés
        nélküli (tört GB). Hiba esetén (None, None)."""
        try:
            ctypes.windll.kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MEMORYSTATUSEX)]
            ctypes.windll.kernel32.GlobalMemoryStatusEx.restype = ctypes.wintypes.BOOL
            stat = _MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return math.ceil(stat.ullTotalPhys / (1024 ** 3)), stat.ullAvailPhys / (1024 ** 3)
        except Exception as e:
            logging.warning(f"[STRESSTOOLS] RAM lekérdezési hiba: {e}")
        return None, None

    def _pick_linpack_ram_option(self, total_gb, avail_gb=None):
        """A LINPACK_RAM_OPTIONS közül a legnagyobb olyan opciót választja, ami a rendszer
        TELJES RAM-jába ("total_gb") belefér, ÉS - ha ismert - az éppen SZABAD memóriába
        ("avail_gb") is, ~1.5GB tartalékot hagyva. Az utóbbi korlát fontos: egy 12GB-os
        gépen a teljes RAM alapján "beleférne" a 10GB-os opció, de ha a Windows + a többi
        épp induló stressz-program mellett csak ~6GB szabad, a 10GB-os allokáció az egész
        gépet lapozásba fojtaná, és egyik teszt sem futna használhatóan."""
        if not total_gb:
            return 4  # ismeretlen RAM esetén biztonságos alapértelmezés (8GB opció)
        cap = total_gb
        if avail_gb:
            cap = min(cap, avail_gb - 1.5)
        best = LINPACK_RAM_OPTIONS[0][0]
        for opt_num, gb in LINPACK_RAM_OPTIONS:
            if gb <= cap:
                best = opt_num
        return best

    def _build_linpack_console_script(self):
        """Összeállítja a Linpack Xtreme konzolos indító menüjéhez tartozó (prompt-részlet,
        válasz) párokat a LINPACK_PROMPT_SCRIPT alapján, a RAM-menü válaszát a gép teljes
        ÉS szabad memóriájához illő opcióra cserélve (lásd _pick_linpack_ram_option)."""
        total_gb, avail_gb = self._get_ram_gb()
        ram_option = self._pick_linpack_ram_option(total_gb, avail_gb)
        avail_txt = f"{avail_gb:.1f}" if avail_gb else "?"
        logging.info(f"[STRESSTOOLS] Linpack RAM-automatizálás: teljes RAM={total_gb} GB, szabad={avail_txt} GB -> {ram_option}. opció")
        return [(prompt, str(ram_option) if answer is None else answer, needs_enter)
                for prompt, answer, needs_enter in LINPACK_PROMPT_SCRIPT]

    def _launch_stress_exe(self, exe, display_name, console_script=None, click_sequence=None, thread_sink=None):
        """Egy stressz-teszt/monitor .exe elindítása, UAC-elutasítás (WinError 740, pl.
        HWiNFO64.exe requireAdministrator manifestje) esetén ShellExecuteExW-es 'runas'
        újrapróbálással. Visszaadási érték:
          - PID (pozitív int), ha sikerült elindítani és ismerjük a PID-jét - ez a normál
            (nem emelt) eset ÉS a 'runas' eset is: utóbbinál ShellExecuteExW-et
            SEE_MASK_NOCLOSEPROCESS maszkkal hívjuk (a sima ShellExecuteW nem adna vissza
            semmilyen handle-t/PID-et), a kapott hProcess-ből GetProcessId-vel kinyerjük a
            valódi PID-et, ugyanúgy, mint a nem emelt ágon,
          - -1, ha sikerült indítani 'runas'-sal, de a ShellExecuteExW mégsem adott vissza
            process handle-t (pl. a felhasználó elutasította a UAC-promptot, vagy
            AppCompat-tükrözés zajlik) - ilyenkor sem az ablak automatikus pozicionálása
            (lásd _position_stress_windows), sem a console_script/click_sequence
            automatizálás nem tud lefutni (nincs PID-ünk),
          - None, ha nem sikerült elindítani.
        console_script: opcionális (prompt, válasz) párlista (pl. Linpack menüjéhez) - ha
        meg van adva, egy háttérszálon _auto_answer_console navigálja végig a program
        konzolos menüjét (lásd ott, miért SendInput-tal, nem stdin-átirányítással).
        click_sequence: opcionális lista (pl. FurMark: ['GPU stress test', 'GO'] - a
        beállító-ablak gombja, majd a rákövetkező figyelmeztető dialógus gombja) - ha meg
        van adva, egy háttérszálon _auto_click_sequence sorban megkeresi és BM_CLICK
        üzenettel megnyomja az egymás után megjelenő ablakok/dialógusok gombjait. Egy
        lépés lehet egyetlen felirat vagy alternatívák listája (localizált szövegekhez).
        A 'runas' ágon is lefut, amíg van valódi PID-ünk (lásd fent) - csak a ritka
        handle-hiány esetén marad ki.
        thread_sink: opcionális lista - ha meg van adva, az elindított automatizálási
        háttérszál belekerül, hogy a hívó (start_stress_tests) bevárhassa a dialógus-
        nyomkodás végét, MIELŐTT az ablakokat rendezné/minimalizálná."""
        def _run_automation_safely(func, *args):
            # A háttérszál céljának védőrétege - ha bármi a try/except-eken KÍVÜL dobna
            # kivételt (pl. egy elgépelés egy jövőbeli módosításban), az itt látszódjon a
            # logban teljes traceback-kel, ne csendben tűnjön el egy daemon szálban.
            try:
                func(*args)
            except Exception as e:
                logging.error(f"[STRESSTOOLS-DEBUG] Automatizálási háttérszál ELSZÁLLT ({func.__name__}, args={args}): {e}")
                logging.error(traceback.format_exc())

        try:
            proc = subprocess.Popen([exe], creationflags=subprocess.CREATE_NEW_CONSOLE, cwd=os.path.dirname(exe))
            logging.info(f"[STRESSTOOLS] Elindítva: {display_name} ({exe}), pid={proc.pid}")
            auto_thread = None
            if console_script:
                auto_thread = threading.Thread(target=_run_automation_safely, args=(self._auto_answer_console, proc.pid, console_script), daemon=True, name=f"auto:{display_name}")
            elif click_sequence:
                auto_thread = threading.Thread(target=_run_automation_safely, args=(self._auto_click_sequence, proc.pid, click_sequence), daemon=True, name=f"auto:{display_name}")
            if auto_thread:
                auto_thread.start()
                if thread_sink is not None:
                    thread_sink.append(auto_thread)
            return proc.pid
        except OSError as e:
            if getattr(e, 'winerror', None) == 740:
                # ERROR_ELEVATION_REQUIRED - az exe manifestje requireAdministrator (pl.
                # HWiNFO64.exe), sima Popen nem tudja elindítani. ShellExecuteExW-et
                # SEE_MASK_NOCLOSEPROCESS maszkkal hívjuk (sima ShellExecuteW nem adna
                # vissza semmilyen handle-t/PID-et) - a kapott hProcess-ből GetProcessId-vel
                # kinyerjük a VALÓDI PID-et, hogy az indító-dialógus automatizálás (
                # console_script/click_sequence) ugyanúgy tudjon futni rá, mint egy nem
                # emelt szintű indításnál. Korábban itt sima ShellExecuteW volt és -1-et
                # adtunk vissza (nincs PID) - emiatt a HWiNFO-nál sosem indult el az
                # automata kattintás-szekvencia, a felhasználónak kézzel kellett nyomnia az
                # "Indítás" gombot.
                try:
                    sei = _SHELLEXECUTEINFOW()
                    sei.cbSize = ctypes.sizeof(_SHELLEXECUTEINFOW)
                    sei.fMask = SEE_MASK_NOCLOSEPROCESS
                    sei.hwnd = None
                    sei.lpVerb = "runas"
                    sei.lpFile = exe
                    sei.lpParameters = None
                    sei.lpDirectory = os.path.dirname(exe)
                    sei.nShow = SW_SHOWNORMAL
                    sei.hInstApp = None

                    ok = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei))
                    if not ok or not sei.hProcess:
                        logging.warning(f"[STRESSTOOLS] Elindítva (Admin): {display_name} ({exe}) - ShellExecuteExW nem adott vissza process handle-t (pl. a felhasználó elutasította a UAC-promptot vagy AppCompat-tükrözés zajlik), automatizálás kimarad.")
                        return -1

                    real_pid = ctypes.windll.kernel32.GetProcessId(sei.hProcess)
                    ctypes.windll.kernel32.CloseHandle(sei.hProcess)
                    logging.info(f"[STRESSTOOLS] Elindítva (Admin): {display_name} ({exe}), pid={real_pid}")

                    auto_thread = None
                    if console_script:
                        auto_thread = threading.Thread(target=_run_automation_safely, args=(self._auto_answer_console, real_pid, console_script), daemon=True, name=f"auto:{display_name}")
                    elif click_sequence:
                        auto_thread = threading.Thread(target=_run_automation_safely, args=(self._auto_click_sequence, real_pid, click_sequence), daemon=True, name=f"auto:{display_name}")
                    if auto_thread:
                        auto_thread.start()
                        if thread_sink is not None:
                            thread_sink.append(auto_thread)
                    return real_pid if real_pid else -1
                except Exception as e2:
                    logging.error(f"[STRESSTOOLS] Indítási hiba (Admin) - {display_name}: {e2}")
                    return None
            logging.error(f"[STRESSTOOLS] Indítási hiba - {display_name}: {e}")
            return None
        except Exception as e:
            logging.error(f"[STRESSTOOLS] Indítási hiba - {display_name}: {e}")
            return None

    def _download_stresstools(self):
        import tempfile, urllib.request, urllib.error, zipfile, ssl, shutil
        # WinPE-ben a %TEMP% az X: RAM-diskre mutat - a stressztesztek zip-jét a valódi C: meghajtóra tesszük.
        is_pe = os.environ.get('SystemDrive', 'C:') == 'X:'
        if is_pe:
            temp_dir = r'C:\DV_Temp'
            os.makedirs(temp_dir, exist_ok=True)
        else:
            temp_dir = tempfile.gettempdir()
        stress_dir = os.path.join(temp_dir, "DriverVarázsló_Stress")
        marker_path = os.path.join(stress_dir, ".extract_complete")
        zip_path = os.path.join(temp_dir, "stresstools.zip")
        download_url = "https://github.com/egonixaimgod/DriverVarazslo/releases/download/stresstools.zip/stresstools.zip"

        # Csak akkor fogadjuk el a cache-t, ha a kicsomagolás korábban teljesen lefutott ÉS
        # a SumatraPDF, ÉS a HP driver is megvan benne. Ez utóbbi két feltétel azért kell,
        # mert mindkettőt UTÓLAG adtuk a stresstools.zip-hez (print_via_store_printer
        # miatt) - egy olyan gépen, ahol a stressz-teszt funkciót MÁR HASZNÁLTÁK a
        # frissítés(ek) előtt, a marker fájl egy régebbi, hiányos ZIP-ből származik, és
        # enélkül a plusz feltétel nélkül a sima marker-ellenőrzés örökre a régi cache-t
        # adná vissza - a friss ZIP-et sosem töltené le újra (terepen bizonyítottan
        # előfordul: ezen a gépen is, illetve egy random teszt-gépen is).
        if os.path.exists(marker_path) and self._find_sumatra_exe(stress_dir) and self._find_hp_driver_inf(stress_dir):
            return stress_dir

        # A "Minden teszt indítása" és az egyenkénti gombok is idekerülhetnek egyszerre
        # (utóbbiak nem mennek át a _task_busy-n, hogy egymás után gyorsan lehessen indítani
        # több eszközt is) - lock nélkül két egyidejű hívás ugyanabba a zip_path/stress_dir
        # mappába írna/csomagolna ki párhuzamosan, ami korrupciót okozhatna.
        with self._stresstools_download_lock:
            # Amíg a lock-ra vártunk, egy másik szál esetleg már befejezte a letöltést.
            if os.path.exists(marker_path) and self._find_sumatra_exe(stress_dir) and self._find_hp_driver_inf(stress_dir):
                return stress_dir
            try:
                logging.info("[STRESSTOOLS] Letöltés INNEN: " + download_url)
                ssl_ctx = ssl.create_default_context()

                req = urllib.request.Request(download_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
                try:
                    with urllib.request.urlopen(req, context=ssl_ctx, timeout=60) as resp, open(zip_path, 'wb') as f:
                        shutil.copyfileobj(resp, f)
                except urllib.error.URLError as dl_err:
                    # Vadonatúj Windows-telepítésen a gyökértanúsítvány-tár még hiányos: a
                    # Windows a gyökereket igény szerint tölti le, de ezt csak a schannel-
                    # alapú kliensek (böngésző, PowerShell, .NET) váltják ki - a Python
                    # OpenSSL-je nem, ezért nála CERTIFICATE_VERIFY_FAILED lesz. Tipikus
                    # tünet: a github.com (Sectigo/USERTrust gyökér) elhasal, miközben a
                    # raw.githubusercontent.com (DigiCert gyökér) működik - ezért megy az
                    # update-ellenőrzés ugyanazon a friss gépen, amin ez a letöltés nem.
                    # Ilyenkor PowerShell Invoke-WebRequest-tel (schannel) töltünk le: a
                    # tanúsítvány-ellenőrzés ott is teljes értékű (SEMMIT nem kapcsolunk
                    # ki!), és mellékhatásként a hiányzó gyökér bekerül a Windows tárba,
                    # így a gép későbbi Python-letöltései is meggyógyulnak.
                    if 'CERTIFICATE_VERIFY_FAILED' not in str(dl_err):
                        raise
                    logging.warning(f"[STRESSTOOLS] Python SSL tanúsítvány-hiba ({dl_err}) - friss Windows tanúsítvány-tár gyanú, áttérés PowerShell (schannel) letöltésre, teljes tanúsítvány-ellenőrzéssel...")
                    ps_cmd = ("$ProgressPreference='SilentlyContinue'; "
                              "[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072; "
                              f"Invoke-WebRequest -Uri '{_ps_quote(download_url)}' -OutFile '{_ps_quote(zip_path)}' -UseBasicParsing")
                    result = self._run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps_cmd], timeout=600)
                    if not result or result.returncode != 0 or not os.path.exists(zip_path):
                        logging.error("[STRESSTOOLS] A PowerShell (schannel) letöltés is sikertelen.")
                        return None
                    logging.info("[STRESSTOOLS] PowerShell (schannel) letöltés sikeres.")

                if not zipfile.is_zipfile(zip_path):
                    return None
                if os.path.exists(stress_dir):
                    shutil.rmtree(stress_dir, ignore_errors=True)
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(stress_dir)
                try: os.remove(zip_path)
                except Exception as e: logging.debug(f"[STRESSTOOLS] Letöltött ZIP törlése sikertelen: {e}")
                with open(marker_path, 'w') as f:
                    f.write('ok')
                return stress_dir
            except Exception as e:
                logging.error(f"[STRESSTOOLS] Download hiba: {e}")
                return None

    def start_stress_tests(self):
        logging.info("[API] start_stress_tests()")

        def worker():
            try:
                self.emit('task_start', {'task': 'stress', 'title': 'Stabilitás Teszt Indítása'})
                self._lock_power_for_stress()
                self.emit('task_progress', {'task': 'stress', 'log': '🌐 Tesztprogramok (ZIP) letöltése a háttérben...', 'indeterminate': True})

                stress_dir = self._download_stresstools()
                if not stress_dir:
                    raise Exception("Hiba a ZIP letöltésekor vagy kicsomagolásakor (Helytelen ZIP / Nincs net).")

                self.emit('task_progress', {'task': 'stress', 'log': '🔥 Programok rászabadítása a gépre...'})

                # Csak a STRESS_TOOLS_BULK-ban felsorolt (valódi terhelés-generáló) programok -
                # a HD Sentinel monitor kifejezett kérésre nincs benne a tömeges indításban.
                found = self._find_stress_tool_exes(stress_dir, STRESS_TOOLS_BULK)

                launched = 0
                pid_map = {}
                auto_threads = []
                for i, key in enumerate(STRESS_TOOLS_BULK):
                    display_name, _ = STRESS_TOOLS[key]
                    exe = found[key]
                    if exe and os.path.exists(exe):
                        if key == 'hwinfo':
                            try:
                                # CheckForUpdate=0: az indítás után felugró "HWiNFO Update"
                                # értesítő letiltása (ha a kulcsot nem venné figyelembe, a
                                # kattintás-szekvencia opcionális Bezárás-lépése a háló).
                                ini_path = os.path.join(os.path.dirname(exe), "HWiNFO64.INI")
                                with open(ini_path, "w") as f:
                                    f.write("[Settings]\nSensorsOnly=1\nCheckForUpdate=0\n")
                            except Exception as e:
                                logging.debug(f"[STRESSTOOLS] HWiNFO64.INI írása sikertelen (az update-értesítőt a kattintás-szekvencia kezeli): {e}")
                        console_script = self._build_linpack_console_script() if key == 'linpack' else None
                        click_sequence = STRESS_CLICK_SEQUENCES.get(key)
                        pid = self._launch_stress_exe(exe, display_name, console_script=console_script, click_sequence=click_sequence, thread_sink=auto_threads)
                        if pid:
                            launched += 1
                            pid_map[key] = pid
                            self._stress_pids[key] = pid  # stop_stress_tests innen tudja, mit kell kilőni
                            self.emit('task_progress', {'task': 'stress', 'log': f'✅ Elindítva: {display_name}'})
                            if console_script:
                                self.emit('task_progress', {'task': 'stress', 'log': '  🤖 Linpack menü automatikus kitöltése elindult (RAM-választás + megerősítések).'})
                            if click_sequence:
                                self.emit('task_progress', {'task': 'stress', 'log': '  🤖 Indító dialógusok automatikus végignyomkodása elindult.'})
                        else:
                            self.emit('task_progress', {'task': 'stress', 'log': f'❌ Hiba indításnál: {display_name}'})
                    else:
                        self.emit('task_progress', {'task': 'stress', 'log': f'⚠️ Nem található a ZIP-ben: {display_name}'})
                    # Egymás után, ne egyszerre indítsuk a 4 programot - ha mind egy pillanatban
                    # próbál elindulni (GPU/CPU detektálás, ablak-létrehozás egyszerre), a gép
                    # erősen leterhelődhet, és ez akár fél perces késéseket okozhat a dialógusok
                    # megjelenésében (ezt debug logban is megfigyeltük).
                    if i < len(STRESS_TOOLS_BULK) - 1:
                        time.sleep(3)

                # Az automatizálási háttérszálak (dialógus-nyomkodás, Linpack menü-kitöltés)
                # VÉGÉT várjuk meg, korábban itt fix 30 mp várakozás volt - az terepen az
                # automatizálás közepén sütött el: a _minimize_other_windows pont a még meg
                # nem válaszolt dialógusokat tette tálcára, a FurMark render-ablaka pedig még
                # nem is létezett, amikor a pozicionálás lefutott. A plafon (240 mp) csak
                # végszükség-fék: normál esetben a szálak pár tíz mp alatt végeznek, egy
                # elakadt lépés pedig a saját 60 mp-es timeoutja után magától feladja.
                if launched > 0:
                    self.emit('task_progress', {'task': 'stress', 'log': '\n⏳ Várakozás, amíg az indító dialógusok automatikus végignyomkodása befejeződik...'})
                    join_deadline = time.time() + 240
                    waited = 0
                    while time.time() < join_deadline and any(t.is_alive() for t in auto_threads):
                        if self._check_cancel():
                            break
                        time.sleep(1)
                        waited += 1
                        if waited % 15 == 0:
                            still_running = [t.name.replace('auto:', '') for t in auto_threads if t.is_alive()]
                            self.emit('task_progress', {'task': 'stress', 'log': f'  ⏳ Még folyamatban: {", ".join(still_running)}...'})
                    # Rövid türelmi idő az UTOLSÓ kattintás után létrejövő végleges ablakoknak
                    # (pl. a FurMark render-ablaka a GO megnyomása után pár mp-cel jelenik
                    # meg). Rövid lehet: az automatizálási szálak a saját utolsó kattintásuk
                    # HATÁSÁT is bevárják (_verify_final_click), tehát mire ideérünk, a
                    # dialógusok bizonyítottan bezárultak - ez csak a fő ablakok megjelenési
                    # ideje, a felhasználói elvárás pedig az, hogy a nyomkodás után AZONNAL
                    # jöjjön a rendezés.
                    for _ in range(3):
                        if self._check_cancel():
                            break
                        time.sleep(1)
                    if self._check_cancel():
                        self.emit('task_progress', {'task': 'stress', 'log': '❗ Ablak-elrendezés kihagyva (megszakítva).'})
                    else:
                        self.emit('task_progress', {'task': 'stress', 'log': '🪟 Ablakok elrendezése...'})
                        self._position_stress_windows(pid_map, task_id='stress')

                if launched == len(STRESS_TOOLS_BULK):
                     self.emit('task_complete', {'task': 'stress', 'status': '👀 Minden teszt elindult. Égjen!'})
                elif launched > 0:
                     self.emit('task_complete', {'task': 'stress', 'status': f'⚠️ Csak {launched}/{len(STRESS_TOOLS_BULK)} program indult el.'})
                else:
                     self.emit('task_complete', {'task': 'stress', 'status': '❌ Egyetlen program sem indult el.'})

            except Exception as e:
                logging.error(f"Stressz teszt hiba: {e}")
                self.emit('task_error', {'task': 'stress', 'error': f'Hiba: {str(e)}'})

        self._safe_thread('stress', worker)

    def start_stress_tool(self, name):
        """Egyetlen stabilitás-teszt/monitor program elindítása (a Stabilitás Teszt nézet
        5 kis ikonja hívja). Tudatosan NEM megy át a task_start/progress-modal rendszeren -
        a felhasználó kifejezett kérése, hogy egy gombnyomásra csendben, ablak/dialógus
        nélkül induljon el a program, csak egy rövid toast-tal tájékoztatva."""
        logging.info(f"[API] start_stress_tool({name})")
        info = STRESS_TOOLS.get(name)
        if not info:
            self.emit('toast', {'message': f'❌ Ismeretlen program: {name}', 'type': 'error'})
            return
        display_name, _ = info

        def worker():
            import tempfile
            try:
                self._lock_power_for_stress()

                is_pe = os.environ.get('SystemDrive', 'C:') == 'X:'
                temp_dir = r'C:\DV_Temp' if is_pe else tempfile.gettempdir()
                marker_path = os.path.join(temp_dir, "DriverVarázsló_Stress", ".extract_complete")
                if not os.path.exists(marker_path):
                    self.emit('toast', {'message': f'⏳ {display_name}: első indítás, tesztprogramok letöltése (eltarthat egy percig)...', 'type': 'info'})

                stress_dir = self._download_stresstools()
                if not stress_dir:
                    self.emit('toast', {'message': f'❌ Hiba a tesztprogramok letöltésekor/kicsomagolásakor ({display_name})!', 'type': 'error'})
                    return

                exe_path = self._find_stress_tool_exes(stress_dir, [name])[name]

                if not exe_path or not os.path.exists(exe_path):
                    self.emit('toast', {'message': f'⚠️ {display_name} nem található a letöltött csomagban!', 'type': 'warning'})
                    return

                if name == 'hwinfo':
                    try:
                        # CheckForUpdate=0 - lásd a start_stress_tests azonos sorát.
                        ini_path = os.path.join(os.path.dirname(exe_path), "HWiNFO64.INI")
                        with open(ini_path, "w") as f:
                            f.write("[Settings]\nSensorsOnly=1\nCheckForUpdate=0\n")
                    except Exception as e:
                        logging.debug(f"[STRESSTOOLS] HWiNFO64.INI írása sikertelen (az update-értesítőt a kattintás-szekvencia kezeli): {e}")

                console_script = self._build_linpack_console_script() if name == 'linpack' else None
                click_sequence = STRESS_CLICK_SEQUENCES.get(name)
                pid = self._launch_stress_exe(exe_path, display_name, console_script=console_script, click_sequence=click_sequence)
                if pid:
                    if pid > 0:
                        self._stress_pids[name] = pid  # stop_stress_tests innen tudja, mit kell kilőni
                    self.emit('toast', {'message': f'✅ {display_name} elindítva!', 'type': 'success'})
                else:
                    self.emit('toast', {'message': f'❌ Hiba a(z) {display_name} indításakor!', 'type': 'error'})
            except Exception as e:
                logging.error(f"[STRESSTOOLS] start_stress_tool hiba ({name}): {e}")
                self.emit('toast', {'message': f'❌ Hiba: {e}', 'type': 'error'})

        threading.Thread(target=worker, daemon=True, name="stress-tool").start()

    def stop_stress_tests(self):
        """Az ÖSSZES futó stressz-teszt/monitor program azonnali bezárása (a Stabilitás
        Teszt nézet és a stressz-folyamat modal piros gombja hívja). Két rétegben öl:
        (1) az általunk indított, eltárolt PID-ek teljes folyamatfája (taskkill /T - ez a
        Linpack cmd+linpack_engine gyerekeit is elviszi), (2) biztonsági hálóként a jól
        ismert programnevek szerint is (STRESS_KILL_IMAGES - pl. UAC 'runas' úton indított
        példány, ahol nincs PID-ünk). Végül visszaállítja a stressz-teszt által letiltott
        energiagazdálkodási beállításokat (képernyő-kikapcsolás/alvás), hiszen a tesztnek
        vége. Szándékosan nem megy át a _task_busy kapun: egy még futó (pl. ablakrendezésre
        váró) stressz-task mellett is azonnal működnie kell."""
        logging.info("[API] stop_stress_tests()")

        def worker():
            try:
                for key, pid in list(self._stress_pids.items()):
                    if pid and pid > 0:
                        self._run(['taskkill', '/PID', str(pid), '/T', '/F'])
                self._stress_pids = {}
                # Egyetlen taskkill hívás az összes ismert programnévre (a taskkill több
                # /IM kapcsolót is elfogad) - a "nem fut ilyen" esetek várható, ártalmatlan
                # hibakódot adnak, ezért nem egyenként hívjuk és nem is ellenőrizzük.
                image_args = []
                for image in STRESS_KILL_IMAGES:
                    image_args += ['/IM', image]
                self._run(['taskkill', '/F', '/T'] + image_args)
                try:
                    self._restore_power_after_stress()
                except Exception as e:
                    logging.warning(f"[STRESSTOOLS] Energiagazdálkodás visszaállítási hiba a leállítás után: {e}")
                logging.info("[STRESSTOOLS] Minden stressz-teszt program bezárva (stop_stress_tests).")
                self.emit('toast', {'message': '🛑 Minden stressz-teszt program bezárva.', 'type': 'success'})
            except Exception as e:
                logging.error(f"[STRESSTOOLS] stop_stress_tests hiba: {e}")
                self.emit('toast', {'message': f'❌ Hiba a tesztek bezárásakor: {e}', 'type': 'error'})

        threading.Thread(target=worker, daemon=True, name="stress-stop").start()
