"""Kijelző-kezelés MAGJA: monitor-felderítés, HDR-állapot és -kapcsolás, SDR fehér szint,
EDID-elemzés (luminancia + gamut), ICC-profil olvasás és a Windows színkezelés állapota.

MIÉRT VAN EZ A PROGRAMBAN (explicit felhasználói kérés, 2026-07-30): a Windows 11 saját
felülete ezekhez vagy hiányos, vagy szét van szórva három különböző helyre, és a szerviznek
pont az kell, hogy EGY képernyőn lássa, mi van a kijelzővel: van-e HDR, be van-e kapcsolva,
milyen profil van rátéve, és hogy a színkezelés egyáltalán ép-e.

MINDEN ITT LÉVŐ ADAT ÉLŐBEN ELLENŐRIZVE (2026-07-30, AOC AG276QZD2 QD-OLED, DisplayPort,
RTX 3060) - a hozzátartozó minta a fejlesztői gépről:
  - QueryDisplayConfig  -> 1 aktív út, GDI név '\\\\.\\DISPLAY1', barátságos név 'AG276QZD2'
  - GET_ADVANCED_COLOR_INFO(9)   -> value=0x1: HDR támogatott, kikapcsolva, 8 bit, RGB
  - GET_ADVANCED_COLOR_INFO_2(15)-> hdrSupported+wcgSupported, activeColorMode=0 (SDR)
  - SET_HDR_STATE(16)            -> bekapcsolva: 10 bit, activeColorMode=2 (HDR), tisztán vissza
  - GET_SDR_WHITE_LEVEL(11)      -> SDR-ben 1000 (=80 nit), HDR-ben 3000 (=240 nit)
  - EDID (384 bájt, CTA-861)     -> csúcs 1015 nit, teljes képmezős 254 nit, fekete 0.0006 nit
  - EDID színpontok              -> R .686/.304  G .240/.712  B .144/.058  W .3135/.3291

ICC-PROFIL TÁRSÍTÁS (2026-07-31): a társítás KÖZVETLEN REGISTRY-ÍRÁSSAL megy, mert az
erre való mscms API-k ezen a Windowson bizonyítottan nem csinálnak semmit - a mérési
jegyzőkönyv az associate_profile() docstringjében. Az InstallColorProfileW ezzel szemben
működik, azt használjuk a profil telepítésére.

AMI TOVÁBBRA SINCS ITT: ICC-profil generálás és a vezetett nit-teszt.
"""

# === AUTO-IMPORTS ===
import os
import struct
import ctypes
import logging
import winreg
from app import win32 as w32
# === /AUTO-IMPORTS ===


_user32 = ctypes.windll.user32
_mscms = ctypes.WinDLL('mscms', use_last_error=True)

# A színkezelés registry-gyökere (ugyanaz az ág, amit a colorprofile_core takarít).
_ICM_BASE = r'SOFTWARE\Microsoft\Windows NT\CurrentVersion\ICM'
_ICM_ASSOC = _ICM_BASE + r'\ProfileAssociations\Display'
# A "Windows-kijelzőkalibráció használata" kapcsoló a Calibration ALKULCSBAN ül, nem az ICM
# gyökerében - ezt a különbséget az első kiadásom elrontotta, és pont a legfontosabb esetet
# tette vakká: az érték hiánya és a 0 érték ugyanúgy "nincs beállítva"-ként jött vissza.
# A colorprofile_core (Build 235 óta) is ezt az utat írja.
_ICM_CALIB = _ICM_BASE + r'\Calibration'
_ICM_REGISTERED = _ICM_BASE + r'\RegisteredProfiles'
_EDID_ROOT = r'SYSTEM\CurrentControlSet\Enum\DISPLAY'

# A DisplayConfig SDR-fehérszint nyers értéke ezzel a képlettel nit: raw / 1000 * 80.
# (A Windows 1000-et ad alapon, ami a szabványos 80 nites SDR fehér.)
SDR_WHITE_LEVEL_UNIT = 80.0 / 1000.0


# ============================================================================
# Monitorok felderítése
# ============================================================================

def _query_display_paths():
    """Az aktív megjelenítési utak lekérése. Visszaad: [(path, mode-tömb)] vagy [] hibánál."""
    n_path = ctypes.wintypes.UINT()
    n_mode = ctypes.wintypes.UINT()
    rc = _user32.GetDisplayConfigBufferSizes(w32.QDC_ONLY_ACTIVE_PATHS,
                                             ctypes.byref(n_path), ctypes.byref(n_mode))
    if rc != 0:
        logging.error(f"[DISPLAY] GetDisplayConfigBufferSizes hiba: {rc}")
        return [], []
    paths = (w32._DISPLAYCONFIG_PATH_INFO * n_path.value)()
    modes = (w32._DISPLAYCONFIG_MODE_INFO * n_mode.value)()
    rc = _user32.QueryDisplayConfig(w32.QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_path), paths,
                                    ctypes.byref(n_mode), modes, None)
    if rc != 0:
        logging.error(f"[DISPLAY] QueryDisplayConfig hiba: {rc}")
        return [], []
    return list(paths)[:n_path.value], list(modes)[:n_mode.value]


def _device_info(struct_obj, info_type, adapter_id, target_id):
    """Egy DisplayConfigGetDeviceInfo hívás. Visszaad: a visszatérési kód (0 = OK)."""
    struct_obj.header.type = info_type
    struct_obj.header.size = ctypes.sizeof(struct_obj)
    struct_obj.header.adapterId = adapter_id
    struct_obj.header.id = target_id
    return _user32.DisplayConfigGetDeviceInfo(ctypes.byref(struct_obj))


