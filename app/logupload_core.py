"""Debug-log feltöltése a bolt saját Google Drive-jára - és VISSZATÖLTÉSE onnan.

MINDKÉT IRÁNY ITT VAN, szándékosan: a modul neve a feltöltésről szól, de a letöltés
ugyanarra a végpontra, ugyanazzal a csomagolással és ugyanazzal a HTTP-réteggel megy -
két fájlba szétvágva a végpont-tudás duplikálódna, ami ennek a projektnek a legrégebbi
visszatérő hibája (lásd CLAUDE.md, Build 192).

MIÉRT KELL (explicit user decision, 2026-08-26): ennek a projektnek a hibajelentése MAGA A
NAPLÓ - a CLAUDE.md is ezzel kezdődik ("a debug log az elsődleges terepi diagnózis-eszköz").
A gyakorlatban viszont a naplót kézzel kell összeszedni a szervizelt gépről, és pont akkor
marad el, amikor a legfontosabb lenne: a technikus otthon/ügyfélnél futtat egy fixet, lát
valami furcsát, aztán a gép már az ügyfélnél van, a napló elérhetetlen. Ez a modul ezt
oldja meg: a lánc végén a napló magától felmegy a bolt Drive-jára.

MIT KÜLD ÉS HOVA: a `DriverVarázsló_debug.log` (+ a rotált `.log.1`) tömörítve, a
Benchmark-ranglista MÁR MEGLÉVŐ Google Apps Script végpontjára (benchmark_defs.py:
BENCHMARK_API_URL_DEFAULT), `action="log"` megkülönböztetéssel. Nincs új szolgáltatás, új
fiók vagy új titok - ugyanaz a végpont, amit a bolt már használ.

ADATVÉDELEM - ezt tudni kell: a napló a szervizelt gép adatait tartalmazza (gépnév,
hardver-azonosítók, sorozatszámok, telepített driverek és programok nevei). A bolt SAJÁT
Drive-jára megy, de attól még ügyféladat. Ezért: a feltöltés a felhasználó kifejezett
kérésére került be, a lánc végén FUT, de HIBÁJA SOSEM akasztja meg a fixet, és a napló
minden esetben ott marad a gépen is (`_app_data_dir()`), tehát a feltöltés nem "elviszi",
csak másolatot küld.

MIÉRT TÖMÖRÍTVE: a napló szöveges, tehát nagyon jól tömörödik (mérve ~10:1), és a Drive-ra
egy ~800 KB-os csomag megy 8 MB helyett. A base64 a JSON-ba ágyazás miatt kell.
"""
import io
import os
import re
import gzip
import json
import base64
import logging
import platform

from app.common import _app_data_dir

# A napló fájlnevei (a rotáló handler a `.1` végűt hagyja hátra) - lásd driver_tool.py.
LOG_BASENAME = 'DriverVarázsló_debug.log'

# A becsomagolt (gzip+base64) küldemény felső korlátja. Az Apps Script Web App bőven
# elbírna többet is, de egy elszabadult napló nem tarthatja fel a lánc lezárását.
# Túllépésnél a RÉGEBBI naplót hagyjuk el, az AKTUÁLISAT sosem - abban van a most futott
# lánc, az érdekes.
UPLOAD_MAX_BYTES = 12 * 1024 * 1024

# Ha maga az aktuális napló is nagyobb ennél, csak a VÉGÉT küldjük: a lánc utolsó szakasza
# (a záró kör, a jelentés, a hibák) mindig a fájl végén van.
SINGLE_LOG_TAIL_BYTES = 20 * 1024 * 1024


# ===========================================================================
# GÉPENKÉNT KÜLÖN NAPLÓ (2026-10-21, explicit user decision)
# ===========================================================================
# *"nagyon sok olyan van hogy 1 db ssd van, és sok gépre rákötöm ezért a logok
# felgyülemlenek 1 log fileba"* - a terepi napló (T440s, 2026-10-21) négy gép két hónapját
# hordta (20KGS4032P, 20ARS21Y0B, ...), és a Drive-ra is így ment fel, a mostani gép
# nevével. Ezért: minden naplófájl első sora a gép azonosítója (`_BomRotatingFileHandler`
# írja, új fájlnál és rotációnál is), és ha indításkor a meglévő napló MÁSIK gépé, az
# átkerül a `naplok_korabbi_gepek\<dátum> - <gép>` mappába (nem törlődik), a mostani gép
# pedig üres naplóval indul. Az azonosító a firmware sorozatszámaiból jön (két egyforma
# típusú gépet is megkülönböztet), subprocess nélkül - a naplózás beállítása előtt fut.
MACHINE_HEADER_PREFIX = '# GEP-AZONOSITO: '
OTHER_MACHINES_SUBDIR = 'naplok_korabbi_gepek'
_HEADER_RE = re.compile(r'^﻿?# GEP-AZONOSITO: (\w+)\s*\|\s*(.*)$')


def machine_log_identity(hw=None, serials=None):
    """(kulcs, címke) a futó gépre; kulcs = None, ha semmi nem azonosítja (3. elv).

    `hw`: `win32.read_hardware_identity()`, `serials`: `win32.read_smbios_serials()`
    (a tesztek adják be)."""
    import hashlib
    from app import machine_core
    if hw is None or serials is None:
        from app import win32
        hw = win32.read_hardware_identity() if hw is None else hw
        serials = win32.read_smbios_serials() if serials is None else serials
    ph = machine_core.is_placeholder

    def v(x):
        x = str(x or '').strip()
        return '' if ph(x) or x.lower() in ('system serial number', '0123456789') else x

    vendor, product = v(hw.get('SystemManufacturer')), v(hw.get('SystemProductName'))
    bvendor, bproduct = v(hw.get('BaseBoardManufacturer')), v(hw.get('BaseBoardProduct'))
    serial, bserial = v(serials.get('system_serial')), v(serials.get('board_serial'))
    uuid = v(serials.get('system_uuid'))
    parts = [vendor, product, serial, uuid, bvendor, bproduct, bserial]
    if not any(parts):
        return None, 'ismeretlen gép'
    key = hashlib.sha1('|'.join(p.lower() for p in parts).encode('utf-8')).hexdigest()[:12]
    if vendor or product:
        name = product if _name_covers_vendor(product, vendor) else f'{_trim_vendor_suffix(vendor)} {product}'
        sn = serial or bserial
    else:
        name, sn = f'{_trim_vendor_suffix(bvendor)} {bproduct}', bserial or serial
    label = ' '.join(name.split()) + (f' · SN {sn}' if sn else '')
    return key, label


