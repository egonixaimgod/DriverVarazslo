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

BUILD_NUMBER = 313

from app import common
common.BUILD_NUMBER = BUILD_NUMBER

import webview

from app.common import (
    check_webview2_runtime,
    default_run,
    download_with_cert_fallback,
    ensure_console,
    gui_attempt_begin,
    gui_crash_check,
    gui_env_signature,
    is_admin,
    resource_path,
    _app_data_dir,
    _webview_ready,
    _webview_error,
)
# Indulási előfeltételek (felmérés + automatikus javítás) - lásd app/prereq.py.
# SZÁNDÉKOSAN modul-szintű import, nem a __main__ blokkban: így a PyInstaller
# függőség-elemzője biztosan becsomagolja a modult (egy hiányzó modul a fagyasztott
# exe-ben induláskori ModuleNotFoundError lenne, azaz pont a néma indulási hiba,
# amit ez a réteg megszüntetni hivatott).
from app.prereq import log_environment, gui_blocker, install_dotnet_framework

# MessageBox konstansok (a WinAPI MessageBoxW paraméterei) - a GUI-előfeltételek
# ellenőrzése és a WebView2-telepítés is használja őket, ezért modul-szinten állnak.
MB_YESNO = 0x4
MB_ICONQUESTION = 0x20
MB_ICONWARNING = 0x30
MB_ICONERROR = 0x10
MB_ICONINFORMATION = 0x40
MB_TOPMOST = 0x40000
IDYES = 6


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

    # A handle-t elérhetővé tesszük: a CLI módra váltás ÚJ folyamatot indít, és a mutexet
    # el kell engedni, különben az új példány "már fut a rendszeren" üzenettel kilépne
    # (lásd common.release_app_mutex).
    common.APP_MUTEX_HANDLE = hMutex
    common.APP_MUTEX_NAME = mutex_name

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
            # A SZÜLŐ KONZOLJA LEHET REJTETT IS - ilyenkor NEM szabad rácsatlakozni.
            # A `CREATE_NO_WINDOW`-val indított folyamatok konzolja létezik, csak nincs
            # megjelenítve; ha ahhoz kötnénk a kimenetet, a program tökéletesen működne,
            # miközben a felhasználó egy üres ablakot bámul (terepen pontosan ez történt,
            # 2026-08-31). Ezért a csatlakozás után megnézzük, LÁTHATÓ-e a konzolablak, és
            # ha nem, elengedjük, hogy az `ensure_console()` sajátot nyithasson.
            _attached = bool(ctypes.windll.kernel32.AttachConsole(-1))
            if _attached:
                _hwnd = ctypes.windll.kernel32.GetConsoleWindow()
                if not (_hwnd and ctypes.windll.user32.IsWindowVisible(_hwnd)):
                    ctypes.windll.kernel32.FreeConsole()
                    _attached = False
            if _attached:
                sys.stdout = open("CONOUT$", "w", encoding="utf-8")
                sys.stderr = open("CONOUT$", "w", encoding="utf-8")
                sys.stdin = open("CONIN$", "r", encoding="utf-8")
            else:
                # NINCS szülő-konzol, amihez csatlakozhatnánk: SAJÁT konzolt nyitunk.
                # Ez az eset NEM elméleti - így indul a CLI, ha (a) a grafikus felület
                # "CLI mód" gombja indította (app/gui/climode.py), vagy (b) valaki az
                # exe-t közvetlenül, `--cli` kapcsolóval futtatja. A windowed exe-ben a
                # `sys.stdout` ilyenkor None, tehát enélkül a menü egy ÜRES, néma ablak
                # lenne - minden kiírás nyomtalanul elveszne (a print() AttributeError-t
                # dobna, amit a konzol-réteg elnyel).
                ensure_console()

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
    # Teljes környezet-kép MINDEN futásnál, MINDEN módban (Rule 0), még a CLI-ág előtt:
    # Windows-verzió, .NET, WebView2, pywebview+SDK, PowerShell-generáció, TLS-beállítás,
    # szabad lemezhely, pnputil-szintaxis. Egy régi gépről érkező hibajelentésnél ezek az
    # első kérdések, és korábban EGYIK SEM szerepelt a naplóban - a 2026-08-13-i Win 8.1-es
    # ügyben emiatt kellett a Windows verzióját megkérdezni, a .NET hiányára pedig csak
    # következtetni lehetett. Lásd app/prereq.py.
    prereq_env = log_environment()

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
    # ------------------------------------------------------------------
    # A GUI-nak KÉT külső feltétele van, és a .NET-et ELŐBB kell nézni
    # ------------------------------------------------------------------
    # A pywebview Windows-os felülete pythonnet-en (clr) keresztül .NET Framework-öt tölt
    # be, és a pythonnet 3.x minimum 4.7.2-t igényel. Ha ez hiányzik, a betöltés NEM
    # Python-kivétel, hanem NATÍV összeomlás: a folyamat nyomtalanul eltűnik, így sem a
    # sys.excepthook, sem a webview-watchdog nem fut le - a felhasználó annyit lát, hogy
    # "nem indul el, nem dob be semmit". Terepen pontosan így nézett ki (2026-08-13,
    # Win 8.1, Build 269): a napló utolsó sora a `webview.start() hívása...`, utána semmi,
    # háromszor egymás után.
    #
    # Ezért itt megállunk, MIELŐTT bármit tennénk: enélkül a program előbb letöltene és
    # 1-3 percig telepítene egy WebView2 Runtime-ot, hogy utána ugyanúgy összeomoljon.
    # A CLI mód viszont pythonnet nélkül is teljes értékű, tehát van hova esni.
    #
    # A --force-gui biztonsági szelep: ha egy gépen a pythonnet mégis elindulna a
    # hivatalos minimum alatt, kézzel kikényszeríthető (a legrosszabb eset a mostani
    # viselkedés, azaz a néma kilépés).
    dotnet_ok = prereq_env['dotnet_ok']
    dotnet_release = prereq_env['dotnet_release']
    dotnet_name = prereq_env['dotnet_name']
    blocker = gui_blocker(prereq_env)

    if blocker and '--force-gui' in sys.argv:
        logging.warning(f"[INIT] --force-gui: {blocker[1]}, de a felhasználó kikényszerítette "
                        f"a grafikus felületet.")
        blocker = None

    if blocker:
        blocker_key, blocker_text = blocker
        logging.warning(f"[INIT] A grafikus felület előfeltétele hiányzik: {blocker_text}")

        # FELAJÁNLJUK A JAVÍTÁST, nem csak jelezzük. A felhasználó kérése szó szerint:
        # "az exe oldja meg a problemat". A .NET telepítése nagy és újraindítást igényel,
        # ezért kérdéssel indul - de a munkát a program végzi el, nem a technikus.
        #
        # Ez az ág HÁROMFÉLEKÉPPEN érhet véget, és mindhárom kilép innen:
        #   - sikeres telepítés, újraindítás nem kell -> os.execv, a program újraindul GUI-val
        #   - sikeres telepítés, újraindítás kell + a felhasználó kéri -> a gép újraindul
        #   - minden más (elutasítás, hiba, "most ne indítsd újra") -> CLI mód lentebb
        # Az utolsó eset üzenetét külön tartjuk, mert egy sikeres, de még nem aktív
        # telepítés után a "túl régi a .NET" szöveg már félrevezető lenne.
        cli_reason = blocker_text
        if blocker_key == 'dotnet':
            answer = ctypes.windll.user32.MessageBoxW(
                None,
                "A grafikus felület ezen a gépen még nem indítható.\n\n"
                f"Telepített .NET Framework: {dotnet_name}\n"
                "Szükséges: .NET Framework 4.7.2 vagy újabb\n\n"
                "Ez Windows 7 / 8 / 8.1 gépeken megszokott - ezek a rendszerek\n"
                "régebbi .NET-tel jönnek.\n\n"
                "Letöltsem és telepítsem most a .NET Framework 4.8-at?\n"
                "(a Microsoft hivatalos telepítője, ~110 MB, 10-20 perc,\n"
                "utána a gép újraindítása szükséges)\n\n"
                "NEM válasz esetén a program szöveges (CLI) módban indul,\n"
                "amiben minden driveres funkció elérhető.",
                "DriverVarázsló - Hiányzó .NET Framework",
                MB_YESNO | MB_ICONQUESTION | MB_TOPMOST
            )
            if answer == IDYES:
                logging.info("[INIT] A felhasználó elfogadta a .NET Framework telepítését.")
                ensure_console()
                print("\n" + "=" * 60)
                print("  .NET FRAMEWORK 4.8 TELEPÍTÉSE")
                print("  (ez kell a grafikus felülethez - a CLI enélkül is működik)")
                print("=" * 60)
                net_ok, net_reboot, net_msg = install_dotnet_framework(
                    log=lambda m: print(f"  {m}"))
                print(f"\n  {net_msg}\n")
                if net_ok and not net_reboot:
                    # Sikerült, és a rendszer már az új verziót mutatja: a programot
                    # újraindítjuk, hogy a friss .NET-tel próbálja a felületet.
                    logging.info("[INIT] A .NET rendben - a program újraindul a felülettel.")
                    ctypes.windll.user32.MessageBoxW(
                        None, net_msg + "\n\nA program most újraindul a grafikus felülettel.",
                        "DriverVarázsló - Siker", 0x40 | MB_TOPMOST)
                    os.execv(sys.executable, [sys.executable] + sys.argv)
                elif net_ok and net_reboot:
                    logging.info("[INIT] A .NET telepítve, újraindítás szükséges.")
                    reboot_answer = ctypes.windll.user32.MessageBoxW(
                        None,
                        net_msg + "\n\nA grafikus felülethez a gépet újra kell indítani.\n\n"
                                  "Újraindítsam most?\n"
                                  "(NEM esetén a program szöveges módban folytatja, és a\n"
                                  "grafikus felület a következő indításnál lesz elérhető.)",
                        "DriverVarázsló - Újraindítás szükséges",
                        MB_YESNO | MB_ICONQUESTION | MB_TOPMOST)
                    if reboot_answer == IDYES:
                        logging.warning("[INIT] A felhasználó kérésére a gép újraindul "
                                        "(.NET telepítés után).")
                        print("  A gép most újraindul...")
                        subprocess.run(['shutdown', '/r', '/t', '5', '/f'],
                                       creationflags=subprocess.CREATE_NO_WINDOW)
                        cleanup_zombies()
                        os._exit(0)
                    cli_reason = ("a .NET Framework telepítve, de a gépet még újra kell "
                                  "indítani - utána a grafikus felület elérhető lesz")
                else:
                    logging.error(f"[INIT] A .NET telepítése nem sikerült: {net_msg}")
                    cli_reason = f"a .NET telepítése nem sikerült ({net_msg})"
            else:
                logging.info("[INIT] A felhasználó elutasította a .NET telepítését.")

        logging.info(f"[INIT] CLI mód indítása. Ok: {cli_reason}")
        ensure_console()
        print("\n" + "=" * 60)
        print("  ⚠️  A GRAFIKUS FELÜLET NEM ELÉRHETŐ")
        print(f"  Ok: {cli_reason}")
        print("  A program szöveges (CLI) módban indul -")
        print("  ebben minden driveres funkció elérhető.")
        print("=" * 60)
        run_cli_mode()
        cleanup_zombies()
        os._exit(0)

    wv2_ok, wv2_info = check_webview2_runtime()
    if wv2_ok:
        logging.info(f"[INIT] WebView2 Runtime OK: v{wv2_info}")
    else:
        logging.warning(f"[INIT] WebView2 nem megfelelő: {wv2_info}")
        logging.info("[INIT] WebView2 telepítés felajánlása...")

        # MessageBox: telepítsük? (a konstansok a modul tetején)
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

            import tempfile

            # A folyamat MOSTANTÓL LÁTHATÓ. Eddig itt egy "Kattints OK-ra és várd meg!"
            # MessageBox állt, utána pedig 1-3 percig SEMMI nem történt a képernyőn:
            # a felhasználó nem tudta, tölt-e még, telepít-e, vagy kifagyott (pontosan ez
            # volt a terepi visszajelzés). Egy windowed exe-nek nincs konzolja, ezért
            # nyitunk egyet - ez amúgy sem "extra ablak": ha a telepítés sikerül, a
            # program os.execv-vel új folyamatként indul újra (ez a konzol eltűnik vele),
            # ha pedig nem, úgyis a CLI mód jön, aminek kell a konzol.
            ensure_console()
            print("\n" + "=" * 60)
            print("  WEBVIEW2 RUNTIME TELEPÍTÉSE")
            print("  (ez kell a grafikus felülethez - a CLI mód enélkül is működik)")
            print("=" * 60)

            try:
                # Letöltés
                logging.info("[INIT] WebView2 Bootstrapper letöltése...")
                bootstrapper_url = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
                temp_dir = tempfile.gettempdir()
                bootstrapper_path = os.path.join(temp_dir, "MicrosoftEdgeWebview2Setup.exe")

                # A projekt KÖZÖS letöltője, a PowerShell (schannel) fallbackkel együtt -
                # NEM a csupasz urllib.request.urlretrieve, ami korábban itt állt. Az a
                # hívás kimaradt minden védelemből, és terepen (2026-08-13, Windows 8.1,
                # Build 266) pontosan emiatt hasalt el:
                #     [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol
                # Vagyis a gépen a WebView2 hiánya miatt CLI-be esett a program, a GUI-t
                # helyreállító telepítő pedig épp azon az egy letöltési úton jött, aminek
                # nem volt fallbackje. Régi Windowson ez a szabály, nem a kivétel: az
                # OpenSSL 3.x szigorúbb a rendszer schannel-jénél, és a .NET-oldali ág
                # ráadásul explicit TLS 1.2-re kapcsol (lásd app/common.py).
                # A futtató a közös `default_run` (app/common.py): itt még nem létezik a
                # DriverToolApi._run, az api objektum csak jóval lentebb jön létre.
                print("\n[1/2] Telepítő letöltése a Microsofttól...")

                def _dl_progress(done, total):
                    """Bájtszintű visszajelzés a konzolra, egy sorban (\\r-rel felülírva).
                    A PowerShell-fallback ágon nincs ilyen callback, ott a lenti pontozás
                    a jelzés - de az is jobb a semminél."""
                    if total:
                        pct = done * 100 // total
                        bar = '#' * (pct // 5) + '-' * (20 - pct // 5)
                        print(f"\r      [{bar}] {pct:3d}%  ({done // 1024} / {total // 1024} KB)",
                              end='', flush=True)
                    else:
                        print(f"\r      {done // 1024} KB letöltve...", end='', flush=True)

                download_with_cert_fallback(
                    default_run, bootstrapper_url, bootstrapper_path,
                    timeout=90, ps_timeout=300, log_tag='WEBVIEW2',
                    error_msg="A WebView2 telepítő letöltése nem sikerült (nincs internet, "
                              "vagy a Microsoft kiszolgálója nem elérhető).",
                    progress_cb=_dl_progress
                )
                dl_size = os.path.getsize(bootstrapper_path)
                print(f"\r      Letöltve: {dl_size // 1024} KB{' ' * 30}")
                logging.info(f"[INIT] Bootstrapper letöltve: {bootstrapper_path} "
                             f"({dl_size} bájt)")

                # Telepítés silent módban.
                # Az időkorlát SZÁNDÉKOSAN nagy (10 perc), nem a korábbi 120 mp: a
                # bootstrapper egy ~2 MB-os LETÖLTŐ, a tényleges runtime (~150 MB) csak
                # ezután jön le - egy régi, gyenge gépen lassú neten ez bőven túlfut két
                # percen. A régi korlátnál a TimeoutExpired a lenti `except`-be esett és
                # "WebView2 telepítési hiba"-ként jelent meg, miközben a telepítő a
                # háttérben simán befejezte volna a munkát.
                logging.info("[INIT] WebView2 telepítés indítása (silent)...")
                print("\n[2/2] Telepítés futtatása...")
                print("      FIGYELEM: a letöltött fájl csak egy ~1,6 MB-os TELEPÍTŐ -")
                print("      a tényleges futtatókörnyezet (~150 MB) most jön le, ezért")
                print("      ez lassú gépen több percig is eltarthat. NE zárd be!")

                # Óraketyegés: a telepítés alatt a folyamat blokkol, és eddig ez volt a
                # program leghosszabb TELJESEN NÉMA szakasza - a felhasználó nem tudta
                # megkülönböztetni a dolgozó telepítőt a kifagyott programtól.
                install_done = threading.Event()

                def _install_ticker():
                    t0 = time.monotonic()
                    while not install_done.wait(2.0):
                        el = int(time.monotonic() - t0)
                        print(f"\r      Telepítés folyamatban... {el // 60}:{el % 60:02d} "
                              f"(időkorlát: 10:00)", end='', flush=True)

                ticker = threading.Thread(target=_install_ticker, daemon=True, name='wv2-ticker')
                ticker.start()
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                try:
                    result = subprocess.run(
                        [bootstrapper_path, '/silent', '/install'],
                        capture_output=True,
                        startupinfo=si,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                        timeout=600
                    )
                    logging.info(f"[INIT] WebView2 telepítés kész, returncode={result.returncode}")
                except subprocess.TimeoutExpired:
                    # Nem dobjuk tovább: a telepítő ilyenkor is előrehaladhatott, a lenti
                    # újraellenőrzés (check_webview2_runtime) mondja meg a valóságot -
                    # az a mérés, nem a kilépési kód.
                    logging.warning("[INIT] A WebView2 telepítő 10 perc alatt sem tért vissza - "
                                    "az eredményt a registry-ellenőrzés dönti el.")
                finally:
                    install_done.set()
                    ticker.join(timeout=1.5)
                    print(f"\r      Telepítő lefutott.{' ' * 30}")

                # Törlés
                try:
                    os.remove(bootstrapper_path)
                except Exception as e:
                    logging.debug(e)

                # Újraellenőrzés
                wv2_ok2, wv2_info2 = check_webview2_runtime()
                if wv2_ok2:
                    logging.info(f"[INIT] WebView2 telepítés SIKERES! v{wv2_info2}")
                    print(f"\n  ✔ Sikeres telepítés! WebView2 verzió: {wv2_info2}")
                    print("  A program most újraindul a grafikus felülettel...\n")
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
                    print("\n  ✘ A telepítés után sem található megfelelő WebView2 Runtime.")
                    print("    A program CLI (szöveges) módban folytatja.\n")
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
                print(f"\n  ✘ Hiba a telepítés közben: {e}")
                print("    A program CLI (szöveges) módban folytatja.\n")
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
        ensure_console()

        print("\n" + "=" * 60)
        print("  📋 DRIVERVARÁZSLÓ - CLI MÓD")
        print("=" * 60)

        run_cli_mode()
        cleanup_zombies()
        os._exit(0)

    # ------------------------------------------------------------------
    # ÖSSZEOMLÁS-ŐR: ha az ELŐZŐ grafikus indítás némán elszállt, ne próbáljuk újra
    # ------------------------------------------------------------------
    # Részletes indoklás: app/common.py, "GUI-ÖSSZEOMLÁS ŐR". Röviden: a felület
    # indítása natívan is összeomolhat (pythonnet/.NET/WebView2), és olyankor SEMMI nem
    # fut le utána - se kivételkezelő, se watchdog. Egy ilyen gépen a program eddig
    # minden indításkor nyomtalanul eltűnt; mostantól másodszorra a működő CLI jön.
    gui_signature = gui_env_signature(wv2_info if wv2_ok else None, dotnet_release)
    crashed_before, _stored = gui_crash_check(gui_signature)
    if crashed_before and '--force-gui' not in sys.argv:
        logging.error(f"[GUI-ŐR] Az előző grafikus indítás összeomlott ugyanebben a "
                      f"környezetben ({gui_signature}) - a felület kihagyva, CLI mód indul. "
                      f"Kényszerítés: --force-gui")
        ctypes.windll.user32.MessageBoxW(
            None,
            "A grafikus felület a legutóbbi indításkor összeomlott, ezért most\n"
            "kihagyjuk, és a program SZÖVEGES (CLI) módban indul.\n\n"
            "Ez régi rendszereken (Windows 7 / 8 / 8.1) fordul elő: ott a\n"
            "WebView2 legfeljebb 109-es lehet, és a felülethez .NET 4.7.2+ kell.\n\n"
            "Amit tehetsz:\n"
            "  - .NET Framework 4.8 telepítése, majd újraindítás\n"
            "    https://go.microsoft.com/fwlink/?LinkId=2085155\n"
            "  - vagy használd a CLI módot: minden driveres funkció elérhető benne\n\n"
            "Ha új környezetet telepítesz, a program magától újra megpróbálja\n"
            "a grafikus felületet.",
            "DriverVarázsló - Szöveges módban indul",
            0x30 | 0x40000  # MB_ICONWARNING | MB_TOPMOST
        )
        ensure_console()
        print("\n" + "=" * 60)
        print("  ⚠️  A GRAFIKUS FELÜLET AZ ELŐZŐ INDÍTÁSKOR ÖSSZEOMLOTT")
        print("  Ezért most szöveges (CLI) módban indul a program.")
        print("  (A grafikus felülethez .NET 4.7.2+ kell - lásd az üzenetablakot.)")
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
        start = time.monotonic()
        while time.monotonic() - start < TIMEOUT:
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
        # A KOCKÁZATOS SZAKASZ KEZDETE. Innentől a vezérlés a pywebview-hoz, azon át a
        # pythonnet/.NET/WebView2 natív rétegéhez kerül, ami egy alkalmatlan környezetben
        # nem kivételt dob, hanem megöli a folyamatot - onnantól SEMMI nem fut le, se a
        # lenti except, se a watchdog. A jelzőt ezért ITT írjuk ki: ha a program eltűnik,
        # ez a fájl marad utána, és a következő induláskor ebből tudjuk, hogy a felület
        # helyett egyből a működő CLI-t kell adni. Sikeres DOM-nál törlődik
        # (app/gui/base.py: set_window -> common.gui_attempt_succeeded).
        gui_attempt_begin(gui_signature)
        logging.info("[MAIN] webview.start() hívása... (ez a natívan is elszállható szakasz; "
                     "ha a napló itt ér véget, a felület omlott össze)")
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
        # Konzol ablak létrehozása ha nincs (windowed exe-nél) - a közös helperrel,
        # ami a sys.stdout=None esetet is kezeli (lásd common.ensure_console).
        ensure_console()

        print("\n" + "=" * 60)
        print("  ⚠️  GUI nem elérhető - CLI mód automatikusan aktiválva")
        print("  (Telepítsd a WebView2 Runtime-ot a GUI-hoz)")
        print("=" * 60)

        run_cli_mode()

    cleanup_zombies()
    os._exit(0)