def read_hdr_state(adapter_id, target_id):
    """Egy kijelző HDR/színállapota. Két API-t kérdezünk: a RÉGI (9) minden Win10/11-en
    megvan, az ÚJ (15) csak Win11 24H2-től, viszont sokkal többet mond (külön HDR és WCG
    támogatás/engedélyezés, aktív színmód). Amit az új tud, azt onnan vesszük, a régi a
    tartalék - így egy régebbi Windowson sem marad üres a nézet.
    Visszaad: dict (soha nem None; hibánál 'supported': False)."""
    out = {'supported': False, 'enabled': False, 'bits': None, 'encoding': None,
           'mode': None, 'wcg_supported': False, 'wcg_enabled': False,
           'limited_by_policy': False, 'api': None}
    aci = w32._DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO()
    if _device_info(aci, w32.DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO,
                    adapter_id, target_id) == 0:
        v = aci.value
        out.update(supported=bool(v & 0x1), enabled=bool(v & 0x2),
                   limited_by_policy=bool(v & 0x8),
                   bits=aci.bitsPerColorChannel,
                   encoding=w32.DISPLAY_COLOR_ENCODING.get(aci.colorEncoding, str(aci.colorEncoding)),
                   api='legacy')
    aci2 = w32._DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO_2()
    if _device_info(aci2, w32.DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO_2,
                    adapter_id, target_id) == 0:
        v = aci2.value
        out.update(supported=bool(v & 0x10) or bool(v & 0x1),
                   enabled=bool(v & 0x20) or bool(v & 0x2),
                   limited_by_policy=bool(v & 0x8),
                   wcg_supported=bool(v & 0x40), wcg_enabled=bool(v & 0x80),
                   bits=aci2.bitsPerColorChannel,
                   encoding=w32.DISPLAY_COLOR_ENCODING.get(aci2.colorEncoding, str(aci2.colorEncoding)),
                   mode=w32.DISPLAY_ADVANCED_COLOR_MODE.get(aci2.activeColorMode,
                                                            str(aci2.activeColorMode)),
                   api='win11')
    return out


def read_sdr_white_level(adapter_id, target_id):
    """Az "SDR-tartalom fényereje" csúszka aktuális értéke nitben (None, ha nem elérhető).
    HDR-ben ez mondja meg, milyen fényesen jelenik meg a hagyományos (SDR) tartalom."""
    swl = w32._DISPLAYCONFIG_SDR_WHITE_LEVEL()
    if _device_info(swl, w32.DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL,
                    adapter_id, target_id) != 0:
        return None
    return round(swl.SDRWhiteLevel * SDR_WHITE_LEVEL_UNIT, 1)


def set_hdr(adapter_id, target_id, enable):
    """HDR be-/kikapcsolása egy kijelzőn. Először az ÚJ (Win11 24H2+) SET_HDR_STATE(16)
    hívást próbáljuk, és csak ha az nem vezet eredményre, akkor a régi
    SET_ADVANCED_COLOR_STATE(10)-et - a régi API a WCG-t is bekapcsolhatja HDR helyett,
    ezért nem az az elsődleges. A siker mércéje NEM a visszatérési kód, hanem hogy a
    VISSZAOLVASOTT állapot tényleg megváltozott-e (élőben mérve: a rossz struktúraméret
    0-t ad vissza, miközben nem történik semmi).
    Visszaad: (sikerult, uj_allapot_dict)."""
    want = bool(enable)
    logging.warning(f"[DISPLAY] HDR {'BEkapcsolása' if want else 'KIkapcsolása'} "
                    f"(adapter={adapter_id.LowPart}:{adapter_id.HighPart}, target={target_id})...")
    s = w32._DISPLAYCONFIG_SET_HDR_STATE()
    s.header.type = w32.DISPLAYCONFIG_DEVICE_INFO_SET_HDR_STATE
    s.header.size = ctypes.sizeof(s)
    s.header.adapterId = adapter_id
    s.header.id = target_id
    s.value = 1 if want else 0
    rc_new = _user32.DisplayConfigSetDeviceInfo(ctypes.byref(s))
    state = read_hdr_state(adapter_id, target_id)
    logging.info(f"[DISPLAY] SET_HDR_STATE(16) rc={rc_new} -> HDR most: "
                 f"{'BE' if state['enabled'] else 'KI'}")
    if state['enabled'] == want:
        return True, state

    s2 = w32._DISPLAYCONFIG_SET_ADVANCED_COLOR_STATE()
    s2.header.type = w32.DISPLAYCONFIG_DEVICE_INFO_SET_ADVANCED_COLOR_STATE
    s2.header.size = ctypes.sizeof(s2)
    s2.header.adapterId = adapter_id
    s2.header.id = target_id
    s2.value = 1 if want else 0
    rc_old = _user32.DisplayConfigSetDeviceInfo(ctypes.byref(s2))
    state = read_hdr_state(adapter_id, target_id)
    logging.info(f"[DISPLAY] tartalék SET_ADVANCED_COLOR_STATE(10) rc={rc_old} -> HDR most: "
                 f"{'BE' if state['enabled'] else 'KI'}")
    if state['enabled'] != want:
        logging.error(f"[DISPLAY] A HDR átkapcsolása NEM sikerült (kért: {want}, "
                      f"tényleges: {state['enabled']}; rc_uj={rc_new}, rc_regi={rc_old}).")
    return state['enabled'] == want, state


# A gamma-rámpa "lineárisnak" tekintésének tűréshatára (65535-ös skálán). Nem 0, mert a
# Windows/illesztőprogram kerekítése miatt egy érintetlen rámpa is szórhat pár egységet.
# 64 = a teljes skála ~0.1%-a: ennél nagyobb eltérés már szándékos korrekció.
GAMMA_LINEAR_TOLERANCE = 64
# Hány pontot adunk vissza a görbéből a felületnek (a 256-ból mintavételezve). 17 pont
# elég egy felismerhető görbe kirajzolásához, és nem fújja fel a JS-nek küldött adatot.
GAMMA_CURVE_POINTS = 17


