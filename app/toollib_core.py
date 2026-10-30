"""Szervízprogramok könyvtára - KÖZÖS mag: melyik program melyik csomagban van, a csomag
honnan jön (GitHub-kiadás vagy a bolt Google Drive-ja), és a letöltött csomagok helye.

MIÉRT CSOMAGONKÉNT (2026-10-30, explicit user decision): a régi `stresstools.zip` 621 MB
volt (kicsomagolva 1,3 GB), és egyetlen program kedvéért is az egészet le kellett tölteni.
Most minden programnak saját zipje van, és minden funkció (stressz-teszt, RAM TEST,
benchmark, riport, bolti nyomtatás, a könyvtár) csak azt tölti le, ami neki kell.

KÉT FORRÁS, A LICENC SZERINT (explicit user decision, 2026-10-30): a repó NYILVÁNOS, és a
Sergei-csomagokból jött fizetős programokban licenckulcs van (AIDA64 `pkey.txt`, HD
Sentinel `reg.dat`, TreeSize Pro licenckezelő...). Ezek NEM kerülhetnek nyilvános GitHub-
kiadásba: a bolt Google Drive-járól jönnek, a naplók letöltésével azonos jelszóval (a
jelszó SOSEM kerül az exébe). Az ingyenesek a GitHub `programok` kiadásából jönnek. Egy
privát repó sem megoldás: a letöltéshez kellő token az exéből kiolvasható lenne.

A régi `stresstools.zip` kiadás SZÁNDÉKOSAN megmarad a GitHubon: a régebbi buildek
(amíg nem frissülnek) azt töltik le.

A DRIVE-ÚT: az Apps Script `toolfile` akciója jelszóval megkeresi a fájlt a Drive
`DRIVE_FOLDER` mappájában, "link birtokában bárki" olvashatóra állítja, és a fájl
azonosítóját adja vissza; a letöltés a `drive.usercontent.google.com` címről megy (a
`confirm=t` a nagy fájlok vírusellenőrzési közbeiktatott oldalát ugorja át). A szkript
kódja a CLAUDE.md 17. fejezetében hivatkozott terep-fájlban van."""

# === AUTO-IMPORTS ===
import os
import logging
import tempfile
# === /AUTO-IMPORTS ===


SOURCE_GITHUB, SOURCE_DRIVE = 'github', 'drive'

GITHUB_RELEASE_TAG = 'programok'
GITHUB_BASE_URL = ('https://github.com/egonixaimgod/DriverVarazslo/releases/download/'
                   + GITHUB_RELEASE_TAG + '/')
# A Drive-mappa neve, amiben az Apps Script a licences csomagokat keresi.
DRIVE_FOLDER = 'DriverVarazslo_programok'
DRIVE_DOWNLOAD_URL = 'https://drive.usercontent.google.com/download?id={id}&export=download&confirm=t'

# csomag -> (forrás, mappanév a gyökér alatt). A zip TARTALMA közvetlenül a program fájljai
# (nincs benne gyökérmappa), a mappát mi adjuk: így a telepítés is ezt a nevet kapja a
# Program Files alatt. Ha egy csomag tartalma változik, a zip NEVÉT kell változtatni (pl.
# `furmark-2`), mert a letöltött példányt a jelző-fájlban lévő név alapján tartjuk frissnek.
PACKAGES = {
    # A régi stresstools.zip-ből (ingyenesek):
    'furmark': (SOURCE_GITHUB, 'FurMark'),
    'prime95': (SOURCE_GITHUB, 'Prime95'),
    'linpack': (SOURCE_GITHUB, 'Linpack'),
    'hwinfo': (SOURCE_GITHUB, 'HWiNFO'),
    'testmem5': (SOURCE_GITHUB, 'TestMem5'),
    'ycruncher': (SOURCE_GITHUB, 'y-cruncher'),
    'hcimemtest': (SOURCE_GITHUB, 'HCI MemTest'),
    'cinebench': (SOURCE_GITHUB, 'CinebenchR20'),
    'heaven': (SOURCE_GITHUB, 'Heaven'),
    'smartctl': (SOURCE_GITHUB, 'smartctl'),
    'sumatrapdf': (SOURCE_GITHUB, 'SumatraPDF'),
    'hpdriver': (SOURCE_GITHUB, 'HPDriver'),
    # A Sergei-csomagokból, ingyenesek:
    'cpuz': (SOURCE_GITHUB, 'CPU-Z'),
    'dismpp': (SOURCE_GITHUB, 'Dism++'),
    'ismylcdok': (SOURCE_GITHUB, 'IsMyLcdOK'),
    # A Sergei-csomagokból, licenccel vagy nem bizonyítottan ingyenes kiadásban:
    'aida64': (SOURCE_DRIVE, 'AIDA64'),
    'hdsentinel': (SOURCE_DRIVE, 'HD Sentinel'),
    'treesize': (SOURCE_DRIVE, 'TreeSize'),
    'teracopy': (SOURCE_DRIVE, 'TeraCopy'),
    'totalcmd': (SOURCE_DRIVE, 'Total Commander'),
    'teamviewer': (SOURCE_DRIVE, 'TeamViewer'),
    'wlu': (SOURCE_DRIVE, 'Windows Login Unlocker'),
    'macrium': (SOURCE_DRIVE, 'Macrium Reflect'),
    'partitionwizard': (SOURCE_DRIVE, 'MiniTool Partition Wizard'),
    'occt': (SOURCE_DRIVE, 'OCCT'),
}

