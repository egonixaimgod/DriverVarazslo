"""DriverVarázsló CLI - a teljes szöveges felület (run_cli_mode).

MIÉRT LÉTEZIK EZ TELJES ÉRTÉKŰ FELÜLETKÉNT (explicit user decision, 2026-08-29): a bolt
régi gépein a grafikus felület használhatatlanul lassú - a felhasználó szavaival *"basszus
10 percig gondolkodik hogy mit kene nyomni"* -, mert a WebView2 + .NET réteg ott túl nehéz.
A CLI viszont mindenhol elindul, ahol van cmd. Ezért a CLI NEM "szűkített változat": minden
funkciót tudnia kell, amit a GUI, és közben áttekinthetőnek kell lennie.

HOGYAN: a menü a `CliApi`-t hajtja, ami ugyanazokat a feature-mixineket kapja meg, mint a
GUI (lásd app/cli/api.py és app/cli/bridge.py). Itt tehát NINCS üzleti logika - csak
menüszerkezet, bekérés és az eredmény megjelenítése. Ha egy funkció viselkedését kell
javítani, az a mixinben/`_core`-ban történik, és a GUI-val együtt javul.

AZ ÚJRAINDÍTÁS-LÁNC IS MŰKÖDIK CLI-BŐL: a `--resume-*` kapcsolókkal induló példány NEM a
menüt hozza fel, hanem azonnal folytatja a láncot (lásd lentebb). Az ütemezett feladat a
`--cli` kapcsolót is továbbviszi (app/gui/autofix.py: _schedule_autofix_resume), különben a
folytatás a grafikus felületet indítaná - pont azon a gépen, ahol az nem használható.
"""

# === AUTO-IMPORTS ===
import os
import sys
import time
import logging
import builtins
from app import common
from app.cli import console as ui
from app.cli.api import CliApi
from app.wusettings_core import AUTOFIX_WU_PAUSE_DAYS
# === /AUTO-IMPORTS ===


def _install_input_logging():
    """Minden CLI input() hívás (menüválasztás, útvonal-megadás, i/n megerősítés)
    bekerüljön a debug logba [CLI-INPUT]-ként - a GUI-oldali UI-akció logolás párja:
    terepi hibakereséskor e nélkül nem rekonstruálható, mit választott a felhasználó.
    A builtins.input-ot cseréljük, így minden modul beolvasása logolódik, hívóhelyenkénti
    módosítás nélkül. Csak a CLI mód futtatja, és idempotens."""
    real_input = builtins.input
    if getattr(real_input, '_dv_logged', False):
        return
    def _logged_input(prompt=''):
        try:
            val = real_input(prompt)
        except (EOFError, KeyboardInterrupt) as e:
            logging.info(f"[CLI-INPUT] {str(prompt).strip()!r} -> megszakítva ({type(e).__name__})")
            raise
        logging.info(f"[CLI-INPUT] {str(prompt).strip()!r} -> {val!r}")
        return val
    _logged_input._dv_logged = True
    builtins.input = _logged_input


# ---------------------------------------------------------------------------
# KÉPERNYŐ-KERET
# ---------------------------------------------------------------------------

def _log_console_state():
    """A konzol TÉNYLEGES állapota a naplóba, a menü megrajzolása előtt.

    Rule 0: egy "fekete ablak, nem történik semmi" jelentés e nélkül megválaszolhatatlan -
    pontosan ez történt 2026-08-31-én. Amit rögzítünk: van-e konzolablak, mik a szabvány
    handle-ök, milyen objektum a sys.stdout, mi a kódolása, tty-e, és sikerül-e ténylegesen
    ráírni. Az utolsó a döntő: ha az `írás-teszt` hibát ad, a stream nem vezet sehova."""
    try:
        import ctypes
        k = ctypes.windll.kernel32
        out = getattr(sys, 'stdout', None)
        try:
            tty = bool(out and out.isatty())
        except Exception:
            tty = None
        proba = 'OK'
        try:
            out.write('')
            out.flush()
        except Exception as e:
            proba = f'HIBA: {e}'
        logging.info(
            f"[CLI-KONZOL] ablak={bool(k.GetConsoleWindow())} "
            f"stdout={type(out).__name__} kódolás={getattr(out, 'encoding', None)} "
            f"tty={tty} írás-teszt={proba} "
            f"handle(out)={k.GetStdHandle(-11)} handle(in)={k.GetStdHandle(-10)} "
            f"kimeneti-kódlap={k.GetConsoleOutputCP()}")
    except Exception as e:
        logging.warning(f"[CLI-KONZOL] Az állapot lekérdezése nem sikerült: {e}")


def _sync(api, fn, *a, **kw):
    """Egy API-hívás lefuttatása ÉS a közben indított háttérszálak bevárása.

    KÖTELEZŐ minden olyan hívásnál, ami után AZONNAL kiolvassuk az eredményét
    (`_cli_take`): a GUI-mixinek egy része saját szálon dolgozik, és enélkül a menü a
    munka befejezése előtt olvasna - üres listát kapna, ami néma hamis eredmény."""
    return api._cli_sync(lambda: fn(*a, **kw))


def _admin_warning():
    """Ha nem adminként futunk, azt KI KELL MONDANI, és nem egyszer.

    Enélkül a legtöbb funkció némán üres eredményt ad: a `dism /Online /Get-Drivers`
    például `Error: 740`-nel elszáll, a lista pedig 0 driverként jelenik meg - a technikus
    ezt "a gépen nincs driver"-ként olvasná. Pontosan az a hibaosztály, amit ez a projekt
    mindenhol üldöz (néma hamis eredmény)."""
    try:
        from app.common import is_admin
        if is_admin():
            return
    except Exception:
        return
    ui.write('')
    ui.panel('NEM RENDSZERGAZDAKÉNT FUTSZ', [
        'A driver-műveletek (listázás, törlés, telepítés) rendszergazdai jogot igényelnek,',
        'és e nélkül ÜRES eredményt adnak — nem azért, mert nincs mit mutatni.',
        '',
        'Zárd be, és indítsd a programot "Futtatás rendszergazdaként" módon.',
    ], color=ui.RED)


def _header(api, screen=''):
    ui.clear()
    sub = f"Build {common.BUILD_NUMBER}"
    if api.target_os_path:
        sub += f"  ·  OFFLINE CÉL: {api.target_os_path}"
    else:
        sub += "  ·  Jelenlegi rendszer (online)"
    if screen:
        sub += f"  ·  {screen}"
    ui.title('DRIVERVARÁZSLÓ — CLI mód', sub)


def _run_screen(api, title_text, fn):
    """Egy művelet lefuttatása saját képernyőn, egységes lezárással."""
    _header(api, title_text)
    api._cli_reset_events()
    try:
        # _cli_sync: a GUI-mixinek egy része saját háttérszálat indít; enélkül a menü a
        # munka befejezése ELŐTT rajzolna eredményt (lásd a bridge magyarázatát).
        api._cli_sync(fn)
    except KeyboardInterrupt:
        ui.warn('Megszakítva.')
    except Exception as e:
        logging.error(f"[CLI] '{title_text}' hibára futott: {e}", exc_info=True)
        ui.err(f"Hiba: {e}")
    ui.pause()


# ---------------------------------------------------------------------------
# 1. DRIVEREK KEZELÉSE
# ---------------------------------------------------------------------------

def _drivers_list(api, all_drivers=False):
    ui.info('Driverek lekérdezése (dism) — ez 15-50 másodperc is lehet...')
    _sync(api, api.load_drivers, all_drivers=all_drivers)
    data = api._cli_take('drivers_loaded', {})
    drivers = data.get('drivers') or []
    if data.get('error'):
        ui.err(str(data['error']))
    return drivers


def _drivers_usage(api, drivers):
    """MELYIK CSOMAGOT HASZNÁLJA MOST A GÉP - ugyanaz a mag, mint a GUI-ban
    (app/driverusage_core.py). A CLI 2026-08-29 óta teljes értékű felület, tehát a
    technikus itt sem törölhet vakon: pont ezen a képernyőn tűnt el annak idején egy
    távoli asztali program drivere úgy, hogy semmi nem szólt róla.

    Hibánál üres dict - a táblázat ilyenkor '?'-et mutat, ami őszinte: az "ismeretlen"
    nem ugyanaz, mint a "nem használt"."""
    return _drivers_usage_info(api, drivers).get('usage') or {}