def read_gamma_ramp(gdi_name):
    """A kijelzőre TÉNYLEGESEN betöltött gamma-rámpa (a videokártya LUT-ja) beolvasása.

    EZ A LEGKÖZVETLENEBB TÉNY az egész színkezelésből: hiába van (vagy nincs) profil
    társítva, a képet végső soron ez a tábla módosítja. Lineáris rámpa = a jel érintetlenül
    megy a panelre; módosított rámpa = valami (ICC-profil VCGT tagja, kalibráló program,
    gyártói eszköz) korrekciót tölt be. A nézetben ezért külön kiírjuk.

    A DC-t a konkrét kijelzőre nyitjuk (CreateDCW a GDI-névvel), nem a teljes asztalra:
    több monitornál külön-külön más rámpa lehet betöltve.
    Visszaad: dict(linear, max_deviation, curve) vagy None, ha nem olvasható."""
    hdc = ctypes.windll.gdi32.CreateDCW(None, gdi_name, None, None) if gdi_name else 0
    own = bool(hdc)
    if not hdc:      # tartalék: az elsődleges kijelző asztali DC-je
        hdc = ctypes.windll.user32.GetDC(0)
        if not hdc:
            logging.warning(f"[DISPLAY] Nem sikerült DC-t nyitni a gamma-rámpához ({gdi_name}).")
            return None
    ramp = (ctypes.c_ushort * 768)()
    try:
        ok = ctypes.windll.gdi32.GetDeviceGammaRamp(hdc, ctypes.byref(ramp))
    finally:
        if own:
            ctypes.windll.gdi32.DeleteDC(hdc)
        else:
            ctypes.windll.user32.ReleaseDC(0, hdc)
    if not ok:
        logging.info(f"[DISPLAY] GetDeviceGammaRamp nem támogatott ezen a kijelzőn ({gdi_name}).")
        return None
    ideal = [(i * 65535) // 255 for i in range(256)]
    max_dev = 0
    for ch in range(3):
        for i in range(256):
            d = abs(ramp[ch * 256 + i] - ideal[i])
            if d > max_dev:
                max_dev = d
    step = 255 / (GAMMA_CURVE_POINTS - 1)
    curve = {}
    for ch, name in enumerate(('r', 'g', 'b')):
        curve[name] = [round(ramp[ch * 256 + min(255, int(round(k * step)))] / 65535.0, 4)
                       for k in range(GAMMA_CURVE_POINTS)]
    linear = max_dev <= GAMMA_LINEAR_TOLERANCE
    logging.info(f"[DISPLAY] Gamma-rámpa ({gdi_name}): "
                 f"{'LINEÁRIS' if linear else 'MÓDOSÍTOTT'}, "
                 f"max eltérés a lineáristól {max_dev}/65535 "
                 f"(tűrés {GAMMA_LINEAR_TOLERANCE}).")
    return {'linear': linear, 'max_deviation': max_dev,
            'tolerance': GAMMA_LINEAR_TOLERANCE, 'curve': curve}


def _gdi_devices():
    """A rendszer GDI kijelző-nevei (\\\\.\\DISPLAY1, ...) és a rajtuk lévő monitor neve."""
    out = {}
    i = 0
    while True:
        d = w32._DISPLAY_DEVICEW()
        d.cb = ctypes.sizeof(d)
        if not _user32.EnumDisplayDevicesW(None, i, ctypes.byref(d), 0):
            break
        i += 1
        if not (d.StateFlags & w32.DISPLAY_DEVICE_ATTACHED_TO_DESKTOP):
            continue
        mon = w32._DISPLAY_DEVICEW()
        mon.cb = ctypes.sizeof(mon)
        mon_id, mon_name = '', ''
        if _user32.EnumDisplayDevicesW(d.DeviceName, 0, ctypes.byref(mon), 0):
            mon_id = mon.DeviceID
            mon_name = mon.DeviceString
        out[d.DeviceName] = {
            'adapter': d.DeviceString,
            'primary': bool(d.StateFlags & w32.DISPLAY_DEVICE_PRIMARY_DEVICE),
            'monitor_device_id': mon_id,
            # A MONITOR illesztőprogramjának neve ("Generic PnP Monitor" vs. a gyári modellnév).
            # Ez dönti el, hogy VAN-E egyáltalán gyári ICC-profil esély ezen a gépen: a gyári
            # monitor-INF telepít és társít egy gyári profilt, a generikus monitor.inf nem.
            # Enélkül a "miért nem olyan a kép, mint gyárilag" kérdés megválaszolhatatlan.
            'monitor_driver': mon_name,
        }
    return out


def enumerate_displays():
    """A csatlakoztatott kijelzők teljes állapota. Ez a nézet fő adatforrása.
    Visszaad: lista dict-ekből; hibánál üres lista (a nézet ezt üzenetként mutatja)."""
    paths, _modes = _query_display_paths()
    gdi = _gdi_devices()
    edids = collect_edids()
    displays = []
    for idx, p in enumerate(paths):
        src = w32._DISPLAYCONFIG_SOURCE_DEVICE_NAME()
        gdi_name = ''
        if _device_info(src, w32.DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME,
                        p.sourceInfo.adapterId, p.sourceInfo.id) == 0:
            gdi_name = src.viewGdiDeviceName
        tgt = w32._DISPLAYCONFIG_TARGET_DEVICE_NAME()
        friendly, dev_path, tech = '', '', None
        if _device_info(tgt, w32.DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME,
                        p.targetInfo.adapterId, p.targetInfo.id) == 0:
            friendly = tgt.monitorFriendlyDeviceName
            dev_path = tgt.monitorDevicePath
            tech = tgt.outputTechnology
        hdr_state = read_hdr_state(p.targetInfo.adapterId, p.targetInfo.id)
        rr = p.targetInfo.refreshRate
        info = gdi.get(gdi_name, {})
        edid = match_edid(dev_path, edids)
        displays.append({
            'index': idx,
            'gdi_name': gdi_name,
            'name': friendly or info.get('adapter') or gdi_name or f'Kijelző {idx + 1}',
            'device_path': dev_path,
            'adapter': info.get('adapter', ''),
            'monitor_driver': info.get('monitor_driver', ''),
            'monitor_device_id': info.get('monitor_device_id', ''),
            'icc': device_profiles(info.get('monitor_device_id', '')),
            'generic_monitor': 'generic' in (info.get('monitor_driver', '') or '').lower(),
            'primary': info.get('primary', False),
            'connection': w32.DISPLAY_OUTPUT_TECHNOLOGY.get(tech, f'0x{tech:x}' if tech else '?'),
            'refresh_hz': round(rr.Numerator / rr.Denominator, 2) if rr.Denominator else None,
            'hdr': hdr_state,
            'sdr_white_nits': read_sdr_white_level(p.targetInfo.adapterId, p.targetInfo.id),
            'gamma_ramp': read_gamma_ramp(gdi_name),
            'edid': edid,
            # a kapcsoláshoz kell - a nézet ezt küldi vissza a set_hdr híváskor
            'adapter_low': p.targetInfo.adapterId.LowPart,
            'adapter_high': p.targetInfo.adapterId.HighPart,
            'target_id': p.targetInfo.id,
        })
    logging.info(f"[DISPLAY] {len(displays)} aktív kijelző: "
                 + '; '.join(f"{d['name']} ({d['connection']}, "
                             f"HDR {'BE' if d['hdr']['enabled'] else ('elérhető' if d['hdr']['supported'] else 'nincs')})"
                             for d in displays))
    return displays


def find_display_target(adapter_low, adapter_high, target_id):
    """A nézetből visszakapott azonosítókból a kapcsoláshoz kellő LUID+target előállítása.
    KÜLÖN függvény, mert a JS oldalról érkező értékek nem struktúrák - és mert így a
    kapcsolás mindig FRISSEN lekérdezett utak közül választ (a kijelző-elrendezés a nézet
    megnyitása óta változhatott)."""
    paths, _ = _query_display_paths()
    for p in paths:
        if (p.targetInfo.adapterId.LowPart == adapter_low
                and p.targetInfo.adapterId.HighPart == adapter_high
                and p.targetInfo.id == target_id):
            return p.targetInfo.adapterId, p.targetInfo.id
    logging.warning(f"[DISPLAY] A kért kijelző (adapter={adapter_low}:{adapter_high}, "
                    f"target={target_id}) már nincs az aktív utak között - "
                    f"a kijelző-elrendezés közben megváltozhatott.")
    return None, None


# ============================================================================
# EDID - a monitor saját adatlapja (luminancia + gamut)
# ============================================================================

def _edid_luminance(raw):
    """A CTA-861 HDR Static Metadata blokkból a fényerő-adatok.
    A kódolás a szabvány szerint: nit = 50 * 2^(érték/32); a minimum ebből százalékosan
    származik. Visszaad: dict vagy None, ha nincs ilyen blokk (nem HDR-képes EDID)."""
    if len(raw) < 256 or raw[128] != 0x02:
        return None
    cta = raw[128:256]
    end = cta[2] if cta[2] > 4 else 128     # a DTD-k kezdete = a data block gyűjtemény vége
    idx = 4
    while idx < end and idx < len(cta):
        b0 = cta[idx]
        tag, ln = (b0 >> 5) & 0x7, b0 & 0x1F
        body = cta[idx + 1: idx + 1 + ln]
        if tag == 7 and body and body[0] == 0x06 and len(body) >= 3:   # extended tag 6 = HDR static
            eotf = body[1]
            out = {
                'eotf_sdr': bool(eotf & 1), 'eotf_hdr_gamma': bool(eotf & 2),
                'eotf_pq': bool(eotf & 4), 'eotf_hlg': bool(eotf & 8),
                'peak_nits': None, 'frame_avg_nits': None, 'min_nits': None,
            }
            if len(body) > 3:
                out['peak_nits'] = round(50 * (2 ** (body[3] / 32.0)))
            if len(body) > 4:
                out['frame_avg_nits'] = round(50 * (2 ** (body[4] / 32.0)))
            if len(body) > 5 and out['peak_nits']:
                out['min_nits'] = round(out['peak_nits'] * ((body[5] / 255.0) ** 2) / 100.0, 4)
            return out
        idx += 1 + ln
    return None


def _edid_chromaticity(raw):
    """Az EDID színpontjai (a monitor VALÓDI gamutja) + a névleges gamma.
    A 10 bites értékek 2-2 bitje két közös bájtban (25-26) ül, a felső 8 bit külön -
    ezt a szétszórt kódolást könnyű elrontani, ezért van külön függvényben."""
    if len(raw) < 38:
        return None
    lo1, lo2 = raw[25], raw[26]
    q = lambda hi, sh, src: (((src >> sh) & 3) | (hi << 2)) / 1024.0
    out = {
        'red':   (q(raw[27], 6, lo1), q(raw[28], 4, lo1)),
        'green': (q(raw[29], 2, lo1), q(raw[30], 0, lo1)),
        'blue':  (q(raw[31], 6, lo2), q(raw[32], 4, lo2)),
        'white': (q(raw[33], 2, lo2), q(raw[34], 0, lo2)),
    }
    out = {k: (round(v[0], 4), round(v[1], 4)) for k, v in out.items()}
    out['gamma'] = round((raw[23] + 100) / 100.0, 2) if raw[23] != 0xFF else None
    return out


def _edid_name(raw):
    """A monitor neve az EDID leíró blokkjaiból (0xFC = Monitor Name)."""
    for off in range(54, min(126, len(raw) - 18), 18):
        if raw[off:off + 3] == b'\x00\x00\x00' and raw[off + 3] == 0xFC:
            return raw[off + 5:off + 18].decode('ascii', 'ignore').strip().strip('\n').strip()
    return ''


def parse_edid(raw):
    """Egy nyers EDID feldolgozása. Visszaad: dict, vagy None ha értelmezhetetlen."""
    if not raw or len(raw) < 128:
        return None
    try:
        return {
            'name': _edid_name(raw),
            'bytes': len(raw),
            'chroma': _edid_chromaticity(raw),
            'luminance': _edid_luminance(raw),
        }
    except Exception as e:
        logging.warning(f"[DISPLAY] EDID feldolgozási hiba: {e}")
        return None


def collect_edids():
    """Minden monitor EDID-je a registryből: {instance_kulcs: parse_edid(...)}.
    FONTOS: egy monitorhoz TÖBB példány is tartozhat (élőben mérve 3 db ugyanahhoz az
    AOC-hoz), és közülük csak EGYNEK van 384 bájtos, CTA-kiterjesztéses EDID-je - a
    többi 128 bájtos csonk, HDR-adat nélkül. Ezért a hosszabb EDID mindig nyer."""
    out = {}
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _EDID_ROOT)
    except OSError as e:
        logging.warning(f"[DISPLAY] A DISPLAY enum ág nem olvasható: {e}")
        return out
    i = 0
    while True:
        try:
            mon = winreg.EnumKey(root, i)
        except OSError:
            break
        i += 1
        try:
            mk = winreg.OpenKey(root, mon)
        except OSError:
            continue
        j = 0
        while True:
            try:
                inst = winreg.EnumKey(mk, j)
            except OSError:
                break
            j += 1
            try:
                dk = winreg.OpenKey(mk, inst + r'\Device Parameters')
                raw = winreg.QueryValueEx(dk, 'EDID')[0]
            except OSError:
                continue
            parsed = parse_edid(bytes(raw))
            if not parsed:
                continue
            key = f'{mon}\\{inst}'
            parsed['instance'] = key
            prev = out.get(key)
            if not prev or parsed['bytes'] > prev['bytes']:
                out[key] = parsed
    logging.debug(f"[DISPLAY] {len(out)} EDID beolvasva a registryből.")
    return out


