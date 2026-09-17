"""Közös alapok: WebView2 ellenőrzés, admin-check, útvonal-helperek, PowerShell-quote,
app-adatmappa, webview-állapot eventek, BUILD_NUMBER hordozó és a hívás-logolás."""

# === AUTO-IMPORTS ===
import ctypes
import ctypes.wintypes
import os
import subprocess
import sys
import threading
import time
import logging
import winreg
# === /AUTO-IMPORTS ===


# A BUILD_NUMBER "hordozója": az igazi értéket a driver_tool.py állítja be induláskor
# (common.BUILD_NUMBER = BUILD_NUMBER). A literál AZÉRT marad a driver_tool.py-ban, mert
# a kint lévő (régi) exe-k auto-updatere a GitHubról letöltött driver_tool.py-ból
# regexeli a ^BUILD_NUMBER\s*=\s*(\d+) sort - ha onnan kikerülne, minden régi
# felhasználó örökre a saját buildjén ragadna!
BUILD_NUMBER = 0

# A repo gyökere (app/ szülője) - forrásból futtatva ez a driver_tool.py mappája.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _app_exe_path():
    """A program futtatható fájljának teljes útvonala: fagyasztva (PyInstaller exe) a
    sys.executable, forrásból futtatva a repo-gyökér driver_tool.py. Szétbontás előtt ez
    az `os.path.abspath(__file__)` kifejezés volt - az a csomagolt app/ almodulra mutatna,
    ami pl. az AutoFix ütemezett-feladat útvonalát rontaná el forrásból futtatva."""
    if getattr(sys, 'frozen', False):
        return sys.executable
    return os.path.join(_PROJECT_ROOT, 'driver_tool.py')


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


def ensure_console():
    """Konzolablak biztosítása, és a sys.stdout/stderr/stdin ráirányítása.

    MIÉRT NEM ELÉG EGY sima print(): a program windowed (konzol nélküli) exe-ként épül
    (DriverVarazslo.spec: console=False), és PyInstaller alatt ilyenkor a `sys.stdout`
    ÉRTÉKE None - egy print() nem csak láthatatlan, hanem AttributeError-t dob. Ezért
    minden konzolra író ág (CLI-fallback, WebView2-telepítés folyamatjelzése) ezen a
    függvényen keresztül megy.

    Ha a konzol semmiképp nem nyitható (pl. szolgáltatás-környezet), a kimenetet a
    null-eszközre kötjük: a program ettől még fusson tovább - egy hiányzó folyamatjelző
    soha nem érhet annyit, mint maga a művelet.

    Visszatérés: True, ha van valódi (látható) konzol."""
    try:
        if not ctypes.windll.kernel32.GetConsoleWindow():
            ctypes.windll.kernel32.AllocConsole()
        if ctypes.windll.kernel32.GetConsoleWindow():
            # A HÁROM STREAMET MINDIG ÚJRAKÖTJÜK, nem csak ha None vagy zárt (2026-08-31).
            # A régi feltétel (`stream is None or closed`) azon bukott meg, hogy a
            # PyInstaller windowed bootloadere NEM mindig None-t ad: adhat egy nyitott, de
            # SEHOVA nem vezető objektumot is. Olyankor az újrakötés kimaradt, a program
            # vidáman "írt" - és a frissen nyitott konzolablak ÜRESEN, feketén állt (pont
            # ez volt a terepi tünet: a CLI elindult, a napló szerint futott, a képernyőn
            # semmi). A kötés önmagában olcsó és idempotens, tehát nincs értelme feltételhez
            # kötni; egy nem látszó kimenet mindig rosszabb, mint egy felesleges open().
            #
            # UTF-8 KÓDOLÁSSAL: az alapértelmezett locale a magyar Windowson cp852, amin a
            # keretrajzoló karakterek nincsenek meg. `errors='replace'`, hogy egy hiányzó
            # jel se dobhasson kivételt a kiírás közben.
            for name, mode, attr in (('CONIN$', 'r', 'stdin'), ('CONOUT$', 'w', 'stdout'),
                                     ('CONOUT$', 'w', 'stderr')):
                try:
                    setattr(sys, attr, open(name, mode, buffering=1,
                                            encoding='utf-8', errors='replace'))
                except OSError as e:
                    logging.debug(f"[CONSOLE] A(z) {attr} nem nyitható ({name}): {e}")
            # A konzol kódlapját is UTF-8-ra állítjuk (65001), különben a fenti utf-8
            # kódolású írás bájtjait a konzol cp852-ként rajzolná ki - ékezet-szemetet adva.
            try:
                ctypes.windll.kernel32.SetConsoleOutputCP(65001)
                ctypes.windll.kernel32.SetConsoleCP(65001)
            except Exception as e:
                logging.debug(f"[CONSOLE] A kódlap átállítása nem sikerült: {e}")
            return True
    except Exception as e:
        logging.debug(f"[CONSOLE] Konzol nyitása sikertelen: {e}")
    # Végső védőháló: legyen MIBE írni, különben a hívó print()-jei kivételt dobnának.
    for attr in ('stdout', 'stderr'):
        if getattr(sys, attr, None) is None:
            try:
                setattr(sys, attr, open(os.devnull, 'w'))
            except OSError:
                pass
    return False


# ============================================================================
# RÉGI WINDOWS (7/8/8.1) TÁMOGATÁS: .NET-ellenőrzés és induláskori diagnosztika
# ============================================================================
# A pywebview Windows-os GUI-ja pythonnet-en (clr) keresztül .NET Framework-öt tölt be.
# A pythonnet 3.x MINIMUM .NET Framework 4.7.2-t igényel - a Windows 8.1 viszont alapból
# 4.5.1-et hoz, a Windows 7 SP1 pedig 3.5.1-et. Ha hiányzik, a betöltés NEM Python-kivétel:
# a folyamat NATÍVAN esik szét, mielőtt bármilyen except ág lefutna. Terepen pontosan így
# nézett ki (2026-08-13, Win 8.1, Build 269): a napló utolsó sora
#     [MAIN] webview.start() hívása...
# és utána SEMMI - se traceback, se pywebview-hibaüzenet, se ablak, háromszor egymás után.
# Ezért ezt ELŐRE kell ellenőrizni: egy működő CLI + érthető üzenet mérhetetlenül többet ér,
# mint egy néma, nyom nélkül eltűnő program.
#
# A Release-számok a Microsoft hivatalos táblázatából valók (HKLM\...\NDP\v4\Full\Release).
DOTNET_RELEASE_MIN = 461808  # 4.7.2 - a pythonnet 3.x alsó határa
DOTNET_RELEASE_NAMES = (
    (533320, '4.8.1'), (528040, '4.8'), (461808, '4.7.2'), (461308, '4.7.1'),
    (460798, '4.7'), (394802, '4.6.2'), (394254, '4.6.1'), (393295, '4.6'),
    (379893, '4.5.2'), (378675, '4.5.1'), (378389, '4.5'),
)


