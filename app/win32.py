"""Win32 ctypes struktúrák és konstansok (SendInput, konzol-puffer, ShellExecuteEx,
GlobalMemoryStatusEx, Lomtár) - főleg a Stabilitás Teszt automatizálás és a
temp-takarítás használja."""

# === AUTO-IMPORTS ===
import ctypes
import ctypes.wintypes
# === /AUTO-IMPORTS ===



class _MEMORYSTATUSEX(ctypes.Structure):
    """A Win32 GlobalMemoryStatusEx-hez tartozó struktúra - a teljes fizikai RAM
    lekérdezéséhez (Linpack RAM-opció automatikus kiválasztásához), subprocess/WMI
    hívás nélkül."""
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


# A konzol képernyőpufferének kiolvasásához (GetConsoleScreenBufferInfo /
# ReadConsoleOutputCharacterW) szükséges struktúrák - a Linpack menü-automatizálása ezzel
# ellenőrzi, hogy a várt prompt tényleg megjelent-e, mielőtt begépelné a választ.
class _COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class _SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]


class _CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [("dwSize", _COORD), ("dwCursorPosition", _COORD),
                ("wAttributes", ctypes.c_ushort), ("srWindow", _SMALL_RECT),
                ("dwMaximumWindowSize", _COORD)]


# SendInput-hoz szükséges struktúrák (a konzolos menük - pl. Linpack - "begépeléséhez"):
# valódi billentyű-esemény szimuláció, mert a konzolablakok (conhost) bemenet-kezelése a
# stdin egyszerű pipe-ra kötésével nem mindig működik együtt (ld. _launch_stress_exe
# docstringje - a Linpack ezzel elindulás előtt megbukott).
_PUL = ctypes.POINTER(ctypes.c_ulong)


class _KeyBdInput(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", _PUL)]


class _HardwareInput(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_short), ("wParamH", ctypes.c_ushort)]


class _MouseInput(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", _PUL)]


class _InputUnion(ctypes.Union):
    _fields_ = [("ki", _KeyBdInput), ("mi", _MouseInput), ("hi", _HardwareInput)]


class _Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("ii", _InputUnion)]


INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002
VK_RETURN = 0x0D
BM_CLICK = 0x00F5  # natív Win32 gomb-vezérlők "megnyomása" üzenettel (pl. FurMark GUI-ja)


# ShellExecuteExW-hez szükséges struktúra - ez kell ahhoz, hogy egy UAC 'runas' verbbel
# (adminként) indított exe (pl. HWiNFO64, aminek requireAdministrator a manifestje) valódi
# PID-jét megkapjuk: a sima ShellExecuteW nem ad vissza process handle-t, csak
# ShellExecuteExW SEE_MASK_NOCLOSEPROCESS maszkkal - ld. _launch_stress_exe.
SEE_MASK_NOCLOSEPROCESS = 0x00000040
SW_SHOWNORMAL = 1


class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", ctypes.wintypes.HWND),
        ("lpVerb", ctypes.wintypes.LPCWSTR),
        ("lpFile", ctypes.wintypes.LPCWSTR),
        ("lpParameters", ctypes.wintypes.LPCWSTR),
        ("lpDirectory", ctypes.wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", ctypes.wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", ctypes.wintypes.LPCWSTR),
        ("hKeyClass", ctypes.wintypes.HANDLE),
        ("dwHotKey", ctypes.wintypes.DWORD),
        ("hIcon", ctypes.wintypes.HANDLE),
        ("hProcess", ctypes.wintypes.HANDLE),
    ]


# SHQueryRecycleBinW-hez (a Temp Törlés funkció Lomtár-ürítés kategóriájához) - ürítés
# ELŐTT kérdezzük le a Lomtár méretét, mert az ürítő hívás (SHEmptyRecycleBinW) magától
# nem adja vissza, mennyi hely szabadult fel.
class _SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD),
        ("i64Size", ctypes.c_int64),
        ("i64NumItems", ctypes.c_int64),
    ]