def match_edid(device_path, edids):
    """A DisplayConfig eszközútvonalához (\\\\?\\DISPLAY#AOCA610#5&...&UID28931#{guid})
    tartozó EDID megkeresése. Az útvonal középső két tagja a registry-beli monitor- és
    példánykulcs, csak '#' helyett '\\' elválasztóval. Ha a pontos példány nem található,
    ugyanannak a monitor-modellnek a LEGBŐVEBB EDID-jével esünk vissza (a csonka
    példányokon nincs HDR-adat, lásd collect_edids)."""
    if not device_path or not edids:
        return None
    parts = device_path.split('#')
    if len(parts) >= 3:
        exact = f'{parts[1]}\\{parts[2]}'
        if exact in edids:
            return edids[exact]
        model = parts[1]
        same = [v for k, v in edids.items() if k.split('\\')[0] == model]
        if same:
            best = max(same, key=lambda e: e['bytes'])
            logging.debug(f"[DISPLAY] EDID: a pontos példány ({exact}) nincs meg, "
                          f"a modell legbővebb EDID-je használva ({best['instance']}, "
                          f"{best['bytes']} bájt).")
            return best
    return None


# ============================================================================
# ICC-profilok
# ============================================================================

def color_directory():
    """A Windows színprofil-mappája (rendszerint …\\system32\\spool\\drivers\\color)."""
    buf = ctypes.create_unicode_buffer(260)
    size = ctypes.wintypes.DWORD(ctypes.sizeof(buf))
    if _mscms.GetColorDirectoryW(None, buf, ctypes.byref(size)):
        return buf.value
    logging.warning("[DISPLAY] GetColorDirectoryW nem adott vissza mappát - a beépített út lesz.")
    return os.path.join(os.environ.get('SystemRoot', r'C:\Windows'),
                        'System32', 'spool', 'drivers', 'color')