def _drivers_usage_info(api, drivers):
    """A teljes válasz: használat + GÉP-TÉRKÉP (`machine`), ugyanabból az egy hívásból,
    mint a GUI-ban. Hibánál/offline üres dict."""
    ui.info('Használat felderítése (eszközök, kernel-szolgáltatások, szűrő-driverek)...')
    info = api.get_driver_usage(drivers) or {}
    if info.get('offline'):
        ui.dim('Offline mód: a futó rendszer állapota nem mond semmit a cél-lemez csomagjairól.')
        return {}
    if info.get('error'):
        ui.warn(f"A használat-felderítés nem futott le: {info['error']}")
        return {}
    usage = info.get('usage') or {}
    c = info.get('counts') or {}
    if usage:
        ui.ok(f"{c.get('active', 0)} csomagot használ MOST a gép · "
              f"{c.get('standby', 0)} készenlétben · {c.get('unused', 0)} nem használt")
    if info.get('machine_error'):
        ui.dim(f"A gép felépítése nem rajzolható ki: {info['machine_error']}")
    return info


def _machine_what(machine, d):
    """A "Mire való" szöveg egy csomaghoz (a GUI oszlopának megfelelője)."""
    mi = ((machine or {}).get('packages') or {}).get((d.get('published') or '').lower()) or {}
    return mi.get('what') or d.get('class') or ''


# A három állapot CLI-jele. A GUI-val azonos jelentés, csak szűkebb helyen; ASCII
# tartalékkal, mert a magyar OEM kódlapon a szín-emojik nincsenek meg.
_USAGE_MARK = {
    'active':  (ui.RED, '🔴 HASZNÁLJA', '[!] HASZNALJA'),
    'standby': (ui.YELLOW, '🟡 készenlét', '[~] keszenlet'),
    'unused':  (ui.DIM, '⚪ nem használt', '[ ] nem hasznalt'),
    'unknown': (ui.DIM, '❔ ismeretlen', '[?] ismeretlen'),
}


def _usage_cell(usage, d):
    st = ((usage or {}).get((d.get('published') or '').lower()) or {}).get('state', 'unknown')
    color, uni, ascii_ = _USAGE_MARK.get(st, _USAGE_MARK['unknown'])
    return color + (uni if ui.UNICODE else ascii_) + ui.RESET


def _drivers_table(drivers, printer_infs=None, usage=None, machine=None):
    rows = []
    for i, d in enumerate(drivers, 1):
        mark = ''
        if printer_infs and (d.get('published') or '').lower() in printer_infs:
            mark = ui.YELLOW + ('🖨 ' if ui.UNICODE else 'P ') + ui.RESET
        # A gép-térkép megléte esetén az "Osztály" helyén a KONKRÉT eszköz / rendeltetés
        # áll - a Windows-osztály a driver gyártójának besorolása, nem az alkatrészé.
        what = _machine_what(machine, d) if machine else (d.get('class') or '')
        row = [str(i), mark + (d.get('published') or ''), d.get('original') or '',
               d.get('provider') or '', what, d.get('version') or '']
        if usage is not None:
            row.insert(1, _usage_cell(usage, d))
        rows.append(row)
    if usage is not None:
        ui.table(['#', 'Használat', 'Published', 'Eredeti INF', 'Gyártó',
                  'Mire való' if machine else 'Osztály', 'Verzió'],
                 rows, widths=[4, 15, 12, 18, 16, 26 if machine else 12, 12])
    else:
        ui.table(['#', 'Published', 'Eredeti INF', 'Gyártó', 'Osztály', 'Verzió'],
                 rows, widths=[4, 12, 22, 20, 14, 16])


def _drivers_usage_screen(api):
    """A "mi van használatban" képernyő: állapot szerint csoportosítva, INDOKKAL.

    A GUI Driverek nézetének megfelelője. Az indoklás (melyik eszköz, melyik futó
    szolgáltatás) itt is látszik, nem csak a verdikt - egy besorolás, aminek nem
    látszik az oka, a technikus számára ugyanolyan vak, mint a puszta INF-név volt."""
    drivers = _drivers_list(api, False)
    if not drivers:
        ui.warn('Nincs megjeleníthető driver.')
        return
    info = _drivers_usage_info(api, drivers)
    usage = info.get('usage') or {}
    if not usage:
        return
    machine = info.get('machine')
    if machine:
        _machine_screen(machine, drivers, usage)
        return
    groups = [('active', 'HASZNÁLATBAN — a gép MOST használja', ui.RED),
              ('standby', 'KÉSZENLÉTBEN — tartozik hozzá eszköz/szolgáltatás, de nem aktív', ui.YELLOW),
              ('unused', 'NEM HASZNÁLT — semmi nem hivatkozik rá', ui.DIM)]
    for state, title, color in groups:
        items = [d for d in drivers
                 if ((usage.get((d.get('published') or '').lower()) or {}).get('state')) == state]
        if not items:
            continue
        lines = []
        for d in items:
            e = usage.get((d.get('published') or '').lower()) or {}
            lines.append(f"{d.get('original') or d.get('published')}  ({d.get('provider') or '?'})")
            # Az ok BEHÚZVA a csomag alá: ez a lényegi információ, nem a fájlnév.
            for r in (e.get('reasons') or [])[:2]:
                lines.append(f"    {r}")
        ui.write('')
        ui.panel(f"{title}  [{len(items)} db]", lines, color=color)


_MM_SECTION = {
    'parts': 'A GÉP ALKATRÉSZEI',
    'external': 'CSATLAKOZTATOTT ESZKÖZÖK',
    'software': 'SZOFTVERES DRIVEREK — nincs fizikai eszközük',
    'absent': 'A GÉP NYILVÁNTARTJA, DE MOST NINCS BENNE',
    'nodev': 'NINCS HOZZÁ ESZKÖZ A GÉPBEN — a driver-fájl szerint ezekhez lennének',
}


def _machine_screen(machine, drivers, usage):
    """A GÉP FELÉPÍTÉSE a konzolon: alkatrészenként a hozzá tartozó driverek - a GUI
    gép-térképének megfelelője, UGYANABBÓL az adatból (get_driver_usage 'machine').

    A beépített drivert futtató alkatrész (pl. a processzor) is megjelenik egy sorral:
    a technikusnak/ügyfélnek a teljes gépet kell látnia, nem csak a gyári csomagokat."""
    by_pub = {(d.get('published') or '').lower(): d for d in drivers}
    pkgs = machine.get('packages') or {}
    per_slot = {}
    for pub, mi in pkgs.items():
        if pub in by_pub:
            per_slot.setdefault(mi.get('slot'), []).append(by_pub[pub])
    kind = 'laptop' if machine.get('form') == 'laptop' else 'asztali gép'
    ui.write('')
    ui.panel(f"A GÉP FELÉPÍTÉSE — {machine.get('title') or '?'}",
             [f"{kind} · {machine.get('cpu') or '?'}"
              + (f" · alaplap: {machine['board']}" if machine.get('board') and machine.get('board') != machine.get('title') else ''),
              f"(géptípus forrása: {machine.get('form_source') or '?'})"], color=ui.CYAN)
    last_section = None
    for s in machine.get('slots') or []:
        if s.get('section') != last_section:
            last_section = s.get('section')
            ui.write('')
            ui.write(ui.BOLD + '== ' + _MM_SECTION.get(last_section, last_section) + ' ==' + ui.RESET)
        items = per_slot.get(s['key'], [])
        head = f"{s.get('title')}" + (f": {s['name']}" if s.get('name') and s.get('section') not in ('nodev', 'absent') else '')
        if s.get('badge'):
            head += f" ({s['badge']})"
        lines = []
        if not items:
            devs = s.get('devices') or []
            if devs:
                lines.append('Windows beépített driverrel működik: ' + ', '.join(
                    f"{dv['name']} ({dv.get('inf') or '?'})" for dv in devs[:3]))
        order = {'active': 0, 'standby': 1, 'unknown': 2, 'unused': 3}
        items.sort(key=lambda d: (order.get(((usage.get((d.get('published') or '').lower()) or {}).get('state')), 9),
                                  (d.get('published') or '').lower()))
        for d in items:
            pub = (d.get('published') or '').lower()
            lines.append(f"{_usage_cell(usage, d)}  {d.get('published')}  {pkgs.get(pub, {}).get('what') or ''}"
                         f"  ({d.get('provider') or '?'}, {d.get('version') or '?'})")
        if not lines:
            continue
        ui.write('')
        ui.panel(f"{head}  [{len(items)} driver]", lines,
                 color=ui.DIM if not items else (ui.YELLOW if s.get('section') in ('nodev', 'absent') else ui.CYAN))


