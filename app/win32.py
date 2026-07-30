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


# SystemParametersInfoW(SPI_GETWORKAREA) - az elsődleges monitor MUNKATERÜLETE (a képernyő
# a tálca és a többi appbar nélkül). A FurMark benchmark ablakos módban fut, és a Windows a
# munkaterülethez VÁGJA a túl nagy ablakot: ebből tudjuk előre, belefér-e a kompenzált
# (keretmérettel megnövelt) ablak, vagy csonkulna - lásd app/gui/benchmark.py.
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