def file_version(path):
    """Egy fájl (DLL/EXE) verziója 'a.b.c.d' alakban, vagy None, ha nincs/nem olvasható.

    Szándékosan ctypes-szal, nem PowerShell-hívással: ez induláskor fut, és egy
    subprocess ott fél-egy másodpercet vinne el minden indulásból.

    A VS_FIXEDFILEINFO struktúrát nem definiáljuk külön (app/win32.py), mert csak két
    mezőjére van szükség, és azok fix eltolásban vannak a struktúra elején:
    dwSignature(0), dwStrucVersion(4), dwFileVersionMS(8), dwFileVersionLS(12)."""
    import struct as _struct
    try:
        if not path or not os.path.exists(path):
            return None
        size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(path, 0, size, buf):
            return None
        ptr = ctypes.c_void_p()
        length = ctypes.c_uint()
        if not ctypes.windll.version.VerQueryValueW(buf, '\\', ctypes.byref(ptr), ctypes.byref(length)):
            return None
        if length.value < 16:
            return None
        data = ctypes.string_at(ptr, length.value)
        ms, ls = _struct.unpack_from('<II', data, 8)
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception as e:
        logging.debug(f"[DIAG] Fájlverzió nem olvasható ({path}): {e}")
        return None


def check_dotnet_framework():
    """A telepített .NET Framework 4.x állapota.

    Visszatérés: (elég_új_e, release_szám_vagy_None, ember-olvasható_verzió).
    Hiányzó kulcs esetén (Win7/8 alapállapot, ahol csak 3.5 van) (False, None, 'nincs 4.x')."""
    release = None
    for view in (winreg.KEY_WOW64_64KEY, 0):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full",
                                0, winreg.KEY_READ | view) as key:
                release, _ = winreg.QueryValueEx(key, "Release")
                if release:
                    break
        except (FileNotFoundError, OSError):
            continue
    if not release:
        return (False, None, 'nincs 4.x')
    name = next((n for r, n in DOTNET_RELEASE_NAMES if release >= r), f'ismeretlen ({release})')
    return (release >= DOTNET_RELEASE_MIN, release, name)


def windows_version():
    """A futó Windows (fő.al.build, ember-olvasható név). A build a döntő: a
    sys.getwindowsversion() a manifest miatt hazudhat a fő/alverzióban, a build nem.

    MIÉRT KELL: a naplóból eddig SEHOL nem derült ki, milyen Windowson futunk - egy
    Win7/8.1-es hibajelentésnél ez az első kérdés, és eddig csak találgatni lehetett."""
    try:
        v = sys.getwindowsversion()
        major, minor, build = v.major, v.minor, v.build
    except Exception:
        return (0, 0, 0, 'ismeretlen')
    names = {
        (6, 1): 'Windows 7', (6, 2): 'Windows 8', (6, 3): 'Windows 8.1',
    }
    if major == 10:
        name = 'Windows 11' if build >= 22000 else 'Windows 10'
    else:
        name = names.get((major, minor), f'Windows {major}.{minor}')
    return (major, minor, build, name)


def is_legacy_windows():
    """Igaz, ha a futó rendszer Windows 8.1 vagy régebbi (build < 10240). Ezeken a
    gépeken több olyan korlát van, amit a kódnak külön kezelnie kell (WebView2 max 109,
    pnputil régi szintaxisa, PowerShell 2.0-4.0)."""
    return windows_version()[2] < 10240


# MEGJEGYZÉS: az induláskori környezet-riport NEM itt van, hanem az app/prereq.py-ban
# (log_environment) - ott, ahol az előfeltételek felmérése és javítása is. Ez a modul csak
# a nyers ellenőrzőket adja (check_dotnet_framework, windows_version, file_version), hogy ne
# legyen két, majdnem azonos riport-implementáció - a duplikált logika, aminek az egyik
# példánya lemarad egy javításról, ennek a projektnek a legrégebbi visszatérő hibája.


# ============================================================================
# GUI-ÖSSZEOMLÁS ŐR: egy némán elszálló felület ne tudja HASZNÁLHATATLANNÁ tenni a programot
# ============================================================================
# A grafikus felület elindítása az egyetlen pont, ahol a program NATÍVAN össze tud omlani:
# a pywebview pythonnet-en át .NET-et tölt be, az pedig egy nem megfelelő környezetben
# (régi .NET, a runtime-nál újabb WebView2 SDK) nem kivételt dob, hanem megöli a folyamatot.
# Ilyenkor sem a sys.excepthook, sem a 60 mp-es webview-watchdog nem fut le - a felhasználó
# annyit lát, hogy "elindítom és nem történik semmi". Terepen bizonyított (2026-08-13,
# Win 8.1, Build 269): a napló utolsó sora háromszor egymás után a `webview.start() hívása...`.
#
# A megoldás nem az összeomlás megelőzése (azt nem tudjuk minden okra), hanem hogy CSAK
# EGYSZER fordulhasson elő: a kísérlet előtt jelzőfájlt írunk, sikeres indulásnál töröljük.
# Ha a következő induláskor a jelző még ott van, az előző kísérlet összeomlott -> egyből a
# (működő) CLI-be megyünk, érthető magyarázattal.
#
# A jelző a KÖRNYEZET ujjlenyomatát is tárolja (WebView2 + .NET verzió): ha a felhasználó
# telepít egy újabb .NET-et vagy WebView2-t, az ujjlenyomat megváltozik, és a program
# magától újra megpróbálja a felületet - különben egy megjavított gép is örökre CLI-ben
# ragadna. Kézi felülbírálás: --force-gui.
GUI_CRASH_MARKER_FILE = 'gui_indulas.json'


