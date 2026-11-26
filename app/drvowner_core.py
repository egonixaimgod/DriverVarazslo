"""MELYIK DRIVER-CSOMAG TARTOZIK EGY MOST IS FENT LÉVŐ PROGRAMHOZ (2026-11-26).

MIÉRT (explicit user decision, terepi eset): egy ASUS-on az eSzignó kártyaolvasó-drivere
a Driverek nézetben „Nem használt” volt, mert épp nem volt bedugva az olvasó, és a
szellemeszköz-nyoma sem volt meg. A fix „Szoftveres driverek megtartása” pipája sem
védte volna (az csak a hardver nélküli csomagokat nézte), tehát egy fix után az ügyfél
programja nem látta volna a kártyát. Terepen ugyanez: Lenovo 20W1 (2026-11-23),
`gemccid.inf` -> „nincs hozzá eszköz” a 193 „nem használt” között. A felhasználó szabálya:
*„ezeket a drivereket amikhez tartozik vmi szoftver jelenleg a gépen … ne töröljük”* —
a gép SAJÁT hardverének drivere viszont mindig törlődik és újra felkerül.

KÉT BIZONYÍTÉK (mindkettő a Windows saját nyilvántartása, nem találgatás):
  1. `%WinDir%\\INF\\setupapi.dev*.log`: minden ÚJ csomag regisztrálásánál
     („Registered driver package '<mappa>' with 'oemNN.inf'”) a szakasz megmondja, honnan
     jött az INF („Import Driver Package: <útvonal>”) és melyik folyamat indította (`cmd:`).
     A kulcs a DriverStore-MAPPA (hash-es név), nem az `oemNN` szám: a szám a fix minden
     törlése után mást jelenthet (CLAUDE.md 4. fejezet).
  2. Tartalék: egy telepített program mappájában ott van ugyanaz az INF-fájl.
A program akkor számít, ha MOST is fent van: létezik a mappája (és ha van, a
telepített programok listáján is szerepel). A Windows Update, a DriverVarázsló saját
katalógus-telepítése és az ideiglenes mappából futó telepítők NEM program-bizonyítékok.

EZ BESOROLÁS, NEM SZŰRŐ: semmit nem rejt el és nem tilt; a kézi törlés mindenre megy.
A fixben a „Szoftveres driverek megtartása” pipa dönt (a képernyőn, emberi döntéssel).
Minden függvény a hívó `log`-ja helyett a modul naplójába ír (Rule 0); a forró ciklusok
nem naplóznak iterációnként.
"""
import glob
import logging
import os
import re
import time

# A szakaszok és a bennük lévő tények (a setupapi.dev.log nyelve mindig angol).
_SECTION_RE = re.compile(r'^>>>  \[(?P<title>[^\r\n]*)\]\s*$', re.M)
_CMD_RE = re.compile(r'^\s+cmd:\s*(?P<cmd>[^\r\n]+)$', re.M)
_IMPORT_RE = re.compile(r'Import Driver Package: (?P<src>[^}\r\n]+?\.inf)\}', re.I)
_REG_RE = re.compile(r"Registered driver package '(?P<folder>[^']+)' with '(?P<pub>oem\d+\.inf)'", re.I)

# Ezekből a forrásokból jött csomag NEM programé (a forrás-útvonal kisbetűs részletei).
_NOT_PROGRAM_SRC = (
    ('\\softwaredistribution\\', 'wu'),            # Windows Update
    ('\\dv_temp\\', 'self'),                       # a DriverVarázsló katalógus-/WU-telepítése
    ('\\driverstore\\filerepository\\', 'reinstall'),
    ('\\windows\\inf\\', 'reinstall'),
    ('\\appdata\\local\\temp\\', 'temp'),          # kibontott telepítő: a mappája eltűnik
    ('\\windows\\temp\\', 'temp'),
)
_SELF_EXE = ('drivervarazslo.exe',)

# A program-mappák gyökerei: csak ALATTUK lévő mappa számít programnak (a gyökér maga nem).
def _program_roots():
    roots = []
    for var in ('ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432', 'ProgramData'):
        v = os.environ.get(var)
        if v:
            roots.append(os.path.normcase(os.path.normpath(v)))
    la = os.environ.get('LOCALAPPDATA')
    if la:
        roots.append(os.path.normcase(os.path.join(la, 'Programs')))
    return sorted(set(roots))


def _norm(p):
    return os.path.normcase(os.path.normpath(p.strip().strip('"'))) if p else ''


def _exe_of(cmd):
    """A `cmd:` sor futtatható fájlja (idézőjeles vagy sima alak)."""
    s = (cmd or '').strip()
    if s.startswith('"'):
        end = s.find('"', 1)
        return s[1:end] if end > 0 else s[1:]
    return s.split(' ')[0]