# SystemParametersInfoW(SPI_GETWORKAREA) - az elsődleges monitor MUNKATERÜLETE (a képernyő a
# tálca és a többi appbar nélkül). A FurMark benchmark ablakkeret-kompenzációja használja: ha
# a keretmérettel megnövelt ablak már nem férne el a képernyőn, akkor nem kompenzálunk, mert
# ott a renderelt kép úgyis csonkulna (lásd app/gui/benchmark.py + app/benchmark_defs.py).
# FIGYELEM: ez NEM azt jelenti, hogy a Windows a munkaterülethez vágja az ablakot - a
# ténylegesen renderelt méretről kizárólag a FurMark score-fájljának [Resolution=..] mezője
# dönt, sosem a képernyőmérettel való összevetés.
# (A stressz-teszt ablakelrendezése nem ezt hívja: annak saját, helyi SPI_GETWORKAREA
# definíciója van az app/gui/stress_automation.py-ban.)
SPI_GETWORKAREA = 0x0030


def get_work_area():
    """Az elsődleges monitor munkaterületének mérete pixelben: (szélesség, magasság),
    vagy None, ha a lekérdezés nem sikerült (a hívó ilyenkor "nem tudjuk"-ként kezeli,
    nem hibaként)."""
    rect = ctypes.wintypes.RECT()
    ok = ctypes.windll.user32.SystemParametersInfoW(
        SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
    if not ok:
        return None
    return rect.right - rect.left, rect.bottom - rect.top


# ============================================================================
# DisplayConfig (CCD) API - a Kijelző nézet HDR-kezeléséhez
# ============================================================================
# A user32 QueryDisplayConfig/DisplayConfigGetDeviceInfo/DisplayConfigSetDeviceInfo hármas
# az EGYETLEN támogatott út a HDR állapotának olvasásához és átkapcsolásához (a Beállítások
# alkalmazás is ezt hívja). Élőben ellenőrizve 2026-07-30-án egy AOC AG276QZD2-n: a
# SET_HDR_STATE(16) hívás bekapcsolta a HDR-t (8 bit -> 10 bit, activeColorMode 0 -> 2),
# és a visszakapcsolás is tisztán lement.
#
# A struktúrák mérete KRITIKUS: a header 'size' mezőjébe a teljes struktúra méretét kell
# írni, és ha az nem stimmel, a hívás ERROR_INVALID_PARAMETER-rel bukik. Ezért egyik
# struktúrához sem szabad mezőt hozzáadni/elvenni a Windows definíciójától eltérően.

QDC_ONLY_ACTIVE_PATHS = 2

# DISPLAYCONFIG_DEVICE_INFO_TYPE értékek (csak amiket használunk)
DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME = 1
DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME = 2
DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO = 9    # régi, minden Win10/11-en megvan
DISPLAYCONFIG_DEVICE_INFO_SET_ADVANCED_COLOR_STATE = 10  # régi HDR-kapcsoló
DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL = 11
DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO_2 = 15  # Win11 24H2+, részletesebb
DISPLAYCONFIG_DEVICE_INFO_SET_HDR_STATE = 16              # Win11 24H2+ HDR-kapcsoló

# DISPLAYCONFIG_VIDEO_OUTPUT_TECHNOLOGY - csak a szerviz által látott gyakoriak
DISPLAY_OUTPUT_TECHNOLOGY = {
    0: 'VGA (D-SUB)', 1: 'S-Video', 2: 'Kompozit', 3: 'Komponens', 4: 'DVI', 5: 'HDMI',
    6: 'LVDS', 8: 'D-Jpn', 9: 'SDI', 10: 'DisplayPort', 11: 'DisplayPort (beépített)',
    12: 'UDI', 13: 'UDI (beépített)', 14: 'SDTV', 15: 'Miracast',
    0x80000000: 'Belső kijelző', 0xFFFFFFFF: 'Egyéb',
}

# DISPLAYCONFIG_COLOR_ENCODING
DISPLAY_COLOR_ENCODING = {0: 'RGB', 1: 'YCbCr444', 2: 'YCbCr422', 3: 'YCbCr420',
                          4: 'Intenzitás'}

# DISPLAYCONFIG_ADVANCED_COLOR_MODE (az ACI2 activeColorMode mezője)
DISPLAY_ADVANCED_COLOR_MODE = {0: 'SDR', 1: 'WCG (széles gamut)', 2: 'HDR'}


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.wintypes.DWORD), ("HighPart", ctypes.c_long)]


