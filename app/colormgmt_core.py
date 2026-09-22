"""Színkezelés MAGJA: ICC-profilok aktiválása (SDR és HDR), valós idejű gamma, a
profil-betöltés tiltása, az ACM (automatikus színkezelés) tartós kikapcsolása, és a
TELJES gyári visszaállítás - a Kijelző & Színkezelés nézet (és a CLI) közös logikája.

A nézet 2026-09-22-i újraírásakor készült (explicit user decision: *"a kijelző & színkezelés
tabot TELJESEN alakítsd át... professzionális de egyszerű és átlátható"*). A monitor-
felderítés, HDR, EDID és gamma-OLVASÁS továbbra is az app/display_core.py-ban van; ez a
modul arra épít.

MINDEN MECHANIZMUS ÉLŐBEN MÉRVE (2026-09-22, AOC AG276QZD2, Win11, RTX 3060) - ne tippelj
újra, és ne cseréld le egyiket sem "egyszerűbbre" mérés nélkül:

  1. PROFIL-TÁRSÍTÁS: a modern `ColorProfileAddDisplayAssociation` (mscms, Win10 2004+)
     MŰKÖDIK - a régi `WcsAssociateColorProfileWithDevice` ugyanezen a gépen TRUE-t adott és
     semmit nem írt (2026-07-31). A modern hívás a HELYES monitor-példányra ír: SDR-ként az
     `ICMProfile`, HDR (advanced color) módra az `ICMProfileAC` értékbe - pontosan oda, ahova
     a Microsoft saját HDR Calibration appja is. Leszedés: `ColorProfileRemoveDisplayAssociation`.
  2. AKTÍV PROFIL: `ColorProfileGetDisplayDefault(scope, luid, sourceID, CPT_ICC, subtype)`,
     ahol subtype 7 = SDR (standard display color mode), 8 = HDR (extended). Visszaolvasva
     pontosan azt adta, amit beírtunk; profil nélkül 0x80070002-t.
  3. VALÓS IDEJŰ GAMMA: a `SetDeviceGammaRamp` ezen a gépen elfogadja a táblát, és a
     visszaolvasás igazolja. A Windows a profil VCGT-jét csak bejelentkezéskor tölti be -
     ezért a "real time" aktiváláshoz MI töltjük be, és visszaolvassuk.
  4. GYÁRI FÁJL: a Windows saját színfájljai (sRGB, RSWOP, WCS .camp/.cdmp/.gmmp) mind
     `NT SERVICE\\TrustedInstaller` tulajdonúak, a később hozzáadottak nem. Ez a gyári
     visszaállítás határa: ami TrustedInstaller-é, az MAGA a gyári állapot.
  5. ACM = WCG: lásd display_core.set_acm.

A GYÁRI VISSZAÁLLÍTÁS TÖRÖL (explicit user decision: *"minden profilt minden beállítást
bármit és mindent ami színnel kapcsolatos"*). Ami megmarad, az a Windows SAJÁT fájlja -
ez nem kivétel, hanem a gyári állapot definíciója. A nyomtató-profilok törlése a képernyőn
egy NEVESÍTETT, alapból bekapcsolt kapcsoló (CLAUDE.md 1. elv: törlésbe néma kivétel nem
kerülhet, csak ember által, a képernyőn meghozott döntés).
"""

# === AUTO-IMPORTS ===
import os
import struct
import ctypes
import logging
import winreg
from ctypes import wintypes
from app import win32 as w32
from app import display_core
from app.common import _app_data_dir
# === /AUTO-IMPORTS ===


_mscms = ctypes.WinDLL('mscms', use_last_error=True)
_advapi = ctypes.WinDLL('advapi32', use_last_error=True)
_kernel = ctypes.WinDLL('kernel32', use_last_error=True)

_ICM_BASE = r'SOFTWARE\Microsoft\Windows NT\CurrentVersion\ICM'
_ICM_ASSOC = _ICM_BASE + r'\ProfileAssociations\Display'
_ICM_REGISTERED = _ICM_BASE + r'\RegisteredProfiles'
_ICM_CALIB = _ICM_BASE + r'\Calibration'
_MONITOR_DATASTORE = r'SYSTEM\CurrentControlSet\Control\GraphicsDrivers\MonitorDataStore'
_COLOR_FILTER_KEY = r'Software\Microsoft\ColorFiltering'
_CLOUDSTORE_CURRENT = r'Software\Microsoft\Windows\CurrentVersion\CloudStore\Store\DefaultAccount\Current'

# Az SDR és a HDR (advanced color) profil registry-értékének neve a monitor-példány kulcsán.
VALUE_SDR = 'ICMProfile'
VALUE_HDR = 'ICMProfileAC'

WCS_SCOPE_SYSTEM = 0
WCS_SCOPE_USER = 1
CPT_ICC = 0
CPST_SDR = 7        # CPST_STANDARD_DISPLAY_COLOR_MODE - mérve
CPST_HDR = 8        # CPST_EXTENDED_DISPLAY_COLOR_MODE - mérve

# A Windows saját színfájljai - a TrustedInstaller-tulajdon mellett névlistával is, hogy
# egy olvashatatlan ACL se tehesse törölhetővé a Windows sRGB-jét (belt and braces).
WINDOWS_COLOR_FILES = {
    'srgb color space profile.icm', 'rswop.icm',
    'd50.camp', 'd65.camp', 'graphics.gmmp', 'mediasim.gmmp', 'photo.gmmp',
    'proofing.gmmp', 'wscrgb.cdmp', 'wsrgb.cdmp',
}
COLOR_FILE_EXTS = ('.icc', '.icm', '.camp', '.cdmp', '.gmmp')

# A HKLM RegisteredProfiles gyári tartalma (a WCS alapértelmezett renderelési szándékai).
# Élőben kiolvasva 2026-09-22; mind Windows-fájlra mutat.
REGISTERED_DEFAULTS = {
    'camp': (winreg.REG_SZ, 'D65.camp'),
    'ri': (winreg.REG_DWORD, 0),
    'riac': (winreg.REG_SZ, 'MediaSim.gmmp'),
    'rip': (winreg.REG_SZ, 'Photo.gmmp'),
    'rirc': (winreg.REG_SZ, 'Proofing.gmmp'),
    'ris': (winreg.REG_SZ, 'Graphics.gmmp'),
}
DISPLAY_CALIBRATOR_DEFAULT = r'%SystemRoot%\System32\DCCW.exe'
CALIB_LOADER_TASK = r'\Microsoft\Windows\WindowsColorSystem\Calibration Loader'

# A MonitorDataStore per-monitor színértékei, amiket a gyári visszaállítás a NEM
# csatlakoztatott monitoroknál töröl (a Windows első csatlakozáskor újraírja őket az
# alapértelmezéssel). A csatlakoztatott monitoroknál a HDR/ACM élő API-val áll vissza.
DATASTORE_COLOR_VALUES = ('HDREnabled', 'SDRWhiteLevel', 'AutoColorManagementEnabled')