def _menu_drivers(api):
    while True:
        _header(api, 'Driverek kezelése')
        c = ui.menu([
            ('1', 'Third-party driverek listázása', 'Amit a gyártók telepítettek (oemNN.inf)'),
            ('2', 'ÖSSZES driver listázása', 'A Windows sajátjaival együtt — veszélyes terület'),
            ('3', 'Driver(ek) törlése', 'Számozott listából, nyomtató-szűrővel'),
            ('4', 'Hardver újraszkennelése', 'pnputil /scan-devices — új eszközök felderítése'),
            ('5', 'Szellemeszközök törlése', 'Nem jelenlévő (ghost) eszközök kitakarítása'),
            ('6', 'Driver-duplikátumok takarítása', 'DriverStore: a régi verziók eltávolítása'),
            ('7', 'Eszközök újrakötése a gyári driverre',
             'Ami a Windows alapdriverén ragadt — újraindítással fejeződik be'),
            ('8', 'Mit használ MOST a gép?',
             'Melyik csomag van használatban, és melyikre nem hivatkozik semmi'),
        ], back_label='Vissza a főmenübe')
        if c == '0':
            return
        if c == '1':
            _run_screen(api, 'Third-party driverek', lambda: _drivers_table(_drivers_list(api, False)))
        elif c == '2':
            _run_screen(api, 'Összes driver', lambda: _drivers_table(_drivers_list(api, True)))
        elif c == '8':
            _run_screen(api, 'Mit használ a gép', lambda: _drivers_usage_screen(api))
        elif c == '3':
            _run_screen(api, 'Driver törlése', lambda: _delete_drivers_flow(api))
        elif c == '4':
            _run_screen(api, 'Hardver újraszkennelés', lambda: _rescan_hw(api))
        elif c == '5':
            _run_screen(api, 'Szellemeszközök', lambda: api.delete_ghost_devices())
        elif c == '6':
            _run_screen(api, 'Driver-duplikátumok', lambda: _dupdrivers_flow(api))
        elif c == '7':
            _run_screen(api, 'Eszközök újrakötése', lambda: _rebind_flow(api))


def _rescan_hw(api):
    ui.info('pnputil /scan-devices futtatása...')
    res = api._run(['pnputil', '/scan-devices'])
    (ui.ok if res.returncode == 0 else ui.warn)(f"Kész (visszatérési kód: {res.returncode})")
    if res.stdout:
        ui.dim(res.stdout.strip()[:600])


def _delete_drivers_flow(api):
    all_mode = ui.confirm('ÖSSZES driver módban (a Windows sajátjait is beleértve)?', False)
    drivers = _drivers_list(api, all_mode)
    if not drivers:
        ui.warn('Nincs megjeleníthető driver.')
        return

    # NYOMTATÓ-SZŰRŐ - ugyanaz a felismerés, mint a GUI-ban és az AutoFixben
    # (wu_core.collect_printer_packages). Alapból BE: egy törölt nyomtató-driver a
    # nyomtató nélkül nem telepíthető vissza.
    hide = ui.confirm('Nyomtató-driverek elrejtése (hogy véletlenül se töröld őket)?', True)
    printer_infs = set()
    if hide:
        ui.info('Nyomtató-driverek felderítése...')
        info = api.get_printer_driver_infs(drivers) or {}
        printer_infs = {p.lower() for p in (info.get('published') or [])}
        if printer_infs:
            ui.ok(f"{len(printer_infs)} nyomtató-driver elrejtve a listáról.")
        else:
            ui.dim('Nem található nyomtató-driver ezen a gépen.')
        drivers = [d for d in drivers if (d.get('published') or '').lower() not in printer_infs]
        if not drivers:
            ui.warn('A szűrés után nem maradt törölhető driver.')
            return

    # HASZNÁLAT SZERINTI SORREND: felül, amit a gép most is használ - ugyanaz a kép,
    # mint a grafikus felületen (ott is ez az alapértelmezett rendezés).
    info = _drivers_usage_info(api, drivers)
    usage = info.get('usage') or {}
    machine = info.get('machine')
    if usage:
        order = {'active': 0, 'standby': 1, 'unknown': 2, 'unused': 3}
        # Ha megvan a gép-térkép, elöl az alkatrész-sorrend (ugyanaz, mint a GUI
        # alapértelmezett csoportosítása), azon belül a használat.
        mpk = (machine or {}).get('packages') or {}
        drivers = sorted(drivers, key=lambda d: (
            (mpk.get((d.get('published') or '').lower()) or {}).get('order', 9999),
            order.get(((usage.get((d.get('published') or '').lower()) or {}).get('state')), 9),
            (d.get('class') or '').lower(), (d.get('published') or '').lower()))

    _drivers_table(drivers, usage=usage if usage else None, machine=machine)
    idx = ui.pick_indices("Törlendő sorszámok (pl. 1,3,5-8 vagy 'mind')", len(drivers))
    if not idx:
        ui.dim('Nincs kijelölve semmi.')
        return
    to_delete = [drivers[i] for i in idx]
    ui.write('')
    ui.panel('TÖRLENDŐ CSOMAGOK', [f"{d.get('published')}  ({d.get('original')}, {d.get('provider')})"
                                   for d in to_delete], color=ui.RED)
    # HASZNÁLATBAN LÉVŐ CSOMAGOK KÜLÖN, NEVESÍTVE. Nem tiltás (minden drivert lehessen
    # törölni), de a technikusnak látnia kell, mit vesz el a géptől.
    act = [d for d in to_delete
           if ((usage.get((d.get('published') or '').lower()) or {}).get('state')) == 'active']
    if act:
        ui.write('')
        ui.panel('FIGYELEM - EZEKET A GÉP MOST IS HASZNÁLJA',
                 [f"{d.get('original') or d.get('published')}  ->  "
                  f"{(usage.get((d.get('published') or '').lower()) or {}).get('summary', '')}"
                  for d in act]
                 + ['', 'A törlésükkel a hozzájuk tartozó eszköz vagy program működése megszűnhet.'],
                 color=ui.RED)
    if not ui.confirm(f"Biztosan törlöd ezt a {len(to_delete)} csomagot?", False):
        ui.dim('Megszakítva.')
        return
    reboot = ui.confirm('Törlés után újraindítás?', False)
    api.delete_drivers([d.get('published') for d in to_delete],
                       list_all=all_mode, reboot=reboot)


def _dupdrivers_flow(api):
    ui.info('DriverStore duplikátumok keresése...')
    _sync(api, api.list_duplicate_drivers)
    data = api._cli_take('dup_drivers_loaded', {})
    groups = data.get('groups') or []
    if not groups:
        ui.ok('Nincs eltávolítható duplikátum.')
        return
    rows, deletable = [], []
    for g in groups:
        for d in (g.get('drivers') or []):
            if d.get('deletable'):
                deletable.append(d)
                rows.append([str(len(deletable)), d.get('published') or '', g.get('original') or '',
                             d.get('version') or '', d.get('date') or ''])
    if not rows:
        ui.ok('Minden duplikátum aktív használatban van — nincs mit törölni.')
        return
    ui.table(['#', 'Published', 'Eredeti INF', 'Verzió', 'Dátum'], rows, widths=[4, 14, 24, 18, 12])
    idx = ui.pick_indices("Törlendő sorszámok (vagy 'mind')", len(deletable))
    if idx and ui.confirm(f"Törlöd a kijelölt {len(idx)} régi csomagot?", False):
        api.delete_duplicate_drivers([deletable[i].get('published') for i in idx])


def _rebind_flow(api):
    ui.panel('MIT CSINÁL EZ', [
        'Azokat az eszközöket, amik a Windows ALAPDRIVERÉN ragadtak, újra felderítteti',
        'a Windowszal — ha van hozzájuk gyári csomag a gépen, azt kapják meg.',
        '',
        'A tárolóvezérlőket, a lemezt, a firmware-t és a rendszerlemez eszközláncát',
        'NEM érinti. Az USB-vezérlő viszont igen: emiatt a gép a VÉGÉN ÚJRAINDUL.',
    ], color=ui.YELLOW)
    if ui.confirm('Indulhat? (a végén a gép újraindul)', False):
        api.rescan_and_rebind_drivers(True)


# ---------------------------------------------------------------------------
# 2. DRIVER KERESÉS ÉS TELEPÍTÉS
# ---------------------------------------------------------------------------