_ICC_CLASS = {b'mntr': 'Monitor', b'prtr': 'Nyomtató', b'scnr': 'Szkenner',
              b'spac': 'Színtér', b'link': 'Eszközlánc', b'abst': 'Absztrakt',
              b'nmcl': 'Névszerinti'}
_ICC_INTENT = {0: 'Perceptuális', 1: 'Relatív kolorimetriás', 2: 'Telítettség',
               3: 'Abszolút kolorimetriás'}


def parse_icc(path):
    """Egy ICC/ICM profil fejlécének és fontos tagjeinek kiolvasása.

    Amit kiszedünk, és miért pont azt: az osztály és a színtér mondja meg, hogy egyáltalán
    MONITOR-profilról van-e szó (a mappában nyomtató-profilok is ülnek); a TRC-görbe a
    gamma (a "2.2 kell-e" kérdés innen dől el); az rXYZ/gXYZ/bXYZ a profil szerinti gamut;
    az MHC2 tag jelenléte pedig azt jelzi, hogy ez egy Windows HDR-kalibrációs profil.
    Visszaad: dict (a 'hiba' kulcs jelzi, ha nem sikerült)."""
    try:
        with open(path, 'rb') as fh:
            data = fh.read()
        if len(data) < 132:
            return {'file': os.path.basename(path), 'hiba': 'túl rövid fájl'}
        ver = struct.unpack('>I', data[8:12])[0]
        cls, space, pcs = data[12:16], data[16:20], data[20:24]
        intent = struct.unpack('>I', data[64:68])[0]
        n_tags = struct.unpack('>I', data[128:132])[0]
        out = {
            'file': os.path.basename(path), 'path': path, 'size': len(data),
            'version': f'{(ver >> 24) & 0xFF}.{(ver >> 20) & 0xF}.{(ver >> 16) & 0xF}',
            'class': _ICC_CLASS.get(cls, cls.decode('ascii', 'replace')),
            'is_monitor': cls == b'mntr',
            'space': space.decode('ascii', 'replace').strip(),
            'pcs': pcs.decode('ascii', 'replace').strip(),
            'intent': _ICC_INTENT.get(intent, str(intent)),
            'desc': '', 'gamma': None, 'tags': [], 'has_vcgt': False, 'has_mhc2': False,
            'primaries': {},
        }
        tags = []
        for i in range(min(n_tags, 200)):
            off = 132 + i * 12
            if off + 12 > len(data):
                break
            sig, o, sz = struct.unpack('>4sII', data[off:off + 12])
            tags.append((sig.decode('ascii', 'replace'), o, sz))
        out['tags'] = [t[0] for t in tags]
        out['has_vcgt'] = 'vcgt' in out['tags']
        out['has_mhc2'] = 'MHC2' in out['tags']
        for sig, o, sz in tags:
            if o + sz > len(data) or sz < 8:
                continue
            body = data[o:o + sz]
            if sig == 'desc' and not out['desc']:
                if body[:4] == b'desc':
                    ln = struct.unpack('>I', body[8:12])[0]
                    out['desc'] = body[12:12 + max(ln - 1, 0)].decode('ascii', 'replace').strip('\x00')
                elif body[:4] == b'mluc' and sz >= 28:
                    ln, ofs = struct.unpack('>II', body[20:28])
                    if ofs + ln <= sz:
                        out['desc'] = body[ofs:ofs + ln].decode('utf-16-be', 'replace').strip('\x00')
            elif sig == 'rTRC' and out['gamma'] is None:
                if body[:4] == b'curv' and sz >= 12:
                    cnt = struct.unpack('>I', body[8:12])[0]
                    if cnt == 0:
                        out['gamma'] = 1.0
                    elif cnt == 1 and sz >= 14:
                        out['gamma'] = round(struct.unpack('>H', body[12:14])[0] / 256.0, 3)
                    else:
                        out['gamma'] = 'táblázat'
                elif body[:4] == b'para':
                    out['gamma'] = 'paraméteres'
            elif sig in ('rXYZ', 'gXYZ', 'bXYZ', 'wtpt') and sz >= 20:
                x, y, z = struct.unpack('>iii', body[8:20])
                out['primaries'][sig] = (round(x / 65536.0, 4), round(y / 65536.0, 4),
                                         round(z / 65536.0, 4))
        if not out['desc']:
            out['desc'] = out['file']
        return out
    except Exception as e:
        logging.warning(f"[DISPLAY] ICC olvasási hiba ({path}): {e}")
        return {'file': os.path.basename(path), 'path': path, 'hiba': str(e)}


def list_color_profiles():
    """A gépre telepített összes ICC/ICM profil, feldolgozva. A monitor-profilok kerülnek
    előre (a nyomtató-profilokhoz ebben a nézetben nincs dolgunk, de a listából ne
    tűnjenek el - a szerviznek az is információ, mi van a gépen)."""
    cdir = color_directory()
    profiles = []
    try:
        names = sorted(os.listdir(cdir))
    except OSError as e:
        logging.error(f"[DISPLAY] A színprofil-mappa nem olvasható ({cdir}): {e}")
        return cdir, []
    for fn in names:
        if fn.lower().endswith(('.icc', '.icm')):
            profiles.append(parse_icc(os.path.join(cdir, fn)))
    profiles.sort(key=lambda p: (not p.get('is_monitor'), p.get('file', '').lower()))
    mon = sum(1 for p in profiles if p.get('is_monitor'))
    logging.info(f"[DISPLAY] {len(profiles)} színprofil a(z) {cdir} mappában "
                 f"({mon} monitor-profil, {sum(1 for p in profiles if p.get('has_mhc2'))} HDR-kalibrációs).")
    return cdir, profiles


# ============================================================================
# A Windows színkezelésének állapota
# ============================================================================

