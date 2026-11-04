"""Driver-mentés, -visszaállítás, Windows-alapdriverek kinyerése telepítő-lemezképből,
visszaállítási pont - KÖZÖS mag (GUI + CLI).

2026-11-04-én ÚJRAÍRVA (explicit user decision: *„ez az egész driver mentés visszaállítás
rész … modernizáld … mindent amit le tudsz menteni tudd is visszaállítani”*). A régi
változat három külön mentés-formátumot írt, a visszaállítás egy része élő gépen a Windows
saját drivereit is `pnputil`-lal próbálta felrakni, és egy megszakított DISM-nél holtpontra
futhatott (`_stream_cmd`). A régi formátumokat a visszaállítás továbbra is felismeri.

A MENTÉS FORMÁTUMA (v1):
    DriverVarázsló_Mentés_<ÉÉÉÉ-HH-NN_óó-pp>_<fajta>\\
        mentes.json              gépi leírás (fajta, forrás, darabszámok)
        INFO.txt                 emberi leírás + a kézi visszaállítás parancsai
        ThirdParty\\<csomag>\\    `dism /export-driver` kimenete (csomagonként egy mappa)
        Windows\\FileRepository\\ a Windows SAJÁT driver-csomagjai (a third-partyk nélkül)
        Windows\\INF\\            a Windows saját INF-jei (`oemNN.inf` nélkül)

MI A „THIRD-PARTY” ÉS MI A „WINDOWS” RÉSZ: a Windows minden utólag felrakott csomagot
`%WinDir%\\INF\\oemNN.inf` néven is eltárol, és ennek a tartalma BÁJTRA azonos a
DriverStore-mappájában lévő INF-fel (mérve 2026-11-04, fejlesztői gép: 58/58 egyezés, a
`SetupGetInfDriverStoreLocationW` eredményével összevetve 0 eltérés). Ami a
`FileRepository`-ban így egy oemNN.inf-hez köthető, az third-party, a többi a Windowsé
(`third_party_store_folders`). Ez fájlolvasás, offline lemezen is működik.

Minden hosszú folyamat `log(msg)` callbackkal ír (GUI: task_progress, CLI: konzol), és
opcionális `check_cancel()` callbackkel szakítható meg.
"""

# === AUTO-IMPORTS ===
import glob
import hashlib
import json
import os
import re
import subprocess
import time
import logging
import shutil
from datetime import datetime
from app import bcd_core
from app import common
from app.common import _app_data_dir, file_version
from app.tempclean_core import _long_path
# === /AUTO-IMPORTS ===


class RestoreCancelled(Exception):
    """A felhasználó megszakította a folyamatot (Mégse gomb)."""


# A mentés fajtái: kulcs -> (mappanév-utótag, emberi név).
BACKUP_MODES = {
    'thirdparty': ('ThirdParty', 'third-party driverek'),
    'windows': ('Windows', 'a Windows saját driverei'),
    'all': ('Teljes', 'teljes mentés (third-party + a Windows saját driverei)'),
}
MANIFEST_NAME = 'mentes.json'
INFO_NAME = 'INFO.txt'
TP_DIR = 'ThirdParty'
WIN_DIR = 'Windows'
# A Windows-rész felismert elrendezései (FileRepository-mappa, INF-mappa), a mostanival
# kezdve: a régi teljes export (`_Windows_Inbox_*`) és a régi WIM-kinyerés (`FileRepository`).
_WIN_LAYOUTS = ((os.path.join(WIN_DIR, 'FileRepository'), os.path.join(WIN_DIR, 'INF')),
                ('_Windows_Inbox_Drivers', '_Windows_Inbox_INF'),
                ('FileRepository', 'INF'))
# A kézi törlés automatikus mentései (Driverek kezelése nézet, 2026-11-04). Az 1 kattintásos
# fix mentése a `driver_backup` (wu_core._driver_backup_dir), és azt a fix minden futása
# felülírja - ezért kap a kézi törlés külön, dátumozott mappákat.
MANUAL_BACKUP_DIRNAME = 'driver_backup_kezi'
FIX_BACKUP_DIRNAME = 'driver_backup'
MANUAL_BACKUP_KEEP = 5
# A mentés-mappa bejárásának korlátja: ha valaki egy teljes meghajtót választ ki, ne
# járjuk be percekig - mondjuk ki, hogy pontosabban kell választani.
INSPECT_MAX_DEPTH = 4
INSPECT_MAX_DIRS = 5000
# Ennyi szabad helyet hagyunk a mentés UTÁN is a célmeghajtón.
FREE_SPACE_MARGIN = 512 * 1024 * 1024
_OEM_INF_RE = re.compile(r'^oem\d+\.inf$', re.IGNORECASE)
_STORE_INF_RE = re.compile(r'^(.+?\.inf)_', re.IGNORECASE)
_PROGRESS_RE = re.compile(r'^\[?[=\s]*(\d{1,3}(?:[.,]\d+)?)\s*%')


# ----------------------------------------------------------------------------- utak
def target_root(path):
    """Egy offline cél gyökere (`D:\\`), akár a lemezt, akár a Windows-mappát adták meg."""
    p = os.path.normpath(path or '')
    if os.path.basename(p).lower() == 'windows':
        p = os.path.dirname(p)
    return p if p.endswith('\\') else p + '\\'


def windows_dir(target_os_path=None):
    """A mentés/visszaállítás Windows-mappája: offline célnál `<cél>\\Windows`, különben a futóé."""
    if target_os_path:
        return os.path.join(target_root(target_os_path), 'Windows')
    return os.environ.get('SystemRoot') or r'C:\Windows'


def _store_dir(win_dir):
    return os.path.join(win_dir, 'System32', 'DriverStore', 'FileRepository')