def _menu_hwscan(api):
    while True:
        _header(api, 'Driver keresés és telepítés')
        found = len(getattr(api, 'hw_updates_pool', []) or [])
        ui.kv('Legutóbbi szken találatai', f"{found} db" if found else 'még nem futott')
        c = ui.menu([
            ('1', 'Driver-keresés indítása', 'Windows Update + Microsoft Update Catalog'),
            ('2', 'Találatok listázása', 'Az előző keresés eredménye'),
            ('3', 'Kijelöltek telepítése', 'A listából számozva'),
            ('4', 'Problémás (hibakódos) eszközök', 'Felsorolás + gyors javítás'),
        ], back_label='Vissza a főmenübe')
        if c == '0':
            return
        if c == '1':
            _run_screen(api, 'Driver-keresés', lambda: _hwscan_flow(api))
        elif c == '2':
            _run_screen(api, 'Találatok', lambda: _hwscan_table(api))
        elif c == '3':
            _run_screen(api, 'Telepítés', lambda: _hwscan_install(api))
        elif c == '4':
            _run_screen(api, 'Problémás eszközök', lambda: _hwscan_problems(api))


def _hwscan_flow(api):
    # GYORS MÓD a kézi szkenben is. A kapcsoló 2026-09-02-án bekerült a GUI-szkenbe és a
    # CLI AutoFixbe, de INNEN kimaradt - vagyis a négy helyből háromban létezett, a
    # negyedikben némán mindig teljes keresés futott. Pontosan az a "duplikált logika,
    # aminek egy példánya lemarad" hiba, amiből ennek a projektnek a legtöbb sebe van.
    use_catalog = ui.confirm('Microsoft Update Catalog keresés is? (nem = GYORS MÓD, gyorsabb, '
                             'de a katalógus-only találatok elvesznek)', True)
    deep = True
    if use_catalog:
        # A kérdés szövege 2026-09-03-ig "Mély keresés" volt - a felhasználó szerint
        # érthetetlen, ráadásul mást sugallt (mélyebb lapozás a katalógusban), mint amit
        # tesz. A belső paraméter neve maradt `deep`; itt azt mondjuk ki, mi történik.
        deep = ui.confirm('A MÁR MŰKÖDŐ gyári driverekhez is keressek újabbat? '
                          '(nem = csak a hiányzó és hibás driverek, gyorsabb)', True)
    ui.write('')
    # TÁROLÓ/FIRMWARE: 2026-09-02 óta nem kérdés, hanem rögzített szabály (lásd
    # app/gui/hwscan.py: start_hw_scan). A CLI is teljes értékű felület, ezért ha ITT
    # megmaradt volna a két kérdés, a kapcsoló csak a grafikus felületről tűnt volna el -
    # a felhasználó kérése viszont az volt, hogy SOHA ne lehessen bekapcsolni.
    ui.dim('Tároló- és firmware-driverek: a program ezekre soha nem keres (rossz tároló-driver')
    ui.dim('után a Windows el sem indul, a firmware-írás pedig visszafordíthatatlan).')
    _sync(api, api.start_hw_scan, deep, False, False, use_catalog)
    res = api._cli_take('hw_scan_result', {})
    ui.write('')
    # A `skipped_risky` mező 2026-09-03-án kikerült a payloadból (a "N tároló-eszköz
    # kihagyva" sor a képernyőről is), ezért az azt kiíró ág is elmaradt innen: egy
    # örökre hamis `if` pont az az élőnek látszó holt kód, amit ez a projekt máshol irt.
    ui.kv('Gép', res.get('machine') or res.get('sys_info') or '-')
    if res.get('dev_count'):
        ui.kv('Megvizsgált eszköz', str(res['dev_count']))
    ui.kv('Forrás', res.get('mode') or '-')
    ui.kv('Eltelt idő', res.get('time') or '-')
    # A szűk eredmény OKA - ugyanazok a figyelmeztetések, mint a grafikus felületen.
    if res.get('catalog_skipped') and res.get('wu_failed'):
        ui.warn('EGYIK FORRÁS SEM FUTOTT LE: a Windows Update nem válaszolt, a katalógus '
                'pedig gyors módban ki volt kapcsolva. Futtasd újra teljes kereséssel!')
    elif res.get('wu_failed'):
        ui.warn('A Windows Update nem válaszolt (időtúllépés) — az eredmény csak a katalógusból van.')
    elif res.get('catalog_skipped'):
        ui.dim('Gyors mód: csak a Windows Update-et kérdeztük meg.')
    _hwscan_table(api, res.get('pool'))
    probs = res.get('problems') or []
    if probs:
        ui.write('')
        ui.warn(f"{len(probs)} problémás eszköz — a menü 4. pontja alatt javíthatók.")


def _hwscan_table(api, pool=None):
    pool = pool if pool is not None else (getattr(api, 'hw_updates_pool', []) or [])
    if not pool:
        ui.warn('Nincs találat (vagy még nem futott keresés).')
        return
    rows = []
    for i, p in enumerate(pool, 1):
        flags = []
        if p.get('risky'):
            flags.append(ui.RED + 'KOCKÁZATOS' + ui.RESET)
        if p.get('downgrade'):
            flags.append(ui.YELLOW + 'régebbi' + ui.RESET)
        if p.get('generic_ok'):
            flags.append(ui.GREEN + 'gyári csere' + ui.RESET)
        if p.get('prev_no_bind'):
            flags.append(ui.GREY + 'korábban nem kötött' + ui.RESET)
        rows.append([str(i), p.get('name') or '', p.get('wu_title') or '',
                     p.get('installed_version') or '-', ' '.join(flags)])
    ui.table(['#', 'Eszköz', 'Ajánlott csomag', 'Telepítve', 'Jelzés'],
             rows, widths=[4, 26, 34, 14, 22])
    ui.dim('A "KOCKÁZATOS" és a "korábban nem kötött" tételek NINCSENEK előre kijelölve.')


def _hwscan_install(api):
    pool = getattr(api, 'hw_updates_pool', []) or []
    if not pool:
        ui.warn('Előbb futtass egy keresést (1. menüpont).')
        return
    _hwscan_table(api, pool)
    ui.dim("Alapértelmezés: a nem kockázatos, nem downgrade tételek.")
    raw = ui.ask("Telepítendő sorszámok ('mind', 'ajanlott', vagy pl. 1,3,5-7)", 'ajanlott')
    if raw.lower() in ('ajanlott', 'ajánlott', ''):
        idx = [i for i, p in enumerate(pool)
               if not p.get('risky') and not p.get('downgrade') and not p.get('prev_no_bind')]
        ui.info(f"{len(idx)} ajánlott tétel kijelölve.")
    elif raw.lower() in ('mind', 'all'):
        idx = list(range(len(pool)))
    else:
        idx = []
        for part in raw.replace(' ', '').split(','):
            if '-' in part[1:]:
                a, _, b = part.partition('-')
                if a.isdigit() and b.isdigit():
                    idx.extend(range(int(a) - 1, int(b)))
            elif part.isdigit():
                idx.append(int(part) - 1)
        idx = [i for i in dict.fromkeys(idx) if 0 <= i < len(pool)]
    if not idx:
        ui.dim('Nincs kijelölve semmi.')
        return
    risky = [pool[i] for i in idx if pool[i].get('risky')]
    if risky:
        ui.write('')
        ui.panel('KOCKÁZATOS TÉTELEK A KIJELÖLÉSBEN', [
            f"{p.get('name')} — {p.get('risk_label') or 'kockázatos'}" for p in risky],
            color=ui.RED)
        if not ui.confirm('Ezekkel együtt telepítsem?', False):
            idx = [i for i in idx if not pool[i].get('risky')]
    if idx and ui.confirm(f"Telepítés indítása ({len(idx)} tétel)?", True):
        api.install_selected_wu(idx)