def _gui_marker_path():
    return os.path.join(_app_data_dir(), GUI_CRASH_MARKER_FILE)


def gui_env_signature(wv2_version=None, dotnet_release=None):
    """A GUI indulását meghatározó környezet ujjlenyomata (WebView2 + .NET verzió)."""
    return f"wv2={wv2_version or 'nincs'}|net={dotnet_release or 'nincs'}"


def gui_crash_check(signature):
    """Összeomlott-e az ELŐZŐ grafikus indítási kísérlet ugyanebben a környezetben?

    Visszatérés: (összeomlott_e, a_jelzőben_tárolt_ujjlenyomat_vagy_None)."""
    try:
        path = _gui_marker_path()
        if not os.path.exists(path):
            return (False, None)
        import json as _json
        with open(path, 'r', encoding='utf-8') as f:
            data = _json.load(f)
        stored = data.get('env')
        if stored != signature:
            logging.info(f"[GUI-ŐR] Van korábbi sikertelen indítás-jelző, de a környezet "
                         f"azóta megváltozott ({stored} -> {signature}) - újra megpróbáljuk a felületet.")
            return (False, stored)
        return (True, stored)
    except Exception as e:
        # Egy sérült/olvashatatlan jelző SOHA ne akadályozza a program indulását.
        logging.debug(f"[GUI-ŐR] A jelzőfájl nem olvasható: {e}")
        return (False, None)


def gui_attempt_begin(signature):
    """Jelzi, hogy MOST kezdődik egy grafikus indítási kísérlet. Ha a folyamat közben
    natívan elszáll, ez a fájl marad utána - ebből tudja a következő indulás, hogy nem
    szabad újra megpróbálni."""
    try:
        import json as _json
        with open(_gui_marker_path(), 'w', encoding='utf-8') as f:
            _json.dump({'env': signature, 'ts': time.strftime('%Y-%m-%d %H:%M:%S')}, f)
        logging.debug(f"[GUI-ŐR] Indítási kísérlet jelölve ({signature}).")
    except Exception as e:
        logging.debug(f"[GUI-ŐR] A jelzőfájl nem írható: {e}")


def gui_attempt_succeeded():
    """A felület elindult - a jelző törlése. A DOM elkészültekor hívjuk
    (app/gui/base.py: set_window), nem a program végén: az a pont bizonyítja, hogy a
    kockázatos natív szakasz (pythonnet + WebView2 betöltés) túl van."""
    try:
        path = _gui_marker_path()
        if os.path.exists(path):
            os.remove(path)
            logging.debug("[GUI-ŐR] A felület elindult - indítási jelző törölve.")
    except Exception as e:
        logging.debug(f"[GUI-ŐR] A jelzőfájl nem törölhető: {e}")