# Az Éjszakai fény (Night light) beállításai. A kulcs törlése = gyári alapállapot
# (a Windows újra létrehozza, alapból kikapcsolva). Kijelentkezés után él teljesen.
NIGHT_LIGHT_MARKER = 'windows.data.bluelightreduction'

# Az ACM-zár ütemezett feladata és szkriptje (lásd install_acm_guard).
ACM_GUARD_TASK = 'DriverVarazsloColorGuard'
ACM_GUARD_SCRIPT = 'color_guard.ps1'
ACM_GUARD_LOG = 'color_guard.log'


# ============================================================================
# Kis segédek
# ============================================================================

def _luid(low, high):
    l = w32._LUID()
    l.LowPart = int(low) & 0xFFFFFFFF
    l.HighPart = int(high)
    return l


def _hr(v):
    return f'0x{(v or 0) & 0xFFFFFFFF:08X}'


def _api(name):
    """Egy mscms függvény, vagy None, ha a Windows túl régi hozzá (Win10 2004 előtt)."""
    try:
        return getattr(_mscms, name)
    except AttributeError:
        return None


def _setup_api():
    f = _api('ColorProfileAddDisplayAssociation')
    if f is not None:
        f.restype = ctypes.c_long
        f.argtypes = [ctypes.c_int, wintypes.LPCWSTR, w32._LUID, wintypes.UINT, wintypes.BOOL, wintypes.BOOL]
    r = _api('ColorProfileRemoveDisplayAssociation')
    if r is not None:
        r.restype = ctypes.c_long
        r.argtypes = [ctypes.c_int, wintypes.LPCWSTR, w32._LUID, wintypes.UINT, wintypes.BOOL]
    g = _api('ColorProfileGetDisplayDefault')
    if g is not None:
        g.restype = ctypes.c_long
        g.argtypes = [ctypes.c_int, w32._LUID, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                      ctypes.POINTER(wintypes.LPWSTR)]
    return f, r, g


_API_ADD, _API_REMOVE, _API_DEFAULT = _setup_api()
logging.debug(f"[COLOR] Modern színprofil-API: add={'OK' if _API_ADD else 'NINCS'}, "
              f"remove={'OK' if _API_REMOVE else 'NINCS'}, default={'OK' if _API_DEFAULT else 'NINCS'}")


def _source_of(display):
    return (_luid(display.get('source_adapter_low', 0), display.get('source_adapter_high', 0)),
            int(display.get('source_id', 0)))


def file_owner(path):
    """Egy fájl tulajdonosának neve ('NT SERVICE\\TrustedInstaller', ...) vagy None."""
    owner = ctypes.c_void_p()
    sd = ctypes.c_void_p()
    rc = _advapi.GetNamedSecurityInfoW(ctypes.c_wchar_p(path), 1, 1, ctypes.byref(owner),
                                       None, None, None, ctypes.byref(sd))
    if rc != 0:
        return None
    try:
        name = ctypes.create_unicode_buffer(256)
        dom = ctypes.create_unicode_buffer(256)
        cn, cd, use = wintypes.DWORD(256), wintypes.DWORD(256), wintypes.DWORD()
        if not _advapi.LookupAccountSidW(None, owner, name, ctypes.byref(cn), dom, ctypes.byref(cd),
                                         ctypes.byref(use)):
            return None
        return f'{dom.value}\\{name.value}' if dom.value else name.value
    finally:
        _kernel.LocalFree(sd)


def is_windows_file(path):
    """A Windows saját (a telepítéssel érkező) színfájlja-e. Két jel, bármelyik elég:
    TrustedInstaller a tulajdonos (mérve: mind a 10 gyári fájl az), vagy a gyári névlista."""
    if os.path.basename(path).lower() in WINDOWS_COLOR_FILES:
        return True
    owner = (file_owner(path) or '').lower()
    return owner.endswith('trustedinstaller')


# ============================================================================
# Profilkönyvtár
# ============================================================================

def profile_kind(p):
    """A profil szerepe emberi nyelven. A 'hdr' a MHC2 tag jelenléte: ez a Windows
    HDR-kalibrációs (advanced color) profilja - a többi SDR-profil."""
    ext = os.path.splitext(p.get('file', ''))[1].lower()
    if ext in ('.camp', '.cdmp', '.gmmp'):
        return 'wcs'
    cls = p.get('class')
    if cls == 'Monitor':
        return 'monitor'
    if cls in ('Nyomtató', 'Szkenner'):
        return 'printer'
    if cls == 'Színtér':
        return 'colorspace'
    return 'other'


def list_library():
    """A színprofil-mappa TELJES tartalma, a nézet profil-könyvtárához.
    Minden bejegyzéshez: a display_core.parse_icc adatai + 'windows' (gyári-e),
    'hdr' (MHC2), 'kind', 'selectable' (kijelzőre tehető-e)."""
    cdir = display_core.color_directory()
    out = []
    try:
        names = sorted(os.listdir(cdir), key=str.lower)
    except OSError as e:
        logging.error(f"[COLOR] A színprofil-mappa nem olvasható ({cdir}): {e}")
        return cdir, out
    for fn in names:
        full = os.path.join(cdir, fn)
        if not os.path.isfile(full) or not fn.lower().endswith(COLOR_FILE_EXTS):
            continue
        if fn.lower().endswith(('.icc', '.icm')):
            p = display_core.parse_icc(full)
        else:
            p = {'file': fn, 'path': full, 'desc': fn, 'class': 'WCS-modell',
                 'size': os.path.getsize(full)}
        p['windows'] = is_windows_file(full)
        p['hdr'] = bool(p.get('has_mhc2'))
        p['kind'] = profile_kind(p)
        # Kijelzőre tehető: monitor- és színtér-profil (a Windows sRGB-je is "színtér").
        p['selectable'] = p['kind'] in ('monitor', 'colorspace') and not p.get('hiba')
        out.append(p)
    order = {'monitor': 0, 'colorspace': 1, 'printer': 2, 'other': 3, 'wcs': 4}
    out.sort(key=lambda p: (order.get(p['kind'], 9), p['windows'], p.get('file', '').lower()))
    logging.info(f"[COLOR] Profilkönyvtár ({cdir}): {len(out)} fájl - "
                 f"{sum(1 for p in out if p['windows'])} Windows-gyári, "
                 f"{sum(1 for p in out if not p['windows'])} hozzáadott, "
                 f"{sum(1 for p in out if p['hdr'])} HDR (MHC2).")
    return cdir, out


# ============================================================================
# Társítások olvasása
# ============================================================================

def _read_multi(root, key, value):
    try:
        k = winreg.OpenKey(root, key)
        v, _t = winreg.QueryValueEx(k, value)
    except OSError:
        return []
    vals = v if isinstance(v, list) else [v]
    return [x.strip() for x in vals if isinstance(x, str) and x.strip()]


