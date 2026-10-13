"""DriverVarázsló CLI - konzol-eszköztár: fejléc, panel, táblázat, nyilas menü, pörgő jelző.

MIÉRT VAN EZ (explicit user decision, 2026-08-29): a GUI-nak a CLI a tartaléka, és ugyanazt
kell tudnia. 2026-10-13 ÓTA "MODERN TERMINÁL-FELÜLET" (explicit user decision: *"legyen
olyan modern cmd-s cucc mint a claude code ... ne csak random kódsorok"*; *"csak win 10 meg
11-en futtatom ... nem érdekel a méret"*): `rich` rajzol (keretes fejléc és panelek,
táblázatok, pörgő jelző, folyamatsáv), `questionary` kérdez (nyilakkal mozgatható menü,
pipálható lista, Igen/Nem választó, alul a kijelölt pont leírása).

A FELÜLET (függvénynevek, a szín-konstansok) VÁLTOZATLAN, mert a menu.py és a bridge.py
erre épül; csak a kirajzolás cserélődött. A szín-konstansok ANSI-kódok maradtak, mert a
menu.py szövegekbe fűzi őket - a kiírás `Text.from_ansi`-val stílussá alakítja őket.

KÉT VÉDŐHÁLÓ, mert egy megjelenítési hiba soha nem akaszthatja meg a munkát:
  - a kiírás SOSEM dob kivételt (`write`);
  - ha a `questionary` (prompt_toolkit) nem tud a konzolra rajzolni, az első hibánál
    WARNING a naplóba, és onnantól sima `input()`-os kérdezés jön (a régi számozott mód).
A Win7/8 támogatás szándékosan nincs (explicit user decision, 2026-10-13).
"""
import os
import sys
import shutil
import logging

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.progress import (Progress, SpinnerColumn, TextColumn, BarColumn,
                           TaskProgressColumn, TimeElapsedColumn)

try:    # ha az exéből hiányozna, a CLI ne álljon le: számozott, begépelős kérdezés jön
    import questionary
    from questionary import Choice, Separator
except Exception as _e:   # noqa: BLE001
    questionary = Choice = Separator = None
    logging.warning(f"[CLI-UI] A questionary nem tölthető be ({_e}) - begépelős kérdezés.")

# ---------------------------------------------------------------------------
# KÉPESSÉG: VT (ANSI) a Windows-konzolon
# ---------------------------------------------------------------------------

def _enable_ansi():
    """ANSI/VT feldolgozás bekapcsolása a konzolon (Win10 1511+). True, ha megy."""
    if os.environ.get('DV_NO_COLOR'):
        return False
    try:
        if not (sys.stdout and sys.stdout.isatty()):
            return False        # átirányított kimenet: ott a színkód szemét lenne
    except Exception:
        return False
    if os.name != 'nt':
        return True
    try:
        import ctypes
        import msvcrt
        k = ctypes.windll.kernel32
        # A sys.stdout SAJÁT handle-je, nem a GetStdHandle: a windowed exe-ben az utóbbi a
        # konzol utólagos megnyitása után is üres lehet (élőben mérve: emiatt esett a rich a
        # 16 színű "legacy Windows" módba, 2026-10-13).
        h = ctypes.c_void_p(msvcrt.get_osfhandle(sys.stdout.fileno()))
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        return bool(k.SetConsoleMode(h, mode.value | 0x0004))  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception as e:
        logging.debug(f"[CLI-UI] ANSI engedélyezés sikertelen: {e}")
        return False


ANSI = _enable_ansi()
UNICODE = True   # Win10+: az ensure_console UTF-8-ra (65001) állítja a konzolt


# A GUI lila témájának színei. ANSI-sztringként, mert a menu.py szövegbe fűzi őket.
def _rgb(r, g, b):
    return f'\033[38;2;{r};{g};{b}m'


RESET = '\033[0m'
BOLD = '\033[1m'
DIM = '\033[2m'
PURPLE = _rgb(180, 100, 255)
PURPLE_B = '\033[1m' + _rgb(205, 150, 255)
CYAN = _rgb(45, 212, 191)
GREEN = _rgb(46, 224, 138)
YELLOW = _rgb(255, 180, 80)
RED = _rgb(255, 107, 91)
GREY = _rgb(140, 132, 168)
WHITE = _rgb(240, 236, 255)