def show_webview2_error(message):
    """MessageBox megjelenítése WebView2 hibáról, majd program kilépés."""
    try:
        import webbrowser
        MB_ICONERROR = 0x10
        MB_TOPMOST = 0x40000
        result = ctypes.windll.user32.MessageBoxW(
            None,
            message + "\n\nMegnyissam a letöltési oldalt?",
            "DriverVarázsló - WebView2 hiba",
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
        base_path = _PROJECT_ROOT
    return os.path.join(base_path, relative_path)


def _ps_quote(value):
    """PowerShell egyszeres idézőjeles string escape: ' -> '' , hogy egy aposztrófot
    tartalmazó fájlútvonal (pl. C:\\Users\\O'Brien\\...) ne törje meg a generált parancsot."""
    return str(value).replace("'", "''")


# ============================================================================
# PARANCS-FUTTATÁS KÖZÖS KONSTANSAI (mindkét _run + a hívók használják)
# ============================================================================

# 0xC0000142 = STATUS_DLL_INIT_FAILED. Nem "a parancs hibázott", hanem "a folyamat EL SEM
# INDULT": a Windows session/asztal olyan állapotba került (leállás alatt, kimerült desktop
# heap, szétesett PnP/session), hogy új processzt már nem lehet inicializálni. Terepen
# bizonyított (Build 218, Dell OptiPlex): egy beragadt tárolóvezérlő-driver törlése után
# ELSŐKÉNT két pnputil hívás 140+ mp-ig lógott, majd onnantól MINDEN pnputil azonnal ezzel
# a kóddal tért vissza - a törlő ciklus így 17 csomagot "törölt" 0,0 mp alatt, majd sikert
# jelentett és rebootolt. Aki ilyet lát, NE folytassa a műveletet: a további hívások
# garantáltan no-opok, és a néma hamis siker rosszabb, mint a látható hiba.
STATUS_DLL_INIT_FAILED = 0xC0000142

# A _run időtúllépéskor ezzel a (szintetikus, Windowsban elő nem forduló) kóddal tér
# vissza, hogy a hívó meg tudja különböztetni a valódi hibakódoktól.
CMD_TIMEOUT_RETURNCODE = -9001


class CommandResult:
    """subprocess.CompletedProcess-kompatibilis minimál eredmény (returncode/stdout/stderr).
    Akkor adjuk vissza, ha nincs valódi CompletedProcess: időtúllépés vagy indítási kivétel."""

    def __init__(self, returncode, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def spawn_failed(result):
    """Igaz, ha a parancs el sem indult (STATUS_DLL_INIT_FAILED) - lásd a konstans
    kommentjét. Ilyenkor a hívónak MEG KELL ÁLLNIA, nem továbbmennie a következő elemre."""
    return getattr(result, 'returncode', None) == STATUS_DLL_INIT_FAILED


# ============================================================================
# POWERSHELL KIMENET KÓDOLÁSA - MINDEN -Command HÍVÁS ELÉ BEKERÜL (mindkét _run)
# ============================================================================
# TEREPEN BIZONYÍTVA (2026-08-07, bolti nyomtatás): a PowerShell 5.1 az átirányított
# (pipe-ra kötött) stdout-ra az OEM kódlappal ír - magyar Windowson cp852 -, MI viszont
# a hívások jó részét `encoding='utf-8'`-cal olvassuk. Az ékezetes karakterek így
# U+FFFD-re cserélődnek MÉG A MEMÓRIÁBAN, nem csak a logban:
#     Get-Printer ...  ->  'Microstore Bolti Nyomtat�'
# és ezzel a névvel a SumatraPDF `-print-to` már nem talál nyomtatót -> 1-es kilépési
# kód, semmi nem nyomtatódik ki, miközben a program sikert jelentett. Ez NEM logmegjelenítési
# kérdés volt (a napló bájtszinten U+FFFD-t tartalmaz), és nem egyedi hiba: minden ékezetet
# visszaadó PowerShell-hívást érint (nyomtatónevek, eszköznevek, SSID, felhasználónév).
#
# MÉRÉS (fejlesztőgép, CREATE_NO_WINDOW + capture_output, mint az appban):
#     előtag nélkül:  b'Nyomtat\xa2'      (cp852)  -> utf-8 dekódolva 'Nyomtat�'
#     előtaggal:      b'Nyomtat\xc3\xb3' (utf-8)  -> helyesen 'Nyomtató'
# A konzol kódlapjától (65001 vagy 852) FÜGGETLENÜL működik, mert a .NET Console
# kódolását állítja, nem a konzolét.
#
# Miért itt, a _run-ban, és nem a ~66 hívási helyen: a projekt egyik visszatérő hibaosztálya
# épp az, hogy a duplikált logika egyik példánya lemarad (lásd a Build 192-es WU-ügyet).
# Egy helyen javítva minden mostani ÉS jövőbeli hívás rendben van.
#
# BOM NÉLKÜLI UTF8Encoding kell: a `[System.Text.Encoding]::UTF8` BOM-os változat, és egy
# beszivárgó BOM a kimenet elején minden .strip()-alapú parse-olást elrontana (pl. egy
# INF-név elején lévő láthatatlan karakter).
PS_UTF8_PREFIX = "[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding $false; "


def ps_force_utf8(cmd):
    """PowerShell `-Command` hívás esetén az elé illeszti a kimenet-kódolás beállítását,
    és jelzi, hogy a kimenetet utf-8-ként kell dekódolni.

    Visszatérés: (cmd, kell_utf8_dekódolás). A parancsot csak akkor módosítja, ha
    tényleg egy powershell `-Command <script>` hívásról van szó - `-File`-t, más
    programokat (dism/pnputil/reg: azok kimenetét szándékosan NEM piszkáljuk, lásd a
    CLAUDE.md vonatkozó szabályát) és string-parancsokat érintetlenül hagy."""
    if not isinstance(cmd, (list, tuple)) or len(cmd) < 2:
        return cmd, False
    exe = os.path.basename(str(cmd[0])).lower()
    if exe not in ('powershell', 'powershell.exe', 'pwsh', 'pwsh.exe'):
        return cmd, False
    try:
        idx = next(i for i, a in enumerate(cmd)
                   if isinstance(a, str) and a.lower() in ('-command', '/command'))
    except StopIteration:
        return cmd, False
    if idx + 1 >= len(cmd) or not isinstance(cmd[idx + 1], str):
        return cmd, False
    script = cmd[idx + 1]
    if 'outputencoding' in script[:400].lower():
        # A script maga már beállítja (pl. _WIFI_DETECT_PS, a riport WMI-lekérdezése) -
        # ilyenkor csak a dekódolást jelezzük vissza, duplán nem állítjuk be.
        return cmd, True
    new_cmd = list(cmd)
    new_cmd[idx + 1] = PS_UTF8_PREFIX + script
    return new_cmd, True


def _app_data_dir():
    """A DriverVarázsló saját adatmappája (debug log, HTML rendszer riportok) - a
    rendszerlemez gyökerében, NEM a program (exe) mellett. Így mindig ugyanott van (a
    felhasználó/szerviz megszokhatja, hova nézzen), függetlenül attól, honnan futtatják
    épp az exe-t (Asztal, letöltések mappa, USB stick, hálózati megosztás - utóbbi kettő
    akár írásvédett is lehet, ahova maga az exe mellé semmiképp nem tudna írni)."""
    sys_drive = os.environ.get('SystemDrive', 'C:') + '\\'
    path = os.path.join(sys_drive, 'DriverVarazslo')
    try:
        os.makedirs(path, exist_ok=True)
    except Exception as e:
        # Nem logolható megbízhatóan: ez a függvény adja magát a log-mappát is,
        # a logging ilyenkor még nincs feltétlenül beállítva - stderr-re írunk.
        try:
            print(f"[APP_DATA] Mappa létrehozási hiba ({path}): {e}", file=sys.stderr)
        except Exception:
            pass
    return path


# A PowerShell (schannel) letöltési fallbackot kiváltó hibaszövegek. MIND TLS/tanúsítvány
# jellegű: ilyenkor nem a hálózat rossz, hanem a PYTHON SSL-verme nem tud megegyezni a
# túloldallal, miközben a rendszer sajátja (schannel) igen. Bármi más hiba (404, DNS,
# időtúllépés) továbbra is azonnal száll - azon a PS sem segítene, csak lassítana.
#
# CERTIFICATE_VERIFY_FAILED: az eredeti eset - vadonatúj Windows hiányos gyökértár-ral.
#
# UNEXPECTED_EOF_WHILE_READING / EOF occurred: TEREPEN MÉRVE (2026-08-13, Windows 8.1,
# Build 266) a WebView2 bootstrapper letöltésén:
#     <urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of
#      protocol (_ssl.c:1081)>
# Ez az OpenSSL 3.x (Python 3.10+) szigorítása: az 1.1.1 még elnézte, ha a túloldal
# "piszkosan" (close_notify nélkül) bontott, a 3.x viszont hibának veszi. Pont a RÉGI
# gépek úton lévő eszközei (régi proxy/TLS-terminátor, régi middlebox) csinálják ezt,
# vagyis ez a Win7/8/8.1-es gépek tipikus letöltési hibája - miközben ugyanaz az URL a
# rendszer schannel-vermén (PowerShell) simán lejön.
#
# A többi tag a TLS-egyeztetés klasszikus bukásai (verzió/alert/handshake): ugyanaz az
# ok-osztály, ugyanaz a helyes válasz.
DOWNLOAD_PS_FALLBACK_ERRORS = (
    'CERTIFICATE_VERIFY_FAILED',
    'UNEXPECTED_EOF_WHILE_READING',
    'EOF occurred in violation of protocol',
    'WRONG_VERSION_NUMBER',
    'SSLV3_ALERT',
    'TLSV1_ALERT',
    'SSLV3_ALERT_HANDSHAKE_FAILURE',
    'handshake failure',
    'SSLError',
    'SSL:',
)


def _should_try_ps_download(err):
    """Igaz, ha a Python-oldali letöltés olyan TLS/tanúsítvány-hibába futott, amire a
    PowerShell (schannel) fallbacknek van esélye. Kis/nagybetű-független, mert a
    hibaszövegek forrása (OpenSSL, urllib, ssl) nem egységes."""
    text = str(err).lower()
    return any(marker.lower() in text for marker in DOWNLOAD_PS_FALLBACK_ERRORS)


def default_run(cmd, **kwargs):
    """Minimál parancsfuttató azoknak a hívásoknak, ahol nincs kéznél API-példány (és így
    annak `_run` metódusa sem): az auto-updater (app/update_core.py - modul-szintű
    függvények) és a belépési pont WebView2-telepítője.

    Ugyanaz a három lényegi beállítás, mint a két nagy `_run`-ban: rejtett ablak,
    elkapott kimenet, DEVNULL stdin. A parancsot és az eredményt naplózza (Rule 0 -
    minden subprocess hagyjon nyomot), és időtúllépésnél a megszokott
    CMD_TIMEOUT_RETURNCODE-os CommandResult-tal tér vissza, nem kivétellel.

    Létezésének oka: a letöltési fallback (download_with_cert_fallback) egy futtatót vár,
    az updater viszont modul-szintű függvényekből hívja - enélkül minden hívási helyre
    külön kis futtatót kellene írni, ami a projekt legrégebbi visszatérő hibája (a
    duplikált logika egyik példánya lemarad)."""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    cmd_str = cmd if isinstance(cmd, str) else ' '.join(str(c) for c in cmd)
    logging.debug(f"[CMD] Futtatás (alap futtató): {cmd_str[:300]}")
    kwargs.setdefault('stdin', subprocess.DEVNULL)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, errors='replace',
                             startupinfo=si, creationflags=subprocess.CREATE_NO_WINDOW, **kwargs)
        logging.debug(f"[CMD] returncode={res.returncode}, stdout={(res.stdout or '').strip()[:500]!r}")
        if res.returncode != 0 and res.stderr:
            logging.warning(f"[CMD] stderr: {res.stderr[:1000]}")
        return res
    except subprocess.TimeoutExpired:
        logging.error(f"[CMD] IDŐTÚLLÉPÉS (limit={kwargs.get('timeout')}s): {cmd_str[:200]}")
        return CommandResult(CMD_TIMEOUT_RETURNCODE, '', 'IDŐTÚLLÉPÉS')


def fetch_text_with_cert_fallback(url, *, run_fn=None, timeout=30, ps_timeout=120,
                                  log_tag='FETCH', encoding='utf-8'):
    """Szöveges tartalom letöltése ugyanazzal a régi-Windows-barát letöltési lánccal,
    amit a fájl-letöltés használ (Python -> PowerShell: Invoke-WebRequest/WebClient/
    certutil). Ideiglenes fájlon keresztül megy, mert így NEM kell külön karbantartani
    egy második letöltő-implementációt - a projekt egyik állandó hibaforrása pont az,
    amikor két majdnem-azonos másolat közül csak az egyik kap javítást.

    Az auto-updater használja: annak a BUILD_NUMBER-ellenőrzése eddig csupasz
    urllib-hívás volt, tehát pontosan az a fajta TLS-hiba buktatta volna el egy régi
    gépen, ami 2026-08-13-án a WebView2 telepítőt is elbuktatta - és ezzel a régi gép
    soha nem értesült volna arról, hogy van újabb (épp a hibát javító) build."""
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix='dv_fetch_', suffix='.tmp')
    os.close(fd)
    try:
        download_with_cert_fallback(run_fn or default_run, url, tmp,
                                    timeout=timeout, ps_timeout=ps_timeout, log_tag=log_tag)
        with open(tmp, 'rb') as f:
            return f.read().decode(encoding, errors='replace')
    finally:
        try:
            os.remove(tmp)
        except OSError as e:
            logging.debug(f"[{log_tag}] Az ideiglenes fájl törlése nem sikerült ({tmp}): {e}")