def all_associations():
    """A ProfileAssociations\\Display ág MINDEN monitor-példányának társításai
    (HKCU + HKLM, SDR + HDR érték). Egy elem: {root, instance, value, profiles}.
    Az 'instance' a '{osztály-GUID}\\NNNN' alak - ez köti a példányt egy monitorhoz."""
    out = []
    for root, rname in ((winreg.HKEY_CURRENT_USER, 'HKCU'), (winreg.HKEY_LOCAL_MACHINE, 'HKLM')):
        try:
            base = winreg.OpenKey(root, _ICM_ASSOC)
        except OSError:
            continue
        i = 0
        while True:
            try:
                guid = winreg.EnumKey(base, i)
            except OSError:
                break
            i += 1
            try:
                gk = winreg.OpenKey(base, guid)
            except OSError:
                continue
            j = 0
            while True:
                try:
                    inst = winreg.EnumKey(gk, j)
                except OSError:
                    break
                j += 1
                key = f'{_ICM_ASSOC}\\{guid}\\{inst}'
                for vname in (VALUE_SDR, VALUE_HDR):
                    profs = _read_multi(root, key, vname)
                    if profs:
                        out.append({'root': rname, 'instance': f'{guid}\\{inst}'.lower(),
                                    'value': vname, 'hdr': vname == VALUE_HDR, 'profiles': profs})
    return out


def instance_of(monitor_device_id):
    """MONITOR\\AOCA610\\{guid}\\0003 -> '{guid}\\0003' (kisbetűvel) vagy None."""
    parts = (monitor_device_id or '').split('\\')
    if len(parts) >= 4 and parts[2].startswith('{'):
        return f'{parts[2]}\\{parts[3]}'.lower()
    return None


def _get_default(display, subtype):
    """A Windows szerint épp aktív profil (subtype 7 = SDR, 8 = HDR) - vagy None."""
    if _API_DEFAULT is None:
        return None
    luid, sid = _source_of(display)
    for scope in (WCS_SCOPE_USER, WCS_SCOPE_SYSTEM):
        out = wintypes.LPWSTR()
        hr = _API_DEFAULT(scope, luid, sid, CPT_ICC, subtype, ctypes.byref(out))
        if hr == 0 and out.value:
            name = out.value
            _kernel.LocalFree(ctypes.cast(out, ctypes.c_void_p))
            return name
    return None


def display_profiles(display, assocs=None):
    """Egy kijelző SDR és HDR profilja. A 'active_*' a Windows SAJÁT válasza
    (ColorProfileGetDisplayDefault) - ha az API nincs, a registry első eleme."""
    if assocs is None:
        assocs = all_associations()
    inst = instance_of(display.get('monitor_device_id', ''))
    sdr, hdr = [], []
    for a in assocs:
        if a['instance'] != inst:
            continue
        bucket = hdr if a['hdr'] else sdr
        for p in a['profiles']:
            if p not in bucket:
                bucket.append(p)
    act_sdr = _get_default(display, CPST_SDR) if _API_DEFAULT else None
    act_hdr = _get_default(display, CPST_HDR) if _API_DEFAULT else None
    if _API_DEFAULT is None:
        act_sdr = sdr[0] if sdr else None
        act_hdr = hdr[0] if hdr else None
    return {'instance': inst, 'sdr': sdr, 'hdr': hdr, 'active_sdr': act_sdr, 'active_hdr': act_hdr}


def orphan_associations(displays, assocs=None):
    """Társítások olyan monitor-példányon, ami MOST NINCS csatlakoztatva. Mérve a fejlesztői
    gépen: a Windows HDR Calibration app a \\0004 példányra írta a profilt, miközben az élő
    monitor a \\0003 - a Windows Beállításai ezért "No profile found"-ot mutattak."""
    if assocs is None:
        assocs = all_associations()
    live = {instance_of(d.get('monitor_device_id', '')) for d in displays}
    return [a for a in assocs if a['instance'] not in live]


# ============================================================================
# Gamma (VCGT) - valós idejű betöltés
# ============================================================================

def parse_vcgt(data):
    """Egy ICC-fájl VCGT (videokártya gamma) tagja -> [r256, g256, b256] 0..65535, vagy None.
    Két formátum: 0 = táblázat (csatorna/elem/elemméret), 1 = formula (gamma, min, max)."""
    try:
        if len(data) < 132:
            return None
        n = struct.unpack('>I', data[128:132])[0]
        body = None
        for i in range(min(n, 200)):
            off = 132 + i * 12
            sig, o, sz = struct.unpack('>4sII', data[off:off + 12])
            if sig == b'vcgt' and o + sz <= len(data):
                body = data[o:o + sz]
                break
        if not body or len(body) < 12:
            return None
        gtype = struct.unpack('>I', body[8:12])[0]
        chans = []
        if gtype == 0:
            ch, cnt, esz = struct.unpack('>HHH', body[12:18])
            if cnt < 2 or esz not in (1, 2) or ch not in (1, 3):
                return None
            raw = body[18:]
            for c in range(ch):
                vals = []
                for k in range(cnt):
                    pos = (c * cnt + k) * esz
                    if esz == 1:
                        vals.append(raw[pos] / 255.0)
                    else:
                        vals.append(struct.unpack('>H', raw[pos:pos + 2])[0] / 65535.0)
                chans.append(vals)
            if ch == 1:
                chans = chans * 3
        elif gtype == 1:
            for c in range(3):
                g, lo, hi = struct.unpack('>iii', body[12 + c * 12:24 + c * 12])
                g, lo, hi = g / 65536.0, lo / 65536.0, hi / 65536.0
                chans.append([lo + (hi - lo) * ((k / 255.0) ** g) for k in range(256)])
        else:
            return None
        out = []
        for vals in chans:
            m = len(vals) - 1
            res = []
            for i in range(256):
                x = i * m / 255.0
                a = int(x)
                b = min(a + 1, m)
                t = x - a
                v = vals[a] * (1 - t) + vals[b] * t
                res.append(max(0, min(65535, int(round(v * 65535)))))
            out.append(res)
        return out
    except Exception as e:
        logging.warning(f"[COLOR] VCGT értelmezési hiba: {e}")
        return None


def _linear_ramp():
    lin = [(i * 65535) // 255 for i in range(256)]
    return [lin, lin, lin]


def set_gamma(gdi_name, ramp, why=''):
    """Gamma-tábla betöltése egy kijelzőre, VISSZAOLVASÁSSAL. Visszaad: (sikerult, uzenet)."""
    hdc = ctypes.windll.gdi32.CreateDCW(None, gdi_name, None, None) if gdi_name else 0
    if not hdc:
        logging.warning(f"[COLOR] Gamma: nem nyitható DC ({gdi_name}).")
        return False, 'A kijelzőhöz nem nyitható eszközkapcsolat.'
    buf = (ctypes.c_ushort * 768)()
    for c in range(3):
        for i in range(256):
            buf[c * 256 + i] = ramp[c][i]
    try:
        ok = ctypes.windll.gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(buf))
    finally:
        ctypes.windll.gdi32.DeleteDC(hdc)
    logging.warning(f"[COLOR] Gamma-tábla BETÖLTÉSE ({gdi_name}; {why}): SetDeviceGammaRamp={ok}")
    if not ok:
        return False, ('A Windows elutasította a gamma-táblát (a túl erős korrekciót a Windows '
                       'biztonsági korlátja nem engedi) - bejelentkezéskor a Windows maga tölti be.')
    # A verdikt a VISSZAOLVASÁS: a kiírt tábla és a kijelzőn lévő legnagyobb eltérése.
    back = display_core.read_gamma_ramp(gdi_name)
    if back is not None:
        want_linear = ramp == _linear_ramp()
        if want_linear and not back.get('linear'):
            logging.error(f"[COLOR] Gamma: lineárist írtunk, de a visszaolvasott tábla módosított "
                          f"({back.get('max_deviation')}/65535) - valami felülírta.")
            return False, 'a visszaolvasott gamma-tábla nem lineáris - egy másik program felülírta'
    return True, ''