def _hwscan_problems(api):
    res = api._cli_take('hw_scan_result', {}) or {}
    probs = res.get('problems') or getattr(api, '_cli_last_problems', []) or []
    if not probs:
        ui.warn('Nincs eltárolt lista — futtass egy keresést (1. menüpont).')
        return
    api._cli_last_problems = probs
    rows = [[str(i), p.get('name') or '', str(p.get('code') or ''), p.get('desc') or '',
             'igen' if p.get('has_fix') else '-'] for i, p in enumerate(probs, 1)]
    ui.table(['#', 'Eszköz', 'Kód', 'Leírás', 'Javítható'], rows, widths=[4, 28, 6, 40, 10])
    # A TEENDŐ IS KIÍRÓDIK, NEM CSAK A HIBAKÓD (2026-09-03, explicit user decision:
    # "ez mit jelent pl h code 24? irja mar le a program h mit kell vele csinalni h
    # eltunjon"). A szöveget a Python adja (hwscan.PNP_ERROR_CODE_REMEDIES), ezért a
    # CLI és a grafikus felület ugyanazt mondja - két külön szöveg előbb-utóbb
    # ellentmondana egymásnak ugyanarról a hibakódról. Táblázat helyett külön blokk:
    # ezek 1-2 mondatos utasítások, egy oszlopba tördelve olvashatatlanok lennének.
    remedies = [p for p in probs if p.get('remedy')]
    if remedies:
        ui.write('')
        for i, p in enumerate(probs, 1):
            if p.get('remedy'):
                ui.write(f"  {i}. {p.get('name')} (Code {p.get('code')})")
                ui.dim(f"     -> {p['remedy']}")
    fixable = [p for p in probs if p.get('has_fix')]
    if not fixable:
        return
    sel = ui.pick_indices('Melyiket javítsam? (sorszám)', len(probs))
    for i in sel:
        p = probs[i]
        if p.get('has_fix') and p.get('pnp_id'):
            ui.info(f"Javítás: {p.get('name')}")
            api.fix_problem_device(p['pnp_id'], p.get('code'))


# A GYÁRTÓI DRIVER-OLDAL MENÜPONTJA (`_vendor_pages`) ITT VOLT, ÉS TELJESEN KIKERÜLT.
# Két lépésben: 2026-09-02-án a videokártya-gyártói ágak (NVIDIA/AMD/Intel), majd
# 2026-09-18-án a gép/alaplap-gyártói link is (explicit user decision) - a Python-oldal
# (`app/gui/oemdrivers.py`) is törölve. Azért kellett INNEN is kivenni, nem csak a
# grafikus felületről, mert a CLI 2026-08-29 óta TELJES ÉRTÉKŰ felület: itt hagyva a
# funkció az egyik felületen tovább élt volna, a másikon nem.


# ---------------------------------------------------------------------------
# 3. 1 KATTINTÁSOS FIX
# ---------------------------------------------------------------------------

def _menu_autofix(api):
    _header(api, '1 Kattintásos Driver Fix')
    ui.panel('MIT CSINÁL', [
        'Visszaállítási pont, a Gyors Rendszerindítás és a WU-driverek tiltása,',
        'szellemeszközök + MINDEN third-party driver törlése, majd a legfrissebb',
        'driverek telepítése a Windows Update-ről, a Microsoft Update Catalogból',
        'és a gyártó (Lenovo/Dell/HP) katalógusából — újraindítás-lánccal.',
        '',
        'A gép a folyamat alatt TÖBBSZÖR ÚJRAINDUL, és magától folytatja a munkát.',
        'Időigény: jellemzően 20-60 perc.',
    ], color=ui.PURPLE)
    ui.write('')

    net = {}
    try:
        net = api.get_autofix_net_state() or {}
    except Exception as e:
        logging.debug(f"[CLI] Hálózat-állapot lekérdezés: {e}")
    if net.get('wifi'):
        ui.warn(f"A gép Wi-Fi-n van ({net.get('ssid') or 'ismeretlen hálózat'}).")

    skip_printer = ui.confirm('Nyomtató-driverek MEGTARTÁSA (ne törölje őket)?', True)
    wifi_mode = ui.confirm('Wi-Fi-s telepítés (a Wi-Fi driver védve, hosszabb hálózat-várakozás)?',
                           bool(net.get('wifi')))
    rebuild_wifi = True
    if wifi_mode:
        rebuild_wifi = ui.confirm('A Wi-Fi drivert teljesen újraépítse (törlés + visszatöltés)?', True)
    ui.write('')
    # (A tároló- és firmware-kérdés 2026-09-02-én kikerült: nem választás többé, hanem
    #  rögzített szabály - lásd app/gui/autofix.py: run_autofix.)
    wu_pause = ui.confirm('A végén a Windows Update szüneteltetése ~10 évre?', True)
    # GYORS MÓD: nemmel a lánc csak a WU Agentből telepít. Alapból IGEN (teljes keresés).
    use_catalog = ui.confirm('Microsoft Update Catalog keresés is? (nem = GYORS MÓD, ~20-25 perccel rövidebb)', True)

    ui.write('')
    ui.panel('ÖSSZEGZÉS — EZ FOG TÖRTÉNNI', [
        f"Nyomtató-driverek megtartása : {'IGEN' if skip_printer else 'nem'}",
        f"Wi-Fi mód                    : {'IGEN' if wifi_mode else 'nem'}"
        + (f" (driver újraépítés: {'igen' if rebuild_wifi else 'nem'})" if wifi_mode else ''),
        f"Tároló-driverek              : tiltva (nem kapcsolható)",
        f"Firmware-frissítések         : tiltva (nem kapcsolható)",
        f"WU szüneteltetés a végén     : {'IGEN' if wu_pause else 'nem'}",
        f"MS Update Catalog keresés    : {'IGEN' if use_catalog else 'NEM (gyors mód)'}",
    ], color=ui.YELLOW)

    if not ui.confirm('INDULHAT a fix? (a gép többször újraindul)', False):
        ui.dim('Megszakítva.')
        ui.pause()
        return
    _header(api, '1 Kattintásos Driver Fix — folyamatban')
    api.run_autofix(skip_printer_drivers=skip_printer, wifi_mode=wifi_mode,
                    rebuild_wifi_driver=rebuild_wifi, pause_windows_update=wu_pause,
                    use_catalog=use_catalog)
    ui.pause()


def _autofix_resume(api):
    """A lánc folytatása újraindítás után. NEM a menü jön fel: az ütemezett feladat
    indított minket, a technikus nincs a gép előtt."""
    leg = '--resume-step1' if api.resume_step1 else '--resume-autofix'
    _header(api, 'AutoFix — a lánc folytatása')
    ui.info(f"Az újraindítás utáni folytatás indul ({leg})...")
    logging.info(f"[CLI] AutoFix lánc folytatása CLI módban ({leg}).")
    ui.write('')
    api.run_autofix()
    ui.write('')
    ui.dim('Ez az ablak nyitva marad, hogy lásd az eredményt.')
    ui.pause('Nyomj ENTER-t a bezáráshoz')


# ---------------------------------------------------------------------------
# TOVÁBBI MENÜK
# ---------------------------------------------------------------------------

def _menu_backup(api):
    while True:
        _header(api, 'Mentés és visszaállítás')
        c = ui.menu([
            ('1', 'Third-party driverek exportálása', 'Minden gyártói csomag mentése mappába'),
            ('2', 'ÖSSZES driver exportálása', 'Nagy méret, hosszabb idő'),
            ('3', 'Driverek visszaállítása (élő rendszer)', None),
            ('4', 'Driverek visszaállítása OFFLINE rendszerre', 'WinPE-ből, másik lemezre'),
            ('5', 'Driverek kinyerése WIM/ESD képfájlból', None),
            ('6', 'Visszaállítási pont készítése', None),
        ], back_label='Vissza a főmenübe')
        if c == '0':
            return
        if c == '1':
            _run_screen(api, 'Export', lambda: api.backup_third_party())
        elif c == '2':
            _run_screen(api, 'Teljes export', lambda: api.backup_all())
        elif c == '3':
            _run_screen(api, 'Visszaállítás', lambda: api.restore_online())
        elif c == '4':
            _run_screen(api, 'Offline visszaállítás', lambda: api.restore_offline())
        elif c == '5':
            _run_screen(api, 'WIM kinyerés', lambda: api.extract_wim())
        elif c == '6':
            _run_screen(api, 'Visszaállítási pont', lambda: api.create_restore_point())


def _menu_wu(api):
    while True:
        _header(api, 'Windows Update')
        try:
            st = api.check_wu_status() or {}
            ui.kv('Driver-frissítések', st.get('status') or '-')
            if st.get('pause_status'):
                ui.kv('Szüneteltetés', st['pause_status'])
        except Exception as e:
            logging.debug(f"[CLI] WU állapot: {e}")
        c = ui.menu([
            ('1', 'Driver-frissítések LETILTÁSA', 'A WU ne írja felül a most felrakott drivereket'),
            ('2', 'Driver-frissítések engedélyezése', 'Vissza az alapállapotba'),
            ('3', 'Windows Update szüneteltetése', '~10 év — a gép így megy vissza a vevőhöz'),
            ('4', 'Szüneteltetés feloldása', None),
            ('5', 'WU szolgáltatások újraindítása', 'Ha a keresés beragadt'),
        ], back_label='Vissza a főmenübe')
        if c == '0':
            return
        if c == '1':
            _run_screen(api, 'WU driver-tiltás', lambda: api.disable_wu())
        elif c == '2':
            _run_screen(api, 'WU driver-engedélyezés', lambda: api.enable_wu())
        elif c == '3':
            _run_screen(api, 'WU szüneteltetés', lambda: api.pause_wu(
                int(ui.ask('Hány napra szüneteltessem?', str(AUTOFIX_WU_PAUSE_DAYS)) or AUTOFIX_WU_PAUSE_DAYS)))
        elif c == '4':
            _run_screen(api, 'WU folytatás', lambda: api.resume_wu())
        elif c == '5':
            _run_screen(api, 'WU szolgáltatások', lambda: api.restart_wu())