def _ps_download_script(url, dest):
    """A PowerShell-oldali (schannel) letöltés scriptje. SZÁNDÉKOSAN a Windows 7 alap
    PowerShell 2.0-jáig lefelé kompatibilis (a 8 3.0-t, a 8.1 4.0-t hozza) - és pont a
    régi gépeken van a legnagyobb szükség erre az ágra.

    HÁROM LETÖLTŐ ÁG, ebben a sorrendben, mindegyik a MÉRT okból:

    1. `Invoke-WebRequest` (PowerShell 3.0+). A legjobb: rendes hibaüzenetek, HTTP-státusz.
       Windows 7-en (PS 2.0, WMF-frissítés nélkül) NINCS - ezért van a létezés-vizsgálat,
       enélkül "The term 'Invoke-WebRequest' is not recognized" lenne.

    2. `System.Net.WebClient` (.NET 2.0 óta létezik, tehát PS 2.0-n is meghívható).

    3. `certutil.exe -urlcache -split -f` - NATÍV, .NET-FÜGGETLEN. Ez nem elméleti
       biztonsági háló: MÉRVE (2026-08-13, `powershell -Version 2` motor alatt) a .NET-es
       ágak MINDEGYIKE elhasal ugyanazzal a hibával -
           'A konfigurációs rendszer inicializálása sikertelen volt'
       (a WebClient és a HttpWebRequest is, már a példányosításnál/hívásnál), miközben a
       certutil ugyanott hibátlanul lehozta a teljes 1 695 960 bájtos fájlt. A certutil
       Vista óta minden Windowson ott van, a rendszer schannel-vermét használja (tehát a
       tanúsítvány-ellenőrzés TELJES értékű marad), és nem érdekli a .NET konfigurációja.
       Megjegyzés a mérés olvasásához: a `-Version 2` motor a fejlesztőgépen a .NET 4-es
       hoszt konfigjával fut, ezért nem állítható biztosra, hogy egy VALÓDI Win7-en is
       bukna a WebClient - de a certutil-ág épp ezért van a lánc végén: nem kerül semmibe,
       ha nem kell, és megment, ha kell.

    Minden ág után MÉRET-ellenőrzés (nem csak Test-Path): egy megszakadt letöltés
    0 bájtos fájlt hagy maga után, amit a következő ág "kész"-nek látna, a hívó pedig
    sikeres letöltésnek - ez pontosan az a néma hamis siker, amit a projekt mindenhol
    kerül. A csonkot ezért minden bukott ág után töröljük.

    A TLS 1.2 (3072) bekapcsolása try/catch-ben van: a .NET 4.x ezeken a rendszereken
    alapból TLS 1.0-t ajánl, amit a github.com és a Microsoft CDN-jei is elutasítanak -
    enélkül az egész fallback értelmetlen lenne. A try azért kell, mert ha a gépen csak
    .NET 4.0 van, az enum-érték ismeretlen, és a kivétel enélkül megölné a letöltést
    AZELŐTT, hogy egyáltalán megpróbálta volna.

    Explicit exit kód a végén: a `powershell -Command` egy nem-terminating hiba után is
    0-val tér vissza, vagyis a hívó "sikeresnek" látná a semmit (ugyanaz a hibaosztály,
    mint a Sumatra-nyomtatás esete)."""
    u, d = _ps_quote(url), _ps_quote(dest)
    return (
        "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
        "try { [Net.ServicePointManager]::SecurityProtocol = "
        "[Net.ServicePointManager]::SecurityProtocol -bor 3072 } catch { }; "
        f"$u='{u}'; $d='{d}'; $err=''; "
        # Egy ág akkor sikeres, ha a fájl LÉTEZIK ÉS NEM ÜRES. A csonkot töröljük, hogy a
        # következő ág tiszta lappal induljon (és hogy a hívó Python-oldali méret-
        # ellenőrzése se egy fél fájlt lásson).
        "function Test-Dl { if ((Test-Path $d) -and ((Get-Item $d).Length -gt 0)) { return $true }; "
        "  if (Test-Path $d) { Remove-Item $d -Force -ErrorAction SilentlyContinue }; return $false }; "
        # 1) Invoke-WebRequest (PS 3.0+)
        "if (Get-Command Invoke-WebRequest -ErrorAction SilentlyContinue) { "
        "  try { Invoke-WebRequest -Uri $u -OutFile $d -UseBasicParsing } "
        "  catch { $err = 'IWR: ' + $_.Exception.Message } "
        "}; "
        # 2) .NET WebClient (PS 2.0-n is)
        "if (-not (Test-Dl)) { "
        "  try { $wc = New-Object System.Net.WebClient; "
        "        $wc.Headers.Add('User-Agent','Mozilla/5.0'); "
        "        $wc.DownloadFile($u, $d) } "
        "  catch { $err = $err + ' | WebClient: ' + $_.Exception.Message } "
        "}; "
        # 3) certutil (natív) - a natív parancs stderr-je Stop mellett terminating hibát
        #    dobna, ezért erre az egy hívásra Continue-ra váltunk.
        "if (-not (Test-Dl)) { "
        "  $ErrorActionPreference='Continue'; "
        "  try { $null = & certutil.exe -urlcache -split -f $u $d 2>&1 } "
        "  catch { $err = $err + ' | certutil: ' + $_.Exception.Message }; "
        "  $ErrorActionPreference='Stop' "
        "}; "
        "if (Test-Dl) { Write-Output ('OK meret=' + (Get-Item $d).Length); exit 0 } "
        "else { Write-Output ('HIBA:' + $err); exit 1 }"
    )


