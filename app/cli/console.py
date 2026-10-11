"""DriverVarázsló CLI - konzol-eszköztár: szín, keret, menü, táblázat, folyamatsáv.

MIÉRT VAN EZ (explicit user decision, 2026-08-29): a régi/lassú gépeken a grafikus felület
használhatatlan (a felhasználó szavaival: *"basszus 10 percig gondolkodik hogy mit kene
nyomni"*) - a WebView2 + .NET réteg ott egyszerűen túl nehéz. A CLI viszont sehol nem
akadhat meg: cmd.exe MINDEN Windowson van. Ezért a CLI-nek ugyanazt kell tudnia, mint a
GUI-nak, és közben áttekinthetőnek is kell lennie - ez a modul adja hozzá a "modern"
megjelenést, függőség nélkül.

KÉT KÉPESSÉGET DERÍTÜNK FEL INDULÁSKOR, ÉS MINDKETTŐRE VAN TARTALÉK:

1. SZÍN (ANSI). Windows 10 1511-től a konzol tud ANSI escape-eket, de csak ha bekapcsoljuk
   (`ENABLE_VIRTUAL_TERMINAL_PROCESSING`). Régi cmd-ben ez nem megy, és ott az escape-ek
   NYERS SZEMÉTKÉNT jelennének meg (`←[36m`) - ami pont a régi gépen tenné olvashatatlanná
   a felületet, tehát a legrosszabb helyen. Ha a bekapcsolás nem sikerül, minden színkód
   üres stringgé válik, és a kimenet sima szöveg marad.

2. UNICODE. A keretrajzoló karakterek (│ ─ ╭) a magyar Windows alapértelmezett OEM
   kódlapján (852) NINCSENEK MEG, és `UnicodeEncodeError`-t vagy kérdőjeleket adnának.
   Ezért indulásnál PRÓBÁVAL ellenőrizzük (nem verzió-tippel), és ha nem megy, ASCII
   keretre váltunk (+ - |). A tartalom így is olvasható marad.

A modul semmit nem importál a projektből: önmagában tesztelhető, és nem húz be semmit,
ami egy régi gépen elhasalhatna.
"""
import os
import sys
import shutil
import logging

# ---------------------------------------------------------------------------
# KÉPESSÉG-FELDERÍTÉS
# ---------------------------------------------------------------------------

def _enable_ansi():
    """ANSI escape-ek engedélyezése a Windows konzolon. True, ha megy."""
    if os.environ.get('DV_NO_COLOR'):
        return False
    if not sys.stdout or not hasattr(sys.stdout, 'isatty'):
        return False
    try:
        if not sys.stdout.isatty():
            # Átirányított kimenet (fájlba/pipe-ba): a színkódok ott szemét lennének.
            return False
    except Exception:
        return False
    if os.name != 'nt':
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        # 0x0004 = ENABLE_VIRTUAL_TERMINAL_PROCESSING
        if not k.SetConsoleMode(h, mode.value | 0x0004):
            return False
        return True
    except Exception as e:
        logging.debug(f"[CLI-UI] ANSI engedélyezés sikertelen: {e}")
        return False


def _can_unicode():
    """Ki tudja-e írni a konzol a keretrajzoló karaktereket. PRÓBÁVAL, nem tippel."""
    try:
        enc = (getattr(sys.stdout, 'encoding', None) or 'ascii')
        '─│╭╯█▏✓'.encode(enc)
        return True
    except Exception:
        return False


ANSI = _enable_ansi()
UNICODE = _can_unicode()


def _c(code):
    """Színkód vagy üres string, ha nincs ANSI."""
    return f'\033[{code}m' if ANSI else ''


# A GUI lila témájához igazított paletta (256 színű ANSI, mindenhol elérhető).
RESET = _c('0')
BOLD = _c('1')
DIM = _c('2')
PURPLE = _c('38;5;141')
PURPLE_B = _c('1;38;5;177')
CYAN = _c('38;5;80')
GREEN = _c('38;5;114')
YELLOW = _c('38;5;221')
RED = _c('38;5;203')
GREY = _c('38;5;245')
WHITE = _c('38;5;255')