# Ugyanezek rich-stílusként (keret, ikon).
C_ACCENT, C_ACCENT_B = '#b464ff', 'bold #cd96ff'
C_CYAN, C_GREEN, C_YELLOW, C_RED, C_GREY, C_WHITE = '#2dd4bf', '#2ee08a', '#ffb450', '#ff6b5b', '#8c84a8', '#f0ecff'
_STYLE_OF = {PURPLE: C_ACCENT, PURPLE_B: C_ACCENT_B, CYAN: C_CYAN, GREEN: C_GREEN, YELLOW: C_YELLOW,
             RED: C_RED, GREY: C_GREY, DIM: C_GREY, WHITE: C_WHITE}

# A régi ASCII-keret kulcsai (a hívók még hivatkozhatnak rá).
BX = {'tl': '╭', 'tr': '╮', 'bl': '╰', 'br': '╯', 'h': '─', 'v': '│',
      'lt': '├', 'rt': '┤', 'dot': '•', 'arrow': '›', 'bar_full': '█', 'bar_empty': '░'}

_console = None


def _con():
    """A rich Console - lustán, mert az ensure_console csak az import UTÁN köti be a
    konzolt. `markup=False`/`emoji=False`: a naplósorokban `[kód=4]` és `:x:` is van, azokat
    nem szabad jelölésként értelmezni."""
    global _console, ANSI
    if _console is None:
        # Az import pillanatában a konzol még nem biztos, hogy be volt kötve: itt dől el.
        _bind_std_handles()
        ANSI = _enable_ansi()
        _console = Console(highlight=False, markup=False, emoji=False,
                           force_terminal=True if ANSI else None,
                           color_system='truecolor' if ANSI else 'auto',
                           legacy_windows=False if ANSI else None)
    return _console


def _style(color, default):
    return _STYLE_OF.get(color, default) if color else default


def _t(s, style=None):
    """Szöveg -> rich Text; a beágyazott ANSI-kódokból stílus lesz."""
    if isinstance(s, Text):
        return s
    s = '' if s is None else str(s)
    return Text.from_ansi(s, style=style or '') if '\033' in s else Text(s, style=style or '')


def width():
    """A konzol szélessége (naplózáshoz és tördeléshez)."""
    try:
        return shutil.get_terminal_size((100, 30)).columns
    except Exception:
        return 100


def _fit(text, w):
    """Látható hossz szerinti vágás (régi segéd, a hívók még használhatják)."""
    t = _t(text)
    if len(t.plain) <= w:
        return text
    t.truncate(max(1, w), overflow='ellipsis')
    return t.plain


# ---------------------------------------------------------------------------
# KIÍRÁS
# ---------------------------------------------------------------------------

def write(text=''):
    """Kiírás, ami SOSEM dob kivételt (egy szépítő karakter miatt nem állhat le a program)."""
    try:
        _con().print(_t(text))
    except Exception:
        try:
            print(Text.from_ansi(str(text)).plain, flush=True)
        except Exception:
            pass


def _print(renderable):
    try:
        _con().print(renderable)
    except Exception as e:
        logging.debug(f"[CLI-UI] Kirajzolás sikertelen: {e}")


def clear():
    """Képernyő- és görgetőpuffer-törlés (ANSI), gyerekfolyamat nélkül."""
    progress_done()
    try:
        if ANSI:
            sys.stdout.write('\033[2J\033[3J\033[H')
            sys.stdout.flush()
        else:
            _con().clear()
    except Exception:
        write('\n' * 3)


def title(text, subtitle='', color=None):
    """Fejléc-kártya (a Claude Code üdvözlő dobozának mintájára): ✻ + cím, alatta szürkén
    a hely (build, cél, képernyő)."""
    accent = _style(color, C_ACCENT)
    body = Text.assemble(('✻ ', accent), (str(text), f'bold {C_WHITE}'))
    if subtitle:
        body.append('\n')
        body.append_text(_t(subtitle, C_GREY))
    _print(Panel(body, box=box.ROUNDED, border_style=accent, padding=(0, 1), expand=True))


def panel(heading, lines, color=None):
    """Keretes információs blokk; a cím a keret tetején."""
    border = _style(color, C_GREY)
    body = Group(*[_t(ln) for ln in (lines or [''])])
    _print(Panel(body, box=box.ROUNDED, border_style=border, padding=(0, 1), expand=True,
                 title=Text(f' {heading} ', style=f'bold {border}') if heading else None,
                 title_align='left'))


