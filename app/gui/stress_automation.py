"""DriverVarázsló GUI - Stabilitás Teszt Win32 UI-automatizálás: dialógus-kattintgatás, konzol-gépelés, ablak-elrendezés."""

# === AUTO-IMPORTS ===
import ctypes
import ctypes.wintypes
import subprocess
import time
import logging
from app.stress_defs import CONSOLE_PROMPT_MAX_WAIT
from app.stress_defs import CONSOLE_UNBLOCK_AFTER
from app.stress_defs import CONSOLE_UNBLOCK_MARKERS
from app.stress_defs import CONSOLE_UNBLOCK_MAX
from app.stress_defs import STRESS_MAIN_WINDOW_WAIT
from app.stress_defs import STRESS_SPLASH_TITLE_MARKERS
from app.stress_defs import STRESS_STEP_TIMEOUT
from app.stress_defs import STRESS_TOOLS
from app.win32 import BM_CLICK
from app.win32 import INPUT_KEYBOARD
from app.win32 import KEYEVENTF_KEYUP
from app.win32 import KEYEVENTF_UNICODE
from app.win32 import VK_RETURN
from app.win32 import _CONSOLE_SCREEN_BUFFER_INFO
from app.win32 import _COORD
from app.win32 import _Input
from app.win32 import _InputUnion
from app.win32 import _KeyBdInput
from app.win32 import _MONITORINFO
# === /AUTO-IMPORTS ===