def reset_gamma(gdi_name, why='lineáris visszaállítás'):
    return set_gamma(gdi_name, _linear_ramp(), why)


def apply_profile_gamma(display, profile_file):
    """A kijelző SDR-profiljának gamma-görbéje azonnal, bejelentkezés nélkül.
    Profil nélkül vagy VCGT nélküli profilnál LINEÁRIS tábla megy ki - különben egy korábbi
    profil görbéje a kijelzőn maradna. Ha a profil-betöltés le van tiltva, mindig lineáris.
    Visszaad: (sikerult, leiras)."""
    gdi = display.get('gdi_name', '')
    if display_core.calibration_management() == 0:
        ok, msg = reset_gamma(gdi, 'a profil-betöltés le van tiltva')
        return ok, msg or 'lineáris (a profil-betöltés le van tiltva)'
    if not profile_file:
        ok, msg = reset_gamma(gdi, 'nincs SDR-profil')
        return ok, msg or 'lineáris (nincs profil)'
    full = os.path.join(display_core.color_directory(), profile_file)
    try:
        with open(full, 'rb') as fh:
            data = fh.read()
    except OSError as e:
        logging.warning(f"[COLOR] A profil nem olvasható ({full}): {e}")
        return False, f'a profilfájl nem olvasható: {e}'
    ramp = parse_vcgt(data)
    if ramp is None:
        ok, msg = reset_gamma(gdi, f'{profile_file}: nincs VCGT')
        return ok, msg or 'lineáris (a profilnak nincs gamma-görbéje)'
    ok, msg = set_gamma(gdi, ramp, f'{profile_file} VCGT')
    return ok, msg or 'a profil gamma-görbéje betöltve'


# ============================================================================
# Profil aktiválása / levétele
# ============================================================================

def _ensure_per_user(instance):
    """UsePerUserProfiles=1 a HKCU példánykulcson - a régi Színkezelés vezérlőpult
    ("Use my settings for this device") enélkül nem a felhasználói társítást mutatja."""
    try:
        k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, f'{_ICM_ASSOC}\\{instance}', 0,
                               winreg.KEY_SET_VALUE)
        winreg.SetValueEx(k, 'UsePerUserProfiles', 0, winreg.REG_DWORD, 1)
    except OSError as e:
        logging.warning(f"[COLOR] UsePerUserProfiles nem írható ({instance}): {e}")


def _registry_assoc(instance, value, profiles):
    """Tartalék: közvetlen registry-írás (a 2026-07-31 óta bizonyítottan működő út),
    ha a modern API nincs meg vagy a visszaolvasás nem igazolta."""
    k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, f'{_ICM_ASSOC}\\{instance}', 0, winreg.KEY_ALL_ACCESS)
    if profiles:
        winreg.SetValueEx(k, value, 0, winreg.REG_MULTI_SZ, profiles)
    else:
        try:
            winreg.DeleteValue(k, value)
        except OSError:
            pass
    winreg.SetValueEx(k, 'UsePerUserProfiles', 0, winreg.REG_DWORD, 1)


def activate_profile(display, profile_file, hdr=False):
    """Egy profil AKTIVÁLÁSA egy kijelzőn, SDR- vagy HDR-módra. Menet: modern API
    (felhasználói hatókör, alapértelmezettnek jelölve) -> VISSZAOLVASÁS a Windows saját
    lekérdezésével -> ha nem igazolt, registry-tartalék -> újra visszaolvasás. SDR-nél a
    gamma-görbe azonnal betöltődik (valós idő). Visszaad: dict(ok, verified, via, gamma)."""
    inst = instance_of(display.get('monitor_device_id', ''))
    slot = 'HDR' if hdr else 'SDR'
    res = {'ok': False, 'verified': False, 'via': None, 'gamma': None, 'error': ''}
    if not inst:
        res['error'] = 'A monitor azonosítója nem értelmezhető.'
        return res
    before = display_profiles(display)
    logging.warning(f"[COLOR] {slot}-profil AKTIVÁLÁSA: '{profile_file}' -> {display.get('name')} "
                    f"({inst}); eddig aktív: {before['active_hdr' if hdr else 'active_sdr'] or 'nincs'}")
    subtype = CPST_HDR if hdr else CPST_SDR
    if _API_ADD is not None:
        luid, sid = _source_of(display)
        hr = _API_ADD(WCS_SCOPE_USER, profile_file, luid, sid, True, bool(hdr))
        logging.info(f"[COLOR] ColorProfileAddDisplayAssociation hr={_hr(hr)}")
        if hr == 0:
            res['via'] = 'api'
    got = _get_default(display, subtype) if _API_DEFAULT else None
    if (got or '').lower() != profile_file.lower():
        logging.warning(f"[COLOR] A Windows aktív {slot}-profilja a hívás után: {got!r} - "
                        f"registry-tartalékra váltunk.")
        try:
            cur = display_profiles(display)['hdr' if hdr else 'sdr']
            _registry_assoc(inst, VALUE_HDR if hdr else VALUE_SDR,
                            [profile_file] + [p for p in cur if p.lower() != profile_file.lower()])
            res['via'] = 'registry'
        except OSError as e:
            res['error'] = str(e)
            logging.error(f"[COLOR] A registry-tartalék sem sikerült: {e}")
            return res
        got = _get_default(display, subtype) if _API_DEFAULT else profile_file
    _ensure_per_user(inst)
    res['verified'] = (got or '').lower() == profile_file.lower()
    res['ok'] = res['verified'] or res['via'] == 'registry'
    if not hdr:
        g_ok, g_msg = apply_profile_gamma(display, profile_file)
        res['gamma'] = g_msg
        res['gamma_ok'] = g_ok
    logging.info(f"[COLOR] {slot}-aktiválás eredménye: ok={res['ok']} igazolva={res['verified']} "
                 f"út={res['via']} gamma={res.get('gamma')}")
    return res