# Program-kulcs (STRESS_TOOLS / BENCH_TOOLS / RAMTEST_TOOLS, ill. a belső segédek) -> csomag.
# Ahol a kettő azonos, nem kell ide írni.
KEY_PACKAGE = {
    'tm5': 'testmem5',
}

# A letöltött csomagok gyökere. CSUPA ASCII (a TestMem5 ANSI fájl-API-kkal dolgozik, lásd
# GuiStressMixin._ascii_tool_copy), és MÁS, mint a régi `DriverVarázsló_Stress`: abban a
# régi zip `stresstools\` almappája van, amiből a fájlkeresők régi exéket is találnának.
CACHE_DIRNAME = 'DriverVarazslo_Programok'
LEGACY_CACHE_DIRNAME = 'DriverVarázsló_Stress'
MARKER_NAME = '.dv_csomag_kesz'


def package_for(key):
    """Egy program-kulcs csomagja, vagy None, ha ismeretlen."""
    pkg = KEY_PACKAGE.get(key, key)
    return pkg if pkg in PACKAGES else None


def packages_for(keys):
    """A kulcsokhoz kellő csomagok, sorrendtartóan, ismétlés nélkül. Ismeretlen kulcsnál
    WARNING (egy elgépelt kulcs különben némán semmit nem töltene le)."""
    out = []
    for k in keys or []:
        pkg = package_for(k)
        if not pkg:
            logging.warning(f"[TOOLLIB] Ismeretlen program-kulcs, nincs hozzá csomag: {k!r}")
        elif pkg not in out:
            out.append(pkg)
    return out


def package_source(pkg):
    return PACKAGES[pkg][0]


def zip_name(pkg):
    return f'{pkg}.zip'


def cache_root(temp_dir=None):
    """A letöltött csomagok gyökere. WinPE-ben a %TEMP% az X: RAM-diskre mutat - ott a
    valódi C: meghajtóra tesszük (ugyanaz a szabály, mint a régi stresstools-nál)."""
    if temp_dir is None:
        if os.environ.get('SystemDrive', 'C:') == 'X:':
            temp_dir = r'C:\DV_Temp'
        else:
            temp_dir = tempfile.gettempdir()
    return os.path.join(temp_dir, CACHE_DIRNAME)


def package_dir(root, pkg):
    return os.path.join(root, PACKAGES[pkg][1])


def package_ready(root, pkg):
    """Kész-e a csomag: a jelző-fájl megvan, és ennek a zipnek a nevét tartalmazza (egy
    félbemaradt kicsomagolásnak nincs jelzője)."""
    try:
        with open(os.path.join(package_dir(root, pkg), MARKER_NAME), encoding='utf-8') as f:
            return f.read().strip() == zip_name(pkg)
    except OSError:
        return False


def mark_package_ready(root, pkg):
    with open(os.path.join(package_dir(root, pkg), MARKER_NAME), 'w', encoding='utf-8') as f:
        f.write(zip_name(pkg))


def github_url(pkg):
    return GITHUB_BASE_URL + zip_name(pkg)


def drive_url(file_id):
    return DRIVE_DOWNLOAD_URL.format(id=file_id)