# Keretrajzolás - Unicode, vagy ASCII tartalék.
if UNICODE:
    BX = {'tl': '╭', 'tr': '╮', 'bl': '╰', 'br': '╯', 'h': '─', 'v': '│',
          'lt': '├', 'rt': '┤', 'dot': '•', 'arrow': '›', 'bar_full': '█', 'bar_empty': '░'}
else:
    BX = {'tl': '+', 'tr': '+', 'bl': '+', 'br': '+', 'h': '-', 'v': '|',
          'lt': '+', 'rt': '+', 'dot': '*', 'arrow': '>', 'bar_full': '#', 'bar_empty': '.'}


def width():
    """A konzol szélessége, ésszerű határok közé szorítva (a cmd alapból 80)."""
    try:
        w = shutil.get_terminal_size((80, 25)).columns
    except Exception:
        w = 80
    return max(60, min(w - 1, 120))


def _plain(text):
    """A szöveg látható hossza (a színkódok nem foglalnak helyet)."""
    if not ANSI:
        return len(text)
    out, i = [], 0
    while i < len(text):
        if text[i] == '\033':
            j = text.find('m', i)
            if j == -1:
                break
            i = j + 1
            continue
        out.append(text[i])
        i += 1
    return len(out)


def _fit(text, w):
    """Szöveg vágása látható hossz szerint (a színkódokat megtartva)."""
    if _plain(text) <= w:
        return text
    out, seen, i = [], 0, 0
    while i < len(text) and seen < w - 1:
        if text[i] == '\033':
            j = text.find('m', i)
            if j == -1:
                break
            out.append(text[i:j + 1])
            i = j + 1
            continue
        out.append(text[i])
        seen += 1
        i += 1
    return ''.join(out) + '…' if UNICODE else ''.join(out) + '.'


# ---------------------------------------------------------------------------
# KIÍRÁS
# ---------------------------------------------------------------------------

def write(text=''):
    """Kiírás, ami SOSEM dob kivételt. Windowed exe-ben a sys.stdout None lehet, és a
    régi kódlapokon egy emoji `UnicodeEncodeError`-t okozna - egy szépítő karakter miatt
    nem szállhat el a program."""
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        try:
            enc = getattr(sys.stdout, 'encoding', None) or 'ascii'
            print(text.encode(enc, 'replace').decode(enc, 'replace'), flush=True)
        except Exception:
            pass
    except Exception:
        pass


def clear():
    """Képernyőtörlés GYEREKFOLYAMAT NÉLKÜL.

    MIÉRT NEM `os.system('cls')`: az egy `cmd.exe`-t indít, ami a windowed exe-ben
    (nincs saját konzol-alrendszer) megbízhatatlan - és ha a gyerek a MI konzolunk
    helyett máshova ír, a képernyő üresen marad. Ez a szakasz a CLI legelső lépése,
    tehát pont itt nem szabad kockáztatni: ha itt hasal el, a felhasználó egy fekete
    ablakot lát, és semmi nem árulja el, mi történt.

    Sorrend: ANSI escape (mindig működik, ha a VT be van kapcsolva) -> Win32
    konzol-puffer törlés -> végső esetben néhány üres sor."""
    if ANSI:
        try:
            sys.stdout.write('\033[2J\033[H')
            sys.stdout.flush()
            return
        except Exception:
            pass
    if os.name == 'nt':
        try:
            import ctypes
            from ctypes import wintypes
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-11)

            class _SBI(ctypes.Structure):
                _fields_ = [('dwSize', wintypes._COORD),
                            ('dwCursorPosition', wintypes._COORD),
                            ('wAttributes', wintypes.WORD),
                            ('srWindow', wintypes.SMALL_RECT),
                            ('dwMaximumWindowSize', wintypes._COORD)]
            info = _SBI()
            if k.GetConsoleScreenBufferInfo(h, ctypes.byref(info)):
                cells = info.dwSize.X * info.dwSize.Y
                written = wintypes.DWORD()
                origin = wintypes._COORD(0, 0)
                k.FillConsoleOutputCharacterW(h, ctypes.c_wchar(' '), cells, origin,
                                              ctypes.byref(written))
                k.FillConsoleOutputAttribute(h, info.wAttributes, cells, origin,
                                             ctypes.byref(written))
                k.SetConsoleCursorPosition(h, origin)
                return
        except Exception as e:
            logging.debug(f"[CLI-UI] Win32 képernyőtörlés sikertelen: {e}")
    write('\n' * 3)