def _remove_from_instance(instance, profile_file=None, hdr=None):
    """Registry-takarítás egy példányon: a megadott profil (vagy MINDEN profil) törlése az
    SDR és/vagy HDR értékből, mindkét gyökérben. Minden törlést névvel naplóz."""
    removed = []
    for root, rname in ((winreg.HKEY_CURRENT_USER, 'HKCU'), (winreg.HKEY_LOCAL_MACHINE, 'HKLM')):
        key = f'{_ICM_ASSOC}\\{instance}'
        try:
            k = winreg.OpenKey(root, key, 0, winreg.KEY_ALL_ACCESS)
        except OSError:
            continue
        for vname in (VALUE_SDR, VALUE_HDR):
            if hdr is not None and (vname == VALUE_HDR) != hdr:
                continue
            cur = _read_multi(root, key, vname)
            if not cur:
                continue
            left = [] if profile_file is None else [p for p in cur if p.lower() != profile_file.lower()]
            gone = [p for p in cur if p not in left]
            if not gone:
                continue
            logging.warning(f"[COLOR] Társítás TÖRLÉSE: {rname}\\...\\{instance} [{vname}] - {gone}")
            try:
                if left:
                    winreg.SetValueEx(k, vname, 0, winreg.REG_MULTI_SZ, left)
                else:
                    winreg.DeleteValue(k, vname)
                removed.extend(f'{rname}:{instance}:{vname}:{p}' for p in gone)
            except OSError as e:
                logging.error(f"[COLOR] Nem sikerült törölni ({rname} {instance} {vname}): {e}")
    return removed


def clear_slot(display, hdr=False):
    """Egy kijelző SDR- vagy HDR-profiljának LEVÉTELE (-> Windows alapértelmezés).
    Modern API-val minden társított profilt leszed, majd registryből is kitakarít
    (mindkét gyökér), végül SDR-nél lineárisra állítja a gammát. Visszaad: (ok, leírás)."""
    inst = instance_of(display.get('monitor_device_id', ''))
    if not inst:
        return False, 'A monitor azonosítója nem értelmezhető.'
    cur = display_profiles(display)
    names = cur['hdr'] if hdr else cur['sdr']
    act = cur['active_hdr'] if hdr else cur['active_sdr']
    if act and act not in names:
        names = [act] + names
    logging.warning(f"[COLOR] {'HDR' if hdr else 'SDR'}-profil LEVÉTELE: {display.get('name')} ({inst}) - {names or 'nincs'}")
    if _API_REMOVE is not None:
        luid, sid = _source_of(display)
        for p in names:
            for scope in (WCS_SCOPE_USER, WCS_SCOPE_SYSTEM):
                hr = _API_REMOVE(scope, p, luid, sid, bool(hdr))
                logging.debug(f"[COLOR] ColorProfileRemoveDisplayAssociation({scope}, {p}) hr={_hr(hr)}")
    _remove_from_instance(inst, None, hdr)
    after = display_profiles(display)
    left = after['active_hdr'] if hdr else after['active_sdr']
    gamma = ''
    if not hdr:
        _ok, gamma = apply_profile_gamma(display, None)
    if left:
        logging.error(f"[COLOR] A levétel után a Windows még mindig ezt mutatja aktívnak: {left}")
        return False, f'a Windows még mindig ezt mutatja: {left}'
    return True, gamma


def remove_all_associations(reason):
    """MINDEN kijelző-társítás törlése (minden példány, SDR+HDR, HKCU+HKLM). A profil-
    betöltés tiltása és a gyári visszaállítás közös lépése. Visszaad: a törölt tételek."""
    removed = []
    insts = sorted({a['instance'] for a in all_associations()})
    logging.warning(f"[COLOR] ÖSSZES kijelző-társítás törlése ({reason}): {len(insts)} példány")
    for inst in insts:
        removed.extend(_remove_from_instance(inst))
    return removed


def remove_orphans(displays):
    removed = []
    for inst in sorted({a['instance'] for a in orphan_associations(displays)}):
        removed.extend(_remove_from_instance(inst))
    return removed


# ============================================================================
# Profil telepítése / törlése
# ============================================================================

def install_profile(src_path):
    return display_core.install_profile(src_path)


def delete_profile(profile_file):
    """Egy profilfájl TÖRLÉSE: előbb minden kijelző minden társításából kivesszük (árva
    hivatkozás ne maradjon), aztán a fájl megy. Windows-gyári fájlt is enged (1. elv:
    ami töröl, az töröl) - a felület külön figyelmeztet rá. Visszaad: (ok, uzenet)."""
    full = os.path.join(display_core.color_directory(), profile_file)
    logging.warning(f"[COLOR] Profilfájl TÖRLÉSE: {full} (Windows-gyári: "
                    f"{is_windows_file(full) if os.path.exists(full) else '?'})")
    for inst in sorted({a['instance'] for a in all_associations()}):
        _remove_from_instance(inst, profile_file)
    ctypes.set_last_error(0)
    ok = _mscms.UninstallColorProfileW(None, profile_file, True)
    err = ctypes.get_last_error()
    if not ok and os.path.exists(full):
        logging.info(f"[COLOR] UninstallColorProfileW sikertelen (err={err}) - közvetlen törlés.")
        try:
            os.remove(full)
        except OSError as e:
            logging.error(f"[COLOR] A fájl nem törölhető: {e}")
            return False, f'nem törölhető: {e}'
    if os.path.exists(full):
        return False, 'a fájl a törlés után is megvan'
    logging.info(f"[COLOR] Törölve: {profile_file}")
    return True, ''


# ============================================================================
# Profil-betöltés engedélyezése / tiltása
# ============================================================================

def set_profile_loading(enabled, displays):
    """A Windows profil-betöltésének (CalibrationManagementEnabled) kapcsolása.
    TILTÁSKOR (explicit user decision, 2026-09-22: *"miután letiltja nyilván bármi icc
    profil ami van a monitoron azt is szedje le róla"*): minden kijelző-társítás törlődik
    és a gamma MOST lineárisra áll - nem csak a következő bejelentkezéskor.
    ENGEDÉLYEZÉSKOR semmi nem kerül vissza: csak újra LEHET profilt aktiválni.
    Visszaad: dict(ok, prev, removed, gamma)."""
    ok, prev = display_core.set_calibration_management(bool(enabled))
    out = {'ok': ok, 'prev': prev, 'removed': [], 'gamma': []}
    if not ok or enabled:
        return out
    out['removed'] = remove_all_associations('a profil-betöltés letiltása')
    for d in displays:
        g_ok, g_msg = reset_gamma(d.get('gdi_name', ''), 'profil-betöltés letiltva')
        out['gamma'].append((d.get('name'), g_ok, g_msg))
    return out


# ============================================================================
# ACM-zár: az automatikus színkezelés tartós kikapcsolása
# ============================================================================