def _read_assoc_branch(root):
    """A ProfileAssociations\\Display ág beolvasása egy registry-gyökérből.
    A profil maga ÉRTÉKKÉNT ül a {osztály-GUID}\\NNNN alkulcsokban - a puszta kulcsok
    üresek is lehetnek, ezért az ÉRTÉKEKET számoljuk, nem a kulcsokat (ez a
    colorprofile_core-ban már megtanult lecke, itt is érvényes)."""
    found = []
    try:
        base = winreg.OpenKey(root, _ICM_ASSOC)
    except OSError:
        return found
    i = 0
    while True:
        try:
            g = winreg.EnumKey(base, i)
        except OSError:
            break
        i += 1
        try:
            gk = winreg.OpenKey(base, g)
        except OSError:
            continue
        j = 0
        while True:
            try:
                sub = winreg.EnumKey(gk, j)
            except OSError:
                break
            j += 1
            try:
                sk = winreg.OpenKey(gk, sub)
            except OSError:
                continue
            m = 0
            while True:
                try:
                    name, val, _t = winreg.EnumValue(sk, m)
                except OSError:
                    break
                m += 1
                vals = val if isinstance(val, list) else [val]
                for v in vals:
                    if isinstance(v, str) and v.strip():
                        found.append({'guid': g, 'sub': sub, 'value_name': name, 'profile': v})
    return found


def monitor_assoc_key(monitor_device_id):
    """A monitor eszköz-azonosítójából a hozzá tartozó ICM registry-alkulcs útja.

    A DeviceID alakja:  MONITOR\\AOCA610\\{4d36e96e-e325-11ce-bfc1-08002be10318}\\0003
                                          ^^^^ osztály-GUID              ^^^^ példány
    és a társítás pontosan ezen a két tagon ül:
        ...\\ICM\\ProfileAssociations\\Display\\{osztály-GUID}\\{példány}
    Visszaad: a relatív kulcsút, vagy None, ha az azonosító nem értelmezhető."""
    if not monitor_device_id:
        return None
    parts = monitor_device_id.split('\\')
    if len(parts) < 4 or not parts[2].startswith('{'):
        logging.debug(f"[DISPLAY] Nem értelmezhető monitor-azonosító: {monitor_device_id!r}")
        return None
    return f"{_ICM_ASSOC}\\{parts[2]}\\{parts[3]}"


def device_profiles(monitor_device_id):
    """Az EHHEZ A MONITORHOZ társított ICC-profilok. Ez válaszolja meg a "melyik profil van
    most használatban?" kérdést - a globális profile_associations() az egész gépről szól,
    ez viszont egy konkrét kijelzőről.

    Visszaad: {'user': [...], 'system': [...], 'active': <a hatályos profil neve vagy None>}
    Az 'active' a felhasználói szintet részesíti előnyben, mert a Windows is azt használja,
    ha a "Use my settings for this device" be van kapcsolva."""
    key = monitor_assoc_key(monitor_device_id)
    out = {'user': [], 'system': [], 'active': None}
    if not key:
        return out
    for root, name in ((winreg.HKEY_CURRENT_USER, 'user'), (winreg.HKEY_LOCAL_MACHINE, 'system')):
        try:
            k = winreg.OpenKey(root, key)
        except OSError:
            continue
        i = 0
        while True:
            try:
                vname, val, _t = winreg.EnumValue(k, i)
            except OSError:
                break
            i += 1
            if vname.lower() != 'icmprofile':
                continue
            for v in (val if isinstance(val, list) else [val]):
                if isinstance(v, str) and v.strip():
                    out[name].append(v.strip())
    out['active'] = (out['user'] or out['system'] or [None])[0]
    return out


def associate_profile(monitor_device_id, profile_name, per_user=True):
    """Egy ICC-profil TÁRSÍTÁSA a monitorhoz - közvetlen registry-írással.

    MIÉRT NEM AZ API-VAL (terepen mérve 2026-07-31, ez a modul legdrágább tanulsága):
    a `WcsAssociateColorProfileWithDevice` ezen a Windows 11-en TRUE-t ad vissza, miközben
    SEMMIT nem ír sehova - rendszergazdaként is, felhasználói és rendszerszintű hatókörrel
    is; a `WcsDisassociate...` utána ERROR_PROFILE_NOT_ASSOCIATED_WITH_DEVICE-szal bukik, a
    régi `AssociateColorProfileWithDeviceW` pedig FALSE-t ad hibakód nélkül. A közvetlen
    registry-írás viszont MŰKÖDIK, és a Windows saját Színkezelés vezérlőpultja azonnal
    látja is (élőben ellenőrizve: "Use my settings for this device" bepipálva, a profil
    "(default)" jelöléssel). Ha valaki egyszer visszaírná API-hívásra, azt előbb pontosan
    ezzel a próbával kell igazolni: társítás után a vezérlőpultnak MUTATNIA kell.

    Az érték neve `ICMProfile` (REG_MULTI_SZ), és a `UsePerUserProfiles`=1 az, ami a
    vezérlőpult "Use my settings for this device" pipájának felel meg.
    Visszaad: (sikerult, uzenet)."""
    key = monitor_assoc_key(monitor_device_id)
    if not key:
        return False, 'A monitor azonosítója nem értelmezhető.'
    root = winreg.HKEY_CURRENT_USER if per_user else winreg.HKEY_LOCAL_MACHINE
    scope = 'felhasználói' if per_user else 'rendszerszintű'
    existing = device_profiles(monitor_device_id)
    current = existing['user'] if per_user else existing['system']
    logging.warning(f"[DISPLAY] ICC-profil TÁRSÍTÁSA ({scope}): '{profile_name}' -> "
                    f"{monitor_device_id} (eddigi: {current or 'nincs'})")
    profiles = [profile_name] + [p for p in current if p.lower() != profile_name.lower()]
    try:
        k = winreg.CreateKeyEx(root, key, 0, winreg.KEY_ALL_ACCESS)
        winreg.SetValueEx(k, 'ICMProfile', 0, winreg.REG_MULTI_SZ, profiles)
        if per_user:
            winreg.SetValueEx(k, 'UsePerUserProfiles', 0, winreg.REG_DWORD, 1)
    except OSError as e:
        logging.error(f"[DISPLAY] A társítás nem sikerült: {e}")
        return False, str(e)
    logging.info(f"[DISPLAY] Társítva. A kijelző profilsora most: {profiles}")
    return True, ''


