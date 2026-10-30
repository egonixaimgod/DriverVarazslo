"""DriverVarázsló GUI - Stress Teszt & Szervíz Programok: eszközök letöltése/indítása/leállítása,
energiagazdálkodás-zár. (A telepítés-funkció külön fájlban: app/gui/toolsinstall.py.)"""

# === AUTO-IMPORTS ===
import ctypes
import ctypes.wintypes
import fnmatch
import os
import subprocess
import threading
import time
import logging
import traceback
import winreg
import math
from app.common import _app_data_dir
from app.common import download_with_cert_fallback
from app import benchmark_core, logupload_core, toollib_core
from app.powerplan_core import parse_setting_indexes
from app.stress_defs import LINPACK_PROMPT_SCRIPT
from app.stress_defs import LINPACK_RAM_OPTIONS
from app.stress_defs import LINPACK_WMIC_SHIM
from app.stress_defs import LINPACK_WMIC_SHIM_NAME
from app.stress_defs import STRESS_CLICK_SEQUENCES
from app.stress_defs import STRESS_KILL_IMAGES
from app.stress_defs import STRESS_POWER_LOCK_KEYS
from app.stress_defs import STRESS_POWER_REG_KEY
from app.stress_defs import STRESS_POWER_SETTINGS
from app.stress_defs import STRESS_TOOLS
from app.stress_defs import STRESS_TOOLS_BULK
from app.stress_defs import STRESS_TOOL_ARGS
from app.stress_defs import STRESS_FURMARK_LABEL
from app.win32 import SEE_MASK_NOCLOSEPROCESS
from app.win32 import SW_SHOWNORMAL
from app.win32 import _MEMORYSTATUSEX
from app.win32 import _SHELLEXECUTEINFOW
# === /AUTO-IMPORTS ===


