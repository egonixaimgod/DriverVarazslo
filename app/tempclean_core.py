"""Rendszer karbantartás (Temp Fájlok Törlése) - közös mag (a GUI és a CLI clean_temp_files is
ezt hívja, hogy a kettő ne driftelhessen szét): kategória-definíciók, méret-felmérés,
mappatartalom-törlés, Lomtár-ürítés, komponenstár-takarítás, méret-formázás.

2026-09-22-I ÁTDOLGOZÁS (explicit user decision, a régi, Gemini-vel írt változat átnézése
után). A MÉRT HIBÁK, amiket javít:
  - EGY ZÁROLT FÁJL MIATT AZ EGÉSZ ALMAPPA BENT MARADT: a régi kód almappánként
    `shutil.rmtree`-t hívott hibakezelés nélkül, ami az ELSŐ hibánál megáll. Mérve: egy
    almappában 50 fájl, ebből 1 zárolt -> 0 törlődött, mind az 50 maradt, a kiírás pedig
    "1 kihagyva"-t mondott. A %TEMP%-ben (nálunk 1,2 GB / 17 almappa) szinte mindig fut
    valami, tehát a törlés jó része csendben elmaradt. Az új `_delete_contents` fájlonként
    halad alulról felfelé: a zárolt fájl kimarad, a többi törlődik, a számláló FÁJLT számol.
  - a felszabadított hely csak teljes siker esetén számolódott - most fájlonként;
  - a mappa-LINKET (junction/symlink) SOHA nem követjük, csak magát a linket töröljük: így
    a %TEMP%-ből nem nyúlhatunk ki máshová. Ez NEM kivétel a törlésből (a CLAUDE.md 1. elve
    szerint ide kivétel-lista nem kerülhet) - a link maga törlődik, csak a CÉLJA nem a
    miénk, az nem a takarított mappa tartalma.

A MAPPA TARTALMÁT MINDEN ESETBEN TÖRLI - ITT SOHA NEM LEHET KIVÉTEL-LISTA (explicit user
decision, 2026-09-22; lásd a CLAUDE.md "A TEMP-TAKARÍTÁS MINDIG MINDENT TÖRÖL" szekcióját)."""

# === AUTO-IMPORTS ===
import ctypes
import ctypes.wintypes
import glob
import os
import stat
import shutil
import logging
from app.win32 import _SHQUERYRBINFO
# === /AUTO-IMPORTS ===


FILE_ATTRIBUTE_REPARSE_POINT = 0x400
# Ennyi kihagyott fájl NEVÉT naplózzuk mintának kategóriánként (hot loop: a teljes lista
# ezres nagyságrendű is lehet, és kitolná a naplóból a valódi előzményt - Rule 0 ellen-szabály).
FAILED_SAMPLE_MAX = 8
# A komponenstár-takarítás (DISM) felső időkorlátja - lassú gépen 10-20 perc is lehet.
COMPONENT_CLEANUP_TIMEOUT = 1800


def _fmt_bytes(n):
    """Bájt -> emberi olvasható méret (KB/MB/GB)."""
    n = float(max(n or 0, 0))
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f"{int(n)} B" if unit == 'B' else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _is_reparse(path):
    """Mappa-link (junction) vagy symlink-e - ezeket SOHA nem követjük (lásd a modul fejét)."""
    try:
        return bool(os.lstat(path).st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT)
    except (OSError, AttributeError):
        return os.path.islink(path)