def title(text, subtitle='', color=None):
    """Fejléc-doboz - minden képernyő ezzel kezdődik, hogy tudni lehessen, hol vagyunk."""
    col = color or PURPLE_B
    w = width()
    inner = w - 2
    write(col + BX['tl'] + BX['h'] * inner + BX['tr'] + RESET)
    line = ' ' + text
    write(col + BX['v'] + RESET + BOLD + WHITE + _fit(line, inner).ljust(inner) + RESET + col + BX['v'] + RESET)
    if subtitle:
        write(col + BX['v'] + RESET + GREY + _fit(' ' + subtitle, inner).ljust(inner) + RESET + col + BX['v'] + RESET)
    write(col + BX['bl'] + BX['h'] * inner + BX['br'] + RESET)


def panel(heading, lines, color=None):
    """Bekeretezett információs blokk (állapot, összefoglaló)."""
    col = color or GREY
    w = width()
    inner = w - 2
    write(col + BX['tl'] + BX['h'] * inner + BX['tr'] + RESET)
    if heading:
        write(col + BX['v'] + RESET + BOLD + CYAN + _fit(' ' + heading, inner).ljust(inner) + RESET + col + BX['v'] + RESET)
        write(col + BX['lt'] + BX['h'] * inner + BX['rt'] + RESET)
    for ln in lines:
        write(col + BX['v'] + RESET + _fit(' ' + str(ln), inner).ljust(inner + (len(str(ln)) - _plain(str(ln)))) + col + BX['v'] + RESET)
    write(col + BX['bl'] + BX['h'] * inner + BX['br'] + RESET)


def kv(label, value, label_w=26):
    """Címke-érték sor - az állapot-képernyők alapeleme.

    A címkét VÁGJUK a megadott szélességre: egy hosszabb címke különben nekifutna az
    értéknek (`...KMS_CliAktiválva`), és pont az állapot-képernyők válnának olvashatatlanná."""
    lab = str(label)
    if len(lab) > label_w - 1:
        lab = lab[:label_w - 2] + ('…' if UNICODE else '.')
    write('  ' + GREY + lab.ljust(label_w) + RESET + str(value))


def step(n, total, text):
    write(f"\n{PURPLE_B}[{n}/{total}]{RESET} {BOLD}{text}{RESET}")


def ok(text):
    write(GREEN + ('  ✓ ' if UNICODE else '  [OK] ') + RESET + text)


def warn(text):
    write(YELLOW + ('  ! ' if UNICODE else '  [!] ') + RESET + text)


def err(text):
    write(RED + ('  ✗ ' if UNICODE else '  [X] ') + RESET + text)


def info(text):
    write(CYAN + '  ' + BX['dot'] + ' ' + RESET + text)


def dim(text):
    write(GREY + '    ' + str(text) + RESET)


def progress(done, total, label='', bar_w=None):
    """Egysoros folyamatsáv, helyben frissülve (\\r). Ha nincs ANSI, csak a
    százalék-ugrásokat írjuk ki, hogy ne szemetelje tele a konzolt."""
    total = max(1, int(total or 1))
    done = max(0, min(int(done or 0), total))
    pct = int(done * 100 / total)
    if not ANSI:
        # Régi konzol: nincs helyben-frissítés, csak 10%-onként egy sor.
        if pct % 10 == 0 and done and getattr(progress, '_last', -1) != pct:
            progress._last = pct
            write(f"    {pct}% ({done}/{total}) {label}")
        return
    w = bar_w or max(20, min(40, width() - 40))
    fill = int(w * done / total)
    bar = BX['bar_full'] * fill + BX['bar_empty'] * (w - fill)
    line = f"  {PURPLE}{bar}{RESET} {BOLD}{pct:3d}%{RESET} {GREY}{_fit(label, max(10, width() - w - 14))}{RESET}"
    try:
        sys.stdout.write('\r' + line + '\033[K')
        sys.stdout.flush()
    except Exception:
        pass
    if done >= total:
        write('')