class GuiStressMixin:
    """Stress Teszt & Szervíz Programok: eszközök letöltése/indítása/leállítása, energiagazdálkodás-zár. A DriverToolApi része (összerakás: app/gui/api.py)."""

    # ================================================================
    # STABILITÁS TESZT - energiagazdálkodás (képernyő/alvó mód letiltása közben)
    # ================================================================
    def _query_power_setting(self, subgroup, setting):
        """(ac_másodperc, dc_másodperc) lekérdezése egy powercfg alias-párra.

        A SUB_VIDEO/VIDEOIDLE stb. alias-kulcsszavak nyelvfüggetlenek, de a `powercfg
        /query` kimenetének felirat-szövegei lokalizáltak lehetnek - ezért nem szöveges
        mintára illesztünk, hanem a hexa értékeket POZÍCIÓ szerint vesszük: az UTOLSÓ kettő
        az AC és a DC (`powerplan_core.parse_setting_indexes`, közös).

        2026-10-12-ig az ELSŐ kettőt vettük - az viszont a "Minimum/Maximum Possible
        Setting" (élőben mérve: 0x00000000 és 0xffffffff), így egy stressz-teszt után a
        "visszaállítás" AC=0-t (soha) és DC=0xffffffff-et írt: a kijelző nem kapcsolt ki,
        a gép nem aludt el, tartósan."""
        try:
            res = self._run(['powercfg', '/query', 'SCHEME_CURRENT', subgroup, setting])
            ac, dc = parse_setting_indexes(res.stdout or '')
            if ac is not None:
                return ac, dc
            logging.warning(f"[STRESS_POWER] A(z) {subgroup}/{setting} értéke nem olvasható ki - nem mentjük.")
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
                if (ac, dc) == (0, 0xFFFFFFFF):
                    # Egy 2026-10-12 előtti build HIBÁS mentése (a "Minimum/Maximum Possible
                    # Setting" került el AC/DC helyett, lásd _query_power_setting) - ezt nem írjuk
                    # vissza, a jelenlegi érték marad.
                    logging.warning(f"[STRESS_POWER] A(z) {subgroup}/{setting} mentett értéke egy régi build "
                                    f"hibás mentése (0 / 0xFFFFFFFF) - nem állítjuk vissza, kézzel ellenőrizendő "
                                    f"(Energiagazdálkodási beállítások).")
                    continue
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
        bejegyzést választjuk ki az összes ténylegesen megtalált jelölt közül.
        Egy filenames-bejegyzés lehet pontos fájlnév vagy fnmatch-minta ('*'/'?') -
        utóbbi a verziószámot a nevükben hordozó exe-khez kell (pl. GPU-Z.2.68.0.exe)."""
        candidates = {key: {} for key in keys}
        for root, dirs, files in os.walk(stress_dir):
            for file in files:
                fl = file.lower()
                for key in keys:
                    for idx, pattern in enumerate(STRESS_TOOLS[key][1]):
                        if '*' in pattern or '?' in pattern:
                            matched = fnmatch.fnmatch(fl, pattern)
                        else:
                            matched = (fl == pattern)
                        if matched and idx not in candidates[key]:
                            candidates[key][idx] = os.path.join(root, file)

        found = {}
        for key in keys:
            found[key] = candidates[key][min(candidates[key])] if candidates[key] else None
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

    def _prepare_linpack_wmic(self, exe):
        """A Linpack mappájába írja a WMIC-pótlékot (LINPACK_WMIC_SHIM, lásd ott, miért), és
        visszaadja a Linpack folyamat környezetét: a mappája a PATH ELEJÉN. Csak a munkamappára
        nem építhetünk: ha a NoDefaultCurrentDirectoryInExePath be van állítva, a cmd nem keres
        ott (mérve). Minden indításkor újraírja, hogy egy régebbi változat ne maradjon ott.
        None = a pótlék nem írható ki, az örökölt környezet marad."""
        folder = os.path.dirname(exe)
        path = os.path.join(folder, LINPACK_WMIC_SHIM_NAME)
        real = os.path.exists(os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'wbem', 'WMIC.exe'))
        try:
            with open(path, 'w', encoding='ascii', newline='\r\n') as f:
                f.write(LINPACK_WMIC_SHIM)
        except Exception as e:
            logging.warning(f"[STRESSTOOLS] A Linpack WMIC-pótléka nem írható ki ({path}): {e} - "
                            f"a rendszer WMIC-je {'megvan' if real else 'HIÁNYZIK, a Linpack a saját DISM-telepítésébe kezd'}.")
            return None
        logging.info(f"[STRESSTOOLS] Linpack WMIC-pótlék kiírva: {path}, a mappa a Linpack PATH-jának elején "
                     f"(a rendszer saját WMIC-je {'megvan' if real else 'HIÁNYZIK'}; a Linpack mindkét esetben a pótlékot használja).")
        return dict(os.environ, PATH=folder + os.pathsep + os.environ.get('PATH', ''))

    def _launch_stress_exe(self, exe, display_name, console_script=None, click_sequence=None, thread_sink=None,
                           result_sink=None, result_key=None, args=None):
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
        nyomkodás végét, MIELŐTT az ablakokat rendezné/minimalizálná.
        result_sink/result_key: opcionális dict + kulcs - az automatizálási szál
        befejezésekor result_sink[result_key] = True/False kerül bele (végigment-e a
        teljes kattintás-/gépelés-sorozat), ebből tudja a start_stress_tests a záró
        összegzésben kimutatni, melyik programnál kellhet kézi beállítás.
        args: opcionális parancssori kapcsolók listája (STRESS_TOOL_ARGS - jelenleg a
        FurMark 4K/8x MSAA/Xtreme burn-in indítása a tömeges úton). CSAK a tömeges indítás
        adja meg; az egyenkénti indítás szándékosan kapcsoló nélkül futtat, hogy a
        felhasználó maga állíthasson be mindent."""
        def _run_automation_safely(func, *args):
            # A háttérszál céljának védőrétege - ha bármi a try/except-eken KÍVÜL dobna
            # kivételt (pl. egy elgépelés egy jövőbeli módosításban), az itt látszódjon a
            # logban teljes traceback-kel, ne csendben tűnjön el egy daemon szálban.
            ok = False
            try:
                ok = bool(func(*args))
            except Exception as e:
                logging.error(f"[STRESSTOOLS-DEBUG] Automatizálási háttérszál ELSZÁLLT ({func.__name__}, args={args}): {e}")
                logging.error(traceback.format_exc())
            finally:
                if result_sink is not None and result_key is not None:
                    result_sink[result_key] = ok

        # A lenti 'runas'-ág (740) ezt az env-et nem kapja meg; a program emelt jogon fut, így a Linpack a Popen-ágon indul.
        env = self._prepare_linpack_wmic(exe) if os.path.basename(exe).lower() in STRESS_TOOLS['linpack'][1] else None
        cmd = [exe] + list(args or [])
        try:
            proc = subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_CONSOLE, cwd=os.path.dirname(exe), env=env)
            logging.info(f"[STRESSTOOLS] Elindítva: {display_name} ({subprocess.list2cmdline(cmd)}), pid={proc.pid}")
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
                    # A parancssori kapcsolók az emelt ágon is kellenek, különben a FurMark
                    # itt beállítások nélkül, a saját alapértelmezésével indulna el.
                    sei.lpParameters = subprocess.list2cmdline(list(args)) if args else None
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

    def _download_stresstools(self, progress=None, keys=(), password=''):
        """A kért programok csomagjainak letöltése + kicsomagolása a közös gyökér alá
        (`toollib_core.cache_root`), csomagonként cache-elve. Visszatér a gyökérrel (a
        fájlkeresők - `_find_stress_tool_exes`, `find_bench_tool_exes`, `find_smartctl`,
        `_find_sumatra_exe` - ezt járják be), vagy None-nal, ha egy kért csomag nem jött le.

        2026-10-30 óta CSOMAGONKÉNT (explicit user decision): a régi 621 MB-os
        stresstools.zip helyett minden program saját zipje; `keys` a program-kulcsok
        (lásd `toollib_core.packages_for`). `password`: a Drive-on lévő (licences)
        csomagokhoz kell, a naplók letöltésével azonos jelszó; üresen a gépen megjegyzett
        jelszó megy, és egy beírt jelszót a sikeres Drive-letöltés után megjegyzünk.

        progress: opcionális callback(fázis, kész, összes) - fázis 'download' (bájtok, az
        összes lehet None) vagy 'extract' (fájldarab). A hívó dolga a megjelenítés. Az
        utolsó hiba oka a `self._toollib_last_error`-ban (a hívó ebből ír üzenetet)."""
        import zipfile, shutil
        root = toollib_core.cache_root()
        pkgs = toollib_core.packages_for(keys)
        self._toollib_last_error = ''
        if not pkgs:
            self._toollib_last_error = 'nincs megadva letöltendő program'
            logging.error(f"[TOOLLIB] _download_stresstools: nincs csomag a kulcsokhoz ({keys!r})")
            return None
        missing = [p for p in pkgs if not toollib_core.package_ready(root, p)]
        if not missing:
            return root
        # Egyszerre több út is idekerülhet (egyenkénti indítás, telepítés): a lock nélkül
        # két szál ugyanabba a mappába csomagolna ki párhuzamosan.
        with self._stresstools_download_lock:
            self._remove_legacy_stresstools()
            os.makedirs(root, exist_ok=True)
            for pkg in missing:
                if toollib_core.package_ready(root, pkg):
                    continue   # amíg a lockra vártunk, egy másik szál letöltötte
                zip_path = os.path.join(os.path.dirname(root), 'DV_' + toollib_core.zip_name(pkg))
                dest = toollib_core.package_dir(root, pkg)
                try:
                    if toollib_core.package_source(pkg) == toollib_core.SOURCE_DRIVE:
                        url = benchmark_core.resolve_endpoint()
                        # Ha most nem írtak be jelszót, a gépen megjegyzett jelszó megy.
                        fid, err = toollib_core.drive_file_id(
                            lambda payload: logupload_core._post(self._run, url, payload),
                            password or toollib_core.load_password(), pkg)
                        if err:
                            raise Exception(f"Drive: {err}")
                        url = toollib_core.drive_url(fid)
                    else:
                        url = toollib_core.github_url(pkg)
                    logging.info(f"[TOOLLIB] Csomag letöltése: {pkg} ({toollib_core.package_source(pkg)}) -> {dest}")
                    dl_cb = (lambda done, total: progress('download', done, total)) if progress else None
                    download_with_cert_fallback(self._run, url, zip_path, timeout=60, ps_timeout=600,
                                                log_tag='TOOLLIB', progress_cb=dl_cb)
                    if not zipfile.is_zipfile(zip_path):
                        # A Drive megosztás nélkül egy HTML bejelentkező-oldalt ad 200-zal.
                        raise Exception("a letöltött fájl nem zip (Drive-on: a fájl nincs megosztva?)")
                    if os.path.exists(dest):
                        logging.info(f"[TOOLLIB] Régi/félbemaradt csomag-mappa törlése: {dest}")
                        shutil.rmtree(dest, ignore_errors=True)
                    with zipfile.ZipFile(zip_path, 'r') as zf:
                        members = zf.infolist()
                        for i, member in enumerate(members):
                            zf.extract(member, dest)
                            if progress:
                                try:
                                    progress('extract', i + 1, len(members))
                                except Exception as cb_err:
                                    logging.debug(f"[TOOLLIB] extract progress hiba (figyelmen kívül hagyva): {cb_err}")
                    toollib_core.mark_package_ready(root, pkg)
                    logging.info(f"[TOOLLIB] {pkg}: kész ({len(members)} fájl).")
                    if password and toollib_core.package_source(pkg) == toollib_core.SOURCE_DRIVE:
                        # A jelszó bizonyítottan jó (a Drive kiadta a fájlt): megjegyezzük.
                        toollib_core.save_password(password)
                except Exception as e:
                    self._toollib_last_error = f"{toollib_core.PACKAGES[pkg][1]}: {e}"
                    logging.error(f"[TOOLLIB] {pkg} letöltése/kicsomagolása nem sikerült: {e}")
                    return None
                finally:
                    # A zipet mindig töröljük - tele lemeznél (terepen látott [Errno 28]) a
                    # maradvány foglalná a helyet a következő próba elől.
                    try:
                        if os.path.exists(zip_path):
                            os.remove(zip_path)
                    except OSError as rm_err:
                        logging.warning(f"[TOOLLIB] A letöltött zip nem törölhető ({zip_path}): {rm_err}")
        return root

    def _remove_legacy_stresstools(self):
        """A régi, egy-zipes cache (`DriverVarázsló_Stress`, kicsomagolva ~1,3 GB) törlése:
        az új buildek már nem használják. Csak a saját régi mappánkat töröljük."""
        legacy = os.path.join(os.path.dirname(toollib_core.cache_root()), toollib_core.LEGACY_CACHE_DIRNAME)
        if os.path.isdir(legacy):
            logging.info(f"[TOOLLIB] A régi stresstools-cache törlése: {legacy}")
            shutil_errors = []
            import shutil
            shutil.rmtree(legacy, onerror=lambda fn, p, ei: shutil_errors.append(f"{p}: {ei[1]}"))
            if shutil_errors:
                logging.warning(f"[TOOLLIB] A régi cache egy része bent maradt ({len(shutil_errors)} fájl): {shutil_errors[:3]}")

    def _stress_dl_progress_emitter(self, label):
        """Throttlingolt progress-callback gyár a stresstools.zip letöltéséhez: a
        'stress_dl_progress' UI-eseményt legfeljebb ~0.3 mp-enként bocsátja ki (a
        letöltés 256 KB-onként hívna - az emit + [EMIT] lognál ez már churn lenne).
        A hívó felelőssége a végén az {'active': False} esemény kibocsátása (finally!)."""
        state = {'last': 0.0}

        def cb(phase, done, total):
            now = time.monotonic()
            is_final = bool(total) and done >= total
            if now - state['last'] < 0.3 and not is_final:
                return
            state['last'] = now
            if phase == 'download':
                if total:
                    self.emit('stress_dl_progress', {
                        'active': True, 'percent': int(done * 100 / total),
                        'label': f'🌐 {label}: csomag letöltése — {done / 1048576:.0f} / {total / 1048576:.0f} MB'})
                else:
                    self.emit('stress_dl_progress', {
                        'active': True, 'percent': None,
                        'label': f'🌐 {label}: csomag letöltése — {done / 1048576:.0f} MB'})
            else:
                self.emit('stress_dl_progress', {
                    'active': True, 'percent': int(done * 100 / total) if total else None,
                    'label': f'📦 {label}: kicsomagolás — {done}/{total} fájl'})
        return cb

    def _task_dl_progress(self, task):
        """A stresstools.zip letöltésének/kicsomagolásának folyamata a FOLYAMAT-ABLAK sávjába
        (task_progress), throttlingolva - a letöltés 256 KB-onként hív, emitenként [EMIT] log is
        születik. (A _stress_dl_progress_emitter a nézetbe ágyazott sávé, amit a modal eltakar.)"""
        dl_state = {'last': 0.0}

        def dl_progress(phase, done, total):
            now = time.monotonic()
            if now - dl_state['last'] < 0.4 and not (total and done >= total):
                return
            dl_state['last'] = now
            if phase == 'download':
                if total:
                    self.emit('task_progress', {'task': task, 'current': done, 'total': total,
                                                'status': f'🌐 Letöltés: {done / 1048576:.0f} / {total / 1048576:.0f} MB'})
                else:
                    self.emit('task_progress', {'task': task, 'indeterminate': True,
                                                'status': f'🌐 Letöltés: {done / 1048576:.0f} MB'})
            else:
                self.emit('task_progress', {'task': task, 'current': done, 'total': total,
                                            'status': f'📦 Kicsomagolás: {done}/{total} fájl'})
        return dl_progress

    def _ascii_tool_copy(self, exe):
        """A TestMem5 ANSI fájl-API-kkal dolgozik (CreateFileA, GetPrivateProfileStringA):
        ékezetes útvonalról ("...\\Temp\\DriverVarázsló_Stress\\...") nem találja a saját
        konfigját ("The configuration file specified on the command line was not found",
        terepen, Build 395). Ezért a mappáját a csupa ASCII <SystemDrive>\\DriverVarazslo alá
        másoljuk, és onnan indítjuk. ASCII útvonalnál (pl. telepített példány) nincs másolás."""
        if exe.isascii():
            return exe
        import shutil
        src = os.path.dirname(exe)
        dst = os.path.join(_app_data_dir(), os.path.basename(src))
        logging.info(f"[STRESSTOOLS] Nem ASCII útvonal, a program átmásolva: {src} -> {dst}")
        shutil.copytree(src, dst, dirs_exist_ok=True)
        return os.path.join(dst, os.path.basename(exe))

    def start_stress_tests(self):
        logging.info("[API] start_stress_tests()")

        def worker():
            try:
                self.emit('task_start', {'task': 'stress', 'title': 'CPU + Memória + GPU teszt'})
                self._lock_power_for_stress()
                self.emit('task_progress', {'task': 'stress', 'log': '🌐 Tesztprogramok (ZIP) letöltése a háttérben...', 'indeterminate': True})

                stress_dir = self._download_stresstools(progress=self._task_dl_progress('stress'), keys=STRESS_TOOLS_BULK)
                if not stress_dir:
                    raise Exception("Hiba a ZIP letöltésekor vagy kicsomagolásakor (Helytelen ZIP / Nincs net).")

                self.emit('task_progress', {'task': 'stress', 'log': '🔥 Programok rászabadítása a gépre...'})

                # Csak a STRESS_TOOLS_BULK-ban felsorolt (valódi terhelés-generáló) programok -
                # a HD Sentinel monitor kifejezett kérésre nincs benne a tömeges indításban.
                found = self._find_stress_tool_exes(stress_dir, STRESS_TOOLS_BULK)

                launched = 0
                pid_map = {}
                auto_threads = []
                auto_results = {}  # kulcs -> végigment-e az automatizálás (a záró összegzéshez)
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
                        # Parancssori kapcsolók CSAK a tömeges úton (STRESS_TOOL_ARGS) - jelenleg
                        # a FurMark indul velük 4K / 8x MSAA / Xtreme burn-in, időkorlát nélkül.
                        tool_args = STRESS_TOOL_ARGS.get(key)
                        pid = self._launch_stress_exe(exe, display_name, console_script=console_script, click_sequence=click_sequence,
                                                      thread_sink=auto_threads, result_sink=auto_results, result_key=key,
                                                      args=tool_args)
                        if pid:
                            launched += 1
                            pid_map[key] = pid
                            self._stress_pids[key] = pid  # stop_stress_tests innen tudja, mit kell kilőni
                            beall = f' ({STRESS_FURMARK_LABEL})' if key == 'furmark' else ''
                            self.emit('task_progress', {'task': 'stress', 'log': f'✅ Elindítva: {display_name}{beall}'})
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
                # nem is létezett, amikor a pozicionálás lefutott. A plafon (600 mp) csak
                # végszükség-fék, lassú (dual-core + HDD) gépekre méretezve: worst case a
                # Linpack minden promptja a STRESS_STEP_TIMEOUT-ig várhat; normál esetben a
                # szálak pár tíz mp alatt végeznek, egy elakadt lépés pedig a saját
                # timeoutja után magától feladja.
                still_running = []
                if launched > 0:
                    self.emit('task_progress', {'task': 'stress', 'log': '\n⏳ Várakozás, amíg az indító dialógusok automatikus végignyomkodása befejeződik...'})
                    join_deadline = time.monotonic() + 600
                    waited = 0
                    while time.monotonic() < join_deadline and any(t.is_alive() for t in auto_threads):
                        if self._check_cancel():
                            break
                        time.sleep(1)
                        waited += 1
                        if waited % 15 == 0:
                            running_names = [t.name.replace('auto:', '') for t in auto_threads if t.is_alive()]
                            self.emit('task_progress', {'task': 'stress', 'log': f'  ⏳ Még folyamatban: {", ".join(running_names)}...'})
                    still_running = [t.name.replace('auto:', '') for t in auto_threads if t.is_alive()]
                    if self._check_cancel():
                        self.emit('task_progress', {'task': 'stress', 'log': '❗ Ablak-elrendezés kihagyva (megszakítva).'})
                    elif still_running:
                        # Futó nyomkodás közben TILOS rendezni/minimalizálni: a fókusz-lopás
                        # pont a Linpack gépelését törné el, a minimalizálás pedig a még meg
                        # nem válaszolt dialógusokat tüntetné el (a régi fix-30 mp-es időzítő
                        # pont ezt a hibát okozta terepen). Ha a plafonig sem végeztek, a
                        # rendezés marad ki - az a kisebbik rossz.
                        self.emit('task_progress', {'task': 'stress', 'log': f'⚠️ Az automatikus nyomkodás nem fejeződött be nála: {", ".join(still_running)} - ablak-elrendezés kihagyva, nehogy a nyomkodás közben rendezzünk.'})
                    else:
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
                        self.emit('task_progress', {'task': 'stress', 'log': '🪟 Ablakok elrendezése...'})
                        self._position_stress_windows(pid_map, task_id='stress')

                # Záró összegzés: az elindult, de végig nem automatizált programokat név
                # szerint kimutatjuk - korábban a "Minden teszt elindult" csak a process-
                # indulást nézte, és egy dialógusán ottragadt program is sikernek látszott.
                auto_failed = [STRESS_TOOLS[k][0] for k in STRESS_TOOLS_BULK
                               if k in pid_map and not auto_results.get(k)]
                if launched == len(STRESS_TOOLS_BULK) and not auto_failed:
                     self.emit('task_complete', {'task': 'stress', 'status': '👀 Mind a 4 teszt elindult, az indító beállítások automatikusan kitöltve. Égjen!'})
                elif launched > 0:
                     status = f'⚠️ {launched}/{len(STRESS_TOOLS_BULK)} program indult el.'
                     if auto_failed:
                         status += f' Kézi beállítás kellhet: {", ".join(auto_failed)} (az automatikus nyomkodás nem futott végig - részletek a naplóban).'
                     self.emit('task_complete', {'task': 'stress', 'status': status})
                else:
                     self.emit('task_complete', {'task': 'stress', 'status': '❌ Egyetlen program sem indult el.'})

            except Exception as e:
                logging.error(f"Stressz teszt hiba: {e}")
                self.emit('task_error', {'task': 'stress', 'error': f'Hiba: {str(e)}'})

        self._safe_thread('stress', worker)

    def start_stress_tool(self, name, password=''):
        """Egyetlen szervíz-/teszt program elindítása (a Stress Teszt nézet program-
        kártyái hívják). Tudatosan NEM megy át a task_start/progress-modal rendszeren -
        egy gombnyomásra csendben induljon el, csak toast + (első letöltésnél) a nézetbe
        ágyazott 'stress_dl_progress' folyamat-sáv tájékoztat.

        Egyenkénti indításnál SZÁNDÉKOSAN NINCS SEMMILYEN AUTOMATIZÁLÁS (kifejezett
        felhasználói kérés, 2026-07): se dialógus-nyomkodás, se Linpack menü-kitöltés,
        se HWiNFO INI-írás, se ablak-pozicionálás - a program a saját beállító
        képernyőjével indul, mindent a felhasználó állít be. Az automatizált út KIZÁRÓLAG
        a start_stress_tests ("CPU + Memória + GPU teszt" (korábban "Stress teszt indítása") gomb). Ha a program korábban
        telepítésre került (install_service_tools), a telepített példány indul - így nem
        kell hozzá a temp-beli csomag (se letöltés); egyébként portable módban, a
        letöltött csomagból fut."""
        logging.info(f"[API] start_stress_tool({name})")
        info = STRESS_TOOLS.get(name)
        if not info:
            self.emit('toast', {'message': f'❌ Ismeretlen program: {name}', 'type': 'error'})
            return
        display_name, _ = info

        def worker():
            try:
                # Infó-eszközöknél (CPU-Z, GPU-Z...) nem nyúlunk az energiagazdálkodáshoz.
                if name in STRESS_POWER_LOCK_KEYS:
                    self._lock_power_for_stress()

                # Telepített példány előnyben: nincs letöltés, a Program Files alól indul.
                exe_path = self._find_installed_tool_exe(name)
                if exe_path:
                    logging.info(f"[STRESSTOOLS] {display_name}: telepített példány indul: {exe_path}")
                else:
                    pkg = toollib_core.package_for(name)
                    if pkg and not toollib_core.package_ready(toollib_core.cache_root(), pkg):
                        self.emit('toast', {'message': f'⏳ {display_name}: első indítás, a program letöltése következik...', 'type': 'info'})

                    try:
                        stress_dir = self._download_stresstools(progress=self._stress_dl_progress_emitter(display_name),
                                                                keys=[name], password=password)
                    finally:
                        self.emit('stress_dl_progress', {'active': False})
                    if not stress_dir:
                        self.emit('toast', {'message': f'❌ {display_name} letöltése nem sikerült: {self._toollib_last_error}', 'type': 'error'})
                        return

                    exe_path = self._find_stress_tool_exes(stress_dir, [name])[name]

                    if not exe_path or not os.path.exists(exe_path):
                        self.emit('toast', {'message': f'⚠️ {display_name} nem található a letöltött csomagban!', 'type': 'warning'})
                        return
                    if name == 'tm5':
                        exe_path = self._ascii_tool_copy(exe_path)

                    if name == 'hwinfo':
                        # Ha egy korábbi automatizált futás (start_stress_tests) beírta a
                        # SensorsOnly=1 INI-t a portable mappába, azt itt eltávolítjuk -
                        # egyenkénti indításnál a felhasználó maga választ módot. Csak a
                        # PONTOSAN általunk írt tartalmat töröljük, kézzel átírt INI-hez
                        # nem nyúlunk.
                        try:
                            ini_path = os.path.join(os.path.dirname(exe_path), "HWiNFO64.INI")
                            if os.path.exists(ini_path):
                                with open(ini_path, "r") as f:
                                    content = f.read()
                                if content == "[Settings]\nSensorsOnly=1\nCheckForUpdate=0\n":
                                    os.remove(ini_path)
                                    logging.info("[STRESSTOOLS] Automatizált futásból maradt HWiNFO64.INI eltávolítva (egyenkénti indítás, teljes kontroll a felhasználónál).")
                        except Exception as e:
                            logging.debug(f"[STRESSTOOLS] HWiNFO64.INI ellenőrzése/törlése sikertelen: {e}")

                pid = self._launch_stress_exe(exe_path, display_name)
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
        if self._task_busy == 'ramtest':
            # A RAM TEST láncnak is meg kell állnia, különben a következő lépést indítaná.
            logging.info("[STRESSTOOLS] Futó RAM TEST: megszakítás kérve a bezárással együtt.")
            self._cancel_flag = True

        def worker():
            try:
                # ok_codes=(0, 128): a 128 a taskkill "nincs ilyen futó folyamat" kódja -
                # teljesen várt eset (a felhasználó közben bezárta a programot, vagy egy
                # korábbi, már befejezett indításból - pl. a Benchmark nézet Heaven-je -
                # maradt itt a PID). E nélkül minden leállítás WARNING-okat szórt a logba,
                # amitől a valódi anomáliák nehezebben szúrnak szemet.
                for key, pid in list(self._stress_pids.items()):
                    if pid and pid > 0:
                        self._run(['taskkill', '/PID', str(pid), '/T', '/F'], ok_codes=(0, 128))
                self._stress_pids = {}
                # Egyetlen taskkill hívás az összes ismert programnévre (a taskkill több
                # /IM kapcsolót is elfogad) - a "nem fut ilyen" esetek várható, ártalmatlan
                # hibakódot adnak, ezért nem egyenként hívjuk és nem is ellenőrizzük.
                image_args = []
                for image in STRESS_KILL_IMAGES:
                    image_args += ['/IM', image]
                self._run(['taskkill', '/F', '/T'] + image_args, ok_codes=(0, 128))
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
