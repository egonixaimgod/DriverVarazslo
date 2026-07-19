"""DriverVarázsló GUI - Stabilitás Teszt Win32 UI-automatizálás: dialógus-kattintgatás, konzol-gépelés, ablak-elrendezés."""

# === AUTO-IMPORTS ===
import ctypes
import ctypes.wintypes
import time
import logging
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
            try:
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
            try:
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

        Visszatérési érték: True, ha a teljes script sikeresen lement; False bármely
        elakadásnál (a hívó _run_automation_safely ebből tudja az eszköz automatizálásának
        kimenetelét jelenteni a záró összegzéshez)."""
        user32 = self._stress_user32()
        logging.info(f"[STRESSTOOLS-DEBUG] _auto_answer_console indul (pid={pid}, script={script})")
        hwnd = None
        deadline = time.time() + STRESS_STEP_TIMEOUT  # leterhelt (lassú HDD-s) gépen ez percekig is eltarthat
        attempt = 0
        while time.time() < deadline:
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
        for prompt, line, needs_enter in script:
            try:
                # Várakozás, amíg a válaszhoz tartozó prompt ténylegesen megjelenik a
                # konzol képernyőjén. Közben az ablak létezését is figyeljük - ha a program
                # bezáródott/összeomlott, ennek itt, konkrét hibaüzenettel kell kiderülnie.
                prompt_deadline = time.time() + STRESS_STEP_TIMEOUT
                screen = None
                prompt_found = False
                poll = 0
                while time.time() < prompt_deadline:
                    poll += 1
                    if not user32.IsWindow(hwnd):
                        logging.warning(f"[STRESSTOOLS-DEBUG] A konzolablak (hwnd={hwnd}, pid={pid}) már NEM létezik a(z) '{prompt}' promptra várva - a program valószínűleg bezáródott/összeomlott. Automatizálás megszakítva. Utolsó ismert képernyőtartalom:\n{screen}")
                        self._debug_dump_pid_windows(pid, f"_auto_answer_console: ablak eltűnt a(z) '{prompt}' promptra várva")
                        return False
                    new_screen = self._read_console_screen(pid)
                    if new_screen is not None:
                        screen = new_screen
                        if prompt.lower() in screen.lower():
                            prompt_found = True
                            break
                    if poll % 10 == 0:  # kb. 5 mp-enként állapotjelzés
                        last_line = screen.splitlines()[-1] if screen else '(nem olvasható)'
                        logging.info(f"[STRESSTOOLS-DEBUG] Még várom a(z) '{prompt}' promptot (pid={pid}, {poll}. próba), a képernyő utolsó sora most: '{last_line}'")
                    time.sleep(0.5)
                if not prompt_found:
                    logging.warning(f"[STRESSTOOLS] A(z) '{prompt}' prompt {STRESS_STEP_TIMEOUT} mp alatt sem jelent meg (pid={pid}), automatizálás megszakítva. A konzol képernyője most:\n{screen}")
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

                    fg_ok = user32.SetForegroundWindow(hwnd)
                    time.sleep(0.15)
                    actual_fg = user32.GetForegroundWindow()
                    if actual_fg != hwnd:
                        logging.warning(f"[STRESSTOOLS-DEBUG] SetForegroundWindow (hwnd={hwnd}) NEM állította előtérbe a konzolablakot a(z) '{line}' sor előtt! "
                                         f"SetForegroundWindow visszatérési értéke={bool(fg_ok)}, a TÉNYLEGES előtérben lévő ablak most: hwnd={actual_fg} title='{self._window_title(actual_fg)}'. "
                                         f"A bevitel valószínűleg nem ér célba - a hatás-ellenőrzés dönti el.")
                    else:
                        logging.debug(f"[STRESSTOOLS-DEBUG] SetForegroundWindow sikeres, hwnd={hwnd} tényleg előtérben van a(z) '{line}' sor előtt.")

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
                    verify_deadline = time.time() + 10
                    while time.time() < verify_deadline:
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
        deadline = time.time() + timeout
        attempt = 0
        logging.info(f"[STRESSTOOLS-DEBUG] _find_pid_window_with_child_text indul: pid={pid}, keresett szöveg(ek)={self._text_alternatives(text_or_alts)}, timeout={timeout}s")
        while time.time() < deadline:
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
                logging.info(f"[STRESSTOOLS-DEBUG] Találat: pid={pid} ablak hwnd={result['hwnd']} title='{self._window_title(result['hwnd'])}', gomb hwnd={result['btn']} ({attempt}. próbálkozásra, {time.time() - (deadline - timeout):.1f}mp alatt)")
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
            deadline = time.time() + wait_secs
            while time.time() < deadline:
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
        return user32

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
        ablakú tételeket egyszerűen kihagyja, nem buktatja el a többit."""
        HWND_BOTTOM = 1
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        SWP_NOSIZE = 0x0001
        SW_RESTORE = 9
        SPI_GETWORKAREA = 0x0030
        user32 = self._stress_user32()

        try:
            rect = ctypes.wintypes.RECT()
            user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
            left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
        except Exception as e:
            logging.warning(f"[STRESSTOOLS] Munkaterület lekérdezési hiba, pozicionálás kihagyva: {e}")
            return
        w = (right - left) // 2
        h = (bottom - top) // 2

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

        positioned_hwnds = []
        for key, x, y, ww, hh, send_back in layout:
            pid = pid_map.get(key)
            display_name = STRESS_TOOLS[key][0]
            if not pid or pid <= 0:
                continue  # nem indult el, vagy UAC-os indítás volt (nincs PID)
            # A Linpack konzolablakát a conhost.exe "birtokolja" más PID alatt, ezért azt
            # nem a sima PID-alapú EnumWindows-szal (_find_window_for_pid), hanem
            # AttachConsole-lal (_find_console_window_for_pid) keressük meg.
            hwnd = self._find_console_window_for_pid(pid) if key == 'linpack' else self._find_window_for_pid(pid)
            if not hwnd:
                logging.warning(f"[STRESSTOOLS] Nem található ablak a pozicionáláshoz: {display_name} (pid={pid})")
                self.emit('task_progress', {'task': task_id, 'log': f'⚠️ {display_name}: nem található ablak a pozicionáláshoz.'})
                self._debug_dump_pid_windows(pid, f"_position_stress_windows: {display_name} ablaka nem található")
                continue
            try:
                user32.ShowWindow(hwnd, SW_RESTORE)
                if key == 'furmark':
                    # Méretet nem változtatunk (az vágná a render-képet), de a bal-felső
                    # sarokba toljuk és z-sorrendben legalulra - lásd a docstringet.
                    user32.SetWindowPos(hwnd, HWND_BOTTOM, x, y, 0, 0, SWP_NOACTIVATE | SWP_NOSIZE)
                else:
                    user32.SetWindowPos(hwnd, 0, x, y, ww, hh, SWP_NOACTIVATE | SWP_NOZORDER)
                positioned_hwnds.append(hwnd)
                logging.info(f"[STRESSTOOLS] Ablak elrendezve: {display_name} (hátra={send_back})")
                self.emit('task_progress', {'task': task_id, 'log': f'🪟 {display_name} elrendezve.'})
            except Exception as e:
                logging.warning(f"[STRESSTOOLS] Pozicionálási hiba ({display_name}): {e}")

        self._minimize_other_windows(positioned_hwnds, task_id=task_id)

    def _minimize_other_windows(self, keep_hwnds, task_id='stress'):
        """Minden egyéb látható, címsoros felső szintű ablakot tálcára tesz (minimalizál),
        KIVÉVE a keep_hwnds-ben szereplőket - hogy a stressz teszt elrendezése után tiszta
        legyen a képernyő (ez a DriverVarázsló saját ablakát és pl. egy program nyitva
        maradt beállító-dialógusát is érinti). A rendszer héj-ablakait (tálca, asztal)
        osztálynév alapján kihagyjuk, nehogy azokat is "minimalizáljuk"."""
        SW_MINIMIZE = 6
        SHELL_CLASSES = {'Progman', 'Shell_TrayWnd', 'Shell_SecondaryTrayWnd', 'WorkerW', 'Button'}
        user32 = self._stress_user32()
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
            to_minimize.append(hwnd)
            return True

        try:
            user32.EnumWindows(WNDENUMPROC(_callback), 0)
            for hwnd in to_minimize:
                try:
                    user32.ShowWindow(hwnd, SW_MINIMIZE)
                except Exception as e:
                    logging.debug(f"[STRESSTOOLS] Ablak minimalizálás sikertelen (hwnd={hwnd}): {e}")
            logging.info(f"[STRESSTOOLS] {len(to_minimize)} egyéb ablak tálcára helyezve.")
            if to_minimize:
                self.emit('task_progress', {'task': task_id, 'log': f'📥 {len(to_minimize)} egyéb ablak tálcára helyezve.'})
        except Exception as e:
            logging.warning(f"[STRESSTOOLS] Egyéb ablakok minimalizálási hiba: {e}")