def _sha1(path):
    h = hashlib.sha1()
    with open(_long_path(path), 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(_long_path(path)):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _mb(n):
    return round(n / (1024 * 1024), 1)


def describe_source(target_os_path=None):
    """Emberi szöveg: honnan ment a program."""
    if target_os_path:
        return f'OFFLINE: a(z) {target_root(target_os_path)} meghajtón lévő Windows'
    return f'ez a gép ({os.environ.get("SystemDrive", "C:")}\\, a most futó Windows)'


def describe_windows(root):
    """`Windows 10 · build 19045` egy offline cél kerneléből, vagy '' (nem olvasható)."""
    ver = file_version(os.path.join(root, 'Windows', 'System32', 'ntoskrnl.exe'))
    if not ver:
        return ''
    parts = ver.split('.')
    try:
        build = int(parts[2])
    except (IndexError, ValueError):
        return f'Windows {ver}'
    name = 'Windows 11' if parts[0] == '10' and build >= 22000 else (
        'Windows 10' if parts[0] == '10' else f'Windows (NT {parts[0]}.{parts[1]})')
    return f'{name} · build {build}'


def offline_target_problem(root):
    """Miért NEM lehet ez egy offline visszaállítás célja? None = mehet."""
    if not os.path.isfile(os.path.join(root, 'Windows', 'System32', 'config', 'SYSTEM')):
        return (f'A(z) {root} meghajtón nincs Windows (hiányzik a '
                f'Windows\\System32\\config\\SYSTEM). Válaszd ki a nem induló Windows meghajtóját.')
    sysdrv = os.environ.get('SystemDrive', 'C:')
    if sysdrv.upper() != 'X:' and os.path.normcase(root) == os.path.normcase(sysdrv + '\\'):
        return ('Ez a most futó Windows meghajtója - erre az „Erre a gépre” móddal állíts vissza '
                '(egy futó Windowsba a DISM offline módja nem tud írni).')
    return None


# ------------------------------------------------- third-party vs. a Windows saját része
def third_party_store_folders(win_dir):
    """{oemNN.inf: a DriverStore-mappa neve} kisbetűvel, vagy None (nem olvasható).

    Az `oemNN.inf` a csomag INF-jének bájtra azonos másolata (mérve, lásd a modul leírását),
    ezért a hash dönt. Előbb a méret szűr, hogy ne kelljen a ~700 Windows-INF mindegyikét
    végigolvasni."""
    inf_dir = os.path.join(win_dir, 'INF')
    repo = _store_dir(win_dir)
    try:
        oem_files = glob.glob(os.path.join(inf_dir, 'oem*.inf'))
        folders = os.listdir(_long_path(repo))
    except OSError as e:
        logging.warning(f"[BACKUP] A driver-tár nem olvasható ({repo}): {e}")
        return None
    by_size = {}
    for p in oem_files:
        if not _OEM_INF_RE.match(os.path.basename(p)):
            continue
        try:
            by_size.setdefault(os.path.getsize(p), []).append(p)
        except OSError as e:
            logging.warning(f"[BACKUP] {p} nem olvasható: {e}")
    hashes = {}
    out = {}
    for name in folders:
        m = _STORE_INF_RE.match(name)
        if not m:
            continue
        inf = os.path.join(repo, name, m.group(1))
        try:
            cands = by_size.get(os.path.getsize(_long_path(inf)))
            if not cands:
                continue
            h = _sha1(inf)
        except OSError:
            continue
        for p in cands:
            if p not in hashes:
                hashes[p] = _sha1(p)
            if hashes[p] == h:
                out[os.path.basename(p).lower()] = name.lower()
    missing = sorted(os.path.basename(p).lower() for ps in by_size.values() for p in ps
                     if os.path.basename(p).lower() not in out)
    logging.info(f"[BACKUP] {repo}: {len(folders)} csomag-mappa, ebből {len(set(out.values()))} "
                 f"third-party ({len(out)} oemNN.inf). Mappa nélküli oemNN.inf: {missing[:10] or 'nincs'}.")
    return out


def store_overview(target_os_path=None):
    """A mentés-fajták mérete a felületnek: {'thirdparty': {count, mb}, 'windows': {...}},
    vagy None, ha a driver-tár nem olvasható."""
    win = windows_dir(target_os_path)
    tp = third_party_store_folders(win)
    if tp is None:
        return None
    tp_set = set(tp.values())
    out = {'thirdparty': {'count': 0, 'bytes': 0}, 'windows': {'count': 0, 'bytes': 0}}
    repo = _store_dir(win)
    for name in os.listdir(_long_path(repo)):
        key = 'thirdparty' if name.lower() in tp_set else 'windows'
        out[key]['count'] += 1
        out[key]['bytes'] += dir_size(os.path.join(repo, name))
    for v in out.values():
        v['mb'] = _mb(v.pop('bytes'))
    return out


# ----------------------------------------------------------------------- folyamatok
def export_drivers_cmd(target_os_path, folder):
    """A `dism /export-driver` parancs (élő vagy offline cél). `/English`: a kimenetből
    a haladást olvassuk (CLAUDE.md 8.)."""
    image = f'/Image:{target_root(target_os_path)}' if target_os_path else '/Online'
    return ['dism', '/English', image, '/Export-Driver', f'/Destination:{folder}']


def _stream_cmd(cmd, on_line, check_cancel, si, nw):
    """Parancs futtatása soronkénti kimenet-továbbítással. Megszakításkor NEM lőjük ki a
    folyamatot (egy megszakadt DISM/pnputil sérült Windowst hagyhat), hanem megvárjuk.

    A KIMENETET A MEGSZAKÍTÁS UTÁN IS KIOLVASSUK (2026-11-04): a régi változat a `break`
    után `wait()`-elt, a folyamat pedig egy megtelt csőbe írva örökre megállt (holtpont).
    Visszatérés: (returncode, cancelled)."""
    logging.info(f"[CMD] Popen: {' '.join(str(c) for c in cmd)}")
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               startupinfo=si, creationflags=nw or 0, errors='replace')
    cancelled = False
    for line in process.stdout:
        if not cancelled and check_cancel and check_cancel():
            cancelled = True
            on_line('⚠️ Megszakítás kérve - a futó lépést megvárjuk, hogy a Windows ne sérüljön...')
        stripped = line.strip()
        if stripped and not cancelled:
            on_line(stripped)
    process.wait()
    logging.info(f"[CMD] -> {process.returncode}{' (megszakítás kérve)' if cancelled else ''}")
    return process.returncode, cancelled


def progress_percent(line):
    """A DISM folyamatjelző sorából (`[====  42.0%  ]`) a százalék, vagy None."""
    m = _PROGRESS_RE.match(line or '')
    return float(m.group(1).replace(',', '.')) if m else None