# A zár-szkript minden bejelentkezéskor lefut (a felhasználó munkamenetében, mert a
# DisplayConfig a felhasználó asztalán dolgozik), és minden kijelzőn, ahol az ACM BE van
# (és nem HDR módban), kikapcsolja. A C# a win32.py struktúráit tükrözi: PATH_INFO = 72
# bájt, a target LUID a 20., a target id a 28. bájton (ctypes-szal lemérve 2026-09-22).
ACM_GUARD_PS = r'''
$ErrorActionPreference = 'Continue'
$log = Join-Path $PSScriptRoot 'color_guard.log'
function W($m) { try { if ((Test-Path $log) -and ((Get-Item $log).Length -gt 65536)) { Remove-Item $log -Force }
  Add-Content -Path $log -Value ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ' + $env:USERNAME + ' ' + $m) } catch {} }
$src = @"
using System; using System.Runtime.InteropServices;
public static class DvAcmGuard {
  [StructLayout(LayoutKind.Sequential)] public struct LUID { public uint Low; public int High; }
  [StructLayout(LayoutKind.Sequential)] public struct ACI2 { public uint type; public uint size; public LUID adapter; public uint id; public uint value; public uint enc; public uint bits; public uint mode; }
  [StructLayout(LayoutKind.Sequential)] public struct WCG { public uint type; public uint size; public LUID adapter; public uint id; public uint value; }
  [DllImport("user32.dll")] static extern int GetDisplayConfigBufferSizes(uint f, out uint np, out uint nm);
  [DllImport("user32.dll")] static extern int QueryDisplayConfig(uint f, ref uint np, byte[] p, ref uint nm, byte[] m, IntPtr t);
  [DllImport("user32.dll")] static extern int DisplayConfigGetDeviceInfo(ref ACI2 p);
  [DllImport("user32.dll")] static extern int DisplayConfigSetDeviceInfo(ref WCG p);
  public static string Run() {
    uint np, nm; string o = "";
    if (GetDisplayConfigBufferSizes(2, out np, out nm) != 0) return "BUFFER-HIBA";
    byte[] p = new byte[np * 72]; byte[] m = new byte[nm * 64];
    if (QueryDisplayConfig(2, ref np, p, ref nm, m, IntPtr.Zero) != 0) return "QUERY-HIBA";
    for (int i = 0; i < np; i++) {
      int b = i * 72;
      LUID l = new LUID(); l.Low = BitConverter.ToUInt32(p, b + 20); l.High = BitConverter.ToInt32(p, b + 24);
      uint tid = BitConverter.ToUInt32(p, b + 28);
      ACI2 a = new ACI2(); a.type = 15; a.size = (uint)Marshal.SizeOf(typeof(ACI2)); a.adapter = l; a.id = tid;
      if (DisplayConfigGetDeviceInfo(ref a) != 0) { o += "[" + tid + ": nincs ACI2] "; continue; }
      bool on = (a.value & 0x80) != 0;
      if (!on || a.mode == 2) { o += "[" + tid + ": ACM ki] "; continue; }
      WCG w = new WCG(); w.type = 17; w.size = (uint)Marshal.SizeOf(typeof(WCG)); w.adapter = l; w.id = tid; w.value = 0;
      int rc = DisplayConfigSetDeviceInfo(ref w);
      o += "[" + tid + ": ACM BE VOLT -> kikapcsolva rc=" + rc + "] ";
    }
    return o;
  }
}
"@
try { Add-Type -TypeDefinition $src -ErrorAction Stop; W ([DvAcmGuard]::Run()) } catch { W ("HIBA: " + $_.Exception.Message) }
'''


def guard_script_path():
    return os.path.join(_app_data_dir(), ACM_GUARD_SCRIPT)


def guard_log_path():
    return os.path.join(_app_data_dir(), ACM_GUARD_LOG)


def acm_guard_installed(run):
    """Él-e az ACM-zár ütemezett feladata. schtasks /query - gyors (nem PowerShell)."""
    res = run(['schtasks', '/query', '/tn', ACM_GUARD_TASK], timeout=20, ok_codes=(0, 1))
    return getattr(res, 'returncode', 1) == 0


def acm_guard_last_line():
    try:
        with open(guard_log_path(), 'r', encoding='utf-8', errors='replace') as fh:
            lines = [l.strip() for l in fh if l.strip()]
        return lines[-1] if lines else ''
    except OSError:
        return ''


def install_acm_guard(run):
    """Az ACM-zár telepítése: szkript az adatmappába + bejelentkezéskor futó ütemezett
    feladat MINDEN felhasználóra (Users csoport, korlátozott jogkör - a DisplayConfig
    WCG-kapcsolásához nem kell rendszergazda). Tartós rendszerváltozás, ezért a nézet
    mindig kiírja, hogy él, és a gyári visszaállítás eltávolítja. Visszaad: (ok, uzenet)."""
    path = guard_script_path()
    logging.warning(f"[COLOR] ACM-ZÁR TELEPÍTÉSE: {path} + ütemezett feladat '{ACM_GUARD_TASK}' "
                    f"(minden bejelentkezéskor kikapcsolja az automatikus színkezelést).")
    try:
        with open(path, 'w', encoding='utf-8-sig', newline='\r\n') as fh:
            fh.write(ACM_GUARD_PS.strip() + '\n')
    except OSError as e:
        logging.error(f"[COLOR] A zár-szkript nem írható: {e}")
        return False, f'a szkript nem írható: {e}'
    # A regisztráció -ErrorAction Stop + try/catch: a Register-ScheduledTask hibája
    # NEM-VÉGZETES, és egy utána álló 'OK' akkor is kiíródna - az első változat pontosan
    # így jelentett sikert egy "Access is denied"-ra (mérve 2026-09-22). A verdikt ráadásul
    # nem is ez a szöveg, hanem a regisztráció UTÁNI schtasks /query.
    # Elsőként MINDEN felhasználóra (Users csoport) - ehhez rendszergazda kell, a program
    # az; ha mégsem megy, az aktuális felhasználóra esünk vissza (a szerviz gépein ez a
    # tipikus egyetlen fiók).
    ps = (
        "$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "
        f"'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"{path}\"'; "
        "$t = New-ScheduledTaskTrigger -AtLogOn; $t.Delay = 'PT15S'; "
        "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5); "
        "$d = 'DriverVarazslo: az automatikus szinkezeles (ACM) tartos kikapcsolasa. "
        "Eltavolitas: a program Kijelzo nezete.'; "
        "try { $p = New-ScheduledTaskPrincipal -GroupId 'S-1-5-32-545' -RunLevel Limited; "
        f"Register-ScheduledTask -TaskName '{ACM_GUARD_TASK}' -Action $a -Trigger $t -Principal $p "
        "-Settings $s -Description $d -Force -ErrorAction Stop | Out-Null; 'REG:minden-felhasznalo' } "
        "catch { 'GROUPFAIL:' + $_.Exception.Message; "
        "try { $p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited; "
        "$t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME; $t.Delay = 'PT15S'; "
        f"Register-ScheduledTask -TaskName '{ACM_GUARD_TASK}' -Action $a -Trigger $t -Principal $p "
        "-Settings $s -Description $d -Force -ErrorAction Stop | Out-Null; 'REG:aktualis-felhasznalo' } "
        "catch { 'FAIL:' + $_.Exception.Message } }"
    )
    res = run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps], timeout=60)
    out = (getattr(res, 'stdout', '') or '').strip()
    logging.info(f"[COLOR] ACM-zár regisztráció kimenete: {out[:400]}")
    if not acm_guard_installed(run):
        logging.error(f"[COLOR] Az ACM-zár feladata a regisztráció után NEM létezik: {out[:400]} "
                      f"{(getattr(res, 'stderr', '') or '')[:300]}")
        return False, 'az ütemezett feladat nem jött létre (részletek a naplóban)'
    return True, 'minden felhasználóra' if 'REG:minden' in out else 'az aktuális felhasználóra'