def log_header_line(key, label):
    return f"{MACHINE_HEADER_PREFIX}{key or 'ismeretlen'} | {label}"


def _read_log_header(path):
    """(kulcs, címke) a napló első sorából; (None, '') ha nincs fejléc (régi napló)."""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            m = _HEADER_RE.match(f.readline().rstrip('\r\n'))
    except OSError:
        return None, ''
    return (m.group(1), m.group(2).strip()) if m else (None, '')


def separate_other_machine_log(log_dir, key):
    """Ha a meglévő napló MÁSIK gépé (vagy fejléc nélküli, több gépet keverő régi napló),
    a fájlok (`.log`, `.log.1`, `.log.2`) átkerülnek a `naplok_korabbi_gepek` alá.

    A naplózás beállítása ELŐTT fut, ezért nem naplóz: (célmappa, régi címke, hibák) a
    visszatérés, a hívó írja ki. Semmi nem történik, ha a mostani gép nem azonosítható,
    vagy ha a napló fejléce 'ismeretlen' (nem bizonyítható, hogy másik gépé)."""
    import shutil
    from datetime import datetime
    base = os.path.join(log_dir, LOG_BASENAME)
    if not key or not os.path.isfile(base) or os.path.getsize(base) == 0:
        return None, '', []
    old_key, old_label = _read_log_header(base)
    if old_key == key or old_key == 'ismeretlen':
        return None, '', []
    stamp = datetime.fromtimestamp(os.path.getmtime(base)).strftime('%Y-%m-%d %H-%M')
    label = old_label if old_key else 'régi napló gépazonosító nélkül (több gép vegyesen)'
    dest = os.path.join(log_dir, OTHER_MACHINES_SUBDIR, _safe_name(f'{stamp} - {label}'))
    errors = []
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError as e:
        return None, label, [f'{dest}: {e}']
    for p in [base] + [f'{base}.{n}' for n in (1, 2, 3)]:
        if os.path.isfile(p):
            try:
                shutil.move(p, os.path.join(dest, os.path.basename(p)))
            except OSError as e:
                errors.append(f'{os.path.basename(p)}: {e}')
                if p == base:
                    # A fő fájl maradt (pl. egy kilépő példány még nyitva tartja): a rotáltakat
                    # sem visszük, különben egy gép naplója két helyre szakadna. Következő indításkor újra.
                    try:
                        os.rmdir(dest)
                    except OSError as e2:
                        errors.append(f'az üres mappa maradt ({dest}): {e2}')
                    return None, label, errors
    return dest, label, errors


def log_paths():
    """A feltöltendő naplófájlok, LEGFRISSEBB ELŐSZÖR. Csak a létezők."""
    base = os.path.join(_app_data_dir(), LOG_BASENAME)
    return [p for p in (base, base + '.1') if os.path.isfile(p)]


def _read_tail(path, max_bytes):
    """A fájl utolsó max_bytes bájtja (a lánc vége mindig a fájl végén van)."""
    size = os.path.getsize(path)
    with open(path, 'rb') as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
            # Az első (fél)sort eldobjuk, hogy ne csonka sorral kezdődjön.
            f.readline()
        return f.read()


def pack_logs(paths, max_bytes=UPLOAD_MAX_BYTES):
    """A naplók egyetlen gzip+base64 csomaggá fűzése.

    Visszatérés: (base64_szöveg, {'files': [...], 'raw': bájt, 'packed': bájt}) vagy
    (None, info) ha nincs mit küldeni."""
    if not paths:
        return None, {'files': [], 'raw': 0, 'packed': 0}
    chunks, used, raw_total = [], [], 0
    for p in paths:
        try:
            data = _read_tail(p, SINGLE_LOG_TAIL_BYTES)
        except Exception as e:
            logging.warning(f"[LOGUP] A napló nem olvasható ({p}): {e}")
            continue
        header = f"\n===== {os.path.basename(p)} ({len(data)} bájt) =====\n".encode('utf-8')
        chunks.append(header + data)
        used.append(os.path.basename(p))
        raw_total += len(data)
        # Minden hozzáadás után ellenőrizzük a TÖMÖRÍTETT méretet: a régebbi naplót csak
        # akkor visszük, ha belefér. Az aktuális (első) mindig megy.
        blob = _gzip_b64(b''.join(chunks))
        if len(blob) > max_bytes and len(chunks) > 1:
            chunks.pop()
            used.pop()
            logging.info(f"[LOGUP] A régebbi napló ({p}) kimarad - a csomag túllépné a "
                         f"{max_bytes / 1048576:.0f} MB korlátot.")
            break
    if not chunks:
        return None, {'files': [], 'raw': 0, 'packed': 0}
    blob = _gzip_b64(b''.join(chunks))
    return blob, {'files': used, 'raw': raw_total, 'packed': len(blob)}


def _gzip_b64(data):
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode='wb', compresslevel=9, mtime=0) as gz:
        gz.write(data)
    return base64.b64encode(buf.getvalue()).decode('ascii')