def download_with_cert_fallback(run_fn, url, dest, *, timeout=60, ps_timeout=120,
                                log_tag='DOWNLOAD', error_msg=None, progress_cb=None):
    """HTTPS letöltés a friss-Windows tanúsítvány-fallbackkel - KÖZÖS példány (korábban
    4 másolatban élt: block.bat, BootFixer.cmd, stresstools.zip + a 2026-07-28-án
    kivett nicpack.zip).

    Vadonatúj Windows-telepítésen a gyökértanúsítvány-tár még hiányos: a Windows a
    gyökereket igény szerint tölti le, de ezt csak a schannel-alapú kliensek (böngésző,
    PowerShell, .NET) váltják ki - a Python OpenSSL-je nem, ezért nála
    CERTIFICATE_VERIFY_FAILED lesz. Tipikus tünet: a github.com (Sectigo/USERTrust
    gyökér) elhasal, miközben a raw.githubusercontent.com (DigiCert gyökér) működik.
    CSAK TLS/tanúsítvány-jellegű hibára esünk vissza PowerShell Invoke-WebRequest-re
    (schannel; a teljes listát lásd DOWNLOAD_PS_FALLBACK_ERRORS): a tanúsítvány-ellenőrzés
    ott is TELJES értékű (SEMMIT nem kapcsolunk ki!), és mellékhatásként a hiányzó gyökér
    bekerül a Windows tárba, így a gép későbbi Python-letöltései is meggyógyulnak. Ez a
    fallback NEM ellenőrzés-megkerülés, és tilos azzá alakítani (admin-jogon futtatott
    payloadokat töltünk le vele).

    RÉGI WINDOWS (7/8/8.1): a fallback ott duplán fontos. Egyrészt az OpenSSL 3.x
    szigorúbb, mint a rendszer schannel-je (lásd UNEXPECTED_EOF_WHILE_READING a
    konstansnál), másrészt a PS-ág explicit TLS 1.2-re kapcsol - a .NET 4.x ezeken a
    rendszereken alapból TLS 1.0-t ajánl, amit ma már szinte minden kiszolgáló elutasít.

    progress_cb: opcionális callback(letöltött_bájt, összes_bájt_vagy_None) - a Python-os
    letöltési ágon darabonként (256 KB) hívódik, hogy a hívó százalékos folyamatjelzőt
    mutathasson. A PowerShell-fallback ágon nincs bájtszintű visszajelzés (az Invoke-WebRequest
    kimenete nem streamelhető ide) - ott a hívó indeterminate sávot mutasson. A callback
    kivételei nem szakítják meg a letöltést.

    Visszaadja a dest-et; hibánál (vagy üres letöltött fájlnál) kivételt dob."""
    import urllib.request
    import urllib.error
    import ssl
    import shutil as _shutil
    logging.info(f"[{log_tag}] Letöltés innen: {url}")
    ssl_ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
    try:
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=timeout) as resp, open(dest, 'wb') as f:
            if progress_cb is None:
                _shutil.copyfileobj(resp, f)
            else:
                try:
                    total = int(resp.headers.get('Content-Length') or 0) or None
                except Exception:
                    total = None
                done = 0
                while True:
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    try:
                        progress_cb(done, total)
                    except Exception as cb_err:
                        logging.debug(f"[{log_tag}] progress_cb hiba (figyelmen kívül hagyva): {cb_err}")
    except (urllib.error.URLError, ssl.SSLError) as dl_err:
        # ssl.SSLError is elkapva: az urlopen a legtöbb TLS-hibát URLError-ba csomagolja,
        # de nem mindet - és mindkettő OSError-leszármazott, tehát a szűk, konkrét
        # kivételpár olvashatóbb, mint egy csupasz `except OSError`.
        if not _should_try_ps_download(dl_err):
            raise
        logging.warning(f"[{log_tag}] Python SSL/TLS hiba ({dl_err}) - a Python OpenSSL-verme nem tudott megegyezni "
                        f"a kiszolgálóval (hiányos gyökértár VAGY régi Windows TLS-útvonala). Áttérés PowerShell "
                        f"(schannel) letöltésre, teljes tanúsítvány-ellenőrzéssel...")
        # Törzs: a részleges (megszakadt) fájl útban lenne a második próbának - a
        # stresstools.zip-nél terepen bizonyított, hogy a bennmaradt csonk pont azt a
        # lemezhelyet eszi meg, ami az újrapróbálkozáshoz kellene.
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except OSError as rm_err:
            logging.debug(f"[{log_tag}] A félbemaradt fájl törlése nem sikerült: {rm_err}")
        result = run_fn(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command',
                         _ps_download_script(url, dest)], timeout=ps_timeout)
        if not result or result.returncode != 0 or not os.path.exists(dest):
            rc = getattr(result, 'returncode', None)
            err_txt = (getattr(result, 'stderr', '') or '')[:500]
            logging.error(f"[{log_tag}] A PowerShell (schannel) letöltés is elhasalt: returncode={rc}, stderr={err_txt!r}")
            raise Exception(error_msg or "A letöltés sikertelen (nincs internet, vagy a GitHub nem elérhető).")
        logging.info(f"[{log_tag}] PowerShell (schannel) letöltés sikeres.")
    if not os.path.exists(dest) or os.path.getsize(dest) == 0:
        raise Exception(error_msg or "A letöltött fájl üres vagy hiányzik.")
    logging.info(f"[{log_tag}] Letöltve: {dest}")
    return dest