def progress_done():
    """A folyamatsáv lezárása (új sor), ha volt."""
    if ANSI:
        try:
            sys.stdout.write('\r\033[K')
            sys.stdout.flush()
        except Exception:
            pass


def table(headers, rows, widths=None, max_rows=None):
    """Egyszerű táblázat. A `widths` oszlopszélességek; ami nem fér ki, azt levágjuk -
    egy tördelt táblázat 80 oszlopon olvashatatlan."""
    if not rows:
        dim('(nincs megjeleníthető elem)')
        return
    n = len(headers)
    widths = widths or [max(8, int((width() - 4) / n))] * n
    head = '  ' + ' '.join(BOLD + CYAN + _fit(str(h), w).ljust(w) + RESET for h, w in zip(headers, widths))
    write(head)
    write('  ' + GREY + BX['h'] * min(width() - 2, sum(widths) + n - 1) + RESET)
    shown = rows if not max_rows else rows[:max_rows]
    for r in shown:
        cells = []
        for i, w in enumerate(widths):
            val = str(r[i]) if i < len(r) else ''
            cells.append(_fit(val, w).ljust(w + (len(val) - _plain(val))))
        write('  ' + ' '.join(cells))
    if max_rows and len(rows) > max_rows:
        dim(f"... és további {len(rows) - max_rows} elem")


# ---------------------------------------------------------------------------
# BEKÉRÉS
# ---------------------------------------------------------------------------

def ask(prompt, default=''):
    """Szöveg bekérése. Az `input` a menu.py-ban naplózva van (lásd _install_input_logging)."""
    suffix = f" {GREY}[{default}]{RESET}" if default else ''
    try:
        val = input(f"  {PURPLE_B}{BX['arrow']}{RESET} {prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        write('')
        return ''
    return val or default


def confirm(prompt, default=False):
    """Igen/nem kérdés. Magyar i/n, de az y/n is elfogadott (a technikus keze így jár)."""
    hint = 'I/n' if default else 'i/N'
    val = ask(f"{prompt} ({hint})").lower()
    if not val:
        return default
    return val[0] in ('i', 'y', '1')


def pause(text='Nyomj ENTER-t a folytatáshoz'):
    try:
        input(f"\n  {GREY}{text}...{RESET}")
    except (EOFError, KeyboardInterrupt):
        write('')


def menu(items, prompt='Választás', back_label='Vissza'):
    """Számozott menü. `items`: [(kulcs, címke, leírás|None), ...]. A '0' mindig a vissza.

    Visszatérés: a választott kulcs, vagy '0'."""
    write('')
    valid = {'0'}
    for key, label, desc in items:
        if key is None:            # elválasztó / szekciócím
            write(f"\n  {BOLD}{GREY}{label}{RESET}")
            continue
        valid.add(str(key))
        line = f"  {PURPLE_B}{str(key).rjust(2)}{RESET}  {label}"
        write(line)
        if desc:
            write(f"      {GREY}{_fit(desc, width() - 8)}{RESET}")
    write(f"\n   {GREY}0{RESET}  {GREY}{back_label}{RESET}\n")
    while True:
        choice = ask(prompt)
        if choice in valid:
            return choice
        err(f"Nincs ilyen menüpont: '{choice}'")


def pick_indices(prompt, count):
    """Sorszám-lista bekérése (pl. '1,3,5-8' vagy 'mind'). Visszatérés: 0-alapú indexek.

    A tartomány-jelölés (5-8) nem luxus: egy 60 elemű driver-listából kézzel felsorolni a
    törlendőket vesszővel kimerítő és hibalehetőség."""
    raw = ask(prompt).strip().lower()
    if not raw:
        return []
    if raw in ('mind', 'all', '*'):
        return list(range(count))
    out = []
    for part in raw.replace(' ', '').split(','):
        if not part:
            continue
        if '-' in part[1:]:
            a, _, b = part.partition('-')
            if a.isdigit() and b.isdigit():
                out.extend(range(int(a) - 1, int(b)))
            continue
        if part.isdigit():
            out.append(int(part) - 1)
    return [i for i in dict.fromkeys(out) if 0 <= i < count]