# A WMI gyártónév cégjogi farka ('Dell Inc.', 'ASUSTeK COMPUTER INC.') - a mappanévben
# csak zaj. Csak a VÉGÉRŐL vágjuk le, tehát egy 'Inc'-cel kezdődő gyártó sértetlen marad.
VENDOR_SUFFIXES = (
    'inc.', 'inc', 'corporation', 'corp.', 'corp', 'co., ltd.', 'co.,ltd.', 'co. ltd.',
    'co ltd', 'ltd.', 'ltd', 'gmbh', 'computer inc.', 'computer inc', 's.a.', 'llc',
)


def _name_covers_vendor(name, vendor):
    """Igaz, ha a típusnév már magában hordozza a gyártót, tehát az külön kiírva ismétlés
    lenne. Nem elég a sima 'startswith': a WMI gyártóneve gyakran hosszabb alak, mint ami a
    típusnévben szerepel ('ASUSTeK' vs. 'ASUS Vivobook...'), ezért az első 4 karaktert is
    összevetjük - az 'HP'/'Dell'/'LENOVO' eseteket a startswith úgyis lefedi."""
    n, v = (name or '').strip().lower(), (vendor or '').strip().lower()
    if not n or not v:
        return False
    return n.startswith(v) or (len(v) >= 4 and n[:4] == v[:4])


def _trim_vendor_suffix(vendor):
    v = (vendor or '').strip().rstrip(',')
    for suf in sorted(VENDOR_SUFFIXES, key=len, reverse=True):
        if v.lower().endswith(' ' + suf):
            v = v[:-(len(suf) + 1)].strip().rstrip(',')
            break
    return v


def machine_label(run_fn):
    """A gép EMBERI azonosítója a Drive-mappa nevéhez (pl. 'ASRock B450M Pro4').

    MIÉRT NEM ELÉG A WINDOWS GÉPNÉV: a `DESKTOP-8H3K2` semmit nem mond arról, milyen gép
    volt - a szerviz viszont pont ezt keresi vissza ("melyik volt az a B450M-es?"). Két
    forrásból dolgozunk, ebben a sorrendben:
      1. `Win32_ComputerSystem`/`ComputerSystemProduct` - gyári gépeknél ez a jó (Lenovo,
         Dell, HP). Lenovónál a `Model` csak MTM kód ('20L9S0KM00'), a beszédes nevet a
         ProductName adja ('ThinkPad T580'), ezért abból indulunk.
      2. `Win32_BaseBoard` - ÖSSZERAKOTT gépnél az 1. csupa placeholder ('To Be Filled By
         O.E.M.'), és pont az alaplap típusa az, ami azonosít. Ez a bolti bevétel nagy része.
    Sosem dob kivételt: ha semmi nem jön össze, üres stringet ad, és a hívó a gépnevet
    használja."""
    try:
        from app import machine_core
        m = machine_core.detect_machine(run_fn) or {}
        ph = machine_core.is_placeholder
        parts = []
        vendor = (m.get('manufacturer') or '').strip()
        # A beszédes név elsőbbséget élvez a nyers Model kóddal szemben.
        name = (m.get('product_name') or '').strip()
        if ph(name):
            name = (m.get('model') or '').strip()
        vendor = _trim_vendor_suffix(vendor)
        # A gyártót CSAK akkor tesszük ki, ha a típusnév nem tartalmazza már ('HP' +
        # 'HP EliteDesk 800 G2 SFF' -> 'HP HP EliteDesk...' lett volna).
        if not ph(vendor) and not _name_covers_vendor(name, vendor):
            parts.append(vendor)
        if not ph(name) and name.lower() not in (p.lower() for p in parts):
            parts.append(name)
        if not parts:
            # Összerakott gép: az alaplap AZ azonosító.
            bv = _trim_vendor_suffix((m.get('board_vendor') or '').strip())
            bp = (m.get('board_product') or '').strip()
            if not ph(bv) and not _name_covers_vendor(bp, bv):
                parts.append(bv)
            if not ph(bp):
                parts.append(bp)
        label = ' '.join(parts).strip()
        logging.info(f"[LOGUP] Gép-azonosító a napló mappájához: '{label or '(nincs)'}'.")
        return label
    except Exception as e:
        logging.warning(f"[LOGUP] A gép típusát nem sikerült megállapítani (nem kritikus): {e}")
        return ''


def folder_base(machine_name='', label=''):
    """A futás-mappa nevének GÉP-fele: "<gépnév> - <géptípus>".

    A DÁTUMOT ÉS A BUILD SZÁMOT NEM ITT FŰZZÜK HOZZÁ, hanem az Apps Script (lásd
    `folder_sort_name` és az appscript.txt `getRunFolder_`-je): egy óra, egy időzóna,
    és a `build` mezőt a payload amúgy is viszi - így a MÁR KIADOTT exe-k naplói is
    azonnal a jó nevű mappába kerülnek, frissítés nélkül."""
    name = (machine_name or platform.node() or 'ismeretlen').strip()
    label = (label or '').strip()
    return f"{name} - {label}" if label and label.lower() != name.lower() else name