def _copy_windows_part(win_dir, folder, skip_folders, log, check_cancel):
    """A Windows saját driverei: a `FileRepository` csomag-mappái (a `skip_folders`, vagyis
    a third-partyk nélkül) + a nem-oem INF-ek -> `<folder>\\Windows\\...`.

    Fájlmásolás, nem DISM: a Windows saját csomagjait a DISM nem exportálja. Olvasni
    rendszergazdaként szabad, a `\\\\?\\` előtag a 260 karakteres korlát ellen kell.
    Visszatérés: (másolt csomagok, [sikertelen nevek])."""
    src_repo = _store_dir(win_dir)
    dst_repo = os.path.join(folder, WIN_DIR, 'FileRepository')
    names = sorted(n for n in os.listdir(_long_path(src_repo)) if n.lower() not in skip_folders)
    log(f'🧱 A Windows saját drivereinek másolása: {len(names)} csomag...')
    failed = []
    for i, name in enumerate(names, 1):
        if check_cancel and check_cancel():
            raise RestoreCancelled()
        try:
            shutil.copytree(_long_path(os.path.join(src_repo, name)),
                            _long_path(os.path.join(dst_repo, name)), dirs_exist_ok=True)
        except (OSError, shutil.Error) as e:
            failed.append(name)
            logging.warning(f"[BACKUP] {name} másolása sikertelen: {str(e)[:300]}")
        if i % 50 == 0 or i == len(names):
            log(f'   {i}/{len(names)} Windows-csomag másolva')
    src_inf = os.path.join(win_dir, 'INF')
    dst_inf = os.path.join(folder, WIN_DIR, 'INF')
    os.makedirs(dst_inf, exist_ok=True)
    for f in os.listdir(src_inf):
        if f.lower().endswith('.inf') and not _OEM_INF_RE.match(f):
            try:
                shutil.copy2(os.path.join(src_inf, f), os.path.join(dst_inf, f))
            except OSError as e:
                failed.append(f)
                logging.warning(f"[BACKUP] {f} másolása sikertelen: {e}")
    return len(names) - sum(1 for n in failed if n in names), failed