class GuiStressAutomationMixin:
    """Stabilitás Teszt Win32 UI-automatizálás: dialógus-kattintgatás, konzol-gépelés, ablak-elrendezés. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def _window_title(self, hwnd):
        """Egy ablak feliratának lekérdezése (debug-loghoz) - sosem dob kivételt."""
        try:
            user32 = self._stress_user32()
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return ''
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            return buf.value
        except Exception:
            return '?'

    def _debug_dump_pid_windows(self, pid, context=''):
        """DIAGNOSZTIKA: kilistázza az adott PID-hez tartozó ÖSSZES felső szintű ablakot
        (látható/láthatatlan, cím, osztálynév, méret) és mindegyik gyermek-vezérlőjét
        (osztálynév + felirat) - akkor hívjuk, amikor egy keresés ("nem található gomb/
        dialógus") sikertelen, hogy lássuk, mi VOLT ténylegesen ott, ahelyett hogy csak
        annyit tudnánk, hogy "nem találtuk meg"."""
        user32 = self._stress_user32()
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        rows = []

        def _child_cb(hwnd, _lparam):
            cls = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(hwnd, cls, 128)
            rows.append(f"      child hwnd={hwnd} class='{cls.value}' text='{self._window_title(hwnd)}' visible={bool(user32.IsWindowVisible(hwnd))}")
            return True

        def _top_cb(hwnd, _lparam):
            found_pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(found_pid))
            if found_pid.value != pid:
                return True
            cls = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(hwnd, cls, 128)
            rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            rows.append(f"    top hwnd={hwnd} class='{cls.value}' text='{self._window_title(hwnd)}' "
                        f"visible={bool(user32.IsWindowVisible(hwnd))} rect=({rect.left},{rect.top},{rect.right},{rect.bottom})")
            try:
                user32.EnumChildWindows(hwnd, WNDENUMPROC(_child_cb), 0)
            except Exception as e:
                rows.append(f"      (EnumChildWindows hiba: {e})")
            return True

        try:
            user32.EnumWindows(WNDENUMPROC(_top_cb), 0)
        except Exception as e:
            rows.append(f"  (EnumWindows hiba: {e})")

        if rows:
            logging.warning(f"[STRESSTOOLS-DEBUG] {context} - pid={pid} ablak/vezérlő leltár:\n" + "\n".join(rows))
        else:
            logging.warning(f"[STRESSTOOLS-DEBUG] {context} - pid={pid}: EGYETLEN felső szintű ablakot sem talált EnumWindows ehhez a PID-hez (a folyamat vagy még nem hozott létre ablakot, vagy már nem fut).")

    def _send_unicode_char(self, user32, char):
        """Egyetlen Unicode karakter (le+fel) szimulálása SendInput-tal - a KEYEVENTF_UNICODE
        közvetlenül a karaktert küldi, nem virtuális billentyűkódot, így Shift-állapot
        (kis/nagybetű) kezelése nélkül is pontosan a kívánt karakter jelenik meg.
        Visszaadja, hogy mindkét (le+fel) SendInput hívás sikeresen beszúrta-e az eseményt
        (a SendInput a ténylegesen beszúrt események számával tér vissza - 0, ha az egész
        bemenetet a rendszer blokkolta, pl. UIPI/más folyamat által)."""
        extra = ctypes.c_ulong(0)
        down = _Input(INPUT_KEYBOARD, _InputUnion(ki=_KeyBdInput(0, ord(char), KEYEVENTF_UNICODE, 0, ctypes.pointer(extra))))
        up = _Input(INPUT_KEYBOARD, _InputUnion(ki=_KeyBdInput(0, ord(char), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, ctypes.pointer(extra))))
        r1 = user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(_Input))
        r2 = user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(_Input))
        ok = (r1 == 1 and r2 == 1)
        if not ok:
            logging.warning(f"[STRESSTOOLS-DEBUG] SendInput ('{char}') sikertelen/blokkolt - le={r1}, fel={r2} (1 lenne a várt mindkettőnél)")
        return ok

    def _send_vk(self, user32, vk):
        """Egy virtuális billentyűkód (pl. Enter) le+fel eseménye - erre azért van szükség
        külön (nem KEYEVENTF_UNICODE-dal), mert az Enter/vezérlő billentyűket a konzolos
        sor-beviteli logika a valódi VK_RETURN esemény alapján ismeri fel megbízhatóan.
        Visszaadja, hogy sikeres volt-e (lásd _send_unicode_char)."""
        extra = ctypes.c_ulong(0)
        down = _Input(INPUT_KEYBOARD, _InputUnion(ki=_KeyBdInput(vk, 0, 0, 0, ctypes.pointer(extra))))
        up = _Input(INPUT_KEYBOARD, _InputUnion(ki=_KeyBdInput(vk, 0, KEYEVENTF_KEYUP, 0, ctypes.pointer(extra))))
        r1 = user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(_Input))
        r2 = user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(_Input))
        ok = (r1 == 1 and r2 == 1)
        if not ok:
            logging.warning(f"[STRESSTOOLS-DEBUG] SendInput (VK={vk}) sikertelen/blokkolt - le={r1}, fel={r2} (1 lenne a várt mindkettőnél)")
        return ok

    def _own_console_keeper_pid(self):
        """CLI módban a programnak SAJÁT konzolja van, a Linpack konzoljának olvasásához pedig
        le kell róla csatlakozni (FreeConsole). Ha a CLI az egyetlen folyamat a konzolján, utána
        a kiírás és a billentyűzet halott (mérve, 2026-10-16: OSError 9 a kiírásra, érvénytelen
        stdin). Egy várakozó segédfolyamat ugyanazon a konzolon tartja azt életben, és rajta
        keresztül csatlakozunk vissza (mérve: kiírás és stdin újra jó). GUI módban (nincs saját
        konzol) None. A hívó a _console_attach_lock-ot tartja."""
        kernel32 = ctypes.windll.kernel32
        kernel32.GetConsoleWindow.restype = ctypes.wintypes.HWND
        if not kernel32.GetConsoleWindow():
            return None
        keeper = getattr(self, '_console_keeper', None)
        if keeper is None or keeper.poll() is not None:
            # stdin = cső, amibe sosem írunk: a cmd azon vár, a konzol billentyűit nem olvassa;
            # új folyamatcsoport: a CLI-ben nyomott Ctrl+C nem lövi le.
            cmd = ['cmd.exe', '/d', '/q', '/k']
            keeper = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            self._console_keeper = keeper
            logging.info(f"[STRESSTOOLS] Saját konzol megtartása a konzol-olvasás idejére: {subprocess.list2cmdline(cmd)}, pid={keeper.pid}")
            # Meg kell várni, hogy a segéd TÉNYLEG a konzolon legyen: ha előbb csatlakozunk le,
            # a konzol mégis megszűnik (mérve: az azonnal utána jövő FreeConsole OSError 9-et hozott).
            pids = (ctypes.wintypes.DWORD * 64)()
            deadline = time.monotonic() + 5
            while keeper.pid not in pids[:min(kernel32.GetConsoleProcessList(pids, 64), 64)]:
                if time.monotonic() > deadline:
                    logging.warning(f"[STRESSTOOLS] A konzol-megtartó segéd (pid={keeper.pid}) 5 mp alatt sem jelent meg a saját konzolon - a konzol-olvasás a CLI kiírását elronthatja.")
                    break
                time.sleep(0.05)
        return keeper.pid

    def _reattach_own_console(self, kernel32, keeper_pid):
        """Visszacsatlakozás a saját (CLI) konzolra a segédfolyamaton át - lásd _own_console_keeper_pid."""
        if keeper_pid and not kernel32.AttachConsole(keeper_pid):
            logging.warning(f"[STRESSTOOLS] Nem sikerült visszacsatlakozni a saját konzolra (segéd pid={keeper_pid}, "
                            f"GetLastError={ctypes.GetLastError()}) - a CLI kiírása ettől kezdve elveszhet.")

    def _find_console_window_for_pid(self, pid):
        """Egy KONZOLOS (nem GUI) program ablak-handle-jének megbízható lekérdezése.

        A sima EnumWindows + GetWindowThreadProcessId (lásd _find_window_for_pid) itt NEM
        feltétlenül működik: egy konzolablakot a klasszikus Windows-modellben nem maga a
        konzolos program, hanem egy külön, rejtett conhost.exe-folyamat "birtokol" - így a
        spawnolt folyamat (pl. Linpack) saját PID-je nem biztos, hogy megegyezik az ablakot
        ténylegesen birtokló folyamat PID-jével. (Ezt debug logban is megerősítettük: a GUI
        programok - FurMark, Prime95, HWiNFO - ablaka PID alapján előbb-utóbb mindig
        megtalálható volt, a Linpické soha, még 30 mp várakozás után sem.)

        Az AttachConsole+GetConsoleWindow ezt megkerüli: a hívó folyamat (mi) átmenetileg
        "csatlakozik" a célfolyamat konzoljához, lekérdezi a hozzá tartozó ablakot, majd
        leválik. Mivel ez folyamat-szintű (nem szálankénti) állapot, self._console_attach_lock
        védi a párhuzamos hívásokat."""
        kernel32 = ctypes.windll.kernel32
        kernel32.AttachConsole.argtypes = [ctypes.wintypes.DWORD]
        kernel32.AttachConsole.restype = ctypes.wintypes.BOOL
        kernel32.FreeConsole.argtypes = []
        kernel32.FreeConsole.restype = ctypes.wintypes.BOOL
        kernel32.GetConsoleWindow.argtypes = []
        kernel32.GetConsoleWindow.restype = ctypes.wintypes.HWND
        with self._console_attach_lock:
            keeper_pid = None
            try:
                keeper_pid = self._own_console_keeper_pid()
                free_ok = kernel32.FreeConsole()
                logging.debug(f"[STRESSTOOLS-DEBUG] FreeConsole (saját konzolról leválás) eredmény={bool(free_ok)}")
                attach_ok = kernel32.AttachConsole(pid)
                if not attach_ok:
                    err = ctypes.GetLastError()
                    logging.warning(f"[STRESSTOOLS-DEBUG] AttachConsole(pid={pid}) sikertelen, GetLastError={err} "
                                     f"(5=ACCESS_DENIED gyakran azt jelenti, hogy a folyamatnak MÁR van/volt konzolja, "
                                     f"6=INVALID_HANDLE hogy a PID-nek nincs is konzolja, pl. mert még nem jött létre)")
                    return None
                try:
                    hwnd = kernel32.GetConsoleWindow()
                    if hwnd:
                        logging.debug(f"[STRESSTOOLS-DEBUG] AttachConsole(pid={pid}) sikeres, GetConsoleWindow hwnd={hwnd} title='{self._window_title(hwnd)}'")
                    else:
                        logging.warning(f"[STRESSTOOLS-DEBUG] AttachConsole(pid={pid}) sikeres volt, de GetConsoleWindow NULL-t adott vissza (a konzolnak nincs saját ablaka?).")
                    return hwnd if hwnd else None
                finally:
                    kernel32.FreeConsole()
            except Exception as e:
                logging.warning(f"[STRESSTOOLS] AttachConsole hiba (pid={pid}): {e}")
                return None
            finally:
                self._reattach_own_console(kernel32, keeper_pid)

    def _read_console_screen(self, pid, max_rows=50):
        """Egy konzolos program képernyőjén éppen LÁTHATÓ szöveg kiolvasása (AttachConsole +
        CONOUT$ + ReadConsoleOutputCharacterW), legfeljebb az utolsó max_rows sor. None, ha
        nem sikerült (pl. a folyamat már nem él). Ezzel ellenőrizhető gépelés előtt, hogy a
        várt prompt tényleg megjelent-e - az AttachConsole folyamat-szintű állapotát itt is
        a self._console_attach_lock védi (lásd _find_console_window_for_pid)."""
        GENERIC_READ = 0x80000000
        GENERIC_WRITE = 0x40000000
        FILE_SHARE_READ_WRITE = 0x3
        OPEN_EXISTING = 3
        kernel32 = ctypes.windll.kernel32
        kernel32.AttachConsole.argtypes = [ctypes.wintypes.DWORD]
        kernel32.AttachConsole.restype = ctypes.wintypes.BOOL
        kernel32.CreateFileW.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.DWORD,
                                         ctypes.wintypes.DWORD, ctypes.c_void_p,
                                         ctypes.wintypes.DWORD, ctypes.wintypes.DWORD,
                                         ctypes.wintypes.HANDLE]
        kernel32.CreateFileW.restype = ctypes.wintypes.HANDLE
        kernel32.GetConsoleScreenBufferInfo.argtypes = [ctypes.wintypes.HANDLE,
                                                        ctypes.POINTER(_CONSOLE_SCREEN_BUFFER_INFO)]
        kernel32.GetConsoleScreenBufferInfo.restype = ctypes.wintypes.BOOL
        kernel32.ReadConsoleOutputCharacterW.argtypes = [ctypes.wintypes.HANDLE,
                                                         ctypes.wintypes.LPWSTR,
                                                         ctypes.wintypes.DWORD, _COORD,
                                                         ctypes.POINTER(ctypes.wintypes.DWORD)]
        kernel32.ReadConsoleOutputCharacterW.restype = ctypes.wintypes.BOOL
        invalid_handle = ctypes.wintypes.HANDLE(-1).value
        with self._console_attach_lock:
            keeper_pid = None
            try:
                keeper_pid = self._own_console_keeper_pid()
                kernel32.FreeConsole()
                if not kernel32.AttachConsole(pid):
                    return None
                try:
                    h = kernel32.CreateFileW("CONOUT$", GENERIC_READ | GENERIC_WRITE,
                                             FILE_SHARE_READ_WRITE, None, OPEN_EXISTING, 0, None)
                    if h == invalid_handle:
                        logging.warning(f"[STRESSTOOLS-DEBUG] _read_console_screen(pid={pid}): CONOUT$ megnyitása sikertelen, GetLastError={ctypes.GetLastError()}")
                        return None
                    try:
                        info = _CONSOLE_SCREEN_BUFFER_INFO()
                        if not kernel32.GetConsoleScreenBufferInfo(h, ctypes.byref(info)):
                            return None
                        width = info.dwSize.X
                        last_row = min(info.dwCursorPosition.Y, info.dwSize.Y - 1)
                        first_row = max(0, last_row - max_rows + 1)
                        lines = []
                        for y in range(first_row, last_row + 1):
                            buf = ctypes.create_unicode_buffer(width + 1)
                            n = ctypes.wintypes.DWORD()
                            if kernel32.ReadConsoleOutputCharacterW(h, buf, width, _COORD(0, y), ctypes.byref(n)):
                                lines.append(buf.value[:n.value].rstrip())
                        return "\n".join(lines).rstrip()
                    finally:
                        kernel32.CloseHandle(h)
                finally:
                    kernel32.FreeConsole()
            except Exception as e:
                logging.warning(f"[STRESSTOOLS] Konzol-képernyő olvasási hiba (pid={pid}): {e}")
                return None
            finally:
                self._reattach_own_console(kernel32, keeper_pid)

    @staticmethod
    def _console_screen_is_progressing(old_screen, new_screen):
        """Változott-e a konzol képernyője az előző olvasás óta? Ha igen, a program DOLGOZIK
        (kicsomagol, DISM-mel komponenst telepít, számol), és a prompt-várás türelem-óráját
        újra kell indítani - lásd _auto_answer_console HALADÁS-ALAPÚ TÜRELEM. Az első
        olvasás (old_screen is None) nem számít haladásnak, csak kiindulási állapot."""
        if old_screen is None:
            return False
        return old_screen != new_screen

    @staticmethod
    def _console_is_blocked_on_keypress(screen, expected_prompt):
        """A konzol egy tetszőleges billentyűre vár, de NEM arra a promptra, amit éppen
        keresünk? (Csak az utolsó néhány sort nézzük: egy régebbi, már túllépett "press any
        key" a képernyő tetején nem jelent blokkolást.) A hívó ilyenkor - és csak akkor, ha
        a képernyő már hosszabb ideje mozdulatlan - küld egy Entert. Lásd
        CONSOLE_UNBLOCK_MARKERS."""
        if not screen:
            return False
        tail = "\n".join(screen.splitlines()[-3:]).lower()
        expected = (expected_prompt or '').lower()
        return any(m in tail and m not in expected for m in CONSOLE_UNBLOCK_MARKERS)

    def _auto_answer_console(self, pid, script, task_id=None):
        """Egy konzolos program (pl. Linpack) menüjét navigálja végig automatikusan. A
        'script' (prompt-részlet, válasz, kell-e Enter) hármasok listája: minden válasz
        elküldése ELŐTT kiolvassa a konzol képernyőpufferét (_read_console_screen), és
        megvárja, hogy a várt prompt ténylegesen megjelenjen - csak ezután hozza előtérbe
        az ablakot (SetForegroundWindow) és gépeli be a választ valódi billentyű-esemény
        szimulációval (SendInput). Enter CSAK akkor megy a válasz után, ha a lépés kéri -
        a 'choice'-alapú batch-menüknél egy fölösleges Enter a pufferben ragadva a
        következő 'set /p'-t üres sorral eteti meg, ami az egész batch-et megszakítja
        (lásd LINPACK_PROMPT_SCRIPT kommentje). Üres válasz + Enter-flag = csak Enter
        (pl. "Press any key").

        A prompt-ellenőrzés NEM elhagyható kényelmi extra: vakon, fix időzítéssel gépelve
        egy leterhelt gépen (ahol a menü akár több mp késéssel jelenik meg) a válaszok
        rossz prompthoz érkeznek, a menü-navigáció szétcsúszik, és a teszt el sem indul -
        pontosan ez történt a terepen. A képernyő-olvasással minden válasz garantáltan a
        neki szánt kérdésre megy, késve megjelenő menünél is.

        A begépelést korábban stdin=subprocess.PIPE-pal próbáltuk megoldani, de a Linpack
        ezzel egyáltalán nem indult el - valószínűleg a konzolos bemenet-kezelése (ami
        valódi konzol-bemenetet vár, nem egy egyszerű átirányított pipe-ot) nem tudott mit
        kezdeni a CREATE_NEW_CONSOLE + átirányított stdin kombinációval, és elindulás előtt
        elszállt. A SendInput-os "valódi begépelés" ezt elkerüli, mert a program
        szemszögéből megkülönböztethetetlen attól, mintha egy felhasználó gépelne.

        FÓKUSZVESZTÉS-VÉDELEM: a SendInput mindig az éppen ELŐTÉRBEN lévő ablakba gépel -
        lassú gépen pont a gépelés közben ugrik elő egy másik, párhuzamosan induló
        stressz-program ablaka és lopja el a fókuszt, a karakterek pedig máshova mennek
        (terepen bizonyított hibamód). Ezért minden válasz elküldése UTÁN visszaolvassuk a
        konzol képernyőjét: ha nem változott (a bevitel nem ért célba), a fókuszt
        visszaállítva újragépeljük, legfeljebb 3 körben. Újragépelés előtt frissen újra
        ellenőrizzük a képernyőt - ha időközben mégis megváltozott (csak lassan rajzolt
        újra a leterhelt gép), NEM gépelünk duplán.

        HALADÁS-ALAPÚ TÜRELEM: a prompt-várás időkorlátja NEM fix - minden alkalommal
        újraindul, amikor a konzol képernyője változik (lásd _console_screen_is_progressing).
        Terepen bizonyított hibamód (friss/klónozott Win11 24H2+): a Linpack indulásakor
        észreveszi, hogy a rendszerből hiányzik a WMIC (opcionális feature lett), és DISM-mel
        MAGA TELEPÍTI - ez a képernyőn egy percekig kúszó százalékos csík ("[===  37.8%  ]"),
        ami bőven túllépi a fix STRESS_STEP_TIMEOUT-ot, így az automatizálás elhasalt, pedig
        a program tökéletesen dolgozott. Amíg a képernyő VÁLTOZIK, addig a program csinál
        valamit -> várunk tovább; csak a tényleges MOZDULATLANSÁG számít elakadásnak.
        A CONSOLE_PROMPT_MAX_WAIT abszolút plafon védi a végtelen várakozás ellen.

        Visszatérési érték: True, ha a teljes script sikeresen lement; False bármely
        elakadásnál (a hívó _run_automation_safely ebből tudja az eszköz automatizálásának
        kimenetelét jelenteni a záró összegzéshez)."""
        user32 = self._stress_user32()
        logging.info(f"[STRESSTOOLS-DEBUG] _auto_answer_console indul (pid={pid}, script={script})")
        hwnd = None
        deadline = time.monotonic() + STRESS_STEP_TIMEOUT  # leterhelt (lassú HDD-s) gépen ez percekig is eltarthat
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            hwnd = self._find_console_window_for_pid(pid)
            if hwnd:
                break
            if attempt % 10 == 0:  # kb. 5 mp-enként egy "még mindig keresem" jelzés
                logging.info(f"[STRESSTOOLS-DEBUG] Konzolablak keresése folyamatban (pid={pid}), {attempt}. próba, még nincs meg...")
            time.sleep(0.5)
        if not hwnd:
            logging.warning(f"[STRESSTOOLS] Automatikus bevitel kihagyva - nem található konzolablak (pid={pid})")
            self._debug_dump_pid_windows(pid, "_auto_answer_console: konzolablak sosem került elő")
            return False
        logging.info(f"[STRESSTOOLS-DEBUG] Konzolablak megtalálva (pid={pid}): hwnd={hwnd} title='{self._window_title(hwnd)}'")
        # Az előtérbe hozás a valódi felső szintű ablaknak szól (Windows Terminalban a konzol
        # ablaka csak egy PseudoConsoleWindow, lásd _top_level_window).
        top = self._top_level_window(user32, hwnd)
        if top != hwnd:
            logging.info(f"[STRESSTOOLS] A konzol egy terminál-ablakban fut (pid={pid}): az előtérbe hozás a valódi ablakra megy, hwnd={top} title='{self._window_title(top)}'")
        for prompt, line, needs_enter in script:
            try:
                # Várakozás, amíg a válaszhoz tartozó prompt ténylegesen megjelenik a
                # konzol képernyőjén. Közben az ablak létezését is figyeljük - ha a program
                # bezáródott/összeomlott, ennek itt, konkrét hibaüzenettel kell kiderülnie.
                # Az időkorlát a képernyő UTOLSÓ VÁLTOZÁSÁTÓL számít, nem a várakozás
                # kezdetétől: egy magától telepítő/kicsomagoló program (Linpack -> WMIC
                # DISM-telepítés friss Win11-en) percekig dolgozhat, miközben végig
                # látszik, hogy halad. hard_deadline: abszolút plafon a valódi végtelen
                # ciklus ellen.
                prompt_deadline = time.monotonic() + STRESS_STEP_TIMEOUT
                hard_deadline = time.monotonic() + CONSOLE_PROMPT_MAX_WAIT
                screen = None
                prompt_found = False
                progress_extends = 0
                last_change = time.monotonic()
                unblocks = 0
                poll = 0
                while time.monotonic() < prompt_deadline and time.monotonic() < hard_deadline:
                    poll += 1
                    if not user32.IsWindow(hwnd):
                        logging.warning(f"[STRESSTOOLS-DEBUG] A konzolablak (hwnd={hwnd}, pid={pid}) már NEM létezik a(z) '{prompt}' promptra várva - a program valószínűleg bezáródott/összeomlott. Automatizálás megszakítva. Utolsó ismert képernyőtartalom:\n{screen}")
                        self._debug_dump_pid_windows(pid, f"_auto_answer_console: ablak eltűnt a(z) '{prompt}' promptra várva")
                        return False
                    new_screen = self._read_console_screen(pid)
                    if new_screen is not None:
                        if self._console_screen_is_progressing(screen, new_screen):
                            # A program dolgozik (rajzol) - türelem-óra újraindítása.
                            prompt_deadline = time.monotonic() + STRESS_STEP_TIMEOUT
                            last_change = time.monotonic()
                            progress_extends += 1
                            if progress_extends == 1 or progress_extends % 20 == 0:
                                last_line = (new_screen.splitlines() or ['(üres)'])[-1]
                                logging.info(f"[STRESSTOOLS-DEBUG] A konzol képernyője változik a(z) '{prompt}' promptra várva (pid={pid}, {progress_extends}. hosszabbítás) - a program dolgozik, várunk tovább. Utolsó sor: '{last_line}'")
                        screen = new_screen
                        if prompt.lower() in screen.lower():
                            prompt_found = True
                            break
                        # BEragadt "nyomj egy gombot" képernyő feloldása: ilyenkor a program
                        # NEM dolgozik és nem is fog, amíg nem kap billentyűt - a mi promptunk
                        # sosem jönne el (lásd CONSOLE_UNBLOCK_MARKERS: a Linpack a hiányzó
                        # WMIC miatt pont így állt meg a menü előtt).
                        if (unblocks < CONSOLE_UNBLOCK_MAX
                                and time.monotonic() - last_change >= CONSOLE_UNBLOCK_AFTER
                                and self._console_is_blocked_on_keypress(screen, prompt)):
                            unblocks += 1
                            logging.info(f"[STRESSTOOLS] A konzol egy billentyűre vár (nem a most keresett '{prompt}' promptra), és {CONSOLE_UNBLOCK_AFTER} mp-e mozdulatlan - Enter küldése a folytatáshoz (pid={pid}, {unblocks}/{CONSOLE_UNBLOCK_MAX}).")
                            user32.SetForegroundWindow(top)
                            time.sleep(0.15)
                            self._send_vk(user32, VK_RETURN)
                            last_change = time.monotonic()
                            prompt_deadline = time.monotonic() + STRESS_STEP_TIMEOUT
                    if poll % 10 == 0:  # kb. 5 mp-enként állapotjelzés
                        last_line = screen.splitlines()[-1] if screen else '(nem olvasható)'
                        logging.info(f"[STRESSTOOLS-DEBUG] Még várom a(z) '{prompt}' promptot (pid={pid}, {poll}. próba), a képernyő utolsó sora most: '{last_line}'")
                    time.sleep(0.5)
                if not prompt_found:
                    reason = (f"a képernyő {STRESS_STEP_TIMEOUT} mp-ig mozdulatlan volt"
                              if time.monotonic() < hard_deadline
                              else f"a(z) {CONSOLE_PROMPT_MAX_WAIT} mp-es abszolút várakozási plafon lejárt")
                    logging.warning(f"[STRESSTOOLS] A(z) '{prompt}' prompt nem jelent meg (pid={pid}, {reason}, {progress_extends} haladás-hosszabbítás után), automatizálás megszakítva. A konzol képernyője most:\n{screen}")
                    return False
                logging.info(f"[STRESSTOOLS-DEBUG] Prompt megjelent: '{prompt}' (pid={pid}), válasz begépelése: '{line}'")

                # A válasz begépelése + hatás-ellenőrzés, fókuszvesztés elleni
                # újrapróbálással (max 3 kör) - lásd a docstring FÓKUSZVESZTÉS-VÉDELEM
                # bekezdését. screen_before: a prompt-észleléskori képernyőtartalom, ehhez
                # képest kell változásnak történnie, ha a bevitel célba ért.
                screen_before = screen
                consumed = False
                for type_attempt in range(1, 4):
                    if not user32.IsWindow(hwnd):
                        logging.warning(f"[STRESSTOOLS-DEBUG] A konzolablak (pid={pid}) eltűnt a(z) '{line}' begépelése előtt/közben - automatizálás megszakítva.")
                        return False
                    if type_attempt > 1:
                        # Újragépelés ELŐTT friss ellenőrzés: ha a képernyő időközben mégis
                        # megváltozott (csak lassan rajzolt újra a leterhelt gép), a bevitel
                        # valójában célba ért - duplán gépelni tilos (a fölös billentyű a
                        # KÖVETKEZŐ promptra menne, és szétcsúszna a menü).
                        recheck = self._read_console_screen(pid)
                        if recheck is not None and recheck != screen_before:
                            logging.info(f"[STRESSTOOLS-DEBUG] Újragépelés kihagyva ('{line}', pid={pid}): a képernyő időközben megváltozott, az előző bevitel mégis célba ért (lassú újrarajzolás).")
                            consumed = True
                            break
                        logging.warning(f"[STRESSTOOLS] Bevitel-újrapróbálás ('{line}', pid={pid}): {type_attempt}/3. kör, fókusz visszaállítása és újragépelés...")

                    fg_ok = user32.SetForegroundWindow(top)
                    time.sleep(0.15)
                    actual_fg = user32.GetForegroundWindow()
                    if actual_fg != top:
                        logging.warning(f"[STRESSTOOLS-DEBUG] SetForegroundWindow (hwnd={top}) NEM állította előtérbe a konzolablakot a(z) '{line}' sor előtt! "
                                         f"SetForegroundWindow visszatérési értéke={bool(fg_ok)}, a TÉNYLEGES előtérben lévő ablak most: hwnd={actual_fg} title='{self._window_title(actual_fg)}'. "
                                         f"A bevitel valószínűleg nem ér célba - a hatás-ellenőrzés dönti el.")
                    else:
                        logging.debug(f"[STRESSTOOLS-DEBUG] SetForegroundWindow sikeres, hwnd={top} tényleg előtérben van a(z) '{line}' sor előtt.")

                    all_ok = True
                    for ch in line:
                        if not self._send_unicode_char(user32, ch):
                            all_ok = False
                        time.sleep(0.03)
                    if needs_enter:
                        if not self._send_vk(user32, VK_RETURN):
                            all_ok = False
                    logging.info(f"[STRESSTOOLS] Automatikus bevitel elküldve: '{line}' (Enter={'igen' if needs_enter else 'nem - choice-alapú prompt'}, pid={pid}, {type_attempt}. kör), minden SendInput esemény sikeres={all_ok}")

                    # Hatás-ellenőrzés: célba ért bevitelnél a konzol képernyőjének
                    # változnia kell (choice-menünél a következő menü jelenik meg, set /p-nél
                    # a beírt karakterek visszhangja, pause-nál a program kimenete). Lassú
                    # gépre méretezett türelmi idő: 10 mp.
                    verify_deadline = time.monotonic() + 10
                    while time.monotonic() < verify_deadline:
                        check_screen = self._read_console_screen(pid)
                        if check_screen is not None and check_screen != screen_before:
                            consumed = True
                            break
                        if not user32.IsWindow(hwnd):
                            # Az utolsó lépés (teszt indul) után az ablak tartalma
                            # változik, nem tűnik el - eltűnő ablak menet közben hibát
                            # jelezne, de a kintlévő bevitelt már nem tudjuk megítélni.
                            break
                        time.sleep(0.5)
                    if consumed:
                        break
                    logging.warning(f"[STRESSTOOLS] A(z) '{line}' bevitel után a konzol képernyője NEM változott (pid={pid}, {type_attempt}. kör) - a billentyűk valószínűleg máshova mentek (fókuszvesztés).")
                if not consumed:
                    logging.warning(f"[STRESSTOOLS] A(z) '{line}' bevitel 3 kör után sem ért célba (pid={pid}), automatizálás megszakítva - a Linpack menüjét kézzel kell végigvinni.")
                    self._debug_dump_pid_windows(pid, f"_auto_answer_console: '{line}' bevitel sosem ért célba")
                    return False
            except Exception as e:
                logging.warning(f"[STRESSTOOLS] Automatikus bevitel hiba ('{line}', pid={pid}): {e}")
                return False
        return True

    @staticmethod
    def _text_alternatives(text_or_alts):
        """Egy lépés címke-megadása vagy egyetlen string (pl. 'OK'), vagy alternatívák
        listája (pl. ['Indítás', 'Start'] - HWiNFO nyelvtől függően magyar vagy angol
        feliratú gombja) - ez egységesíti a kettőt egy listává."""
        if isinstance(text_or_alts, (list, tuple, set)):
            return list(text_or_alts)
        return [text_or_alts]

    @staticmethod
    def _normalize_ctrl_text(text):
        """Vezérlő-felirat normalizálása összehasonlításhoz: kisbetűsít, eltávolítja a
        gyorsbillentyű-jelölő '&' karaktereket, és minden szóköz-sorozatot egyetlen
        szóközre von össze. Egyik lépés sem elhagyható, mindkettő valós gépen bizonyított
        hibát javít: a Prime95 Small FFTs rádiógombjának valódi szövege 'Small FFTs
        (tests L1/L2/L&3 caches, ...' (rejtett '&'), a GIMPS üdvözlő gombjáé pedig
        'Just  &Stress Testing' - DUPLA szóközzel a 'Just' után! Anélkül a 'l1/l2/l3',
        illetve a 'Just Stress Testing' keresés soha nem találná meg őket."""
        return ' '.join(text.replace('&', '').lower().split())

    def _find_child_by_text(self, hwnd_parent, text_or_alts, exact=False):
        """Megkeresi a hwnd_parent egy közvetlen gyermek-vezérlőjét (pl. gombot), aminek a
        felirata (kis/nagybetűtől, gyorsbillentyű-jelölő '&'-től és dupla szóközöktől
        függetlenül) tartalmazza a megadott szövegek bármelyikét. exact=True esetén csak a
        TELJES felirat-egyezés számít találatnak - nagyon rövid keresett szövegnél (pl.
        'OK', 'Igen') ez véd a hamis találatoktól: részleges kereséssel bármely '...ok'
        végű magyar felirat (pl. 'ventilátorok' egy HWiNFO szenzorlistában) találat lenne."""
        user32 = self._stress_user32()
        result = {'hwnd': None}
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        needles = [self._normalize_ctrl_text(t) for t in self._text_alternatives(text_or_alts)]

        def _callback(hwnd, _lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                text_lower = self._normalize_ctrl_text(buf.value)
                if any((needle == text_lower if exact else needle in text_lower) for needle in needles):
                    result['hwnd'] = hwnd
                    return False  # megvan, leállítjuk a bejárást
            return True

        try:
            user32.EnumChildWindows(hwnd_parent, WNDENUMPROC(_callback), 0)
        except Exception as e:
            logging.warning(f"[STRESSTOOLS] EnumChildWindows hiba: {e}")
        return result['hwnd']

    def _find_pid_window_with_child_text(self, pid, text_or_alts, timeout=STRESS_STEP_TIMEOUT, exact=False):
        """Megkeresi az adott PID-hez tartozó BÁRMELYIK (nem feltétlenül a legnagyobb)
        felső szintű, látható ablakot, aminek van a megadott feliratú (vagy alternatívák
        egyikének megfelelő) gyermek-vezérlője - pl. egy épp megjelenő modális
        figyelmeztető/megerősítő dialógusablakot a rajta lévő gomb alapján. Eltér a
        _find_window_for_pid-től, ami mindig a legnagyobb ablakot választja - egy
        dialógus viszont jellemzően KISEBB, mint a program főablaka, arra a logika itt
        nem használható. Legfeljebb 'timeout' másodpercig vár, amíg a dialógus megjelenik -
        a hosszú alapérték (STRESS_STEP_TIMEOUT) szándékos: ha a gép egyszerre 4
        stressz-teszt programot (és esetleg egy párhuzamosan futó DISM lekérdezést) indít,
        a rendszer erősen leterhelődhet, és egy dialógus akár percekig is késhet (debug
        logban megfigyelt eset: a FurMark gombja egy ERŐS gépen is 56 mp késéssel került
        elő - egy lassú dual-core + HDD-s gépen a régi 60 mp-es korlát ezért kevés volt).
        Visszaad: (ablak hwnd, gomb hwnd) vagy (None, None)."""
        user32 = self._stress_user32()
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        deadline = time.monotonic() + timeout
        attempt = 0
        logging.info(f"[STRESSTOOLS-DEBUG] _find_pid_window_with_child_text indul: pid={pid}, keresett szöveg(ek)={self._text_alternatives(text_or_alts)}, timeout={timeout}s")
        while time.monotonic() < deadline:
            attempt += 1
            result = {'hwnd': None, 'btn': None}
            windows_seen = []

            def _callback(hwnd, _lparam):
                found_pid = ctypes.wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(found_pid))
                if found_pid.value != pid:
                    return True
                if not user32.IsWindowVisible(hwnd):
                    return True
                windows_seen.append((hwnd, self._window_title(hwnd)))
                btn = self._find_child_by_text(hwnd, text_or_alts, exact=exact)
                if btn:
                    result['hwnd'] = hwnd
                    result['btn'] = btn
                    return False
                return True

            try:
                user32.EnumWindows(WNDENUMPROC(_callback), 0)
            except Exception as e:
                logging.warning(f"[STRESSTOOLS] EnumWindows hiba (pid={pid}): {e}")
            if result['btn']:
                logging.info(f"[STRESSTOOLS-DEBUG] Találat: pid={pid} ablak hwnd={result['hwnd']} title='{self._window_title(result['hwnd'])}', gomb hwnd={result['btn']} ({attempt}. próbálkozásra, {time.monotonic() - (deadline - timeout):.1f}mp alatt)")
                return result['hwnd'], result['btn']
            if attempt % 10 == 0:  # kb. 3 mp-enként egy állapotjelzés
                titles = [f"'{t}'" for _, t in windows_seen] or ['(egy sem)']
                logging.info(f"[STRESSTOOLS-DEBUG] Még keresem (pid={pid}, {attempt}. próba): jelenleg látható ablakai ehhez a PID-hez: {', '.join(titles)} - egyikben sincs '{text_or_alts}' feliratú vezérlő.")
            time.sleep(0.3)
        return None, None

    def _auto_click_sequence(self, pid, steps, task_id=None):
        """Egy GUI program egymás után megjelenő ablakait/dialógusait navigálja végig:
        minden lépésnél megvárja (max 60 mp / lépés), amíg megjelenik egy olyan ablak,
        amiben van a lépéshez tartozó feliratú gomb/rádiógomb (lásd
        _find_pid_window_with_child_text), és BM_CLICK üzenettel megnyomja. Ezzel több
        egymást követő popup is végignyomkodható felügyelet nélkül (pl. FurMark: "GPU
        stress test" -> "GO!" figyelmeztetés; Prime95: "Just Stress Testing" -> "Small
        FFTs" rádiógomb -> "OK"). Egy 'steps'-beli elem lehet egyetlen string, alternatívák
        listája (lokalizált feliratokhoz, pl. HWiNFO "Indítás"/"Start"), vagy dict
        {'labels': [...], 'skip_if_found': [...]} - utóbbinál ha a keresés a
        'skip_if_found' egyik feliratát találja meg (vagyis egy KÉSŐBBI lépés vezérlője
        van már jelen), a lépés kattintás nélkül kimarad (pl. a Prime95 GIMPS üdvözlője
        csak a legelső indításkor létezik, lásd STRESS_CLICK_SEQUENCES).

        A BM_CLICK-et szándékosan PostMessageW-vel (nem SendMessageW-vel) küldjük: a
        SendMessageW addig blokkol, amíg a gomb kattintás-kezelője lefut - ha viszont a
        gomb egy MODÁLIS dialógust nyit (pl. a FurMark 'GPU stress test' gombja a CAUTION
        figyelmeztetést), a kezelő csak a dialógus bezárásakor tér vissza, vagyis a
        SendMessageW-s hívás beragad, és a következő lépés (a 'GO' megnyomása ugyanazon a
        dialóguson) SOSEM indulna el. Terepen ez konkrétan 50 mp-es beragadásként
        jelentkezett, amit csak az ablak kézi bezárása oldott fel.

        Visszatérési érték: True, ha minden lépés lement ÉS az utolsó kattintás hatása
        visszaigazolódott (_verify_final_click); False bármely elakadásnál - a hívó
        _run_automation_safely ebből jelenti az eszköz automatizálásának kimenetelét a
        start_stress_tests záró összegzéséhez."""
        user32 = self._stress_user32()
        logging.info(f"[STRESSTOOLS-DEBUG] _auto_click_sequence indul: pid={pid}, lépések={steps}")
        last_clicked = None  # (gomb hwnd, labels) - az utoljára TÉNYLEGESEN megnyomott lépés
        for step_idx, step in enumerate(steps, 1):
            if isinstance(step, dict):
                labels = self._text_alternatives(step['labels'])
                skip_markers = self._text_alternatives(step.get('skip_if_found', []))
                optional = bool(step.get('optional'))
                timeout = step.get('timeout', STRESS_STEP_TIMEOUT)
                exact = bool(step.get('exact'))
            else:
                labels = self._text_alternatives(step)
                skip_markers = []
                optional = False
                timeout = STRESS_STEP_TIMEOUT
                exact = False
            logging.info(f"[STRESSTOOLS-DEBUG] {step_idx}/{len(steps)}. lépés keresése: pid={pid}, cél='{labels}'" + (f", kihagyás-jelzők='{skip_markers}'" if skip_markers else "") + (" (opcionális)" if optional else ""))
            hwnd, btn = self._find_pid_window_with_child_text(pid, labels + skip_markers, timeout=timeout, exact=exact)
            if not btn:
                if optional:
                    # Az opcionális lépés dialógusa nem mindig jelenik meg (pl. a HWiNFO
                    # indítás utáni figyelmeztetése) - ha nincs, az nem hiba. A leltár-dump
                    # csak diagnosztika: ha a felugró ablak gombfelirata más, mint amire
                    # számítunk, ebből derül ki, mi volt ott valójában.
                    logging.info(f"[STRESSTOOLS] {step_idx}/{len(steps)}. (opcionális) lépés ('{labels}') nem jelent meg {timeout} mp alatt (pid={pid}) - kihagyva, ez nem hiba.")
                    self._debug_dump_pid_windows(pid, f"_auto_click_sequence: opcionális '{labels}' lépés nem került elő (diagnosztikai leltár, NEM hiba)")
                    continue
                logging.warning(f"[STRESSTOOLS] '{labels}' gomb/dialógus nem található (pid={pid}), automatizálás megszakítva.")
                self._debug_dump_pid_windows(pid, f"_auto_click_sequence: {step_idx}/{len(steps)}. lépés ('{labels}') sosem került elő")
                return False
            btn_text = self._window_title(btn)
            if skip_markers and not any(self._normalize_ctrl_text(l) in self._normalize_ctrl_text(btn_text) for l in labels):
                # A találat a kihagyás-jelző (egy későbbi lépés vezérlője), nem a lépés
                # saját gombja -> a lépés dialógusa ennél a futásnál nem létezik, ugrás
                # tovább kattintás nélkül (a következő lépés ugyanezt a vezérlőt azonnal
                # újra megtalálja és megnyomja).
                logging.info(f"[STRESSTOOLS] {step_idx}/{len(steps)}. lépés ('{labels}') kihagyva (pid={pid}): helyette már a(z) '{btn_text}' vezérlő van jelen - pl. a Prime95 üdvözlő dialógusa csak a legelső indításkor jelenik meg.")
                continue
            try:
                cls = ctypes.create_unicode_buffer(128)
                user32.GetClassNameW(btn, cls, 128)
                posted = user32.PostMessageW(btn, BM_CLICK, 0, 0)
                logging.info(f"[STRESSTOOLS] '{labels}' megnyomva (pid={pid}): gomb hwnd={btn} class='{cls.value}' text='{btn_text}', PostMessageW eredmény={bool(posted)}, ablak='{self._window_title(hwnd)}'.")
                if not posted:
                    logging.warning(f"[STRESSTOOLS-DEBUG] PostMessageW(BM_CLICK) sikertelen (pid={pid}, gomb hwnd={btn}), GetLastError={ctypes.GetLastError()}")
                last_clicked = (btn, labels)
            except Exception as e:
                logging.warning(f"[STRESSTOOLS] Gombnyomási hiba ('{labels}', pid={pid}): {e}")
                return False
            time.sleep(1)  # a következő dialógus (ha van) megjelenéséhez
        # Az utoljára megnyomott lépés hatás-ellenőrzése: a közbülső lépéseknél a következő
        # lépés keresése önmagában visszaigazolás (ha az előző kattintás elveszett, a
        # következő dialógus sosem jelenik meg, és az kiderül a logból), az utolsó
        # kattintásnál viszont senki nem ellenőrizne - pedig terepen előfordult, hogy a
        # HWiNFO 'Indítás' gombjának PostMessage-elt kattintása egy 4 másik stressz-teszttel
        # párhuzamosan terhelt gépen hatástalan maradt, és a startup ablak csak ült ott.
        # (Kihagyott opcionális utolsó lépésnél így a megelőző valódi kattintás ellenőrződik.)
        if last_clicked:
            return self._verify_final_click(pid, last_clicked[0], last_clicked[1])
        return True

    def _verify_final_click(self, pid, btn, labels, retries=3, wait_secs=6):
        """A kattintás-sorozat utolsó gombjának (pl. HWiNFO 'Indítás', Prime95 'OK',
        FurMark 'GO') megnyomása mindig bezárja a saját dialógusát - tehát a gombnak
        rövid időn belül el kell tűnnie (megszűnik vagy láthatatlanná válik). Ha
        'wait_secs' után is látható, a kattintás valószínűleg elveszett: újrapróbáljuk
        PostMessageW-vel, majd ráadásként SendMessageTimeoutW-vel is - utóbbi a régi,
        szinkron kézbesítés (ami a HWiNFO-nál bizonyítottan működött), de korlátos
        várakozással, így modális dialógust nyitó gombnál sem ragadhat be örökre.
        A dupla kattintás veszélytelen: ha az első hatott, a dialógus bezárult, és a
        második már egy halott/láthatatlan gombra megy (no-op)."""
        user32 = self._stress_user32()
        SMTO_NORMAL = 0x0000
        for attempt in range(1, retries + 1):
            deadline = time.monotonic() + wait_secs
            while time.monotonic() < deadline:
                if not user32.IsWindow(btn) or not user32.IsWindowVisible(btn):
                    logging.info(f"[STRESSTOOLS-DEBUG] Utolsó lépés ('{labels}') visszaigazolva (pid={pid}): a gomb/dialógus eltűnt ({attempt}. próbálkozási körben).")
                    return True
                time.sleep(0.5)
            if attempt >= retries:
                break
            logging.warning(f"[STRESSTOOLS] Az utolsó lépés ('{labels}') gombja {wait_secs} mp után is látható (pid={pid}) - a kattintás valószínűleg elveszett, újrapróbálás ({attempt + 1}/{retries}. kör)...")
            try:
                posted = user32.PostMessageW(btn, BM_CLICK, 0, 0)
                smto_result = ctypes.c_size_t(0)
                delivered = user32.SendMessageTimeoutW(btn, BM_CLICK, 0, 0, SMTO_NORMAL, 3000, ctypes.byref(smto_result))
                logging.info(f"[STRESSTOOLS-DEBUG] Újra-kattintás elküldve ('{labels}', pid={pid}): PostMessageW={bool(posted)}, SendMessageTimeoutW kézbesítve={bool(delivered)} (0=timeout/hiba, az üzenet ettől még feldolgozás alatt lehet).")
            except Exception as e:
                logging.warning(f"[STRESSTOOLS] Újra-kattintási hiba ('{labels}', pid={pid}): {e}")
                return False
        logging.warning(f"[STRESSTOOLS] Az utolsó lépés ('{labels}') dialógusa {retries} próbálkozási kör után SEM tűnt el (pid={pid}) - a program valószínűleg nem indult el rendesen.")
        self._debug_dump_pid_windows(pid, f"_verify_final_click: '{labels}' dialógusa nem záródott be")
        return False

    def _stress_user32(self):
        """A stressz-teszt ablak-pozicionáláshoz használt user32 függvényekre explicit
        argtypes/restype-ot állít be (idempotens - hívható többször is). Enélkül egy
        HWND-típusú (64 biten pointer-méretű) paraméter argtypes deklaráció nélküli, sima
        Python int-ként való átadása ctypes-szal 64 bites Windows-on elméletileg hibás
        marshalling-hoz vezethet - ez itt garantáltan helyesen konvertál."""
        user32 = ctypes.windll.user32
        HWND, LPARAM, DWORD, BOOL, RECT, UINT = (ctypes.wintypes.HWND, ctypes.wintypes.LPARAM,
                                                  ctypes.wintypes.DWORD, ctypes.wintypes.BOOL,
                                                  ctypes.wintypes.RECT, ctypes.wintypes.UINT)
        user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(BOOL, HWND, LPARAM), LPARAM]
        user32.EnumWindows.restype = BOOL
        user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(DWORD)]
        user32.GetWindowThreadProcessId.restype = DWORD
        user32.IsWindowVisible.argtypes = [HWND]
        user32.IsWindowVisible.restype = BOOL
        user32.GetWindowTextLengthW.argtypes = [HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowRect.argtypes = [HWND, ctypes.POINTER(RECT)]
        user32.GetWindowRect.restype = BOOL
        user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
        user32.ShowWindow.restype = BOOL
        user32.SetWindowPos.argtypes = [HWND, HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, UINT]
        user32.SetWindowPos.restype = BOOL
        user32.SystemParametersInfoW.argtypes = [UINT, UINT, ctypes.wintypes.LPVOID, UINT]
        user32.SystemParametersInfoW.restype = BOOL
        user32.GetClassNameW.argtypes = [HWND, ctypes.wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        user32.IsIconic.argtypes = [HWND]
        user32.IsIconic.restype = BOOL
        user32.SetForegroundWindow.argtypes = [HWND]
        user32.SetForegroundWindow.restype = BOOL
        user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(_Input), ctypes.c_int]
        user32.SendInput.restype = ctypes.c_uint
        user32.EnumChildWindows.argtypes = [HWND, ctypes.WINFUNCTYPE(BOOL, HWND, LPARAM), LPARAM]
        user32.EnumChildWindows.restype = BOOL
        user32.GetWindowTextW.argtypes = [HWND, ctypes.wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.SendMessageW.argtypes = [HWND, UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
        user32.SendMessageW.restype = ctypes.wintypes.LPARAM
        user32.PostMessageW.argtypes = [HWND, UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
        user32.PostMessageW.restype = BOOL
        user32.SendMessageTimeoutW.argtypes = [HWND, UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM,
                                               UINT, UINT, ctypes.POINTER(ctypes.c_size_t)]
        user32.SendMessageTimeoutW.restype = ctypes.wintypes.LPARAM
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = HWND
        user32.IsWindow.argtypes = [HWND]
        user32.IsWindow.restype = BOOL
        user32.GetAncestor.argtypes = [HWND, UINT]
        user32.GetAncestor.restype = HWND
        user32.IsZoomed.argtypes = [HWND]
        user32.IsZoomed.restype = BOOL
        user32.GetWindowLongW.argtypes = [HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = ctypes.c_long
        user32.MonitorFromPoint.argtypes = [ctypes.wintypes.POINT, DWORD]
        user32.MonitorFromPoint.restype = ctypes.wintypes.HANDLE
        user32.GetMonitorInfoW.argtypes = [ctypes.wintypes.HANDLE, ctypes.POINTER(_MONITORINFO)]
        user32.GetMonitorInfoW.restype = BOOL
        return user32

    def _stress_dwmapi(self):
        """dwmapi a látható ablakkerethez és a "cloaked" (rejtett UWP) ablakok felismeréséhez."""
        dwm = ctypes.windll.dwmapi
        dwm.DwmGetWindowAttribute.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.DWORD,
                                              ctypes.c_void_p, ctypes.wintypes.DWORD]
        dwm.DwmGetWindowAttribute.restype = ctypes.c_long  # HRESULT
        return dwm

    def _frame_rects(self, user32, hwnd):
        """(ablak-rect, LÁTHATÓ keret) bal, felső, jobb, alsó, vagy (None, None), ha az ablak
        már nem olvasható. Win10/11-en az ablakoknak ~7 px láthatatlan átméretező kerete van:
        a látható keretet a DWM adja (DWMWA_EXTENDED_FRAME_BOUNDS). Ha az érték nem értelmes
        (nincs DWM-adat, vagy más DPI-térből jön), a látható keret = az ablak-rect."""
        DWMWA_EXTENDED_FRAME_BOUNDS = 9
        wr = ctypes.wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(wr)):
            return None, None
        win = (wr.left, wr.top, wr.right, wr.bottom)
        fr = ctypes.wintypes.RECT()
        try:
            hr = self._stress_dwmapi().DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(fr), ctypes.sizeof(fr))
        except Exception as e:
            logging.debug(f"[STRESSTOOLS] DwmGetWindowAttribute nem elérhető (hwnd={hwnd}): {e}")
            return win, win
        vis = (fr.left, fr.top, fr.right, fr.bottom)
        margins = (vis[0] - win[0], vis[1] - win[1], win[2] - vis[2], win[3] - vis[3])
        if hr != 0 or not all(0 <= m <= 32 for m in margins):
            return win, win
        return win, vis

    def _primary_work_area(self, user32):
        """A fő monitor munkaterülete (tálca nélkül) a hívó szál DPI-terében: (bal, felső, jobb, alsó).
        Tartalék: SPI_GETWORKAREA. None, ha egyik sem olvasható."""
        MONITOR_DEFAULTTOPRIMARY, SPI_GETWORKAREA = 1, 0x0030
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(mi)
        hmon = user32.MonitorFromPoint(ctypes.wintypes.POINT(0, 0), MONITOR_DEFAULTTOPRIMARY)
        if hmon and user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcWork
        else:
            r = ctypes.wintypes.RECT()
            if not user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0):
                return None
        return (r.left, r.top, r.right, r.bottom) if r.right > r.left and r.bottom > r.top else None

    def _top_level_window(self, user32, hwnd):
        """A ténylegesen látható, mozgatható felső szintű ablak.

        Win11-en a konzolos program (Linpack) alapból a Windows Terminalban nyílik. Ilyenkor
        a GetConsoleWindow egy PseudoConsoleWindow-t ad, ami a WT-ablak tulajdonában áll, és
        látható is: azt mozgatva a WT-ablak NEM mozdul (mérve, WT 1.25, 2026-10-16), a valódi
        ablakot pedig a _minimize_other_windows tálcára tette. A valódi ablak a
        gyökér-tulajdonos (GA_ROOTOWNER). Klasszikus konzolnál és GUI-főablaknál ez maga az ablak."""
        GA_ROOTOWNER = 3
        return (user32.GetAncestor(hwnd, GA_ROOTOWNER) if hwnd else None) or hwnd

    def _window_in_place(self, user32, hwnd, x, y):
        """A látható keret bal-felső sarka (x, y)-on van-e (±8 px). (ok, látható keret)."""
        _win, vis = self._frame_rects(user32, hwnd)
        return bool(vis) and abs(vis[0] - x) <= 8 and abs(vis[1] - y) <= 8, vis

    def _place_window(self, user32, hwnd, x, y, w, h, to_bottom):
        """Az ablak LÁTHATÓ keretét az (x, y, w, h) téglalapba teszi (a láthatatlan keretet
        ráhagyva, így a negyedek rés nélkül illeszkednek). A verdikt a VISSZAOLVASOTT bal-felső
        sarok, nem a SetWindowPos visszatérési értéke. Legfeljebb 3 próba: egy program a
        mozgatás után a saját helyére is visszaugorhat, egy másik DPI-jű monitorról áthozott
        ablak pedig az első mozgatás után méretezi át magát.
        to_bottom: méret nélkül, z-sorrendben legalulra (FurMark). Visszatérés: (ok, próba, rect)."""
        HWND_BOTTOM, SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE, SW_SHOWNOACTIVATE = 1, 0x0001, 0x0004, 0x0010, 4
        flags = SWP_NOACTIVATE | (SWP_NOSIZE if to_bottom else SWP_NOZORDER)
        vis = None
        for attempt in range(1, 4):
            # Csak a kis méretű/teljes méretű ablakot állítjuk vissza, és aktiválás nélkül:
            # a régi SW_RESTORE minden próbánál előtérbe rántotta az ablakot.
            if user32.IsIconic(hwnd) or user32.IsZoomed(hwnd):
                user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
            win, vis = self._frame_rects(user32, hwnd)
            if not win:
                return False, attempt, None  # az ablak közben bezárult
            ml, mt, mr, mb = vis[0] - win[0], vis[1] - win[1], win[2] - vis[2], win[3] - vis[3]
            user32.SetWindowPos(hwnd, HWND_BOTTOM if to_bottom else 0, x - ml, y - mt,
                                (w + ml + mr) if w else 0, (h + mt + mb) if h else 0, flags)
            time.sleep(0.5)
            ok, vis = self._window_in_place(user32, hwnd, x, y)
            if ok:
                return True, attempt, vis
        return False, 3, vis

    def _find_window_for_pid(self, pid):
        """Megkeresi az adott PID-hez tartozó legnagyobb (kliens-terület szerint), látható,
        címsoros felső szintű ablakot - ha egy folyamatnak több ablaka/rejtett segédablaka
        is van, a legnagyobbat tekintjük a "fő" ablaknak."""
        user32 = self._stress_user32()
        result = {'hwnd': None, 'area': -1}
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

        def _callback(hwnd, _lparam):
            found_pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(found_pid))
            if found_pid.value != pid:
                return True
            if not user32.IsWindowVisible(hwnd):
                return True
            if user32.GetWindowTextLengthW(hwnd) == 0:
                return True
            rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            area = (rect.right - rect.left) * (rect.bottom - rect.top)
            if area > result['area']:
                result['area'] = area
                result['hwnd'] = hwnd
            return True

        try:
            user32.EnumWindows(WNDENUMPROC(_callback), 0)
        except Exception as e:
            logging.warning(f"[STRESSTOOLS] EnumWindows hiba (pid={pid}): {e}")
        if result['hwnd']:
            logging.debug(f"[STRESSTOOLS-DEBUG] _find_window_for_pid(pid={pid}) -> hwnd={result['hwnd']} title='{self._window_title(result['hwnd'])}' area={result['area']}")
        else:
            logging.debug(f"[STRESSTOOLS-DEBUG] _find_window_for_pid(pid={pid}) -> nincs látható, feliratos felső szintű ablak.")
        return result['hwnd']

    def _find_main_window_for_pid(self, pid, timeout=STRESS_MAIN_WINDOW_WAIT):
        """Mint a _find_window_for_pid, de MEGVÁRJA a valódi főablakot: amíg csak egy
        betöltő/elemző splash van kint (STRESS_SPLASH_TITLE_MARKERS), vagy egyáltalán nincs
        látható ablak, tovább pollozunk.

        Terepen bizonyított (2026-07, X670): a HWiNFO64 az indítás után 20-60 mp-ig a
        szenzorokat deríti fel, közben csak egy "Elemzés..." dialógusa látszik, a főablaka
        még nem létezik. Az elrendezés emiatt az egyik futásnál semmit nem talált, a
        másiknál a pici "Elemzés..." dobozt tette a negyedbe a szenzor-ablak helyett.

        Időtúllépéskor a legutóbb talált ablakkal térünk vissza (jobb egy splash, mint a
        semmi), ha az sem volt, None-nal - a hívó ilyenkor a szokásos figyelmeztetést adja."""
        deadline = time.monotonic() + timeout
        last = None
        waited_logged = False
        while True:
            hwnd = self._find_window_for_pid(pid)
            if hwnd:
                last = hwnd
                title = (self._window_title(hwnd) or '').lower()
                if not any(m in title for m in STRESS_SPLASH_TITLE_MARKERS):
                    return hwnd
                if not waited_logged:
                    logging.info(f"[STRESSTOOLS-DEBUG] A pid={pid} még csak a betöltő képernyőjét mutatja ('{title}') - várunk a valódi főablakra (max {timeout} mp).")
                    waited_logged = True
            if time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        logging.warning(f"[STRESSTOOLS] A pid={pid} főablaka {timeout} mp alatt sem jött elő; a pozicionálás a legutóbb látott ablakkal folytatódik (hwnd={last}).")
        return last

    def _position_stress_windows(self, pid_map, task_id='stress'):
        """A négy stressz-teszt ablakot rendezi a fő monitor hasznos területén (tálca
        nélkül) négy negyedbe: FurMark bal-fent, Prime95 jobb-fent, Linpack bal-lent,
        HWiNFO jobb-lent. Az utóbbi 3 a maga negyedére van méretezve; a FurMarkot viszont
        NEM méretezzük át - a render-felülete fix (a kiválasztott felbontáshoz kötött)
        belső méretű, egy kényszerített átméretezés csak levágja/eltolja a képet (pl. az
        FPS-kijelzést), nem skálázza. Ehelyett natív méretben a bal-felső sarokba TOLJUK
        (SWP_NOSIZE - a mozgatás nem vágja a képet, csak az átméretezés) és z-sorrendben
        legalulra küldjük: így a bal-felső negyedben pont a FurMark látszik (a bal-felső
        sarka, FPS-kijelzéssel), a másik 3 negyedet pedig a fölé rendezett ablakok fedik.
        A végén minden egyéb (nem ide tartozó, pl. a DriverVarázsló saját ablaka vagy egy
        program nyitva maradt beállító-dialógusa) ablakot tálcára teszünk, hogy tiszta
        legyen a képernyő.
        pid_map: {STRESS_TOOLS kulcs: pid}. A hiányzó/‑1 (UAC) PID-ű vagy meg nem található
        ablakú tételeket egyszerűen kihagyja, nem buktatja el a többit.

        UNIVERZÁLIS RÉSZLETEK (2026-10-16):
        - minden ablaknál a valódi felső szintű ablakot mozgatjuk (_top_level_window - Windows
          Terminalban futó Linpack), és a visszaolvasott hely a verdikt (_place_window): ami nem
          került a helyére, arra nem írjuk ki, hogy "elrendezve";
        - per-monitor DPI-tudatos szálkontextusban fut: a GUI csak rendszer-DPI-tudatos, a CLI
          egyáltalán nem, egy nagyított (125/150%) kijelzőn a koordináták különben
          virtualizáltak; így a munkaterület, az ablak-rect és a DWM-keret is fizikai pixel;
        - a negyedekbe a LÁTHATÓ keret kerül (a ~7 px láthatatlan keretet ráhagyjuk), rés nélkül;
        - pár mp múlva egy ellenőrző kör a később visszaugró ablakokat újra a helyére teszi."""
        user32 = self._stress_user32()
        prev_ctx = None
        try:
            user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            # -4 = PER_MONITOR_AWARE_V2 (Win10 1703+), -3 = PER_MONITOR_AWARE (1607+)
            prev_ctx = (user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
                        or user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-3)))
        except Exception as e:
            logging.debug(f"[STRESSTOOLS] SetThreadDpiAwarenessContext nem elérhető: {e}")
        if not prev_ctx:
            logging.info("[STRESSTOOLS] Az ablak-elrendezés a folyamat saját DPI-módjában fut (per-monitor szálkontextus nem állítható be).")
        try:
            self._arrange_stress_windows(user32, pid_map, task_id)
        finally:
            if prev_ctx:
                user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(prev_ctx))

    def _arrange_stress_windows(self, user32, pid_map, task_id):
        """A _position_stress_windows törzse, már a per-monitor DPI-kontextusban."""
        area = self._primary_work_area(user32)
        if not area:
            logging.warning("[STRESSTOOLS] A fő monitor munkaterülete nem olvasható - ablak-elrendezés kihagyva.")
            self.emit('task_progress', {'task': task_id, 'log': '⚠️ A képernyő mérete nem olvasható, az ablakok elrendezése kimaradt.'})
            return
        left, top, right, bottom = area
        w = (right - left) // 2
        h = (bottom - top) // 2
        logging.info(f"[STRESSTOOLS] Ablak-elrendezés: munkaterület={area}, negyed={w}x{h}")

        # A sorrend itt számít: a FurMarkot kell ELŐSZÖR HWND_BOTTOM-ra küldeni, utána a
        # többit SWP_NOZORDER-rel (érintetlenül hagyva a z-sorrendjüket) - így garantált,
        # hogy a másik 3 a FurMark FÖLÖTT marad, nem kell nekik explicit HWND_TOP.
        # A furmark bejegyzésnél w/h nem releváns (SWP_NOSIZE miatt), az x/y a bal-felső
        # sarok: natív méretben oda toljuk, hogy a bal-felső negyedben ő látsszon.
        layout = [
            ('furmark', left, top, None, None, True),
            ('prime95', left + w, top, w, h, False),
            ('linpack', left, top + h, w, h, False),
            ('hwinfo', left + w, top + h, w, h, False),
        ]

        placed = []  # (név, hwnd, x, y, w, h, hátra, az első körben a helyére került-e)
        for key, x, y, ww, hh, send_back in layout:
            pid = pid_map.get(key)
            display_name = STRESS_TOOLS[key][0]
            if not pid or pid <= 0:
                continue  # nem indult el, vagy UAC-os indítás volt (nincs PID)
            # A Linpack konzolablakát a conhost.exe "birtokolja" más PID alatt, ezért azt
            # nem a sima PID-alapú EnumWindows-szal (_find_window_for_pid), hanem
            # AttachConsole-lal (_find_console_window_for_pid) keressük meg. A többinél a
            # _find_main_window_for_pid vár a valódi főablakra (a HWiNFO percekig csak az
            # "Elemzés..." splash-t mutatja - lásd ott).
            hwnd = self._find_console_window_for_pid(pid) if key == 'linpack' else self._find_main_window_for_pid(pid)
            if not hwnd:
                logging.warning(f"[STRESSTOOLS] Nem található ablak a pozicionáláshoz: {display_name} (pid={pid})")
                self.emit('task_progress', {'task': task_id, 'log': f'⚠️ {display_name}: nem található ablak a pozicionáláshoz.'})
                self._debug_dump_pid_windows(pid, f"_position_stress_windows: {display_name} ablaka nem található")
                continue
            found = hwnd
            hwnd = self._top_level_window(user32, hwnd)
            cls_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls_buf, 256)
            try:
                # FurMark: méretet nem változtatunk (az vágná a render-képet), csak a bal-felső
                # sarokba toljuk és z-sorrendben legalulra - lásd a docstringet.
                ok, attempts, got = self._place_window(user32, hwnd, x, y, ww, hh, send_back)
                # Akkor sem tesszük tálcára, ha nem került a helyére: ez a program egyik ablaka.
                placed.append((display_name, hwnd, x, y, ww, hh, send_back, ok))
                where = (f"hwnd={hwnd} osztály='{cls_buf.value}'" + (f" (a konzol saját ablaka: hwnd={found})" if found != hwnd else '')
                         + f", cél=({x},{y},{ww}x{hh}), látható keret={got}, {attempts}. próba, hátra={send_back}")
                if ok:
                    logging.info(f"[STRESSTOOLS] Ablak elrendezve: {display_name} ({where})")
                    self.emit('task_progress', {'task': task_id, 'log': f'🪟 {display_name} elrendezve.'})
                else:
                    logging.warning(f"[STRESSTOOLS] Az ablak 3 próba után sem került a helyére, az ellenőrző kör újrapróbálja: {display_name} ({where})")
            except Exception as e:
                logging.warning(f"[STRESSTOOLS] Pozicionálási hiba ({display_name}): {e}", exc_info=True)

        self._minimize_other_windows([p[1] for p in placed], task_id=task_id)

        # Ellenőrző kör: egy program a mozgatás után pár mp-cel is visszaugorhat a saját mentett
        # helyére (vagy a minimalizálás rendezi át a z-sorrendet) - ami nincs a helyén, újra oda.
        for _ in range(3):
            if self._check_cancel():
                return
            time.sleep(1)
        for display_name, hwnd, x, y, ww, hh, send_back, first_ok in placed:
            if not user32.IsWindow(hwnd):
                logging.warning(f"[STRESSTOOLS] {display_name}: az ablak az elrendezés után bezárult (hwnd={hwnd}).")
                continue
            in_place, vis = self._window_in_place(user32, hwnd, x, y)
            # A FurMarkot a helyén is újra legalulra küldjük: a hely jó lehet, a z-sorrend nem.
            if in_place and not send_back:
                if not first_ok:
                    self.emit('task_progress', {'task': task_id, 'log': f'🪟 {display_name} elrendezve.'})
                continue
            try:
                ok, attempts, got = self._place_window(user32, hwnd, x, y, ww, hh, send_back)
            except Exception as e:
                logging.warning(f"[STRESSTOOLS] Pozicionálási hiba az ellenőrző körben ({display_name}): {e}", exc_info=True)
                ok, attempts, got = False, 0, vis
            if ok:
                if in_place:
                    logging.info(f"[STRESSTOOLS] {display_name}: az ellenőrző kör újra legalulra küldte (látható keret={got}).")
                else:
                    logging.info(f"[STRESSTOOLS] {display_name}: az ablak elmozdult ({vis}), az ellenőrző kör visszatette ({got}, {attempts}. próba).")
                if not first_ok:
                    self.emit('task_progress', {'task': task_id, 'log': f'🪟 {display_name} elrendezve.'})
            else:
                logging.warning(f"[STRESSTOOLS] {display_name}: az ablak az ellenőrző kör után sincs a helyén (cél=({x},{y}), látható keret={got}).")
                self.emit('task_progress', {'task': task_id, 'log': f'⚠️ {display_name}: az ablakot nem sikerült a helyére tenni.'})

    def _minimize_other_windows(self, keep_hwnds, task_id='stress'):
        """Minden egyéb látható, címsoros felső szintű ablakot tálcára tesz (minimalizál),
        KIVÉVE a keep_hwnds-ben szereplőket - hogy a stressz teszt elrendezése után tiszta
        legyen a képernyő (ez a DriverVarázsló saját ablakát és pl. egy program nyitva
        maradt beállító-dialógusát is érinti). A rendszer héj-ablakait (tálca, asztal)
        osztálynév alapján kihagyjuk, nehogy azokat is "minimalizáljuk".

        SW_SHOWMINNOACTIVE, nem SW_MINIMIZE: az utóbbi a sorban KÖVETKEZŐ ablakot aktiválja, ami
        a z-sorrend elejére ugrik - ez a legalulra küldött, natív méretű FurMarkot a másik három
        fölé hozhatta. Kimarad a "cloaked" (rejtett UWP) ablak és a TOOLWINDOW: utóbbinak nincs
        tálca-gombja, lekicsinyítve a felhasználó nem tudná visszahozni."""
        SW_SHOWMINNOACTIVE, GWL_EXSTYLE, WS_EX_TOOLWINDOW, DWMWA_CLOAKED = 7, -20, 0x00000080, 14
        SHELL_CLASSES = {'Progman', 'Shell_TrayWnd', 'Shell_SecondaryTrayWnd', 'WorkerW', 'Button'}
        user32 = self._stress_user32()
        try:
            dwm = self._stress_dwmapi()
        except Exception as e:
            dwm = None
            logging.debug(f"[STRESSTOOLS] dwmapi nem elérhető, a rejtett ablakok szűrése kimarad: {e}")
        keep = set(h for h in keep_hwnds if h)
        to_minimize = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

        def _callback(hwnd, _lparam):
            if hwnd in keep:
                return True
            if not user32.IsWindowVisible(hwnd):
                return True
            if user32.GetWindowTextLengthW(hwnd) == 0:
                return True
            if user32.IsIconic(hwnd):
                return True
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, buf, 256)
            if buf.value in SHELL_CLASSES:
                return True
            if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
                return True
            cloaked = ctypes.wintypes.DWORD()
            if dwm and dwm.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0 and cloaked.value:
                return True
            to_minimize.append(hwnd)
            return True

        try:
            user32.EnumWindows(WNDENUMPROC(_callback), 0)
            for hwnd in to_minimize:
                try:
                    user32.ShowWindow(hwnd, SW_SHOWMINNOACTIVE)
                except Exception as e:
                    logging.debug(f"[STRESSTOOLS] Ablak minimalizálás sikertelen (hwnd={hwnd}): {e}")
            logging.info(f"[STRESSTOOLS] {len(to_minimize)} egyéb ablak tálcára helyezve.")
            if to_minimize:
                self.emit('task_progress', {'task': task_id, 'log': f'📥 {len(to_minimize)} egyéb ablak tálcára helyezve.'})
        except Exception as e:
            logging.warning(f"[STRESSTOOLS] Egyéb ablakok minimalizálási hiba: {e}")