# ===========================================================================
# A FUTÁS-MAPPA NEVE: DÁTUM ELÖL, UTÁNA A BUILD (2026-09-07, explicit user decision)
# ===========================================================================
# MIÉRT: a mappák a Windows Intézőben és a Drive-on is NÉV szerint rendeződnek, a
# letöltés viszont MINDEGYIKNEK ugyanazt a módosítás-dátumot adja (a letöltés napját),
# tehát dátum szerint rendezni sem lehet őket. A régi név a dátumot a VÉGÉRE tette
# ("16065 - HP EliteDesk 800 G2 SFF - 2026-09-03 14-54"), így az ABC-rend a gépnév
# szerint rendezett, és egy futás időrendi helyét sehogy nem lehetett megállapítani.
#
# A `YYYY-MM-DD HH-MM` alak elöl EGYBEN oldja meg: minden mezője fix szélességű és
# balról nullázott, ezért a LEXIKOGRAFIKUS rend AZONOS az időrenddel - a sima név
# szerinti rendezés időrendbe teszi őket (a felhasználó a valódi mappákon lemérte).
#
# A MEZŐK SORRENDJE NEM ÖNKÉNYES: elöl a FIX szélességűek (dátum, build), hátul a
# változó hosszúak (gépnév, típus). Így a build szám oszlopként, egymás alatt olvasható,
# és nem az vágódik le, ha egy hosszú típusnév nem fér ki a listanézetben:
#
#     2026-09-03 14-54 - build294 - 16065 - HP EliteDesk 800 G2 SFF
#     2026-09-03 16-50 - build295 - HP-Pavilion-X360 - HP Pavilion x360 Convertible...
#
# AMIT EZÉRT FELADTUNK (a régi indoklás, hogy ne "javítsa vissza" senki): a gépnév-elöl
# alaknál EGY gép összes futása egymás alatt sorakozott. Ez a felhasználó kifejezett
# döntésével esett el - egy gép futásait a Drive/Intéző keresőmezőjével is meg lehet
# találni, az időrendet viszont semmi nem pótolta.

# "2026-09-03 14-54" (a másodperc opcionális - egy jövőbeli pontosabb bélyeg is illeszkedjen).
_FOLDER_TS = r'\d{4}-\d{2}-\d{2} \d{2}-\d{2}(?:-\d{2})?'
# A RÉGI alak: az időbélyeg a név VÉGÉN áll, ' - ' elválasztóval.
_FOLDER_TS_TAIL_RE = re.compile(r'^(?P<base>.+?)\s+-\s+(?P<ts>' + _FOLDER_TS + r')$')
# Az ÚJ alak: a névvel KEZDŐDIK.
_FOLDER_TS_HEAD_RE = re.compile(r'^(?P<ts>' + _FOLDER_TS + r')(?=\s|$)')
# A build-jelölés bárhol a névben ("build294", "Build 294").
_FOLDER_BUILD_RE = re.compile(r'\bbuild\s*(?P<num>\d+)\b', re.IGNORECASE)
# A napló FÁJLJÁNAK neve hordozza a buildet: "2026-09-03_14-54-12_16065_build294.log".
_FILE_BUILD_RE = re.compile(r'[_\-]build\s*(\d+)', re.IGNORECASE)


def folder_sort_name(folder, build=''):
    """Egy futás-mappa nevének átrendezése a "dátum - build - gép" alakra.

    IDEMPOTENS - a saját kimenetére alkalmazva UGYANAZT adja. Ez nem elegancia, hanem
    követelmény: ugyanezt a szabályt az Apps Script is alkalmazza (ott jön létre a
    mappa), a letöltés pedig itt is átfut rajta, hogy a RÉGI néven fent lévő mappák is
    jó néven kerüljenek a gépre. Ha nem lenne idempotens, a kétszer átfutó név
    összetörne - és pontosan ez a hiba ölte meg egyszer már a tartós no-bind tárat
    (lásd CLAUDE.md, `_device_stem`).

    SOSEM TALÁL KI DÁTUMOT: ha a névben nincs időbélyeg (pl. a Drive-gyökérben maradt
    régi, almappa nélküli napló), a nevet változatlanul adja vissza. Egy kitalált dátum
    rosszabb, mint a hiánya.

    Tiszta függvény, offline tesztelhető."""
    name = (folder or '').strip()
    if not name:
        return name
    m = _FOLDER_TS_HEAD_RE.match(name)
    if m:
        # Már átrendezett név - de a buildet még hiányolhatja (pl. egy korábbi migráció
        # után), ezért végigmegy a normál összerakón. Az eredmény változatlan, ha minden
        # a helyén van: ez adja az idempotenciát.
        ts, rest = m.group('ts'), name[m.end():]
    else:
        m = _FOLDER_TS_TAIL_RE.match(name)
        if not m:
            return name
        ts, rest = m.group('ts'), m.group('base')
    # A build-jelölést KIVESSZÜK, bárhol állt, és egységesen a dátum mögé tesszük -
    # így egy régi, "... - build294" végű név sem duplázódik.
    #
    # A NÉVBEN LÉVŐ BUILD AZ ERŐSEBB, a paraméterből jövő csak kitölteni tud. Fordítva
    # (`build or b.group(...)`) a függvény ELRONTANÁ a nevet: a hívó a mappában talált
    # naplófájl nevéből olvassa ki a buildet, és ha oda valamiért egy másik futás naplója
    # is bekerül, az felülírná a mappa saját, hiteles build számát. Ez egyben az
    # idempotencia feltétele is: `f(f(x, a), b) == f(x, a)` bármilyen `b`-re.
    b = _FOLDER_BUILD_RE.search(rest)
    if b:
        build = b.group('num')
        rest = rest[:b.start()] + rest[b.end():]
    rest = _tidy_separators(rest)
    parts = [ts]
    num = str(build or '').strip()
    if num:
        parts.append('build' + num)
    if rest:
        parts.append(rest)
    return ' - '.join(parts)


def _tidy_separators(s):
    """A kivágások után maradt ' - - ' / széli ' - ' elválasztók eltakarítása.

    Szándékosan CSAK a szeparátor-alakú kötőjeleket (szóközzel körülvett, vagy a név
    szélén álló) bántja: a `B450M-HDV R4.0` és a `HP Pavilion x360 14-dh1xxx`
    típusnevekben a kötőjel a névhez tartozik, azt nem szabad megfogni."""
    s = re.sub(r'\s+-\s+-\s+', ' - ', s or '')
    s = re.sub(r'^\s*-\s*', '', s)
    s = re.sub(r'\s*-\s*$', '', s)
    return s.strip()