def _write_manifest(folder, mode, source, tp_count, win_count, extra=''):
    created = datetime.now().strftime('%Y-%m-%d %H:%M')
    data = {'formatum': 1, 'fajta': mode, 'keszult': created, 'forras': source,
            'third_party_csomagok': tp_count, 'windows_csomagok': win_count, 'build': common.BUILD_NUMBER}
    try:
        with open(os.path.join(folder, MANIFEST_NAME), 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        lines = [
            'DriverVarázsló - driver-mentés',
            f'Készült: {created} (Build {common.BUILD_NUMBER})',
            f'Forrás: {source}',
            f'Tartalom: {BACKUP_MODES[mode][1]}',
        ]
        if tp_count is not None:
            lines.append(f'   third-party csomagok: {tp_count} db  ->  {TP_DIR}\\')
        if win_count is not None:
            lines.append(f'   a Windows saját csomagjai: {win_count} db  ->  {WIN_DIR}\\')
        if extra:
            lines.append(extra)
        lines += [
            '',
            'VISSZAÁLLÍTÁS A PROGRAMMAL: Mentés és Visszaállítás -> Driverek visszaállítása ->',
            'válaszd ki EZT a mappát. Erre a gépre és egy nem induló Windowsra is megy, és',
            'kiválaszthatod, melyik drivereket rakja vissza.',
            '',
            'KÉZI VISSZAÁLLÍTÁS (rendszergazdai parancssor; D:\\ = a nem induló Windows meghajtója,',
            'WinPE/Sergei alatt más betű is lehet!):',
        ]
        if tp_count is not None:
            lines += [
                f'   third-party, futó Windowsra:       pnputil /add-driver "{folder}\\{TP_DIR}\\*.inf" /subdirs /install',
                f'   third-party, nem induló Windowsra: dism /Image:D:\\ /Add-Driver /Driver:"{folder}\\{TP_DIR}" /Recurse',
            ]
        if win_count is not None:
            lines += [
                '   a Windows saját része (csak a hiányzó fájlok, a meglévőket nem írja felül):',
                f'      robocopy "{folder}\\{WIN_DIR}\\FileRepository" D:\\Windows\\System32\\DriverStore\\FileRepository /E /ZB /XC /XN /XO',
                f'      robocopy "{folder}\\{WIN_DIR}\\INF" D:\\Windows\\INF *.inf /ZB /XC /XN /XO',
            ]
        with open(os.path.join(folder, INFO_NAME), 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
    except OSError as e:
        logging.warning(f"[BACKUP] A mentés leírása nem írható ({folder}): {e}")


def backup_drivers(run, log, check_cancel, dest_root, target_os_path, mode, si=None, nw=None):
    """Driver-mentés egy új, dátumozott mappába a `dest_root` alatt.

    `mode`: 'thirdparty' (dism /export-driver) | 'windows' (a Windows saját csomagjai,
    fájlmásolással) | 'all' (mindkettő). Visszatérés: dict - status: 'ok' | 'partial' |
    'fail' | 'no_space' | 'cancelled'; folder; size_mb; tp_count; win_count; problems."""
    if mode not in BACKUP_MODES:
        raise ValueError(f'Ismeretlen mentés-fajta: {mode!r}')
    win = windows_dir(target_os_path)
    source = describe_source(target_os_path)
    res = {'status': 'fail', 'folder': None, 'size_mb': 0, 'tp_count': None, 'win_count': None,
           'problems': []}
    log(f'Forrás: {source}')
    tp = third_party_store_folders(win)
    if tp is None:
        res['problems'].append(f'A driver-tár nem olvasható: {_store_dir(win)}')
        log(f'❌ {res["problems"][-1]}')
        return res
    tp_set = set(tp.values())
    want_tp, want_win = mode in ('thirdparty', 'all'), mode in ('windows', 'all')
    repo = _store_dir(win)
    need = sum(dir_size(os.path.join(repo, n)) for n in os.listdir(_long_path(repo))
               if (n.lower() in tp_set and want_tp) or (n.lower() not in tp_set and want_win))
    try:
        os.makedirs(dest_root, exist_ok=True)
        free = shutil.disk_usage(dest_root).free
    except OSError as e:
        res['problems'].append(f'A cél nem írható ({dest_root}): {e}')
        log(f'❌ {res["problems"][-1]}')
        return res
    log(f'Becsült méret: {_mb(need)} MB, szabad hely a célon: {_mb(free)} MB')
    if need + FREE_SPACE_MARGIN > free:
        res['status'] = 'no_space'
        res['problems'].append(f'Nincs elég hely: kb. {_mb(need)} MB kellene, {_mb(free)} MB szabad.')
        log(f'❌ {res["problems"][-1]}')
        return res

    stamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    folder = os.path.join(dest_root, f'DriverVarázsló_Mentés_{stamp}_{BACKUP_MODES[mode][0]}')
    os.makedirs(folder, exist_ok=True)
    res['folder'] = folder
    logging.info(f"[BACKUP] Mentés ({mode}) ide: {folder} - forrás: {source}")
    log(f'Célmappa: {folder}')
    try:
        if want_tp:
            if not tp_set:
                log('ℹ️ Ezen a Windowson nincs third-party driver - nincs mit menteni ebből a részből.')
                res['tp_count'] = 0
            else:
                log(f'🧩 Third-party driverek exportálása ({len(tp_set)} csomag, DISM)...')
                tp_dir = os.path.join(folder, TP_DIR)
                os.makedirs(tp_dir, exist_ok=True)
                rc, cancelled = _stream_cmd(export_drivers_cmd(target_os_path, tp_dir), log,
                                            check_cancel, si, nw)
                if cancelled:
                    raise RestoreCancelled()
                got = sum(1 for d in os.listdir(tp_dir) if os.path.isdir(os.path.join(tp_dir, d)))
                res['tp_count'] = got
                if rc != 0 or got < len(tp_set):
                    res['problems'].append(f'A third-party export {got}/{len(tp_set)} csomagot mentett '
                                           f'(DISM kód: {rc}).')
                    log(f'⚠️ {res["problems"][-1]}')
                else:
                    log(f'✅ {got} third-party csomag elmentve.')
        if want_win:
            done, failed = _copy_windows_part(win, folder, tp_set, log, check_cancel)
            res['win_count'] = done
            if failed:
                res['problems'].append(f'{len(failed)} Windows-elem másolása nem sikerült: '
                                       f'{", ".join(failed[:6])}')
                log(f'⚠️ {res["problems"][-1]}')
            else:
                log(f'✅ A Windows saját drivereiből {done} csomag elmentve.')
    except RestoreCancelled:
        res['status'] = 'cancelled'
        log('❗ Megszakítva - a félkész mentés-mappa megmaradt, nyugodtan törölheted.')
        return res
    _write_manifest(folder, mode, source, res['tp_count'], res['win_count'])
    res['size_mb'] = _mb(dir_size(folder))
    res['status'] = 'partial' if res['problems'] else 'ok'
    logging.info(f"[BACKUP] Kész: {res}")
    return res


# ------------------------------------------------------------- mentés felismerése
def _read_info_created(folder):
    for name in (MANIFEST_NAME, INFO_NAME):
        p = os.path.join(folder, name)
        try:
            with open(p, encoding='utf-8', errors='replace') as f:
                text = f.read(4000)
        except OSError:
            continue
        if name == MANIFEST_NAME:
            try:
                return json.loads(text).get('keszult') or ''
            except ValueError:
                continue
        m = re.search(r'Készült:\s*([0-9][0-9:\- .]+)', text)
        if m:
            return m.group(1).strip()
    try:
        return datetime.fromtimestamp(os.path.getmtime(folder)).strftime('%Y-%m-%d %H:%M')
    except OSError:
        return ''


def _package_entry(path, infs, root):
    """Egy csomag-mappa adatai a felületnek, a fő INF [Version] szekciójából."""
    from app.wu_core import _read_text_best_effort
    from app.driverusage_core import parse_inf_models
    base = os.path.basename(path).lower()
    main = next((f for f in infs if base.startswith(f.lower())), infs[0])
    entry = {'dir': path, 'rel': os.path.relpath(path, root), 'inf': main, 'infs': infs,
             'name': '', 'provider': '', 'cls': '', 'version': '', 'date': ''}
    try:
        models = parse_inf_models(_read_text_best_effort(os.path.join(path, main)) or '')
        ver = models.get('version') or {}
        entry['provider'] = ver.get('provider', '')
        entry['cls'] = ver.get('class', '')
        dv = [p.strip() for p in (ver.get('driverver') or '').split(',')]
        entry['date'] = dv[0] if dv else ''
        entry['version'] = dv[1] if len(dv) > 1 else ''
        entry['name'] = (models.get('descriptions') or [''])[0]
    except Exception as e:
        logging.debug(f"[RESTORE] {path}\\{main} [Version] nem olvasható: {e}")
    return entry


def inspect_backup(path):
    """Mi van egy mentés-mappában? A visszaállítás ÉS a felület előnézete ebből dolgozik.

    Felismeri a v1 formátumot, a régi teljes exportot, a régi WIM-kinyerést, a fix és a
    kézi törlés automatikus mentéseit, és bármilyen mappát, amiben .inf van. Egy csomag
    BELSEJÉBEN lévő további INF a csomag része (nem külön csomag), ezért oda nem lépünk be.
    Visszatérés: {'ok', 'error', 'path', 'kind', 'created', 'source', 'packages': [...],
    'tp_root', 'tp_root_clean', 'windows': {'repo', 'inf', 'count'} | None}."""
    root = os.path.normpath(path or '')
    out = {'ok': False, 'error': None, 'path': root, 'kind': '', 'created': '', 'source': '',
           'packages': [], 'tp_root': root, 'tp_root_clean': True, 'windows': None}
    if not path or not os.path.isdir(root):
        out['error'] = f'A mappa nem létezik: {path}'
        return out
    manifest = {}
    try:
        with open(os.path.join(root, MANIFEST_NAME), encoding='utf-8') as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        pass
    skip = set()
    for repo_rel, inf_rel in _WIN_LAYOUTS:
        repo = os.path.join(root, repo_rel)
        if os.path.isdir(repo):
            inf = os.path.join(root, inf_rel)
            try:
                count = sum(1 for d in os.listdir(_long_path(repo)) if os.path.isdir(os.path.join(repo, d)))
            except OSError:
                count = 0
            out['windows'] = {'repo': repo, 'inf': inf if os.path.isdir(inf) else None, 'count': count}
            skip = {repo_rel.split(os.sep)[0].lower(), inf_rel.split(os.sep)[0].lower()}
            break
    tp_root = os.path.join(root, TP_DIR)
    if not os.path.isdir(tp_root):
        tp_root = root
        out['tp_root_clean'] = not skip
    out['tp_root'] = tp_root
    base_depth = tp_root.rstrip('\\').count('\\')
    visited = 0
    for cur, dirs, files in os.walk(tp_root):
        visited += 1
        if visited > INSPECT_MAX_DIRS:
            out['error'] = ('Ez a mappa túl nagy egy driver-mentéshez (több ezer almappa) - '
                            'válaszd ki pontosabban a mentés mappáját.')
            return out
        if os.path.normcase(cur) == os.path.normcase(root) and skip:
            dirs[:] = [d for d in dirs if d.lower() not in skip]
        infs = sorted(f for f in files if f.lower().endswith('.inf'))
        if infs:
            out['packages'].append(_package_entry(cur, infs, root))
            dirs[:] = []
            continue
        if cur.rstrip('\\').count('\\') - base_depth >= INSPECT_MAX_DEPTH:
            dirs[:] = []
    out['packages'].sort(key=lambda p: (p['cls'].lower(), (p['name'] or p['inf']).lower()))
    if not out['packages'] and not out['windows']:
        out['error'] = 'Ebben a mappában nincs driver-mentés (egyetlen .inf fájl sincs benne).'
        return out
    mode = manifest.get('fajta')
    if mode in BACKUP_MODES:
        kind = BACKUP_MODES[mode][1][0].upper() + BACKUP_MODES[mode][1][1:]
    elif out['windows'] and out['windows']['repo'].lower().endswith('_windows_inbox_drivers'):
        kind = 'Régi DriverVarázsló teljes mentés'
    elif out['windows']:
        kind = 'A Windows saját driverei (régi ISO/WIM-kinyerés)'
    elif os.path.basename(root).lower() == FIX_BACKUP_DIRNAME:
        kind = 'Automatikus mentés az 1 kattintásos fix törlése előtt'
    elif os.path.basename(os.path.dirname(root)).lower() == MANUAL_BACKUP_DIRNAME:
        kind = 'Automatikus mentés egy kézi törlés előtt'
    else:
        kind = 'Driver-mappa'
    out.update({'ok': True, 'kind': kind, 'created': _read_info_created(root),
                'source': manifest.get('forras', '')})
    return out


def list_auto_backups(target_os_path=None):
    """A program automatikus mentései ezen a lemezen (a fix és a kézi törlés előtti),
    a legfrissebb elöl. Offline célnál a cél-lemez `DriverVarazslo` mappáját nézzük."""
    base = (os.path.join(target_root(target_os_path), 'DriverVarazslo') if target_os_path
            else _app_data_dir())
    cands = []
    kezi = os.path.join(base, MANUAL_BACKUP_DIRNAME)
    if os.path.isdir(kezi):
        cands += [(os.path.join(kezi, d), 'Kézi törlés előtt') for d in os.listdir(kezi)]
    cands.append((os.path.join(base, FIX_BACKUP_DIRNAME), '1 kattintásos fix előtt'))
    out = []
    for path, kind in cands:
        if not os.path.isdir(path):
            continue
        pkg_root = os.path.join(path, TP_DIR) if os.path.isdir(os.path.join(path, TP_DIR)) else path
        try:
            count = sum(1 for d in os.listdir(pkg_root) if os.path.isdir(os.path.join(pkg_root, d)))
        except OSError:
            count = 0
        if count:
            out.append({'path': path, 'kind': kind, 'created': _read_info_created(path), 'count': count})
    out.sort(key=lambda b: b['created'], reverse=True)
    return out


def manual_backup_root(target_os_path=None):
    """A kézi törlés mentéseinek gyökere: `<SystemDrive>\\DriverVarazslo\\driver_backup_kezi`,
    offline célnál a CÉL lemezén (WinPE-ben a SystemDrive az X: RAM-lemez)."""
    base = (os.path.join(target_root(target_os_path), 'DriverVarazslo') if target_os_path
            else _app_data_dir())
    return os.path.join(base, MANUAL_BACKUP_DIRNAME)


def prune_manual_backups(root, keep=MANUAL_BACKUP_KEEP):
    """A kézi törlés mentései közül csak a legfrissebb `keep` marad (a mappanév dátummal
    kezdődik vagy tartalmazza, ezért a mtime dönt). Egy teljes törlés mentése 1-2,5 GB:
    korlát nélkül egy ügyfélgép lemezét töltené meg."""
    try:
        dirs = [os.path.join(root, d) for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]
    except OSError:
        return []
    dirs.sort(key=os.path.getmtime, reverse=True)
    removed = []
    for d in dirs[keep:]:
        logging.info(f"[BACKUP] Régi kézi-törlés mentés törlése (csak a legfrissebb {keep} marad): {d}")
        shutil.rmtree(_long_path(d), ignore_errors=True)
        removed.append(d)
    return removed


# --------------------------------------------------------------------- visszaállítás
def _robocopy_missing(run, src, dst, files=None):
    """Csak a HIÁNYZÓ fájlok másolása (`/XC /XN /XO`: a meglévőt semmilyen irányban nem
    írja felül), backup-módban (`/ZB`: a DriverStore-ba a rendszergazda sem írhat másképp).
    Visszatérés: 'copied' | 'nothing' | 'failed' (a robocopy 8+ kódja hiba)."""
    cmd = ['robocopy', src, dst] + (files or []) + (['/XF', 'oem*.inf', 'oem*.pnf'] if files else ['/E']) + [
        '/ZB', '/XC', '/XN', '/XO', '/R:0', '/W:0', '/NFL', '/NDL', '/NJH', '/NJS', '/NC', '/NS', '/NP']
    rc = run(cmd).returncode
    if rc >= 8:
        return 'failed'
    return 'copied' if rc & 1 else 'nothing'


def _write_rescan_script(root, log):
    """Az offline visszaállítás után az első bejelentkezéskor egy `pnputil /scan-devices`
    (az eszközök felismerik az új drivereket), utána a szkript törli magát."""
    startup_dir = os.path.join(root, 'ProgramData', 'Microsoft', 'Windows', 'Start Menu', 'Programs', 'Startup')
    bat_path = os.path.join(startup_dir, 'auto_pnputil_scan.bat')
    try:
        os.makedirs(startup_dir, exist_ok=True)
        with open(bat_path, 'w', encoding='utf-8') as f:
            f.write('@echo off\n'
                    'set LOGFILE="%SystemDrive%\\Users\\Public\\driver_startup_log.txt"\n'
                    'echo [%DATE% %TIME%] Boot rescan indult... >> %LOGFILE%\n'
                    'pnputil /scan-devices >> %LOGFILE% 2>&1\n'
                    'echo [%DATE% %TIME%] Kesz! >> %LOGFILE%\n'
                    'ping 127.0.0.1 -n 3 > nul\n'
                    '(goto) 2>nul & del "%~f0"\n')
        logging.info(f"[RESTORE] Első-bejelentkezési eszköz-újraszkennelés: {bat_path}")
        log('✅ Az első bejelentkezéskor a Windows újra felismeri az eszközöket (egyszer futó szkript).')
    except OSError as e:
        logging.warning(f"[RESTORE] A rescan-szkript nem írható ({bat_path}): {e}")
        log(f'⚠️ Az első-bejelentkezési szkript nem írható: {e}')


def restore_drivers(run, log, check_cancel, si, nw, source, target_os_path=None,
                    package_dirs=None, include_windows=True, fix_boot=True):
    """Driverek visszaállítása egy mentésből (bármelyik felismert formátumból).

    `target_os_path` None = erre a gépre (pnputil), különben egy nem induló Windowsra (DISM).
    `package_dirs`: a visszarakandó third-party csomag-mappák (None = mind). `include_windows`:
    a Windows saját részéből a HIÁNYZÓ fájlok visszamásolása (a meglévőket soha nem írja
    felül - egy másik Windows-verzió INF-je egy működő driver mellé rakva több kárt tenne).
    `fix_boot`: offline célnál a BCD újraírása is.
    Visszatérés: {'status': 'ok'|'errors'|'cancelled'|'fail', 'tp_done', 'tp_failed',
    'windows': None|'copied'|'nothing'|'failed', 'boot': None|bool}."""
    res = {'status': 'fail', 'tp_done': 0, 'tp_failed': [], 'windows': None, 'boot': None}
    info = inspect_backup(source)
    if not info['ok']:
        log(f'❌ {info["error"]}')
        return res
    offline = bool(target_os_path)
    root = target_root(target_os_path) if offline else None
    if offline:
        problem = offline_target_problem(root)
        if problem:
            log(f'❌ {problem}')
            return res
    pkgs = info['packages']
    if package_dirs is not None:
        want = {os.path.normcase(os.path.normpath(p)) for p in package_dirs}
        pkgs = [p for p in pkgs if os.path.normcase(p['dir']) in want]
    # Ha MINDEN csomag megy, és a gyökér csak third-party csomag-mappákat tartalmaz (se a
    # Windows-rész, se maga nem egy csomag), egyetlen parancs elég - offline ez sokat
    # számít: a DISM minden híváskor újra betölti a lemezképet.
    single = (bool(pkgs) and len(pkgs) == len(info['packages']) and info['tp_root_clean']
              and all(os.path.normcase(p['dir']) != os.path.normcase(info['tp_root']) for p in pkgs))
    groups = [(info['tp_root'], f'mind a(z) {len(pkgs)} csomag')] if single else [
        (p['dir'], f'{p["inf"]}' + (f' ({p["provider"]})' if p['provider'] else '')) for p in pkgs]
    where = f'a(z) {root} meghajtón lévő Windowsra' if offline else 'erre a gépre'
    log(f'Mentés: {info["path"]} - {info["kind"]}')
    log(f'Cél: {where}. Third-party csomag: {len(pkgs)} db; a Windows saját része: '
        f'{"igen (csak a hiányzó fájlok)" if include_windows and info["windows"] else "nem"}.')
    logging.info(f"[RESTORE] {info['path']} -> {root or 'élő rendszer'}: {len(pkgs)} csomag, "
                 f"windows={bool(include_windows and info['windows'])}, boot={fix_boot and offline}")
    scratch = os.path.join(root, 'DV_Temp', 'Scratch') if offline else None
    if scratch:
        os.makedirs(scratch, exist_ok=True)
    errors = False
    for i, (g, label) in enumerate(groups, 1):
        if check_cancel and check_cancel():
            res['status'] = 'cancelled'
            return res
        whole = single
        log(f'\n🧩 [{i}/{len(groups)}] {label}')
        if offline:
            cmd = ['dism', '/English', f'/Image:{root}', '/Add-Driver', f'/Driver:{g}', '/ForceUnsigned',
                   f'/ScratchDir:{scratch}'] + (['/Recurse'] if whole else [])
            ok_codes = (0, 3010)
        else:
            cmd = ['pnputil', '/add-driver', os.path.join(g, '*.inf'), '/install'] + (['/subdirs'] if whole else [])
            ok_codes = (0, 259, 3010)     # 259 = nincs hozzá jelenlévő eszköz / már fent van
        rc, cancelled = _stream_cmd(cmd, log, check_cancel, si, nw)
        if cancelled:
            res['status'] = 'cancelled'
            return res
        if rc in ok_codes:
            res['tp_done'] += len(pkgs) if whole else 1
        else:
            errors = True
            res['tp_failed'].append(label)
            logging.error(f"[RESTORE] {label}: {cmd[0]} {rc} kóddal tért vissza.")
            log(f'   ❌ Nem sikerült (kód: {rc}).')
    if include_windows and info['windows']:
        if check_cancel and check_cancel():
            res['status'] = 'cancelled'
            return res
        win = windows_dir(target_os_path)
        log('\n🧱 A Windows saját drivereiből a hiányzó fájlok visszamásolása...')
        a = _robocopy_missing(run, info['windows']['repo'], _store_dir(win))
        b = (_robocopy_missing(run, info['windows']['inf'], os.path.join(win, 'INF'), ['*.inf'])
             if info['windows']['inf'] else 'nothing')
        res['windows'] = 'failed' if 'failed' in (a, b) else ('copied' if 'copied' in (a, b) else 'nothing')
        log({'failed': '   ❌ A visszamásolás hibával ért véget (a napló tartalmazza a robocopy kódját).',
             'copied': '   ✅ A hiányzó Windows-driverfájlok visszakerültek.',
             'nothing': '   ✅ Egy fájl sem hiányzott - a Windows saját drivereihez nem kellett nyúlni.'}[res['windows']])
        errors = errors or res['windows'] == 'failed'
    if scratch:
        shutil.rmtree(scratch, ignore_errors=True)
    if offline:
        if fix_boot:
            log('\n🔧 A boot-betöltő (BCD) javítása...')
            res['boot'] = bool(bcd_core.repair_bcd(run, log, root))
            if not res['boot']:
                errors = True
                log('❌ A BCD javítása NEM sikerült - ha a gép nem indul, futtasd a BootFixert '
                    '(Mentés és Visszaállítás → BootFixer).')
        if res['tp_done'] or res['windows'] == 'copied':
            _write_rescan_script(root, log)
    elif os.environ.get('SystemDrive', 'C:').upper() != 'X:' and (res['tp_done'] or res['windows'] == 'copied'):
        log('\nAz eszközök újraszkennelése...')
        run(['pnputil', '/scan-devices'])
        time.sleep(5)
    res['status'] = 'errors' if errors else 'ok'
    logging.info(f"[RESTORE] Kész: {res}")
    return res


# ------------------------------------------------ Windows-driverek telepítő-lemezképből
def _wim_info(run, image):
    """`Windows 10 Pro · 10.0.19041` a lemezkép 1. indexéről, vagy ''."""
    out = run(['dism', '/English', '/Get-WimInfo', f'/WimFile:{image}', '/Index:1']).stdout or ''
    name = re.search(r'^Name\s*:\s*(.+)$', out, re.M)
    ver = re.search(r'^Version\s*:\s*(.+)$', out, re.M)
    return ' · '.join(m.group(1).strip() for m in (name, ver) if m)


def extract_windows_drivers(run, log, check_cancel, image_path, dest_root, target_os_path=None):
    """A Windows saját driverei egy telepítő-lemezkép (`install.wim` / `install.esd`) 1.
    indexéből -> egy „a Windows saját driverei” mentés (v1), amit a visszaállítás ugyanúgy
    kezel, mint a gépről készültet. Az ESD-t előbb WIM-mé alakítja (az ESD nem csatolható).
    A csatolás csak olvasható, és hibánál/megszakításkor is leválasztjuk.
    Visszatérés: dict - status: 'ok' | 'partial' | 'fail' | 'cancelled'; folder; count; error."""
    image = os.path.abspath(image_path or '')
    res = {'status': 'fail', 'folder': None, 'count': 0, 'error': None, 'size_mb': 0}
    if not os.path.isfile(image) or not image.lower().endswith(('.wim', '.esd')):
        res['error'] = f'Nem install.wim / install.esd fájl: {image_path}'
        log(f'❌ {res["error"]}')
        return res
    # A csatolási mappa NTFS-en legyen (cserélhető USB-t a DISM elutasítja); WinPE-ben a
    # SystemDrive az X: RAM-lemez, oda nagy fájlt soha.
    if os.environ.get('SystemDrive', 'C:').upper() == 'X:':
        work = os.path.join(target_root(target_os_path), 'DV_Temp') if target_os_path else r'C:\DV_Temp'
    else:
        work = os.environ.get('SystemDrive', 'C:') + '\\DV_Temp'
    mount_dir = os.path.join(work, f'WIM_{int(time.time())}')
    temp_wim = None
    mounted = False
    desc = _wim_info(run, image)
    log(f'Lemezkép: {image}' + (f' ({desc})' if desc else ''))
    try:
        os.makedirs(mount_dir, exist_ok=True)
        if image.lower().endswith('.esd'):
            need = os.path.getsize(image) * 2
            free = shutil.disk_usage(work).free
            if need > free:
                raise RuntimeError(f'Nincs elég hely az ESD átalakításához: kb. {_mb(need)} MB kellene, '
                                   f'{_mb(free)} MB szabad ({work}).')
            temp_wim = os.path.join(work, f'converted_{int(time.time())}.wim')
            log('ESD → WIM átalakítás (ez 10-15 perc is lehet)...')
            r = run(['dism', '/English', '/Export-Image', f'/SourceImageFile:{image}', '/SourceIndex:1',
                     f'/DestinationImageFile:{temp_wim}', '/Compress:max', '/CheckIntegrity'])
            if r.returncode != 0:
                raise RuntimeError(f'Az ESD átalakítása nem sikerült (DISM kód: {r.returncode}).')
        if check_cancel and check_cancel():
            raise RestoreCancelled()
        log('A lemezkép csatolása (csak olvasásra, pár perc)...')
        r = run(['dism', '/English', '/Mount-Image', f'/ImageFile:{temp_wim or image}', '/Index:1',
                 f'/MountDir:{mount_dir}', '/ReadOnly'])
        if r.returncode != 0:
            raise RuntimeError(f'A lemezkép csatolása nem sikerült (DISM kód: {r.returncode}).')
        mounted = True
        win = os.path.join(mount_dir, 'Windows')
        if not os.path.isdir(_store_dir(win)):
            raise RuntimeError('A lemezképben nincs Windows\\System32\\DriverStore\\FileRepository.')
        stamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        folder = os.path.join(dest_root, f'DriverVarázsló_Mentés_{stamp}_Windows_ISO')
        os.makedirs(folder, exist_ok=True)
        res['folder'] = folder
        tp = third_party_store_folders(win) or {}
        done, failed = _copy_windows_part(win, folder, set(tp.values()), log, check_cancel)
        res['count'] = done
        _write_manifest(folder, 'windows', f'telepítő-lemezkép: {image}' + (f' ({desc})' if desc else ''),
                        None, done, extra='FONTOS: csak ugyanolyan verziójú Windowsra rakd vissza!')
        res['size_mb'] = _mb(dir_size(folder))
        res['status'] = 'partial' if failed else 'ok'
        if failed:
            res['error'] = f'{len(failed)} elem másolása nem sikerült: {", ".join(failed[:6])}'
        return res
    except RestoreCancelled:
        res['status'] = 'cancelled'
        return res
    except Exception as e:
        logging.error(f"[WIM] {e}", exc_info=True)
        res['error'] = str(e)
        log(f'❌ {e}')
        return res
    finally:
        if mounted:
            log('A lemezkép leválasztása...')
            run(['dism', '/English', '/Unmount-Image', f'/MountDir:{mount_dir}', '/Discard'])
        else:
            run(['dism', '/English', '/Cleanup-Wim'])
        shutil.rmtree(mount_dir, ignore_errors=True)
        if temp_wim and os.path.exists(temp_wim):
            try:
                os.remove(temp_wim)
            except OSError as e:
                logging.warning(f"[WIM] Az átalakított ideiglenes WIM nem törölhető ({temp_wim}): {e}")


# ------------------------------------------------------------- visszaállítási pont
def create_restore_point(run, log, desc=None):
    """Visszaállítási pont létrehozása (rendszervédelem bekapcsolása fallbackkal,
    24 órás limit feloldása, Checkpoint-Computer + utólagos ellenőrzés).
    Visszatérés: (status, desc) - status: 'ok' | 'ok_unverified' | 'enable_failed' | 'fail'."""
    desc = desc or f"DriverVarázsló_Backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    logging.info("[RESTORE_POINT] Rendszervédelem engedélyezése...")
    log('Rendszervédelem engedélyezése...')
    enable_ps = '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; try { Enable-ComputerRestore -Drive "$($env:SystemDrive)\\" -ErrorAction Stop; Write-Output "OK" } catch { Write-Output "FAIL: $($_.Exception.Message)" }'
    enable_res = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", enable_ps], encoding='utf-8')
    enable_out = (enable_res.stdout or '').strip()
    if 'FAIL' in enable_out:
        logging.warning(f"[RESTORE_POINT] Enable-ComputerRestore hiba: {enable_out}")
        log(f'⚠ Enable-ComputerRestore hiba: {enable_out}\nRegistry + vssadmin fallback...')
        run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore', '/v', 'DisableSR', '/t', 'REG_DWORD', '/d', '0', '/f'])
        run(['vssadmin', 'resize', 'shadowstorage', f'/for={os.environ.get("SystemDrive", "C:")}', f'/on={os.environ.get("SystemDrive", "C:")}', '/maxsize=5%'])
        enable_res2 = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", enable_ps], encoding='utf-8')
        enable_out2 = (enable_res2.stdout or '').strip()
        if 'FAIL' in enable_out2:
            logging.error(f"[RESTORE_POINT] Rendszervédelem nem kapcsolható be: {enable_out2}")
            log(f'❌ Rendszervédelem nem kapcsolható be: {enable_out2}')
            return 'enable_failed', desc
        logging.info("[RESTORE_POINT] Rendszervédelem bekapcsolva (fallback)")
        log('✅ Rendszervédelem bekapcsolva (fallback)')
    else:
        logging.info("[RESTORE_POINT] Rendszervédelem bekapcsolva")
        log('✅ Rendszervédelem bekapcsolva')

    logging.info("[RESTORE_POINT] 24 órás limit feloldása...")
    run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore',
         '/v', 'SystemRestorePointCreationFrequency', '/t', 'REG_DWORD', '/d', '0', '/f'])

    logging.info("[RESTORE_POINT] Checkpoint-Computer futtatása...")
    log(f'Visszaállítási pont: {desc}')
    create_ps = f'[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; try {{ Checkpoint-Computer -Description "{desc}" -RestorePointType "MODIFY_SETTINGS" -ErrorAction Stop; Write-Output "OK" }} catch {{ Write-Output "FAIL: $($_.Exception.Message)" }}'
    res = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", create_ps], encoding='utf-8')
    create_out = (res.stdout or '').strip()
    logging.debug(f"[RESTORE_POINT] Checkpoint result: {create_out}")

    logging.info("[RESTORE_POINT] Ellenőrzés...")
    verify_ps = f'[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; (Get-ComputerRestorePoint | Where-Object {{ $_.Description -eq "{desc}" }}).Description'
    verify_res = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", verify_ps], encoding='utf-8')
    verified = desc in (verify_res.stdout or '')
    logging.debug(f"[RESTORE_POINT] Verified: {verified}")

    if 'OK' in create_out and verified:
        logging.info(f"[RESTORE_POINT] Sikeresen létrehozva: {desc}")
        return 'ok', desc
    if 'OK' in create_out:
        logging.warning("[RESTORE_POINT] Lefutott de nem ellenőrizhető (késleltetett létrehozás?)")
        return 'ok_unverified', desc
    logging.error(f"[RESTORE_POINT] Hiba: {create_out}")
    log(f'❌ Hiba: {create_out}')
    return 'fail', desc