# ============================================================================
# HÍVÁS-LOGOLÁS: minden API-metódus be/kimenete a debug logba
# ============================================================================
# Cél: a terepi hibakeresésnél a log önmagában elmondja, MI hívódott MILYEN
# paraméterekkel, MENNYI ideig futott és MI lett az eredménye (vagy a kivétel
# teljes traceback-je). A subprocess-szint ([CMD]) és a UI-események ([EMIT])
# eddig is logolva voltak - ez a réteg az API-metódus szintet fedi le.

# Ezeket NEM csomagoljuk: vagy saját, részletesebb logolásuk van (emit/_run/js_log),
# vagy olyan forró ciklusban hívódnak (billentyűnként/poll-onként), hogy a logot
# másodpercek alatt telepörgetnék és a rotáció épp a hasznos sorokat dobná ki.
_CALL_LOG_EXCLUDE = {
    'emit', '_run', 'js_log', '_check_cancel', '_print_progress',
    '_send_unicode_char', '_send_vk', '_window_title', '_find_child_by_text',
    '_find_window_for_pid', '_read_console_screen', '_normalize_ctrl_text',
    '_text_alternatives',
    # Katalógus-sor pontozó: eszközönként MINDEN sorra (25-100 db) meghívódik, és
    # bejegyzésenként 2 sort írna (belépés + visszatérés). Terepen (Build 228) egyetlen
    # 2 eszközös katalógus-kör több SZÁZ sornyi zajt termelt, ami az 5 MB-os rotáló
    # logból kiszorítja a valódi előzményt - épp azt, amiből a hibát keressük. A
    # tényleges DÖNTÉS (mennyi sorból melyik nyert és miért) egyetlen összegző sorként
    # megy ki a _catalog_find_driver-ből, ami többet ér, mint a hívásonkénti nyers zaj.
    '_catalog_row_score', '_catalog_row_is_microsoft',

    # ------------------------------------------------------------------------------
    # ESZKÖZÖNKÉNT/CSOMAGONKÉNT hívott segédek (2026-09-07, terepi naplóból MÉRVE).
    #
    # Egy ASRock B450M lánc (Build 303, 24 perc 52 mp, 15 driver) 3,4 MB naplót írt, és
    # ennek a 73%-a ezeknek a függvényeknek a [CALL] belépés/visszatérés párja volt:
    #
    #     _rebind_pkg_ids          797 KB / 4011 sor  <- MINDÖSSZE 20 egyedi INF-re!
    #     _catalog_rows_cache      538 KB / 2236 sor
    #     _catalog_find_driver     440 KB / 1002 sor  <- a teljes driver-táblát vitte argumentumként
    #     _catalog_fetch_rows      260 KB / 1792 sor
    #     _catalog_detail_page     255 KB / 902 sor
    #     _catalog_supported_hwids 187 KB / 888 sor
    #
    # A kár nem elméleti: a napló A LÁNC KÖZBEN fordult át (21:17:15), a záró 2 perc
    # egymaga 1,34 MB-ot írt - vagyis egy következő futás után ennek a láncnak a
    # törlési fázisa már kiesne a rotációból, épp az a rész, amiből hibát keresünk.
    # Ez pontosan a Rule 0 ellen-szabálya: maximális INFORMÁCIÓ, nem maximális sor.
    #
    # MINDEGYIKNÉL ELLENŐRIZVE, hogy a DÖNTÉS a saját naplósoraiban megmarad:
    #   _rebind_pkg_ids, _staged_vendor_inf_for -> a kör kiírja a jelölteket, a névvel
    #       felsorolt kihagyottakat és a kiválasztottakat ([REBIND] sorok);
    #   _catalog_find_driver, _catalog_fetch_rows -> minden lekérdezés elé kimegy a
    #       "[CATALOG] Keresés: <eszköz> (<hwid>)" sor, a végén a "Döntés:" összegzés
    #       (Döntés-sor hiánya = nulla sor minden kulcsra - lásd CLAUDE.md 8. lépés);
    #   _catalog_rows_cache -> betöltéskor egy INFO sor, találatkor "Gyorsítótárból:";
    #   _catalog_detail_page, _catalog_supported_hwids -> a hívó NÉVSZERINT logolja
    #       minden letöltés előtt kizárt csomagot ("KIZÁRVA letöltés előtt");
    #   _autofix_stats_path -> állandó útvonal, nincs benne döntés.
    # Ha ezek bármelyikéből eltűnik a saját naplósor, ide is vissza kell nyúlni.
    '_rebind_pkg_ids', '_staged_vendor_inf_for',
    '_catalog_find_driver', '_catalog_fetch_rows', '_catalog_rows_cache',
    '_catalog_detail_page', '_catalog_supported_hwids',
    '_autofix_stats_path',

    # A gép-térkép építője (2026-09-17): a teljes eszközfát (146+ csomópont, ~15 mező
    # mindegyiken) kapja és egy ~50 KB-os térképet ad vissza - a [CALL]-réteg ennek a
    # repr-jét minden hívásnál felépítené. A döntés a saját soraiban megmarad: a
    # `log_machine_map` INFO-n kártyánként, DEBUG-on csomagonként INDOKKAL naplóz, és a
    # kivételt a metódus maga kapja el és naplózza WARNING-gal, teljes veremmel.
    '_build_machine_map',
}