# ---------------------------------------------------------------------------
# 1) setupapi.dev.log
# ---------------------------------------------------------------------------
def parse_setupapi_registrations(text):
    """A naplószövegből: {DriverStore-mappa (kisbetű): {'pub','src','cmd','title'}}.

    TISZTA függvény. Csak azok a szakaszok számítanak, amelyekben új csomag regisztrálódott
    (`Registered driver package`): egy már meglévő csomag újratelepítése nem mond semmit a
    származásáról. Ugyanaz a mappa később újra regisztrálódhat (törlés + újra-import): a
    LEGUTÓBBI regisztráció nyer, mert az a mostani példány."""
    out = {}
    heads = list(_SECTION_RE.finditer(text or ''))
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = text[h.end():end]
        regs = list(_REG_RE.finditer(body))
        if not regs:
            continue
        cm = _CMD_RE.search(body)
        im = _IMPORT_RE.search(body)
        title = h.group('title')
        src = im.group('src').strip() if im else ''
        if not src and ' - ' in title and title.lower().endswith('.inf'):
            src = title.split(' - ', 1)[1].strip()
        for r in regs:
            out[r.group('folder').lower()] = {
                'pub': r.group('pub').lower(), 'src': src,
                'cmd': cm.group('cmd').strip() if cm else '', 'title': title,
            }
    return out


def read_setupapi_registrations(inf_dir=None, reader=None):
    """Az összes `setupapi.dev*.log` (archívumok időrendben, a mostani a végén).

    `None` = a napló nem olvasható (NEMTUDÁS, nem „nincs program”); `{}` = olvasható, de
    egyetlen regisztráció sincs benne."""
    inf_dir = inf_dir or os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'INF')
    files = sorted(f for f in glob.glob(os.path.join(inf_dir, 'setupapi.dev.*.log')))
    cur = os.path.join(inf_dir, 'setupapi.dev.log')
    if os.path.exists(cur):
        files.append(cur)
    if not files:
        logging.warning(f"[DRVOWNER] Nincs setupapi.dev*.log itt: {inf_dir} - a csomagok "
                        f"származása ebből nem állapítható meg.")
        return None
    reader = reader or (lambda p: open(p, encoding='utf-8', errors='replace').read())
    out, bad = {}, []
    for f in files:
        try:
            out.update(parse_setupapi_registrations(reader(f)))
        except Exception as e:
            bad.append(f"{os.path.basename(f)}: {e}")
    if bad:
        logging.warning(f"[DRVOWNER] {len(bad)} setupapi-napló nem olvasható: {bad[:3]}")
    if bad and len(bad) == len(files):
        return None
    logging.info(f"[DRVOWNER] setupapi-naplók: {len(files)} fájl, {len(out)} csomag-regisztráció.")
    return out