def _menu_winact(api):
    _header(api, 'Windows & Office aktiválás')
    ui.info('Aktiválási állapot lekérdezése...')
    data = api.get_activation_status() or {}
    if data.get('offline'):
        ui.warn('Offline módban nem elérhető.')
        ui.pause()
        return
    if data.get('error'):
        ui.err(str(data['error']))
        ui.pause()
        return
    w = data.get('windows') or {}
    ui.write('')
    if w.get('oem_key'):
        ui.panel('VAN GYÁRI KULCS A GÉPBEN!', [
            '',
            f"    {ui.BOLD}{ui.WHITE}{w['oem_key']}{ui.RESET}",
            '',
            'Ez a kulcs az alaplapba (BIOS/UEFI) van égetve, és MINDIG MŰKÖDNI FOG —',
            'újratelepítés után is megmarad. NE cseréld le másik kulcsra!',
        ], color=ui.GREEN)
    else:
        ui.panel('NINCS gyári kulcs a BIOS-ban',
                 ['Nem gyári Windowsos gép, vagy összerakott konfiguráció.'], color=ui.GREY)
    ui.write('')
    ui.kv('Állapot', w.get('status_text') or '-')
    ui.kv('Windows', (w.get('os_caption') or '-') + (f" (build {w['os_build']})" if w.get('os_build') else ''))
    ui.kv('Licenc típusa', (w.get('channel') or '-') + (f" — {w['channel_text']}" if w.get('channel_text') else ''))
    ui.kv('Telepített kulcs vége', w.get('partial_key') or '-')
    if w.get('kms_host'):
        ui.kv('Beállított KMS-kiszolgáló', w['kms_host'])
    office = (data.get('office') or {}).get('products') or []
    ui.write('')
    if office:
        for p in office:
            ui.kv(p.get('name', '')[:40], p.get('status_text') or '-')
            # Sikernél csend, hibánál magyarázat - ugyanaz a szabály, mint a GUI-ban
            # (2026-09-07): egy aktivált terméknél nincs mit indokolni.
            if p.get('status_color') != 'ok' and (p.get('reason_text') or p.get('reason_hex')):
                ui.dim(f"      Ok: {p.get('reason_text') or '—'} {p.get('reason_hex') or ''}".rstrip())
    else:
        ui.dim('Nem található licencelt Office (a Microsoft 365 fiókhoz kötött, itt nem látszik).')

    # A "mit fog csinálni a gomb" terv CSAK AKADÁLY esetén jelenik meg (2026-09-07,
    # explicit user decision - a GUI-ban a teljes kártya törölve). Ha nem indulhat, az
    # okot ki KELL írni: a menüpont maga csak annyit mond, hogy "jelenleg nem lehetséges".
    plan = data.get('plan') or {}
    if not plan.get('ready'):
        ui.write('')
        ui.err(plan.get('text') or '')
    # Az Office-aktiváló csomag beállítottsága (2026-09-14). Ugyanaz a szabály, mint
    # fent: ha a művelet nem indulhat, az OKOT ki kell írni - a menüsor "jelenleg nem
    # lehetséges" megjegyzéséből nem derülne ki, hogy mit hova kell beírni.
    oplan = data.get('office_plan') or {}
    if not oplan.get('ready'):
        ui.write('')
        ui.err(oplan.get('text') or '')
    elif oplan.get('mode') == 'list':
        ui.write('')
        ui.warn(oplan.get('text') or '')
    c = ui.menu([
        ('1', 'Windows aktiválása', 'A fenti terv szerint' if plan.get('ready') else 'jelenleg nem lehetséges'),
        ('2', 'Office aktiválása', 'Letöltött aktiváló script futtatása'
            if oplan.get('ready') else 'jelenleg nem lehetséges'),
        ('3', 'Beállított KMS-kiszolgáló törlése', None),
        ('4', 'Windows aktiválás-beállítások megnyitása', None),
    ], back_label='Vissza a főmenübe')
    if c == '1':
        if not plan.get('ready'):
            ui.err('Nincs mivel aktiválni — lásd a fenti üzenetet.')
            ui.pause()
            return
        if w.get('oem_key') and not ui.confirm(
                'A gépbe égetett GYÁRI kulccsal aktiválunk (ez a helyes). Indulhat?', True):
            return
        if w.get('activated') and not ui.confirm('Ez a Windows MÁR aktiválva van. Újraaktiválod?', False):
            return
        _run_screen(api, 'Aktiválás', lambda: api.activate_windows())
    elif c == '2':
        if not oplan.get('ready'):
            ui.err('Az Office-aktiváló csomag nincs beállítva — lásd a fenti üzenetet.')
            ui.pause()
            return
        # NINCS külön megerősítés: a menüpont kiválasztása MAGA a megerősítés (a GUI-ban
        # is a kattintás az - 2026-09-14, explicit user decision). A részletes eredményt
        # viszont ki KELL írni: az `officeact_result` adat-esemény, amit a bridge csak
        # eltárol (a GUI-ban a gomb alatti dobozba megy), tehát a toast önmagában csak
        # annyit mondana, hogy "a részletek a gomb alatt" - ami itt nem létezik.
        _header(api, 'Office aktiválása')
        api._cli_reset_events()
        try:
            api._cli_sync(lambda: api.activate_office())
        except KeyboardInterrupt:
            ui.warn('Megszakítva.')
        except Exception as e:
            logging.error(f"[CLI] Az Office-aktiválás hibára futott: {e}", exc_info=True)
            ui.err(f'Hiba: {e}')
        res = api._cli_take('officeact_result') or {}
        if res.get('text'):
            ui.write('')
            for line in str(res['text']).split('\n'):
                (ui.ok if res.get('ok') else ui.warn)(line) if line.strip() else ui.write('')
        ui.pause()
    elif c == '3':
        _run_screen(api, 'KMS törlése', lambda: api.clear_kms_server())
    elif c == '4':
        api.open_activation_tool('settings')