class _DISPLAYCONFIG_RATIONAL(ctypes.Structure):
    _fields_ = [("Numerator", ctypes.wintypes.UINT), ("Denominator", ctypes.wintypes.UINT)]


class _DISPLAYCONFIG_PATH_SOURCE_INFO(ctypes.Structure):
    _fields_ = [("adapterId", _LUID), ("id", ctypes.wintypes.UINT),
                ("modeInfoIdx", ctypes.wintypes.UINT), ("statusFlags", ctypes.wintypes.UINT)]


class _DISPLAYCONFIG_PATH_TARGET_INFO(ctypes.Structure):
    _fields_ = [("adapterId", _LUID), ("id", ctypes.wintypes.UINT),
                ("modeInfoIdx", ctypes.wintypes.UINT),
                ("outputTechnology", ctypes.wintypes.UINT),
                ("rotation", ctypes.wintypes.UINT), ("scaling", ctypes.wintypes.UINT),
                ("refreshRate", _DISPLAYCONFIG_RATIONAL),
                ("scanLineOrdering", ctypes.wintypes.UINT),
                ("targetAvailable", ctypes.wintypes.BOOL),
                ("statusFlags", ctypes.wintypes.UINT)]


class _DISPLAYCONFIG_PATH_INFO(ctypes.Structure):
    _fields_ = [("sourceInfo", _DISPLAYCONFIG_PATH_SOURCE_INFO),
                ("targetInfo", _DISPLAYCONFIG_PATH_TARGET_INFO),
                ("flags", ctypes.wintypes.UINT)]


class _DISPLAYCONFIG_MODE_INFO(ctypes.Structure):
    """A mód-leíró uniójának belsejét nem bontjuk ki (nem használjuk) - csak a méret
    kell, hogy a tömb léptetése stimmeljen. A forrás-mód (felbontás) a blob elejéről
    olvasható ki, lásd source_mode_size()."""
    _fields_ = [("infoType", ctypes.wintypes.UINT), ("id", ctypes.wintypes.UINT),
                ("adapterId", _LUID), ("blob", ctypes.c_byte * 48)]


class _DISPLAYCONFIG_DEVICE_INFO_HEADER(ctypes.Structure):
    _fields_ = [("type", ctypes.wintypes.UINT), ("size", ctypes.wintypes.UINT),
                ("adapterId", _LUID), ("id", ctypes.wintypes.UINT)]


class _DISPLAYCONFIG_SOURCE_DEVICE_NAME(ctypes.Structure):
    _fields_ = [("header", _DISPLAYCONFIG_DEVICE_INFO_HEADER),
                ("viewGdiDeviceName", ctypes.wintypes.WCHAR * 32)]


class _DISPLAYCONFIG_TARGET_DEVICE_NAME(ctypes.Structure):
    _fields_ = [("header", _DISPLAYCONFIG_DEVICE_INFO_HEADER),
                ("flags", ctypes.wintypes.UINT),
                ("outputTechnology", ctypes.wintypes.UINT),
                ("edidManufactureId", ctypes.wintypes.USHORT),
                ("edidProductCodeId", ctypes.wintypes.USHORT),
                ("connectorInstance", ctypes.wintypes.UINT),
                ("monitorFriendlyDeviceName", ctypes.wintypes.WCHAR * 64),
                ("monitorDevicePath", ctypes.wintypes.WCHAR * 128)]


class _DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO(ctypes.Structure):
    _fields_ = [("header", _DISPLAYCONFIG_DEVICE_INFO_HEADER),
                ("value", ctypes.wintypes.UINT), ("colorEncoding", ctypes.wintypes.UINT),
                ("bitsPerColorChannel", ctypes.wintypes.UINT)]


class _DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO_2(ctypes.Structure):
    _fields_ = [("header", _DISPLAYCONFIG_DEVICE_INFO_HEADER),
                ("value", ctypes.wintypes.UINT), ("colorEncoding", ctypes.wintypes.UINT),
                ("bitsPerColorChannel", ctypes.wintypes.UINT),
                ("activeColorMode", ctypes.wintypes.UINT)]