def _remove_file(path):
    """Egy fájl törlése; írásvédett fájlnál a védelmet levéve újrapróbálja."""
    try:
        os.remove(path)
        return True
    except PermissionError:
        try:
            os.chmod(path, stat.S_IWRITE)
            os.remove(path)
            return True
        except OSError:
            return False
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _delete_contents(folder, st, cancel_check=None):
    """Egy mappa TARTALMÁNAK törlése, alulról felfelé, fájlonként. A zárolt fájl kimarad,
    a többi törlődik; az üressé vált almappák is törlődnek. `st` = számláló-dict."""
    try:
        entries = list(os.scandir(folder))
    except OSError as e:
        st['failed'] += 1
        st['samples'].append(f'{folder} (nem olvasható: {e.__class__.__name__})')
        return
    for e in entries:
        if cancel_check and cancel_check():
            st['cancelled'] = True
            return
        full = e.path
        if _is_reparse(full):
            # A link maga törlődik (rmdir a junction/dir-symlink, unlink a fájl-symlink),
            # a célja érintetlen.
            try:
                os.rmdir(full)
            except OSError:
                if not _remove_file(full):
                    st['failed'] += 1
                    st['samples'].append(full)
            continue
        try:
            is_dir = e.is_dir(follow_symlinks=False)
        except OSError:
            is_dir = False
        if is_dir:
            _delete_contents(full, st, cancel_check)
            try:
                os.rmdir(full)
            except OSError:
                pass        # zárolt fájl maradt benne - azt a fájl-szint már számolta
            continue
        try:
            size = e.stat(follow_symlinks=False).st_size
        except OSError:
            size = 0
        if _remove_file(full):
            st['freed'] += size
            st['removed'] += 1
        else:
            st['failed'] += 1
            if len(st['samples']) < FAILED_SAMPLE_MAX:
                st['samples'].append(full)


def _clean_folder_contents(folder, cancel_check=None):
    """Egy mappa TARTALMÁNAK (nem magának a mappának) törlése. Visszaad:
    (felszabadított_bájt, törölt_fájlok, kihagyott_fájlok) - FÁJLT számol, nem mappát.
    A kihagyottakból mintát naplóz (név szerint), hogy egy "miért maradt ott?" kérdés
    megválaszolható legyen."""
    st = {'freed': 0, 'removed': 0, 'failed': 0, 'samples': [], 'cancelled': False}
    if not folder or not os.path.isdir(folder):
        return 0, 0, 0
    _delete_contents(folder, st, cancel_check)
    logging.info(f"[TEMPCLEAN] {folder}: {st['removed']} fájl törölve ({_fmt_bytes(st['freed'])}), "
                 f"{st['failed']} kihagyva{' (MEGSZAKÍTVA)' if st['cancelled'] else ''}"
                 + (f" - minta: {st['samples']}" if st['samples'] else ''))
    return st['freed'], st['removed'], st['failed']


def folder_size(folder):
    """(bájt, fájl) egy mappa teljes tartalmára - a méret-előnézethez. Linket nem követ.
    Ha maga a mappa NEM OLVASHATÓ, (None, None) - a nemtudást nem írjuk ki 0-ként
    (CLAUDE.md 3. elv; mérve: nem emelt jogkörrel a Windows\\Temp így "0 B"-t mutatott)."""
    try:
        os.scandir(folder).close()
    except OSError:
        return None, None
    tot = n = 0
    stack = [folder]
    while stack:
        cur = stack.pop()
        try:
            it = list(os.scandir(cur))
        except OSError:
            continue
        for e in it:
            try:
                if _is_reparse(e.path):
                    continue
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                else:
                    tot += e.stat(follow_symlinks=False).st_size
                    n += 1
            except OSError:
                continue
    return tot, n


def _recycle_bin_info():
    """(bájt, elemszám) a Lomtárról, vagy (None, None) ha nem kérdezhető le."""
    try:
        info = _SHQUERYRBINFO()
        info.cbSize = ctypes.sizeof(_SHQUERYRBINFO)
        if ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) == 0:
            return info.i64Size, info.i64NumItems
    except Exception as e:
        logging.debug(f"[TEMPCLEAN] SHQueryRecycleBinW hiba: {e}")
    return None, None


