"""DriverVarázsló - belépési pont.

A tényleges funkciók az app/ csomagban élnek, feature-fájlokra bontva (nagyjából a
bal oldali menüpontok szerint) - lásd CLAUDE.md "Architecture". Ez a fájl csak:
  - a BUILD_NUMBER definícióját tartalmazza (LENT NEM MOZDÍTHATÓ: a kint lévő régi
    exe-k auto-updatere EZT a fájlt tölti le GitHubról és regexeli belőle a
    ^BUILD_NUMBER\\s*=\\s*(\\d+) sort - ha innen kikerül, minden régi felhasználó
    örökre a saját buildjén ragad, és a bump_build.py is ezt a fájlt írja),
  - és a program indítását: single-instance mutex, UAC-emelés, logging beállítás,
    GUI (pywebview) vagy CLI mód kiválasztása, WebView2 watchdog.
"""
import ctypes
import os
import sys
import subprocess
import threading
import time
import logging

BUILD_NUMBER = 210

from app import common
common.BUILD_NUMBER = BUILD_NUMBER

import webview

from app.common import (
    check_webview2_runtime,
    is_admin,
    resource_path,
    _app_data_dir,
    _webview_ready,
    _webview_error,
)


# ================================================================
# MAIN
# ================================================================
if __name__ == "__main__":
    # --- SINGLE INSTANCE CHECK (Csak egyszer fusson) ---
    ERROR_ALREADY_EXISTS = 183
    mutex_name = "Global\\DriverVarazslo_App_Mutex_Lock"
    hMutex = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
    if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        try:
            ctypes.windll.user32.MessageBoxW(
                None,
                "A DriverVarázsló már fut a rendszeren!\n\nKérjük, zárd be a másik ablakot, vagy ellenőrizd a tálcán.",
                "DriverVarázsló - Figyelmeztetés",
                0x30 | 0x40000  # MB_ICONWARNING | MB_TOPMOST
            )
        except Exception:
            pass
        sys.exit(0)

    import multiprocessing
    multiprocessing.freeze_support()

    def _relaunch_elevated():
        """UAC self-elevation. True, ha az emelt jogú processz sikeresen elindult;
        False, ha a felhasználó elutasította a UAC-promptot vagy hiba történt
        (ShellExecuteW <= 32 visszatérési érték = hiba, ld. WinAPI dokumentáció)."""
        params = ' '.join([f'"{arg}"' for arg in sys.argv[1:]])
        if getattr(sys, 'frozen', False):
            exe, args = sys.executable, params
        else:
            exe, args = sys.executable, f'"{sys.argv[0]}" {params}'
        result = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, None, 1)
        return result > 32

    if "--cli" in sys.argv:
        if getattr(sys, "frozen", False):
            # Attach to the parent console if running from cmd in windowed mode
            if ctypes.windll.kernel32.AttachConsole(-1):
                sys.stdout = open("CONOUT$", "w", encoding="utf-8")
                sys.stderr = open("CONOUT$", "w", encoding="utf-8")
                sys.stdin = open("CONIN$", "r", encoding="utf-8")

    # Logging - RotatingFileHandler, hogy a DEBUG-szintű, minden subprocess-kimenetet logoló
    # fájl ne nőhessen korlátlanul (egy hosszú élettartamú szerviz-USB-n/WinPE-n, sok gépen,
    # sok futtatás alatt évekig gyűlő log könnyen több száz MB-ra hízhatna rotáció nélkül).
    # A beállítás SZÁNDÉKOSAN a --cli ág ELŐTT van (a szétbontás előtt utána volt): így a
    # CLI módú futások is ugyanabba a debug logba írnak, nem vesznek el nyomtalanul.
    log_filename = os.path.join(_app_data_dir(), "DriverVarázsló_debug.log")
    try:
        from logging.handlers import RotatingFileHandler

        class _BomRotatingFileHandler(RotatingFileHandler):
            """UTF-8 BOM-ot ír minden ÚJ (üres) log fájl elejére - enélkül a sima
            Jegyzettömb/más szerkesztők a BOM nélküli UTF-8 fájlt gyakran ANSI-ként
            találgatják, és a magyar ékezetek "Ã¡"-szerű szemétként jelennek meg
            (terepen bizonyított). A BOM csak a fájl legelejére kerül (új fájl vagy
            rotáció utáni friss fájl), meglévő fájl folytatásakor nem szúrunk be
            semmit a közepére."""
            def _open(self):
                stream = super()._open()
                try:
                    if stream.tell() == 0:
                        stream.write('﻿')
                except Exception:
                    pass
                return stream

        log_handler = _BomRotatingFileHandler(log_filename, maxBytes=5 * 1024 * 1024, backupCount=2, encoding='utf-8')
        # %(threadName)s: párhuzamos szálak (pl. két egyszerre futó listázás) sorai a
        # logban összefésülődnek - a szálnév nélkül a [CMD] parancs és a hozzá tartozó
        # eredmény nem párosítható össze (terepen félrevezető volt: egy PowerShell
        # parancs "eredményeként" egy másik szál DISM-kimenete látszott).
        logging.basicConfig(level=logging.DEBUG, handlers=[log_handler],
                            format='%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    except Exception:
        logging.basicConfig(level=logging.DEBUG)

    logging.info("=" * 50)
    logging.info(f"DriverVarázsló ELINDITVA (Build {BUILD_NUMBER})")
    logging.info(f"Parancssor: {sys.argv}")
    logging.info(f"Futtatasi konyvtar: {os.getcwd()}")
    logging.info(f"Admin jog: {bool(is_admin())}")
    logging.info("=" * 50)

    # CLI mód
    if '--cli' in sys.argv:
        if not is_admin():
            print("⚠️  Rendszergazdai jogosultság szükséges, UAC-emelés kérése...")
            if _relaunch_elevated():
                sys.exit(0)
            print("❌ Az emelt jogú indítás megszakadt vagy elutasításra került!")
            print("   Futtasd manuálisan rendszergazdaként!")
            input("Nyomj ENTER-t a kilépéshez...")
            sys.exit(1)
        from app.cli.menu import run_cli_mode
        run_cli_mode()
        sys.exit(0)

    if not is_admin():
        if not _relaunch_elevated():
            try:
                ctypes.windll.user32.MessageBoxW(
                    None,
                    "A DriverVarázsló futtatásához rendszergazdai jogosultság szükséges.\n\n"
                    "Az emelt jogú indítás megszakadt vagy elutasításra került (UAC).\n"
                    "Indítsd el újra, és fogadd el a jogosultság-kérést.",
                    "DriverVarázsló - Jogosultság szükséges",
                    0x10 | 0x40000  # MB_ICONERROR | MB_TOPMOST
                )
            except Exception:
                pass
        sys.exit()

    def global_exception_handler(exc_type, exc_value, exc_traceback):
        err_str = str(exc_value)
        logging.exception("FATÁLIS HIBA:", exc_info=(exc_type, exc_value, exc_traceback))
        # WebView2 hibák detektálása
        if 'WebView2' in err_str or 'ICoreWebView2' in err_str or '.NET' in err_str:
            logging.error("[MAIN] WebView2 hiba detektálva exception handler-ben!")
            _webview_error.set()
    sys.excepthook = global_exception_handler

    def cleanup_zombies():
        # Nem atexit-tel regisztrálva, mert a program mindig os._exit(0)-val lép ki,
        # ami teljesen kihagyja az atexit hook-okat - ezért itt explicit hívjuk meg
        # minden os._exit(0) előtt.
        try:
            pid = os.getpid()
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception as e:
            logging.debug(f"[MAIN] cleanup_zombies taskkill sikertelen: {e}")

    def thread_exception_handler(args):
        err_str = str(args.exc_value)
        logging.exception("HÁTTÉRSZÁL HIBA:", exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        if 'WebView2' in err_str or 'ICoreWebView2' in err_str or '.NET' in err_str:
            logging.error("[MAIN] WebView2 hiba detektálva szál exception handler-ben!")
            _webview_error.set()
    threading.excepthook = thread_exception_handler

    from app.cli.menu import run_cli_mode

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
            "A DriverVarázsló GUI-hoz WebView2 v109+ szükséges.\n\n"
            "Telepítsem automatikusan?\n"
            "(~2MB letöltés, pár másodperc)",
            "DriverVarázsló - WebView2 telepítés",
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
                    "DriverVarázsló",
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
                        "DriverVarázsló - Siker",
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
                        "DriverVarázsló - Hiba",
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
                    "DriverVarázsló - Hiba",
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
        print("  📋 DRIVERVARÁZSLÓ - CLI MÓD")
        print("=" * 60)

        run_cli_mode()
        cleanup_zombies()
        os._exit(0)

    # Hardware rendering (gyors) - az autofix progress külön ablakban jelenik meg

    from app.gui import DriverToolApi

    api = DriverToolApi()
    html_path = resource_path('ui.html')

    window = webview.create_window(
        'DriverVarázsló',
        url=html_path,
        js_api=api,
        width=1200, height=780,
        min_size=(900, 600)
    )

    def on_start():
        api.set_window(window)

    # Watchdog: ha 15mp alatt nem indul el a GUI, bezárja az ablakot és CLI-re vált
    def webview_watchdog():
        TIMEOUT = 60  # seconds
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

    cleanup_zombies()
    os._exit(0)