def create_restore_point_quick(run, desc):
    """Gyors (nem ellenőrzött) visszaállítási pont - az AutoFix és a kézi törlés/telepítés
    használja, ahol egy elutasított pont nem állítja meg a folyamatot. Visszatérés:
    sikerült-e a parancs.

    A `-ErrorAction SilentlyContinue` mellett a kilépési kód MINDIG 0 volt, tehát a lánc
    akkor is "✅ Visszaállítási pont elkészült"-t írt, ha a Windows elutasította (2026-10-12).
    Most a parancs kimenete (OK/FAIL) dönt."""
    run(["powershell", "-NoProfile", "-Command", 'Enable-ComputerRestore -Drive "$($env:SystemDrive)\\" -ErrorAction SilentlyContinue'])
    run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore', '/v', 'SystemRestorePointCreationFrequency', '/t', 'REG_DWORD', '/d', '0', '/f'])
    ps_cmd = (f'try {{ Checkpoint-Computer -Description "{desc}" -RestorePointType "MODIFY_SETTINGS" '
              f'-ErrorAction Stop; Write-Output "RPOK" }} catch {{ Write-Output ("RPFAIL: " + $_.Exception.Message) }}')
    res = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd], encoding='utf-8')
    out = (res.stdout or '').strip()
    ok = 'RPOK' in out
    if not ok:
        logging.warning(f"[RESTORE_POINT] A gyors visszaállítási pont nem készült el: {out[:300]}")
    return ok