def _empty_recycle_bin():
    """Lomtár ürítése, VISSZAOLVASÁSSAL. Visszaad: (felszabadított_bájt, elemek_előtte, ok).
    A verdikt nem az ürítő hívás visszatérési kódja (üres Lomtárra is hibát ad), hanem hogy
    utána 0 elem maradt-e."""
    size, items = _recycle_bin_info()
    if not items:
        logging.info("[TEMPCLEAN] A Lomtár már üres volt.")
        return 0, 0, True
    logging.warning(f"[TEMPCLEAN] Lomtár ÜRÍTÉSE: {items} elem, {_fmt_bytes(size)}")
    try:
        ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x1 | 0x2 | 0x4)
    except Exception as e:
        logging.warning(f"[TEMPCLEAN] SHEmptyRecycleBinW hiba: {e}")
    after_size, after_items = _recycle_bin_info()
    ok = after_items == 0
    if not ok:
        logging.warning(f"[TEMPCLEAN] A Lomtár ürítés után is {after_items} elemet tartalmaz.")
    return max(0, (size or 0) - (after_size or 0)), items, ok


def _thumbnail_files():
    local = os.environ.get('LOCALAPPDATA')
    d = os.path.join(local, 'Microsoft', 'Windows', 'Explorer') if local else None
    if not d or not os.path.isdir(d):
        return []
    return [os.path.join(d, n) for n in os.listdir(d) if n.startswith(('thumbcache_', 'iconcache_'))]


def clean_thumbnails():
    """A miniatűr-gyorsítótár törlése. Visszaad: (bájt, törölt, zárolt). A zárolt fájlokat a
    Windows Intéző tartja nyitva - mérve 30-ból 10 -, azok kijelentkezés után törölhetők."""
    freed = removed = failed = 0
    for f in _thumbnail_files():
        try:
            size = os.path.getsize(f)
        except OSError:
            size = 0
        if _remove_file(f):
            freed += size
            removed += 1
        else:
            failed += 1
    logging.info(f"[TEMPCLEAN] Miniatűr-gyorsítótár: {removed} törölve ({_fmt_bytes(freed)}), {failed} zárolt.")
    return freed, removed, failed


def _browser_cache_dirs():
    """A böngészők GYORSÍTÓTÁR-mappái profilonként - CSAK a cache, soha nem a süti, az
    előzmény, a jelszó vagy a könyvjelző. Chromium-alapúak: Cache / Code Cache / GPUCache
    minden profilban; Firefox: cache2 (az a %LOCALAPPDATA% alatt van, a profil a Roamingban)."""
    local = os.environ.get('LOCALAPPDATA', '')
    if not local:
        return []
    out = []
    chromium = [os.path.join(local, 'Google', 'Chrome', 'User Data'),
                os.path.join(local, 'Microsoft', 'Edge', 'User Data'),
                os.path.join(local, 'BraveSoftware', 'Brave-Browser', 'User Data')]
    for base in chromium:
        if not os.path.isdir(base):
            continue
        for prof in glob.glob(os.path.join(base, '*')):
            name = os.path.basename(prof)
            if name != 'Default' and not name.startswith('Profile '):
                continue
            for sub in ('Cache', 'Code Cache', 'GPUCache'):
                p = os.path.join(prof, sub)
                if os.path.isdir(p):
                    out.append(p)
    for p in glob.glob(os.path.join(local, 'Mozilla', 'Firefox', 'Profiles', '*', 'cache2')):
        if os.path.isdir(p):
            out.append(p)
    return out