class _DISPLAYCONFIG_SET_ADVANCED_COLOR_STATE(ctypes.Structure):
    _fields_ = [("header", _DISPLAYCONFIG_DEVICE_INFO_HEADER),
                ("value", ctypes.wintypes.UINT)]


class _DISPLAYCONFIG_SET_HDR_STATE(ctypes.Structure):
    _fields_ = [("header", _DISPLAYCONFIG_DEVICE_INFO_HEADER),
                ("value", ctypes.wintypes.UINT)]


class _DISPLAYCONFIG_SDR_WHITE_LEVEL(ctypes.Structure):
    _fields_ = [("header", _DISPLAYCONFIG_DEVICE_INFO_HEADER),
                ("SDRWhiteLevel", ctypes.wintypes.ULONG)]


class _DISPLAY_DEVICEW(ctypes.Structure):
    r"""EnumDisplayDevicesW-hez: az adapter (\\.\DISPLAY1) és a rajta lévő monitor."""
    _fields_ = [("cb", ctypes.wintypes.DWORD), ("DeviceName", ctypes.wintypes.WCHAR * 32),
                ("DeviceString", ctypes.wintypes.WCHAR * 128),
                ("StateFlags", ctypes.wintypes.DWORD),
                ("DeviceID", ctypes.wintypes.WCHAR * 128),
                ("DeviceKey", ctypes.wintypes.WCHAR * 128)]


DISPLAY_DEVICE_ATTACHED_TO_DESKTOP = 0x1
DISPLAY_DEVICE_PRIMARY_DEVICE = 0x4


# =============================================================================
# ESZKÖZFA (cfgmgr32) - a "Gép felépítése" nézet és a használat-felderítés KÖZÖS forrása
# =============================================================================
# MIÉRT cfgmgr32 ÉS NEM PowerShell/WMI (mérve, 2026-09-17, HP EliteDesk 800 G2):
#   - 0,23 mp a TELJES fa (146 eszköz-példány, a nem jelenlévők is), subprocess és admin
#     jog nélkül - a registry-bejárós PowerShell ugyanerre 2,9 mp volt;
#   - EGYEDÜL ez adja meg olcsón az eszköz SZÜLŐJÉT (a `Get-PnpDeviceProperty`
#     eszközönként ~1,1 mp) és a CONTAINER ID-t, ami nélkül a gépet nem lehet
#     alkatrészekre bontani: a Windows ugyanazzal a GUID-dal (`{00000000-0000-0000-
#     FFFF-FFFFFFFFFFFF}`) jelöl minden eszközt, ami a SZÁMÍTÓGÉP RÉSZE, és külön
#     container-t ad minden külső eszköznek (egér, nyomtató, monitor, telefon) - ez az
#     "Eszközök és nyomtatók" ablak saját csoportosítása, nem a mi találgatásunk.
# A PCI/USB OSZTÁLYKÓDOK (a kompatibilis azonosítókban: `PCI\CC_0403`, `USB\Class_0E`)
# a HARDVER által jelentett adatok, nem a driver gyártójának besorolása - a "mi ez az
# eszköz?" kérdésre ezek a legerősebb bizonyítékok.

PC_CONTAINER_ID = '00000000-0000-0000-ffff-ffffffffffff'


class _DEVPROPKEY(ctypes.Structure):
    _fields_ = [('fmtid', ctypes.c_ubyte * 16), ('pid', ctypes.c_ulong)]


def _devpkey(guid, pid):
    import uuid
    k = _DEVPROPKEY()
    ctypes.memmove(k.fmtid, uuid.UUID(guid).bytes_le, 16)
    k.pid = pid
    return k