def kv(label, value, label_w=26):
    lab = str(label)
    if len(lab) > label_w - 1:
        lab = lab[:label_w - 2] + '…'
    line = Text('  ')
    line.append(lab.ljust(label_w), style=C_GREY)
    line.append_text(_t(value))
    _print(line)


def step(n, total, text):
    _print(Text.assemble('\n', (f'[{n}/{total}] ', C_ACCENT_B), (str(text), 'bold')))


def _mark(icon, color, text, text_style=''):
    line = Text.assemble(('  ' + icon + ' ', f'bold {color}'))
    line.append_text(_t(text, text_style))
    _print(line)


def ok(text):
    _mark('✔', C_GREEN, text)


def warn(text):
    _mark('▲', C_YELLOW, text)


def err(text):
    _mark('✘', C_RED, text, C_RED)


def info(text):
    _mark('●', C_ACCENT, text)


def dim(text):
    _print(_t('    ' + str(text), C_GREY))


# ---------------------------------------------------------------------------
# ÉLŐ SÁV: pörgő jelző + folyamatsáv (egy példány, a kiírás fölötte görög)
# ---------------------------------------------------------------------------

_prog = None
_task = None


def _live():
    global _prog, _task
    if _prog is None:
        _prog = Progress(
            SpinnerColumn('dots', style=C_ACCENT),
            TextColumn('{task.description}', style=C_WHITE),
            BarColumn(bar_width=28, style='#3a2a55', complete_style=C_ACCENT,
                      finished_style=C_GREEN, pulse_style=C_ACCENT),
            TaskProgressColumn(style=C_ACCENT_B),
            TimeElapsedColumn(),
            console=_con(), transient=True, refresh_per_second=10)
        _task = _prog.add_task('', total=None)
        _prog.start()
    return _prog


def busy(text):
    """Pörgő jelző határozatlan ideig tartó munkára (a szöveg az aktuális lépés)."""
    try:
        _live().update(_task, description=_fit(str(text), max(20, width() - 50)), total=None)
    except Exception as e:
        logging.debug(f"[CLI-UI] Pörgő jelző: {e}")


def progress(done, total, label='', bar_w=None):
    """Folyamatsáv (helyben frissül). A régi hívási forma megmaradt."""
    try:
        total = max(1, int(total or 1))
        done = max(0, min(int(done or 0), total))
        _live().update(_task, description=_fit(str(label or ''), max(20, width() - 50)),
                       total=total, completed=done)
    except Exception as e:
        logging.debug(f"[CLI-UI] Folyamatsáv: {e}")


def progress_done():
    """Az élő sáv lezárása (kérdezés előtt kötelező: a menü nem rajzolhat alá)."""
    global _prog, _task
    if _prog is not None:
        try:
            _prog.stop()
        except Exception:
            pass
        _prog, _task = None, None


def table(headers, rows, widths=None, max_rows=None):
    """Táblázat; ami nem fér ki, az tördel (a `widths` a régi hívási forma, a rich méretez)."""
    if not rows:
        dim('(nincs megjeleníthető elem)')
        return
    tb = Table(box=box.ROUNDED, border_style='#4a3570', header_style=f'bold {C_CYAN}',
               show_lines=False, pad_edge=True, expand=False)
    for i, h in enumerate(headers):
        if i == 0 and str(h) == '#':
            tb.add_column('#', justify='right', style=C_ACCENT_B, no_wrap=True)
        else:
            tb.add_column(str(h), overflow='fold')
    shown = rows if not max_rows else rows[:max_rows]
    for r in shown:
        tb.add_row(*[_t(r[i]) if i < len(r) else Text('') for i in range(len(headers))])
    _print(tb)
    if max_rows and len(rows) > max_rows:
        dim(f"... és további {len(rows) - max_rows} elem")


# ---------------------------------------------------------------------------
# BEKÉRÉS (questionary, input()-os tartalékkal)
# ---------------------------------------------------------------------------

_Q_BROKEN = False
_HINT = '(↑↓ mozgás · Enter választás · Ctrl+C vissza)'


def _qstyle():
    return questionary.Style([
        ('qmark', f'fg:{C_ACCENT} bold'), ('question', 'bold'), ('answer', f'fg:{C_GREEN} bold'),
        ('pointer', f'fg:{C_ACCENT} bold'), ('highlighted', f'fg:#e2c4ff bold'),
        ('selected', f'fg:{C_GREEN}'), ('separator', f'fg:{C_GREY}'),
        ('instruction', f'fg:{C_GREY} italic'), ('disabled', f'fg:#6c6685 italic'),
        ('key', f'fg:{C_ACCENT} bold'), ('desc', f'fg:{C_GREY}'), ('text', ''),
    ])