def disassociate_profile(monitor_device_id, profile_name):
    """Egy ICC-profil társításának MEGSZÜNTETÉSE a monitorról (mindkét hatókörben).
    Ha a monitorhoz nem marad profil, az `ICMProfile` érték is eltűnik - a Windows ilyenkor
    a saját alapértelmezéséhez tér vissza. A profil FÁJLJÁHOZ nem nyúlunk.
    Visszaad: (sikerult, uzenet)."""
    key = monitor_assoc_key(monitor_device_id)
    if not key:
        return False, 'A monitor azonosítója nem értelmezhető.'
    logging.warning(f"[DISPLAY] ICC-profil társításának MEGSZÜNTETÉSE: '{profile_name}' -> "
                    f"{monitor_device_id}")
    touched, errors = 0, []
    for root, scope in ((winreg.HKEY_CURRENT_USER, 'felhasználói'),
                        (winreg.HKEY_LOCAL_MACHINE, 'rendszerszintű')):
        try:
            k = winreg.OpenKey(root, key, 0, winreg.KEY_ALL_ACCESS)
        except OSError:
            continue
        try:
            val, _t = winreg.QueryValueEx(k, 'ICMProfile')
        except OSError:
            continue
        cur = [v for v in (val if isinstance(val, list) else [val]) if isinstance(v, str) and v.strip()]
        left = [v for v in cur if v.lower() != profile_name.lower()]
        if len(left) == len(cur):
            continue
        try:
            if left:
                winreg.SetValueEx(k, 'ICMProfile', 0, winreg.REG_MULTI_SZ, left)
            else:
                winreg.DeleteValue(k, 'ICMProfile')
            touched += 1
            logging.info(f"[DISPLAY] {scope} hatókör: maradt {left or 'semmi'}")
        except OSError as e:
            logging.error(f"[DISPLAY] Nem sikerült eltávolítani ({scope}): {e}")
            errors.append(str(e))
    if errors:
        return False, errors[0]
    if not touched:
        return False, 'Ez a profil nem volt társítva ehhez a kijelzőhöz.'
    return True, ''


def install_profile(src_path):
    """Egy ICC/ICM fájl TELEPÍTÉSE a rendszerbe (bemásolás a színprofil-mappába).

    Az `InstallColorProfileW` az EGYETLEN mscms-hívás, ami ezen a gépen bizonyítottan
    működik (rc=1, err=0) - ezért ezt használjuk, és nem kézzel másolunk: így a Windows
    a saját nyilvántartásába is felveszi a profilt.
    Visszaad: (sikerult, telepitett_fajlnev_vagy_hibauzenet)."""
    if not src_path or not os.path.isfile(src_path):
        return False, 'A fájl nem található.'
    if not src_path.lower().endswith(('.icc', '.icm')):
        return False, 'Csak .icc vagy .icm kiterjesztésű színprofil telepíthető.'
    name = os.path.basename(src_path)
    logging.warning(f"[DISPLAY] ICC-profil TELEPÍTÉSE a rendszerbe: {src_path}")
    ctypes.set_last_error(0)
    ok = _mscms.InstallColorProfileW(None, src_path)
    err = ctypes.get_last_error()
    if not ok:
        logging.error(f"[DISPLAY] InstallColorProfileW sikertelen (err={err}).")
        return False, f'A Windows nem fogadta el a profilt (hibakód: {err}).'
    dest = os.path.join(color_directory(), name)
    if not os.path.exists(dest):
        logging.warning(f"[DISPLAY] A telepítés sikeresnek tűnt, de a fájl nincs a mappában: {dest}")
        return False, 'A telepítés nem hozta létre a fájlt a színprofil-mappában.'
    logging.info(f"[DISPLAY] Telepítve: {dest}")
    return True, name


def uninstall_profile(profile_name):
    """Egy telepített színprofil ELTÁVOLÍTÁSA a rendszerből (a fájl törlése).
    Előbb minden kijelzőről leszedjük a társítását, hogy ne maradjon árva hivatkozás -
    pont az a hiba, amit a registered_profiles_report a gépen talált.
    Visszaad: (sikerult, uzenet)."""
    logging.warning(f"[DISPLAY] Színprofil ELTÁVOLÍTÁSA a rendszerből: {profile_name}")
    for d in enumerate_displays():
        icc = d.get('icc') or {}
        if any(p.lower() == profile_name.lower() for p in icc.get('user', []) + icc.get('system', [])):
            disassociate_profile(d.get('monitor_device_id', ''), profile_name)
    ctypes.set_last_error(0)
    ok = _mscms.UninstallColorProfileW(None, profile_name, True)
    err = ctypes.get_last_error()
    if not ok:
        logging.error(f"[DISPLAY] UninstallColorProfileW sikertelen (err={err}).")
        return False, f'Nem sikerült eltávolítani (hibakód: {err}).'
    logging.info(f"[DISPLAY] Eltávolítva: {profile_name}")
    return True, ''


def profile_associations():
    """Melyik monitorhoz milyen ICC-profil van társítva (rendszer- és felhasználói szinten).
    Visszaad: {'system': [...], 'user': [...]}"""
    out = {'system': _read_assoc_branch(winreg.HKEY_LOCAL_MACHINE),
           'user': _read_assoc_branch(winreg.HKEY_CURRENT_USER)}
    n = len(out['system']) + len(out['user'])
    if n:
        logging.info(f"[DISPLAY] {n} monitor-profil társítás: "
                     + '; '.join(f"{a['profile']} ({a['sub']})"
                                 for a in out['system'] + out['user']))
    else:
        logging.info("[DISPLAY] EGYETLEN ICC-profil sincs a kijelzőkhöz társítva - "
                     "a Windows sRGB-ként kezeli a panelt.")
    return out


def calibration_management():
    """A "Windows kijelzőkalibráció használata" kapcsoló állapota.

    Ez dönti el, betöltődik-e egyáltalán a profilok gamma-görbéje (VCGT). Ha ki van
    kapcsolva, hiába társít bárki bármilyen profilt - a gamma nem fog betöltődni, és a
    panel korrekció nélkül marad (terepen: egy TN Dellen kiégett fehér, eltűnő
    világosszürkék). A saját programunk Build 235-ös kiadása KIKAPCSOLTA ezt az AutoFix
    végén; a 238-as kiadás javította (azóta 1-re állítja) - de egy 235-tel megfixált gépen
    a 0 MAGÁTÓL SOSEM áll vissza, ezért kell ezt kiírni a szerviznek.

    Mindkét lehetséges helyet nézzük: a kapcsoló az ICM-Calibration alkulcsban ül (ezt
    írja a Windows és a mi resetünk is), de a régebbi leírások az ICM gyökeret említik -
    egy hiányzó érték nem jelenthet "rendben van"-t, ha a másik helyen 0 áll.
    Visszaad: 1 / 0 / None (sehol nincs beállítva = a Windows alapértelmezése)."""
    found = None
    for path in (_ICM_CALIB, _ICM_BASE):
        try:
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
            val = int(winreg.QueryValueEx(k, 'CalibrationManagementEnabled')[0])
        except (OSError, ValueError, TypeError):
            continue
        logging.info(f"[DISPLAY] CalibrationManagementEnabled = {val} "
                     f"(HKLM\\{path})")
        if val == 0:            # a rossz állapot mindig nyer: ez az, amiről szólni kell
            return 0
        if found is None:
            found = val
    if found is None:
        logging.debug("[DISPLAY] CalibrationManagementEnabled sehol nincs beállítva "
                      "(a Windows alapértelmezése érvényes).")
    return found