# A JELSZÓ MEGJEGYZÉSE (2026-10-30, explicit user decision): a jelszót csak akkor kérjük,
# amikor egy licences programot tényleg le kell tölteni, és a GÉP jegyezze meg - de NE a
# `C:\DriverVarazslo` mappában (*"ne a c:/drivervarazsloba rakja ... mert az ugy nem jo"*).
# Ezért a Windows Hitelesítőadat-kezelőjébe kerül (Credential Manager, általános
# hitelesítő adat, LOCAL_MACHINE megőrzéssel): a Windows maga titkosítja, a felhasználói
# fiókhoz kötve, és a programmappa törlése/másolása nem viszi. Az exébe SOHA nem kerül.
# Jelszó nélkül nem lehet: a végpont címe a nyilvános exében benne van.
CRED_TARGET = 'DriverVarazslo/Szervizprogramok'
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168


def _cred_api():
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD),
                    ('TargetName', wintypes.LPWSTR), ('Comment', wintypes.LPWSTR),
                    ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
                    ('CredentialBlob', ctypes.POINTER(ctypes.c_char)), ('Persist', wintypes.DWORD),
                    ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p),
                    ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR)]
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    adv.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
    adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    adv.CredFree.argtypes = [ctypes.c_void_p]
    return ctypes, CREDENTIAL, adv


def load_password():
    """A megjegyzett jelszó, vagy '' (nincs elmentve; olvasási hibánál WARNING is)."""
    try:
        ctypes, CREDENTIAL, adv = _cred_api()
        pcred = ctypes.POINTER(CREDENTIAL)()
        if not adv.CredReadW(CRED_TARGET, _CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)):
            err = ctypes.get_last_error()
            if err != _ERROR_NOT_FOUND:
                logging.warning(f"[TOOLLIB] A megjegyzett jelszó nem olvasható (CredReadW hiba {err}) - újra be kell írni.")
            return ''
        try:
            c = pcred.contents
            return ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode('utf-8')
        finally:
            adv.CredFree(pcred)
    except Exception as e:
        logging.warning(f"[TOOLLIB] A megjegyzett jelszó nem olvasható: {e} - újra be kell írni.")
        return ''


def save_password(password):
    try:
        ctypes, CREDENTIAL, adv = _cred_api()
        data = password.encode('utf-8')
        buf = ctypes.create_string_buffer(data, len(data))
        cred = CREDENTIAL()
        cred.Type = _CRED_TYPE_GENERIC
        cred.TargetName = CRED_TARGET
        cred.Comment = 'DriverVarazslo - a Szervizprogramok konyvtaranak Drive-jelszava'
        cred.CredentialBlobSize = len(data)
        cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))
        cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
        cred.UserName = 'DriverVarazslo'
        if not adv.CredWriteW(ctypes.byref(cred), 0):
            raise OSError(f"CredWriteW hiba {ctypes.get_last_error()}")
        logging.info(f"[TOOLLIB] A Drive-jelszó megjegyezve a Windows Hitelesítőadat-kezelőjében ({CRED_TARGET}).")
    except Exception as e:
        logging.warning(f"[TOOLLIB] A jelszó megjegyzése nem sikerült: {e}")


def forget_password():
    """True, ha nincs (már) elmentett jelszó."""
    try:
        ctypes, _CREDENTIAL, adv = _cred_api()
        logging.info(f"[TOOLLIB] A megjegyzett Drive-jelszó törlése ({CRED_TARGET}).")
        if not adv.CredDeleteW(CRED_TARGET, _CRED_TYPE_GENERIC, 0):
            err = ctypes.get_last_error()
            if err != _ERROR_NOT_FOUND:
                logging.warning(f"[TOOLLIB] A megjegyzett jelszó nem törölhető (CredDeleteW hiba {err}).")
                return False
        return True
    except Exception as e:
        logging.warning(f"[TOOLLIB] A megjegyzett jelszó nem törölhető: {e}")
        return False


def drive_file_id(post, password, pkg):
    """A Drive-fájl azonosítója az Apps Scripttől. `post(payload) -> (dict, hiba)` (a
    `logupload_core._post` a végponttal). Visszatérés: (azonosító, hiba-szöveg)."""
    if not password:
        return None, 'a licences programokhoz meg kell adni a Drive-jelszót'
    resp, err = post({'action': 'toolfile', 'pw': password, 'name': zip_name(pkg)})
    if err:
        return None, err
    fid = (resp or {}).get('id')
    if not fid:
        return None, f"a szerver nem adott fájl-azonosítót ({zip_name(pkg)} nincs a Drive '{DRIVE_FOLDER}' mappájában?)"
    return fid, ''