def _trunc_repr(value, limit):
    """repr() biztonságosan + hossz-korlátozva (egy óriási driverlista ne öljön logot)."""
    try:
        r = repr(value)
    except Exception:
        r = f'<repr hiba: {type(value).__name__}>'
    if len(r) > limit:
        return r[:limit] + f'...[+{len(r) - limit} kar.]'
    return r


def _make_logged(cls_name, fn):
    import functools

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        level = logging.DEBUG if fn.__name__.startswith('_') else logging.INFO
        arg_s = ', '.join([_trunc_repr(a, 200) for a in args] +
                          [f'{k}={_trunc_repr(v, 200)}' for k, v in kwargs.items()])
        logging.log(level, f"[CALL] {cls_name}.{fn.__name__}({arg_s})")
        t0 = time.monotonic()
        try:
            rv = fn(self, *args, **kwargs)
        except Exception as e:
            logging.error(f"[CALL] {cls_name}.{fn.__name__} KIVÉTEL ({time.monotonic() - t0:.2f}s): {e}",
                          exc_info=True)
            raise
        logging.log(level, f"[CALL] {cls_name}.{fn.__name__} -> {_trunc_repr(rv, 300)} ({time.monotonic() - t0:.2f}s)")
        return rv
    return wrapper


def install_call_logging(cls):
    """A cls MINDEN (öröklött, nem-dunder, nem-kizárt) metódusát log-csomagolóba teszi.
    Az app/gui/api.py és app/cli/api.py hívja az összerakott osztályokra. A staticmethod-ok
    kimaradnak (nincs self paraméterük, a wrapper elrontaná őket - egyébként is mind a
    kizárt forró-helper listán vannak)."""
    import inspect
    for name in dir(cls):
        if name.startswith('__') or name in _CALL_LOG_EXCLUDE:
            continue
        static_attr = inspect.getattr_static(cls, name)
        if isinstance(static_attr, staticmethod):
            continue
        fn = getattr(cls, name)
        if not inspect.isfunction(fn):
            continue
        setattr(cls, name, _make_logged(cls.__name__, fn))


# ===========================================================================
# EGYPÉLDÁNYOS MUTEX - a belépési pont hozza létre, de EL KELL TUDNI ENGEDNI
# ---------------------------------------------------------------------------
# MIÉRT: a program csak egy példányban futhat (driver_tool.py, "Global\...Mutex_Lock").
# A CLI módra váltás viszont ÚJ FOLYAMATOT indít, és a régi (grafikus) példány még
# fogja a mutexet, amikor az új elindul - az új példány ezért "A DriverVarázsló már fut
# a rendszeren!" üzenettel azonnal kilépne (terepen ez történt, 2026-08-29).
#
# Ezért a belépési pont ide teszi a handle-t, és a váltás előtt elengedjük. Ha az új
# folyamat indítása mégis elhasal, visszavesszük - különben a mostani példány mutex
# nélkül futna tovább, és utána bárhányszor el lehetne indítani a programot.
# ===========================================================================
APP_MUTEX_HANDLE = None
APP_MUTEX_NAME = r"Global\DriverVarazslo_App_Mutex_Lock"


def release_app_mutex():
    """Az egypéldányos mutex elengedése. True, ha tényleg elengedtük."""
    global APP_MUTEX_HANDLE
    h = APP_MUTEX_HANDLE
    if not h:
        logging.debug("[MUTEX] Nincs elengedhető mutex-handle.")
        return False
    try:
        import ctypes
        ctypes.windll.kernel32.ReleaseMutex(h)
        ctypes.windll.kernel32.CloseHandle(h)
        APP_MUTEX_HANDLE = None
        logging.info("[MUTEX] Az egypéldányos mutex elengedve (CLI módra váltás).")
        return True
    except Exception as e:
        logging.warning(f"[MUTEX] A mutex elengedése nem sikerült: {e}")
        return False


def acquire_app_mutex():
    """A mutex visszavétele (ha az elengedés utáni művelet elhasalt)."""
    global APP_MUTEX_HANDLE
    if APP_MUTEX_HANDLE:
        return True
    try:
        import ctypes
        APP_MUTEX_HANDLE = ctypes.windll.kernel32.CreateMutexW(None, False, APP_MUTEX_NAME)
        logging.info("[MUTEX] Az egypéldányos mutex visszavéve.")
        return bool(APP_MUTEX_HANDLE)
    except Exception as e:
        logging.warning(f"[MUTEX] A mutex visszavétele nem sikerült: {e}")
        return False