def _menu_display(api):
    """Kijelző & Színkezelés - ugyanazok a műveletek, mint a GUI nézetben (2026-09-22-i
    újraírás), ugyanazon a magon (app/colormgmt_core.py)."""
    while True:
        _header(api, 'Kijelző & Színkezelés')
        ui.info('Kijelzők és színprofilok beolvasása...')
        api._cli_reset_events()
        _sync(api, api.load_display_info)
        data = api._cli_take('display_info', {}) or {}
        mons = data.get('displays') or []
        if not mons:
            ui.warn('Nem sikerült kijelző-adatot olvasni.')
        for i, m in enumerate(mons, 1):
            hdr = m.get('hdr') or {}
            pr = m.get('profiles') or {}
            lum = (m.get('edid') or {}).get('luminance') or {}
            gamma = m.get('gamma_ramp') or {}
            ui.write('')
            ui.panel(f"{i}. {m.get('name') or m.get('gdi_name') or 'Kijelző'}", [
                f"Csatlakozás    : {m.get('connection') or '-'}"
                + (f"  ·  {m.get('refresh_hz')} Hz" if m.get('refresh_hz') else ''),
                'HDR            : ' + ('nem támogatott' if not hdr.get('supported')
                                       else ('BEKAPCSOLVA' if hdr.get('enabled') else 'kikapcsolva')),
                'Autom. színkez.: ' + ('nem elérhető' if not hdr.get('wcg_supported')
                                       else ('BE' if hdr.get('wcg_enabled') else 'KI')),
                f"SDR-profil     : {pr.get('active_sdr') or 'nincs (Windows alapértelmezés)'}",
                f"HDR-profil     : {pr.get('active_hdr') or 'nincs (Windows alapértelmezés)'}",
                'Gamma (GPU)    : ' + ('LINEÁRIS' if gamma.get('linear') else 'MÓDOSÍTOTT'),
                f"Csúcsfényerő   : {lum.get('peak_nits') or '-'} nit (a GYÁRTÓ adata, nem mérés)",
                f"Monitor driver : {m.get('monitor_driver') or '-'}",
            ])
        ui.write('')
        cal = data.get('calibration')
        ui.kv('Profil-betöltés', ('TARTÓSAN LETILTVA' + ('' if data.get('profile_guard')
                                                          else ' (a bejelentkezéskori zár HIÁNYZIK)'))
              if cal == 0 else 'engedélyezve', label_w=30)
        ui.kv('Automatikus színkezelés', 'TARTÓSAN LETILTVA (bejelentkezéskor kikapcsol)'
              if data.get('acm_guard') else 'engedélyezve (a Windows dönt)', label_w=30)
        if data.get('broken') or data.get('orphans'):
            ui.warn(f"{data.get('broken', 0)} törött regisztráció, {len(data.get('orphans') or [])} árva "
                    f"társítás (a 7. menüpont javítja).")
        c = ui.menu([
            ('1', 'HDR be/ki', None),
            ('2', 'Automatikus színkezelés (ACM) be/ki', None),
            ('3', 'Automatikus színkezelés engedélyezése / tartós letiltása', None),
            ('4', 'Profil-betöltés engedélyezése / tartós letiltása', 'Tiltáskor minden profil lekerül'),
            ('5', 'Színprofil aktiválása / levétele', None),
            ('6', 'Színprofil törlése', None),
            ('7', 'Hibás bejegyzések javítása', None),
            ('8', 'MINDEN színbeállítás GYÁRI visszaállítása', None),
            ('9', 'Windows Színkezelés megnyitása', None),
        ], back_label='Vissza a főmenübe')
        if not c:
            return

        def pick_mon():
            if len(mons) == 1:
                return mons[0]
            i = ui.pick_indices('Melyik kijelző? (sorszám)', len(mons))
            return mons[i[0]] if i else None

        if c in ('1', '2', '5') and not mons:
            continue
        if c == '1':
            m = pick_mon()
            if m:
                on = ui.confirm('Bekapcsoljam a HDR-t?', not (m.get('hdr') or {}).get('enabled'))
                _run_screen(api, 'HDR', lambda: api.set_display_hdr(m['index'], on))
        elif c == '2':
            m = pick_mon()
            if m:
                on = ui.confirm('Bekapcsoljam az automatikus színkezelést?', False)
                _run_screen(api, 'ACM', lambda: api.set_display_acm(m['index'], on))
        elif c == '3':
            allow = ui.confirm('ENGEDÉLYEZZEM az automatikus színkezelést? (Nem = tartós letiltás)',
                               bool(data.get('acm_guard')))
            _run_screen(api, 'Automatikus színkezelés', lambda: api.set_acm_lock(not allow))
        elif c == '4':
            on = ui.confirm('ENGEDÉLYEZZEM a profil-betöltést? (Nem = tartós letiltás, minden profil lekerül '
                            'most és minden bejelentkezéskor)', cal == 0)
            _run_screen(api, 'Profil-betöltés', lambda: api.set_profile_loading(on))
        elif c == '5':
            m = pick_mon()
            if not m:
                continue
            slot = 'hdr' if ui.confirm('HDR-módra? (Nem = SDR)', False) else 'sdr'
            libs = [p for p in (data.get('library') or []) if p.get('selectable')]
            ui.table(['#', 'Profil', 'Típus'], [[str(k), p.get('file'), 'HDR' if p.get('hdr') else 'SDR']
                                                for k, p in enumerate(libs, 1)])
            ui.dim('0 = profil levétele (Windows alapértelmezés)')
            raw = ui.ask('Sorszám', '0').strip()
            if raw.isdigit() and 0 <= int(raw) <= len(libs):
                f = libs[int(raw) - 1]['file'] if int(raw) else ''
                _run_screen(api, 'Profil', lambda: api.activate_color_profile(m['index'], slot, f))
        elif c == '6':
            libs = data.get('library') or []
            ui.table(['#', 'Fájl', 'Eredet'], [[str(k), p.get('file'), 'Windows' if p.get('windows') else 'hozzáadott']
                                               for k, p in enumerate(libs, 1)])
            i = ui.pick_indices('Melyik fájlt törlöm?', len(libs))
            if i and ui.confirm(f"Biztosan törlöd: {libs[i[0]]['file']}?", False):
                f = libs[i[0]]['file']
                _run_screen(api, 'Törlés', lambda: api.uninstall_icc_profile(f))
        elif c == '7':
            _run_screen(api, 'Javítás', lambda: api.repair_color_registration())
        elif c == '8':
            plan = api.get_color_reset_preview() or {}
            ui.write('')
            ui.panel('Gyári visszaállítás - ezt fogja csinálni', [
                f"Profil-társítás törlése : {len(plan.get('associations') or [])}",
                f"Hozzáadott profilfájl   : {len(plan.get('files') or [])}",
                f"Nyomtató-profilfájl     : {len(plan.get('printer_files') or [])}",
                f"HDR kikapcsolása        : {', '.join(plan.get('hdr_on') or []) or '-'}",
                f"ACM kikapcsolása        : {', '.join(plan.get('acm_on') or []) or '-'}",
                f"ACM-zár eltávolítása    : {'igen' if plan.get('acm_guard') else '-'}",
                'Megmarad (Windows gyári): ' + ', '.join(plan.get('kept_windows') or []),
            ])
            if ui.confirm('Biztosan MINDENT visszaállítasz gyárira?', False):
                pr = ui.confirm('A nyomtató-profilokat is töröljem?', True)
                _run_screen(api, 'Gyári visszaállítás', lambda: api.factory_reset_colors(pr))
        elif c == '9':
            api.open_windows_color_tool('colorcpl')


def _menu_stress(api):
    while True:
        _header(api, 'Stress teszt & szervíz programok')
        c = ui.menu([
            ('1', 'Teljes stress teszt indítása', 'FurMark + Prime95 + Linpack + HWiNFO, automatikusan'),
            ('2', 'Egy program indítása', 'Kézi beállítással, automatizálás nélkül'),
            ('3', 'Futó tesztek leállítása', None),
            ('4', 'Szervíz programok telepítése', 'Program Files + asztali parancsikon'),
            ('5', 'Benchmark futtatása', 'Cinebench + FurMark, feltöltés a ranglistára'),
        ], back_label='Vissza a főmenübe')
        if c == '0':
            return
        if c == '1':
            _run_screen(api, 'Stress teszt', lambda: api.start_stress_tests())
        elif c == '2':
            _run_screen(api, 'Program indítása', lambda: _stress_single(api))
        elif c == '3':
            _run_screen(api, 'Leállítás', lambda: api.stop_stress_tests())
        elif c == '4':
            _run_screen(api, 'Telepítés', lambda: _tools_install(api))
        elif c == '5':
            _run_screen(api, 'Benchmark', lambda: _benchmark_flow(api))


def _stress_single(api):
    from app.stress_defs import STRESS_TOOLS
    keys = list(STRESS_TOOLS.keys())
    ui.table(['#', 'Program'], [[str(i), k] for i, k in enumerate(keys, 1)], widths=[4, 40])
    sel = ui.pick_indices('Melyiket indítsam? (sorszám)', len(keys))
    if sel:
        api.start_stress_tool(keys[sel[0]])


def _tools_install(api):
    status = api.get_service_tools_status() or {}
    if not status:
        ui.warn('Nem sikerült lekérdezni a programlistát.')
        return
    keys = list(status.keys())
    rows = [[str(i), k, 'telepítve' if status[k].get('installed') else '-']
            for i, k in enumerate(keys, 1)]
    ui.table(['#', 'Program', 'Állapot'], rows, widths=[4, 36, 14])
    sel = ui.pick_indices("Melyeket telepítsem? (pl. 1,3 vagy 'mind')", len(keys))
    if sel:
        api.install_service_tools([keys[i] for i in sel])


def _benchmark_flow(api):
    ui.panel('BENCHMARK', [
        'Cinebench R20 (CPU, több szálon) + FurMark (GPU) automatikus futtatása,',
        'majd az eredmény feltöltése a bolt ranglistájára.',
        'A gép a mérés alatt terhelés alatt lesz, ~2-4 perc.',
    ], color=ui.PURPLE)
    if not ui.confirm('Indulhat?', False):
        return
    _sync(api, api.run_benchmark_suite, ui.confirm('Zárjam be előtte a futó programokat?', True))
    res = api._cli_take('benchmark_auto_result', {}) or {}
    if res:
        ui.kv('Cinebench', res.get('cinebench') or '-')
        ui.kv('FurMark FPS', res.get('furmark') or '-')
        name = ui.ask('Gép neve a ranglistához (üresen = nem töltjük fel)')
        if name:
            api.upload_benchmark_result(res.get('cinebench'), res.get('furmark'), name)


