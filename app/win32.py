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