def remove_acm_guard(run):
    logging.warning(f"[COLOR] ACM-ZÁR ELTÁVOLÍTÁSA: '{ACM_GUARD_TASK}' + {guard_script_path()}")
    run(['schtasks', '/delete', '/tn', ACM_GUARD_TASK, '/f'], timeout=30, ok_codes=(0, 1))
    for p in (guard_script_path(),):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError as e:
            logging.warning(f"[COLOR] {p} nem törölhető: {e}")
    return not acm_guard_installed(run)


def enforce_acm_off(displays, reason):
    """Minden kijelzőn kikapcsolja az ACM-et, ahol be van (HDR módot kihagyva).
    Visszaad: a kikapcsolt kijelzők nevei."""
    done = []
    for d in displays:
        h = d.get('hdr') or {}
        if not h.get('wcg_enabled') or h.get('enabled'):
            continue
        aid, tid = display_core.find_display_target(d['adapter_low'], d['adapter_high'], d['target_id'])
        if aid is None:
            continue
        logging.warning(f"[COLOR] ACM kikapcsolása ({reason}): {d.get('name')}")
        ok, _st = display_core.set_acm(aid, tid, False)
        if ok:
            done.append(d.get('name'))
    return done


# ============================================================================
# Gyári visszaállítás
# ============================================================================

def _registered_changes():
    """A RegisteredProfiles eltérései a gyári állapottól: [(root, name, current, action)]."""
    out = []
    for root, rname in ((winreg.HKEY_CURRENT_USER, 'HKCU'), (winreg.HKEY_LOCAL_MACHINE, 'HKLM')):
        try:
            k = winreg.OpenKey(root, _ICM_REGISTERED)
        except OSError:
            if rname == 'HKLM':
                out.extend(('HKLM', n, None, 'set') for n in REGISTERED_DEFAULTS)
            continue
        seen = set()
        i = 0
        while True:
            try:
                name, val, typ = winreg.EnumValue(k, i)
            except OSError:
                break
            i += 1
            seen.add(name.lower())
            if rname == 'HKLM' and name.lower() in REGISTERED_DEFAULTS:
                dtyp, dval = REGISTERED_DEFAULTS[name.lower()]
                if typ != dtyp or val != dval:
                    out.append((rname, name, val, 'set'))
            else:
                out.append((rname, name, val, 'delete'))
        if rname == 'HKLM':
            out.extend(('HKLM', n, None, 'set') for n in REGISTERED_DEFAULTS if n not in seen)
    return out


def _datastore_changes(displays):
    """A NEM csatlakoztatott monitorok eltárolt szín-értékei (HDR/SDR-fény/ACM)."""
    live = set()
    for d in displays:
        parts = (d.get('monitor_device_id') or '').split('\\')
        if len(parts) > 1:
            live.add(parts[1].upper())
    out = []
    try:
        base = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _MONITOR_DATASTORE)
    except OSError:
        return out
    i = 0
    while True:
        try:
            mon = winreg.EnumKey(base, i)
        except OSError:
            break
        i += 1
        if any(mon.upper().startswith(p) for p in live):
            continue
        try:
            mk = winreg.OpenKey(base, mon)
        except OSError:
            continue
        for v in DATASTORE_COLOR_VALUES:
            try:
                val, _t = winreg.QueryValueEx(mk, v)
                out.append((mon, v, val))
            except OSError:
                pass
    return out


def _night_light_keys():
    out = []
    try:
        base = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _CLOUDSTORE_CURRENT)
    except OSError:
        return out
    i = 0
    while True:
        try:
            n = winreg.EnumKey(base, i)
        except OSError:
            break
        i += 1
        if NIGHT_LIGHT_MARKER in n.lower():
            out.append(n)
    return out


def _color_filter_state():
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _COLOR_FILTER_KEY)
        return int(winreg.QueryValueEx(k, 'Active')[0])
    except (OSError, ValueError, TypeError):
        return None


def factory_reset_plan(run, displays):
    """MIT csinálna a gyári visszaállítás EZEN a gépen - a megerősítő ablak ebből épül,
    és a tényleges futás is ebből dolgozik (ne térhessen el az előnézet és a valóság)."""
    _cdir, lib = list_library()
    added = [p for p in lib if not p['windows']]
    assocs = all_associations()
    plan = {
        'associations': [f"{a['root']} · {a['instance']} · {'HDR' if a['hdr'] else 'SDR'}: "
                         + ', '.join(a['profiles']) for a in assocs],
        'files': [{'file': p['file'], 'desc': p.get('desc') or p['file'], 'kind': p['kind'],
                   'hdr': p['hdr']} for p in added if p['kind'] != 'printer'],
        'printer_files': [{'file': p['file'], 'desc': p.get('desc') or p['file']}
                          for p in added if p['kind'] == 'printer'],
        'kept_windows': [p['file'] for p in lib if p['windows']],
        'registered': [f"{r[0]} RegisteredProfiles\\{r[1]} "
                       f"({'visszaállítás' if r[3] == 'set' else 'törlés'})" for r in _registered_changes()],
        'calibration': display_core.calibration_management(),
        'hdr_on': [d.get('name') for d in displays if (d.get('hdr') or {}).get('enabled')],
        'acm_on': [d.get('name') for d in displays if (d.get('hdr') or {}).get('wcg_enabled')],
        'gamma_modified': [d.get('name') for d in displays
                           if d.get('gamma_ramp') and not d['gamma_ramp'].get('linear')],
        'acm_guard': acm_guard_installed(run),
        'datastore': [f'{m} · {v}={val}' for m, v, val in _datastore_changes(displays)],
        'night_light': bool(_night_light_keys()),
        'color_filter': _color_filter_state(),
    }
    logging.info(f"[COLOR] Gyári visszaállítás előnézete: {len(plan['associations'])} társítás, "
                 f"{len(plan['files'])} hozzáadott profil + {len(plan['printer_files'])} nyomtató-profil, "
                 f"{len(plan['registered'])} regisztráció-eltérés, HDR BE: {plan['hdr_on']}, "
                 f"ACM BE: {plan['acm_on']}, módosított gamma: {plan['gamma_modified']}, "
                 f"ACM-zár: {plan['acm_guard']}, datastore: {len(plan['datastore'])}, "
                 f"éjszakai fény kulcs: {plan['night_light']}, színszűrő: {plan['color_filter']}.")
    return plan