def set_calibration_management(enabled):
    """A "Windows-kijelzőkalibráció használata" kapcsoló ÁTÁLLÍTÁSA (1 = be, 0 = ki).

    MIT CSINÁL EZ VALÓJÁBAN - a két irány NEM szimmetrikus a hatását tekintve:
      - 1 (be): a Windows alapértelmezése. Önmagában SEMMILYEN kalibrációt nem állít be,
        csak MEGENGEDI, hogy egy társított profil gamma-görbéje (VCGT) betöltődjön. Ha
        nincs társított profil, semmi nem változik a képen.
      - 0 (ki): a betöltés letiltása. Ettől kezdve a gépen BÁRMILYEN profilt lehet
        társítani, a gamma akkor sem tölt be - a panel korrekció nélkül, natívan megy.

    MINDKÉT IRÁNYNAK VAN LEGITIM HASZNÁLATA (explicit user decision, 2026-07-31), ezért van
    rá kapcsoló és nem csak "javítás" gomb:
      - KI: szélesgamutos OLED-en, ahol a felhasználó szándékosan a natív, telített képet
        akarja, és biztosra akar menni, hogy semmi (frissítés, gyártói eszköz, egy másik
        program) ne tudjon a háttérben gamma-görbét betölteni;
      - BE: minden más gépen, különösen ha egy korábbi kiadásunk (Build 235-237) AutoFixe
        kikapcsolta - ott a 0 magától soha nem áll vissza, és a panel korrekció nélkül marad
        (terepen: TN Dell, kiégett fehér, eltűnő világosszürkék).

    A művelet destruktív-jellegű (tartós rendszerbeállítás), ezért az ÁTÁLLÍTÁS ELŐTT
    naplózzuk az előző értéket. Visszaad: (sikerult, elozo_ertek)."""
    want = 1 if enabled else 0
    prev = calibration_management()
    logging.warning(f"[DISPLAY] CalibrationManagementEnabled beállítása {want}-re "
                    f"(előző érték: {prev if prev is not None else 'nincs beállítva'}) - "
                    f"HKLM-{_ICM_CALIB}")
    try:
        k = winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, _ICM_CALIB, 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(k, 'CalibrationManagementEnabled', 0, winreg.REG_DWORD, want)
    except OSError as e:
        logging.error(f"[DISPLAY] A kalibrációkezelés átállítása nem sikerült: {e}")
        return False, prev
    logging.info(f"[DISPLAY] A Windows kijelzőkalibráció-kezelése "
                 f"{'BEkapcsolva' if want else 'KIkapcsolva'} "
                 f"(a hatáshoz ki-be jelentkezés vagy újraindítás kellhet).")
    return True, prev


def registered_profiles_report():
    """A Windowsban REGISZTRÁLT profilok (pl. az sRGB munkatér) + létezik-e a fájljuk.

    MIÉRT KELL EZ - terepen bizonyított, a fejlesztői gépen (2026-07-30): a
    HKCU\\...\\ICM\\RegisteredProfiles\\sRGB egy 'srgb_to_gamma2p2_300_mhc2.icm' nevű fájlra
    mutatott, ami NINCS a színprofil-mappában (egy külső HDR-eszköz hagyta ott). Ettől a
    mscms MINDEN hívása ERROR_FILE_NOT_FOUND-dal tért vissza - vagyis a gép színkezelése
    csendben törött volt, és ezt semmilyen Windows-felület nem mutatja meg.
    Visszaad: lista dict-ekből, a 'missing' kulcs jelzi a törött bejegyzést."""
    cdir = color_directory()
    out = []
    for root, rname in ((winreg.HKEY_CURRENT_USER, 'HKCU'), (winreg.HKEY_LOCAL_MACHINE, 'HKLM')):
        try:
            k = winreg.OpenKey(root, _ICM_REGISTERED)
        except OSError:
            continue
        i = 0
        while True:
            try:
                name, val, _t = winreg.EnumValue(k, i)
            except OSError:
                break
            i += 1
            if not isinstance(val, str) or not val.strip():
                continue
            full = val if os.path.isabs(val) else os.path.join(cdir, val)
            missing = not os.path.exists(full)
            out.append({'root': rname, 'name': name, 'profile': val, 'missing': missing})
            if missing:
                logging.warning(f"[DISPLAY] TÖRÖTT színkezelés-regisztráció: {rname} "
                                f"RegisteredProfiles\\{name} = '{val}', de ez a fájl NINCS MEG "
                                f"({full}). Emiatt a Windows színkezelő hívásai hibára futnak.")
    return out


def repair_registered_profiles(dry_run=False):
    """A törött (nem létező fájlra mutató) profil-regisztrációk eltávolítása.

    Csak azokat a bejegyzéseket törli, amelyek fájlja BIZONYÍTOTTAN hiányzik - ép
    regisztrációhoz nem nyúlunk. A törlés előtt minden érintett bejegyzést névvel
    naplózunk (destruktív lépés). A Windows a hiányzó bejegyzést a saját beépített
    alapértelmezésével pótolja, tehát a törlés a helyreállítás, nem a kár.
    Visszaad: (eltavolitott_lista, hiba_lista)."""
    removed, errors = [], []
    for entry in registered_profiles_report():
        if not entry['missing']:
            continue
        root = winreg.HKEY_CURRENT_USER if entry['root'] == 'HKCU' else winreg.HKEY_LOCAL_MACHINE
        label = f"{entry['root']}\\RegisteredProfiles\\{entry['name']} -> '{entry['profile']}'"
        if dry_run:
            removed.append(label)
            continue
        logging.warning(f"[DISPLAY] Törött regisztráció ELTÁVOLÍTÁSA: {label}")
        try:
            k = winreg.OpenKey(root, _ICM_REGISTERED, 0, winreg.KEY_SET_VALUE)
            winreg.DeleteValue(k, entry['name'])
            removed.append(label)
        except OSError as e:
            logging.error(f"[DISPLAY] Nem sikerült eltávolítani ({label}): {e}")
            errors.append(f"{label}: {e}")
    return removed, errors
