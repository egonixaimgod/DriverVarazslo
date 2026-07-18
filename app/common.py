"""Közös alapok: WebView2 ellenőrzés, admin-check, útvonal-helperek, PowerShell-quote,
app-adatmappa, webview-állapot eventek, BUILD_NUMBER hordozó és a hívás-logolás."""

# === AUTO-IMPORTS ===
import ctypes
import ctypes.wintypes
import os
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
        t0 = time.time()
        try:
            rv = fn(self, *args, **kwargs)
        except Exception as e:
            logging.error(f"[CALL] {cls_name}.{fn.__name__} KIVÉTEL ({time.time() - t0:.2f}s): {e}",
                          exc_info=True)
            raise
        logging.log(level, f"[CALL] {cls_name}.{fn.__name__} -> {_trunc_repr(rv, 300)} ({time.time() - t0:.2f}s)")
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