def _temp_clean_category_defs(sys_drive):
    """A MAPPA-alapú kategóriák - EGY helyen (GUI és CLI is ezt hívja). Elemek:
    (kulcs, címke, [törlendő mappák], [leállítandó szolgáltatások], alapból_bepipálva).
    A 3 alapból bepipált a biztonságos törzs-tartalom, a többi opt-in extra."""
    import tempfile
    local = os.environ.get('LOCALAPPDATA', '')
    wer_base = os.path.join(sys_drive, 'ProgramData', 'Microsoft', 'Windows', 'WER')
    wer_user = os.path.join(local, 'Microsoft', 'Windows', 'WER') if local else ''
    return [
        ('user_temp', '👤 Felhasználói TEMP mappa (%TEMP%)',
         [tempfile.gettempdir()], [], True),
        ('windows_temp', '🖥️ Rendszer TEMP mappa (Windows\\Temp)',
         [os.path.join(sys_drive, 'Windows', 'Temp')], [], True),
        ('wu_cache', '🔄 Windows Update letöltési gyorsítótár',
         [os.path.join(sys_drive, 'Windows', 'SoftwareDistribution', 'Download')], ['wuauserv', 'bits'], True),
        ('delivery_opt', '📦 Delivery Optimization gyorsítótár',
         [os.path.join(sys_drive, 'Windows', 'SoftwareDistribution', 'DeliveryOptimization')], ['DoSvc'], False),
        # 2026-09-22: a FELHASZNÁLÓI hibajelentések is ide kerültek (addig csak a gépszintűek).
        ('wer', '⚠️ Hibajelentések (Windows Error Reporting)',
         [os.path.join(wer_base, 'ReportQueue'), os.path.join(wer_base, 'ReportArchive')]
         + ([os.path.join(wer_user, 'ReportQueue'), os.path.join(wer_user, 'ReportArchive')] if wer_user else []),
         [], False),
        ('browser_cache', '🌐 Böngésző-gyorsítótárak (Chrome, Edge, Brave, Firefox)',
         _browser_cache_dirs(), [], False),
        ('shader_cache', '🎮 DirectX Shader Cache',
         [os.path.join(local, 'D3DSCache')] if local else [], [], False),
        ('cbs_logs', '📜 Windows telepítési naplók (CBS logok)',
         [os.path.join(sys_drive, 'Windows', 'Logs', 'CBS')], [], False),
        ('crash_dumps', '💥 Programösszeomlás-dumpok (Crash Dumps)',
         [os.path.join(local, 'CrashDumps')] if local else [], [], False),
        ('inet_cache', '🌍 Internet Explorer / régi Edge gyorsítótár',
         [os.path.join(local, 'Microsoft', 'Windows', 'INetCache')] if local else [], [], False),
        # A 'color_profiles' kategória 2026-09-22-én KIKERÜLT (explicit user decision): a
        # hozzáadott profilokat a Kijelző & Színkezelés nézet gyári visszaállítása tisztábban
        # törli (a kijelző-hozzárendeléseket is leszedi, árva bejegyzést nem hagy), a Windows
        # saját színfájljait pedig ez a kategória AMÚGY SEM tudta törölni - mérve: a
        # Rendszergazdák csoportnak csak olvasási joga van rájuk (TrustedInstaller-tulajdon).
    ]


# A NEM mappa-alapú (külön lépésként futó) kategóriák: (kulcs, címke, alapból).
SPECIAL_CATEGORIES = [
    ('thumbnail_cache', '🖼️ Miniatűr (thumbnail) gyorsítótár', False),
    ('recycle_bin', '🗑️ Lomtár ürítése', False),
    ('component_cleanup', '🧩 Régi Windows-frissítések maradéka (komponenstár)', False),
]


def measure_categories(sys_drive):
    """A méret-előnézet: {kulcs: {'bytes', 'files'}} + a meghajtó szabad/teljes helye.
    A komponenstár mérete olcsón nem kérdezhető le (a DISM-elemzés percekig tart), ezért
    ott `None` - a felület kimondja, hogy futtatáskor derül ki."""
    sizes = {}
    for key, _label, paths, _svc, _d in _temp_clean_category_defs(sys_drive):
        b = n = 0
        for p in paths:
            if p and os.path.isdir(p):
                x, y = folder_size(p)
                if x is None:
                    b = n = None
                    break
                b += x
                n += y
        sizes[key] = {'bytes': b, 'files': n}
    tb = tn = 0
    for f in _thumbnail_files():
        try:
            tb += os.path.getsize(f)
            tn += 1
        except OSError:
            pass
    sizes['thumbnail_cache'] = {'bytes': tb, 'files': tn}
    rb, ri = _recycle_bin_info()
    sizes['recycle_bin'] = {'bytes': rb, 'files': ri}
    sizes['component_cleanup'] = {'bytes': None, 'files': None}
    try:
        du = shutil.disk_usage(sys_drive)
        disk = {'free': du.free, 'total': du.total}
    except OSError:
        disk = {'free': None, 'total': None}
    logging.info("[TEMPCLEAN] Méret-felmérés: " + ', '.join(
        f"{k}={_fmt_bytes(v['bytes']) if v['bytes'] is not None else '?'}" for k, v in sizes.items())
        + f" | szabad hely: {_fmt_bytes(disk['free']) if disk['free'] is not None else '?'}")
    return {'sizes': sizes, 'disk': disk}