def factory_reset(run, displays, delete_printer_profiles=True, log=None):
    """MINDEN színnel kapcsolatos beállítás visszaállítása a friss Windows-telepítés
    állapotára. Soha nem dob; minden lépés külön try-ban, a hibák a lista végén.
    Visszaad: dict(done=[...], errors=[...])."""
    done, errors = [], []

    def say(m):
        done.append(m)
        logging.info(f"[COLOR-RESET] {m}")
        if log:
            try:
                log(m)
            except Exception as e:
                logging.debug(f"[COLOR-RESET] log callback hiba: {e}")

    def step(title, fn):
        try:
            r = fn()
            if r:
                say(f'{title}: {r}')
        except Exception as e:
            errors.append(f'{title}: {e}')
            logging.error(f"[COLOR-RESET] {title} HIBA: {e}", exc_info=True)

    plan = factory_reset_plan(run, displays)
    logging.warning(f"[COLOR-RESET] GYÁRI VISSZAÁLLÍTÁS INDUL (nyomtató-profilok törlése: "
                    f"{delete_printer_profiles}).")

    # 1) Élő kijelző-állapot: HDR ki, ACM ki (a Windows telepítés utáni állapota).
    def _live():
        out = []
        for d in displays:
            aid, tid = display_core.find_display_target(d['adapter_low'], d['adapter_high'], d['target_id'])
            if aid is None:
                continue
            h = d.get('hdr') or {}
            if h.get('enabled'):
                ok, _s = display_core.set_hdr(aid, tid, False)
                out.append(f"{d.get('name')} HDR ki{'' if ok else ' (NEM sikerült)'}")
                if not ok:
                    errors.append(f"{d.get('name')}: a HDR nem kapcsolható ki")
            h = display_core.read_hdr_state(aid, tid)
            if h.get('wcg_enabled') and not h.get('enabled'):
                ok, _s = display_core.set_acm(aid, tid, False)
                out.append(f"{d.get('name')} ACM ki{'' if ok else ' (NEM sikerült)'}")
        return ', '.join(out) or 'már alapállapotban'
    step('HDR / ACM', _live)

    # 2) ACM-zár eltávolítása (a felhasználó szerint a gyári gomb oldja fel).
    step('ACM-zár', lambda: ('eltávolítva' if remove_acm_guard(run) else 'NEM sikerült eltávolítani')
         if plan['acm_guard'] else '')

    # 3) Minden kijelző-társítás (élő és árva példányok, SDR+HDR, HKCU+HKLM).
    step('Profil-társítások', lambda: f"{len(remove_all_associations('gyári visszaállítás'))} törölve")

    # 4) Hozzáadott profilfájlok (a Windows sajátjai maradnak - az a gyári állapot).
    def _files():
        n, fails = 0, []
        items = plan['files'] + (plan['printer_files'] if delete_printer_profiles else [])
        for p in items:
            ok, msg = delete_profile(p['file'])
            if ok:
                n += 1
            else:
                fails.append(f"{p['file']} ({msg})")
        if fails:
            errors.append('nem törölhető profilfájl: ' + '; '.join(fails))
        kept = '' if delete_printer_profiles or not plan['printer_files'] else \
            f", {len(plan['printer_files'])} nyomtató-profil a döntésed szerint megmaradt"
        return f"{n} fájl törölve{kept}"
    step('Profilfájlok', _files)

    # 5) Regisztrált profilok gyári értékre.
    def _registered():
        n = 0
        for rname, name, cur, action in _registered_changes():
            root = winreg.HKEY_CURRENT_USER if rname == 'HKCU' else winreg.HKEY_LOCAL_MACHINE
            logging.warning(f"[COLOR-RESET] RegisteredProfiles {rname}\\{name} = {cur!r} -> "
                            f"{'gyári' if action == 'set' else 'TÖRLÉS'}")
            k = winreg.CreateKeyEx(root, _ICM_REGISTERED, 0, winreg.KEY_ALL_ACCESS)
            if action == 'set':
                typ, val = REGISTERED_DEFAULTS[name.lower()]
                winreg.SetValueEx(k, name.lower(), 0, typ, val)
            else:
                winreg.DeleteValue(k, name)
            n += 1
        return f'{n} bejegyzés gyári értékre' if n else ''
    step('Regisztrált profilok', _registered)

    # 6) Profil-betöltés engedélyezve + a kalibráló alapértéke + a Windows gamma-betöltője.
    def _calib():
        k = winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, _ICM_CALIB, 0, winreg.KEY_ALL_ACCESS)
        winreg.SetValueEx(k, 'CalibrationManagementEnabled', 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, 'DisplayCalibrator', 0, winreg.REG_SZ, DISPLAY_CALIBRATOR_DEFAULT)
        run(['schtasks', '/change', '/tn', CALIB_LOADER_TASK, '/enable'], timeout=30, ok_codes=(0, 1))
        return f"engedélyezve (előtte: {plan['calibration'] if plan['calibration'] is not None else 'nincs beállítva'})"
    step('Profil-betöltés', _calib)

    # 7) Gamma azonnal lineárisra minden kijelzőn.
    def _gamma():
        out = []
        for d in displays:
            ok, _m = reset_gamma(d.get('gdi_name', ''), 'gyári visszaállítás')
            out.append(f"{d.get('name')}{'' if ok else ' (NEM sikerült)'}")
        return 'lineáris: ' + ', '.join(out) if out else ''
    step('Gamma', _gamma)

    # 8) A NEM csatlakoztatott monitorok eltárolt HDR/SDR/ACM értékei.
    def _datastore():
        n = 0
        for mon, v, val in _datastore_changes(displays):
            logging.warning(f"[COLOR-RESET] MonitorDataStore\\{mon}\\{v} = {val} TÖRLÉSE")
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f'{_MONITOR_DATASTORE}\\{mon}', 0, winreg.KEY_SET_VALUE)
            winreg.DeleteValue(k, v)
            n += 1
        return f'{n} érték törölve (nem csatlakoztatott monitorok)' if n else ''
    step('Monitor-adattár', _datastore)

    # 9) Éjszakai fény + színszűrők (Kisegítő lehetőségek).
    def _night():
        keys = _night_light_keys()
        for n in keys:
            logging.warning(f"[COLOR-RESET] Éjszakai fény beállítás TÖRLÉSE: HKCU\\{_CLOUDSTORE_CURRENT}\\{n}")
            _delete_tree(winreg.HKEY_CURRENT_USER, f'{_CLOUDSTORE_CURRENT}\\{n}')
        return f'{len(keys)} beállítás-kulcs törölve (kijelentkezés után él)' if keys else ''
    step('Éjszakai fény', _night)

    def _filters():
        if _color_filter_state() is None:
            return ''
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _COLOR_FILTER_KEY, 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(k, 'Active', 0, winreg.REG_DWORD, 0)
        return 'kikapcsolva'
    step('Színszűrők', _filters)

    logging.warning(f"[COLOR-RESET] KÉSZ - {len(done)} lépés, {len(errors)} hiba: {errors}")
    return {'done': done, 'errors': errors}


def _delete_tree(root, key):
    try:
        k = winreg.OpenKey(root, key, 0, winreg.KEY_ALL_ACCESS)
    except OSError:
        return
    while True:
        try:
            sub = winreg.EnumKey(k, 0)
        except OSError:
            break
        _delete_tree(root, f'{key}\\{sub}')
    k.Close()
    parent, _, leaf = key.rpartition('\\')
    winreg.DeleteKey(winreg.OpenKey(root, parent, 0, winreg.KEY_ALL_ACCESS), leaf)