_DEVPKEY_GUID_DEVICE = 'a45c254e-df1c-4efd-8020-67d146a850e0'
_DEVPKEYS = {
    'desc': (_DEVPKEY_GUID_DEVICE, 2),
    'hwids': (_DEVPKEY_GUID_DEVICE, 3),
    'compat': (_DEVPKEY_GUID_DEVICE, 4),
    'service': (_DEVPKEY_GUID_DEVICE, 6),
    'cls': (_DEVPKEY_GUID_DEVICE, 9),
    'mfg': (_DEVPKEY_GUID_DEVICE, 13),
    'friendly': (_DEVPKEY_GUID_DEVICE, 14),
    'location': (_DEVPKEY_GUID_DEVICE, 15),
    'upper_filters': (_DEVPKEY_GUID_DEVICE, 19),
    'lower_filters': (_DEVPKEY_GUID_DEVICE, 20),
    'busdesc': ('540b947e-8b40-45bc-a8a2-6a0b894cbda2', 4),
    'parent': ('4340a6c5-93fa-4706-972c-7b648008a5a7', 8),
    'container': ('8c7ed206-3f8a-4827-b3ab-ae9e1faefc6c', 2),
    'inf': ('a8b865dd-2e3d-4094-ad97-e593a70c75d6', 5),
}

_CR_SUCCESS = 0
_CM_LOCATE_DEVNODE_PHANTOM = 1
_DN_HAS_PROBLEM = 0x400
_DEVPROP_TYPE_STRING = 0x12
_DEVPROP_TYPE_STRING_LIST = 0x2012
_DEVPROP_TYPE_GUID = 0x0D


def _devnode_prop(cfg, devinst, key):
    """Egy eszköz-tulajdonság kiolvasása. Ismeretlen típusnál / hiánynál None."""
    import uuid
    ptype = ctypes.c_ulong(0)
    size = ctypes.c_ulong(0)
    cfg.CM_Get_DevNode_PropertyW(devinst, ctypes.byref(key), ctypes.byref(ptype),
                                 None, ctypes.byref(size), 0)
    if not size.value:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if cfg.CM_Get_DevNode_PropertyW(devinst, ctypes.byref(key), ctypes.byref(ptype),
                                    buf, ctypes.byref(size), 0) != _CR_SUCCESS:
        return None
    raw = buf.raw[:size.value]
    if ptype.value == _DEVPROP_TYPE_STRING:
        return raw.decode('utf-16-le', 'replace').rstrip('\x00')
    if ptype.value == _DEVPROP_TYPE_STRING_LIST:
        return [s for s in raw.decode('utf-16-le', 'replace').split('\x00') if s]
    if ptype.value == _DEVPROP_TYPE_GUID and len(raw) >= 16:
        return str(uuid.UUID(bytes_le=raw[:16]))
    return None


def enumerate_device_nodes():
    """A Windows TELJES eszközfája (jelenlévő + nem jelenlévő eszközök), egy lépésben.

    Visszatérés: dict-ek listája - `id`, `present`, `problem` (hibakód vagy 0),
    `parent`, `container`, `cls` (Windows-osztály), `desc`, `friendly`, `busdesc` (a
    hardver saját neve, pl. 'HP USB Optical Mouse'), `inf` (a kötött INF), `hwids`,
    `compat`, `service`, `mfg`, `location`, `upper_filters`, `lower_filters`.

    Hiba esetén KIVÉTELT dob (nem üres listát): egy üres fa azt állítaná, hogy a gépben
    nincs eszköz, és a hívó erre törlési döntést alapozhatna. A hívó dönti el, hogy
    tartalék úton (PowerShell) próbálkozik-e."""
    cfg = ctypes.WinDLL('cfgmgr32')
    cfg.CM_Get_Device_ID_List_SizeW.argtypes = [ctypes.POINTER(ctypes.c_ulong), ctypes.c_wchar_p, ctypes.c_ulong]
    cfg.CM_Get_Device_ID_ListW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong]
    cfg.CM_Locate_DevNodeW.argtypes = [ctypes.POINTER(ctypes.c_ulong), ctypes.c_wchar_p, ctypes.c_ulong]
    cfg.CM_Get_DevNode_PropertyW.argtypes = [ctypes.c_ulong, ctypes.POINTER(_DEVPROPKEY),
                                             ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p,
                                             ctypes.POINTER(ctypes.c_ulong), ctypes.c_ulong]
    cfg.CM_Get_DevNode_Status.argtypes = [ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
                                          ctypes.c_ulong, ctypes.c_ulong]

    size = ctypes.c_ulong(0)
    cr = cfg.CM_Get_Device_ID_List_SizeW(ctypes.byref(size), None, 0)
    if cr != _CR_SUCCESS or not size.value:
        raise OSError(f"CM_Get_Device_ID_List_SizeW hiba (CR={cr})")
    # Két hívás között új eszköz is megjelenhet (USB-bedugás): a puffer ezért tágabb.
    buf_len = size.value + 4096
    buf = ctypes.create_unicode_buffer(buf_len)
    cr = cfg.CM_Get_Device_ID_ListW(None, buf, buf_len, 0)
    if cr != _CR_SUCCESS:
        raise OSError(f"CM_Get_Device_ID_ListW hiba (CR={cr})")
    ids = [s for s in buf[:buf_len].split('\x00') if s]

    keys = {name: _devpkey(g, p) for name, (g, p) in _DEVPKEYS.items()}
    nodes = []
    for dev_id in ids:
        devinst = ctypes.c_ulong(0)
        if cfg.CM_Locate_DevNodeW(ctypes.byref(devinst), dev_id, _CM_LOCATE_DEVNODE_PHANTOM) != _CR_SUCCESS:
            continue
        status = ctypes.c_ulong(0)
        problem = ctypes.c_ulong(0)
        present = cfg.CM_Get_DevNode_Status(ctypes.byref(status), ctypes.byref(problem),
                                            devinst.value, 0) == _CR_SUCCESS
        node = {'id': dev_id, 'present': present,
                'problem': problem.value if (present and status.value & _DN_HAS_PROBLEM) else 0}
        for name, key in keys.items():
            node[name] = _devnode_prop(cfg, devinst.value, key)
        nodes.append(node)
    return nodes