_STD_BOUND = False


def _bind_std_handles():
    """A Win32 szabványos handle-ök ráállítása a Python-streamekre (egyszer).

    A windowed exe a konzolt utólag kapja (AttachConsole / ensure_console), és csak a
    `sys.stdin/stdout`-ot nyitja meg CONIN$/CONOUT$-ként - a prompt_toolkit viszont a
    billentyűket `GetStdHandle`-ről olvassa, ami egy GUI-folyamatban üres maradhat."""
    global _STD_BOUND
    if _STD_BOUND or os.name != 'nt':
        return
    _STD_BOUND = True
    try:
        import ctypes
        import msvcrt
        k = ctypes.windll.kernel32
        for stream, std in ((sys.stdin, -10), (sys.stdout, -11), (sys.stderr, -12)):
            try:
                k.SetStdHandle(std, ctypes.c_void_p(msvcrt.get_osfhandle(stream.fileno())))
            except Exception as e:
                logging.debug(f"[CLI-UI] SetStdHandle({std}) kimaradt: {e}")
    except Exception as e:
        logging.debug(f"[CLI-UI] A szabványos handle-ök kötése kimaradt: {e}")


def _q(build):
    """Egy questionary-kérdés lefuttatása. None, ha a questionary nem használható (akkor a
    hívó a régi módon kérdez). A Ctrl+C KeyboardInterrupt-ként jön vissza."""
    global _Q_BROKEN
    if _Q_BROKEN or questionary is None:
        return None
    progress_done()
    _bind_std_handles()
    try:
        return ('ok', build().unsafe_ask())
    except KeyboardInterrupt:
        raise
    except Exception as e:
        _Q_BROKEN = True
        logging.warning(f"[CLI-UI] A nyilas menü nem rajzolható ki ({type(e).__name__}: {e}) - "
                        f"innentől számozott, begépelős kérdezés.", exc_info=True)
        return None


def _log_input(prompt, val):
    logging.info(f"[CLI-INPUT] {str(Text.from_ansi(str(prompt)).plain).strip()!r} -> {val!r}")


def _plain_input(prompt, on_eof=''):
    """`input()` (a menu.py naplózza: _install_input_logging). Lezárt bemenetnél (EOF) az
    `on_eof` jön vissza - a menü ilyenkor kilép, nem kérdez végtelen ciklusban."""
    progress_done()
    try:
        return input(f"  {PURPLE_B}›{RESET} {prompt}: ")
    except EOFError:
        write('')
        return on_eof
    except KeyboardInterrupt:
        write('')
        return ''


def ask(prompt, default=''):
    """Szöveg bekérése. Üres válasz = `default`."""
    try:
        r = _q(lambda: questionary.text(str(prompt), default=str(default or ''), qmark='›',
                                        style=_qstyle()))
    except KeyboardInterrupt:
        write('')
        _log_input(prompt, '<Ctrl+C>')
        return default
    if r is None:
        suffix = f" {GREY}[{default}]{RESET}" if default else ''
        return (_plain_input(f"{prompt}{suffix}").strip() or default)
    val = (r[1] or '').strip()
    _log_input(prompt, val)
    return val or default


def confirm(prompt, default=False):
    """Igen/Nem választó (nyilak vagy i/n). Ctrl+C = az alapérték, mint régen."""
    try:
        r = _q(lambda: questionary.select(
            str(prompt), qmark='?', style=_qstyle(), instruction=' ',
            choices=[Choice('Igen', value=True, shortcut_key='i'),
                     Choice('Nem', value=False, shortcut_key='n')],
            default=bool(default), use_shortcuts=True, use_arrow_keys=True))
    except KeyboardInterrupt:
        _log_input(prompt, '<Ctrl+C>')
        return default
    if r is None:
        val = _plain_input(f"{prompt} ({'I/n' if default else 'i/N'})").strip().lower()
        return default if not val else val[0] in ('i', 'y', '1')
    _log_input(prompt, r[1])
    return bool(r[1])


def pause(text='Nyomj Entert a folytatáshoz'):
    progress_done()
    try:
        input(f"\n  {GREY}{text}…{RESET}")
    except (EOFError, KeyboardInterrupt):
        write('')