def _menu_maintenance(api):
    while True:
        _header(api, 'Karbantartás')
        c = ui.menu([
            ('1', 'Temp fájlok törlése', 'Lemez felszabadítása, kategóriánként'),
            ('2', 'Rendszer riport készítése (HTML)', 'S.M.A.R.T. + hardver, nyomtatható'),
            ('3', 'Riport nyomtatása a bolti nyomtatóra', None),
            ('4', 'BitLocker állapot / kikapcsolás', None),
            ('5', 'BCD boot-javító letöltése (BootFixer.cmd)', 'Csak letöltés, nem futtatja'),
            ('6', 'Net Blokkoló script letöltése (block.bat)', 'Csak letöltés, nem futtatja'),
            ('7', 'Naplók letöltése a szerviz Drive-járól', None),
        ], back_label='Vissza a főmenübe')
        if c == '0':
            return
        if c == '1':
            _run_screen(api, 'Temp törlés', lambda: _tempclean_flow(api))
        elif c == '2':
            _run_screen(api, 'Rendszer riport', lambda: api.generate_system_report(
                ui.ask('Megjegyzés a riportra (nem kötelező)'),
                ui.confirm('Teszt-SSD-ről fut a rendszer? (a futó Windows lemeze kimarad a riportból)')))
        elif c == '3':
            _run_screen(api, 'Bolti nyomtatás', lambda: api.print_via_store_printer())
        elif c == '4':
            _run_screen(api, 'BitLocker', lambda: _bitlocker_flow(api))
        elif c == '5':
            _run_screen(api, 'BootFixer', lambda: api.download_boot_fixer())
        elif c == '6':
            _run_screen(api, 'block.bat', lambda: api.download_block_script())
        elif c == '7':
            _run_screen(api, 'Naplók letöltése', lambda: api.download_uploaded_logs(
                ui.ask('Jelszó')))


def _tempclean_flow(api):
    """A kategóriák UGYANABBÓL a definícióból jönnek, amit a GUI is használ
    (`tempclean_core._temp_clean_category_defs`), tehát a két felület nem csúszhat szét."""
    from app import tempclean_core as tc
    ui.info('Méretek felmérése...')
    meas = tc.measure_categories(api.sys_drive)
    sizes = meas.get('sizes', {})
    items = [(k, l, d) for k, l, _p, _s, d in tc._temp_clean_category_defs(api.sys_drive)] + list(tc.SPECIAL_CATEGORIES)

    def sz(k):
        b = (sizes.get(k) or {}).get('bytes')
        return 'futáskor derül ki' if b is None else tc._fmt_bytes(b)
    rows = [[str(i), label, sz(key), 'BE' if default else '-'] for i, (key, label, default) in enumerate(items, 1)]
    ui.table(['#', 'Kategória', 'Méret', 'Alapból'], rows, widths=[4, 52, 16, 8])
    free = (meas.get('disk') or {}).get('free')
    if free is not None:
        ui.dim(f'Szabad hely most: {tc._fmt_bytes(free)}. Az alapból BE jelölésűek a biztonságos törzs-tartalom.')
    sel = ui.pick_indices("Melyeket törölje? (ENTER = az alapértelmezettek)", len(items))
    opts = {key: (i in sel) for i, (key, _l, _d) in enumerate(items)} if sel else None
    api.clean_temp_files(opts)


def _bitlocker_flow(api):
    st = api.get_bitlocker_status() or {}
    ui.kv('Állapot', st.get('status') or '-')
    if st.get('percent') is not None:
        ui.kv('Titkosítva', f"{st.get('percent')}%")
    if ui.confirm('Kikapcsolod a BitLockert (dekódolás, órákig tarthat)?', False):
        api.disable_bitlocker()


def _menu_settings(api):
    while True:
        _header(api, 'Beállítások')
        ui.kv('Cél rendszer', api.target_os_path or 'Jelenlegi (élő) rendszer')
        ui.kv('Napló helye', common._app_data_dir() if hasattr(common, '_app_data_dir') else '-')
        c = ui.menu([
            ('1', 'Offline cél kiválasztása', 'Másik lemezen lévő Windows kezelése (WinPE)'),
            ('2', 'Vissza az élő rendszerre', None),
            ('3', 'Programfrissítés keresése', None),
        ], back_label='Vissza a főmenübe')
        if c == '0':
            return
        if c == '1':
            p = ui.ask('A cél Windows mappája (pl. D:\\Windows) vagy a lemez gyökere')
            if p:
                api.apply_target_os(p)
        elif c == '2':
            api.target_os_path = None
            ui.ok('Visszaálltunk az élő rendszerre.')
            ui.pause()
        elif c == '3':
            _run_screen(api, 'Frissítés', lambda: api.check_for_updates_cli())


# ---------------------------------------------------------------------------
# BELÉPÉSI PONT
# ---------------------------------------------------------------------------

def run_cli_mode():
    """A CLI mód belépési pontja - teljes funkcionalitás konzolon."""
    _install_input_logging()

    # AZONNALI ÉLETJEL, MÉG BÁRMILYEN KÉPERNYŐTÖRLÉS ELŐTT.
    # Terepen (2026-08-31) a CLI elindult - a napló szerint a menüig eljutott -, a
    # felhasználó viszont egy ÜRES FEKETE ABLAKOT látott: a kimenet nem jutott ki a
    # konzolra. Ez a sor a legegyszerűbb eszközökkel megy ki (sima print, csak ASCII,
    # semmi szín/keret/törlés), tehát ha EZ látszik, a konzol jó és a hiba feljebb van;
    # ha ez sem, akkor maga a stream-kötés a bűnös. Így a következő hibajelentés
    # eldönti a kérdést ahelyett, hogy megint találgatnánk.
    try:
        print('DriverVarazslo CLI - indul...', flush=True)
    except Exception:
        pass

    _log_console_state()
    logging.info(f"[CLI] CLI mód indul (ANSI={ui.ANSI}, Unicode={ui.UNICODE}, "
                 f"szélesség={ui.width()}).")
    try:
        api = CliApi()
    except Exception as e:
        logging.error(f"[CLI] A CliApi példányosítása elhasalt: {e}", exc_info=True)
        ui.err(f"A program nem tudott elindulni: {e}")
        ui.pause()
        return

    # ÚJRAINDÍTÁS UTÁNI FOLYTATÁS: nem a menü jön fel. Az ütemezett feladat indított
    # minket, a technikus nincs a gép előtt - a láncnak magától kell folytatódnia.
    if getattr(api, 'resume_mode', False) or getattr(api, 'resume_step1', False):
        _autofix_resume(api)
        return

    while True:
        _header(api)
        _admin_warning()
        c = ui.menu([
            (None, 'DRIVEREK', None),
            ('1', 'Driverek kezelése', 'Listázás, törlés, szellemeszközök, duplikátumok, újrakötés'),
            ('2', 'Driver keresés és telepítés', 'Windows Update + Microsoft Update Catalog + gyártók'),
            ('3', '1 Kattintásos Driver Fix', 'Teljes rendberakás újraindítás-lánccal'),
            ('4', 'Mentés és visszaállítás', 'Export, visszaállítás, WIM, visszaállítási pont'),
            (None, 'RENDSZER', None),
            ('5', 'Windows Update', 'Driver-frissítések tiltása, szüneteltetés'),
            ('6', 'Windows & Office aktiválás', 'Állapot, gyári kulcs a BIOS-ból, aktiválás'),
            ('7', 'Kijelző & Színkezelés', 'HDR, ICC profilok, gamma'),
            (None, 'ESZKÖZÖK', None),
            ('8', 'Stress teszt, szervíz programok, benchmark', None),
            ('9', 'Karbantartás', 'Temp, riport, nyomtatás, BitLocker, naplók, scriptek'),
            ('10', 'Beállítások', 'Offline cél, frissítés'),
        ], prompt='Menüpont', back_label='Kilépés')
        if c == '0':
            ui.write('')
            ui.dim('Viszlát!')
            logging.info('[CLI] Kilépés a menüből.')
            return
        {
            '1': _menu_drivers, '2': _menu_hwscan, '3': _menu_autofix, '4': _menu_backup,
            '5': _menu_wu, '6': _menu_winact, '7': _menu_display, '8': _menu_stress,
            '9': _menu_maintenance, '10': _menu_settings,
        }[c](api)