def read_class_filters():
    """Az osztály-szintű szűrő-driverek (`Control\\Class\\{guid}` Upper/LowerFilters)
    kisbetűs neveinek halmaza. Tiszta registry-olvasás; hibánál üres halmaz + napló."""
    import winreg
    import logging
    out = set()
    base = r'SYSTEM\CurrentControlSet\Control\Class'
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError as e:
        logging.warning(f"[DEVTREE] Az osztály-szűrők nem olvashatók: {e}")
        return out
    with root:
        i = 0
        while True:
            try:
                guid = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(root, guid) as k:
                    for vn in ('UpperFilters', 'LowerFilters'):
                        try:
                            val, _t = winreg.QueryValueEx(k, vn)
                        except OSError:
                            continue
                        for f in (val if isinstance(val, list) else [val]):
                            if f:
                                out.add(str(f).strip().lower())
            except OSError:
                continue
    return out


def read_hardware_identity():
    """A gép neve, alaplapja és processzora a registryből (`HARDWARE\\DESCRIPTION`).

    Ugyanazt adja, mint a WMI `Win32_ComputerSystem`/`Win32_BaseBoard`/`Win32_Processor`
    (a firmware SMBIOS-táblájából töltődik bootkor), csak subprocess nélkül, azonnal."""
    import winreg
    out = {}

    def _read(path, names):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
                for n in names:
                    try:
                        out[n] = str(winreg.QueryValueEx(k, n)[0]).strip()
                    except OSError:
                        out.setdefault(n, '')
        except OSError:
            for n in names:
                out.setdefault(n, '')

    _read(r'HARDWARE\DESCRIPTION\System\BIOS',
          ('SystemManufacturer', 'SystemProductName', 'SystemFamily', 'SystemVersion',
           'BaseBoardManufacturer', 'BaseBoardProduct'))
    _read(r'HARDWARE\DESCRIPTION\System\CentralProcessor\0', ('ProcessorNameString',))
    return out


def platform_role():
    """A firmware által bejelentett géptípus (`PowerDeterminePlatformRoleEx`).

    1 = asztali, 2 = mobil (laptop), 3 = munkaállomás, 8 = tablet ("Slate"), 0 = nem
    meghatározott. Az ACPI FADT "Preferred PM Profile" mezőjéből jön, tehát a gép
    GYÁRTÓJA mondja meg, nem mi tippeljük. Hibánál None."""
    try:
        powrprof = ctypes.WinDLL('powrprof')
        fn = powrprof.PowerDeterminePlatformRoleEx
        fn.argtypes = [ctypes.c_ulong]
        fn.restype = ctypes.c_int
        return int(fn(2))  # POWER_PLATFORM_ROLE_V2
    except Exception:
        return None