def menu(items, prompt='Választás', back_label='Vissza'):
    """Nyilas menü. `items`: [(kulcs, címke, leírás|None), ...]; a kulcs None = szakaszcím.
    A leírás a kijelölt pont alatt jelenik meg. Visszatérés: a kulcs, vagy '0' (vissza/Ctrl+C)."""
    try:
        def build():
            choices = []
            for key, label, desc in items:
                if key is None:
                    choices.append(Separator(f'  ── {label} '))
                    continue
                choices.append(Choice(title=[('class:key', f'{str(key):>2}  '), ('class:text', str(label))],
                                      value=str(key), description=desc or None))
            choices.append(Separator(' '))
            choices.append(Choice(title=[('class:key', ' 0  '), ('class:desc', str(back_label))], value='0'))
            return questionary.select(str(prompt), choices=choices, qmark='❯', style=_qstyle(),
                                      instruction=_HINT, show_description=True)
        r = _q(build)
    except KeyboardInterrupt:
        _log_input(prompt, '<Ctrl+C> -> 0')
        return '0'
    if r is not None:
        _log_input(prompt, r[1])
        return r[1] or '0'
    # Tartalék: a régi számozott menü.
    valid = {'0'}
    for key, label, desc in items:
        if key is None:
            write(f"\n  {BOLD}{GREY}{label}{RESET}")
            continue
        valid.add(str(key))
        write(f"  {PURPLE_B}{str(key).rjust(2)}{RESET}  {label}")
        if desc:
            write(f"      {GREY}{desc}{RESET}")
    write(f"\n   {GREY}0  {back_label}{RESET}\n")
    while True:
        choice = _plain_input(prompt, on_eof='0').strip()
        if choice in valid:
            return choice
        err(f"Nincs ilyen menüpont: '{choice}'")


def parse_indices(raw, count):
    """'1,3,5-8' / 'mind' -> 0-alapú indexek (a begépelős tartalék és a hosszú listák útja)."""
    raw = (raw or '').strip().lower()
    if not raw:
        return []
    if raw in ('mind', 'all', '*'):
        return list(range(count))
    out = []
    for part in raw.replace(' ', '').split(','):
        if '-' in part[1:]:
            a, _, b = part.partition('-')
            if a.isdigit() and b.isdigit():
                out.extend(range(int(a) - 1, int(b)))
        elif part.isdigit():
            out.append(int(part) - 1)
    return [i for i in dict.fromkeys(out) if 0 <= i < count]


def pick_indices(prompt, count, labels=None, defaults=None, single=False):
    """Kiválasztás egy listából. Visszatérés: 0-alapú indexek.

    `labels` megadásával pipálható lista (szóköz = pipa, `a` = mind, Enter = kész), illetve
    `single=True`-val egyetlen választás nyilakkal; `defaults` = az előre bepipált indexek.
    Címkék nélkül (vagy ha a nyilas mód nem megy) a régi '1,3,5-8 / mind' begépelés."""
    if labels and len(labels) == count and count:
        def build():
            pre = set(defaults or [])
            choices = [Choice(title=_t(lb).plain, value=i, checked=(i in pre)) for i, lb in enumerate(labels)]
            if single:
                return questionary.select(str(prompt), choices=choices + [Choice('— mégse —', value=-1)],
                                          qmark='❯', style=_qstyle(), instruction=_HINT)
            return questionary.checkbox(
                str(prompt), choices=choices, qmark='❯', style=_qstyle(),
                instruction='(szóköz: pipa · a: mind · i: fordít · Enter: kész · Ctrl+C: mégse)')
        try:
            r = _q(build)
        except KeyboardInterrupt:
            _log_input(prompt, '<Ctrl+C>')
            return []
        if r is not None:
            val = r[1]
            out = ([] if val in (None, -1) else [val]) if single else list(val or [])
            _log_input(prompt, [i + 1 for i in out])
            return out
        # Tartalék: a lista számozva, hogy legyen mit begépelni.
        for i, lb in enumerate(labels, 1):
            write(f"  {PURPLE_B}{str(i).rjust(3)}{RESET}  {lb}")
        out = parse_indices(_plain_input(f"{prompt} (pl. 1,3,5-8{'' if single else ' vagy mind'})"), count)
        return out[:1] if single else out
    return parse_indices(_plain_input(prompt), count)