def run_component_cleanup(run):
    """A komponenstár (WinSxS) takarítása: `DISM /Online /Cleanup-Image /StartComponentCleanup`.
    A régi, már lecserélt frissítés-komponenseket törli. SZÁNDÉKOSAN /ResetBase NÉLKÜL: az
    minden telepített frissítést véglegesít (eltávolíthatatlanná tesz) - ennyi engedmény egy
    alapértelmezett takarításba nem fér. Visszaad: (ok, üzenet)."""
    logging.warning("[TEMPCLEAN] Komponenstár-takarítás INDUL (DISM /StartComponentCleanup).")
    res = run(['dism', '/English', '/Online', '/Cleanup-Image', '/StartComponentCleanup'],
              timeout=COMPONENT_CLEANUP_TIMEOUT)
    rc = getattr(res, 'returncode', -1)
    out = (getattr(res, 'stdout', '') or '')
    ok = rc == 0 and 'successfully' in out.lower()
    logging.info(f"[TEMPCLEAN] Komponenstár-takarítás vége: rc={rc}, ok={ok}")
    if ok:
        return True, ''
    tail = ' '.join(l.strip() for l in out.splitlines() if l.strip())[-200:]
    return False, f'rc={rc} {tail}'.strip()


def stop_services(run, names):
    """A megadott szolgáltatások ÁLLAPOTÁNAK felmérése, majd leállítása. Visszaad: a
    leállítás ELŐTT futó szolgáltatások listája - csak ezeket indítjuk vissza (egy eredetileg
    leállított szolgáltatás elindítása ugyanúgy hamis állapot lenne)."""
    if not names:
        return []
    ps = ("$r=@(); foreach($n in @(" + ','.join(f"'{n}'" for n in names) + ")) { "
          "$s=Get-Service -Name $n -ErrorAction SilentlyContinue; "
          "if ($s -and $s.Status -eq 'Running') { $r += $n } }; "
          "Stop-Service -Name @(" + ','.join(f"'{n}'" for n in names) + ") -Force -ErrorAction SilentlyContinue; "
          "'RUNNING:' + ($r -join ',')")
    res = run(['powershell', '-NoProfile', '-Command', ps], timeout=90)
    out = getattr(res, 'stdout', '') or ''
    running = []
    for line in out.splitlines():
        if line.startswith('RUNNING:'):
            running = [x for x in line[8:].strip().split(',') if x]
    logging.warning(f"[TEMPCLEAN] Szolgáltatások leállítva: {names} (előtte futott: {running or 'egyik sem'})")
    return running


def start_services(run, names):
    """A korábban futó szolgáltatások visszaindítása, VISSZAOLVASÁSSAL. Visszaad: azok
    listája, amelyek NEM futnak utána (üres = minden rendben)."""
    if not names:
        return []
    lst = ','.join(f"'{n}'" for n in names)
    ps = (f"Start-Service -Name @({lst}) -ErrorAction SilentlyContinue; Start-Sleep -Milliseconds 800; "
          f"foreach($n in @({lst})) {{ $s=Get-Service -Name $n -ErrorAction SilentlyContinue; "
          "if (-not $s -or $s.Status -ne 'Running') { 'NOTRUNNING:' + $n } }")
    res = run(['powershell', '-NoProfile', '-Command', ps], timeout=90)
    bad = [l[11:].strip() for l in (getattr(res, 'stdout', '') or '').splitlines() if l.startswith('NOTRUNNING:')]
    (logging.error if bad else logging.info)(
        f"[TEMPCLEAN] Szolgáltatások visszaindítva: {names}" + (f" - NEM FUT: {bad}" if bad else ''))
    return bad