# ---------------------------------------------------------------------------
# 2) Telepített programok
# ---------------------------------------------------------------------------
_UNINSTALL_KEYS = (
    ('HKLM', r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'),
    ('HKLM', r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'),
    ('HKCU', r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'),
)


def _read_uninstall_entries():
    """A registry Uninstall-bejegyzései: [{'name','publisher','location','icon','uninstall'}]."""
    import winreg
    hives = {'HKLM': winreg.HKEY_LOCAL_MACHINE, 'HKCU': winreg.HKEY_CURRENT_USER}
    out = []
    for hive, path in _UNINSTALL_KEYS:
        try:
            root = winreg.OpenKey(hives[hive], path)
        except OSError:
            continue
        with root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    with winreg.OpenKey(root, winreg.EnumKey(root, i)) as k:
                        def val(n):
                            try:
                                v = winreg.QueryValueEx(k, n)[0]
                                return v if isinstance(v, str) else ''
                            except OSError:
                                return ''
                        name = val('DisplayName')
                        if name:
                            out.append({'name': name, 'publisher': val('Publisher'),
                                        'location': val('InstallLocation'),
                                        'icon': val('DisplayIcon'),
                                        'uninstall': val('UninstallString')})
                except OSError:
                    continue
    return out


def program_dirs(entries, roots=None, isdir=os.path.isdir):
    """Uninstall-bejegyzésekből a programok MOST LÉTEZŐ mappái: [(mappa, név)].

    TISZTA (a fájlrendszert az `isdir`-en át kérdezi). A mappa: InstallLocation, ennek híján
    a DisplayIcon / UninstallString exéjének mappája - de csak egy program-gyökér ALATT
    (Program Files, ProgramData, %LOCALAPPDATA%\\Programs), és soha maga a gyökér (egy
    `msiexec` vagy `C:\\Windows\\…` eltávolító nem program-mappa)."""
    roots = roots if roots is not None else _program_roots()
    out = []
    for e in entries:
        cands = [e.get('location')]
        for k in ('icon', 'uninstall'):
            exe = _exe_of(e.get(k) or '').split(',')[0]
            if exe.lower().endswith('.exe'):
                cands.append(os.path.dirname(exe))
        for c in cands:
            d = _norm(c)
            if not d:
                continue
            if not any(d.startswith(r + os.sep) for r in roots):
                continue
            if isdir(d):
                out.append((d, e.get('name') or ''))
                break
    return out


# ---------------------------------------------------------------------------
# 3) Döntés
# ---------------------------------------------------------------------------
def _list_dir(path):
    """A mappa bejegyzés-nevei; `None`, ha nem olvasható (pl. a WindowsApps admin nélkül)."""
    try:
        return os.listdir(path)
    except OSError:
        return None


def _store_family_dir(path, lister=_list_dir):
    """`…\\WindowsApps\\<Név>_<verzió>_<arch>__<kiadó>\\…` -> (család-név, létezik-e most
    BÁRMELYIK verziója: True / False / None = nem tudjuk). A Store-alkalmazás frissítéskor új
    verzió-mappába költözik. A WindowsApps mappa admin nélkül NEM listázható: a `glob` ezt
    némán üres eredménynek venné, és egy fent lévő alkalmazás „már nincs fent” lenne
    (mérve 2026-11-26, a GamingServices xvdd/gameflt csomagjain)."""
    m = re.search(r'\\windowsapps\\([^\\_]+)_[^\\]*__([^\\]+)', path, re.I)
    if not m:
        return None
    names = lister(path[:m.start()] + '\\WindowsApps')
    if names is None:
        return m.group(1), None
    fam, pub = m.group(1).lower(), m.group(2).lower()
    alive = any(n.lower().startswith(fam + '_') and n.lower().endswith('__' + pub) for n in names)
    return m.group(1), alive


def classify_origin(rec, prog_dirs, roots=None, isdir=os.path.isdir, lister=_list_dir):
    """Egy regisztráció származása -> (fajta, programnév vagy None, bizonyíték-szöveg).

    fajta: 'program' | 'wu' | 'self' | 'reinstall' | 'temp' | 'gone' | 'unknown'.
    TISZTA (fájlrendszer: `isdir`/`lister`)."""
    roots = roots if roots is not None else _program_roots()
    src = _norm(rec.get('src'))
    exe = _norm(_exe_of(rec.get('cmd')))
    if os.path.basename(exe).lower() in _SELF_EXE:
        return 'self', None, 'a DriverVarázsló telepítette'
    for frag, kind in _NOT_PROGRAM_SRC:
        if frag in (src.lower() or ''):
            return kind, None, f"forrás: {rec.get('src')}"
    # Store-alkalmazás (WindowsApps): a család bármelyik verziója számít.
    # (az EREDETI útvonalból, hogy a név a valódi kis-nagybetűivel jelenjen meg)
    for p in ((rec.get('src') or '').strip(), _exe_of(rec.get('cmd'))):
        fam = _store_family_dir(p, lister) if p else None
        if fam:
            name, alive = fam
            if alive is None:
                return 'unknown', None, f"Store-alkalmazás ({name}), de a mappája nem olvasható"
            return (('program', name, f"Store-alkalmazás mappájából: {rec.get('src') or rec.get('cmd')}")
                    if alive else ('gone', None, f"a Store-alkalmazás ({name}) már nincs fent"))
    # Telepített program mappája (a leghosszabb illeszkedő nyer).
    best = None
    for d, name in prog_dirs:
        for p in (src, exe):
            if p and (p == d or p.startswith(d + os.sep)) and (best is None or len(d) > len(best[0])):
                best = (d, name)
    if best:
        return 'program', best[1], f"a(z) „{best[1]}” program mappájából: {rec.get('src') or rec.get('cmd')}"
    # Program-gyökér alatti, MOST is létező mappa, Uninstall-bejegyzés nélkül (hordozható
    # telepítés): a program neve a gyökér alatti első mappa.
    for p in (src, exe):
        if not p:
            continue
        for r in roots:
            if p.startswith(r + os.sep):
                top = p[len(r) + 1:].split(os.sep)[0]
                d = os.path.join(r, top)
                if top and isdir(d):
                    return 'program', top, f"a(z) „{top}” mappából (Program Files): {rec.get('src') or rec.get('cmd')}"
                return 'gone', None, f"a forrás-mappa már nincs meg: {rec.get('src') or rec.get('cmd')}"
    return 'unknown', None, f"forrás: {rec.get('src') or '?'}, folyamat: {rec.get('cmd') or '?'}"


def scan_program_infs(prog_dirs, wanted, max_entries=80000, max_seconds=4.0, per_program=6000):
    """A program-mappákban keresi a `wanted` INF-fájlneveket (kisbetű) -> {inf: (mappa, név)}.

    Korlátos (összesen és programonként is, plusz idő): egy játék több százezer fájlja nem
    akaszthatja meg a felderítést, és nem viheti el a többi program elől a keretet (az első
    változat egyetlen közös korláttal az első pár nagy programnál elfogyott). Ha egy korlát
    elfogy, kimondjuk (a hiány NEM azt jelenti, hogy nincs ott)."""
    found, seen, t0, cut = {}, 0, time.monotonic(), False
    if not wanted:
        return found, False
    for d, name in prog_dirs:
        stack, mine = [(d, 0)], 0
        while stack:
            cur, depth = stack.pop()
            if mine > per_program:
                cut = True
                break
            try:
                with os.scandir(cur) as it:
                    for e in it:
                        seen += 1
                        mine += 1
                        if seen > max_entries or time.monotonic() - t0 > max_seconds:
                            return found, True
                        if e.is_dir(follow_symlinks=False):
                            if depth < 5:
                                stack.append((e.path, depth + 1))
                        elif e.name.lower() in wanted and e.name.lower() not in found:
                            found[e.name.lower()] = (os.path.dirname(e.path), name)
            except OSError:
                continue
    return found, cut


def find_package_owners(packages, registrations, prog_dirs, folder_infs=None,
                        roots=None, isdir=os.path.isdir, lister=_list_dir):
    """{published: {'program','evidence','origin'}} - csak a programhoz köthető csomagok.

    `packages`: [{'published','original','folder'}] (a `folder` a csomag mostani
    DriverStore-mappája); `registrations`: `read_setupapi_registrations` kimenete (vagy
    None); `folder_infs`: a `scan_program_infs` találatai. TISZTA."""
    owners, origins = {}, {}
    for p in packages:
        pub = (p.get('published') or '').lower()
        rec = (registrations or {}).get((p.get('folder') or '').lower())
        if rec:
            kind, name, ev = classify_origin(rec, prog_dirs, roots, isdir, lister)
        else:
            kind, name, ev = ('no-record' if registrations is not None else 'no-log'), None, ''
        origins[pub] = kind
        if kind == 'program' and name:
            owners[pub] = {'program': name, 'evidence': 'setupapi: ' + ev, 'origin': kind}
            continue
        # Tartalék: ugyanaz az INF egy telepített program mappájában. A Windows Update és a
        # saját telepítésünk bizonyítéka erősebb: ott nem írjuk felül.
        if kind in ('wu', 'self'):
            continue
        hit = (folder_infs or {}).get((p.get('original') or '').lower())
        if hit:
            owners[pub] = {'program': hit[1],
                           'evidence': f"a(z) „{hit[1]}” program mappájában ott az INF: {hit[0]}",
                           'origin': 'folder'}
    return owners, origins


def collect_package_owners(packages, inf_dir=None, entries_fn=None, reader=None):
    """A MODUL BELÉPÉSI PONTJA (élő rendszeren): {published: {'program','evidence','origin'}}.

    `packages`: [{'published','original','folder'}]. Hiba esetén `None` (NEMTUDÁS: a hívó
    ilyenkor semmit nem változtat a besoroláson), és WARNING."""
    t0 = time.monotonic()
    try:
        regs = read_setupapi_registrations(inf_dir, reader)
        entries = (entries_fn or _read_uninstall_entries)()
        dirs = program_dirs(entries)
        wanted = {(p.get('original') or '').lower() for p in packages if p.get('original')}
        folder_infs, truncated = scan_program_infs(dirs, wanted)
        owners, origins = find_package_owners(packages, regs, dirs, folder_infs)
    except Exception as e:
        logging.warning(f"[DRVOWNER] A csomagok program-hovatartozása nem állapítható meg: {e}",
                        exc_info=True)
        return None
    from collections import Counter
    cnt = Counter(origins.values())
    logging.info(f"[DRVOWNER] {len(packages)} csomag származása: {dict(cnt)}; programhoz kötve: "
                 f"{len(owners)} ({len(entries)} telepített program, {len(dirs)} létező mappával"
                 f"{', a mappa-keresés korlátba ütközött' if truncated else ''}; "
                 f"{time.monotonic() - t0:.1f} mp)")
    for pub, o in sorted(owners.items()):
        logging.debug(f"[DRVOWNER] {pub}: programé - {o['program']} ({o['evidence']})")
    return owners