def build_payload(blob, info, machine_name='', build='', outcome='', extra=None, folder=''):
    """A Drive-ra küldött JSON. `action="log"` - a benchmark-sorokat ugyanaz a végpont
    kezeli, a szkript ebből tudja megkülönböztetni a kettőt."""
    payload = {
        'action': 'log',
        'machine': machine_name or platform.node() or 'ismeretlen',
        'build': str(build or ''),
        'outcome': outcome or '',
        'files': info.get('files') or [],
        'raw_bytes': info.get('raw') or 0,
        # A gépenkénti almappa neve. Üresen hagyva a szkript a napló-gyökérbe teszi a
        # fájlt (így egy régebbi kliens sem törik el).
        'folder': folder or '',
        'gz': blob,
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def friendly_upload_error(msg):
    """Az Apps Script nyers hibaszövegéből érthető, TEENDŐT tartalmazó mondat.

    MIÉRT: az Apps Script kivételei a saját belső API-járól beszélnek, és a képernyőre
    így kerültek ki (terepen mérve, 2026-09-01, egy egyébként hibátlan lánc legvégén):

        A napló feltöltése nem sikerült (Exception: Nincs engedélye a(z)
        DriveApp.getFoldersByName függvény meghívására. Szükséges engedélyek:
        (https://www.googleapis.com/auth/drive.readonly || .../auth/drive).
        További információ: https://developers.google.com/apps-script/...)

    Ebből a technikus nem tudja kitalálni, hogy a teendő NEM a gépen van, hanem a
    Drive-on: egyszer le kell futtatni a szkript `engedelyezes()` függvényét, majd új
    verzióként telepíteni. Az eredeti szöveg a NAPLÓBAN változatlanul megmarad (ott
    az a pontos nyom), a képernyőre viszont a teendő megy ki.

    Tiszta függvény, offline tesztelhető."""
    low = (msg or '').lower()
    if 'driveapp' in low or 'auth/drive' in low or 'engedély' in low or 'authorization' in low:
        return ('a Drive-engedély hiányzik a szerviz Apps Script-jéből - EGYSZER kell '
                'megcsinálni: a táblázat > Bővítmények > Apps Script, futtasd az '
                '"engedelyezes" függvényt, fogadd el az engedélyt, majd Telepítés > '
                'Telepítések kezelése > ceruza > Verzió: "Új verzió"')
    if 'jelsz' in low or 'password' in low:
        return 'hibás jelszó (a szkript LOG_READ_PASSWORD értéke)'
    return msg


def upload_logs(run, url, machine_name='', build='', outcome='', http=None):
    """A naplók feltöltése. Visszatérés: (siker, üzenet).

    SOSEM dob kivételt: ez a lánc legvégén fut, és egy feltöltési hiba nem ronthatja el
    egy egyébként sikeres fix lezárását."""
    try:
        if not url:
            return False, 'nincs beállítva feltöltési végpont'
        paths = log_paths()
        if not paths:
            return False, 'nincs naplófájl'
        blob, info = pack_logs(paths)
        if not blob:
            return False, 'a naplót nem sikerült becsomagolni'
        ratio = (info['raw'] / info['packed']) if info['packed'] else 0
        logging.info(f"[LOGUP] Feltöltés indul: {info['files']} - {info['raw'] / 1048576:.1f} MB "
                     f"-> {info['packed'] / 1024:.0f} KB ({ratio:.1f}:1 tömörítés)")
        folder = folder_base(machine_name, machine_label(run))
        body = build_payload(blob, info, machine_name, build, outcome, folder=folder)
        sender = http or _default_http
        txt = sender(run, url, 'POST', body)
        # NEM-JSON / ÜRES VÁLASZ = HIBA (2026-10-12): az Apps Script hibánál HTML-hibaoldalt
        # ad (lásd _post) - a régi kód ezt `{}`-nek vette, és "Feltöltés kész"-t mondott.
        try:
            resp = json.loads(txt) if txt and txt.strip() else None
        except ValueError:
            resp = None
        if not isinstance(resp, dict):
            snippet = (txt or '')[:200].replace('\n', ' ')
            logging.warning(f"[LOGUP] A szerver válasza nem értelmezhető (nem JSON): {snippet!r}")
            return False, 'a szerver válasza nem értelmezhető' + (f' ({snippet[:80]})' if snippet else ' (üres válasz)')
        if resp.get('ok') is False:
            msg = resp.get('error') or 'a szerver hibát jelzett'
            logging.warning(f"[LOGUP] A szerver elutasította a feltöltést: {msg}")
            return False, friendly_upload_error(msg)
        where = (resp or {}).get('url') or (resp or {}).get('file') or ''
        logging.info(f"[LOGUP] Feltöltés kész{(' -> ' + where) if where else ''}.")
        return True, where
    except Exception as e:
        # Szándékosan elnyelt: hálózati hiba, lejárt szkript, bármi - a fix ettől még kész.
        logging.warning(f"[LOGUP] A napló feltöltése nem sikerült (nem kritikus): {e}")
        return False, str(e)


def _default_http(run, url, method, body):
    """A benchmark HTTP-rétege (friss-Windows tanúsítvány-fallbackkel). Helyben
    importálva, hogy ez a modul önmagában is tesztelhető maradjon."""
    from app.benchmark_core import _http_request
    return _http_request(run, url, method, body=body, timeout=120)


# ---------------------------------------------------------------------------
# LETÖLTÉS: a Drive-ra feltöltött naplók visszahozása erre a gépre.
#
# MIÉRT KELL JELSZÓ: a végpont címe MINDEN kiadott exe-ben benne van és kiolvasható
# belőle. Feltölteni jelszó nélkül lehet (a lánc ember nélkül fut, nincs ki begépelje),
# letölteni viszont csak jelszóval - a naplókban ügyfélgépek adatai vannak. A jelszót
# SOSEM ágyazzuk az exe-be: azt a technikus gépeli be, különben pont annyit érne, mint
# a jelszó nélküli végpont.
# ---------------------------------------------------------------------------

# A letöltött naplók helye. SZÁNDÉKOSAN NEM az `_app_data_dir()` gyökere: ott van az
# ÉLŐ napló, amit a feltöltő beolvas - idegen gépek naplói közé keveredve előbb-utóbb
# rossz gép naplója menne fel.
DOWNLOAD_SUBDIR = 'letoltott_naplok'


def download_dir():
    return os.path.join(_app_data_dir(), DOWNLOAD_SUBDIR)


def _post(run, url, payload, http=None):
    """Egy JSON-kérés a végpontra. Visszatérés: (dict, hibaszöveg-vagy-üres)."""
    sender = http or _default_http
    txt = sender(run, url, 'POST', json.dumps(payload, ensure_ascii=False))
    try:
        resp = json.loads(txt) if txt and txt.strip() else {}
    except Exception:
        # Az Apps Script hiba esetén HTML hibaoldalt ad vissza - az eleje a beszédes.
        snippet = (txt or '')[:200].replace('\n', ' ')
        return {}, f"a szerver válasza nem JSON ({snippet})"
    if not isinstance(resp, dict):
        return {}, 'váratlan szerver-válasz'
    if resp.get('ok') is False:
        # Ugyanaz a fordítás, mint a feltöltésnél: a letöltés/listázás UGYANAZOKBA a
        # Drive-engedélyekbe fut bele (listDriverLogs_ -> getLogFolder_), tehát a
        # technikus itt is a nyers Apps Script-kivételt kapná teendő nélkül.
        return resp, friendly_upload_error(resp.get('error') or 'a szerver hibát jelzett')
    return resp, ''


# IDŐSZAK-SZŰRŐ A LETÖLTÉSHEZ (2026-10-12, explicit user decision: *"ki tudjam választani h
# csak az e heti naplókat szedjem le ... 7 nap, 30 nap, 1 év, csak a mai, vagy az összes"*).
# kulcs -> (felirat, napok száma a mai nappal együtt; None = mind)
LOG_PERIODS = {
    'today': ('Ma', 1),
    '7': ('Elmúlt 7 nap', 7),
    '30': ('Elmúlt 30 nap', 30),
    '365': ('Elmúlt 1 év', 365),
    'all': ('Összes', None),
}
_LEAD_DATE_RE = re.compile(r'^\s*(\d{4})-(\d{2})-(\d{2})')


def row_run_date(row):
    """Egy távoli napló FUTÁSÁNAK dátuma (datetime.date), vagy None, ha nem állapítható meg.

    Sorrend: a futás-mappa neve (a `folder_sort_name` dátummal kezdi: "2026-10-12 15-02 -
    build373 - ..."), a fájlnév ("2026-09-03_14-54-12_..._build294.log"), végül a szerver
    `date` mezője, ha ISO-alakkal kezdődik. Csak a név ELEJÉN álló dátumot fogadjuk el -
    egy név közepén álló számsor nem dátum. Kitalált dátum nincs (None)."""
    import datetime as _dt
    for src in (folder_sort_name(row.get('folder') or ''), row.get('name') or '', str(row.get('date') or '')):
        m = _LEAD_DATE_RE.match(src or '')
        if m:
            try:
                return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue
    return None


def filter_rows_by_period(rows, period, today=None):
    """A sorok szűrése időszakra. Visszatérés: (megtartott_sorok, dátum_nélküliek_száma).

    `period`: a LOG_PERIODS kulcsa; ismeretlen kulcs = 'all'. A 'N nap' a mai napot is
    beleszámolja (7 nap = ma + az előző 6). Egy dátum nélküli sor csak az 'Összes'-be
    kerül: ha nem tudjuk, mikor készült, nem állíthatjuk, hogy a kért időszakba esik."""
    import datetime as _dt
    days = (LOG_PERIODS.get(period) or LOG_PERIODS['all'])[1]
    if days is None:
        return list(rows or []), 0
    today = today or _dt.date.today()
    cutoff = today - _dt.timedelta(days=days - 1)
    kept, no_date = [], 0
    for r in rows or []:
        d = row_run_date(r)
        if d is None:
            no_date += 1
        elif d >= cutoff:
            kept.append(r)
    return kept, no_date


def list_remote_logs(run, url, password, http=None):
    """A Drive-on lévő naplók listája. Visszatérés: (sorok, hibaszöveg).

    Egy sor: {'id','name','folder','size','date','desc'}."""
    if not url:
        return [], 'nincs beállítva végpont'
    if not password:
        return [], 'nincs megadva jelszó'
    resp, err = _post(run, url, {'action': 'loglist', 'pw': password}, http)
    if err:
        logging.warning(f"[LOGDL] A napló-lista lekérése nem sikerült: {err}")
        return [], err
    rows = resp.get('logs') or []
    logging.info(f"[LOGDL] A Drive-on {resp.get('total', len(rows))} napló van, "
                 f"{len(rows)} sor érkezett.")
    return rows, ''


def local_path_for(row, dest_dir=None):
    """Egy távoli napló helye ezen a gépen: <cél>\\<futás mappája>\\<fájlnév>.log

    KICSOMAGOLVA mentjük: a `.gz`-t az elemzéshez úgyis ki kell bontani, és egy olyan
    fájl, amit előbb kézzel ki kell tömöríteni, a gyakorlatban nem lesz elolvasva.

    A MAPPANÉV ITT IS ÁTFUT A `folder_sort_name`-en, nem csak a Drive-on: a már fent
    lévő RÉGI mappák neve nem változik meg attól, hogy a szkriptet frissítjük, és a
    technikus letöltés után épp az időrendet nem kapná meg. Mivel a függvény idempotens,
    egy Drive-on már átnevezett mappa ugyanezt a nevet adja - tehát a `skip_existing`
    sem tölt le semmit kétszer."""
    base = dest_dir or download_dir()
    name = _safe_name(row.get('name') or 'naplo.log.gz')
    # A build a FÁJL nevében mindig ott van, a mappáéban egy régi feltöltésnél még nem.
    fb = _FILE_BUILD_RE.search(name)
    folder = _safe_name(folder_sort_name(row.get('folder') or '', fb.group(1) if fb else ''))
    if name.endswith('.gz'):
        name = name[:-3]
    if not name.endswith('.log'):
        name += '.log'
    return os.path.join(base, folder, name) if folder else os.path.join(base, name)


def _safe_name(name):
    """Windows-on tiltott karakterek cseréje (a mappanév a Drive-ról jön)."""
    out = ''.join('-' if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in str(name or ''))
    return out.strip().rstrip('.')[:120]


def migrate_download_dir(dest_dir=None):
    """A MÁR LETÖLTÖTT futás-mappák átnevezése a "dátum - build - gép" alakra.

    MIÉRT KELL EGYÁLTALÁN: a névforma 2026-09-07-én változott, és a technikus gépén
    addigra ott áll egy csomó régi nevű mappa. Azok kézi átnevezése pont az a munka,
    ami sosem történik meg - a lista pedig félig időrendben, félig ABC-ben állna, ami
    rosszabb, mint bármelyik tisztán.

    A LETÖLTÉS ELŐTT FUT, nem valami külön gombról: így a `skip_existing` a már
    átnevezett mappákat látja, és egy meglévő naplót nem tölt le újra.

    Visszatérés: (átnevezve, változatlan, hibák). SOSEM dob: egy sikertelen átnevezés
    kevesebb baj, mint egy emiatt elmaradt letöltés."""
    base = dest_dir or download_dir()
    renamed, kept, errors = 0, 0, []
    try:
        if not os.path.isdir(base):
            return 0, 0, []
        for entry in sorted(os.listdir(base)):
            src = os.path.join(base, entry)
            if not os.path.isdir(src):
                continue
            new = _safe_name(folder_sort_name(entry, _build_from_log_files(src)))
            if not new or new == entry:
                kept += 1
                continue
            dst = os.path.join(base, new)
            if os.path.exists(dst):
                # Nem írunk felül és nem olvasztunk össze: egy azonos nevű mappa itt
                # csak úgy állhat elő, ha valami félrement - azt a technikus nézze meg.
                errors.append(f"{entry}: a cél már létezik ({new})")
                logging.warning(f"[LOGDL] Átnevezés kihagyva, a cél már létezik: {new}")
                continue
            try:
                os.rename(src, dst)
                renamed += 1
                logging.info(f"[LOGDL] Mappa átnevezve: '{entry}' -> '{new}'")
            except OSError as e:
                errors.append(f"{entry}: {e}")
                logging.warning(f"[LOGDL] A mappa átnevezése nem sikerült ('{entry}'): {e}")
    except Exception as e:
        logging.warning(f"[LOGDL] A letöltött naplók átnevezése hibára futott (nem kritikus): {e}")
        errors.append(str(e))
    if renamed:
        logging.info(f"[LOGDL] {renamed} futás-mappa átnevezve az új (dátum elöl) alakra.")
    return renamed, kept, errors


def _build_from_log_files(dirpath):
    """A build szám a mappában lévő naplófájl nevéből ("..._16065_build294.log").

    A RÉGI mappanevekben nincs build - a fájlnévben viszont MINDIG ott volt, tehát a
    régi letöltések is megkapják, nem csak az ezután érkezők."""
    try:
        for fn in sorted(os.listdir(dirpath)):
            m = _FILE_BUILD_RE.search(fn)
            if m:
                return m.group(1)
    except OSError as e:
        logging.debug(f"[LOGDL] A mappa nem olvasható a build számhoz ({dirpath}): {e}")
    return ''


def download_logs(run, url, password, rows, dest_dir=None, http=None,
                  progress=None, check_cancel=None, skip_existing=True):
    """A megadott naplók letöltése és kicsomagolása. Visszatérés: (letöltve, kihagyva, hibák).

    `progress(index, total, name)` - a hívó ezzel rajzol sávot; `check_cancel()` igazra
    megszakít. Egyetlen fájl hibája sosem állítja meg a többit: egy elérhetetlen napló
    kevesebb baj, mint egy félbehagyott letöltés."""
    dest = dest_dir or download_dir()
    done, skipped, errors = 0, 0, []
    total = len(rows)
    for i, row in enumerate(rows, 1):
        if check_cancel and check_cancel():
            logging.info("[LOGDL] A letöltést a felhasználó megszakította.")
            break
        path = local_path_for(row, dest)
        if skip_existing and os.path.isfile(path) and os.path.getsize(path) > 0:
            skipped += 1
            continue
        if progress:
            progress(i, total, row.get('folder') or row.get('name') or '')
        try:
            resp, err = _post(run, url, {'action': 'logget', 'pw': password,
                                         'id': row.get('id')}, http)
            if err:
                raise RuntimeError(err)
            blob = resp.get('gz')
            if not blob:
                raise RuntimeError('a szerver nem küldött tartalmat')
            data = gzip.decompress(base64.b64decode(blob))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as f:
                f.write(data)
            done += 1
            logging.info(f"[LOGDL] Letöltve: {path} ({len(data) / 1024:.0f} KB kicsomagolva)")
        except Exception as e:
            errors.append(f"{row.get('folder') or row.get('name')}: {e}")
            logging.warning(f"[LOGDL] Nem sikerült letölteni ({row.get('name')}): {e}")
    logging.info(f"[LOGDL] Kész: {done} letöltve, {skipped} már megvolt, {len(errors)} hiba.")
    return done, skipped, errors


# ===========================================================================
# A DEBUG-NAPLÓK TÖRLÉSE (2026-08-31, explicit user decision: "clear log" gomb)
# ===========================================================================
# MIÉRT KELL: a napló 5 MB x 3 fájl, és a technikus gyakran EGY konkrét gép futását
# akarja tisztán látni. Kézzel törölni azért nem lehet, mert a fájl FUT KÖZBEN NYITVA
# van - az Explorerből "A fájlt egy másik program használja" hibát ad.
#
# EZÉRT NEM ELÉG AZ os.remove(): a Windows nem engedi törölni a rotáló handler által
# nyitva tartott fájlt. A helyes sorrend: a handlert LEZÁRJUK (elengedi a fájlt) ->
# törlünk -> a handlert ÚJRANYITJUK. Az újranyitás a handler saját `_open()`-jén megy,
# tehát a `_BomRotatingFileHandler` UTF-8 BOM-ja is a helyére kerül (enélkül a friss
# napló a Jegyzettömbben ékezet-szemétként jelenne meg - lásd driver_tool.py).
#
# ÉS A TÖRLÉS TÉNYE BEKERÜL AZ ÚJ NAPLÓBA (Rule 0): egy üres napló önmagában nem árulja
# el, hogy törölték-e vagy sosem futott a program - és pont ez a különbség dönti el egy
# hibajelentésnél, hogy hiányzó bizonyítékot vagy hiányzó futást keresünk.

def clear_logs():
    """A debug-naplók (`DriverVarázsló_debug.log` + rotált másai) törlése.

    Visszatérés: (törölt_fájlok_száma, felszabadított_bájt, hibák_listája).

    A letöltött IDEGEN naplókat (`letoltott_naplok`) SZÁNDÉKOSAN nem bántja: azok más
    gépek bizonyítékai, amiket a technikus külön kért le - egy "napló törlése" gombtól
    senki nem várja, hogy azokat is elviszi."""
    import glob as _glob
    base = os.path.join(_app_data_dir(), LOG_BASENAME)
    # A rotált fájlok `.1`, `.2` végűek (backupCount=2), de gyűjtsük mintával, hogy egy
    # későbbi backupCount-változás se hagyjon itt fájlt.
    # A félrerakott, korábbi gépek naplói is (2026-10-21 előtt ugyanebben a fájlban voltak,
    # tehát a gomb eddig is törölte őket).
    other = os.path.join(_app_data_dir(), OTHER_MACHINES_SUBDIR)
    targets = sorted(set([base] + _glob.glob(base + '.*')
                         + _glob.glob(os.path.join(_glob.escape(other), '*', LOG_BASENAME + '*'))))
    targets = [p for p in targets if os.path.isfile(p)]
    if not targets:
        logging.info("[LOGCLEAR] Nincs törölhető naplófájl.")
        return 0, 0, []

    sizes = {}
    for p in targets:
        try:
            sizes[p] = os.path.getsize(p)
        except OSError:
            sizes[p] = 0
    # Még a régi naplóba: ha valaki később ezt a fájlt menti ki, lássa a szándékot.
    logging.warning(f"[LOGCLEAR] NAPLÓ-TÖRLÉS kérve: {[os.path.basename(p) for p in targets]} "
                    f"({sum(sizes.values()) / 1048576:.1f} MB).")

    # A fájlt nyitva tartó handlerek lezárása. Csak azok, amik TÉNYLEG ezekre a fájlokra
    # mutatnak - egy jövőbeli másik fájl-handlert nem szabad mellékesen elkaszálni.
    handlers = []
    try:
        for h in list(logging.getLogger().handlers):
            fn = getattr(h, 'baseFilename', None)
            if fn and os.path.normcase(fn) == os.path.normcase(base):
                handlers.append(h)
    except Exception as e:
        logging.warning(f"[LOGCLEAR] A naplóző handlerek felderítése nem sikerült: {e}")

    for h in handlers:
        try:
            h.acquire()
            h.close()          # elengedi a fájlt: enélkül a törlés WinError 32-vel bukna
        except Exception as e:
            logging.debug(f"[LOGCLEAR] A handler lezárása nem sikerült: {e}")

    removed, freed, errors = 0, 0, []
    try:
        for p in targets:
            try:
                os.remove(p)
                removed += 1
                freed += sizes.get(p, 0)
            except Exception as e:
                errors.append(f"{os.path.basename(p)}: {e}")
    finally:
        # ÚJRANYITÁS MINDENKÉPPEN, akkor is, ha a törlés elhasalt - egy naplózás nélkül
        # továbbfutó program a lehető legrosszabb kimenet ebben a projektben.
        for h in handlers:
            try:
                h.stream = h._open()
            except Exception as e:
                errors.append(f"a naplózás újraindítása: {e}")
            finally:
                try:
                    h.release()
                except Exception:
                    pass

    # A kiürült gép-mappák (amiben más is van, az marad - azt nem mi tettük oda).
    for d in _glob.glob(os.path.join(_glob.escape(other), '*')) + [other]:
        if os.path.isdir(d) and not os.listdir(d):
            try:
                os.rmdir(d)
            except OSError as e:
                errors.append(f"{os.path.basename(d)}: {e}")

    # Az ÚJ naplóba (ezért van a handler-újranyitás után): mi tűnt el és mikor.
    logging.warning(f"[LOGCLEAR] Napló törölve: {removed} fájl, {freed / 1048576:.1f} MB "
                    f"felszabadítva. Hibák: {errors or 'nincs'}")
    return removed, freed, errors
