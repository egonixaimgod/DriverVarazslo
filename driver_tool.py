import ctypes
import ctypes.wintypes
import os
import sys
import subprocess
import re
import threading
import time
import logging
import shutil
import json
import glob
import traceback
import winreg
import queue
import math
import socket
from datetime import datetime, timezone
from html import escape as html_escape

BUILD_NUMBER = 195

try:
    import webview
except ImportError:
    print("HIBA: pywebview nem található! Telepítsd: pip install pywebview")
    sys.exit(1)

# pywebview 6.x deprecation compat
try:
    _FOLDER_DIALOG = webview.FileDialog.FOLDER
    _OPEN_DIALOG = webview.FileDialog.OPEN
except AttributeError:
    _FOLDER_DIALOG = webview.FOLDER_DIALOG
    _OPEN_DIALOG = webview.OPEN_DIALOG

# WebView2 init state (watchdog)
_webview_ready = threading.Event()
_webview_error = threading.Event()

# WebView2 minimum verzió ellenőrzés (ICoreWebView2Environment10 interface min v109 kell)
MIN_WEBVIEW2_MAJOR = 109

def check_webview2_runtime():
    """
    Ellenőrzi, hogy a WebView2 Runtime telepítve van-e és megfelelő verzió-e.
    Visszatérési értékek:
        (True, verzió_string) - OK
        (False, hibaüzenet) - Hiba
    """
    version = None
    
    # 1. Önálló WebView2 Runtime telepítések (EdgeUpdate registry)
    edgeupdate_paths = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
    ]
    for hive, path in edgeupdate_paths:
        try:
            with winreg.OpenKey(hive, path) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
                if version and version != "0.0.0.0":
                    break
        except (FileNotFoundError, OSError):
            continue
    
    # 2. Edge beépített WebView2 (Windows 11 / Edge-be integrált)
    if not version:
        edge_webview_paths = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeWebView\BLBeacon"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeWebView\BLBeacon"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\EdgeWebView\BLBeacon"),
        ]
        for hive, path in edge_webview_paths:
            try:
                with winreg.OpenKey(hive, path) as key:
                    version, _ = winreg.QueryValueEx(key, "version")
                    if version:
                        break
            except (FileNotFoundError, OSError):
                continue
    
    # 3. Edge böngésző verzió (fallback - ha WebView2 nincs külön regisztrálva)
    if not version:
        edge_paths = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Edge\BLBeacon"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Edge\BLBeacon"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Edge\BLBeacon"),
        ]
        for hive, path in edge_paths:
            try:
                with winreg.OpenKey(hive, path) as key:
                    version, _ = winreg.QueryValueEx(key, "version")
                    if version:
                        break
            except (FileNotFoundError, OSError):
                continue
    
    # 4. Utolsó esély: GetAvailableCoreWebView2BrowserVersionString (ha van WebView2Loader.dll)
    if not version:
        try:
            wv2_loader = ctypes.windll.LoadLibrary("WebView2Loader.dll")
            buf = ctypes.create_unicode_buffer(256)
            hr = wv2_loader.GetAvailableCoreWebView2BrowserVersionString(None, ctypes.byref(buf))
            if hr == 0 and buf.value:
                version = buf.value
        except Exception as e:
            logging.debug(e)
    
    if not version:
        return (False, "WebView2 Runtime nem található!\n\n"
                       "A program működéséhez telepíteni kell:\n"
                       "https://go.microsoft.com/fwlink/p/?LinkId=2124703\n\n"
                       "(Evergreen Bootstrapper)")
    
    # Verzió parsing: pl. "109.0.1518.61" -> 109
    try:
        major = int(version.split('.')[0])
    except (ValueError, IndexError):
        major = 0
    
    if major < MIN_WEBVIEW2_MAJOR:
        return (False, f"WebView2 Runtime túl régi! (v{version})\n\n"
                       f"Minimum v{MIN_WEBVIEW2_MAJOR}.x szükséges.\n\n"
                       "Frissítsd itt:\n"
                       "https://go.microsoft.com/fwlink/p/?LinkId=2124703")
    
    return (True, version)


def show_webview2_error(message):
    """MessageBox megjelenítése WebView2 hibáról, majd program kilépés."""
    try:
        import webbrowser
        MB_ICONERROR = 0x10
        MB_TOPMOST = 0x40000
        result = ctypes.windll.user32.MessageBoxW(
            None,
            message + "\n\nMegnyissam a letöltési oldalt?",
            "DriverVarázsló - WebView2 hiba",
            0x4 | MB_ICONERROR | MB_TOPMOST  # MB_YESNO
        )
        if result == 6:  # IDYES
            webbrowser.open("https://go.microsoft.com/fwlink/p/?LinkId=2124703")
    except Exception as e:
        logging.debug(e)
    sys.exit(1)


# Suppress noisy PIL/Pillow debug logging
logging.getLogger('PIL').setLevel(logging.WARNING)
logging.getLogger('PIL.PngImagePlugin').setLevel(logging.WARNING)




def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False


def resource_path(relative_path):
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


def _ps_quote(value):
    """PowerShell egyszeres idézőjeles string escape: ' -> '' , hogy egy aposztrófot
    tartalmazó fájlútvonal (pl. C:\\Users\\O'Brien\\...) ne törje meg a generált parancsot."""
    return str(value).replace("'", "''")


# AutoFix-nál opcionálisan kihagyható driver-osztályok (nyomtató + szkenner/multifunkciós) -
# ezek gyakran csak gyári driverrel működnek jól, a WU nem mindig telepíti vissza automatikusan.
AUTOFIX_PRINTER_SKIP_CLASSES = {'Printer', 'PrintQueue', 'Image'}


# ============================================================================
# WU DRIVER KERESÉS / TELEPÍTÉS - KÖZÖS MAG
# A temp-cleanup mintájára: az eszköz-szűrés, a WU-találat<->eszköz párosítás és
# a telepítő PowerShell script EGYETLEN példányban itt él, és a manuális
# telepítés (DriverToolApi._install_wu_api + start_hw_scan), a GUI AutoFix
# (_scan_and_install_wu_sync) és a CLI AutoFix (CliApi) is EZEKET hívja.
# Ha itt javítasz valamit, mindhárom út egyszerre javul - NE másold vissza a
# logikát egyik osztályba se, mert pont az szülte a korábbi "az autofix
# működik, a manuális eltört" hibát!
# ============================================================================

# WU driver-kereséskor figyelmen kívül hagyott PnP eszközosztályok (mindhárom út közös szűrője).
WU_SCAN_IGNORED_CLASSES = ['Volume', 'VolumeSnapshot', 'DiskDrive', 'CDROM', 'Monitor', 'Battery',
                           'Processor', 'Computer',
                           'LegacyDriver', 'Endpoint', 'AudioEndpoint', 'PrintQueue', 'Printer', 'WPD']

# A jelenlévő PnP eszközök lekérdezése (a kimenetet a _filter_wu_scan_devices dolgozza fel).
WU_PNP_QUERY_PS = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                   "Get-WmiObject Win32_PnPEntity | Where-Object { $_.Present -eq $true -and $_.ConfigManagerErrorCode -ne 45 } | "
                   "Select-Object Name, PNPClass, PNPDeviceID, HardwareID | ConvertTo-Json -Compress")


def _filter_wu_scan_devices(pnp_data):
    """A WU_PNP_QUERY_PS JSON kimenetéből kiszűri a driver-kereséshez érdemi eszközöket
    (virtuális/ROOT/ignorált osztályok nélkül, HWID szerint deduplikálva) és kategorizálja őket."""
    if not isinstance(pnp_data, list):
        pnp_data = [pnp_data] if pnp_data else []
    seen_hwids = set()
    devices = []
    for d in pnp_data:
        n = d.get("Name") or "Ismeretlen Eszköz"
        pid = d.get("PNPDeviceID") or ""
        pclass = d.get("PNPClass") or ""
        hwids_list = d.get("HardwareID") or []
        if isinstance(hwids_list, str):
            hwids_list = [hwids_list]

        if not pid:
            continue
        if "virtual" in n.lower() or "pseudo" in n.lower() or "vmware" in n.lower():
            continue
        if pid.upper().startswith("ROOT\\"):
            continue
        if pclass in WU_SCAN_IGNORED_CLASSES:
            continue

        hwid_clean = hwids_list[0] if hwids_list else pid
        if not hwid_clean or hwid_clean in seen_hwids:
            continue
        seen_hwids.add(hwid_clean)

        if pclass == "Display": cat = "🎮 Videókártya (VGA)"
        elif pclass == "Media": cat = "🎵 Hangkártya (Audio)"
        elif pclass == "Net": cat = "🌐 Hálózat (LAN/Wi-Fi)"
        elif pclass == "Bluetooth": cat = "🔵 Bluetooth"
        elif pclass == "System": cat = "⚙️ Rendszereszköz"
        elif pclass == "USB": cat = "🔌 USB Vezérlő"
        elif pclass in ("Camera", "Image"): cat = "📷 Webkamera"
        elif pclass in ("Mouse", "Keyboard", "HIDClass"): cat = "🖱️ Periféria"
        elif pclass == "Biometric": cat = "🔒 Ujjlenyomat / Biometria"
        else: cat = f"🔧 Egyéb ({pclass})"

        devices.append({"cat": cat, "name": n, "id": hwid_clean, "pnp_id": pid, "all_hwids": hwids_list})
    return devices


def _match_wu_updates_to_devices(wu_results, devices, exclude_uids=None):
    """WU-találatok párosítása a jelenlévő eszközökhöz. A "legjobb mindkettőből" logika:
    - elsődlegesen HWID prefix-egyezés (a manuális szkennelés bizonyítottan pontos módszere;
      a substring-egyezés rövid HWID-knél - pl. "usbmmidd" - hamis találatot adhat),
    - tartalékként cím<->eszköznév egyezés (az AutoFix módszere - e nélkül a SoftwareComponent
      típusú csomagok, pl. Realtek szolgáltatások, sosem párosulnak, mert nincs a jelenlévő
      eszközökhöz köthető HWID-jük).
    Egy WU-csomag legfeljebb egyszer szerepel (UpdateID szerint deduplikálva), de egy eszközhöz
    több csomag is tartozhat. A párosítatlan (ghost) találatok kimaradnak.
    Visszatérés: [{'uid', 'title', 'device'}] lista."""
    exclude_uids = exclude_uids or set()
    matches = []
    seen_uids = set()
    for wu in wu_results:
        uid = wu.get('UpdateID')
        if not uid or uid in exclude_uids or uid in seen_uids:
            continue
        hwids = wu.get('HardwareID') or []
        if isinstance(hwids, str):
            hwids = [hwids]
        hwids_upper = [str(h).upper() for h in hwids]
        title = wu.get('Title', '') or ''

        matched_dev = None
        for dev in devices:
            dev_hwids_upper = [str(dh).upper() for dh in dev.get('all_hwids', [])]
            dev_pnp_upper = (dev.get('pnp_id') or '').upper()
            for wu_h in hwids_upper:
                if any(wu_h.startswith(dh) or dh.startswith(wu_h) for dh in dev_hwids_upper) or \
                   (dev_pnp_upper and (dev_pnp_upper.startswith(wu_h) or wu_h.startswith(dev_pnp_upper))):
                    matched_dev = dev
                    break
            if matched_dev:
                break

        if matched_dev is None:
            w_title = title.lower()
            for dev in devices:
                n_lower = (dev.get('name') or '').lower()
                if n_lower and n_lower != "ismeretlen eszköz" and len(n_lower) > 3 and \
                   (n_lower in w_title or w_title in n_lower):
                    matched_dev = dev
                    break

        if matched_dev is not None:
            seen_uids.add(uid)
            matches.append({'uid': uid, 'title': title, 'device': matched_dev})
    return matches


def _build_wu_install_ps(target_uids=(), target_hwids=(), match_system_devices=False):
    """A WUA (Microsoft.Update.Session) telepítő PowerShell script EGYETLEN forrása.
    Szűrési módok (vagylagosak egy csomagra, de kombinálhatók egy híváson belül):
    - target_uids: pontos UpdateID egyezés (manuális telepítés + GUI AutoFix),
    - target_hwids: HWID prefix-egyezés, tartalék UpdateID nélküli pool-elemekhez,
    - match_system_devices: a gép ÖSSZES jelenlévő eszközéhez párosítás a scripten belül
      (CLI AutoFix - ott nincs Python-oldali előszűrés).
    Ha egyik szűrő sincs megadva, SEMMIT nem telepít (EMPTY) - nincs "mindent telepít" mód!
    A letöltés SZINKRON $DL.Download() - SOHA ne cseréld BeginDownload($null,...)-ra, az
    null callbackekkel azonnal NullReferenceException-nel elhal (Build ~192 regresszió).
    Kimeneti protokoll (a hívók ezt parse-olják): INIT/SEARCH/FOUND/SKIP/TOTAL/DLONE/
    INSTONE/OK/FAIL/EMPTY/DONE/ERROR prefixű sorok."""
    uid_list_ps = ','.join(f"'{_ps_quote(u)}'" for u in target_uids)
    hwid_list_ps = ','.join(f"'{_ps_quote(str(h).upper())}'" for h in target_hwids)
    match_sys_ps = '$true' if match_system_devices else '$false'
    return ('$TargetUIDs = @(' + uid_list_ps + ')\n'
            '$TargetHWIDs = @(' + hwid_list_ps + ')\n'
            '$MatchSystemDevices = ' + match_sys_ps + '\n') + r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
try {
    Write-Output "INIT: Windows Update Session létrehozása..."
    $Session = New-Object -ComObject Microsoft.Update.Session
    $Searcher = $Session.CreateUpdateSearcher()
    try { $SM = New-Object -ComObject Microsoft.Update.ServiceManager; $SM.AddService2("7971f918-a847-4430-9279-4a52d1efe18d", 7, "") | Out-Null } catch {}
    $Searcher.ServerSelection = 3
    $Searcher.ServiceID = "7971f918-a847-4430-9279-4a52d1efe18d"
    Write-Output "SEARCH: Driver frissítések keresése..."
    $Result = $Searcher.Search("IsInstalled=0 and Type='Driver'")
    if ($Result.Updates.Count -eq 0) { Write-Output "EMPTY: Nem található elérhető driver frissítés."; return }

    $systemHWIDs = @()
    if ($MatchSystemDevices) {
        $pnpDevs = Get-WmiObject Win32_PnPEntity | Where-Object { $_.Present -eq $true -and $_.ConfigManagerErrorCode -ne 45 }
        foreach ($dev in $pnpDevs) {
            if ($dev.HardwareID) {
                foreach ($hid in $dev.HardwareID) { $systemHWIDs += "$hid".ToUpper() }
            }
            if ($dev.PNPDeviceID) { $systemHWIDs += "$($dev.PNPDeviceID)".ToUpper() }
        }
    }

    $ToInstall = New-Object -ComObject Microsoft.Update.UpdateColl
    foreach ($U in $Result.Updates) {
        $matchFound = $false
        if ($TargetUIDs.Count -gt 0 -and $TargetUIDs -contains $U.Identity.UpdateID) { $matchFound = $true }
        if (-not $matchFound -and $TargetHWIDs.Count -gt 0) {
            foreach ($hwid in $U.DriverHardwareID) {
                if (-not $hwid) { continue }
                $hUpper = "$hwid".ToUpper()
                foreach ($tgt in $TargetHWIDs) {
                    if ($tgt.StartsWith($hUpper) -or $hUpper.StartsWith($tgt)) {
                        $matchFound = $true; break
                    }
                }
                if ($matchFound) { break }
            }
        }
        if (-not $matchFound -and $MatchSystemDevices) {
            foreach ($hwid in $U.DriverHardwareID) {
                if (-not $hwid) { continue }
                $hUpper = "$hwid".ToUpper()
                foreach ($sys_hid in $systemHWIDs) {
                    if ($sys_hid.StartsWith($hUpper) -or $hUpper.StartsWith($sys_hid)) {
                        $matchFound = $true; break
                    }
                }
                if ($matchFound) { break }
            }
        }
        if (-not $matchFound) { Write-Output "SKIP: $($U.Title)"; continue }
        if (-not $U.EulaAccepted) { $U.AcceptEula() }
        $ToInstall.Add($U) | Out-Null
        Write-Output "FOUND: $($U.Title)"
    }
    if ($ToInstall.Count -eq 0) { Write-Output "EMPTY: Nem található egyező driver. (Lehet, hogy időközben települt vagy lekerült a szerverről - futtass új szkennelést!)"; return }
    $total = $ToInstall.Count; Write-Output "TOTAL: $total"
    $s = 0; $f = 0
    for ($i = 0; $i -lt $total; $i++) {
        $U = $ToInstall.Item($i); $t = $U.Title; $idx = $i + 1
        Write-Output "DLONE: $idx/$total $t"
        $SC = New-Object -ComObject Microsoft.Update.UpdateColl; $SC.Add($U) | Out-Null
        $DL = $Session.CreateUpdateDownloader(); $DL.Updates = $SC
        try { $DR = $DL.Download() } catch { Write-Output "FAIL: [LETÖLTÉS HIBA] $t - $($_.Exception.Message)"; $f++; continue }
        if (-not $DR -or ($DR.ResultCode -ne 2 -and $DR.ResultCode -ne 3)) { Write-Output "FAIL: [LETÖLTÉS HIBA kód=$($DR.ResultCode)] $t"; $f++; continue }
        Write-Output "INSTONE: $idx/$total $t"
        $Inst = $Session.CreateUpdateInstaller(); $Inst.Updates = $SC
        try { $IR = $Inst.Install() } catch { Write-Output "FAIL: [TELEPÍTÉS HIBA] $t"; $f++; continue }
        $rc = $IR.GetUpdateResult(0).ResultCode
        switch ($rc) { 2 { Write-Output "OK: $t"; $s++ } 3 { Write-Output "OK: $t"; $s++ } default { Write-Output "FAIL: [kód=$rc] $t"; $f++ } }
    }
    Write-Output "DONE: Sikeres=$s, Sikertelen=$f"
} catch { Write-Output "ERROR: $($_.Exception.Message)" }
"""

# Stabilitás Teszt: egyenként is indítható programok, kulcs -> (megjelenített név, a
# stresstools.zip-ben keresett fájlnév-változatok). A HDSentinel jelenléte a ZIP-től függ -
# ha nincs benne, a keresés futásidőben "nem található" hibát ad, ami nem kódhiba.
STRESS_TOOLS = {
    'furmark': ('FurMark', ['furmark.exe']),
    'prime95': ('Prime95', ['prime95.exe']),
    'linpack': ('Linpack Xtreme', ['linpackxtreme.exe', 'linpack.exe']),
    # A lista SORRENDJE itt prioritás: a 64 bites verziót preferáljuk, a 32 bites csak
    # akkor indul, ha nincs 64 bites az extracted mappában (lásd _find_stress_tool_exes).
    'hwinfo': ('HWiNFO64 (Sensor Only)', ['hwinfo64.exe', 'hwinfo32.exe']),
    'hdsentinel': ('HD Sentinel', ['hdsentinel.exe', 'hdsentinel_x64.exe', 'hdsentinel64.exe']),
}

# "Minden teszt indítása" gomb csak ezeket a valódi terhelés-generáló stressz teszteket
# indítja - a HD Sentinel egy lemez-egészség MONITOR (nem terhel semmit), ezért
# kifejezett felhasználói kérésre nem szerepel a tömeges indításban, csak egyenként
# (start_stress_tool) érhető el.
STRESS_TOOLS_BULK = ['furmark', 'prime95', 'linpack', 'hwinfo']

# A "Minden teszt bezárása" (stop_stress_tests) által név szerint is kilövendő programok -
# biztonsági háló arra az esetre, ha egy folyamatot nem az általunk eltárolt PID-fa alól
# indítottak (pl. UAC 'runas' út, ahol nincs PID-ünk, vagy kézzel indított példány). A
# Linpack tényleges terhelő motorja (linpack_amd64/intel64) és az opcionális HWMonitor a
# Linpack.exe gyerekfolyamatai - a PID-fa kilövése normál esetben elviszi őket, ez itt
# csak tartalék.
STRESS_KILL_IMAGES = [
    'furmark.exe', 'prime95.exe',
    'linpack.exe', 'linpackxtreme.exe', 'linpack_amd64.exe', 'linpack_intel64.exe',
    'linpack_amd32.exe', 'linpack_intel32.exe', 'HWMonitor_x64.exe',
    'hwinfo64.exe', 'hwinfo32.exe',
    'hdsentinel.exe', 'hdsentinel_x64.exe', 'hdsentinel64.exe',
]

# Microstore bolti hálózati nyomtató - "1 kattintás" nyomtatás a Rendszer Riporthoz
# (print_via_store_printer). SUMATRA_PDF_FILENAMES a stresstools.zip-ben keresett néma
# (dialógus nélküli) PDF-nyomtató segédprogram, HP_DRIVER_INF_FILENAMES a szintén a
# ZIP-be csomagolt HP LaserJet 1320 PCL6 driver (pnputil /export-driver-rel exporttal
# kinyerve egy már működő gépről) INF fájlja - mindkettő ugyanabból a ZIP-ből, mint a
# stabilitás-teszt eszközök, hogy ne kelljen külön letöltési URL egy-egy apró fájlért.
STORE_PRINTER_IP = "192.168.35.12"
STORE_PRINTER_PORT_NAME = "IP_192.168.35.12"
STORE_PRINTER_NAME = "Microstore Bolti Nyomtató"
# STORE_PRINTER_REFERENCE_NAME csak ott segít, ahol ÉPPEN ez a nyomtató már fel van véve
# - lásd _resolve_store_printer_driver. Terepen bizonyítva (egy random gépen tesztelve):
# sem ez a nyomtató, sem a hozzá tartozó driver NEM garantált egyetlen más gépen sem, és
# az `Add-PrinterDriver -Name` MAGÁBAN NEM tölt le semmit a Windows Update-ről (az
# interaktív "Nyomtató hozzáadása" varázsló automatikus driver-felismerése egy MÁSIK,
# PowerShell-ből el nem érhető mechanizmust használ) - csak akkor sikerül, ha a driver
# MÁR a driver store-ban van. Emiatt a becsomagolt INF-et `pnputil /add-driver`-rel kell
# előbb odastageelni, utána sikerül csak az Add-PrinterDriver/Add-Printer.
STORE_PRINTER_REFERENCE_NAME = "BOLT hp LaserJet 1320 PCL 6"
STORE_PRINTER_HP_DRIVER_NAME = "hp LaserJet 1320 PCL 6"
SUMATRA_PDF_FILENAMES = ['sumatrapdf.exe']
HP_DRIVER_INF_FILENAMES = ['hpc1320u.inf']

# Linpack Xtreme RAM-választó menüjének opciói (a program konzolos menüjéből, sorrendben):
# (menüpont szám, GB). Az automatizálás a rendszer teljes RAM-jához a legnagyobb ide illő
# (<= a ténylegesen meglévő RAM) opciót választja - lásd _pick_linpack_ram_option().
LINPACK_RAM_OPTIONS = [(1, 2), (2, 4), (3, 6), (4, 8), (5, 10), (6, 14), (7, 30)]

# GUI programok indítás utáni, egymást követő dialógusablakainak automatikus végignyomkodása
# (lásd _auto_click_sequence). Egy lépés lehet egyetlen felirat, alternatívák listája
# (localizált feliratokhoz - pl. HWiNFO a rendszer nyelvén jelenik meg, "Indítás" vagy "Start"),
# vagy egy dict az alábbi kulcsokkal:
#   'labels':        felirat(ok), amelyik gombot meg kell nyomni
#   'skip_if_found': ha a keresés közben nem a 'labels', hanem ezek egyike kerül elő, a lépés
#                    kattintás nélkül KIMARAD - a Prime95 miatt kell: a GIMPS üdvözlő ("Just
#                    Stress Testing") CSAK a legelső indításkor jelenik meg, a gomb megnyomása
#                    után a prime.txt-be írt StressTester=1 miatt minden további indítás
#                    egyből a "Run a Torture Test" dialógussal (Small FFTs rádiógomb) kezdődik
#   'optional':      ha a lépés dialógusa a saját timeoutján belül nem jelenik meg, az NEM
#                    hiba - a lépés kimarad, a sorozat nem szakad meg
#   'timeout':       a lépés saját keresési időkorlátja mp-ben (alapértelmezés: 60)
#   'exact':         csak TELJES felirat-egyezés számít (rövid feliratoknál - 'OK', 'Igen' -
#                    véd a részleges hamis találatoktól, pl. 'ventilátorok' vége 'ok')
STRESS_CLICK_SEQUENCES = {
    'furmark': ['GPU stress test', 'GO'],  # beállító-ablak -> "*** CAUTION ***" figyelmeztetés
    'prime95': [
        {'labels': ['Just Stress Testing'], 'skip_if_found': ['small ffts (tests l1/l2/l3']},  # GIMPS üdvözlő (csak első indításkor)
        'small ffts (tests l1/l2/l3',  # torture test típus rádiógomb
        'OK',
    ],
    'hwinfo': [
        ['Indítás', 'Start'],  # a HWiNFO64.INI SensorsOnly=1 már kiválasztja a módot
        # Indítás után a HWiNFO még feldobhat egy ablakot: terepen (debug leltárból
        # azonosítva) ez a "HWiNFO® 64 Update" frissítés-értesítő volt, aminek a gombja
        # 'Bezárás'/'Close' - de más megerősítő popup (OK/Igen/Yes gombbal) is előfordulhat.
        # Ha 20 mp-en belül megjelenik ezek egyike, lenyomjuk; ha nem, a lépés hang nélkül
        # kimarad. (Az INI-be írt CheckForUpdate=0 elvileg magát az update-ablakot is
        # letiltja - ez a lépés a biztonsági háló, ha az INI-kulcsot nem venné figyelembe.)
        {'labels': ['OK', 'Igen', 'Yes', 'Bezárás', 'Close'], 'optional': True, 'timeout': 20, 'exact': True},
    ],
}

# A Linpack Xtreme v1.1.8 konzolos stressz-teszt menüjének (valódi gépen, a konzol
# képernyőpufferét kiolvasva ÉS a Linpack.exe-be csomagolt .bat forrását elemezve
# ellenőrzött) prompt-sorrendje: (prompt-részlet, válasz, kell-e Enter) hármasok.
# Az automatizálás (_auto_answer_console) minden válasz elküldése ELŐTT megvárja, hogy a
# hozzá tartozó prompt ténylegesen megjelenjen a konzol képernyőjén - vakon, fix időzítéssel
# gépelve egy leterhelt gépen (ahol a menü több mp késéssel jön elő) a válaszok rossz
# prompthoz érkeznek, és a teszt el sem indul.
#
# A "kell-e Enter" flag NEM opcionális finomság: a Linpack indítója egy .bat, amiben a
# menük/kérdések 'choice' paranccsal olvasnak (EGYETLEN billentyű, Enter nélkül), a
# futásszám viszont 'set /p'-vel (teljes sor Enterrel). Ha egy choice-os menünek Enterrel
# együtt küldjük a választ, a choice csak a billentyűt fogyasztja el, az Enter a konzol
# pufferében marad, és a következő 'set /p' üres sorként olvassa be -> a batch
# "if %RUNS% LSS 1" sora szintaktikai hibává válik, a cmd az EGÉSZ szkriptet megszakítja,
# és a Linpack ablaka szó nélkül eltűnik ~1 mp-cel a RAM-válasz után (valós gépen
# bizonyított, sokáig érthetetlen "összeomlás"). A RAM-opció válasza futásidőben kerül a
# listába (lásd _build_linpack_console_script). Üres válasz = csak Enter.
LINPACK_PROMPT_SCRIPT = [
    ('select an action', '2', False),            # főmenü (choice): 2 = Stress Test
    ('amount of ram', None, False),              # RAM-menü (choice): futásidőben kiválasztott opciószám
    ('number of times to run', '10000', True),   # futásszám (set /p!): gyakorlatilag "amíg le nem állítják"
    ('all available threads', 'Y', False),       # choice: minden szál használata
    ('disable sleep mode', 'N', False),          # choice: alvó módot az app maga tiltja (_lock_power_for_stress)
    ('hwmonitor', 'N', False),                   # choice: CPUID HWMonitor nem kell, fut a HWiNFO
    ('press any key', '', True),                 # pause: Enter, ezután indul a teszt
]


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


# Temp Törlés funkció - modul-szintű (nem osztálymetódus) segédfüggvények, mert a
# DriverToolApi (GUI) és a CliApi (szöveges menü) egymástól független osztályok, de
# mindkettőnek ugyanez kell (ld. clean_temp_files mindkét osztályban) - így egy helyen
# módosítva nem tud a kettő szétdriftelni.
def _fmt_bytes(n):
    """Bájt -> emberi olvasható méret (KB/MB/GB) - a Temp Törlés progress-logjához/kiírásához."""
    n = float(max(n, 0))
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f"{int(n)} B" if unit == 'B' else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _clean_folder_contents(folder, cancel_check=None):
    """Egy mappa TARTALMÁNAK (nem magának a mappának) törlése, elemenként próbálkozva -
    így egy zárolt (épp használatban lévő) fájl/almappa nem akasztja meg a többi elem
    törlését, csak kimarad a számlálásból. cancel_check: opcionális () -> bool (a GUI
    megszakítás-jelzőjéhez) - CLI hívásnál nincs ilyen, ott sosem szakad meg félúton.
    Visszaadja: (felszabadított_bájt, törölt_elemek, kihagyott_elemek)."""
    freed = 0
    removed = 0
    failed = 0
    if not folder or not os.path.isdir(folder):
        return freed, removed, failed
    try:
        entries = os.listdir(folder)
    except Exception as e:
        logging.warning(f"[TEMPCLEAN] Mappa nem olvasható ({folder}): {e}")
        return freed, removed, failed
    for name in entries:
        if cancel_check and cancel_check():
            break
        full = os.path.join(folder, name)
        try:
            if os.path.isdir(full) and not os.path.islink(full):
                size = 0
                for root, _dirs, files in os.walk(full):
                    for f in files:
                        try:
                            size += os.path.getsize(os.path.join(root, f))
                        except OSError:
                            pass
                shutil.rmtree(full)
            else:
                size = os.path.getsize(full)
                os.remove(full)
            freed += size
            removed += 1
        except Exception as e:
            failed += 1
            logging.debug(f"[TEMPCLEAN] Nem törölhető ({full}): {e}")
    return freed, removed, failed


def _empty_recycle_bin():
    """Lomtár ürítése SHEmptyRecycleBinW-vel. Előtte SHQueryRecycleBinW-vel lekérdezi a
    méretét (bájtban), mert az ürítő hívás magától nem adja vissza, mennyi hely szabadult
    fel - ez csak a progress-logban/kiírásban megjelenő becsléshez kell, az ürítés akkor
    is lefut, ha a lekérdezés valamiért hibázna."""
    freed = 0
    try:
        info = _SHQUERYRBINFO()
        info.cbSize = ctypes.sizeof(_SHQUERYRBINFO)
        hr = ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info))
        if hr == 0:
            freed = info.i64Size
    except Exception as e:
        logging.debug(f"[TEMPCLEAN] SHQueryRecycleBinW hiba: {e}")
    try:
        SHERB_NOCONFIRMATION = 0x00000001
        SHERB_NOPROGRESSUI = 0x00000002
        SHERB_NOSOUND = 0x00000004
        ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND)
    except Exception as e:
        logging.warning(f"[TEMPCLEAN] SHEmptyRecycleBinW hiba: {e}")
    return freed


def _temp_clean_category_defs(sys_drive):
    """Temp Törlés kategóriák definíciója - EGY helyen (a GUI clean_temp_files és a CLI
    clean_temp_files is ezt hívja), hogy a kettő ne driftelhessen szét. Elemek:
    (kulcs, címke, [törlendő mappák], [leállítandó szolgáltatások], alapból_bepipálva).
    A 3 "alapból_bepipálva" kategória (user_temp/windows_temp/wu_cache) a törzs-tartalom,
    a többi opcionális extra - felhasználói kérésre bővítve, de tudatosan kikapcsolva
    alapból, mert vagy specifikusabb (pl. csak DirectX-es játékosoknak van D3DSCache-e),
    vagy csak diagnosztikai adat (CBS log, crash dump), amit nem mindenki akar automatikusan
    elveszíteni."""
    import tempfile
    local = os.environ.get('LOCALAPPDATA', '')
    wer_base = os.path.join(sys_drive, 'ProgramData', 'Microsoft', 'Windows', 'WER')
    return [
        ('user_temp', '👤 Felhasználói TEMP mappa (%TEMP%)',
         [tempfile.gettempdir()], [], True),
        ('windows_temp', '🖥️ Rendszer TEMP mappa (Windows\\Temp)',
         [os.path.join(sys_drive, 'Windows', 'Temp')], [], True),
        ('wu_cache', '🔄 Windows Update letöltési gyorsítótár',
         [os.path.join(sys_drive, 'Windows', 'SoftwareDistribution', 'Download')], ['wuauserv', 'bits'], True),
        ('delivery_opt', '📦 Delivery Optimization gyorsítótár',
         [os.path.join(sys_drive, 'Windows', 'SoftwareDistribution', 'DeliveryOptimization')], ['DoSvc'], False),
        ('wer', '⚠️ Hibajelentések (Windows Error Reporting)',
         [os.path.join(wer_base, 'ReportQueue'), os.path.join(wer_base, 'ReportArchive')], [], False),
        ('shader_cache', '🎮 DirectX Shader Cache',
         [os.path.join(local, 'D3DSCache')] if local else [], [], False),
        ('cbs_logs', '📜 Windows telepítési naplók (CBS logok)',
         [os.path.join(sys_drive, 'Windows', 'Logs', 'CBS')], [], False),
        ('crash_dumps', '💥 Programösszeomlás-dumpok (Crash Dumps)',
         [os.path.join(local, 'CrashDumps')] if local else [], [], False),
        ('inet_cache', '🌐 Internet Explorer/Edge (legacy) gyorsítótár',
         [os.path.join(local, 'Microsoft', 'Windows', 'INetCache')] if local else [], [], False),
        # Színprofil-mappa: főleg nyomtatódriverek (tipikusan HP) szemetelik tele akár
        # több GB ICC-profillal. A Spoolert leállítjuk törlés előtt, mert a nyomtató-
        # driverek profiljait zárolhatja. A Windows beépített profiljai (pl. sRGB) is
        # törlődnek, ezért csak opt-in extra - a driverek újratelepítése visszateszi őket.
        ('color_profiles', '🎨 Színprofilok (spool\\drivers\\color)',
         [os.path.join(sys_drive, 'Windows', 'System32', 'spool', 'drivers', 'color')], ['Spooler'], False),
    ]


def _app_data_dir():
    """A DriverVarázsló saját adatmappája (debug log, HTML rendszer riportok) - a
    rendszerlemez gyökerében, NEM a program (exe) mellett. Így mindig ugyanott van (a
    felhasználó/szerviz megszokhatja, hova nézzen), függetlenül attól, honnan futtatják
    épp az exe-t (Asztal, letöltések mappa, USB stick, hálózati megosztás - utóbbi kettő
    akár írásvédett is lehet, ahova maga az exe mellé semmiképp nem tudna írni)."""
    sys_drive = os.environ.get('SystemDrive', 'C:') + '\\'
    path = os.path.join(sys_drive, 'DriverVarazslo')
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path


# Net Blokkoló script (block.bat) - GitHub release-ből letölthető batch script, ami a
# saját mappájában (és almappáiban) lévő összes .exe kimenő internet-elérését letiltja a
# Windows tűzfalban (netsh advfirewall). A program CSAK letölti a _app_data_dir()
# mappába, NEM futtatja. Szándékosan .bat és nem .ps1: a batch fájlokra nem vonatkozik a
# PowerShell execution policy (ami alapértelmezetten Restricted, azaz a sima dupla katt
# / "Futtatás PowerShell-lel" egy azonnal bezáruló ablakkal elhal - terepen bizonyított),
# és a .bat magától kéri az admin jogot (UAC) is, így az ügyfélgépen tényleg csak dupla
# kattintás kell hozzá.
BLOCK_SCRIPT_URL = "https://github.com/egonixaimgod/DriverVarazslo/releases/download/block.bat/block.bat"


def _download_block_script(run_fn):
    """Letölti a block.bat scriptet a _app_data_dir() mappába - EGY helyen (a GUI
    download_block_script és a CLI download_block_script is ezt hívja), hogy a kettő ne
    driftelhessen szét. Visszaadja a mentett fájl teljes útvonalát, hibánál kivételt dob.
    run_fn: a hívó API-osztály _run metódusa (a friss-Windows tanúsítvány-fallbackhez)."""
    import urllib.request, urllib.error, ssl
    dest = os.path.join(_app_data_dir(), 'block.bat')
    logging.info("[BLOCK-SCRIPT] Letöltés INNEN: " + BLOCK_SCRIPT_URL)
    ssl_ctx = ssl.create_default_context()
    req = urllib.request.Request(BLOCK_SCRIPT_URL, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
    try:
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=60) as resp, open(dest, 'wb') as f:
            shutil.copyfileobj(resp, f)
    except urllib.error.URLError as dl_err:
        # Ugyanaz a friss-Windows gyökértanúsítvány-probléma, mint a _download_stresstools-nál
        # (a github.com Sectigo/USERTrust gyökere hiányzik a vadonatúj gép tárából, amit csak
        # schannel-kliens tölt le igény szerint) - ezért CSAK erre a hibára esünk vissza
        # PowerShell Invoke-WebRequest-re, teljes tanúsítvány-ellenőrzéssel (SEMMIT nem
        # kapcsolunk ki!).
        if 'CERTIFICATE_VERIFY_FAILED' not in str(dl_err):
            raise
        logging.warning(f"[BLOCK-SCRIPT] Python SSL tanúsítvány-hiba ({dl_err}) - áttérés PowerShell (schannel) letöltésre, teljes tanúsítvány-ellenőrzéssel...")
        ps_cmd = ("$ProgressPreference='SilentlyContinue'; "
                  "[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072; "
                  f"Invoke-WebRequest -Uri '{_ps_quote(BLOCK_SCRIPT_URL)}' -OutFile '{_ps_quote(dest)}' -UseBasicParsing")
        result = run_fn(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps_cmd], timeout=120)
        if not result or result.returncode != 0 or not os.path.exists(dest):
            raise Exception("A letöltés sikertelen (nincs internet, vagy a GitHub nem elérhető).")
    if not os.path.exists(dest) or os.path.getsize(dest) == 0:
        raise Exception("A letöltött fájl üres vagy hiányzik.")
    logging.info(f"[BLOCK-SCRIPT] Letöltve: {dest}")
    return dest


# Stabilitás Teszt közben letiltandó energiagazdálkodási beállítások (powercfg alias-ok -
# ezek a kulcsszavak nyelvfüggetlenek, minden Windows-nyelven ugyanígy kell megadni őket).
# SUB_VIDEO/VIDEOIDLE = kijelző kikapcsolása, SUB_SLEEP/STANDBYIDLE = alvó mód,
# SUB_SLEEP/HIBERNATEIDLE = hibernálás.
STRESS_POWER_SETTINGS = [('SUB_VIDEO', 'VIDEOIDLE'), ('SUB_SLEEP', 'STANDBYIDLE'), ('SUB_SLEEP', 'HIBERNATEIDLE')]
STRESS_POWER_REG_KEY = r"SOFTWARE\DriverVarazslo\StressPowerBackup"


class DriverToolApi:
    def __init__(self):
        logging.info("[INIT] DriverToolApi inicializálás...")
        self._window = None
        self.target_os_path = None
        self.sys_drive = os.environ.get('SystemDrive', 'C:') + '\\'
        self.hw_updates_pool = []
        self._hw_installed_devs = []
        self._hw_scanning = False
        self._hw_loaded = False
        self.wu_api_mode = True
        self._cancel_flag = False  # Flag for cancelling long-running tasks
        self._task_busy = None  # None, vagy a jelenleg futó feladat neve (lásd _safe_thread)
        self._stresstools_download_lock = threading.Lock()
        self._console_attach_lock = threading.Lock()
        self._stress_pids = {}  # az általunk indított stressz-programok PID-jei (stop_stress_tests-hez)
        self._last_report_path = None  # a legutóbb generált Rendszer Riport útvonala (print_via_store_printer-hez)
        self.resume_mode = '--resume-autofix' in sys.argv
        self.resume_step1 = '--resume-step1' in sys.argv
        self.skip_printer_drivers = '--skip-printer-drivers' in sys.argv
        self._si = subprocess.STARTUPINFO()
        self._si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        self._nw = subprocess.CREATE_NO_WINDOW
        logging.info(f"[INIT] sys_drive={self.sys_drive}")
        
        # Takarítás: frissítés utáni régi .exe törlése
        try:
            exe_path = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
            old_path = exe_path + ".old"
            if os.path.exists(old_path):
                os.remove(old_path)
                logging.info("[INIT] Régi verzió (update előtti) törölve.")
        except Exception:
            pass

        # Ha egy korábbi Stabilitás Teszt indítás letiltotta a képernyő-kikapcsolást/alvó
        # módot, itt, a program (újra)indulásakor állítjuk vissza az akkor elmentett eredeti
        # energiagazdálkodási beállításokat (lásd _lock_power_for_stress/_restore_power_after_stress).
        self._restore_power_after_stress()

        logging.info("[INIT] DriverToolApi kész.")

    def set_window(self, window):
        logging.info("[WINDOW] WebView ablak beállítása...")
        self._window = window
        # Wait for WebView2 DOM to be ready (max 12s, watchdog timeout: 60s)
        dom_ready = False
        for i in range(120):  # 120 * 0.1s = 12s
            try:
                if self._window and self._window.evaluate_js('1+1') == 2:
                    logging.info(f"[WINDOW] WebView2 DOM kész ({i+1} próba után, {(i+1)*0.1:.1f}s)")
                    dom_ready = True
                    _webview_ready.set()
                    break
            except Exception as e:
                if i == 119:
                    logging.warning(f"[WINDOW] WebView2 DOM nem reagál: {e}")
            time.sleep(0.1)
        if not dom_ready:
            logging.error("[WINDOW] WebView2 init sikertelen, watchdog átveszi...")
            _webview_error.set()

    def emit(self, event, data=None):
        # Log minden emit event-et
        try:
            if isinstance(data, dict):
                log_msg = data.get('log') or data.get('status') or data.get('error') or data.get('phase')
                if log_msg:
                    logging.info(f"[EMIT:{event}] {str(log_msg).strip()}")
                else:
                    # Log egyéb data mezőket is
                    logging.debug(f"[EMIT:{event}] data={json.dumps(data, ensure_ascii=False, default=str)[:200]}")
            else:
                logging.debug(f"[EMIT:{event}] data={data}")
        except Exception as e:
            logging.warning(f"[EMIT] Logging hiba: {e}")

        if self._window:
            payload = None
            try:
                payload = json.dumps({"event": event, "data": data}, ensure_ascii=False, default=str)
                # U+2028/U+2029 a JSON-ban érvényes, de egy JS string-literálba nyers szövegként
                # beillesztve (nem JSON.parse-on át) sor-terminátornak számíthat és megszakíthatja
                # a generált window.handlePyEvent(...) hívást - escape-eljük explicit \uXXXX-ként.
                payload = payload.replace(' ', '\\u2028').replace(' ', '\\u2029')
                self._window.evaluate_js(f'window.handlePyEvent({payload})')
            except Exception as e:
                if 'NoneType' in str(e) and payload:
                    logging.warning(f"[EMIT:{event}] Window None, újrapróbálás...")
                    time.sleep(0.5)
                    try:
                        self._window.evaluate_js(f'window.handlePyEvent({payload})')
                    except Exception as e2:
                        logging.error(f"[EMIT:{event}] Újrapróbálás sikertelen: {e2}")
                elif payload is None:
                    logging.error(f"[EMIT:{event}] JSON serializálási hiba: {e}")
                else:
                    logging.error(f"[EMIT:{event}] Hiba: {e}")

    def _run(self, cmd, **kwargs):
        # Log minden parancs futtatását
        cmd_str = cmd if isinstance(cmd, str) else ' '.join(str(c) for c in cmd)
        logging.debug(f"[CMD] Futtatás: {cmd_str[:300]}")
        # stdin alapból DEVNULL: egyik parancsunk sem olvas stdin-t, VISZONT a stressz-teszt
        # automatizálás AttachConsole/FreeConsole hívásai után a folyamat örökölt stdin
        # handle-je érvénytelenné válik, és az örökölt-stdin + capture_output kombináció
        # ettől kezdve MINDEN parancsindítást "[WinError 6] A leíró érvénytelen" hibával
        # buktatna el (terepen bizonyított: stressz teszt után a taskkill sem futott le).
        kwargs.setdefault('stdin', subprocess.DEVNULL)
        start = time.time()
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, errors='replace',
                                  startupinfo=self._si, creationflags=self._nw, **kwargs)
            elapsed = time.time() - start
            # Log eredmény
            if result.returncode != 0:
                logging.warning(f"[CMD] Visszatérési kód: {result.returncode} ({elapsed:.1f}s)")
                if result.stderr:
                    logging.warning(f"[CMD] stderr: {result.stderr[:4000]}")
            else:
                logging.debug(f"[CMD] OK ({elapsed:.1f}s)")
            
            # Log teljes kimenet 4000 karakterig
            if result.stdout:
                out_txt = result.stdout.strip()
                if len(out_txt) > 4000: out_txt = out_txt[:4000] + '... [TRUNCATED]'
                logging.debug(f"[CMD] stdout: {out_txt}")
            return result
        except Exception as e:
            logging.error(f"[CMD] Kivétel: {e}")
            raise

    def _safe_thread(self, task, target):
        """Háttérszálon futtatja a target()-et.

        Egyszerre csak EGY ilyen feladat futhat: ha már fut egy másik, ezt elutasítjuk,
        mert két egyidejű feladat egyébként ütközne a közös self._cancel_flag-en (egy
        épp induló új feladat False-ra állítaná a MÁR futó feladat megszakítás-kérését),
        és pl. a hardver-scan/driver-telepítés is ugyanazt a self.hw_updates_pool listát
        írná-olvasná egyszerre. A _cancel_flag reset-jét is itt, a busy-check UTÁN
        végezzük - ha korábban minden hívó metódus saját maga nullázta a flaget MIELŐTT
        idekerült volna, egy elutasított (busy) próbálkozás is csendben visszavonta volna
        a ténylegesen futó feladat megszakítás-kérését.
        """
        if self._task_busy:
            logging.warning(f"[THREAD:{task}] Elutasítva - már fut egy másik feladat ({self._task_busy}).")
            self.emit('toast', {'message': f'⚠️ Már folyamatban van egy másik művelet ({self._task_busy}), várd meg amíg befejeződik!', 'type': 'warning'})
            return
        self._task_busy = task
        self._cancel_flag = False

        def wrapper():
            logging.info(f"[THREAD:{task}] Háttérszál indul...")
            start_time = time.time()
            try:
                target()
                elapsed = time.time() - start_time
                logging.info(f"[THREAD:{task}] Befejezve ({elapsed:.1f}s)")
            except Exception as e:
                elapsed = time.time() - start_time
                logging.error(f"[THREAD:{task}] HIBA ({elapsed:.1f}s): {e}")
                logging.error(f"[THREAD:{task}] Traceback:\n{traceback.format_exc()}")
                self.emit('task_error', {'task': task, 'error': str(e)})
                self.emit('task_complete', {'task': task, 'status': f'❌ Hiba: {e}'})
            finally:
                self._task_busy = None
        threading.Thread(target=wrapper, daemon=True).start()

    # ================================================================
    # GENERAL
    # ================================================================

    def js_log(self, level, msg):
        # UI-bol jovo nyers JavaScript logok kozvetitess
        level = str(level).upper()
        if level == 'ERROR': log_lvl = logging.ERROR
        elif level == 'WARN' or level == 'WARNING': log_lvl = logging.WARNING
        elif level == 'DEBUG': log_lvl = logging.DEBUG
        else: log_lvl = logging.INFO
        logging.log(log_lvl, f"[JS_UI] {msg}")

    def check_for_updates(self):
        """Update-ellenőrzés a GitHub-on lévő driver_tool.py BUILD_NUMBER-je alapján.
        Legfeljebb UPDATE_CHECK_ATTEMPTS-szor próbálkozik, próbálkozások közt
        UPDATE_CHECK_RETRY_SEC másodperc szünettel - a raw.githubusercontent.com egy
        Fastly CDN mögött fut, aminek edge-cache-e egy friss push után még percekig a
        RÉGI tartalmat adhatja vissza (a lekérésbe rakott ?t=<timestamp> csak a
        kliens/böngésző-oldali cache-t kerüli meg, a CDN edge-cache-ét nem) - emiatt
        közvetlenül egy rebuild.bat futtatása utáni azonnali indításkor az első próbálkozás
        könnyen a még nem frissült verziót kaphatja vissza. Ez a retry nem garantált fix
        (a CDN-lag néha percekben mérhető, ennél tovább a felhasználót nem várakoztatjuk
        induláskor), de a gyakori pár-másodperces eseteket lefedi."""
        logging.info("[UPDATE] check_for_updates()")
        import urllib.request
        import ssl
        ssl_ctx = ssl.create_default_context()
        UPDATE_CHECK_ATTEMPTS = 3
        UPDATE_CHECK_RETRY_SEC = 3
        for attempt in range(1, UPDATE_CHECK_ATTEMPTS + 1):
            try:
                # Bypassing GitHub cache with a timestamp
                url = f"https://raw.githubusercontent.com/egonixaimgod/DriverVarazslo/main/driver_tool.py?t={int(time.time())}"
                logging.info(f"[UPDATE] Update ellenőrzése erről a címről ({attempt}/{UPDATE_CHECK_ATTEMPTS}. próbálkozás): {url}")
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
                with urllib.request.urlopen(req, context=ssl_ctx, timeout=10) as resp:
                    content = resp.read().decode('utf-8')
                m = re.search(r'^BUILD_NUMBER\s*=\s*(\d+)', content, re.MULTILINE)
                if m:
                    new_build = int(m.group(1))
                    logging.info(f"[UPDATE] Letöltött BUILD_NUMBER: {new_build}, Helyi: {BUILD_NUMBER}")
                    if new_build > BUILD_NUMBER:
                        logging.info(f"[UPDATE] Új verzió elérhető: {new_build} (Jelenlegi: {BUILD_NUMBER})")
                        return {'has_update': True, 'new_version': new_build}
                    else:
                        logging.info("[UPDATE] Nincs újabb verzió.")
                else:
                    logging.error("[UPDATE] Nem található BUILD_NUMBER a letöltött fájlban!")
            except Exception:
                logging.error(f"[UPDATE] Ellenőrzési hiba ({attempt}/{UPDATE_CHECK_ATTEMPTS}. próbálkozás):", exc_info=True)
            if attempt < UPDATE_CHECK_ATTEMPTS:
                time.sleep(UPDATE_CHECK_RETRY_SEC)
        return {'has_update': False}

    def perform_update(self):
        logging.info("[UPDATE] perform_update indítása...")
        def worker():
            try:
                self.emit('task_start', {'task': 'update', 'title': 'Program Frissítése'})
                self.emit('task_progress', {'task': 'update', 'log': 'Új verzió letöltése GitHubról...', 'indeterminate': True})
                import tempfile
                import urllib.request
                import ssl
                import shutil
                ssl_ctx = ssl.create_default_context()

                exe_url = f"https://raw.githubusercontent.com/egonixaimgod/DriverVarazslo/main/dist/DriverVarazslo.exe?t={int(time.time())}"
                # WinPE-ben a %TEMP% az X: RAM-diskre mutat - a letöltött exe-t a valódi C: meghajtóra tesszük.
                is_pe = os.environ.get('SystemDrive', 'C:') == 'X:'
                if is_pe:
                    temp_dir = r'C:\DV_Temp'
                    os.makedirs(temp_dir, exist_ok=True)
                else:
                    temp_dir = tempfile.gettempdir()
                new_exe = os.path.join(temp_dir, f"DriverVarazslo_Update_{int(time.time())}.exe")
                
                logging.info(f"[UPDATE] EXE letöltése innen: {exe_url}")
                logging.info(f"[UPDATE] Cél fájl: {new_exe}")
                
                req = urllib.request.Request(exe_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
                with urllib.request.urlopen(req, context=ssl_ctx, timeout=60) as resp, open(new_exe, 'wb') as f:
                    shutil.copyfileobj(resp, f)
                    
                downloaded_size = os.path.getsize(new_exe)
                logging.info(f"[UPDATE] EXE letöltve. Fájlméret: {downloaded_size} byte.")
                if downloaded_size < 1000000: # Ha kevesebb mint 1 MB, valószínűleg nem jó a letöltés
                    logging.warning("[UPDATE] A letöltött fájl gyanúsan kicsi! Lehet, hogy hiba történt vagy 404 oldalt töltött le.")
                
                self.emit('task_progress', {'task': 'update', 'log': '✅ Letöltés kész! A program frissítése és újraindítása következik...'})
                time.sleep(2)
                
                current_exe = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
                logging.info(f"[UPDATE] Jelenlegi futtatható fájl: {current_exe}")
                
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as key:
                        desktop_dir, _ = winreg.QueryValueEx(key, "Desktop")
                except Exception:
                    desktop_dir = os.path.join(os.environ.get('USERPROFILE', 'C:\\'), 'Desktop')
                    
                desktop_exe = os.path.join(desktop_dir, "DriverVarazslo.exe")
                logging.info(f"[UPDATE] Asztali elérési út: {desktop_exe}")
                
                bat_path = os.path.join(temp_dir, f"dv_update_{int(time.time())}.bat")
                # A ".old" biztonsági másolatokat sosem olvassuk vissza (nincs rollback funkció),
                # kizárólag a következő indításkori törlésre szolgálnak - ha viszont a program
                # legközelebb egy MÁSIK elérési útról indul (pl. nem az asztalról), a __init__-beli
                # takarítás sosem találja meg és törli őket, és örökre a lemezen maradnak. Ezért itt,
                # helyben (és csak sikeres másolás esetén) rögtön eltávolítjuk mindkettőt.
                bat_content = f"""@echo off
set _MEIPASS2=
set _MEIPASS=
set _PYIBoot_Pkg_ID=
timeout /t 3 /nobreak > nul

if /I not "{current_exe}"=="{desktop_exe}" (
    move /y "{current_exe}" "{current_exe}.old" > nul 2>&1
    copy /y "{new_exe}" "{current_exe}" > nul 2>&1
    if not errorlevel 1 del /f /q "{current_exe}.old" > nul 2>&1
)

move /y "{desktop_exe}" "{desktop_exe}.old" > nul 2>&1
copy /y "{new_exe}" "{desktop_exe}" > nul 2>&1
if not errorlevel 1 del /f /q "{desktop_exe}.old" > nul 2>&1

start "" "{desktop_exe}"
del "%~f0"
"""
                logging.info(f"[UPDATE] .bat fájl írása: {bat_path}")
                with open(bat_path, 'w', encoding='utf-8') as f:
                    f.write(bat_content)

                env = os.environ.copy()
                keys_to_remove = [k for k in env.keys() if k.startswith('_MEI') or k.startswith('_PYI')]
                for k in keys_to_remove:
                    env.pop(k, None)
                
                logging.info("[UPDATE] .bat fájl elindítása és program bezárása...")
                subprocess.Popen(["cmd.exe", "/c", bat_path],
                                 creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NO_WINDOW,
                                 env=env)
                os._exit(0)
            except Exception as e:
                logging.error(f"[UPDATE] Hiba a letöltés/frissítés során:", exc_info=True)
                self.emit('task_error', {'task': 'update', 'error': str(e)})
        self._safe_thread('update', worker)

    def get_init_data(self):
        logging.info(f"[API] get_init_data() hívás - build={BUILD_NUMBER}, target={self.target_os_path}")
        return {'build': BUILD_NUMBER, 'sys_drive': self.sys_drive, 'target_os': self.target_os_path, 'resume_mode': getattr(self, 'resume_mode', False), 'resume_step1': getattr(self, 'resume_step1', False), 'app_data_dir': _app_data_dir()}

    def reboot_system(self):
        logging.info("[API] reboot_system() - Felhasználó újraindítást kért")
        self._run(['shutdown', '/r', '/t', '0', '/f'])
        return True

    def open_defender(self):
        logging.info("[API] open_defender() - Windows Defender megnyitása")
        self._run(['start', 'windowsdefender://threat'], shell=True)
        return True

    def cancel_task(self):
        """API hívás a hosszan tartó műveletek (pl. törlés) megszakítására."""
        logging.warning("[API] cancel_task() — Felhasználó megszakítást kért!")
        self._cancel_flag = True
        self.emit('toast', {'message': '⚠️ Megszakítás kérve...', 'type': 'warning'})
        return True

    # ================================================================
    # STABILITÁS TESZT - energiagazdálkodás (képernyő/alvó mód letiltása közben)
    # ================================================================
    def _query_power_setting(self, subgroup, setting):
        """(ac_másodperc, dc_másodperc) lekérdezése egy powercfg alias-párra.

        A SUB_VIDEO/VIDEOIDLE stb. alias-kulcsszavak nyelvfüggetlenek, de a `powercfg
        /query` kimenetének felirat-szövegei lokalizáltak lehetnek - ezért nem szöveges
        mintára illesztünk, hanem a kimenetben szereplő "0x..." hexa értékeket szedjük ki
        POZÍCIÓ szerint (a sorrend - előbb AC, utána DC - nem nyelvfüggő)."""
        try:
            res = self._run(['powercfg', '/query', 'SCHEME_CURRENT', subgroup, setting])
            hexes = re.findall(r'0x[0-9a-fA-F]+', res.stdout or '')
            if len(hexes) >= 2:
                return int(hexes[0], 16), int(hexes[1], 16)
        except Exception as e:
            logging.warning(f"[STRESS_POWER] Lekérdezési hiba ({subgroup}/{setting}): {e}")
        return None, None

    def _set_power_setting(self, subgroup, setting, ac_seconds, dc_seconds):
        self._run(['powercfg', '/setacvalueindex', 'SCHEME_CURRENT', subgroup, setting, str(ac_seconds)])
        self._run(['powercfg', '/setdcvalueindex', 'SCHEME_CURRENT', subgroup, setting, str(dc_seconds)])

    def _lock_power_for_stress(self):
        """Letiltja a kijelző kikapcsolását és az alvó/hibernálás módot (AC és DC is), amíg
        a stressz-teszt programok futnak - enélkül a gép/kijelző elalhatna egy hosszú, több
        órás stabilitás-teszt közben. Az EREDETI értékeket (csak ha még nincs korábbi
        mentés) elmentjük a registrybe, hogy a program legközelebbi indításakor
        (_restore_power_after_stress, hívva __init__-ből) visszaállíthassuk őket."""
        try:
            try:
                already_saved = True
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, STRESS_POWER_REG_KEY, 0, winreg.KEY_READ):
                    pass
            except FileNotFoundError:
                already_saved = False

            if not already_saved:
                with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, STRESS_POWER_REG_KEY, 0, winreg.KEY_WRITE) as key:
                    for subgroup, setting in STRESS_POWER_SETTINGS:
                        ac, dc = self._query_power_setting(subgroup, setting)
                        if ac is not None:
                            winreg.SetValueEx(key, f'{subgroup}_{setting}_AC', 0, winreg.REG_DWORD, ac)
                        if dc is not None:
                            winreg.SetValueEx(key, f'{subgroup}_{setting}_DC', 0, winreg.REG_DWORD, dc)
                logging.info("[STRESS_POWER] Eredeti energiagazdálkodási beállítások elmentve.")

            for subgroup, setting in STRESS_POWER_SETTINGS:
                self._set_power_setting(subgroup, setting, 0, 0)
            self._run(['powercfg', '/setactive', 'SCHEME_CURRENT'])
            logging.info("[STRESS_POWER] Képernyő-kikapcsolás és alvó mód letiltva a stressz teszt idejére.")
        except Exception as e:
            logging.warning(f"[STRESS_POWER] Letiltási hiba: {e}")

    def _restore_power_after_stress(self):
        """A program indulásakor hívva: ha egy korábbi Stabilitás Teszt futás során
        elmentettük az eredeti energiagazdálkodási értékeket, itt visszaállítjuk őket, majd
        töröljük a mentést, hogy egy következő stressz teszt friss eredeti állapotot
        mentsen el (ne egy már egyszer nullázott értéket)."""
        try:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, STRESS_POWER_REG_KEY, 0, winreg.KEY_READ) as key:
                    values = {}
                    i = 0
                    while True:
                        try:
                            name, value, _ = winreg.EnumValue(key, i)
                            values[name] = value
                            i += 1
                        except OSError:
                            break
            except FileNotFoundError:
                return

            restored_any = False
            for subgroup, setting in STRESS_POWER_SETTINGS:
                ac = values.get(f'{subgroup}_{setting}_AC')
                dc = values.get(f'{subgroup}_{setting}_DC')
                if ac is not None and dc is not None:
                    self._set_power_setting(subgroup, setting, ac, dc)
                    restored_any = True
            if restored_any:
                self._run(['powercfg', '/setactive', 'SCHEME_CURRENT'])
                logging.info("[STRESS_POWER] Eredeti energiagazdálkodási beállítások visszaállítva.")

            try:
                winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, STRESS_POWER_REG_KEY)
            except Exception:
                pass
        except Exception as e:
            logging.warning(f"[STRESS_POWER] Visszaállítási hiba: {e}")

    def _find_stress_tool_exes(self, stress_dir, keys):
        """Megkeresi a kicsomagolt mappában a megadott STRESS_TOOLS kulcsokhoz tartozó
        exe-ket. Egy kulcson belül a STRESS_TOOLS[key][1] filenames-lista SORRENDJE
        prioritást jelent (pl. HWiNFO-nál előbb a 64, majd a 32 bites) - ezért nem az
        os.walk bejárási sorrendjében elsőként talált fájlt fogadjuk el, hanem a teljes
        bejárás után, kulcsonként, a legmagasabb prioritású (legkorábbi) filenames-
        bejegyzést választjuk ki az összes ténylegesen megtalált jelölt közül."""
        candidates = {key: {} for key in keys}
        for root, dirs, files in os.walk(stress_dir):
            for file in files:
                fl = file.lower()
                for key in keys:
                    filenames = STRESS_TOOLS[key][1]
                    if fl in filenames and fl not in candidates[key]:
                        candidates[key][fl] = os.path.join(root, file)

        found = {}
        for key in keys:
            found[key] = None
            for fname in STRESS_TOOLS[key][1]:
                if fname in candidates[key]:
                    found[key] = candidates[key][fname]
                    break
        return found

    def _get_ram_gb(self):
        """(teljes, szabad) fizikai RAM GB-ban. A teljes felfelé kerekítve - a Windows a
        ténylegesen jelentett bájtszámot mindig a "reklámozott" kapacitás alatt adja
        vissza a hardver számára fenntartott tartomány miatt, pl. egy 8GB-os gép gyakran
        ~7.85 GB-ot jelent - felkerekítve ez helyesen 8-cá válik. A szabad érték kerekítés
        nélküli (tört GB). Hiba esetén (None, None)."""
        try:
            ctypes.windll.kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MEMORYSTATUSEX)]
            ctypes.windll.kernel32.GlobalMemoryStatusEx.restype = ctypes.wintypes.BOOL
            stat = _MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return math.ceil(stat.ullTotalPhys / (1024 ** 3)), stat.ullAvailPhys / (1024 ** 3)
        except Exception as e:
            logging.warning(f"[STRESSTOOLS] RAM lekérdezési hiba: {e}")
        return None, None

    def _pick_linpack_ram_option(self, total_gb, avail_gb=None):
        """A LINPACK_RAM_OPTIONS közül a legnagyobb olyan opciót választja, ami a rendszer
        TELJES RAM-jába ("total_gb") belefér, ÉS - ha ismert - az éppen SZABAD memóriába
        ("avail_gb") is, ~1.5GB tartalékot hagyva. Az utóbbi korlát fontos: egy 12GB-os
        gépen a teljes RAM alapján "beleférne" a 10GB-os opció, de ha a Windows + a többi
        épp induló stressz-program mellett csak ~6GB szabad, a 10GB-os allokáció az egész
        gépet lapozásba fojtaná, és egyik teszt sem futna használhatóan."""
        if not total_gb:
            return 4  # ismeretlen RAM esetén biztonságos alapértelmezés (8GB opció)
        cap = total_gb
        if avail_gb:
            cap = min(cap, avail_gb - 1.5)
        best = LINPACK_RAM_OPTIONS[0][0]
        for opt_num, gb in LINPACK_RAM_OPTIONS:
            if gb <= cap:
                best = opt_num
        return best

    def _build_linpack_console_script(self):
        """Összeállítja a Linpack Xtreme konzolos indító menüjéhez tartozó (prompt-részlet,
        válasz) párokat a LINPACK_PROMPT_SCRIPT alapján, a RAM-menü válaszát a gép teljes
        ÉS szabad memóriájához illő opcióra cserélve (lásd _pick_linpack_ram_option)."""
        total_gb, avail_gb = self._get_ram_gb()
        ram_option = self._pick_linpack_ram_option(total_gb, avail_gb)
        avail_txt = f"{avail_gb:.1f}" if avail_gb else "?"
        logging.info(f"[STRESSTOOLS] Linpack RAM-automatizálás: teljes RAM={total_gb} GB, szabad={avail_txt} GB -> {ram_option}. opció")
        return [(prompt, str(ram_option) if answer is None else answer, needs_enter)
                for prompt, answer, needs_enter in LINPACK_PROMPT_SCRIPT]

    def _window_title(self, hwnd):
        """Egy ablak feliratának lekérdezése (debug-loghoz) - sosem dob kivételt."""
        try:
            user32 = self._stress_user32()
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return ''
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            return buf.value
        except Exception:
            return '?'

    def _debug_dump_pid_windows(self, pid, context=''):
        """DIAGNOSZTIKA: kilistázza az adott PID-hez tartozó ÖSSZES felső szintű ablakot
        (látható/láthatatlan, cím, osztálynév, méret) és mindegyik gyermek-vezérlőjét
        (osztálynév + felirat) - akkor hívjuk, amikor egy keresés ("nem található gomb/
        dialógus") sikertelen, hogy lássuk, mi VOLT ténylegesen ott, ahelyett hogy csak
        annyit tudnánk, hogy "nem találtuk meg"."""
        user32 = self._stress_user32()
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        rows = []

        def _child_cb(hwnd, _lparam):
            cls = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(hwnd, cls, 128)
            rows.append(f"      child hwnd={hwnd} class='{cls.value}' text='{self._window_title(hwnd)}' visible={bool(user32.IsWindowVisible(hwnd))}")
            return True

        def _top_cb(hwnd, _lparam):
            found_pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(found_pid))
            if found_pid.value != pid:
                return True
            cls = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(hwnd, cls, 128)
            rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            rows.append(f"    top hwnd={hwnd} class='{cls.value}' text='{self._window_title(hwnd)}' "
                        f"visible={bool(user32.IsWindowVisible(hwnd))} rect=({rect.left},{rect.top},{rect.right},{rect.bottom})")
            try:
                user32.EnumChildWindows(hwnd, WNDENUMPROC(_child_cb), 0)
            except Exception as e:
                rows.append(f"      (EnumChildWindows hiba: {e})")
            return True

        try:
            user32.EnumWindows(WNDENUMPROC(_top_cb), 0)
        except Exception as e:
            rows.append(f"  (EnumWindows hiba: {e})")

        if rows:
            logging.warning(f"[STRESSTOOLS-DEBUG] {context} - pid={pid} ablak/vezérlő leltár:\n" + "\n".join(rows))
        else:
            logging.warning(f"[STRESSTOOLS-DEBUG] {context} - pid={pid}: EGYETLEN felső szintű ablakot sem talált EnumWindows ehhez a PID-hez (a folyamat vagy még nem hozott létre ablakot, vagy már nem fut).")

    def _send_unicode_char(self, user32, char):
        """Egyetlen Unicode karakter (le+fel) szimulálása SendInput-tal - a KEYEVENTF_UNICODE
        közvetlenül a karaktert küldi, nem virtuális billentyűkódot, így Shift-állapot
        (kis/nagybetű) kezelése nélkül is pontosan a kívánt karakter jelenik meg.
        Visszaadja, hogy mindkét (le+fel) SendInput hívás sikeresen beszúrta-e az eseményt
        (a SendInput a ténylegesen beszúrt események számával tér vissza - 0, ha az egész
        bemenetet a rendszer blokkolta, pl. UIPI/más folyamat által)."""
        extra = ctypes.c_ulong(0)
        down = _Input(INPUT_KEYBOARD, _InputUnion(ki=_KeyBdInput(0, ord(char), KEYEVENTF_UNICODE, 0, ctypes.pointer(extra))))
        up = _Input(INPUT_KEYBOARD, _InputUnion(ki=_KeyBdInput(0, ord(char), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, ctypes.pointer(extra))))
        r1 = user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(_Input))
        r2 = user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(_Input))
        ok = (r1 == 1 and r2 == 1)
        if not ok:
            logging.warning(f"[STRESSTOOLS-DEBUG] SendInput ('{char}') sikertelen/blokkolt - le={r1}, fel={r2} (1 lenne a várt mindkettőnél)")
        return ok

    def _send_vk(self, user32, vk):
        """Egy virtuális billentyűkód (pl. Enter) le+fel eseménye - erre azért van szükség
        külön (nem KEYEVENTF_UNICODE-dal), mert az Enter/vezérlő billentyűket a konzolos
        sor-beviteli logika a valódi VK_RETURN esemény alapján ismeri fel megbízhatóan.
        Visszaadja, hogy sikeres volt-e (lásd _send_unicode_char)."""
        extra = ctypes.c_ulong(0)
        down = _Input(INPUT_KEYBOARD, _InputUnion(ki=_KeyBdInput(vk, 0, 0, 0, ctypes.pointer(extra))))
        up = _Input(INPUT_KEYBOARD, _InputUnion(ki=_KeyBdInput(vk, 0, KEYEVENTF_KEYUP, 0, ctypes.pointer(extra))))
        r1 = user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(_Input))
        r2 = user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(_Input))
        ok = (r1 == 1 and r2 == 1)
        if not ok:
            logging.warning(f"[STRESSTOOLS-DEBUG] SendInput (VK={vk}) sikertelen/blokkolt - le={r1}, fel={r2} (1 lenne a várt mindkettőnél)")
        return ok

    def _find_console_window_for_pid(self, pid):
        """Egy KONZOLOS (nem GUI) program ablak-handle-jének megbízható lekérdezése.

        A sima EnumWindows + GetWindowThreadProcessId (lásd _find_window_for_pid) itt NEM
        feltétlenül működik: egy konzolablakot a klasszikus Windows-modellben nem maga a
        konzolos program, hanem egy külön, rejtett conhost.exe-folyamat "birtokol" - így a
        spawnolt folyamat (pl. Linpack) saját PID-je nem biztos, hogy megegyezik az ablakot
        ténylegesen birtokló folyamat PID-jével. (Ezt debug logban is megerősítettük: a GUI
        programok - FurMark, Prime95, HWiNFO - ablaka PID alapján előbb-utóbb mindig
        megtalálható volt, a Linpické soha, még 30 mp várakozás után sem.)

        Az AttachConsole+GetConsoleWindow ezt megkerüli: a hívó folyamat (mi) átmenetileg
        "csatlakozik" a célfolyamat konzoljához, lekérdezi a hozzá tartozó ablakot, majd
        leválik. Mivel ez folyamat-szintű (nem szálankénti) állapot, self._console_attach_lock
        védi a párhuzamos hívásokat."""
        kernel32 = ctypes.windll.kernel32
        kernel32.AttachConsole.argtypes = [ctypes.wintypes.DWORD]
        kernel32.AttachConsole.restype = ctypes.wintypes.BOOL
        kernel32.FreeConsole.argtypes = []
        kernel32.FreeConsole.restype = ctypes.wintypes.BOOL
        kernel32.GetConsoleWindow.argtypes = []
        kernel32.GetConsoleWindow.restype = ctypes.wintypes.HWND
        with self._console_attach_lock:
            try:
                free_ok = kernel32.FreeConsole()
                logging.debug(f"[STRESSTOOLS-DEBUG] FreeConsole (saját konzolról leválás) eredmény={bool(free_ok)}")
                attach_ok = kernel32.AttachConsole(pid)
                if not attach_ok:
                    err = ctypes.GetLastError()
                    logging.warning(f"[STRESSTOOLS-DEBUG] AttachConsole(pid={pid}) sikertelen, GetLastError={err} "
                                     f"(5=ACCESS_DENIED gyakran azt jelenti, hogy a folyamatnak MÁR van/volt konzolja, "
                                     f"6=INVALID_HANDLE hogy a PID-nek nincs is konzolja, pl. mert még nem jött létre)")
                    return None
                try:
                    hwnd = kernel32.GetConsoleWindow()
                    if hwnd:
                        logging.debug(f"[STRESSTOOLS-DEBUG] AttachConsole(pid={pid}) sikeres, GetConsoleWindow hwnd={hwnd} title='{self._window_title(hwnd)}'")
                    else:
                        logging.warning(f"[STRESSTOOLS-DEBUG] AttachConsole(pid={pid}) sikeres volt, de GetConsoleWindow NULL-t adott vissza (a konzolnak nincs saját ablaka?).")
                    return hwnd if hwnd else None
                finally:
                    kernel32.FreeConsole()
            except Exception as e:
                logging.warning(f"[STRESSTOOLS] AttachConsole hiba (pid={pid}): {e}")
                return None

    def _read_console_screen(self, pid, max_rows=50):
        """Egy konzolos program képernyőjén éppen LÁTHATÓ szöveg kiolvasása (AttachConsole +
        CONOUT$ + ReadConsoleOutputCharacterW), legfeljebb az utolsó max_rows sor. None, ha
        nem sikerült (pl. a folyamat már nem él). Ezzel ellenőrizhető gépelés előtt, hogy a
        várt prompt tényleg megjelent-e - az AttachConsole folyamat-szintű állapotát itt is
        a self._console_attach_lock védi (lásd _find_console_window_for_pid)."""
        GENERIC_READ = 0x80000000
        GENERIC_WRITE = 0x40000000
        FILE_SHARE_READ_WRITE = 0x3
        OPEN_EXISTING = 3
        kernel32 = ctypes.windll.kernel32
        kernel32.AttachConsole.argtypes = [ctypes.wintypes.DWORD]
        kernel32.AttachConsole.restype = ctypes.wintypes.BOOL
        kernel32.CreateFileW.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.DWORD,
                                         ctypes.wintypes.DWORD, ctypes.c_void_p,
                                         ctypes.wintypes.DWORD, ctypes.wintypes.DWORD,
                                         ctypes.wintypes.HANDLE]
        kernel32.CreateFileW.restype = ctypes.wintypes.HANDLE
        kernel32.GetConsoleScreenBufferInfo.argtypes = [ctypes.wintypes.HANDLE,
                                                        ctypes.POINTER(_CONSOLE_SCREEN_BUFFER_INFO)]
        kernel32.GetConsoleScreenBufferInfo.restype = ctypes.wintypes.BOOL
        kernel32.ReadConsoleOutputCharacterW.argtypes = [ctypes.wintypes.HANDLE,
                                                         ctypes.wintypes.LPWSTR,
                                                         ctypes.wintypes.DWORD, _COORD,
                                                         ctypes.POINTER(ctypes.wintypes.DWORD)]
        kernel32.ReadConsoleOutputCharacterW.restype = ctypes.wintypes.BOOL
        invalid_handle = ctypes.wintypes.HANDLE(-1).value
        with self._console_attach_lock:
            try:
                kernel32.FreeConsole()
                if not kernel32.AttachConsole(pid):
                    return None
                try:
                    h = kernel32.CreateFileW("CONOUT$", GENERIC_READ | GENERIC_WRITE,
                                             FILE_SHARE_READ_WRITE, None, OPEN_EXISTING, 0, None)
                    if h == invalid_handle:
                        logging.warning(f"[STRESSTOOLS-DEBUG] _read_console_screen(pid={pid}): CONOUT$ megnyitása sikertelen, GetLastError={ctypes.GetLastError()}")
                        return None
                    try:
                        info = _CONSOLE_SCREEN_BUFFER_INFO()
                        if not kernel32.GetConsoleScreenBufferInfo(h, ctypes.byref(info)):
                            return None
                        width = info.dwSize.X
                        last_row = min(info.dwCursorPosition.Y, info.dwSize.Y - 1)
                        first_row = max(0, last_row - max_rows + 1)
                        lines = []
                        for y in range(first_row, last_row + 1):
                            buf = ctypes.create_unicode_buffer(width + 1)
                            n = ctypes.wintypes.DWORD()
                            if kernel32.ReadConsoleOutputCharacterW(h, buf, width, _COORD(0, y), ctypes.byref(n)):
                                lines.append(buf.value[:n.value].rstrip())
                        return "\n".join(lines).rstrip()
                    finally:
                        kernel32.CloseHandle(h)
                finally:
                    kernel32.FreeConsole()
            except Exception as e:
                logging.warning(f"[STRESSTOOLS] Konzol-képernyő olvasási hiba (pid={pid}): {e}")
                return None

    def _auto_answer_console(self, pid, script, task_id=None):
        """Egy konzolos program (pl. Linpack) menüjét navigálja végig automatikusan. A
        'script' (prompt-részlet, válasz, kell-e Enter) hármasok listája: minden válasz
        elküldése ELŐTT kiolvassa a konzol képernyőpufferét (_read_console_screen), és
        megvárja, hogy a várt prompt ténylegesen megjelenjen - csak ezután hozza előtérbe
        az ablakot (SetForegroundWindow) és gépeli be a választ valódi billentyű-esemény
        szimulációval (SendInput). Enter CSAK akkor megy a válasz után, ha a lépés kéri -
        a 'choice'-alapú batch-menüknél egy fölösleges Enter a pufferben ragadva a
        következő 'set /p'-t üres sorral eteti meg, ami az egész batch-et megszakítja
        (lásd LINPACK_PROMPT_SCRIPT kommentje). Üres válasz + Enter-flag = csak Enter
        (pl. "Press any key").

        A prompt-ellenőrzés NEM elhagyható kényelmi extra: vakon, fix időzítéssel gépelve
        egy leterhelt gépen (ahol a menü akár több mp késéssel jelenik meg) a válaszok
        rossz prompthoz érkeznek, a menü-navigáció szétcsúszik, és a teszt el sem indul -
        pontosan ez történt a terepen. A képernyő-olvasással minden válasz garantáltan a
        neki szánt kérdésre megy, késve megjelenő menünél is.

        A begépelést korábban stdin=subprocess.PIPE-pal próbáltuk megoldani, de a Linpack
        ezzel egyáltalán nem indult el - valószínűleg a konzolos bemenet-kezelése (ami
        valódi konzol-bemenetet vár, nem egy egyszerű átirányított pipe-ot) nem tudott mit
        kezdeni a CREATE_NEW_CONSOLE + átirányított stdin kombinációval, és elindulás előtt
        elszállt. A SendInput-os "valódi begépelés" ezt elkerüli, mert a program
        szemszögéből megkülönböztethetetlen attól, mintha egy felhasználó gépelne."""
        user32 = self._stress_user32()
        logging.info(f"[STRESSTOOLS-DEBUG] _auto_answer_console indul (pid={pid}, script={script})")
        hwnd = None
        deadline = time.time() + 60  # a rendszer terheltségétől függően ez akár fél percig is eltarthat
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            hwnd = self._find_console_window_for_pid(pid)
            if hwnd:
                break
            if attempt % 10 == 0:  # kb. 5 mp-enként egy "még mindig keresem" jelzés
                logging.info(f"[STRESSTOOLS-DEBUG] Konzolablak keresése folyamatban (pid={pid}), {attempt}. próba, még nincs meg...")
            time.sleep(0.5)
        if not hwnd:
            logging.warning(f"[STRESSTOOLS] Automatikus bevitel kihagyva - nem található konzolablak (pid={pid})")
            self._debug_dump_pid_windows(pid, "_auto_answer_console: konzolablak sosem került elő")
            return
        logging.info(f"[STRESSTOOLS-DEBUG] Konzolablak megtalálva (pid={pid}): hwnd={hwnd} title='{self._window_title(hwnd)}'")
        for prompt, line, needs_enter in script:
            try:
                # Várakozás, amíg a válaszhoz tartozó prompt ténylegesen megjelenik a
                # konzol képernyőjén. Közben az ablak létezését is figyeljük - ha a program
                # bezáródott/összeomlott, ennek itt, konkrét hibaüzenettel kell kiderülnie.
                prompt_deadline = time.time() + 60
                screen = None
                prompt_found = False
                poll = 0
                while time.time() < prompt_deadline:
                    poll += 1
                    if not user32.IsWindow(hwnd):
                        logging.warning(f"[STRESSTOOLS-DEBUG] A konzolablak (hwnd={hwnd}, pid={pid}) már NEM létezik a(z) '{prompt}' promptra várva - a program valószínűleg bezáródott/összeomlott. Automatizálás megszakítva. Utolsó ismert képernyőtartalom:\n{screen}")
                        self._debug_dump_pid_windows(pid, f"_auto_answer_console: ablak eltűnt a(z) '{prompt}' promptra várva")
                        return
                    new_screen = self._read_console_screen(pid)
                    if new_screen is not None:
                        screen = new_screen
                        if prompt.lower() in screen.lower():
                            prompt_found = True
                            break
                    if poll % 10 == 0:  # kb. 5 mp-enként állapotjelzés
                        last_line = screen.splitlines()[-1] if screen else '(nem olvasható)'
                        logging.info(f"[STRESSTOOLS-DEBUG] Még várom a(z) '{prompt}' promptot (pid={pid}, {poll}. próba), a képernyő utolsó sora most: '{last_line}'")
                    time.sleep(0.5)
                if not prompt_found:
                    logging.warning(f"[STRESSTOOLS] A(z) '{prompt}' prompt 60 mp alatt sem jelent meg (pid={pid}), automatizálás megszakítva. A konzol képernyője most:\n{screen}")
                    return
                logging.info(f"[STRESSTOOLS-DEBUG] Prompt megjelent: '{prompt}' (pid={pid}), válasz begépelése: '{line}'")

                fg_ok = user32.SetForegroundWindow(hwnd)
                time.sleep(0.15)
                actual_fg = user32.GetForegroundWindow()
                if actual_fg != hwnd:
                    logging.warning(f"[STRESSTOOLS-DEBUG] SetForegroundWindow (hwnd={hwnd}) NEM állította előtérbe a konzolablakot a(z) '{line}' sor előtt! "
                                     f"SetForegroundWindow visszatérési értéke={bool(fg_ok)}, a TÉNYLEGES előtérben lévő ablak most: hwnd={actual_fg} title='{self._window_title(actual_fg)}'. "
                                     f"A begépelt karakterek valószínűleg NEM a Linpackbe mentek.")
                else:
                    logging.debug(f"[STRESSTOOLS-DEBUG] SetForegroundWindow sikeres, hwnd={hwnd} tényleg előtérben van a(z) '{line}' sor előtt.")

                all_ok = True
                for ch in line:
                    if not self._send_unicode_char(user32, ch):
                        all_ok = False
                    time.sleep(0.03)
                if needs_enter:
                    if not self._send_vk(user32, VK_RETURN):
                        all_ok = False
                logging.info(f"[STRESSTOOLS] Automatikus bevitel elküldve: '{line}' (Enter={'igen' if needs_enter else 'nem - choice-alapú prompt'}, pid={pid}), minden SendInput esemény sikeres={all_ok}")
            except Exception as e:
                logging.warning(f"[STRESSTOOLS] Automatikus bevitel hiba ('{line}', pid={pid}): {e}")

    @staticmethod
    def _text_alternatives(text_or_alts):
        """Egy lépés címke-megadása vagy egyetlen string (pl. 'OK'), vagy alternatívák
        listája (pl. ['Indítás', 'Start'] - HWiNFO nyelvtől függően magyar vagy angol
        feliratú gombja) - ez egységesíti a kettőt egy listává."""
        if isinstance(text_or_alts, (list, tuple, set)):
            return list(text_or_alts)
        return [text_or_alts]

    @staticmethod
    def _normalize_ctrl_text(text):
        """Vezérlő-felirat normalizálása összehasonlításhoz: kisbetűsít, eltávolítja a
        gyorsbillentyű-jelölő '&' karaktereket, és minden szóköz-sorozatot egyetlen
        szóközre von össze. Egyik lépés sem elhagyható, mindkettő valós gépen bizonyított
        hibát javít: a Prime95 Small FFTs rádiógombjának valódi szövege 'Small FFTs
        (tests L1/L2/L&3 caches, ...' (rejtett '&'), a GIMPS üdvözlő gombjáé pedig
        'Just  &Stress Testing' - DUPLA szóközzel a 'Just' után! Anélkül a 'l1/l2/l3',
        illetve a 'Just Stress Testing' keresés soha nem találná meg őket."""
        return ' '.join(text.replace('&', '').lower().split())

    def _find_child_by_text(self, hwnd_parent, text_or_alts, exact=False):
        """Megkeresi a hwnd_parent egy közvetlen gyermek-vezérlőjét (pl. gombot), aminek a
        felirata (kis/nagybetűtől, gyorsbillentyű-jelölő '&'-től és dupla szóközöktől
        függetlenül) tartalmazza a megadott szövegek bármelyikét. exact=True esetén csak a
        TELJES felirat-egyezés számít találatnak - nagyon rövid keresett szövegnél (pl.
        'OK', 'Igen') ez véd a hamis találatoktól: részleges kereséssel bármely '...ok'
        végű magyar felirat (pl. 'ventilátorok' egy HWiNFO szenzorlistában) találat lenne."""
        user32 = self._stress_user32()
        result = {'hwnd': None}
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        needles = [self._normalize_ctrl_text(t) for t in self._text_alternatives(text_or_alts)]

        def _callback(hwnd, _lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                text_lower = self._normalize_ctrl_text(buf.value)
                if any((needle == text_lower if exact else needle in text_lower) for needle in needles):
                    result['hwnd'] = hwnd
                    return False  # megvan, leállítjuk a bejárást
            return True

        try:
            user32.EnumChildWindows(hwnd_parent, WNDENUMPROC(_callback), 0)
        except Exception as e:
            logging.warning(f"[STRESSTOOLS] EnumChildWindows hiba: {e}")
        return result['hwnd']

    def _find_pid_window_with_child_text(self, pid, text_or_alts, timeout=60, exact=False):
        """Megkeresi az adott PID-hez tartozó BÁRMELYIK (nem feltétlenül a legnagyobb)
        felső szintű, látható ablakot, aminek van a megadott feliratú (vagy alternatívák
        egyikének megfelelő) gyermek-vezérlője - pl. egy épp megjelenő modális
        figyelmeztető/megerősítő dialógusablakot a rajta lévő gomb alapján. Eltér a
        _find_window_for_pid-től, ami mindig a legnagyobb ablakot választja - egy
        dialógus viszont jellemzően KISEBB, mint a program főablaka, arra a logika itt
        nem használható. Legfeljebb 'timeout' másodpercig vár, amíg a dialógus megjelenik -
        ez a hosszú alapérték szándékos: ha a gép egyszerre 4 stressz-teszt programot (és
        esetleg egy párhuzamosan futó DISM lekérdezést) indít, a rendszer erősen
        leterhelődhet, és egy dialógus akár fél percig is késhet (ezt debug logban is
        megfigyeltük - a FurMark gombja végül 56 mp késéssel, de sikeresen megnyomódott).
        Visszaad: (ablak hwnd, gomb hwnd) vagy (None, None)."""
        user32 = self._stress_user32()
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        deadline = time.time() + timeout
        attempt = 0
        logging.info(f"[STRESSTOOLS-DEBUG] _find_pid_window_with_child_text indul: pid={pid}, keresett szöveg(ek)={self._text_alternatives(text_or_alts)}, timeout={timeout}s")
        while time.time() < deadline:
            attempt += 1
            result = {'hwnd': None, 'btn': None}
            windows_seen = []

            def _callback(hwnd, _lparam):
                found_pid = ctypes.wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(found_pid))
                if found_pid.value != pid:
                    return True
                if not user32.IsWindowVisible(hwnd):
                    return True
                windows_seen.append((hwnd, self._window_title(hwnd)))
                btn = self._find_child_by_text(hwnd, text_or_alts, exact=exact)
                if btn:
                    result['hwnd'] = hwnd
                    result['btn'] = btn
                    return False
                return True

            try:
                user32.EnumWindows(WNDENUMPROC(_callback), 0)
            except Exception as e:
                logging.warning(f"[STRESSTOOLS] EnumWindows hiba (pid={pid}): {e}")
            if result['btn']:
                logging.info(f"[STRESSTOOLS-DEBUG] Találat: pid={pid} ablak hwnd={result['hwnd']} title='{self._window_title(result['hwnd'])}', gomb hwnd={result['btn']} ({attempt}. próbálkozásra, {time.time() - (deadline - timeout):.1f}mp alatt)")
                return result['hwnd'], result['btn']
            if attempt % 10 == 0:  # kb. 3 mp-enként egy állapotjelzés
                titles = [f"'{t}'" for _, t in windows_seen] or ['(egy sem)']
                logging.info(f"[STRESSTOOLS-DEBUG] Még keresem (pid={pid}, {attempt}. próba): jelenleg látható ablakai ehhez a PID-hez: {', '.join(titles)} - egyikben sincs '{text_or_alts}' feliratú vezérlő.")
            time.sleep(0.3)
        return None, None

    def _auto_click_sequence(self, pid, steps, task_id=None):
        """Egy GUI program egymás után megjelenő ablakait/dialógusait navigálja végig:
        minden lépésnél megvárja (max 60 mp / lépés), amíg megjelenik egy olyan ablak,
        amiben van a lépéshez tartozó feliratú gomb/rádiógomb (lásd
        _find_pid_window_with_child_text), és BM_CLICK üzenettel megnyomja. Ezzel több
        egymást követő popup is végignyomkodható felügyelet nélkül (pl. FurMark: "GPU
        stress test" -> "GO!" figyelmeztetés; Prime95: "Just Stress Testing" -> "Small
        FFTs" rádiógomb -> "OK"). Egy 'steps'-beli elem lehet egyetlen string, alternatívák
        listája (lokalizált feliratokhoz, pl. HWiNFO "Indítás"/"Start"), vagy dict
        {'labels': [...], 'skip_if_found': [...]} - utóbbinál ha a keresés a
        'skip_if_found' egyik feliratát találja meg (vagyis egy KÉSŐBBI lépés vezérlője
        van már jelen), a lépés kattintás nélkül kimarad (pl. a Prime95 GIMPS üdvözlője
        csak a legelső indításkor létezik, lásd STRESS_CLICK_SEQUENCES).

        A BM_CLICK-et szándékosan PostMessageW-vel (nem SendMessageW-vel) küldjük: a
        SendMessageW addig blokkol, amíg a gomb kattintás-kezelője lefut - ha viszont a
        gomb egy MODÁLIS dialógust nyit (pl. a FurMark 'GPU stress test' gombja a CAUTION
        figyelmeztetést), a kezelő csak a dialógus bezárásakor tér vissza, vagyis a
        SendMessageW-s hívás beragad, és a következő lépés (a 'GO' megnyomása ugyanazon a
        dialóguson) SOSEM indulna el. Terepen ez konkrétan 50 mp-es beragadásként
        jelentkezett, amit csak az ablak kézi bezárása oldott fel."""
        user32 = self._stress_user32()
        logging.info(f"[STRESSTOOLS-DEBUG] _auto_click_sequence indul: pid={pid}, lépések={steps}")
        last_clicked = None  # (gomb hwnd, labels) - az utoljára TÉNYLEGESEN megnyomott lépés
        for step_idx, step in enumerate(steps, 1):
            if isinstance(step, dict):
                labels = self._text_alternatives(step['labels'])
                skip_markers = self._text_alternatives(step.get('skip_if_found', []))
                optional = bool(step.get('optional'))
                timeout = step.get('timeout', 60)
                exact = bool(step.get('exact'))
            else:
                labels = self._text_alternatives(step)
                skip_markers = []
                optional = False
                timeout = 60
                exact = False
            logging.info(f"[STRESSTOOLS-DEBUG] {step_idx}/{len(steps)}. lépés keresése: pid={pid}, cél='{labels}'" + (f", kihagyás-jelzők='{skip_markers}'" if skip_markers else "") + (" (opcionális)" if optional else ""))
            hwnd, btn = self._find_pid_window_with_child_text(pid, labels + skip_markers, timeout=timeout, exact=exact)
            if not btn:
                if optional:
                    # Az opcionális lépés dialógusa nem mindig jelenik meg (pl. a HWiNFO
                    # indítás utáni figyelmeztetése) - ha nincs, az nem hiba. A leltár-dump
                    # csak diagnosztika: ha a felugró ablak gombfelirata más, mint amire
                    # számítunk, ebből derül ki, mi volt ott valójában.
                    logging.info(f"[STRESSTOOLS] {step_idx}/{len(steps)}. (opcionális) lépés ('{labels}') nem jelent meg {timeout} mp alatt (pid={pid}) - kihagyva, ez nem hiba.")
                    self._debug_dump_pid_windows(pid, f"_auto_click_sequence: opcionális '{labels}' lépés nem került elő (diagnosztikai leltár, NEM hiba)")
                    continue
                logging.warning(f"[STRESSTOOLS] '{labels}' gomb/dialógus nem található (pid={pid}), automatizálás megszakítva.")
                self._debug_dump_pid_windows(pid, f"_auto_click_sequence: {step_idx}/{len(steps)}. lépés ('{labels}') sosem került elő")
                return
            btn_text = self._window_title(btn)
            if skip_markers and not any(self._normalize_ctrl_text(l) in self._normalize_ctrl_text(btn_text) for l in labels):
                # A találat a kihagyás-jelző (egy későbbi lépés vezérlője), nem a lépés
                # saját gombja -> a lépés dialógusa ennél a futásnál nem létezik, ugrás
                # tovább kattintás nélkül (a következő lépés ugyanezt a vezérlőt azonnal
                # újra megtalálja és megnyomja).
                logging.info(f"[STRESSTOOLS] {step_idx}/{len(steps)}. lépés ('{labels}') kihagyva (pid={pid}): helyette már a(z) '{btn_text}' vezérlő van jelen - pl. a Prime95 üdvözlő dialógusa csak a legelső indításkor jelenik meg.")
                continue
            try:
                cls = ctypes.create_unicode_buffer(128)
                user32.GetClassNameW(btn, cls, 128)
                posted = user32.PostMessageW(btn, BM_CLICK, 0, 0)
                logging.info(f"[STRESSTOOLS] '{labels}' megnyomva (pid={pid}): gomb hwnd={btn} class='{cls.value}' text='{btn_text}', PostMessageW eredmény={bool(posted)}, ablak='{self._window_title(hwnd)}'.")
                if not posted:
                    logging.warning(f"[STRESSTOOLS-DEBUG] PostMessageW(BM_CLICK) sikertelen (pid={pid}, gomb hwnd={btn}), GetLastError={ctypes.GetLastError()}")
                last_clicked = (btn, labels)
            except Exception as e:
                logging.warning(f"[STRESSTOOLS] Gombnyomási hiba ('{labels}', pid={pid}): {e}")
                return
            time.sleep(1)  # a következő dialógus (ha van) megjelenéséhez
        # Az utoljára megnyomott lépés hatás-ellenőrzése: a közbülső lépéseknél a következő
        # lépés keresése önmagában visszaigazolás (ha az előző kattintás elveszett, a
        # következő dialógus sosem jelenik meg, és az kiderül a logból), az utolsó
        # kattintásnál viszont senki nem ellenőrizne - pedig terepen előfordult, hogy a
        # HWiNFO 'Indítás' gombjának PostMessage-elt kattintása egy 4 másik stressz-teszttel
        # párhuzamosan terhelt gépen hatástalan maradt, és a startup ablak csak ült ott.
        # (Kihagyott opcionális utolsó lépésnél így a megelőző valódi kattintás ellenőrződik.)
        if last_clicked:
            self._verify_final_click(pid, last_clicked[0], last_clicked[1])

    def _verify_final_click(self, pid, btn, labels, retries=3, wait_secs=6):
        """A kattintás-sorozat utolsó gombjának (pl. HWiNFO 'Indítás', Prime95 'OK',
        FurMark 'GO') megnyomása mindig bezárja a saját dialógusát - tehát a gombnak
        rövid időn belül el kell tűnnie (megszűnik vagy láthatatlanná válik). Ha
        'wait_secs' után is látható, a kattintás valószínűleg elveszett: újrapróbáljuk
        PostMessageW-vel, majd ráadásként SendMessageTimeoutW-vel is - utóbbi a régi,
        szinkron kézbesítés (ami a HWiNFO-nál bizonyítottan működött), de korlátos
        várakozással, így modális dialógust nyitó gombnál sem ragadhat be örökre.
        A dupla kattintás veszélytelen: ha az első hatott, a dialógus bezárult, és a
        második már egy halott/láthatatlan gombra megy (no-op)."""
        user32 = self._stress_user32()
        SMTO_NORMAL = 0x0000
        for attempt in range(1, retries + 1):
            deadline = time.time() + wait_secs
            while time.time() < deadline:
                if not user32.IsWindow(btn) or not user32.IsWindowVisible(btn):
                    logging.info(f"[STRESSTOOLS-DEBUG] Utolsó lépés ('{labels}') visszaigazolva (pid={pid}): a gomb/dialógus eltűnt ({attempt}. próbálkozási körben).")
                    return True
                time.sleep(0.5)
            if attempt >= retries:
                break
            logging.warning(f"[STRESSTOOLS] Az utolsó lépés ('{labels}') gombja {wait_secs} mp után is látható (pid={pid}) - a kattintás valószínűleg elveszett, újrapróbálás ({attempt + 1}/{retries}. kör)...")
            try:
                posted = user32.PostMessageW(btn, BM_CLICK, 0, 0)
                smto_result = ctypes.c_size_t(0)
                delivered = user32.SendMessageTimeoutW(btn, BM_CLICK, 0, 0, SMTO_NORMAL, 3000, ctypes.byref(smto_result))
                logging.info(f"[STRESSTOOLS-DEBUG] Újra-kattintás elküldve ('{labels}', pid={pid}): PostMessageW={bool(posted)}, SendMessageTimeoutW kézbesítve={bool(delivered)} (0=timeout/hiba, az üzenet ettől még feldolgozás alatt lehet).")
            except Exception as e:
                logging.warning(f"[STRESSTOOLS] Újra-kattintási hiba ('{labels}', pid={pid}): {e}")
                return False
        logging.warning(f"[STRESSTOOLS] Az utolsó lépés ('{labels}') dialógusa {retries} próbálkozási kör után SEM tűnt el (pid={pid}) - a program valószínűleg nem indult el rendesen.")
        self._debug_dump_pid_windows(pid, f"_verify_final_click: '{labels}' dialógusa nem záródott be")
        return False

    def _launch_stress_exe(self, exe, display_name, console_script=None, click_sequence=None, thread_sink=None):
        """Egy stressz-teszt/monitor .exe elindítása, UAC-elutasítás (WinError 740, pl.
        HWiNFO64.exe requireAdministrator manifestje) esetén ShellExecuteExW-es 'runas'
        újrapróbálással. Visszaadási érték:
          - PID (pozitív int), ha sikerült elindítani és ismerjük a PID-jét - ez a normál
            (nem emelt) eset ÉS a 'runas' eset is: utóbbinál ShellExecuteExW-et
            SEE_MASK_NOCLOSEPROCESS maszkkal hívjuk (a sima ShellExecuteW nem adna vissza
            semmilyen handle-t/PID-et), a kapott hProcess-ből GetProcessId-vel kinyerjük a
            valódi PID-et, ugyanúgy, mint a nem emelt ágon,
          - -1, ha sikerült indítani 'runas'-sal, de a ShellExecuteExW mégsem adott vissza
            process handle-t (pl. a felhasználó elutasította a UAC-promptot, vagy
            AppCompat-tükrözés zajlik) - ilyenkor sem az ablak automatikus pozicionálása
            (lásd _position_stress_windows), sem a console_script/click_sequence
            automatizálás nem tud lefutni (nincs PID-ünk),
          - None, ha nem sikerült elindítani.
        console_script: opcionális (prompt, válasz) párlista (pl. Linpack menüjéhez) - ha
        meg van adva, egy háttérszálon _auto_answer_console navigálja végig a program
        konzolos menüjét (lásd ott, miért SendInput-tal, nem stdin-átirányítással).
        click_sequence: opcionális lista (pl. FurMark: ['GPU stress test', 'GO'] - a
        beállító-ablak gombja, majd a rákövetkező figyelmeztető dialógus gombja) - ha meg
        van adva, egy háttérszálon _auto_click_sequence sorban megkeresi és BM_CLICK
        üzenettel megnyomja az egymás után megjelenő ablakok/dialógusok gombjait. Egy
        lépés lehet egyetlen felirat vagy alternatívák listája (localizált szövegekhez).
        A 'runas' ágon is lefut, amíg van valódi PID-ünk (lásd fent) - csak a ritka
        handle-hiány esetén marad ki.
        thread_sink: opcionális lista - ha meg van adva, az elindított automatizálási
        háttérszál belekerül, hogy a hívó (start_stress_tests) bevárhassa a dialógus-
        nyomkodás végét, MIELŐTT az ablakokat rendezné/minimalizálná."""
        def _run_automation_safely(func, *args):
            # A háttérszál céljának védőrétege - ha bármi a try/except-eken KÍVÜL dobna
            # kivételt (pl. egy elgépelés egy jövőbeli módosításban), az itt látszódjon a
            # logban teljes traceback-kel, ne csendben tűnjön el egy daemon szálban.
            try:
                func(*args)
            except Exception as e:
                logging.error(f"[STRESSTOOLS-DEBUG] Automatizálási háttérszál ELSZÁLLT ({func.__name__}, args={args}): {e}")
                logging.error(traceback.format_exc())

        try:
            proc = subprocess.Popen([exe], creationflags=subprocess.CREATE_NEW_CONSOLE, cwd=os.path.dirname(exe))
            logging.info(f"[STRESSTOOLS] Elindítva: {display_name} ({exe}), pid={proc.pid}")
            auto_thread = None
            if console_script:
                auto_thread = threading.Thread(target=_run_automation_safely, args=(self._auto_answer_console, proc.pid, console_script), daemon=True, name=f"auto:{display_name}")
            elif click_sequence:
                auto_thread = threading.Thread(target=_run_automation_safely, args=(self._auto_click_sequence, proc.pid, click_sequence), daemon=True, name=f"auto:{display_name}")
            if auto_thread:
                auto_thread.start()
                if thread_sink is not None:
                    thread_sink.append(auto_thread)
            return proc.pid
        except OSError as e:
            if getattr(e, 'winerror', None) == 740:
                # ERROR_ELEVATION_REQUIRED - az exe manifestje requireAdministrator (pl.
                # HWiNFO64.exe), sima Popen nem tudja elindítani. ShellExecuteExW-et
                # SEE_MASK_NOCLOSEPROCESS maszkkal hívjuk (sima ShellExecuteW nem adna
                # vissza semmilyen handle-t/PID-et) - a kapott hProcess-ből GetProcessId-vel
                # kinyerjük a VALÓDI PID-et, hogy az indító-dialógus automatizálás (
                # console_script/click_sequence) ugyanúgy tudjon futni rá, mint egy nem
                # emelt szintű indításnál. Korábban itt sima ShellExecuteW volt és -1-et
                # adtunk vissza (nincs PID) - emiatt a HWiNFO-nál sosem indult el az
                # automata kattintás-szekvencia, a felhasználónak kézzel kellett nyomnia az
                # "Indítás" gombot.
                try:
                    sei = _SHELLEXECUTEINFOW()
                    sei.cbSize = ctypes.sizeof(_SHELLEXECUTEINFOW)
                    sei.fMask = SEE_MASK_NOCLOSEPROCESS
                    sei.hwnd = None
                    sei.lpVerb = "runas"
                    sei.lpFile = exe
                    sei.lpParameters = None
                    sei.lpDirectory = os.path.dirname(exe)
                    sei.nShow = SW_SHOWNORMAL
                    sei.hInstApp = None

                    ok = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei))
                    if not ok or not sei.hProcess:
                        logging.warning(f"[STRESSTOOLS] Elindítva (Admin): {display_name} ({exe}) - ShellExecuteExW nem adott vissza process handle-t (pl. a felhasználó elutasította a UAC-promptot vagy AppCompat-tükrözés zajlik), automatizálás kimarad.")
                        return -1

                    real_pid = ctypes.windll.kernel32.GetProcessId(sei.hProcess)
                    ctypes.windll.kernel32.CloseHandle(sei.hProcess)
                    logging.info(f"[STRESSTOOLS] Elindítva (Admin): {display_name} ({exe}), pid={real_pid}")

                    auto_thread = None
                    if console_script:
                        auto_thread = threading.Thread(target=_run_automation_safely, args=(self._auto_answer_console, real_pid, console_script), daemon=True, name=f"auto:{display_name}")
                    elif click_sequence:
                        auto_thread = threading.Thread(target=_run_automation_safely, args=(self._auto_click_sequence, real_pid, click_sequence), daemon=True, name=f"auto:{display_name}")
                    if auto_thread:
                        auto_thread.start()
                        if thread_sink is not None:
                            thread_sink.append(auto_thread)
                    return real_pid if real_pid else -1
                except Exception as e2:
                    logging.error(f"[STRESSTOOLS] Indítási hiba (Admin) - {display_name}: {e2}")
                    return None
            logging.error(f"[STRESSTOOLS] Indítási hiba - {display_name}: {e}")
            return None
        except Exception as e:
            logging.error(f"[STRESSTOOLS] Indítási hiba - {display_name}: {e}")
            return None

    def _stress_user32(self):
        """A stressz-teszt ablak-pozicionáláshoz használt user32 függvényekre explicit
        argtypes/restype-ot állít be (idempotens - hívható többször is). Enélkül egy
        HWND-típusú (64 biten pointer-méretű) paraméter argtypes deklaráció nélküli, sima
        Python int-ként való átadása ctypes-szal 64 bites Windows-on elméletileg hibás
        marshalling-hoz vezethet - ez itt garantáltan helyesen konvertál."""
        user32 = ctypes.windll.user32
        HWND, LPARAM, DWORD, BOOL, RECT, UINT = (ctypes.wintypes.HWND, ctypes.wintypes.LPARAM,
                                                  ctypes.wintypes.DWORD, ctypes.wintypes.BOOL,
                                                  ctypes.wintypes.RECT, ctypes.wintypes.UINT)
        user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(BOOL, HWND, LPARAM), LPARAM]
        user32.EnumWindows.restype = BOOL
        user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(DWORD)]
        user32.GetWindowThreadProcessId.restype = DWORD
        user32.IsWindowVisible.argtypes = [HWND]
        user32.IsWindowVisible.restype = BOOL
        user32.GetWindowTextLengthW.argtypes = [HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowRect.argtypes = [HWND, ctypes.POINTER(RECT)]
        user32.GetWindowRect.restype = BOOL
        user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
        user32.ShowWindow.restype = BOOL
        user32.SetWindowPos.argtypes = [HWND, HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, UINT]
        user32.SetWindowPos.restype = BOOL
        user32.SystemParametersInfoW.argtypes = [UINT, UINT, ctypes.wintypes.LPVOID, UINT]
        user32.SystemParametersInfoW.restype = BOOL
        user32.GetClassNameW.argtypes = [HWND, ctypes.wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        user32.IsIconic.argtypes = [HWND]
        user32.IsIconic.restype = BOOL
        user32.SetForegroundWindow.argtypes = [HWND]
        user32.SetForegroundWindow.restype = BOOL
        user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(_Input), ctypes.c_int]
        user32.SendInput.restype = ctypes.c_uint
        user32.EnumChildWindows.argtypes = [HWND, ctypes.WINFUNCTYPE(BOOL, HWND, LPARAM), LPARAM]
        user32.EnumChildWindows.restype = BOOL
        user32.GetWindowTextW.argtypes = [HWND, ctypes.wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.SendMessageW.argtypes = [HWND, UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
        user32.SendMessageW.restype = ctypes.wintypes.LPARAM
        user32.PostMessageW.argtypes = [HWND, UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
        user32.PostMessageW.restype = BOOL
        user32.SendMessageTimeoutW.argtypes = [HWND, UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM,
                                               UINT, UINT, ctypes.POINTER(ctypes.c_size_t)]
        user32.SendMessageTimeoutW.restype = ctypes.wintypes.LPARAM
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = HWND
        user32.IsWindow.argtypes = [HWND]
        user32.IsWindow.restype = BOOL
        return user32

    def _find_window_for_pid(self, pid):
        """Megkeresi az adott PID-hez tartozó legnagyobb (kliens-terület szerint), látható,
        címsoros felső szintű ablakot - ha egy folyamatnak több ablaka/rejtett segédablaka
        is van, a legnagyobbat tekintjük a "fő" ablaknak."""
        user32 = self._stress_user32()
        result = {'hwnd': None, 'area': -1}
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

        def _callback(hwnd, _lparam):
            found_pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(found_pid))
            if found_pid.value != pid:
                return True
            if not user32.IsWindowVisible(hwnd):
                return True
            if user32.GetWindowTextLengthW(hwnd) == 0:
                return True
            rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            area = (rect.right - rect.left) * (rect.bottom - rect.top)
            if area > result['area']:
                result['area'] = area
                result['hwnd'] = hwnd
            return True

        try:
            user32.EnumWindows(WNDENUMPROC(_callback), 0)
        except Exception as e:
            logging.warning(f"[STRESSTOOLS] EnumWindows hiba (pid={pid}): {e}")
        if result['hwnd']:
            logging.debug(f"[STRESSTOOLS-DEBUG] _find_window_for_pid(pid={pid}) -> hwnd={result['hwnd']} title='{self._window_title(result['hwnd'])}' area={result['area']}")
        else:
            logging.debug(f"[STRESSTOOLS-DEBUG] _find_window_for_pid(pid={pid}) -> nincs látható, feliratos felső szintű ablak.")
        return result['hwnd']

    def _position_stress_windows(self, pid_map, task_id='stress'):
        """A négy stressz-teszt ablakot rendezi a fő monitor hasznos területén (tálca
        nélkül) négy negyedbe: FurMark bal-fent, Prime95 jobb-fent, Linpack bal-lent,
        HWiNFO jobb-lent. Az utóbbi 3 a maga negyedére van méretezve; a FurMarkot viszont
        NEM méretezzük át - a render-felülete fix (a kiválasztott felbontáshoz kötött)
        belső méretű, egy kényszerített átméretezés csak levágja/eltolja a képet (pl. az
        FPS-kijelzést), nem skálázza. Ehelyett natív méretben a bal-felső sarokba TOLJUK
        (SWP_NOSIZE - a mozgatás nem vágja a képet, csak az átméretezés) és z-sorrendben
        legalulra küldjük: így a bal-felső negyedben pont a FurMark látszik (a bal-felső
        sarka, FPS-kijelzéssel), a másik 3 negyedet pedig a fölé rendezett ablakok fedik.
        A végén minden egyéb (nem ide tartozó, pl. a DriverVarázsló saját ablaka vagy egy
        program nyitva maradt beállító-dialógusa) ablakot tálcára teszünk, hogy tiszta
        legyen a képernyő.
        pid_map: {STRESS_TOOLS kulcs: pid}. A hiányzó/‑1 (UAC) PID-ű vagy meg nem található
        ablakú tételeket egyszerűen kihagyja, nem buktatja el a többit."""
        HWND_BOTTOM = 1
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        SWP_NOSIZE = 0x0001
        SW_RESTORE = 9
        SPI_GETWORKAREA = 0x0030
        user32 = self._stress_user32()

        try:
            rect = ctypes.wintypes.RECT()
            user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
            left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
        except Exception as e:
            logging.warning(f"[STRESSTOOLS] Munkaterület lekérdezési hiba, pozicionálás kihagyva: {e}")
            return
        w = (right - left) // 2
        h = (bottom - top) // 2

        # A sorrend itt számít: a FurMarkot kell ELŐSZÖR HWND_BOTTOM-ra küldeni, utána a
        # többit SWP_NOZORDER-rel (érintetlenül hagyva a z-sorrendjüket) - így garantált,
        # hogy a másik 3 a FurMark FÖLÖTT marad, nem kell nekik explicit HWND_TOP.
        # A furmark bejegyzésnél w/h nem releváns (SWP_NOSIZE miatt), az x/y a bal-felső
        # sarok: natív méretben oda toljuk, hogy a bal-felső negyedben ő látsszon.
        layout = [
            ('furmark', left, top, None, None, True),
            ('prime95', left + w, top, w, h, False),
            ('linpack', left, top + h, w, h, False),
            ('hwinfo', left + w, top + h, w, h, False),
        ]

        positioned_hwnds = []
        for key, x, y, ww, hh, send_back in layout:
            pid = pid_map.get(key)
            display_name = STRESS_TOOLS[key][0]
            if not pid or pid <= 0:
                continue  # nem indult el, vagy UAC-os indítás volt (nincs PID)
            # A Linpack konzolablakát a conhost.exe "birtokolja" más PID alatt, ezért azt
            # nem a sima PID-alapú EnumWindows-szal (_find_window_for_pid), hanem
            # AttachConsole-lal (_find_console_window_for_pid) keressük meg.
            hwnd = self._find_console_window_for_pid(pid) if key == 'linpack' else self._find_window_for_pid(pid)
            if not hwnd:
                logging.warning(f"[STRESSTOOLS] Nem található ablak a pozicionáláshoz: {display_name} (pid={pid})")
                self.emit('task_progress', {'task': task_id, 'log': f'⚠️ {display_name}: nem található ablak a pozicionáláshoz.'})
                self._debug_dump_pid_windows(pid, f"_position_stress_windows: {display_name} ablaka nem található")
                continue
            try:
                user32.ShowWindow(hwnd, SW_RESTORE)
                if key == 'furmark':
                    # Méretet nem változtatunk (az vágná a render-képet), de a bal-felső
                    # sarokba toljuk és z-sorrendben legalulra - lásd a docstringet.
                    user32.SetWindowPos(hwnd, HWND_BOTTOM, x, y, 0, 0, SWP_NOACTIVATE | SWP_NOSIZE)
                else:
                    user32.SetWindowPos(hwnd, 0, x, y, ww, hh, SWP_NOACTIVATE | SWP_NOZORDER)
                positioned_hwnds.append(hwnd)
                logging.info(f"[STRESSTOOLS] Ablak elrendezve: {display_name} (hátra={send_back})")
                self.emit('task_progress', {'task': task_id, 'log': f'🪟 {display_name} elrendezve.'})
            except Exception as e:
                logging.warning(f"[STRESSTOOLS] Pozicionálási hiba ({display_name}): {e}")

        self._minimize_other_windows(positioned_hwnds, task_id=task_id)

    def _minimize_other_windows(self, keep_hwnds, task_id='stress'):
        """Minden egyéb látható, címsoros felső szintű ablakot tálcára tesz (minimalizál),
        KIVÉVE a keep_hwnds-ben szereplőket - hogy a stressz teszt elrendezése után tiszta
        legyen a képernyő (ez a DriverVarázsló saját ablakát és pl. egy program nyitva
        maradt beállító-dialógusát is érinti). A rendszer héj-ablakait (tálca, asztal)
        osztálynév alapján kihagyjuk, nehogy azokat is "minimalizáljuk"."""
        SW_MINIMIZE = 6
        SHELL_CLASSES = {'Progman', 'Shell_TrayWnd', 'Shell_SecondaryTrayWnd', 'WorkerW', 'Button'}
        user32 = self._stress_user32()
        keep = set(h for h in keep_hwnds if h)
        to_minimize = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

        def _callback(hwnd, _lparam):
            if hwnd in keep:
                return True
            if not user32.IsWindowVisible(hwnd):
                return True
            if user32.GetWindowTextLengthW(hwnd) == 0:
                return True
            if user32.IsIconic(hwnd):
                return True
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, buf, 256)
            if buf.value in SHELL_CLASSES:
                return True
            to_minimize.append(hwnd)
            return True

        try:
            user32.EnumWindows(WNDENUMPROC(_callback), 0)
            for hwnd in to_minimize:
                try:
                    user32.ShowWindow(hwnd, SW_MINIMIZE)
                except Exception:
                    pass
            logging.info(f"[STRESSTOOLS] {len(to_minimize)} egyéb ablak tálcára helyezve.")
            if to_minimize:
                self.emit('task_progress', {'task': task_id, 'log': f'📥 {len(to_minimize)} egyéb ablak tálcára helyezve.'})
        except Exception as e:
            logging.warning(f"[STRESSTOOLS] Egyéb ablakok minimalizálási hiba: {e}")

    def _download_stresstools(self):
        import tempfile, urllib.request, urllib.error, zipfile, ssl, shutil
        # WinPE-ben a %TEMP% az X: RAM-diskre mutat - a stressztesztek zip-jét a valódi C: meghajtóra tesszük.
        is_pe = os.environ.get('SystemDrive', 'C:') == 'X:'
        if is_pe:
            temp_dir = r'C:\DV_Temp'
            os.makedirs(temp_dir, exist_ok=True)
        else:
            temp_dir = tempfile.gettempdir()
        stress_dir = os.path.join(temp_dir, "DriverVarázsló_Stress")
        marker_path = os.path.join(stress_dir, ".extract_complete")
        zip_path = os.path.join(temp_dir, "stresstools.zip")
        download_url = "https://github.com/egonixaimgod/DriverVarazslo/releases/download/stresstools.zip/stresstools.zip"

        # Csak akkor fogadjuk el a cache-t, ha a kicsomagolás korábban teljesen lefutott ÉS
        # a SumatraPDF, ÉS a HP driver is megvan benne. Ez utóbbi két feltétel azért kell,
        # mert mindkettőt UTÓLAG adtuk a stresstools.zip-hez (print_via_store_printer
        # miatt) - egy olyan gépen, ahol a stressz-teszt funkciót MÁR HASZNÁLTÁK a
        # frissítés(ek) előtt, a marker fájl egy régebbi, hiányos ZIP-ből származik, és
        # enélkül a plusz feltétel nélkül a sima marker-ellenőrzés örökre a régi cache-t
        # adná vissza - a friss ZIP-et sosem töltené le újra (terepen bizonyítottan
        # előfordul: ezen a gépen is, illetve egy random teszt-gépen is).
        if os.path.exists(marker_path) and self._find_sumatra_exe(stress_dir) and self._find_hp_driver_inf(stress_dir):
            return stress_dir

        # A "Minden teszt indítása" és az egyenkénti gombok is idekerülhetnek egyszerre
        # (utóbbiak nem mennek át a _task_busy-n, hogy egymás után gyorsan lehessen indítani
        # több eszközt is) - lock nélkül két egyidejű hívás ugyanabba a zip_path/stress_dir
        # mappába írna/csomagolna ki párhuzamosan, ami korrupciót okozhatna.
        with self._stresstools_download_lock:
            # Amíg a lock-ra vártunk, egy másik szál esetleg már befejezte a letöltést.
            if os.path.exists(marker_path) and self._find_sumatra_exe(stress_dir) and self._find_hp_driver_inf(stress_dir):
                return stress_dir
            try:
                logging.info("[STRESSTOOLS] Letöltés INNEN: " + download_url)
                ssl_ctx = ssl.create_default_context()

                req = urllib.request.Request(download_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
                try:
                    with urllib.request.urlopen(req, context=ssl_ctx, timeout=60) as resp, open(zip_path, 'wb') as f:
                        shutil.copyfileobj(resp, f)
                except urllib.error.URLError as dl_err:
                    # Vadonatúj Windows-telepítésen a gyökértanúsítvány-tár még hiányos: a
                    # Windows a gyökereket igény szerint tölti le, de ezt csak a schannel-
                    # alapú kliensek (böngésző, PowerShell, .NET) váltják ki - a Python
                    # OpenSSL-je nem, ezért nála CERTIFICATE_VERIFY_FAILED lesz. Tipikus
                    # tünet: a github.com (Sectigo/USERTrust gyökér) elhasal, miközben a
                    # raw.githubusercontent.com (DigiCert gyökér) működik - ezért megy az
                    # update-ellenőrzés ugyanazon a friss gépen, amin ez a letöltés nem.
                    # Ilyenkor PowerShell Invoke-WebRequest-tel (schannel) töltünk le: a
                    # tanúsítvány-ellenőrzés ott is teljes értékű (SEMMIT nem kapcsolunk
                    # ki!), és mellékhatásként a hiányzó gyökér bekerül a Windows tárba,
                    # így a gép későbbi Python-letöltései is meggyógyulnak.
                    if 'CERTIFICATE_VERIFY_FAILED' not in str(dl_err):
                        raise
                    logging.warning(f"[STRESSTOOLS] Python SSL tanúsítvány-hiba ({dl_err}) - friss Windows tanúsítvány-tár gyanú, áttérés PowerShell (schannel) letöltésre, teljes tanúsítvány-ellenőrzéssel...")
                    ps_cmd = ("$ProgressPreference='SilentlyContinue'; "
                              "[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072; "
                              f"Invoke-WebRequest -Uri '{_ps_quote(download_url)}' -OutFile '{_ps_quote(zip_path)}' -UseBasicParsing")
                    result = self._run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps_cmd], timeout=600)
                    if not result or result.returncode != 0 or not os.path.exists(zip_path):
                        logging.error("[STRESSTOOLS] A PowerShell (schannel) letöltés is sikertelen.")
                        return None
                    logging.info("[STRESSTOOLS] PowerShell (schannel) letöltés sikeres.")

                if not zipfile.is_zipfile(zip_path):
                    return None
                if os.path.exists(stress_dir):
                    shutil.rmtree(stress_dir, ignore_errors=True)
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(stress_dir)
                try: os.remove(zip_path)
                except: pass
                with open(marker_path, 'w') as f:
                    f.write('ok')
                return stress_dir
            except Exception as e:
                logging.error(f"[STRESSTOOLS] Download hiba: {e}")
                return None

    def start_stress_tests(self):
        logging.info("[API] start_stress_tests()")

        def worker():
            try:
                self.emit('task_start', {'task': 'stress', 'title': 'Stabilitás Teszt Indítása'})
                self._lock_power_for_stress()
                self.emit('task_progress', {'task': 'stress', 'log': '🌐 Tesztprogramok (ZIP) letöltése a háttérben...', 'indeterminate': True})

                stress_dir = self._download_stresstools()
                if not stress_dir:
                    raise Exception("Hiba a ZIP letöltésekor vagy kicsomagolásakor (Helytelen ZIP / Nincs net).")

                self.emit('task_progress', {'task': 'stress', 'log': '🔥 Programok rászabadítása a gépre...'})

                # Csak a STRESS_TOOLS_BULK-ban felsorolt (valódi terhelés-generáló) programok -
                # a HD Sentinel monitor kifejezett kérésre nincs benne a tömeges indításban.
                found = self._find_stress_tool_exes(stress_dir, STRESS_TOOLS_BULK)

                launched = 0
                pid_map = {}
                auto_threads = []
                for i, key in enumerate(STRESS_TOOLS_BULK):
                    display_name, _ = STRESS_TOOLS[key]
                    exe = found[key]
                    if exe and os.path.exists(exe):
                        if key == 'hwinfo':
                            try:
                                # CheckForUpdate=0: az indítás után felugró "HWiNFO Update"
                                # értesítő letiltása (ha a kulcsot nem venné figyelembe, a
                                # kattintás-szekvencia opcionális Bezárás-lépése a háló).
                                ini_path = os.path.join(os.path.dirname(exe), "HWiNFO64.INI")
                                with open(ini_path, "w") as f:
                                    f.write("[Settings]\nSensorsOnly=1\nCheckForUpdate=0\n")
                            except Exception:
                                pass
                        console_script = self._build_linpack_console_script() if key == 'linpack' else None
                        click_sequence = STRESS_CLICK_SEQUENCES.get(key)
                        pid = self._launch_stress_exe(exe, display_name, console_script=console_script, click_sequence=click_sequence, thread_sink=auto_threads)
                        if pid:
                            launched += 1
                            pid_map[key] = pid
                            self._stress_pids[key] = pid  # stop_stress_tests innen tudja, mit kell kilőni
                            self.emit('task_progress', {'task': 'stress', 'log': f'✅ Elindítva: {display_name}'})
                            if console_script:
                                self.emit('task_progress', {'task': 'stress', 'log': '  🤖 Linpack menü automatikus kitöltése elindult (RAM-választás + megerősítések).'})
                            if click_sequence:
                                self.emit('task_progress', {'task': 'stress', 'log': '  🤖 Indító dialógusok automatikus végignyomkodása elindult.'})
                        else:
                            self.emit('task_progress', {'task': 'stress', 'log': f'❌ Hiba indításnál: {display_name}'})
                    else:
                        self.emit('task_progress', {'task': 'stress', 'log': f'⚠️ Nem található a ZIP-ben: {display_name}'})
                    # Egymás után, ne egyszerre indítsuk a 4 programot - ha mind egy pillanatban
                    # próbál elindulni (GPU/CPU detektálás, ablak-létrehozás egyszerre), a gép
                    # erősen leterhelődhet, és ez akár fél perces késéseket okozhat a dialógusok
                    # megjelenésében (ezt debug logban is megfigyeltük).
                    if i < len(STRESS_TOOLS_BULK) - 1:
                        time.sleep(3)

                # Az automatizálási háttérszálak (dialógus-nyomkodás, Linpack menü-kitöltés)
                # VÉGÉT várjuk meg, korábban itt fix 30 mp várakozás volt - az terepen az
                # automatizálás közepén sütött el: a _minimize_other_windows pont a még meg
                # nem válaszolt dialógusokat tette tálcára, a FurMark render-ablaka pedig még
                # nem is létezett, amikor a pozicionálás lefutott. A plafon (240 mp) csak
                # végszükség-fék: normál esetben a szálak pár tíz mp alatt végeznek, egy
                # elakadt lépés pedig a saját 60 mp-es timeoutja után magától feladja.
                if launched > 0:
                    self.emit('task_progress', {'task': 'stress', 'log': '\n⏳ Várakozás, amíg az indító dialógusok automatikus végignyomkodása befejeződik...'})
                    join_deadline = time.time() + 240
                    waited = 0
                    while time.time() < join_deadline and any(t.is_alive() for t in auto_threads):
                        if self._check_cancel():
                            break
                        time.sleep(1)
                        waited += 1
                        if waited % 15 == 0:
                            still_running = [t.name.replace('auto:', '') for t in auto_threads if t.is_alive()]
                            self.emit('task_progress', {'task': 'stress', 'log': f'  ⏳ Még folyamatban: {", ".join(still_running)}...'})
                    # Rövid türelmi idő az UTOLSÓ kattintás után létrejövő végleges ablakoknak
                    # (pl. a FurMark render-ablaka a GO megnyomása után pár mp-cel jelenik
                    # meg). Rövid lehet: az automatizálási szálak a saját utolsó kattintásuk
                    # HATÁSÁT is bevárják (_verify_final_click), tehát mire ideérünk, a
                    # dialógusok bizonyítottan bezárultak - ez csak a fő ablakok megjelenési
                    # ideje, a felhasználói elvárás pedig az, hogy a nyomkodás után AZONNAL
                    # jöjjön a rendezés.
                    for _ in range(3):
                        if self._check_cancel():
                            break
                        time.sleep(1)
                    if self._check_cancel():
                        self.emit('task_progress', {'task': 'stress', 'log': '❗ Ablak-elrendezés kihagyva (megszakítva).'})
                    else:
                        self.emit('task_progress', {'task': 'stress', 'log': '🪟 Ablakok elrendezése...'})
                        self._position_stress_windows(pid_map, task_id='stress')

                if launched == len(STRESS_TOOLS_BULK):
                     self.emit('task_complete', {'task': 'stress', 'status': '👀 Minden teszt elindult. Égjen!'})
                elif launched > 0:
                     self.emit('task_complete', {'task': 'stress', 'status': f'⚠️ Csak {launched}/{len(STRESS_TOOLS_BULK)} program indult el.'})
                else:
                     self.emit('task_complete', {'task': 'stress', 'status': '❌ Egyetlen program sem indult el.'})

            except Exception as e:
                logging.error(f"Stressz teszt hiba: {e}")
                self.emit('task_error', {'task': 'stress', 'error': f'Hiba: {str(e)}'})

        self._safe_thread('stress', worker)

    def start_stress_tool(self, name):
        """Egyetlen stabilitás-teszt/monitor program elindítása (a Stabilitás Teszt nézet
        5 kis ikonja hívja). Tudatosan NEM megy át a task_start/progress-modal rendszeren -
        a felhasználó kifejezett kérése, hogy egy gombnyomásra csendben, ablak/dialógus
        nélkül induljon el a program, csak egy rövid toast-tal tájékoztatva."""
        logging.info(f"[API] start_stress_tool({name})")
        info = STRESS_TOOLS.get(name)
        if not info:
            self.emit('toast', {'message': f'❌ Ismeretlen program: {name}', 'type': 'error'})
            return
        display_name, _ = info

        def worker():
            import tempfile
            try:
                self._lock_power_for_stress()

                is_pe = os.environ.get('SystemDrive', 'C:') == 'X:'
                temp_dir = r'C:\DV_Temp' if is_pe else tempfile.gettempdir()
                marker_path = os.path.join(temp_dir, "DriverVarázsló_Stress", ".extract_complete")
                if not os.path.exists(marker_path):
                    self.emit('toast', {'message': f'⏳ {display_name}: első indítás, tesztprogramok letöltése (eltarthat egy percig)...', 'type': 'info'})

                stress_dir = self._download_stresstools()
                if not stress_dir:
                    self.emit('toast', {'message': f'❌ Hiba a tesztprogramok letöltésekor/kicsomagolásakor ({display_name})!', 'type': 'error'})
                    return

                exe_path = self._find_stress_tool_exes(stress_dir, [name])[name]

                if not exe_path or not os.path.exists(exe_path):
                    self.emit('toast', {'message': f'⚠️ {display_name} nem található a letöltött csomagban!', 'type': 'warning'})
                    return

                if name == 'hwinfo':
                    try:
                        # CheckForUpdate=0 - lásd a start_stress_tests azonos sorát.
                        ini_path = os.path.join(os.path.dirname(exe_path), "HWiNFO64.INI")
                        with open(ini_path, "w") as f:
                            f.write("[Settings]\nSensorsOnly=1\nCheckForUpdate=0\n")
                    except Exception:
                        pass

                console_script = self._build_linpack_console_script() if name == 'linpack' else None
                click_sequence = STRESS_CLICK_SEQUENCES.get(name)
                pid = self._launch_stress_exe(exe_path, display_name, console_script=console_script, click_sequence=click_sequence)
                if pid:
                    if pid > 0:
                        self._stress_pids[name] = pid  # stop_stress_tests innen tudja, mit kell kilőni
                    self.emit('toast', {'message': f'✅ {display_name} elindítva!', 'type': 'success'})
                else:
                    self.emit('toast', {'message': f'❌ Hiba a(z) {display_name} indításakor!', 'type': 'error'})
            except Exception as e:
                logging.error(f"[STRESSTOOLS] start_stress_tool hiba ({name}): {e}")
                self.emit('toast', {'message': f'❌ Hiba: {e}', 'type': 'error'})

        threading.Thread(target=worker, daemon=True).start()

    def stop_stress_tests(self):
        """Az ÖSSZES futó stressz-teszt/monitor program azonnali bezárása (a Stabilitás
        Teszt nézet és a stressz-folyamat modal piros gombja hívja). Két rétegben öl:
        (1) az általunk indított, eltárolt PID-ek teljes folyamatfája (taskkill /T - ez a
        Linpack cmd+linpack_engine gyerekeit is elviszi), (2) biztonsági hálóként a jól
        ismert programnevek szerint is (STRESS_KILL_IMAGES - pl. UAC 'runas' úton indított
        példány, ahol nincs PID-ünk). Végül visszaállítja a stressz-teszt által letiltott
        energiagazdálkodási beállításokat (képernyő-kikapcsolás/alvás), hiszen a tesztnek
        vége. Szándékosan nem megy át a _task_busy kapun: egy még futó (pl. ablakrendezésre
        váró) stressz-task mellett is azonnal működnie kell."""
        logging.info("[API] stop_stress_tests()")

        def worker():
            try:
                for key, pid in list(self._stress_pids.items()):
                    if pid and pid > 0:
                        self._run(['taskkill', '/PID', str(pid), '/T', '/F'])
                self._stress_pids = {}
                # Egyetlen taskkill hívás az összes ismert programnévre (a taskkill több
                # /IM kapcsolót is elfogad) - a "nem fut ilyen" esetek várható, ártalmatlan
                # hibakódot adnak, ezért nem egyenként hívjuk és nem is ellenőrizzük.
                image_args = []
                for image in STRESS_KILL_IMAGES:
                    image_args += ['/IM', image]
                self._run(['taskkill', '/F', '/T'] + image_args)
                try:
                    self._restore_power_after_stress()
                except Exception as e:
                    logging.warning(f"[STRESSTOOLS] Energiagazdálkodás visszaállítási hiba a leállítás után: {e}")
                logging.info("[STRESSTOOLS] Minden stressz-teszt program bezárva (stop_stress_tests).")
                self.emit('toast', {'message': '🛑 Minden stressz-teszt program bezárva.', 'type': 'success'})
            except Exception as e:
                logging.error(f"[STRESSTOOLS] stop_stress_tests hiba: {e}")
                self.emit('toast', {'message': f'❌ Hiba a tesztek bezárásakor: {e}', 'type': 'error'})

        threading.Thread(target=worker, daemon=True).start()

    def _check_cancel(self):
        """Ellenőrzi, hogy a felhasználó megszakította-e a műveletet."""
        if self._cancel_flag:
            logging.info("[CANCEL] Megszakítás flag aktiv!")
            return True
        return False

    def change_target_os(self):
        logging.info("[API] change_target_os() hívás")
        result = self._window.create_file_dialog(_FOLDER_DIALOG, allow_multiple=False)
        if result and len(result) > 0:
            d = os.path.abspath(result[0]).replace("/", "\\")
            has_win = os.path.exists(os.path.join(d, "Windows"))
            logging.info(f"[API] change_target_os: kiválasztva={d}, has_windows={has_win}")
            return {'path': d, 'has_windows': has_win}
        logging.info("[API] change_target_os: mégse")
        return None

    def apply_target_os(self, path):
        logging.info(f"[API] apply_target_os({path})")
        self.target_os_path = path
        return True

    def reset_target_os(self):
        logging.info("[API] reset_target_os() - visszatérés jelenlegi rendszerre")
        self.target_os_path = None
        return True

    def select_directory(self, title='Válassz mappát'):
        logging.info(f"[API] select_directory(title={title})")
        result = self._window.create_file_dialog(_FOLDER_DIALOG, allow_multiple=False)
        if result and len(result) > 0:
            logging.info(f"[API] select_directory: kiválasztva={result[0]}")
            return result[0]
        logging.info("[API] select_directory: mégse")
        return None

    def select_file(self, title='Válassz fájlt', file_types=''):
        logging.info(f"[API] select_file(title={title}, types={file_types})")
        ft = (file_types.split('|')[0],) if file_types else ()
        result = self._window.create_file_dialog(_OPEN_DIALOG, allow_multiple=False, file_types=ft)
        if result and len(result) > 0:
            logging.info(f"[API] select_file: kiválasztva={result[0]}")
            return result[0]
        logging.info("[API] select_file: mégse")
        return None

    # ================================================================
    # DRIVER LISTING
    # ================================================================
    def load_drivers(self, all_drivers=False):
        logging.info(f"[API] load_drivers(all_drivers={all_drivers})")
        def worker():
            self.emit('drivers_loading')
            start = time.time()
            try:
                if self.target_os_path:
                    logging.info(f"[DRIVERS] Offline mód: {self.target_os_path}")
                    drivers = self._get_offline_drivers(all_drivers)
                elif all_drivers:
                    logging.info("[DRIVERS] Összes driver lekérdezés (élő rendszer)")
                    drivers = self._get_all_drivers()
                else:
                    logging.info("[DRIVERS] Third-party driverek lekérdezés")
                    drivers = self._get_third_party_drivers()
                elapsed = time.time() - start
                logging.info(f"[DRIVERS] Betöltve: {len(drivers)} driver ({elapsed:.1f}s)")
                self.emit('drivers_loaded', {'drivers': drivers, 'elapsed': round(elapsed, 1)})
            except Exception as e:
                logging.error(f"[DRIVERS] Betöltési hiba: {e}")
                logging.error(traceback.format_exc())
                self.emit('drivers_loaded', {'drivers': [], 'elapsed': 0, 'error': str(e)})
        threading.Thread(target=worker, daemon=True).start()

    def _get_third_party_drivers(self):
        logging.debug("[DRIVERS] dism /English /Online /Get-Drivers futtatása...")
        res = self._run(['dism', '/English', '/Online', '/Get-Drivers'])
        drivers = []
        current = {}
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                if current and "published" in current:
                    drivers.append(current)
                    current = {}
                continue
            parts = line.split(":", 1)
            if len(parts) == 2:
                key, val = parts[0].strip(), parts[1].strip()
                if "Published Name" in key:
                    current["published"] = val
                elif "Original File Name" in key:
                    current["original"] = val
                elif "Provider Name" in key:
                    current["provider"] = val
                elif "Class Name" in key:
                    current["class"] = val
                elif "Version" in key:
                    current["version"] = val
        if current and "published" in current:
            drivers.append(current)
        return drivers

    def _get_all_drivers(self):
        logging.debug("[DRIVERS] _get_all_drivers() indult")
        cmd = ['powershell', '-NoProfile', '-Command',
               '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Get-WindowsDriver -Online -All | Select-Object ProviderName, ClassName, Version, Driver, OriginalFileName | ConvertTo-Json -Depth 2 -WarningAction SilentlyContinue']
        res = self._run(cmd, encoding='utf-8')
        out = res.stdout.strip()
        if not out:
            logging.debug("[DRIVERS] _get_all_drivers: üres kimenet")
            return []
        data = json.loads(out)
        if isinstance(data, dict):
            data = [data]
        parsed_drivers = [{"published": d.get("Driver", ""), "original": d.get("OriginalFileName", ""),
                 "provider": d.get("ProviderName", ""), "class": d.get("ClassName", ""),
                 "version": d.get("Version", "")} for d in data]

        # Filter ghosts (force-deleted inbox drivers)
        valid_drivers = []
        rep = os.path.join(os.environ.get('SYSTEMROOT', r'C:\Windows'), "System32", "DriverStore", "FileRepository")
        for d in parsed_drivers:
            pub = d.get("published", "")
            if not pub:
                continue
            if pub.lower().startswith("oem"):
                valid_drivers.append(d)
                continue
            if glob.glob(os.path.join(rep, f"{pub}_*")):
                valid_drivers.append(d)

        logging.debug(f"[DRIVERS] _get_all_drivers: {len(valid_drivers)} valid driver")
        return valid_drivers

    def _get_offline_drivers(self, all_drivers=False):
        logging.debug(f"[DRIVERS] _get_offline_drivers(all_drivers={all_drivers})")
        cmd = ['dism', '/English', f'/Image:{self.target_os_path}', '/Get-Drivers']
        if all_drivers:
            cmd.append('/all')
        res = self._run(cmd)
        drivers = []
        current = {}
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                if current and "published" in current:
                    drivers.append(current)
                    current = {}
                continue
            parts = line.split(":", 1)
            if len(parts) == 2:
                key, val = parts[0].strip(), parts[1].strip()
                if "Published Name" in key:
                    current["published"] = val
                elif "Original File Name" in key:
                    current["original"] = val
                elif "Provider Name" in key:
                    current["provider"] = val
                elif "Class Name" in key:
                    current["class"] = val
                elif "Version" in key:
                    current["version"] = val
        if current and "published" in current:
            drivers.append(current)

        # Filter ghosts (force-deleted inbox drivers)
        valid_drivers = []
        rep = os.path.join(self.target_os_path, "Windows", "System32", "DriverStore", "FileRepository")
        for d in drivers:
            pub = d.get("published", "")
            if not pub:
                continue
            if pub.lower().startswith("oem"):
                valid_drivers.append(d)
                continue
            if glob.glob(os.path.join(rep, f"{pub}_*")):
                valid_drivers.append(d)

        logging.debug(f"[DRIVERS] _get_offline_drivers: {len(valid_drivers)} valid driver")
        return valid_drivers

    # ================================================================
    # BCD REPAIR (boot loader javítás offline restore után)
    # ================================================================
    def _repair_bcd(self, target_drive):
        """BCD újraépítése offline restore után - megakadályozza a boot hibákat."""
        logging.info(f"[BCD] BCD javítás indítása: {target_drive}")
        self.emit('task_progress', {'task': 'restore', 'log': '\n--- BOOT LOADER (BCD) JAVÍTÁS ---'})
        
        target_drive = target_drive.rstrip('\\') + '\\'
        windows_path = os.path.join(target_drive, 'Windows')
        
        if not os.path.exists(windows_path):
            self.emit('task_progress', {'task': 'restore', 'log': f'⚠️ Windows mappa nem található: {windows_path}'})
            return False
            
        success = False
        
        # 1. Próbáljuk a legegyszerűbb módszert (ALL)
        self.emit('task_progress', {'task': 'restore', 'log': f'bcdboot {target_drive}Windows /f ALL'})
        res = self._run(['bcdboot', f'{target_drive}Windows', '/f', 'ALL'])
        if res.returncode == 0:
            success = True
            self.emit('task_progress', {'task': 'restore', 'log': '✅ BCD sikeresen újraépítve (ALL)!'})
        else:
            err_msg = res.stderr.strip() if res.stderr else res.stdout.strip() if res.stdout else f'Exit code: {res.returncode}'
            self.emit('task_progress', {'task': 'restore', 'log': f'⚠️ bcdboot hiba (0x{res.returncode:X}): {err_msg[:300]}'})
            
        # 2. bootrec parancsok (ha a bcdboot nem sikerült teljesen)
        if not success:
            self.emit('task_progress', {'task': 'restore', 'log': 'bootrec parancsok futtatása...'})
            for cmd in ['/fixmbr', '/fixboot', '/rebuildbcd']:
                res = self._run(['bootrec', cmd])
                if res.returncode == 0:
                    self.emit('task_progress', {'task': 'restore', 'log': f'  bootrec {cmd}: ✅'})
                else:
                    self.emit('task_progress', {'task': 'restore', 'log': f'  bootrec {cmd}: ⚠️ (nem elérhető)'})
        
        logging.info(f"[BCD] Javítás befejezve, success={success}")
        return success

    # ================================================================
    # DRIVER DELETION
    # ================================================================
    def delete_drivers(self, published_names, list_all=False, reboot=False):
        logging.info(f"[API] delete_drivers() - {len(published_names)} driver, list_all={list_all}, reboot={reboot}")
        logging.info(f"[DELETE] Törlendő driverek: {published_names}")
        def worker():
            total = len(published_names)
            success = 0
            fail = 0
            logging.info(f"[DELETE] Törlés indulása: {total} db driver")
            self.emit('task_start', {'task': 'delete', 'title': f'Törlés folyamatban... ({total} driver)'})
            self.emit('task_progress', {'task': 'delete', 'log': f'Kijelölt driverek törlése indult ({total} db)'})

            cancelled = False
            for i, pub in enumerate(published_names):
                if self._cancel_flag:
                    self.emit('task_progress', {'task': 'delete', 'log': '❗ Törlés megszakítva a felhasználó által!'})
                    self.emit('task_progress', {'status': '❗ Megszakítva!', 'counter': f'{i} / {total}'})
                    cancelled = True
                    break
                
                self.emit('task_progress', {
                    'task': 'delete', 'current': i, 'total': total,
                    'status': f'Törlés: {pub}', 'counter': f'{i+1} / {total}',
                    'log': f'🗑 Törlés: {pub}'
                })
                try:
                    is_offline = bool(self.target_os_path)
                    is_oem = pub.lower().startswith("oem")

                    if is_offline:
                        res = self._run(['dism', f'/Image:{self.target_os_path}', '/Remove-Driver', f'/Driver:{pub}'])
                    else:
                        res = self._run(['pnputil', '/delete-driver', pub, '/uninstall', '/force'])

                    if res.returncode == 0 or any(k in res.stdout for k in ["Deleted", "törölve", "successfully"]):
                        success += 1
                        self.emit('task_progress', {'task': 'delete', 'log': f'  ✅ {pub} törölve'})
                    else:
                        if list_all and not is_oem:
                            if is_offline:
                                rep = os.path.join(self.target_os_path, "Windows", "System32", "DriverStore", "FileRepository")
                                inf_dir = os.path.join(self.target_os_path, "Windows", "INF")
                            else:
                                rep = os.path.join(os.environ.get('SYSTEMROOT', r'C:\Windows'), "System32", "DriverStore", "FileRepository")
                                inf_dir = os.path.join(os.environ.get('SYSTEMROOT', r'C:\Windows'), "INF")
                            dirs = glob.glob(os.path.join(rep, f"{pub}_*"))
                            
                            found_any = False
                            if dirs:
                                for d in dirs:
                                    self._run(f'takeown /f "{d}" /r /A', shell=True)
                                    self._run(f'icacls "{d}" /grant *S-1-5-32-544:F /t', shell=True)
                                    shutil.rmtree(d, ignore_errors=True)
                                    self._run(f'rmdir /s /q "{d}"', shell=True)
                                found_any = True

                            bname = os.path.splitext(pub)[0]
                            for ext in ['.inf', '.pnf', '.INF', '.PNF']:
                                fpath = os.path.join(inf_dir, bname + ext)
                                if os.path.exists(fpath):
                                    self._run(f'takeown /f "{fpath}" /A', shell=True)
                                    self._run(f'icacls "{fpath}" /grant *S-1-5-32-544:F', shell=True)
                                    try:
                                        os.remove(fpath)
                                        found_any = True
                                    except OSError:
                                        self._run(f'del /f /q "{fpath}"', shell=True)
                                        found_any = True

                            if found_any:
                                success += 1
                                self.emit('task_progress', {'task': 'delete', 'log': f'  ✅ {pub} törölve (force)'})
                            else:
                                fail += 1
                                self.emit('task_progress', {'task': 'delete', 'log': f'  ❌ {pub} sikertelen (nem található)'})
                        else:
                            fail += 1
                            self.emit('task_progress', {'task': 'delete', 'log': f'  ❌ {pub} sikertelen'})
                except Exception as e:
                    fail += 1
                    self.emit('task_progress', {'task': 'delete', 'log': f'  ❌ {pub} hiba: {e}'})

            # Post-delete scan
            is_offline = bool(self.target_os_path)
            is_pe = os.environ.get('SystemDrive', 'C:') == 'X:'
            if not is_offline and not is_pe and success > 0:
                self.emit('task_progress', {'task': 'delete', 'log': 'Hardverek újraszkennelése...', 'status': 'Hardverek újraszkennelése...'})
                self._run(['pnputil', '/scan-devices'])
                time.sleep(10)
                self.emit('task_progress', {'task': 'delete', 'log': '✅ Hardverek frissítve!'})

            if cancelled:
                self.emit('task_progress', {'task': 'delete', 'log': f'\n--- MEGSZAKÍTVA! Sikeres: {success}, Sikertelen: {fail} ---', 'current': i, 'total': total})
                self.emit('task_complete', {'task': 'delete', 'success': success, 'fail': fail,
                                            'counter': '❗ Megszakítva',
                                            'status': f'❗ Megszakítva! Sikeres: {success}, Sikertelen: {fail}'})
            else:
                self.emit('task_progress', {'task': 'delete', 'log': f'\n--- Sikeres: {success}, Sikertelen: {fail} ---', 'current': total, 'total': total})
                self.emit('task_complete', {'task': 'delete', 'success': success, 'fail': fail,
                                            'counter': f'✅ {success} / ❌ {fail}',
                                            'status': f'Kész! Sikeres: {success}, Sikertelen: {fail}'})
                
                # Újraindítás ha kérték
                if reboot and success > 0:
                    self.emit('task_progress', {'task': 'delete', 'log': '\n🔄 Újraindítás 5 másodperc múlva...'})
                    time.sleep(5)
                    self._run(['shutdown', '/r', '/t', '0', '/f'])

        self._safe_thread('delete', worker)

    # ================================================================
    # HARDWARE SCAN
    # ================================================================
    def delete_ghost_devices(self):
        logging.info("[API] delete_ghost_devices()")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: Ez a funkció csak Élő (Online) rendszeren működik!', 'type': 'error'})
            return
        def worker():
            logging.info("[GHOST] Szellemeszközök törlésének indítása...")
            self.emit('task_start', {'task': 'ghost', 'title': 'Szellemeszközök Törlése'})
            self.emit('task_progress', {'task': 'ghost', 'log': 'Nem csatlakoztatott (fantom) eszközök azonosítása...', 'indeterminate': True})

            ps_script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ghosts = Get-PnpDevice -PresentOnly:$false | Where-Object { $_.Present -eq $false -and $_.InstanceId -ne $null -and $_.PNPClass -ne 'SoftwareDevice' -and $_.PNPClass -ne 'Net' -and $_.PNPClass -ne 'System' }
$count = 0
$total = @($ghosts).Count
if ($total -eq 0) {
    Write-Output "DONE: Nincs szellemeszköz a rendszerben."
    exit
}
Write-Output "TOTAL: $total"
foreach ($dev in $ghosts) {
    $id = $dev.PNPDeviceID
    $name = $dev.Name
    if (-not $name) { $name = "Ismeretlen eszköz" }
    Write-Output "RM: $name"
    $res = & pnputil /remove-device "$($id)" 2>&1
    if ($LASTEXITCODE -eq 0 -or $res -match "deleted" -or $res -match "törölve" -or $res -match "successfully") {
        Write-Output "OK: $name"
        $count++
    } else {
        Write-Output "FAIL: $name"
    }
}
Write-Output "DONE: Törölve: $count / $total"
"""
            process = subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                startupinfo=self._si, creationflags=self._nw)
            
            success = 0
            total = 0
            
            for line in process.stdout:
                if self._check_cancel():
                    self._run(['taskkill', '/F', '/T', '/PID', str(process.pid)])
                    process.wait()
                    self.emit('task_progress', {'task': 'ghost', 'log': '\n❗ Megszakítva!'})
                    self.emit('task_complete', {'task': 'ghost', 'status': '❗ Megszakítva!', 'success': success, 'fail': total-success})
                    return
                line = line.strip()
                if not line:
                    continue
                if line.startswith("TOTAL:"):
                    m = re.search(r'TOTAL:\s*(\d+)', line)
                    if m:
                        total = int(m.group(1))
                    self.emit('task_progress', {'task': 'ghost', 'log': f'Összesen {total} db szellemeszköz azonosítva...\n', 'total': total, 'current': 0, 'counter': f'0 / {total}'})
                elif line.startswith("RM:"):
                    self.emit('task_progress', {'task': 'ghost', 'log': f'  🗑 Próbálkozás: {line[3:].strip()}', 'status': f'Eltávolítás: {line[3:].strip()}'})
                elif line.startswith("OK:"):
                    success += 1
                    self.emit('task_progress', {'task': 'ghost', 'log': f'  ✅ Sikeresen törölve: {line[3:].strip()}', 'current': success, 'counter': f'{success} / {total}'})
                elif line.startswith("FAIL:"):
                    self.emit('task_progress', {'task': 'ghost', 'log': f'  ❌ Sikertelen (valószínűleg védett eszköz): {line[5:].strip()}', 'current': success, 'counter': f'{success} / {total}'})
                elif line.startswith("DONE:"):
                    self.emit('task_progress', {'task': 'ghost', 'log': f'\n{line[5:].strip()}'})
                else:
                    self.emit('task_progress', {'task': 'ghost', 'log': line})
            
            process.wait()
            self.emit('task_progress', {'task': 'ghost', 'log': '✅ Szellemeszközök törlése befejeződött.'})
            self.emit('task_complete', {'task': 'ghost', 'status': f'Kész! Törölve: {success} / {total}'})

        self._safe_thread('ghost', worker)

    # ================================================================
    # TEMP FÁJLOK TÖRLÉSE (lemez felszabadítás)
    # ================================================================
    def clean_temp_files(self, options=None):
        """Windows ideiglenes fájlok törlése a bejelölt kategóriák szerint - lásd
        _temp_clean_category_defs a teljes listáért (felhasználói/rendszer TEMP, WU/
        Delivery Optimization cache, hibajelentések, Shader Cache, CBS logok, Crash
        Dumpok, IE/Edge cache), plusz a két speciális kategória (miniatűr-gyorsítótár,
        Lomtár). Csak élő (online) rendszeren értelmezhető - ua. mint a
        szellemeszköz-törlésnél: egy célzott offline OS TEMP mappáinak törlése minden
        felhasználói profilra kiterjedne (nem tudnánk, melyik "aktuális" felhasználóé a
        %TEMP%), ezért nem támogatott."""
        logging.info(f"[API] clean_temp_files(options={options})")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: Ez a funkció csak Élő (Online) rendszeren működik!', 'type': 'error'})
            return

        opts = options or {}
        folder_categories = []
        for key, label, paths, services, default_checked in _temp_clean_category_defs(self.sys_drive):
            if opts.get(key, default_checked) and paths:
                folder_categories.append((label, paths, services))
        do_thumbnails = opts.get('thumbnail_cache', False)
        do_recycle_bin = opts.get('recycle_bin', False)

        if not folder_categories and not do_thumbnails and not do_recycle_bin:
            self.emit('toast', {'message': '⚠️ Nincs kiválasztva egyetlen törlendő kategória sem!', 'type': 'warning'})
            return

        def worker():
            self.emit('task_start', {'task': 'tempclean', 'title': 'Temp Fájlok Törlése'})
            total_freed = 0
            total_removed = 0
            total_failed = 0

            # Néhány kategória (WU cache, Delivery Optimization) mappáját egy szolgáltatás
            # tartja zárolva - ezeket egyszerre, egy körben állítjuk le/indítjuk újra (nem
            # kategóriánként), hogy egy szolgáltatást ne kelljen kétszer le-/felkapcsolni,
            # ha véletlenül több kategória is hivatkozna rá.
            services_to_stop = sorted({s for _, _, services in folder_categories for s in services})
            if services_to_stop:
                self.emit('task_progress', {'task': 'tempclean', 'log': f'⏸️ Szolgáltatások leállítása a cache törléséhez ({", ".join(services_to_stop)})...', 'indeterminate': True})
                self._run(['powershell', '-NoProfile', '-Command', f'Stop-Service {",".join(services_to_stop)} -Force -ErrorAction SilentlyContinue'])

            for label, paths, _services in folder_categories:
                if self._check_cancel():
                    break
                cat_freed = cat_removed = cat_failed = 0
                for path in paths:
                    self.emit('task_progress', {'task': 'tempclean', 'log': f'{label} törlése ({path})...', 'indeterminate': True})
                    freed, removed, failed = _clean_folder_contents(path, self._check_cancel)
                    cat_freed += freed
                    cat_removed += removed
                    cat_failed += failed
                self.emit('task_progress', {'task': 'tempclean', 'log': f'  ✅ {cat_removed} elem törölve, {cat_failed} zárolt/hozzáférhetetlen elem kihagyva ({_fmt_bytes(cat_freed)} felszabadítva).'})
                total_freed += cat_freed
                total_removed += cat_removed
                total_failed += cat_failed

            if services_to_stop:
                self.emit('task_progress', {'task': 'tempclean', 'log': '▶️ Szolgáltatások újraindítása...'})
                self._run(['powershell', '-NoProfile', '-Command', f'Start-Service {",".join(services_to_stop)} -ErrorAction SilentlyContinue'])

            if do_thumbnails and not self._check_cancel():
                self.emit('task_progress', {'task': 'tempclean', 'log': '🖼️ Miniatűr (thumbnail) gyorsítótár törlése...', 'indeterminate': True})
                freed = removed = failed = 0
                local = os.environ.get('LOCALAPPDATA')
                explorer_dir = os.path.join(local, 'Microsoft', 'Windows', 'Explorer') if local else None
                if explorer_dir and os.path.isdir(explorer_dir):
                    for name in os.listdir(explorer_dir):
                        if not (name.startswith('thumbcache_') or name.startswith('iconcache_')):
                            continue
                        full = os.path.join(explorer_dir, name)
                        try:
                            size = os.path.getsize(full)
                            os.remove(full)
                            freed += size
                            removed += 1
                        except Exception as e:
                            failed += 1
                            logging.debug(f"[TEMPCLEAN] Nem törölhető ({full}): {e}")
                self.emit('task_progress', {'task': 'tempclean', 'log': f'  ✅ {removed} fájl törölve, {failed} kihagyva ({_fmt_bytes(freed)} felszabadítva).'})
                total_freed += freed
                total_removed += removed
                total_failed += failed

            if do_recycle_bin and not self._check_cancel():
                self.emit('task_progress', {'task': 'tempclean', 'log': '🗑️ Lomtár ürítése...', 'indeterminate': True})
                rb_freed = _empty_recycle_bin()
                self.emit('task_progress', {'task': 'tempclean', 'log': f'  ✅ Lomtár kiürítve ({_fmt_bytes(rb_freed)} felszabadítva).'})
                total_freed += rb_freed
                total_removed += 1

            if self._check_cancel():
                self.emit('task_progress', {'task': 'tempclean', 'log': '\n❗ Megszakítva!'})
                self.emit('task_complete', {'task': 'tempclean', 'status': f'❗ Megszakítva! Eddig felszabadítva: {_fmt_bytes(total_freed)}'})
                return

            self.emit('task_progress', {'task': 'tempclean', 'log': f'\n✅ Kész! Összesen {total_removed} elem törölve, {total_failed} kihagyva (zárolt/hozzáférhetetlen fájlok - ez normális, ha épp használatban vannak).'})
            self.emit('task_complete', {'task': 'tempclean', 'status': f'🧹 Felszabadított hely: {_fmt_bytes(total_freed)}'})

        self._safe_thread('tempclean', worker)

    def _check_internet(self):
        """Megbízható TCP port alapú internet ellenőrzés."""
        import socket
        try:
            socket.setdefaulttimeout(3.0)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect(("8.8.8.8", 53))
            return True
        except Exception:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.connect(("www.microsoft.com", 80))
                return True
            except Exception:
                return False

    def start_hw_scan(self):
        logging.info("[API] start_hw_scan() hívás")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: Hardver keresés csak Élő rendszeren működik!', 'type': 'error'})
            self.emit('hw_scan_result', {'pool': [], 'installed': [], 'sys_info': '❌ Offline módban nem elérhető', 'time': ''})
            return

        if self._hw_scanning:
            logging.warning("[HW_SCAN] Már fut egy scan!")
            return
        if self._task_busy:
            # Megosztjuk a _safe_thread-alapú feladatokkal ugyanazt a "busy" jelzőt, mert
            # a scan és egy driver-telepítés/törlés egyszerre futva ugyanazt a
            # self.hw_updates_pool listát írná-olvasná (race condition).
            logging.warning(f"[HW_SCAN] Elutasítva - már fut egy másik feladat ({self._task_busy}).")
            self.emit('toast', {'message': f'⚠️ Már folyamatban van egy másik művelet ({self._task_busy}), várd meg amíg befejeződik!', 'type': 'warning'})
            # A JS oldal a scan gomb megnyomásakor azonnal "folyamatban" állapotba kapcsol -
            # e nélkül az emit nélkül elutasítás esetén a progress sáv örökre "Scannelés
            # folyamatban..." állapotban ragadna, hiszen sosem indul valódi scan-szál.
            self.emit('hw_scan_result', {'pool': self.hw_updates_pool, 'installed': self._hw_installed_devs,
                                          'sys_info': f'⚠️ Másik művelet ({self._task_busy}) fut, próbáld újra pár másodperc múlva', 'time': ''})
            return
        self._hw_scanning = True
        self._task_busy = 'hw_scan'
        logging.info("[HW_SCAN] Hardver scan indítása...")

        def worker():
            try:
                _start = time.time()
                
                # Internet ellenőrzés
                self.emit('hw_scan_progress', {'status': '⏳ Internetkapcsolat ellenőrzése...'})
                if not self._check_internet():
                    self.emit('toast', {'message': '❌ Nincs internetkapcsolat! Telepíts egy hálózati drivert!', 'type': 'error'})
                    self.emit('hw_scan_result', {'pool': [], 'installed': [], 'sys_info': '❌ Nincs Internet!', 'time': ''})
                    return
                
                # Hardver változások frissítése szkennelés előtt
                logging.info("[HW_SCAN] Eszközök újra-szkennelése (PnP)...")
                self.emit('hw_scan_progress', {'status': '⏳ Hardver változások keresése...'})
                self._run(['pnputil', '/scan-devices'])
                time.sleep(2)
                
                sys_info_text = "Ismeretlen PC / Laptop"
                logging.info("[HW_SCAN] Rendszer info lekérdezése...")
                self.emit('hw_scan_progress', {'status': '⏳ Rendszer információk lekérdezése...'})

                # System info
                try:
                    ps_cmd = (
                        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                        "$cs = Get-WmiObject Win32_ComputerSystem | Select-Object Manufacturer, Model, PCSystemType; "
                        "$bb = Get-WmiObject Win32_BaseBoard | Select-Object Manufacturer, Product; "
                        "$enc = Get-WmiObject Win32_SystemEnclosure | Select-Object ChassisTypes; "
                        "@{CS=$cs; BB=$bb; ENC=$enc} | ConvertTo-Json -Depth 3"
                    )
                    res = self._run(["powershell", "-NoProfile", "-Command", ps_cmd], encoding='utf-8')
                    if res.stdout.strip():
                        data = json.loads(res.stdout.strip())
                        cs = data.get("CS", {}) or {}
                        bb = data.get("BB", {}) or {}
                        enc = data.get("ENC", {}) or {}

                        man = (cs.get("Manufacturer") or "").strip()
                        mod = (cs.get("Model") or "").strip()
                        pct = cs.get("PCSystemType", -1)

                        # Fallback: ha OEM placeholder, használjuk az alaplap infót
                        oem_junk = {"to be filled by o.e.m.", "default string", "system manufacturer",
                                    "system product name", "not applicable", ""}
                        if man.lower() in oem_junk:
                            man = (bb.get("Manufacturer") or "").strip()
                        if mod.lower() in oem_junk:
                            mod = (bb.get("Product") or "").strip()
                        if man.lower() in oem_junk:
                            man = "Ismeretlen gyártó"
                        if mod.lower() in oem_junk:
                            mod = "Ismeretlen modell"

                        # Chassis-alapú laptop/desktop detekció (pontosabb mint PCSystemType)
                        chassis = enc.get("ChassisTypes", []) or []
                        if isinstance(chassis, int):
                            chassis = [chassis]
                        laptop_chassis = {8, 9, 10, 11, 14, 30, 31, 32}  # Portable, Laptop, Notebook, Sub Notebook, etc.
                        is_laptop = pct == 2 or any(c in laptop_chassis for c in chassis)
                        prefix = "💻 Laptop" if is_laptop else "🖥️ Asztali (Desktop)"

                        sys_info_text = f"{prefix} | {man} - {mod}"
                except Exception as e:
                    logging.debug(e)
                self.emit('hw_scan_progress', {'sys_info': sys_info_text, 'status': '⏳ PnP eszközök lekérdezése...'})

                # PnP devices - a szűrés/kategorizálás a KÖZÖS _filter_wu_scan_devices-ben él
                # (az AutoFix ugyanezt használja - ne ide írj eszköz-szűrési logikát!)
                pnp_data = []
                try:
                    res = self._run(["powershell", "-NoProfile", "-Command", WU_PNP_QUERY_PS], encoding='utf-8')
                    if res.stdout:
                        pnp_data = json.loads(res.stdout)
                except Exception as ex:
                    logging.error(f"PNP Query error: {ex}")

                self.emit('hw_scan_progress', {'status': '📋 PnP eszközök szűrése...'})

                devices_to_check = _filter_wu_scan_devices(pnp_data)

                logging.info(f"PnP szürés: {len(devices_to_check)} eszköz átment")
                total_devs = len(devices_to_check)
                # WU COM API search
                self.emit('hw_scan_progress', {'status': f'✅ {total_devs} hardverelem azonosítva, WU keresés indul...',
                                               'sys_info': f'{sys_info_text} | ⏳ Driver keresés...'})

                self.hw_updates_pool = []
                self._hw_installed_devs = []
                self.wu_api_mode = True
                
                # Közvetlen WU API lekérdezés (a COM objektum ezen kulcs módosítása nélkül is látja a drivereket)
                wu_results = self._search_wu_api()
                wu_api_success = wu_results is not None

                if wu_results is None:
                    wu_results = []

                self.emit('hw_scan_progress', {'status': '📋 Eredmények feldolgozása...'})

                # Párosítás a KÖZÖS _match_wu_updates_to_devices-szel (HWID prefix + név-tartalék,
                # az AutoFix is pontosan ezt hívja - ne ide írj párosítási logikát!)
                matches = _match_wu_updates_to_devices(wu_results, devices_to_check)
                matched_hwids = set()
                matched_uids = set()
                for m in matches:
                    dev = m['device']
                    matched_hwids.add(dev['id'])
                    matched_uids.add(m['uid'])
                    self.hw_updates_pool.append({
                        "name": dev['name'], "cat": dev['cat'], "hwid": dev['id'],
                        "wu_title": m['title'], "pnp_id": dev.get('pnp_id', ''),
                        # A pontos WU UpdateID a telepítéshez: e nélkül a telepítő csak
                        # HWID-prefix alapján tudna szűrni, ami azonos HWID-jű csomagoknál
                        # (pl. Realtek Extension + MEDIA ugyanazon hdaudio ID-n) többet
                        # telepítene, mint amit a felhasználó kijelölt.
                        "update_id": m['uid']
                    })
                # A párosítatlan (ghost) WU-találatok kimaradnak a poolból
                for wu in wu_results:
                    if wu.get('UpdateID') not in matched_uids:
                        logging.debug(f"[WU_API] Ghost / Unmatched eszköz kihagyva: {wu.get('Title')}")

                self._hw_installed_devs = [dev for dev in devices_to_check if dev['id'] not in matched_hwids]

                # Catalog fallback if WU API failed
                if not self.hw_updates_pool and not wu_api_success:
                    self.wu_api_mode = False
                    self.emit('hw_scan_progress', {'status': f'🌐 WU API hiba, katalógus keresés ({total_devs} eszköz)...'})
                    self._catalog_search(devices_to_check)

                elapsed = int(time.time() - _start)
                _m, _s = divmod(elapsed, 60)
                time_str = f"{_m} perc {_s} mp" if _m else f"{_s} mp"
                mode = "WU API" if self.wu_api_mode else "Katalógus"
                found = len(self.hw_updates_pool)
                final_sys = f"{sys_info_text} | ✅ Kész ({mode})! {found} frissítés ({total_devs} eszköz)"

                self.emit('hw_scan_result', {
                    'pool': self.hw_updates_pool, 'installed': self._hw_installed_devs,
                    'sys_info': final_sys, 'time': time_str
                })
                self._hw_loaded = True
            except Exception as e:
                logging.error(f"hw_scan crash: {e}")
                logging.error(traceback.format_exc())
                self.emit('hw_scan_progress', {'status': '❌ Hiba történt!'})
                self.emit('hw_scan_result', {'pool': [], 'installed': [], 'sys_info': '❌ Scan hiba', 'time': ''})
            finally:
                self._hw_scanning = False
                self._task_busy = None

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception as e:
            logging.error(f"[HW_SCAN] Thread indítási hiba: {e}")
            self._hw_scanning = False
            self._task_busy = None
            self.emit('hw_scan_result', {'pool': [], 'installed': [], 'sys_info': '❌ Thread hiba', 'time': ''})

    def _set_wu_pause(self, pause=True):
        if pause:
            ps = r'''
            $pauseDate = (Get-Date).AddDays(7).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
            $nowDate = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
            if (!(Test-Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings')) { New-Item -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Force | Out-Null }
            Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseUpdatesExpiryTime' -Value $pauseDate -Type String -Force | Out-Null
            Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseFeatureUpdatesEndTime' -Value $pauseDate -Type String -Force | Out-Null
            Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseQualityUpdatesEndTime' -Value $pauseDate -Type String -Force | Out-Null
            Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseUpdatesStartTime' -Value $nowDate -Type String -Force | Out-Null
            Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseFeatureUpdatesStartTime' -Value $nowDate -Type String -Force | Out-Null
            Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseQualityUpdatesStartTime' -Value $nowDate -Type String -Force | Out-Null
            '''
            self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps])
            self._run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching', '/v', 'SearchOrderConfig', '/t', 'REG_DWORD', '/d', '0', '/f'])
            self._run(['reg', 'add', r'HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate', '/v', 'ExcludeWUDriversInQualityUpdate', '/t', 'REG_DWORD', '/d', '1', '/f'])
        else:
            ps = r'''
            Remove-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseUpdatesExpiryTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseFeatureUpdatesEndTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseQualityUpdatesEndTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseUpdatesStartTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseFeatureUpdatesStartTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseQualityUpdatesStartTime' -ErrorAction SilentlyContinue
            Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
            Start-Service wuauserv -ErrorAction SilentlyContinue
            '''
            self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps])
            self._run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching', '/v', 'SearchOrderConfig', '/t', 'REG_DWORD', '/d', '1', '/f'])
            self._run(['reg', 'delete', r'HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate', '/v', 'ExcludeWUDriversInQualityUpdate', '/f'])

    def _search_wu_api(self):
        logging.info("[WU_API] _search_wu_api() indult...")
        try:
            ps_cmd = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
try {
    $Session = New-Object -ComObject Microsoft.Update.Session
    $Searcher = $Session.CreateUpdateSearcher()
    try {
        $SM = New-Object -ComObject Microsoft.Update.ServiceManager
        $SM.AddService2("7971f918-a847-4430-9279-4a52d1efe18d", 7, "") | Out-Null
    } catch {}
    $Searcher.ServerSelection = 3
    $Searcher.ServiceID = "7971f918-a847-4430-9279-4a52d1efe18d"
    $Result = $Searcher.Search("IsInstalled=0 and Type='Driver'")
    $updates = @()
    foreach ($U in $Result.Updates) {
        $updates += [PSCustomObject]@{
            Title = $U.Title; DriverModel = $U.DriverModel; HardwareID = $U.DriverHardwareID
            DriverClass = $U.DriverClass; DriverProvider = $U.DriverProvider
            UpdateID = $U.Identity.UpdateID; Size = $U.MaxDownloadSize
        }
    }
    if ($updates.Count -eq 0) { Write-Output "[]" }
    else { $updates | ConvertTo-Json -Depth 2 -Compress }
} catch { Write-Error $_.Exception.Message }
"""
            res = self._run(["powershell", "-NoProfile", "-Command", ps_cmd], timeout=300, encoding='utf-8')
            out = res.stdout.strip()
            if not out and res.stderr:
                logging.warning(f"[WU_API] Stderr: {res.stderr[:200]}")
                return None
            if out:
                data = json.loads(out)
                if isinstance(data, dict):
                    data = [data]
                logging.info(f"[WU_API] Talált frissítések: {len(data) if isinstance(data, list) else 0}")
                return data if isinstance(data, list) else None
        except subprocess.TimeoutExpired:
            logging.error("[WU_API] WU API timeout (300s), megpróbáljuk újraindítani a szolgáltatást...")
            self.emit('task_progress', {'task': 'autofix', 'log': '⚠️ Windows Update API időtúllépés! Szolgáltatások újraindítása és újrapróbálkozás...'})
            
            # Restart WU services to recover from freeze
            reset_ps = r"""
            Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
            Stop-Service bits -Force -ErrorAction SilentlyContinue
            Stop-Service cryptsvc -Force -ErrorAction SilentlyContinue
            Start-Service cryptsvc -ErrorAction SilentlyContinue
            Start-Service bits -ErrorAction SilentlyContinue
            Start-Service wuauserv -ErrorAction SilentlyContinue
            """
            self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", reset_ps])
            time.sleep(5)
            
            # Retry once
            try:
                res = self._run(["powershell", "-NoProfile", "-Command", ps_cmd], timeout=300, encoding='utf-8')
                out = res.stdout.strip()
                if out:
                    data = json.loads(out)
                    if isinstance(data, dict):
                        data = [data]
                    logging.info(f"[WU_API] Újrapróbálás sikeres, talált frissítések: {len(data) if isinstance(data, list) else 0}")
                    return data if isinstance(data, list) else None
            except Exception as retry_e:
                logging.error(f"[WU_API] Újrapróbálás is elbukott: {retry_e}")
                
        except Exception as e:
            logging.error(f"[WU_API] WU API error: {e}")
        return None

    def _catalog_search(self, devices_to_check):
        logging.info(f"[CATALOG] _catalog_search() - {len(devices_to_check)} eszköz ellenőrzése...")
        import urllib.request, urllib.parse, ssl
        ssl_ctx = ssl.create_default_context()
        lock = threading.Lock()

        def check_one(item):
            try:
                url = 'https://www.catalog.update.microsoft.com/Search.aspx?q=' + urllib.parse.quote(item['id'])
                logging.debug(f"[CATALOG] Keresés: {item['name']} ({item['id']})")
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                html = urllib.request.urlopen(req, context=ssl_ctx, timeout=30).read().decode('utf-8')
                match_ids = re.findall(r"id=['\"]([a-fA-F0-9\-]+)_link['\"]", html)
                if match_ids:
                    best_id = match_ids[0]
                    dl_body = f'updateIDs=[{{"size":0,"languages":"","uidInfo":"{best_id}","updateID":"{best_id}"}}]'
                    dl_req = urllib.request.Request(
                        'https://www.catalog.update.microsoft.com/DownloadDialog.aspx',
                        data=dl_body.encode('utf-8'),
                        headers={'User-Agent': 'Mozilla/5.0', 'Content-Type': 'application/x-www-form-urlencoded'})
                    dl_html = urllib.request.urlopen(dl_req, context=ssl_ctx, timeout=30).read().decode('utf-8')
                    cab_link = re.search(r'downloadInformation\[0\]\.files\[0\]\.url\s*=\s*[\"\']([^\"\']+)[\"\']', dl_html)
                    if cab_link:
                        logging.debug(f"[CATALOG] Találat: {item['name']} - {cab_link.group(1)[:50]}...")
                        with lock:
                            self.hw_updates_pool.append({
                                "name": item['name'], "cat": item['cat'], "hwid": item['id'],
                                "url": cab_link.group(1), "pnp_id": item.get('pnp_id', ''),
                                "wu_title": f"MS Katalógus: {item['name']}"
                            })
            except Exception as e:
                logging.debug(f"[CATALOG] Hiba: {item['name']} - {e}")
                pass

        q = queue.Queue()
        for dev in devices_to_check:
            q.put(dev)

        import concurrent.futures

        def cat_worker():
            while not q.empty():
                try:
                    dev = q.get_nowait()
                except Exception:
                    break
                check_one(dev)
                q.task_done()

        threads = [threading.Thread(target=cat_worker, daemon=True) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        catalog_hwids = {drv['hwid'] for drv in self.hw_updates_pool}
        self._hw_installed_devs = [dev for dev in devices_to_check if dev['id'] not in catalog_hwids]
        logging.info(f"[CATALOG] Kész - {len(self.hw_updates_pool)} találat, {len(self._hw_installed_devs)} nem elérhető")

    # ================================================================
    # WU DRIVER INSTALL
    # ================================================================
    def install_selected_wu(self, selected_indices):
        logging.info(f"[API] install_selected_wu() - {len(selected_indices)} index kiválasztva")
        logging.debug(f"[WU_INSTALL] Indexek: {selected_indices}")
        selected_pool = [self.hw_updates_pool[i] for i in selected_indices if 0 <= i < len(self.hw_updates_pool)]
        if not selected_pool:
            logging.warning("[WU_INSTALL] Nincs érvényes driver kiválasztva!")
            self.emit('toast', {'message': '⚠️ Nincs érvényes driver kiválasztva!', 'type': 'warning'})
            return
        logging.info(f"[WU_INSTALL] {len(selected_pool)} driver telepítése, mód={'WU API' if self.wu_api_mode else 'Katalógus'}")

        if self.wu_api_mode:
            if self.target_os_path:
                # A WU API (Microsoft.Update.Session COM) mindig az élő rendszert célozza meg,
                # offline cél-OS esetén ez csendben a host gépre telepítene drivert a kiválasztott
                # offline image helyett - ezért ilyenkor a dism-alapú katalógus módra váltunk.
                logging.warning("[WU_INSTALL] WU API mód offline cél-OS mellett nem használható, katalógus módra váltás.")
                self.emit('toast', {'message': '⚠️ Offline célrendszer esetén a WU API mód nem elérhető - katalógus (DISM) módban folytatjuk.', 'type': 'warning'})
                self._install_catalog(selected_pool)
            else:
                self._install_wu_api(selected_pool)
        else:
            self._install_catalog(selected_pool)

    def _install_wu_api(self, selected_pool):
        logging.info(f"[WU_API] WU API telepítés indítása: {len(selected_pool)} driver")
        def worker():
            self.emit('task_start', {'task': 'wu_install', 'title': f'Driver Telepítés WU Szerverekről ({len(selected_pool)} db)'})
            self.emit('task_progress', {'task': 'wu_install', 'log': 'Windows Update szervereiről történő telepítés indítása...', 'indeterminate': True})

            # A kiválasztott driverek azonosítói: elsődlegesen a pontos WU UpdateID
            # (a hardver-szkennelés eredményéből), HWID-prefix egyezés csak azokra a
            # bejegyzésekre, amelyeknek nincs UpdateID-ja - a kettő NEM vagylagos egy
            # elemen belül, mert azonos HWID-n több különböző csomag is lóghat.
            pool_uids = []
            pool_hwids = []
            for drv in selected_pool:
                if drv.get('update_id'):
                    pool_uids.append(str(drv['update_id']))
                elif drv.get('hwid'):
                    pool_hwids.append(str(drv['hwid']).upper())

            if not pool_uids and not pool_hwids:
                logging.warning("[WU_INSTALL] A kiválasztott elemekhez nincs UpdateID/HWID - telepítés megszakítva.")
                self.emit('toast', {'message': '⚠️ A kiválasztott driverekhez nincs azonosító, futtass új hardver-szkennelést!', 'type': 'warning'})
                self.emit('task_complete', {'task': 'wu_install', 'success': 0, 'fail': 0,
                                            'status': '⚠️ Hiányzó azonosítók - futtass új szkennelést!'})
                return

            # A telepítő script a KÖZÖS _build_wu_install_ps-ből jön - az AutoFix (GUI és CLI)
            # is ugyanazt használja, itt csak a szűrők (kijelölt UpdateID-k) különböznek.
            ps_script = _build_wu_install_ps(target_uids=pool_uids, target_hwids=pool_hwids)
            process = subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                startupinfo=self._si, creationflags=self._nw)

            success = 0
            fail = 0
            install_total = 0
            had_error = False

            try:
                for line in process.stdout:
                    if self._check_cancel():
                        self._run(['taskkill', '/F', '/T', '/PID', str(process.pid)])
                        process.wait()  # Prevent zombie process
                        self.emit('task_progress', {'task': 'wu_install', 'log': '\n❗ Megszakítva!'})
                        self.emit('task_complete', {'task': 'wu_install', 'status': '❗ Megszakítva!', 'success': success, 'fail': fail})
                        return
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("INIT:") or line.startswith("SEARCH:"):
                        self.emit('task_progress', {'task': 'wu_install', 'status': line.split(":", 1)[1].strip(), 'log': line})
                    elif line.startswith("FOUND:"):
                        self.emit('task_progress', {'task': 'wu_install', 'log': f'  📦 {line[6:].strip()}'})
                    elif line.startswith("SKIP:"):
                        self.emit('task_progress', {'task': 'wu_install', 'log': f'  ⏭ {line[5:].strip()}'})
                    elif line.startswith("TOTAL:"):
                        m = re.search(r'(\d+)', line)
                        if m:
                            install_total = int(m.group(1))
                        self.emit('task_progress', {'task': 'wu_install', 'log': f'Összesen {install_total} driver telepítése...',
                                                    'total': install_total, 'current': 0, 'counter': f'0 / {install_total}'})
                    elif line.startswith("DLONE:"):
                        self.emit('task_progress', {'task': 'wu_install', 'status': f'⬇ Letöltés: {line[6:].strip()}', 'log': f'  ⬇ {line[6:].strip()}'})
                    elif line.startswith("INSTONE:"):
                        self.emit('task_progress', {'task': 'wu_install', 'status': f'⚙ Telepítés: {line[8:].strip()}', 'log': f'  ⚙ {line[8:].strip()}'})
                    elif line.startswith("OK:"):
                        success += 1
                        done = success + fail
                        self.emit('task_progress', {'task': 'wu_install', 'log': f'  ✅ {line[3:].strip()}',
                                                    'current': done, 'total': install_total, 'counter': f'{done}/{install_total} (✅{success} ❌{fail})'})
                    elif line.startswith("FAIL:"):
                        fail += 1
                        done = success + fail
                        self.emit('task_progress', {'task': 'wu_install', 'log': f'  ❌ {line[5:].strip()}',
                                                    'current': done, 'total': install_total, 'counter': f'{done}/{install_total} (✅{success} ❌{fail})'})
                    elif line.startswith("DONE:"):
                        self.emit('task_progress', {'task': 'wu_install', 'log': f'\n--- {line[5:].strip()} ---'})
                    elif line.startswith("EMPTY:"):
                        self.emit('task_progress', {'task': 'wu_install', 'log': line[6:].strip()})
                    elif line.startswith("ERROR:"):
                        had_error = True
                        logging.error(f"[WU_INSTALL] PowerShell hiba: {line[6:].strip()}")
                        self.emit('task_progress', {'task': 'wu_install', 'log': f'❌ HIBA: {line[6:].strip()}'})
                    else:
                        self.emit('task_progress', {'task': 'wu_install', 'log': line})
                process.wait()
            finally:
                pass

            if success > 0:
                self.emit('task_progress', {'task': 'wu_install', 'log': 'Eszközök újraszkennelése...', 'status': 'Aktiválás...'})
                self._run(['pnputil', '/scan-devices'])
                self.emit('task_progress', {'task': 'wu_install', 'log': '✅ Eszközök frissítve!'})

            if had_error and success == 0 and fail == 0:
                msg = '❌ A telepítés hibával leállt! (részletek fent a naplóban)'
            else:
                msg = f'Sikeres: {success}, Sikertelen: {fail}'
            self.emit('task_complete', {'task': 'wu_install', 'success': success, 'fail': fail,
                                        'status': msg, 'counter': msg})

        self._safe_thread('wu_install', worker)

    def _install_catalog(self, selected_pool):
        logging.info(f"[CATALOG_INSTALL] _install_catalog() - {len(selected_pool)} driver")
        def worker():
            logging.info("[CATALOG_INSTALL] Worker indult...")
            import urllib.request, ssl
            ssl_ctx = ssl.create_default_context()
            total = len(selected_pool)
            self.emit('task_start', {'task': 'wu_install', 'title': f'Katalógus Driver Telepítés ({total} db)'})

            temp_dir = os.path.join(os.environ.get('SystemDrive', 'C:') + '\\DV_Temp', 'driverdoktor_wu')
            os.makedirs(temp_dir, exist_ok=True)
            logging.debug(f"[CATALOG_INSTALL] Temp dir: {temp_dir}")
            success = 0
            fail = 0
            skipped = 0

            try:
                for i, drv in enumerate(selected_pool):
                    if self._check_cancel():
                        logging.warning("[CATALOG_INSTALL] Megszakítva!")
                        self.emit('task_progress', {'task': 'wu_install', 'log': '\n❗ Megszakítva!'})
                        self.emit('task_complete', {'task': 'wu_install', 'status': '❗ Megszakítva!', 'success': success, 'fail': fail})
                        return
                    name = drv['name']
                    url = drv.get('url', '')
                    logging.info(f"[CATALOG_INSTALL] [{i+1}/{total}] {name}")
                    if not url:
                        logging.warning(f"[CATALOG_INSTALL] Kihagyás - nincs URL: {name}")
                        self.emit('task_progress', {'task': 'wu_install', 'log': f'  [KIHAGYÁS] {name} - nincs link'})
                        skipped += 1
                        continue

                    cab_path = os.path.join(temp_dir, f"drv_{i}.cab")
                    ext_path = os.path.join(temp_dir, f"drv_ext_{i}")
                    self.emit('task_progress', {'task': 'wu_install', 'current': i, 'total': total,
                                                'status': f'Letöltés: {name}', 'counter': f'{i+1}/{total}',
                                                'log': f'-> {name} letöltése...'})
                    try:
                        logging.debug(f"[CATALOG_INSTALL] Letöltés: {url[:80]}...")
                        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
                        with urllib.request.urlopen(req, context=ssl_ctx) as resp, open(cab_path, 'wb') as f:
                            shutil.copyfileobj(resp, f)
                        logging.debug(f"[CATALOG_INSTALL] Letöltve: {cab_path}")
                    except Exception as e:
                        logging.error(f"[CATALOG_INSTALL] Letöltési hiba: {e}")
                        self.emit('task_progress', {'task': 'wu_install', 'log': f'  [HIBA] Letöltés: {e}'})
                        fail += 1
                        continue

                import concurrent.futures

                def process_catalog_driver(idx, drv):
                    nonlocal success, fail, skipped
                    if self._check_cancel():
                        return
                    name = drv['name']
                    url = drv.get('url', '')
                    if not url:
                        skipped += 1
                        return

                    cab_path = os.path.join(temp_dir, f"drv_{idx}.cab")
                    ext_path = os.path.join(temp_dir, f"drv_ext_{idx}")
                    
                    self.emit('task_progress', {'task': 'wu_install', 'log': f'-> {name} letöltése aszinkron...'})
                    try:
                        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
                        with urllib.request.urlopen(req, context=ssl_ctx, timeout=60) as resp, open(cab_path, 'wb') as f:
                            shutil.copyfileobj(resp, f)
                    except Exception as e:
                        fail += 1
                        return

                    os.makedirs(ext_path, exist_ok=True)
                    self._run(['expand', cab_path, '-F:*', ext_path])
                    for inner_cab in glob.glob(os.path.join(ext_path, '*.cab')):
                        inner_ext = inner_cab + '_ext'
                        os.makedirs(inner_ext, exist_ok=True)
                        self._run(['expand', inner_cab, '-F:*', inner_ext])

                    self.emit('task_progress', {'task': 'wu_install', 'log': f'  Telepítés: {name}...'})
                    is_offline = bool(self.target_os_path)
                    if is_offline:
                        cmd = ['dism', f'/Image:{self.target_os_path}', '/Add-Driver', f'/Driver:{ext_path}', '/Recurse']
                    else:
                        cmd = ['pnputil', '/add-driver', f"{ext_path}\\*.inf", '/subdirs', '/install']
                    res = self._run(cmd)
                    if res.returncode == 0 or any(k in res.stdout for k in ["Added", "sikeres", "successfully"]):
                        success += 1
                        self.emit('task_progress', {'task': 'wu_install', 'log': f'  ✅ {name} telepítve!'})
                    else:
                        fail += 1
                        self.emit('task_progress', {'task': 'wu_install', 'log': f'  ❌ {name} hiba: {res.stdout[:100]}'})

                with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                    futures = [executor.submit(process_catalog_driver, i, drv) for i, drv in enumerate(selected_pool)]
                    concurrent.futures.wait(futures)

                if self._check_cancel():
                    self.emit('task_progress', {'task': 'wu_install', 'log': '\n❗ Megszakítva!'})
                    self.emit('task_complete', {'task': 'wu_install', 'status': '❗ Megszakítva!', 'success': success, 'fail': fail})
                    return

                if success > 0 and not self.target_os_path:
                    self.emit('task_progress', {'task': 'wu_install', 'log': 'Eszközök újraszkennelése és Code 14 újraindítások elvégzése...'})
                    self._run(['pnputil', '/scan-devices'])
                    
                    # Automatikus Eszközkezelő restart Code 14 (Restart Required) esetén
                    code14_ps = r"""
                    $devs = Get-PnpDevice | Where-Object { $_.ConfigManagerErrorCode -eq 14 }
                    foreach ($d in $devs) {
                        Write-Output "Restarting $($d.Name)..."
                        Disable-PnpDevice -InstanceId $d.InstanceId -Confirm:$false -ErrorAction SilentlyContinue
                        Enable-PnpDevice -InstanceId $d.InstanceId -Confirm:$false -ErrorAction SilentlyContinue
                    }
                    """
                    self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", code14_ps])
                    
            finally:
                logging.debug(f"[CATALOG_INSTALL] Temp dir törlése: {temp_dir}")
                for _ in range(3):
                    try:
                        shutil.rmtree(temp_dir, ignore_errors=False)
                        break
                    except Exception:
                        time.sleep(2)
                shutil.rmtree(temp_dir, ignore_errors=True)

            logging.info(f"[CATALOG_INSTALL] Kész - Sikeres: {success}/{total}, Sikertelen: {fail}, Kihagyott: {skipped}")
            self.emit('task_progress', {'task': 'wu_install', 'current': total, 'total': total,
                                        'log': f'\n--- Sikeres: {success}, Sikertelen: {fail}, Kihagyott: {skipped} ---'})
            self.emit('task_complete', {'task': 'wu_install', 'success': success, 'fail': fail,
                                        'status': f'Kész! Sikeres: {success}, Sikertelen: {fail}' + (f', Kihagyott: {skipped}' if skipped else '')})

        self._safe_thread('wu_install', worker)

    # ================================================================
    # WU MANAGEMENT
    # ================================================================
    def check_wu_status(self):
        logging.info("[API] check_wu_status()")
        if self.target_os_path:
            return {'status': 'Offline (Nem olvasható)', 'color': 'unknown'}
        try:
            policy_disabled = False
            search_disabled = False
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate", 0, winreg.KEY_READ) as key:
                    val, _ = winreg.QueryValueEx(key, "ExcludeWUDriversInQualityUpdate")
                    if val == 1: policy_disabled = True
                    logging.debug(f"[WU_STATUS] ExcludeWUDriversInQualityUpdate = {val}")
            except FileNotFoundError:
                logging.debug("[WU_STATUS] ExcludeWUDriversInQualityUpdate kulcs nem létezik")
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching", 0, winreg.KEY_READ) as key:
                    val, _ = winreg.QueryValueEx(key, "SearchOrderConfig")
                    if val == 0: search_disabled = True
                    logging.debug(f"[WU_STATUS] SearchOrderConfig = {val}")
            except FileNotFoundError:
                logging.debug("[WU_STATUS] SearchOrderConfig kulcs nem létezik")

            service_disabled = False
            try:
                res = self._run(['powershell', '-NoProfile', '-Command', '(Get-Service wuauserv).StartType'], encoding='utf-8')
                if res.stdout and 'Disabled' in res.stdout:
                    service_disabled = True
            except Exception:
                pass

            paused_until = None
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\WindowsUpdate\UX\Settings", 0, winreg.KEY_READ) as key:
                    val, _ = winreg.QueryValueEx(key, "PauseUpdatesExpiryTime")
                    if val:
                        dt = datetime.strptime(val, "%Y-%m-%dT%H:%M:%SZ")
                        if dt > datetime.now(timezone.utc).replace(tzinfo=None):
                            paused_until = val
            except Exception:
                pass

            drv_status = "ENGEDÉLYEZVE"
            if policy_disabled and search_disabled:
                drv_status = "Teljesen LETILTVA"
            elif policy_disabled:
                drv_status = "Házirend által LETILTVA"
            elif search_disabled:
                drv_status = "Eszközbeáll. LETILTVA"

            if service_disabled:
                result = {'status': 'Szolgáltatás LETILTVA (services.msc)', 'color': 'disabled'}
            elif paused_until:
                date_only = paused_until.split('T')[0] if 'T' in paused_until else paused_until
                result = {'status': f'SZÜNET idáig: {date_only} | Driverek: {drv_status}', 'color': 'warning'}
            else:
                color = 'disabled' if 'LETILTVA' in drv_status else 'enabled'
                result = {'status': f'Driver frissítés: {drv_status}', 'color': color}
                
            logging.info(f"[WU_STATUS] Eredmény: {result['status']}")
            return result
        except Exception as e:
            logging.error(f"[WU_STATUS] Hiba: {e}")
            return {'status': 'Ismeretlen', 'color': 'unknown'}

    def _create_restore_point_sync(self, task_id='autofix'):
        desc = "DriverVarázsló AutoFix - " + datetime.now().strftime("%Y-%m-%d %H:%M")
        self.emit('task_progress', {'task': task_id, 'log': 'Registry Mentés (Restore Point) készítése folyamatban...', 'indeterminate': True})
        self._run(["powershell", "-NoProfile", "-Command", 'Enable-ComputerRestore -Drive "$($env:SystemDrive)\\" -ErrorAction SilentlyContinue'])
        self._run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore', '/v', 'SystemRestorePointCreationFrequency', '/t', 'REG_DWORD', '/d', '0', '/f'])
        ps_cmd = f'Checkpoint-Computer -Description "{desc}" -RestorePointType "MODIFY_SETTINGS" -ErrorAction SilentlyContinue'
        res1 = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd], encoding='utf-8')
        if res1.returncode == 0:
            self.emit('task_progress', {'task': task_id, 'log': '✅ Registry mentés / Visszaállítási pont elkészült.\n'})
        else:
            self.emit('task_progress', {'task': task_id, 'log': '⚠️ Visszaállítási pont elutasítva a rendszer által. - FOLYTATÁS...\n'})

    def _disable_sleep_sync(self, task_id='autofix'):
        self.emit('task_progress', {'task': task_id, 'log': 'Alvó mód ideiglenes blokkolása a folyamat végéig (Windows API)...'})
        try:
            # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001 | 0x00000002)
            self.emit('task_progress', {'task': task_id, 'log': '✅ Energiagazdálkodás felülbírálva.\n'})
        except Exception as e:
            self.emit('task_progress', {'task': task_id, 'log': f'⚠️ Alvás tiltása sikertelen: {e}\n'})

    def _disable_wu_sync(self, task_id='autofix'):
        self.emit('task_progress', {'task': task_id, 'log': 'Windows automata driver frissítések letiltása a Registryben...', 'indeterminate': True})
        reg_cmd = ['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching', '/v', 'SearchOrderConfig', '/t', 'REG_DWORD', '/d', '0', '/f']
        self._run(reg_cmd)
        
        # Ez a registry kulcs megakadályozza, hogy a Gépház "Frissítések keresése" gomb megnyomásakor a rendszer drivereket is lehúzzon
        self._run(['reg', 'add', r'HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate', '/v', 'ExcludeWUDriversInQualityUpdate', '/t', 'REG_DWORD', '/d', '1', '/f'])
        
        self.emit('task_progress', {'task': task_id, 'log': '✅ Automatikus driver telepítés letiltva.\n'})

    def _delete_ghost_devices_sync(self, task_id='autofix', skip_classes=None):
        self.emit('task_progress', {'task': task_id, 'log': 'Nem csatlakoztatott (fantom) eszközök azonosítása és törlése...', 'indeterminate': True})
        skip_classes = skip_classes or set()
        # A skip_classes mindig a hardcodeolt AUTOFIX_PRINTER_SKIP_CLASSES konstansból jön
        # (nem felhasználói inputból), ezért biztonságos a PowerShell scriptbe fűzni.
        extra_exclusions = ''.join(f" -and $_.PNPClass -ne '{c}'" for c in sorted(skip_classes))
        if skip_classes:
            skip_match = ' -or '.join(f"$_.PNPClass -eq '{c}'" for c in sorted(skip_classes))
            skipped_count_expr = f"@(Get-PnpDevice -PresentOnly:$false | Where-Object {{ $_.Present -eq $false -and $_.InstanceId -ne $null -and ({skip_match}) }}).Count"
        else:
            skipped_count_expr = "0"
        ps_script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$skippedGhosts = """ + skipped_count_expr + r"""
Write-Output "SKIPPED: $skippedGhosts"
$ghosts = Get-PnpDevice -PresentOnly:$false | Where-Object { $_.Present -eq $false -and $_.InstanceId -ne $null -and $_.PNPClass -ne 'SoftwareDevice' -and $_.PNPClass -ne 'Net' -and $_.PNPClass -ne 'System'""" + extra_exclusions + r""" }
$count = 0
$total = @($ghosts).Count
if ($total -eq 0) {
    Write-Output "DONE: Nincs szellemeszköz a rendszerben."
    exit
}
Write-Output "TOTAL: $total"
foreach ($dev in $ghosts) {
    $id = $dev.PNPDeviceID
    $name = $dev.Name
    if (-not $name) { $name = "Ismeretlen eszköz" }
    $res = & pnputil /remove-device "$($id)" 2>&1
    if ($LASTEXITCODE -eq 0 -or $res -match "deleted" -or $res -match "törölve" -or $res -match "successfully") {
        $count++
    }
}
Write-Output "DONE: Törölve: $count / $total"
"""
        logging.debug(f"[CMD] Popen futtatása: {ps_script[:300]}...")
        process = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
            startupinfo=self._si, creationflags=self._nw)
        
        for line in process.stdout:
            if getattr(self, '_cancel_flag', False):
                self._run(['taskkill', '/F', '/T', '/PID', str(process.pid)])
                process.wait()
                raise Exception("Magyar_Megszakit_Flag")
            line = line.strip()
            if not line:
                continue
            if line.startswith("SKIPPED:"):
                m = re.search(r'SKIPPED:\s*(\d+)', line)
                if m and int(m.group(1)) > 0:
                    self.emit('task_progress', {'task': task_id, 'log': f'ℹ️ {m.group(1)} db nyomtató/szkenner szellemeszköz kihagyva.\n'})
            elif line.startswith("TOTAL:"):
                m = re.search(r'TOTAL:\s*(\d+)', line)
                if m:
                    total = int(m.group(1))
                self.emit('task_progress', {'task': task_id, 'log': f'{total} db szellemeszköz azonosítva. Törlés folyamatban...\n'})
            elif line.startswith("DONE:"):
                self.emit('task_progress', {'task': task_id, 'log': f'✅ {line[5:].strip()}\n'})
        
        process.wait()

    def _delete_third_party_sync(self, task_id='autofix', skip_classes=None):
        self.emit('task_progress', {'task': task_id, 'log': 'Third-party driverek összegyűjtése és törlése...', 'indeterminate': True})
        drivers = self._get_third_party_drivers()
        skip_classes = skip_classes or set()
        if skip_classes:
            skipped = [d for d in drivers if d.get('class', '') in skip_classes]
            drivers = [d for d in drivers if d.get('class', '') not in skip_classes]
            if skipped:
                self.emit('task_progress', {'task': task_id, 'log': f'ℹ️ {len(skipped)} db nyomtató/szkenner driver kihagyva (felhasználói beállítás szerint).\n'})
        total = len(drivers)
        if total > 0:
            self.emit('task_progress', {'task': task_id, 'log': f'{total} db third-party driver eltávolítása...\n'})
            for i, drv in enumerate(drivers):
                if self._cancel_flag: raise Exception("Magyar_Megszakit_Flag")
                name = drv.get('published', '')
                if not name: continue
                self.emit('task_progress', {'task': task_id, 'log': f'🗑 Törlés ({i+1}/{total}): {name}', 'current': i+1, 'total': total})
                self._run(['pnputil', '/delete-driver', name, '/uninstall', '/force'])
            self.emit('task_progress', {'task': task_id, 'log': '✅ Driverek eltávolítva.\n'})
        else:
            self.emit('task_progress', {'task': task_id, 'log': '✅ Nincs third-party driver a rendszerben.\n'})

    def _scan_and_install_wu_sync(self, task_id='autofix'):
        max_loops = 4
        total_installed_in_session = 0

        attempted_uids = set()
        
        for loop_idx in range(1, max_loops + 1):
            if getattr(self, '_cancel_flag', False):
                break
            self.emit('task_progress', {'task': task_id, 'log': f'\n--- DRIVER KERESÉS KÖR: {loop_idx} / {max_loops} ---'})
            self.emit('task_progress', {'task': task_id, 'log': 'Új eszközök szkennelése PnP Util-lal...', 'indeterminate': True})
            self._run(['pnputil', '/scan-devices'])
            time.sleep(10)
            self.emit('task_progress', {'task': task_id, 'log': 'Hivatalos driverek keresése és egyeztetése (Windows Update). Ez percekig is eltarthat...'})
            
            # Eszköz-lekérdezés és párosítás a KÖZÖS magból (_filter_wu_scan_devices +
            # _match_wu_updates_to_devices) - pontosan ugyanaz fut, mint a manuális
            # hardver-szkennelésnél, ne ide írj szűrési/párosítási logikát!
            res = self._run(["powershell", "-NoProfile", "-Command", WU_PNP_QUERY_PS], encoding='utf-8')
            pnp_data = []
            if res.stdout:
                try:
                    pnp_data = json.loads(res.stdout)
                except Exception:
                    pass
            devices_to_check = _filter_wu_scan_devices(pnp_data)

            self.emit('task_progress', {'task': task_id, 'log': f'✅ {len(devices_to_check)} hardverelem azonosítva. Egyeztetés...'})
            wu_results = self._search_wu_api() or []

            matches = _match_wu_updates_to_devices(wu_results, devices_to_check, exclude_uids=attempted_uids)
            matched_updates = [m['uid'] for m in matches]
            attempted_uids.update(matched_updates)

            if not matched_updates:
                self.emit('task_progress', {'task': task_id, 'log': '✅ Szerveren nincs újabb valós illesztőprogram.'})
                self.emit('task_progress', {'task': task_id, 'log': 'Minden elérhető driver telepítve! Keresési lánc befejezve.'})
                break
                
            self.emit('task_progress', {'task': task_id, 'log': f'✅ Telepítendő driverek száma: {len(matched_updates)}'})

            # A telepítő script a KÖZÖS _build_wu_install_ps-ből jön - ugyanaz, mint a
            # manuális telepítésnél, csak itt a kör összes párosított UpdateID-jával fut.
            install_ps = _build_wu_install_ps(target_uids=matched_updates)
            logging.debug(f"[CMD] Popen futtatása: {install_ps[:300]}...")
            process = subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", install_ps],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                startupinfo=self._si, creationflags=self._nw)

            for line in process.stdout:
                if getattr(self, '_cancel_flag', False):
                    self._run(['taskkill', '/F', '/T', '/PID', str(process.pid)])
                    process.wait()
                    self.emit('task_progress', {'task': task_id, 'log': '\n❗ Megszakítva!'})
                    raise Exception("Magyar_Megszakit_Flag")
                line = line.strip()
                if not line:
                    continue
                # A közös script kimeneti protokollja (INIT/SEARCH/FOUND/SKIP/TOTAL/DLONE/
                # INSTONE/OK/FAIL/EMPTY/DONE/ERROR) - lásd _build_wu_install_ps docstring.
                if line.startswith("TOTAL:"):
                    self.emit('task_progress', {'task': task_id, 'log': '--- LETÖLTÉS ÉS TELEPÍTÉS ---'})
                elif line.startswith("DLONE:"):
                    self.emit('task_progress', {'task': task_id, 'log': f'[LETOLTES] {line[6:].strip()}'})
                elif line.startswith("INSTONE:"):
                    self.emit('task_progress', {'task': task_id, 'log': f'[TELEPITES] Telepítés alatt: {line[8:].strip()}'})
                elif line.startswith("OK:"):
                    total_installed_in_session += 1
                    self.emit('task_progress', {'task': task_id, 'log': f'[OK] SIKERES: {line[3:].strip()}'})
                elif line.startswith("FAIL:"):
                    self.emit('task_progress', {'task': task_id, 'log': f'[HIBA] SIKERTELEN: {line[5:].strip()}'})
                elif line.startswith("EMPTY:"):
                    self.emit('task_progress', {'task': task_id, 'log': f'[FIGYELMEZTETES] {line[6:].strip()}'})
                elif line.startswith("ERROR:"):
                    logging.error(f"[AUTOFIX-WU] PowerShell hiba: {line[6:].strip()}")
                    self.emit('task_progress', {'task': task_id, 'log': f'[HIBA] {line[6:].strip()}'})
                elif line.startswith("DONE:"):
                    self.emit('task_progress', {'task': task_id, 'log': f'--- {line[5:].strip()} ---'})
                elif line.startswith("INIT:") or line.startswith("SEARCH:") or \
                        line.startswith("FOUND:") or line.startswith("SKIP:"):
                    pass  # protokoll-sorok, a kör elején már kiírtuk az összesítést
                else:
                    self.emit('task_progress', {'task': task_id, 'log': line})
            process.wait()
                        
        return total_installed_in_session

    def run_autofix(self, skip_printer_drivers=True):
        logging.info(f"[API] run_autofix() indítása (skip_printer_drivers={skip_printer_drivers})")
        if self.target_os_path:
            self.emit('toast', {'message': 'Az 1 kattintásos fix csak az Élő (jelenlegi) rendszeren futtatható le biztonságosan!', 'type': 'error'})
            return

        def worker():
            is_resume_step1 = getattr(self, 'resume_step1', False)
            is_resume_mode = getattr(self, 'resume_mode', False)
            # Resume lábakon (új processz, a dialógus meg sem jelenik újra) a JS-paraméter
            # irreleváns - az A láb által a Scheduled Task argumentumába épített flag-et kell
            # sys.argv-ből visszaolvasni (lásd __init__: self.skip_printer_drivers).
            if is_resume_step1 or is_resume_mode:
                skip_printers = getattr(self, 'skip_printer_drivers', True)
            else:
                skip_printers = skip_printer_drivers

            task_title = '1 Katt. Fix (RESTART UTÁNI LÁNC FOLYTATÁSA!)' if (is_resume_mode or is_resume_step1) else '1 Kattintásos Driver Javítás és Frissítés'
            self.emit('task_start', {'task': 'autofix', 'title': task_title})
            try:
                # Internet ellenőrzés autofix elején (ha nem resume mód)
                if not is_resume_mode and not is_resume_step1:
                    self.emit('task_progress', {'task': 'autofix', 'log': '⏳ Internetkapcsolat ellenőrzése...'})
                    if not self._check_internet():
                        self.emit('toast', {'message': '❌ Nincs internetkapcsolat! Kérlek csatlakozz egy hálózathoz az Autofix előtt!', 'type': 'error'})
                        self.emit('task_complete', {'task': 'autofix', 'status': '❌ Nincs Internetkapcsolat!'})
                        return

                # ÚJ LÉPÉS (-1. LÉPÉS)
                if not is_resume_mode and not is_resume_step1:
                    self.emit('task_progress', {'task': 'autofix', 'log': '-1. LÉPÉS: Windows Update szüneteltetése és újraindítás...'})
                    
                    self._disable_sleep_sync()
                    
                    self.emit('task_progress', {'task': 'autofix', 'log': 'WU szüneteltetése 1 hétre...'})
                    ps_pause = r"""
                    $pauseDate = (Get-Date).AddDays(7).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
                    $nowDate = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
                    Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseUpdatesExpiryTime' -Value $pauseDate -Type String -Force | Out-Null
                    Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseFeatureUpdatesEndTime' -Value $pauseDate -Type String -Force | Out-Null
                    Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseQualityUpdatesEndTime' -Value $pauseDate -Type String -Force | Out-Null
                    Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseUpdatesStartTime' -Value $nowDate -Type String -Force | Out-Null
                    Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseFeatureUpdatesStartTime' -Value $nowDate -Type String -Force | Out-Null
                    Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseQualityUpdatesStartTime' -Value $nowDate -Type String -Force | Out-Null
                    """
                    self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_pause])
                    self.emit('task_progress', {'task': 'autofix', 'log': '✅ WU szüneteltetve 1 hétre.\n'})
                    
                    self.emit('task_progress', {'task': 'autofix', 'log': '🔄 A számítógép újraindul, majd a folyamat a rendszer előkészítésével folytatódik!'})
                    
                    exe_path = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
                    temp_env = os.environ.get('TEMP', '!!').lower()
                    if temp_env in exe_path.lower():
                        try:
                            public_dir = os.environ.get('PUBLIC', 'C:\\Users\\Public')
                            safe_exe = os.path.join(public_dir, "DriverVarazslo_Resume.exe" if getattr(sys, 'frozen', False) else "DriverVarazslo_Resume.py")
                            shutil.copy2(exe_path, safe_exe)
                            exe_path = safe_exe
                            self.emit('task_progress', {'task': 'autofix', 'log': 'ℹ️ Temp mappából futás detektálva. Biztonsági másolat készítve a Public mappába.'})
                        except Exception as e:
                            logging.error(f"[AUTOFIX] Biztonsági másolat hiba: {e}")
                    
                    resume_flag = '--resume-step1'
                    if skip_printers:
                        resume_flag += ' --skip-printer-drivers'

                    if getattr(sys, 'frozen', False):
                        exec_path = exe_path
                        args = resume_flag
                    else:
                        exec_path = sys.executable
                        args = f'"{exe_path}" {resume_flag}'

                    task_ps = f'''
                    $action = New-ScheduledTaskAction -Execute '{_ps_quote(exec_path)}' -Argument '{_ps_quote(args)}'
                    $trigger = New-ScheduledTaskTrigger -AtLogOn
                    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
                    Register-ScheduledTask -TaskName "DriverVarazsloResume" -Action $action -Trigger $trigger -Principal $principal -Force
                    '''
                    self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", task_ps])

                    self.emit('task_complete', {'task': 'autofix', 'status': 'Újraindulás felkészítve (-1. lépés)...'})
                    time.sleep(5)
                    self._run(['shutdown', '/r', '/t', '0', '/f'])
                    return

                if not is_resume_mode:
                    if is_resume_step1:
                        self._run(["powershell", "-NoProfile", "-Command", 'Unregister-ScheduledTask -TaskName "DriverVarazsloResume" -Confirm:$false -ErrorAction SilentlyContinue'])
                    self.emit('task_progress', {'task': 'autofix', 'log': '0. LÉPÉS: Rendszer előkészítése és régi driverek törlése...'})
                    
                    self._disable_sleep_sync()
                    
                    self.emit('task_progress', {'task': 'autofix', 'log': 'Windows Gyors Rendszerindítás (Fast Startup) kikapcsolása...'})
                    self._run(["powercfg", "/h", "off"])
                    
                    self._disable_wu_sync()
                    if getattr(self, '_cancel_flag', False): raise Exception("Magyar_Megszakit_Flag")
                    
                    self._create_restore_point_sync()
                    if getattr(self, '_cancel_flag', False): raise Exception("Magyar_Megszakit_Flag")

                    skip_cls = AUTOFIX_PRINTER_SKIP_CLASSES if skip_printers else None

                    self._delete_ghost_devices_sync(skip_classes=skip_cls)
                    if getattr(self, '_cancel_flag', False): raise Exception("Magyar_Megszakit_Flag")

                    self._delete_third_party_sync(skip_classes=skip_cls)
                    if getattr(self, '_cancel_flag', False): raise Exception("Magyar_Megszakit_Flag")
                    
                    self.emit('task_progress', {'task': 'autofix', 'log': 'Szolgáltatások leállítása és újraindítási jelzések (Pending Reboot) törlése...'})
                    self._run(['reg', 'delete', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired', '/f'])

                    self.emit('task_progress', {'task': 'autofix', 'log': 'Beragadt frissítések és WU gyorsítótár (SoftwareDistribution) ürítése...'})
                    clear_cache = r"""
                    Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
                    Stop-Service bits -Force -ErrorAction SilentlyContinue
                    Stop-Service cryptsvc -Force -ErrorAction SilentlyContinue
                    Stop-Service UsoSvc -Force -ErrorAction SilentlyContinue
                    """
                    self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", clear_cache])
                    
                    sysroot = os.environ.get('SYSTEMROOT', r'C:\Windows')
                    sw_dist = os.path.join(sysroot, 'SoftwareDistribution')
                    deleted_cache = False
                    for _ in range(4):
                        try:
                            if os.path.exists(sw_dist):
                                shutil.rmtree(sw_dist, ignore_errors=False)
                                deleted_cache = True
                                break
                            else:
                                deleted_cache = True
                                break
                        except Exception as e:
                            logging.warning(f"[AUTOFIX] Cache törlés újrapróbálás: {e}")
                            time.sleep(3)
                            
                    if not deleted_cache:
                        self._run(["powershell", "-NoProfile", "-Command", f'Remove-Item -Path "{sw_dist}" -Recurse -Force -ErrorAction SilentlyContinue'])
                    
                    self.emit('task_progress', {'task': 'autofix', 'log': 'WU szüneteltetése 1 hétre...'})
                    ps_pause = r"""
                    $pauseDate = (Get-Date).AddDays(7).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
                    $nowDate = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
                    Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseUpdatesExpiryTime' -Value $pauseDate -Type String -Force | Out-Null
                    Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseFeatureUpdatesEndTime' -Value $pauseDate -Type String -Force | Out-Null
                    Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseQualityUpdatesEndTime' -Value $pauseDate -Type String -Force | Out-Null
                    Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseUpdatesStartTime' -Value $nowDate -Type String -Force | Out-Null
                    Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseFeatureUpdatesStartTime' -Value $nowDate -Type String -Force | Out-Null
                    Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseQualityUpdatesStartTime' -Value $nowDate -Type String -Force | Out-Null
                    """
                    self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_pause])
                    self.emit('task_progress', {'task': 'autofix', 'log': '✅ WU gyorsítótár ürítve és szüneteltetve 1 hétre.\n'})
                    
                    self.emit('task_progress', {'task': 'autofix', 'log': '🔄 A számítógép újraindul, majd a folyamat automatikusan a TELEPÍTÉSSEL folytatódik!'})
                    
                    exe_path = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
                    temp_env = os.environ.get('TEMP', '!!').lower()
                    # Biztonsági másolat, ha temp könyvtárból fut a program
                    if temp_env in exe_path.lower():
                        try:
                            public_dir = os.environ.get('PUBLIC', 'C:\\Users\\Public')
                            safe_exe = os.path.join(public_dir, "DriverVarazslo_Resume.exe" if getattr(sys, 'frozen', False) else "DriverVarazslo_Resume.py")
                            shutil.copy2(exe_path, safe_exe)
                            exe_path = safe_exe
                            self.emit('task_progress', {'task': 'autofix', 'log': 'ℹ️ Temp mappából futás detektálva. Biztonsági másolat készítve a Public mappába.'})
                        except Exception as e:
                            logging.error(f"[AUTOFIX] Biztonsági másolat hiba: {e}")
                    
                    if getattr(sys, 'frozen', False):
                        exec_path = exe_path
                        args = '--resume-autofix'
                    else:
                        exec_path = sys.executable
                        args = f'"{exe_path}" --resume-autofix'
                    
                    task_ps = f'''
                    $action = New-ScheduledTaskAction -Execute '{_ps_quote(exec_path)}' -Argument '{_ps_quote(args)}'
                    $trigger = New-ScheduledTaskTrigger -AtLogOn
                    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
                    Register-ScheduledTask -TaskName "DriverVarazsloResume" -Action $action -Trigger $trigger -Principal $principal -Force
                    '''
                    self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", task_ps])
                    
                    self.emit('task_complete', {'task': 'autofix', 'status': 'Újraindulás felkészítve...'})
                    time.sleep(5)
                    self._run(['shutdown', '/r', '/t', '0', '/f'])
                    return
                else:
                    self._run(["powershell", "-NoProfile", "-Command", 'Unregister-ScheduledTask -TaskName "DriverVarazsloResume" -Confirm:$false -ErrorAction SilentlyContinue'])
                    self.emit('task_progress', {'task': 'autofix', 'log': 'Láncolt folytatás gépújraindítás után. Régi driverek törlése kihagyva, hogy ne töröljünk friss drivereket.\n'})
                    self._disable_sleep_sync()

                # 4. Átmenetileg engedélyezzük a WU-t és unpause a driverkereséshez
                self.emit('task_progress', {'task': 'autofix', 'log': 'Windows Update ideiglenes felébresztése a szükséges driverek lekéréséhez...', 'indeterminate': True})
                # BIZTOSÍTÉK: Teljesen letiltjuk a háttérben futó Automatikus Frissítéseket (Group Policy)
                self._run(['reg', 'add', r'HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU', '/v', 'NoAutoUpdate', '/t', 'REG_DWORD', '/d', '1', '/f'])
                self._set_wu_pause(pause=False)

                # 4. Keresés és visszaépítés
                # A finally garantálja, hogy az 5. lépés (WU letiltás/szüneteltetés visszaállítása)
                # akkor is lefusson, ha a scan/install kivétellel elszáll - különben a WU
                # véglegesen (NoAutoUpdate=1) letiltva maradna a gépen, ütemezett feladat nélkül,
                # ami ezt valaha visszaállítaná.
                try:
                    installed_count = self._scan_and_install_wu_sync()
                finally:
                    # 5. Végső WU letiltás és szüneteltetés visszaállítása
                    self._run(['reg', 'delete', r'HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU', '/v', 'NoAutoUpdate', '/f'])
                    self._disable_wu_sync()
                    self._set_wu_pause(pause=True)

                self.emit('task_progress', {'task': 'autofix', 'log': '\n🎉 MINDEN LÉPÉS KÉSZ!'})
                
                if installed_count > 0:
                    self.emit('task_progress', {'task': 'autofix', 'log': f'\n🔄 EBBEN A KÖRBEN {installed_count} DRIVER TELEPÜLT!\nTovább láncolt hardverek aktiválásához újabb automatikus újraindítás szükséges!\nA rendszer az újraindulás után folytatja a szkennelést!'})
                    # Set RunOnce
                    exe_path = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
                    temp_env = os.environ.get('TEMP', '!!').lower()
                    # Biztonsági másolat, ha temp könyvtárból fut a program
                    if temp_env in exe_path.lower():
                        try:
                            public_dir = os.environ.get('PUBLIC', 'C:\\Users\\Public')
                            safe_exe = os.path.join(public_dir, "DriverVarazslo_Resume.exe" if getattr(sys, 'frozen', False) else "DriverVarazslo_Resume.py")
                            shutil.copy2(exe_path, safe_exe)
                            exe_path = safe_exe
                            self.emit('task_progress', {'task': 'autofix', 'log': 'ℹ️ Temp mappából futás detektálva. Biztonsági másolat készítve a Public mappába.'})
                        except Exception as e:
                            logging.error(f"[AUTOFIX] Biztonsági másolat hiba: {e}")
                    
                    if getattr(sys, 'frozen', False):
                        exec_path = exe_path
                        args = '--resume-autofix'
                    else:
                        exec_path = sys.executable
                        args = f'"{exe_path}" --resume-autofix'
                    
                    task_ps = f'''
                    $action = New-ScheduledTaskAction -Execute '{_ps_quote(exec_path)}' -Argument '{_ps_quote(args)}'
                    $trigger = New-ScheduledTaskTrigger -AtLogOn
                    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
                    Register-ScheduledTask -TaskName "DriverVarazsloResume" -Action $action -Trigger $trigger -Principal $principal -Force
                    '''
                    self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", task_ps])
                    
                    self.emit('task_complete', {'task': 'autofix', 'status': 'Újraindulás felkészítve...'})
                    time.sleep(5)
                    self._run(['shutdown', '/r', '/t', '0', '/f'])
                    return
                else:
                    self.emit('task_progress', {'task': 'autofix', 'log': '\n🎉 KÉSZ! Nulla újonnan fellelt driver, a konfiguráció végigért.'})
                    self._run(["powershell", "-NoProfile", "-Command", 'Unregister-ScheduledTask -TaskName "DriverVarazsloResume" -Confirm:$false -ErrorAction SilentlyContinue'])

                    self.emit('task_progress', {'task': 'autofix', 'log': 'DCH alkalmazások (Microsoft Store) frissítésének kényszerítése...'})
                    try:
                        # Ez aszinkron elindítja a Store App-ok (pl. Realtek Audio Console) szinkronizálását a háttérben
                        ws_script = r"Get-CimInstance -Namespace 'Root\cimv2\mdm\dmmap' -ClassName 'MDM_EnterpriseModernAppManagement_AppManagement01' | Invoke-CimMethod -MethodName UpdateScanMethod"
                        self._run(["powershell", "-WindowStyle", "Hidden", "-Command", ws_script])
                        self.emit('task_progress', {'task': 'autofix', 'log': '✅ Store App-ok szinkronizálása a háttérben elindítva.'})
                    except Exception as e:
                        logging.debug(f"[AUTOFIX] Store App sync error: {e}")
                    
                    try:
                        self.emit('task_progress', {'task': 'autofix', 'log': '\nA FOLYAMAT SIKERESEN BEFEJEZŐDÖTT!'})
                    except Exception:
                        pass
                    
                    # If we were in resume mode, it means this was an automated post-boot check that found nothing.
                    # We can close the app or leave it open. Let's just finish the task.
                    self.emit('task_complete', {'task': 'autofix', 'status': 'Teljesen befejezve'})
                    if not getattr(self, 'resume_mode', False):
                        time.sleep(1)
                        self.emit('ask_reboot', None)

            except Exception as e:
                if str(e) == "Magyar_Megszakit_Flag":
                    self.emit('task_error', {'task': 'autofix', 'error': 'Felhasználó által megszakítva.'})
                else:
                    logging.error(f"[AUTOFIX] Hiba: {e}")
                    self.emit('task_error', {'task': 'autofix', 'error': str(e)})
                    
        self._safe_thread('autofix', worker)

    def disable_wu(self):
        logging.info("[API] disable_wu()")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: A Windows Update beállítások csak Élő rendszeren módosíthatók!', 'type': 'error'})
            return
        def worker():
            logging.info("[WU] WU driver letiltás indítása...")
            self.emit('task_start', {'task': 'disable_wu', 'title': 'WU Driver Letiltás'})
            self._disable_wu_sync()
            
            self.emit('task_progress', {'task': 'disable_wu', 'log': 'Szolgáltatások leállítása és újraindítási jelzések (Pending Reboot) törlése...'})
            stop_svc = r"""
            Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
            Stop-Service bits -Force -ErrorAction SilentlyContinue
            Stop-Service cryptsvc -Force -ErrorAction SilentlyContinue
            Stop-Service UsoSvc -Force -ErrorAction SilentlyContinue
            """
            self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", stop_svc])
            time.sleep(2)
            self._run(['reg', 'delete', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired', '/f'])
            
            self.emit('task_progress', {'task': 'disable_wu', 'log': 'Beragadt frissítések és WU gyorsítótár (SoftwareDistribution) ürítése...'})
            sysroot = os.environ.get('SYSTEMROOT', r'C:\Windows')
            sw_dist = os.path.join(sysroot, 'SoftwareDistribution')
            deleted_cache = False
            for _ in range(4):
                try:
                    if os.path.exists(sw_dist):
                        shutil.rmtree(sw_dist, ignore_errors=False)
                        deleted_cache = True
                        break
                    else:
                        deleted_cache = True
                        break
                except Exception as e:
                    logging.warning(f"[WU_DISABLE] Cache törlés újrapróbálás: {e}")
                    time.sleep(3)
                    
            if not deleted_cache:
                self._run(["powershell", "-NoProfile", "-Command", f'Remove-Item -Path "{sw_dist}" -Recurse -Force -ErrorAction SilentlyContinue'])
            
            self.emit('task_progress', {'task': 'disable_wu', 'log': '✅ Gyorsítótár törölve.'})
            self._run('net start wuauserv', shell=True)
            self.emit('task_progress', {'task': 'disable_wu', 'log': '✅ WU szolgáltatás újraindítva'})
            self.emit('task_complete', {'task': 'disable_wu', 'status': '✅ WU driver letiltás kész (Cache ürítve)!'})
        self._safe_thread('disable_wu', worker)

    def enable_wu(self):
        logging.info("[API] enable_wu()")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: A Windows Update beállítások csak Élő rendszeren módosíthatók!', 'type': 'error'})
            return
        def worker():
            logging.info("[WU_ENABLE] Worker indult - WU engedélyezés és reset...")
            self.emit('task_start', {'task': 'enable_wu', 'title': 'WU Driver Engedélyezés + Reset'})
            self.emit('task_progress', {'task': 'enable_wu', 'log': 'WU driver engedélyezés + teljes reset...', 'indeterminate': True})

            # Szüneteltetés (Pause) feloldása a registry-ből
            self.emit('task_progress', {'task': 'enable_wu', 'log': 'Szüneteltetés (Pause) feloldása...'})
            ps_resume = """
            $regPath = 'HKLM:\\SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings'
            Remove-ItemProperty -Path $regPath -Name 'PauseUpdatesExpiryTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path $regPath -Name 'PauseFeatureUpdatesEndTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path $regPath -Name 'PauseQualityUpdatesEndTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path $regPath -Name 'PauseUpdatesStartTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path $regPath -Name 'PauseFeatureUpdatesStartTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path $regPath -Name 'PauseQualityUpdatesStartTime' -ErrorAction SilentlyContinue
            """
            self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_resume])
            self.emit('task_progress', {'task': 'enable_wu', 'log': '✅ Szüneteltetés törölve'})

            # Delete policy
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate", 0, winreg.KEY_WRITE) as key:
                    winreg.DeleteValue(key, "ExcludeWUDriversInQualityUpdate")
                logging.info("[WU_ENABLE] ExcludeWUDrivers policy törölve")
                self.emit('task_progress', {'task': 'enable_wu', 'log': '✅ ExcludeWUDrivers policy törölve'})
            except FileNotFoundError:
                logging.debug("[WU_ENABLE] Policy nem létezett")
                self.emit('task_progress', {'task': 'enable_wu', 'log': '  Policy nem létezett'})
            except Exception as e:
                logging.warning(f"[WU_ENABLE] Policy törlés hiba: {e}")
                self.emit('task_progress', {'task': 'enable_wu', 'log': f'⚠ {e}'})

            # SearchOrderConfig = 1
            try:
                with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching", 0, winreg.KEY_WRITE) as key:
                    winreg.SetValueEx(key, "SearchOrderConfig", 0, winreg.REG_DWORD, 1)
                logging.info("[WU_ENABLE] SearchOrderConfig = 1")
                self.emit('task_progress', {'task': 'enable_wu', 'log': '✅ SearchOrderConfig = 1'})
            except Exception as e:
                logging.warning(f"[WU_ENABLE] SearchOrderConfig hiba: {e}")
                self.emit('task_progress', {'task': 'enable_wu', 'log': f'⚠ {e}'})

            self._run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching',
                       '/v', 'SearchOrderConfig', '/t', 'REG_DWORD', '/d', '1', '/f'])

            # Stop services
            logging.info("[WU_ENABLE] Szolgáltatások leállítása...")
            for svc in ['wuauserv', 'bits', 'cryptsvc']:
                self._run(f'net stop {svc} /y', shell=True)
            time.sleep(2)

            # Delete SoftwareDistribution
            sysroot = os.environ.get('SYSTEMROOT', r'C:\Windows')
            sw_dist = os.path.join(sysroot, 'SoftwareDistribution')
            logging.info(f"[WU_ENABLE] SoftwareDistribution törlése: {sw_dist}")
            self.emit('task_progress', {'task': 'enable_wu', 'log': 'SoftwareDistribution törlése...'})
            for _ in range(3):
                try:
                    if os.path.exists(sw_dist):
                        shutil.rmtree(sw_dist, ignore_errors=False)
                        logging.info("[WU_ENABLE] SoftwareDistribution törölve")
                        self.emit('task_progress', {'task': 'enable_wu', 'log': '  ✅ Törölve'})
                        break
                except Exception as e:
                    logging.warning(f"[WU_ENABLE] SoftwareDistribution törlés újrapróbálás: {e}")
                    self.emit('task_progress', {'task': 'enable_wu', 'log': f'  ⚠ Újrapróbálás: {e}'})
                    time.sleep(3)

            # Rename catroot2
            catroot2 = os.path.join(sysroot, 'System32', 'catroot2')
            bak = catroot2 + '.bak'
            try:
                if os.path.exists(bak):
                    shutil.rmtree(bak, ignore_errors=True)
                if os.path.exists(catroot2):
                    os.rename(catroot2, bak)
                    logging.info("[WU_ENABLE] catroot2 átnevezve")
                    self.emit('task_progress', {'task': 'enable_wu', 'log': '✅ catroot2 átnevezve'})
            except Exception as e:
                logging.warning(f"[WU_ENABLE] catroot2 hiba: {e}")
                self.emit('task_progress', {'task': 'enable_wu', 'log': f'⚠ catroot2: {e}'})

            # Re-register DLLs
            logging.info("[WU_ENABLE] WU DLL-ek újraregisztrálása...")
            sys32 = os.path.join(sysroot, 'System32')
            for dll in ['wuaueng.dll', 'wuapi.dll', 'wups.dll', 'wups2.dll', 'wuwebv.dll', 'wucltux.dll']:
                fp = os.path.join(sys32, dll)
                if os.path.exists(fp):
                    self._run(f'regsvr32.exe /s "{fp}"', shell=True)
            self.emit('task_progress', {'task': 'enable_wu', 'log': '✅ WU DLL-ek újraregisztrálva'})

            # Winsock reset
            logging.info("[WU_ENABLE] Winsock reset...")
            self._run('netsh winsock reset', shell=True)

            # Start services
            logging.info("[WU_ENABLE] Szolgáltatások indítása...")
            for svc in ['cryptsvc', 'bits', 'wuauserv']:
                for _ in range(3):
                    res = self._run(f'net start {svc}', shell=True)
                    if res.returncode == 0 or 'already' in (res.stdout + res.stderr).lower():
                        break
                    time.sleep(3)

            self._run('wuauclt.exe /resetauthorization /detectnow', shell=True)
            self._run('UsoClient.exe StartScan', shell=True)
            logging.info("[WU_ENABLE] Kész!")
            self.emit('task_progress', {'task': 'enable_wu', 'log': '✅ Frissítés-keresés elindítva'})
            self.emit('task_complete', {'task': 'enable_wu', 'status': '✅ WU engedélyezés + reset kész!'})

        self._safe_thread('enable_wu', worker)

    def restart_wu(self):
        logging.info("[API] restart_wu()")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: A Windows Update szolgáltatások csak Élő rendszeren indíthatók újra!', 'type': 'error'})
            return
        def worker():
            logging.info("[WU_RESTART] Worker indult - szolgáltatások újraindítása...")
            self.emit('task_start', {'task': 'restart_wu', 'title': 'WU Szolgáltatások Újraindítása'})
            self.emit('task_progress', {'task': 'restart_wu', 'log': 'WU szolgáltatások újraindítása...', 'indeterminate': True})

            logging.info("[WU_RESTART] Szolgáltatások leállítása...")
            for svc in ['wuauserv', 'bits', 'cryptsvc', 'msiserver']:
                self._run(f'net stop {svc} /y', shell=True)
                self.emit('task_progress', {'task': 'restart_wu', 'log': f'  stop {svc}'})
            time.sleep(2)
            logging.info("[WU_RESTART] Szolgáltatások indítása...")
            for svc in ['rpcss', 'cryptsvc', 'bits', 'msiserver', 'wuauserv']:
                for _ in range(3):
                    res = self._run(f'net start {svc}', shell=True)
                    if res.returncode == 0 or 'already' in (res.stdout + res.stderr).lower():
                        break
                    time.sleep(3)
                self.emit('task_progress', {'task': 'restart_wu', 'log': f'  start {svc}'})
            self._run('wuauclt.exe /resetauthorization /detectnow', shell=True)
            self._run('UsoClient.exe StartScan', shell=True)
            logging.info("[WU_RESTART] Kész!")
            self.emit('task_progress', {'task': 'restart_wu', 'log': '✅ Frissítés-keresés elindítva'})
            self.emit('task_complete', {'task': 'restart_wu', 'status': '✅ WU szolgáltatások újraindítva!'})

        self._safe_thread('restart_wu', worker)


    def pause_wu(self, days):
        logging.info(f"[API] pause_wu(days={days})")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: Offline módban nem elérhető!', 'type': 'error'})
            return
            
        def worker():
            self.emit('task_start', {'task': 'pause_wu', 'title': f'WU Szüneteltetés ({days} nap)'})
            self.emit('task_progress', {'task': 'pause_wu', 'log': f'{days} nap hozzáadása a Windows Update szüneteltetéséhez...', 'indeterminate': True})
            
            ps = """
            $regPath = 'HKLM:\\SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings'
            if (!(Test-Path $regPath)) { New-Item -Path $regPath -Force | Out-Null }
            
            $daysToAdd = """ + str(days) + """
            $now = (Get-Date).ToUniversalTime()
            
            $currentPauseStr = (Get-ItemProperty -Path $regPath -Name 'PauseUpdatesExpiryTime' -ErrorAction SilentlyContinue).PauseUpdatesExpiryTime
            
            if ($currentPauseStr -and $daysToAdd -eq 7) {
                try {
                    $currentPause = [datetime]$currentPauseStr
                    if ($currentPause -lt $now) { $currentPause = $now }
                } catch {
                    $currentPause = $now
                }
                $newDate = $currentPause.AddDays($daysToAdd)
            } else {
                $newDate = $now.AddDays($daysToAdd)
            }
            
            $dateStr = $newDate.ToString("yyyy-MM-ddTHH:mm:ssZ")
            $startStr = $now.ToString("yyyy-MM-ddTHH:mm:ssZ")
            
            Set-ItemProperty -Path $regPath -Name 'PauseUpdatesExpiryTime' -Value $dateStr -Type String -Force | Out-Null
            Set-ItemProperty -Path $regPath -Name 'PauseFeatureUpdatesEndTime' -Value $dateStr -Type String -Force | Out-Null
            Set-ItemProperty -Path $regPath -Name 'PauseQualityUpdatesEndTime' -Value $dateStr -Type String -Force | Out-Null
            Set-ItemProperty -Path $regPath -Name 'PauseUpdatesStartTime' -Value $startStr -Type String -Force | Out-Null
            Set-ItemProperty -Path $regPath -Name 'PauseFeatureUpdatesStartTime' -Value $startStr -Type String -Force | Out-Null
            Set-ItemProperty -Path $regPath -Name 'PauseQualityUpdatesStartTime' -Value $startStr -Type String -Force | Out-Null
            
            Write-Output $dateStr
            """
            res = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], encoding='utf-8')
            new_date = res.stdout.strip()
            
            self.emit('task_progress', {'task': 'pause_wu', 'log': 'Szolgáltatások leállítása és újraindítási jelzések törlése...'})
            stop_svc = r"""
            Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
            Stop-Service bits -Force -ErrorAction SilentlyContinue
            Stop-Service cryptsvc -Force -ErrorAction SilentlyContinue
            Stop-Service UsoSvc -Force -ErrorAction SilentlyContinue
            """
            self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", stop_svc])
            time.sleep(2)
            self._run(['reg', 'delete', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired', '/f'])
            
            self.emit('task_progress', {'task': 'pause_wu', 'log': 'Beragadt frissítések és WU gyorsítótár ürítése...'})
            sysroot = os.environ.get('SYSTEMROOT', r'C:\Windows')
            sw_dist = os.path.join(sysroot, 'SoftwareDistribution')
            for _ in range(4):
                try:
                    if os.path.exists(sw_dist):
                        shutil.rmtree(sw_dist, ignore_errors=False)
                        break
                    else:
                        break
                except Exception:
                    time.sleep(3)
            self._run(["powershell", "-NoProfile", "-Command", f'Remove-Item -Path "{sw_dist}" -Recurse -Force -ErrorAction SilentlyContinue'])
            self._run('net start wuauserv', shell=True)
            
            self.emit('task_progress', {'task': 'pause_wu', 'log': f'✅ Frissítések sikeresen szüneteltetve idáig: {new_date}'})
            self.emit('task_complete', {'task': 'pause_wu', 'status': f'✅ Szüneteltetve idáig: {new_date}'})
            
        self._safe_thread('pause_wu', worker)

    def resume_wu(self):
        logging.info("[API] resume_wu()")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: Offline módban nem elérhető!', 'type': 'error'})
            return
            
        def worker():
            self.emit('task_start', {'task': 'resume_wu', 'title': 'WU Szüneteltetés Feloldása'})
            self.emit('task_progress', {'task': 'resume_wu', 'log': 'Windows Update szüneteltetés törlése...', 'indeterminate': True})
            
            ps = """
            $regPath = 'HKLM:\\SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings'
            Remove-ItemProperty -Path $regPath -Name 'PauseUpdatesExpiryTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path $regPath -Name 'PauseFeatureUpdatesEndTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path $regPath -Name 'PauseQualityUpdatesEndTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path $regPath -Name 'PauseUpdatesStartTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path $regPath -Name 'PauseFeatureUpdatesStartTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path $regPath -Name 'PauseQualityUpdatesStartTime' -ErrorAction SilentlyContinue
            
            Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
            Start-Service wuauserv -ErrorAction SilentlyContinue
            
            try { (New-Object -ComObject Microsoft.Update.AutoUpdate).Resume() | Out-Null } catch {}
            """
            self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps])
            
            self.emit('task_progress', {'task': 'resume_wu', 'log': '✅ Szüneteltetés feloldva!'})
            self.emit('task_complete', {'task': 'resume_wu', 'status': '✅ Szüneteltetés feloldva!'})
            
        self._safe_thread('resume_wu', worker)

    # ================================================================
    # BACKUP / RESTORE
    # ================================================================
    def backup_third_party(self):
        logging.info("[API] backup_third_party()")
        dest = self.select_directory('Válassz mappát a driverek kimentéséhez')
        if not dest:
            logging.info("[BACKUP] Mégse - nincs mappa kiválasztva")
            return
        logging.info(f"[BACKUP] Third-party backup indítása -> {dest}")

        def worker():
            folder = os.path.join(dest, f"DriverVarázsló_Export_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            logging.info(f"[BACKUP] Célmappa létrehozása: {folder}")
            os.makedirs(folder, exist_ok=True)
            self.emit('task_start', {'task': 'backup', 'title': 'Driver Exportálás'})
            self.emit('task_progress', {'task': 'backup', 'log': f'Célmappa: {folder}\nExportálás indítása...', 'indeterminate': True})

            logging.info("[BACKUP] DISM export-driver futtatása...")
            dism_cmd = ['dism', f'/Image:{self.target_os_path}', '/export-driver', f'/destination:{folder}'] if self.target_os_path else ['dism', '/online', '/export-driver', f'/destination:{folder}']
            logging.debug(f"[CMD] Popen futtatása: {' '.join(dism_cmd)}")
            process = subprocess.Popen(
                dism_cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                startupinfo=self._si, creationflags=self._nw, errors='replace')

            cancelled = False
            for line in process.stdout:
                if self._check_cancel():
                    self._run(['taskkill', '/F', '/T', '/PID', str(process.pid)])
                    process.wait()  # Prevent zombie process
                    cancelled = True
                    break
                line = line.strip()
                if not line:
                    continue
                logging.debug(f"[BACKUP] DISM: {line[:100]}")
                m = re.search(r'(\d+)\s*(?:/|of)\s*(\d+)', line, re.I)
                if m:
                    self.emit('task_progress', {'task': 'backup', 'current': int(m.group(1)), 'total': int(m.group(2)),
                                                'counter': f'{m.group(1)}/{m.group(2)}', 'status': line[:60]})
                self.emit('task_progress', {'task': 'backup', 'log': line})
            process.wait()

            if cancelled:
                self.emit('task_complete', {'task': 'backup', 'status': '❗ Megszakítva!', 'log': '\n--- MEGSZAKÍTVA! ---'})
                return

            success = process.returncode == 0
            logging.info(f"[BACKUP] DISM befejezve, returncode={process.returncode}")
            self.emit('task_complete', {'task': 'backup',
                                        'status': f'{"✅ Sikeres export!" if success else "❌ Hiba!"} Mappa: {folder}',
                                        'log': f'\n--- {"Sikeres" if success else "Hibás"} export: {folder} ---'})
        self._safe_thread('backup', worker)

    def backup_all(self):
        logging.info("[API] backup_all()")
        dest = self.select_directory('Válassz mappát az ÖSSZES driver kimentéséhez')
        if not dest:
            logging.info("[BACKUP_ALL] Mégse - nincs mappa kiválasztva")
            return
        logging.info(f"[BACKUP_ALL] Összes driver backup indítása -> {dest}")

        def worker():
            folder = os.path.join(dest, f"DriverVarázsló_FullExport_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            os.makedirs(folder, exist_ok=True)
            self.emit('task_start', {'task': 'backup', 'title': 'ÖSSZES Driver Exportálása'})
            self.emit('task_progress', {'task': 'backup', 'log': 'Driver lista lekérdezése...', 'indeterminate': True})

            success = 0
            fail = 0
            cancelled = False

            self.emit('task_progress', {'task': 'backup', 'log': 'DISM driver exportálás indítása... (Ez eltarthat egy ideig)', 'indeterminate': True})
            dism_cmd = ['dism', f'/Image:{self.target_os_path}', '/export-driver', f'/destination:{folder}'] if self.target_os_path else ['dism', '/online', '/export-driver', f'/destination:{folder}']
            res = self._run(dism_cmd)
            if res.returncode == 0:
                success += 1
                self.emit('task_progress', {'task': 'backup', 'log': '✅ DISM exportálás sikeres!'})
            else:
                fail += 1
                self.emit('task_progress', {'task': 'backup', 'log': f'❌ Hiba az exportálásnál: {res.stderr[:300]}'})

            if cancelled:
                self.emit('task_complete', {'task': 'backup', 'status': f'❗ Megszakítva! OEM: {success} db exportálva',
                                            'log': f'\n--- MEGSZAKÍTVA! Sikeres: {success}, Sikertelen: {fail} ---'})
                return

            # Copy inbox drivers (FileRepository + INF)
            if self._check_cancel():
                self.emit('task_complete', {'task': 'backup', 'status': '❗ Megszakítva!', 'log': '\n--- MEGSZAKÍTVA! ---'})
                return
            self.emit('task_progress', {'task': 'backup', 'log': 'Windows inbox driverek másolása (FileRepository)...', 'indeterminate': True})
            windows_dir = os.path.join(self.target_os_path, 'Windows') if self.target_os_path else os.environ.get('SYSTEMROOT', r'C:\Windows')
            driverstore = os.path.join(windows_dir, 'System32', 'DriverStore', 'FileRepository')
            inbox_folder = os.path.join(folder, '_Windows_Inbox_Drivers')
            os.makedirs(inbox_folder, exist_ok=True)

            needed_bytes = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(driverstore) for f in fs if os.path.exists(os.path.join(r, f))) if os.path.exists(driverstore) else 0
            free_bytes = shutil.disk_usage(dest).free
            if needed_bytes > free_bytes:
                self.emit('task_progress', {'task': 'backup', 'log': f'❌ Nincs elég szabad hely a célmeghajtón! Szükséges kb. {needed_bytes // (1024*1024)} MB, elérhető: {free_bytes // (1024*1024)} MB.'})
                self.emit('task_complete', {'task': 'backup', 'status': '❌ Nincs elég szabad hely!'})
                return

            self._run(['robocopy', driverstore, inbox_folder, '/E', '/R:0', '/W:0', '/NFL', '/NDL', '/NJH', '/NJS', '/NC', '/NS', '/NP'])

            if self._check_cancel():
                self.emit('task_complete', {'task': 'backup', 'status': '❗ Megszakítva!', 'log': '\n--- MEGSZAKÍTVA! ---'})
                return
            self.emit('task_progress', {'task': 'backup', 'log': 'Windows INF mappa másolása...'})
            inf_src = os.path.join(windows_dir, 'INF')
            inbox_inf_folder = os.path.join(folder, '_Windows_Inbox_INF')
            os.makedirs(inbox_inf_folder, exist_ok=True)
            self._run(['robocopy', inf_src, inbox_inf_folder, '/E', '/R:0', '/W:0', '/NFL', '/NDL', '/NJH', '/NJS', '/NC', '/NS', '/NP'])

            total_size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fns in os.walk(folder) for f in fns
                             if os.path.exists(os.path.join(dp, f)))
            size_mb = total_size / (1024 * 1024)
            self.emit('task_complete', {'task': 'backup',
                                        'status': f'✅ Kész! OEM: {"Sikeres" if success else "Sikertelen"}, Inbox másolva. Méret: {size_mb:.0f} MB',
                                        'log': f'\n--- Export kész: {folder} ({size_mb:.0f} MB) | Sikeres: {success}, Sikertelen: {fail} ---'})
        self._safe_thread('backup', worker)

    def create_restore_point(self):
        logging.info("[API] create_restore_point()")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: Visszaállítási pont csak Élő rendszeren készíthető!', 'type': 'error'})
            return
        def worker():
            logging.info("[RESTORE_POINT] Worker indult - visszaállítási pont létrehozása...")
            desc = f"DriverVarázsló_Backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            logging.info(f"[RESTORE_POINT] Név: {desc}")
            self.emit('task_start', {'task': 'rp', 'title': 'Visszaállítási Pont'})
            self.emit('task_progress', {'task': 'rp', 'log': 'Rendszervédelem engedélyezése...', 'indeterminate': True})

            # 1) Enable System Restore on C: (force enable even if disabled)
            logging.info("[RESTORE_POINT] Rendszervédelem engedélyezése...")
            enable_ps = '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; try { Enable-ComputerRestore -Drive "$($env:SystemDrive)\\" -ErrorAction Stop; Write-Output "OK" } catch { Write-Output "FAIL: $($_.Exception.Message)" }'
            enable_res = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", enable_ps], encoding='utf-8')
            enable_out = (enable_res.stdout or '').strip()
            if 'FAIL' in enable_out:
                logging.warning(f"[RESTORE_POINT] Enable-ComputerRestore hiba: {enable_out}")
                # Try via registry + vssadmin as fallback
                self.emit('task_progress', {'task': 'rp', 'log': f'⚠ Enable-ComputerRestore hiba: {enable_out}\nRegistry + vssadmin fallback...'})
                self._run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore', '/v', 'DisableSR', '/t', 'REG_DWORD', '/d', '0', '/f'])
                self._run(['vssadmin', 'resize', 'shadowstorage', f'/for={os.environ.get("SystemDrive", "C:")}', f'/on={os.environ.get("SystemDrive", "C:")}', '/maxsize=5%'])
                # Retry enable
                enable_res2 = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", enable_ps], encoding='utf-8')
                enable_out2 = (enable_res2.stdout or '').strip()
                if 'FAIL' in enable_out2:
                    logging.error(f"[RESTORE_POINT] Rendszervédelem nem kapcsolható be: {enable_out2}")
                    self.emit('task_complete', {'task': 'rp', 'status': f'❌ Rendszervédelem nem kapcsolható be: {enable_out2}'})
                    return
                logging.info("[RESTORE_POINT] Rendszervédelem bekapcsolva (fallback)")
                self.emit('task_progress', {'task': 'rp', 'log': '✅ Rendszervédelem bekapcsolva (fallback)'})
            else:
                logging.info("[RESTORE_POINT] Rendszervédelem bekapcsolva")
                self.emit('task_progress', {'task': 'rp', 'log': '✅ Rendszervédelem bekapcsolva'})

            # 2) Disable 24-hour frequency limit
            logging.info("[RESTORE_POINT] 24 órás limit feloldása...")
            self._run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore', 
                       '/v', 'SystemRestorePointCreationFrequency', '/t', 'REG_DWORD', '/d', '0', '/f'])

            # 3) Create restore point
            logging.info("[RESTORE_POINT] Checkpoint-Computer futtatása...")
            self.emit('task_progress', {'task': 'rp', 'log': f'Visszaállítási pont: {desc}', 'status': 'Pont létrehozása...'})
            create_ps = f'[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; try {{ Checkpoint-Computer -Description "{desc}" -RestorePointType "MODIFY_SETTINGS" -ErrorAction Stop; Write-Output "OK" }} catch {{ Write-Output "FAIL: $($_.Exception.Message)" }}'
            res = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", create_ps], encoding='utf-8')
            create_out = (res.stdout or '').strip()
            logging.debug(f"[RESTORE_POINT] Checkpoint result: {create_out}")

            # 4) Verify
            logging.info("[RESTORE_POINT] Ellenőrzés...")
            verify_ps = f'[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; (Get-ComputerRestorePoint | Where-Object {{ $_.Description -eq "{desc}" }}).Description'
            verify_res = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", verify_ps], encoding='utf-8')
            verified = desc in (verify_res.stdout or '')
            logging.debug(f"[RESTORE_POINT] Verified: {verified}")

            if 'OK' in create_out and verified:
                logging.info(f"[RESTORE_POINT] Sikeresen létrehozva: {desc}")
                self.emit('task_complete', {'task': 'rp', 'status': f'✅ Visszaállítási pont létrehozva: {desc}'})
            elif 'OK' in create_out:
                logging.warning("[RESTORE_POINT] Lefutott de nem ellenőrizhető (késleltetett létrehozás?)")
                self.emit('task_complete', {'task': 'rp', 'status': '⚠ Visszaállítási pont létrehozás elindítva (ellenőrzés később)'})
            else:
                logging.error(f"[RESTORE_POINT] Hiba: {create_out}")
                self.emit('task_complete', {'task': 'rp', 'status': f'❌ Hiba: {create_out}'})
        self._safe_thread('rp', worker)

    def repair_bcd_standalone(self):
        """Önálló BCD javítás - a felhasználó kiválasztja a meghajtót."""
        logging.info("[API] repair_bcd_standalone()")
        target = self.select_directory('Válaszd ki a HALOTT WINDOWS meghajtóját (ahol a Windows mappa van)')
        if not target:
            logging.info("[BCD] Mégse - nincs cél kiválasztva")
            return
        target = os.path.splitdrive(os.path.abspath(target))[0] + "\\"
        logging.info(f"[BCD] Standalone BCD javítás: {target}")
        
        def worker():
            self.emit('task_start', {'task': 'bcd', 'title': 'BCD Boot Hiba Javítása'})
            self.emit('task_progress', {'task': 'bcd', 'log': f'Kiválasztott meghajtó: {target}\n', 'indeterminate': True})
            
            # Ellenőrzés - van-e Windows mappa
            windows_path = os.path.join(target, 'Windows')
            if not os.path.exists(windows_path):
                self.emit('task_progress', {'task': 'bcd', 'log': f'❌ Hiba: Windows mappa nem található!\n   Elérési út: {windows_path}'})
                self.emit('task_complete', {'task': 'bcd', 'status': '❌ Windows mappa nem található!'})
                return
            
            # BCD javítás (ugyanaz a kód mint a restore után)
            self._repair_bcd_for_task(target, 'bcd')
            
            self.emit('task_progress', {'task': 'bcd', 'log': '\n==== BCD JAVÍTÁS BEFEJEZVE ===='})
            self.emit('task_complete', {'task': 'bcd', 'status': '✅ BCD javítás befejezve!'})
        
        self._safe_thread('bcd', worker)
    
    def _repair_bcd_for_task(self, target_drive, task_name):
        """BCD javítás közös logika - használható restore-ból vagy önállóan is."""
        target_drive = target_drive.rstrip('\\') + '\\'
        
        self.emit('task_progress', {'task': task_name, 'log': '\n--- BOOT LOADER (BCD) JAVÍTÁS ---'})
        self.emit('task_progress', {'task': task_name, 'log': f'Cél Windows meghajtó: {target_drive}'})
        self.emit('task_progress', {'task': task_name, 'log': 'A Windows meghajtó lemezének azonosítása (PowerShell)...'})
        
        ps_script = f"""
$TargetDrive = "{target_drive[0]}"
try {{
    $winVol = Get-Partition | Where-Object {{ $_.DriveLetter -eq $TargetDrive }}
    if (-not $winVol) {{ Write-Output "FAIL: Nem található a Windows partíció ($TargetDrive:)"; exit }}
    
    $diskNum = $winVol.DiskNumber
    Write-Output "INFO: Lemez azonosítva: Disk $diskNum"
    
    $efiPart = Get-Partition -DiskNumber $diskNum | Where-Object {{ $_.Type -eq 'System' -or $_.GptType -eq '{{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}}' }}
    if (-not $efiPart) {{ Write-Output "FAIL: Nem található EFI System partíció ezen a lemezen!"; exit }}
    
    Write-Output "INFO: EFI Partíció azonosítva: Partition $($efiPart.PartitionNumber)"
    
    $used = (Get-Volume).DriveLetter
    $free = (65..90 | ForEach-Object {{ [char]$_ }}) | Where-Object {{ $used -notcontains $_ }} | Select-Object -First 1
    if (-not $free) {{ Write-Output "FAIL: Nincs szabad betűjel!"; exit }}
    
    Set-Partition -DiskNumber $diskNum -PartitionNumber $efiPart.PartitionNumber -NewDriveLetter $free | Out-Null
    Write-Output "EFI:$free"
}} catch {{
    Write-Output "ERROR: $($_.Exception.Message)"
}}
"""
        res = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script], encoding='utf-8')
        
        success = False
        efi_letter = None
        disk_number = None
        efi_partition = None
        
        if res.stdout:
            for line in res.stdout.splitlines():
                line = line.strip()
                if line.startswith("INFO:"):
                    self.emit('task_progress', {'task': task_name, 'log': line[6:]})
                    if "Disk" in line:
                        m = re.search(r'Disk (\d+)', line)
                        if m: disk_number = m.group(1)
                    if "Partition" in line:
                        m = re.search(r'Partition (\d+)', line)
                        if m: efi_partition = m.group(1)
                elif line.startswith("EFI:"):
                    efi_letter = line[4:].strip() + ":"
                    self.emit('task_progress', {'task': task_name, 'log': f'EFI betűjel hozzárendelve: {efi_letter}'})
                elif line.startswith("FAIL:") or line.startswith("ERROR:"):
                    self.emit('task_progress', {'task': task_name, 'log': f'⚠️ {line}'})

        if efi_letter:
            self.emit('task_progress', {'task': task_name, 'log': f'bcdboot {target_drive}Windows /s {efi_letter} /f UEFI'})
            boot_res = self._run(['bcdboot', f'{target_drive}Windows', '/s', efi_letter, '/f', 'UEFI'])
            if boot_res.returncode == 0:
                success = True
                self.emit('task_progress', {'task': task_name, 'log': '✅ BCD sikeresen újraépítve (UEFI)!'})
            else:
                self.emit('task_progress', {'task': task_name, 'log': '⚠️ UEFI bcdboot hiba, fallback...'})
            
            # EFI betűjel eltávolítása PowerShell-el
            if disk_number and efi_partition:
                rm_ps = f"Remove-PartitionAccessPath -DiskNumber {disk_number} -PartitionNumber {efi_partition} -AccessPath '{efi_letter}\\'"
                self._run(["powershell", "-NoProfile", "-Command", f"for ($i=0; $i -lt 3; $i++) {{ try {{ Invoke-Expression \"{rm_ps}\"; break }} catch {{ Start-Sleep -Seconds 2 }} }}"])
                # Fallback diskpart
                dp_cmd = f"select disk {disk_number}\nselect partition {efi_partition}\nremove letter={efi_letter[0]}\n"
                self._run(['diskpart'], input=dp_cmd, timeout=30)
                
        if not success:
            self.emit('task_progress', {'task': task_name, 'log': f'bcdboot {target_drive}Windows /f ALL'})
            res_fb = self._run(['bcdboot', f'{target_drive}Windows', '/f', 'ALL'])
            if res_fb.returncode == 0:
                success = True
                self.emit('task_progress', {'task': task_name, 'log': '✅ BCD sikeresen újraépítve (ALL fallback)!'})
            else:
                err_msg = res_fb.stderr.strip() if res_fb.stderr else res_fb.stdout.strip() if res_fb.stdout else f'Exit code: {res_fb.returncode}'
                self.emit('task_progress', {'task': task_name, 'log': f'⚠️ bcdboot hiba (0x{res_fb.returncode:X}): {err_msg[:300]}'})
        
        if not success:
            self.emit('task_progress', {'task': task_name, 'log': 'bootrec parancsok futtatása...'})
            for cmd in ['/fixmbr', '/fixboot', '/rebuildbcd']:
                br_res = self._run(['bootrec', cmd])
                status = '✅' if br_res.returncode == 0 else '⚠️ (nem elérhető)'
                self.emit('task_progress', {'task': task_name, 'log': f'  bootrec {cmd}: {status}'})
        
        return success

    def restore_online(self):
        logging.info("[API] restore_online()")
        source = self.select_directory('ÉLŐ MÓD: Válassz kimentett driver mappát')
        if not source:
            logging.info("[RESTORE] Mégse - nincs forrás kiválasztva")
            return
        logging.info(f"[RESTORE] Online restore indítása: source={source}")
        self._run_restore(online=True, source=source, target=None)

    def restore_offline(self):
        logging.info("[API] restore_offline()")
        target = self.select_directory('OFFLINE MÓD: 1. Válaszd ki a HALOTT WINDOWS meghajtóját')
        if not target:
            logging.info("[RESTORE] Mégse - nincs cél kiválasztva")
            return
        target = os.path.splitdrive(os.path.abspath(target))[0] + "\\"
        logging.info(f"[RESTORE] Offline target: {target}")
        source = self.select_directory('OFFLINE MÓD: 2. Válassz kimentett driver mappát')
        if not source:
            logging.info("[RESTORE] Mégse - nincs forrás kiválasztva")
            return
        logging.info(f"[RESTORE] Offline restore indítása: source={source}, target={target}")
        self._run_restore(online=False, source=source, target=target)

    def _run_restore(self, online, source, target):
        logging.info(f"[RESTORE] _run_restore: online={online}, source={source}, target={target}")
        def worker():
            mode = 'Élő' if online else 'Offline'
            logging.info(f"[RESTORE] Worker indult - {mode} mód")
            self.emit('task_start', {'task': 'restore', 'title': f'Driver Visszaállítás ({mode})'})
            self.emit('task_progress', {'task': 'restore', 'log': f'=== {mode.upper()} RESTORE ===\nForrás: {source}\nCél: {target or "jelenlegi rendszer"}\n', 'indeterminate': True})

            restore_had_errors = False
            norm_source = os.path.normpath(source)
            norm_target = os.path.normpath(target) if target else None
            logging.debug(f"[RESTORE] norm_source={norm_source}, norm_target={norm_target}")

            # Detect source type
            is_wim_extract = False
            if not online:
                repo_check = os.path.join(norm_source, "FileRepository")
                inf_check = os.path.join(norm_source, "INF")
                if os.path.isdir(repo_check) or os.path.isdir(inf_check):
                    is_wim_extract = True
            inbox_subfolder = os.path.join(norm_source, "_Windows_Inbox_Drivers") if not online else None
            has_inbox_subfolder = inbox_subfolder and os.path.isdir(inbox_subfolder)
            logging.info(f"[RESTORE] Típus detektálás: is_wim_extract={is_wim_extract}, has_inbox_subfolder={has_inbox_subfolder}")

            def force_copy(src, dst):
                """Robocopy-based forced copy with fallback for inbox/system drivers.
                Visszatérési érték: True, ha a másolás közben bármilyen hiba történt
                (a hívónak ezt a végső "sikeres" összegzésbe be KELL számítania,
                különben egy ténylegesen hiányos másolás is sikeresnek tűnik)."""
                logging.debug(f"[RESTORE] force_copy: {src} -> {dst}")
                if not os.path.exists(src):
                    logging.warning(f"[RESTORE] Forrás nem létezik: {src}")
                    self.emit('task_progress', {'task': 'restore', 'log': f'  ❌ Forrás nem létezik: {src}'})
                    return True
                os.makedirs(dst, exist_ok=True)

                free_bytes = shutil.disk_usage(dst).free
                needed_bytes = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(src) for f in fs if os.path.exists(os.path.join(r, f)))
                if needed_bytes > free_bytes:
                    msg = (f'  ❌ Nincs elég szabad hely a célmeghajtón! Szükséges: {needed_bytes // (1024*1024)} MB, '
                           f'elérhető: {free_bytes // (1024*1024)} MB.')
                    self.emit('task_progress', {'task': 'restore', 'log': msg})
                    return True

                self.emit('task_progress', {'task': 'restore', 'log': f'\n  Robocopy indul: {os.path.basename(src)} -> {os.path.basename(dst)}\n  (Backup mód - Windows jogosultságok megkerülése)'})
                cmd = ['robocopy', src, dst, '/E', '/ZB', '/R:1', '/W:1', '/COPY:DAT', '/NC', '/NS', '/NFL', '/NDL', '/NP']
                res = self._run(cmd)

                if res.returncode < 8:
                    logging.info(f"[RESTORE] Robocopy sikeres, returncode={res.returncode}")
                    self.emit('task_progress', {'task': 'restore', 'log': f'  ✅ Sikeres robocopy kényszerítés ({res.returncode})'})
                    return False
                else:
                    self.emit('task_progress', {'task': 'restore', 'log': f'  ⚠️ Robocopy hiba ({res.returncode}), végső tartalék: mappánkénti jogszerzés (lassabb)...'})
                    had_error = False
                    for root, _, files in os.walk(src):
                        if self._cancel_flag: return had_error
                        rel = os.path.relpath(root, src)
                        target_dir = os.path.join(dst, rel) if rel != '.' else dst
                        os.makedirs(target_dir, exist_ok=True)

                        for f in files:
                            if self._cancel_flag: return had_error
                            sfile = os.path.join(root, f)
                            dfile = os.path.join(target_dir, f)
                            if os.path.exists(dfile):
                                self._run(f'takeown /f "{dfile}" /A', shell=True)
                                self._run(f'icacls "{dfile}" /grant *S-1-5-32-544:F', shell=True)
                                self._run(f'attrib -R "{dfile}"', shell=True)
                            try:
                                shutil.copy2(sfile, dfile)
                            except Exception as e:
                                self.emit('task_progress', {'task': 'restore', 'log': f'❌ Hiba ({f}): {e}'})
                                had_error = True
                    if had_error:
                        self.emit('task_progress', {'task': 'restore', 'log': '  ⚠️ Fallback másolás hibákkal fejeződött be.'})
                    else:
                        self.emit('task_progress', {'task': 'restore', 'log': '  ✅ Fallback másolás befejeződött.'})
                    return had_error

            def run_dism_add_driver(driver_path, label=""):
                """Run DISM /Add-Driver on a folder with /Recurse. Returns (returncode, cancelled)."""
                scratch = os.path.join(norm_target, "Scratch")
                os.makedirs(scratch, exist_ok=True)
                cmd = ['dism', f'/Image:{norm_target}', '/Add-Driver', f'/Driver:{driver_path}', '/Recurse', '/ForceUnsigned', f'/ScratchDir:{scratch}']
                self.emit('task_progress', {'task': 'restore', 'log': f'{label}Parancs: {" ".join(cmd)}'})
                process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                           startupinfo=self._si, creationflags=self._nw, errors='replace')
                cancelled = False
                for line in process.stdout:
                    if self._check_cancel():
                        # Nem lőjük ki a processzt erőszakosan, hogy ne korrumpálódjon a Windows
                        cancelled = True
                        self.emit('task_progress', {'task': 'restore', 'log': '⚠️ Megszakítás kérve, várakozás a biztonságos leállásra...'})
                        break
                    stripped = line.strip()
                    if stripped:
                        self.emit('task_progress', {'task': 'restore', 'log': stripped})
                process.wait()
                if not cancelled:
                    self.emit('task_progress', {'task': 'restore', 'log': f'Return code: {process.returncode}'})
                return (process.returncode, cancelled)

            if online:
                cmd = ['pnputil', '/add-driver', f"{norm_source}\\*.inf", '/subdirs', '/install']
                self.emit('task_progress', {'task': 'restore', 'log': f'Parancs: {" ".join(cmd)}'})
                process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                           startupinfo=self._si, creationflags=self._nw, errors='replace')
                cancelled = False
                for line in process.stdout:
                    if self._check_cancel():
                        # Nem lőjük ki a processzt erőszakosan, hogy ne korrumpálódjon a Windows
                        cancelled = True
                        self.emit('task_progress', {'task': 'restore', 'log': '⚠️ Megszakítás kérve, várakozás a biztonságos leállásra...'})
                        break
                    self.emit('task_progress', {'task': 'restore', 'log': line.strip()})
                process.wait()
                if cancelled:
                    self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                    return
                self.emit('task_progress', {'task': 'restore', 'log': f'\nReturn code: {process.returncode}'})
            elif is_wim_extract:
                # WIM-ből kimentett driverek (Windows_Gyari_Alap_Driverek_*)
                # Ezek FileRepository + INF formátumban vannak
                self.emit('task_progress', {'task': 'restore', 'log': 'WIM-ből kimentett gyári driverek visszaállítása...'})
                new_format_repo = os.path.join(norm_source, "FileRepository")
                new_format_inf = os.path.join(norm_source, "INF")
                target_repo = os.path.join(norm_target, "Windows", "System32", "DriverStore", "FileRepository")
                target_inf = os.path.join(norm_target, "Windows", "INF")

                try:
                    if os.path.exists(new_format_repo):
                        self.emit('task_progress', {'task': 'restore', 'log': '1/2 FileRepository és INF fizikai másolása...'})
                        restore_had_errors = force_copy(new_format_repo, target_repo) or restore_had_errors
                        if self._check_cancel():
                            self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                            return
                        if os.path.exists(new_format_inf):
                            restore_had_errors = force_copy(new_format_inf, target_inf) or restore_had_errors
                            if self._check_cancel():
                                self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                                return
                    else:
                        self.emit('task_progress', {'task': 'restore', 'log': '1/2 DriverStore fizikai másolása...'})
                        restore_had_errors = force_copy(norm_source, target_repo) or restore_had_errors
                        if self._check_cancel():
                            self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                            return

                    if restore_had_errors:
                        self.emit('task_progress', {'task': 'restore', 'log': '⚠️ Fizikai másolás hibákkal fejeződött be!'})
                    else:
                        self.emit('task_progress', {'task': 'restore', 'log': '✅ Fizikai másolás kész!'})
                except Exception as e:
                    err_msg = str(e)
                    if len(err_msg) > 300: err_msg = err_msg[:300] + "..."
                    self.emit('task_progress', {'task': 'restore', 'log': f'❌ Másolási hiba: {err_msg}'})
                    restore_had_errors = True

                # DISM regisztrálás a fizikai másolás után
                self.emit('task_progress', {'task': 'restore', 'log': '\n2/2 DISM driver regisztrálás (inbox drivereknél sok hiba normális)...'})
                _, dism_cancelled = run_dism_add_driver(norm_source, "")
                if dism_cancelled:
                    self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                    return
                self.emit('task_progress', {'task': 'restore', 'log': '✅ A fizikai másolás + DISM regisztrálás kész. Az inbox driverek a másolásnak köszönhetően elérhetőek.'})

            elif has_inbox_subfolder:
                # DriverVarázsló_FullExport / ALL_Driver_Backup formátum: _Windows_Inbox_Drivers + oem almappák
                self.emit('task_progress', {'task': 'restore', 'log': 'Teljes export formátum észlelve (DriverVarázsló_FullExport / ALL_Driver_Backup).\n'
                                            'Az inbox drivereket fizikailag másoljuk (DISM nem tudja telepíteni őket),\n'
                                            'az OEM drivereket DISM-mel regisztráljuk.\n'})

                # 1) Inbox driverek fizikai másolása (FileRepository + INF)
                target_repo = os.path.join(norm_target, "Windows", "System32", "DriverStore", "FileRepository")
                target_inf = os.path.join(norm_target, "Windows", "INF")
                inbox_inf_subfolder = os.path.join(norm_source, "_Windows_Inbox_INF")
                self.emit('task_progress', {'task': 'restore', 'log': '--- 1. LÉPÉS: Inbox driverek fizikai másolása a DriverStore-ba ---'})
                try:
                    restore_had_errors = force_copy(inbox_subfolder, target_repo) or restore_had_errors
                    if self._check_cancel():
                        self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                        return
                    if os.path.isdir(inbox_inf_subfolder):
                        self.emit('task_progress', {'task': 'restore', 'log': 'Windows INF mappa visszamásolása (új formátumú backup)...'})
                        restore_had_errors = force_copy(inbox_inf_subfolder, target_inf) or restore_had_errors
                        if self._check_cancel():
                            self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                            return
                    else:
                        # Régi backup: nincs _Windows_Inbox_INF, ezért a FileRepository almappáiból
                        # kiszedjük az .inf fájlokat és bemásoljuk a Windows\INF-be
                        self.emit('task_progress', {'task': 'restore', 'log': 'Régi backup formátum: _Windows_Inbox_INF nem található.\n'
                                                    'INF fájlok kinyerése a FileRepository almappáiból...'})
                        os.makedirs(target_inf, exist_ok=True)
                        inf_count = 0
                        for repo_dir in os.listdir(inbox_subfolder):
                            repo_path = os.path.join(inbox_subfolder, repo_dir)
                            if not os.path.isdir(repo_path):
                                continue
                            for fname in os.listdir(repo_path):
                                if fname.lower().endswith('.inf'):
                                    src_inf = os.path.join(repo_path, fname)
                                    dst_inf = os.path.join(target_inf, fname)
                                    try:
                                        shutil.copy2(src_inf, dst_inf)
                                        inf_count += 1
                                    except Exception as e:
                                        logging.debug(e)
                        self.emit('task_progress', {'task': 'restore', 'log': f'✅ {inf_count} db .inf fájl kinyerve a Windows\\INF mappába (.pnf-eket a Windows legenerálja bootoláskor).'})
                    if restore_had_errors:
                        self.emit('task_progress', {'task': 'restore', 'log': '⚠️ Inbox driverek fizikai másolása hibákkal fejeződött be!'})
                    else:
                        self.emit('task_progress', {'task': 'restore', 'log': '✅ Inbox driverek fizikai másolása kész!'})
                except Exception as e:
                    err_msg = str(e)
                    if len(err_msg) > 300: err_msg = err_msg[:300] + "..."
                    self.emit('task_progress', {'task': 'restore', 'log': f'❌ Inbox másolási hiba: {err_msg}'})
                    restore_had_errors = True

                # 2) OEM driverek DISM-mel (almappák, amik nem _Windows_Inbox_Drivers)
                oem_folders = []
                for item in os.listdir(norm_source):
                    item_path = os.path.join(norm_source, item)
                    if os.path.isdir(item_path) and item not in ("_Windows_Inbox_Drivers", "_Windows_Inbox_INF"):
                        # Check if folder contains any .inf files (directly or in subfolders)
                        has_inf = any(f.lower().endswith('.inf') for _, _, fns in os.walk(item_path) for f in fns)
                        if has_inf:
                            oem_folders.append(item_path)

                if oem_folders:
                    self.emit('task_progress', {'task': 'restore', 'log': f'\n--- 2. LÉPÉS: {len(oem_folders)} db OEM driver mappa DISM regisztrálása ---'})
                    for i, oem_path in enumerate(oem_folders):
                        if self._check_cancel():
                            self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                            return
                        self.emit('task_progress', {'task': 'restore', 'log': f'\n[{i+1}/{len(oem_folders)}] {os.path.basename(oem_path)}:'})
                        _, dism_cancelled = run_dism_add_driver(oem_path, "  ")
                        if dism_cancelled:
                            self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                            return
                    self.emit('task_progress', {'task': 'restore', 'log': '\n✅ OEM driverek DISM regisztrálása kész!'})
                else:
                    self.emit('task_progress', {'task': 'restore', 'log': '\nNincs OEM driver mappa a backup-ban.'})

            else:
                # Egyéb mappa (pl. DriverVarázsló_Export / Driver_Backup third-party export) — tisztán DISM
                _, dism_cancelled = run_dism_add_driver(norm_source, "")
                if dism_cancelled:
                    self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                    return

            # Post-install
            if online:
                is_pe = os.environ.get('SystemDrive', 'C:') == 'X:'
                if not is_pe:
                    self.emit('task_progress', {'task': 'restore', 'log': 'Hardverváltozások keresése...'})
                    time.sleep(1.5)
                    self._run(['pnputil', '/scan-devices'])
                    time.sleep(10)
                    self.emit('task_progress', {'task': 'restore', 'log': '✅ Scan kész!'})
            else:
                # === BCD JAVÍTÁS (boot loader) ===
                self._repair_bcd(norm_target)
                
                # Automata PnP rescan beállítása az asztal betöltésére
                self.emit('task_progress', {'task': 'restore', 'log': '\nElső bejelentkezési rescan script beállítása...'})
                startup_dir = os.path.join(target, "ProgramData", "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
                os.makedirs(startup_dir, exist_ok=True)
                bat_path = os.path.join(startup_dir, "auto_pnputil_scan.bat")
                bat_content = (
                    '@echo off\n'
                    'set LOGFILE="%SystemDrive%\\Users\\Public\\driver_startup_log.txt"\n'
                    'echo [%DATE% %TIME%] Boot rescan indult... >> %LOGFILE%\n'
                    'pnputil /scan-devices >> %LOGFILE% 2>&1\n'
                    'echo [%DATE% %TIME%] Kesz! >> %LOGFILE%\n'
                    'ping 127.0.0.1 -n 3 > nul\n'
                    '(goto) 2>nul & del "%~f0"\n'
                )
                try:
                    with open(bat_path, 'w', encoding='utf-8') as f:
                        f.write(bat_content)
                    self.emit('task_progress', {'task': 'restore', 'log': '✅ Startup script elhelyezve.'})
                except Exception as e:
                    self.emit('task_progress', {'task': 'restore', 'log': f'⚠ Script írási hiba: {e}'})

            self.emit('task_progress', {'task': 'restore', 'log': '\n==== BEFEJEZVE ===='})
            if restore_had_errors:
                self.emit('task_progress', {'task': 'restore', 'log': '⚠️ A visszaállítás hibákkal fejeződött be - egyes driverek fizikai másolása sikertelen volt, a napló tartalmazza a részleteket!'})
                self.emit('task_complete', {'task': 'restore', 'status': '⚠️ Visszaállítás hibákkal fejeződött be!'})
            else:
                self.emit('task_complete', {'task': 'restore', 'status': '✅ Visszaállítás befejezve!'})

        self._safe_thread('restore', worker)

    def get_bitlocker_status(self):
        logging.info("[API] get_bitlocker_status()")
        if self.target_os_path:
            return {'status': 'Offline', 'color': 'unknown'}
        try:
            ps_script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$vol = Get-BitLockerVolume -MountPoint $env:SystemDrive -ErrorAction SilentlyContinue
if (-not $vol) { Write-Output "Ismeretlen"; exit }
$ps = $vol.ProtectionStatus
$vs = $vol.VolumeStatus
$pct = $vol.EncryptionPercentage
if ($ps -eq 'On' -or $vs -eq 'FullyEncrypted') {
    Write-Output "Titkosítva (Aktív)"
} elseif ($vs -eq 'EncryptionInProgress') {
    Write-Output "Titkosítás folyamatban ($pct%)"
} elseif ($vs -eq 'DecryptionInProgress') {
    Write-Output "Dekódolás folyamatban ($pct%)"
} elseif ($ps -eq 'Off' -or $vs -eq 'FullyDecrypted') {
    Write-Output "Nincs titkosítva (Kikapcsolva)"
} else {
    Write-Output "Állapot: $vs ($pct%)"
}
"""
            res = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script], encoding='utf-8')
            status_text = res.stdout.strip()
            
            color = 'unknown'
            if 'Titkosítva' in status_text: color = 'enabled'
            elif 'Dekódolás folyamatban' in status_text: color = 'warning'
            elif 'Nincs titkosítva' in status_text: color = 'disabled'
            
            return {'status': status_text, 'color': color}
        except Exception as e:
            logging.error(f"[BITLOCKER] Status hiba: {e}")
            return {'status': f'Hiba: {e}', 'color': 'unknown'}

    def disable_bitlocker(self):
        logging.info("[API] disable_bitlocker()")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: Offline módban nem elérhető!', 'type': 'error'})
            return
            
        def worker():
            self.emit('task_start', {'task': 'bitlocker', 'title': 'BitLocker Végleges Kikapcsolása'})
            self.emit('task_progress', {'task': 'bitlocker', 'log': 'Dekódolási parancs kiadása a rendszernek (Disable-BitLocker)...', 'indeterminate': True})
            
            ps_cmd = r"Disable-BitLocker -MountPoint $env:SystemDrive"
            res = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd])
            
            if res.returncode == 0:
                self.emit('task_progress', {'task': 'bitlocker', 'log': '✅ Parancs sikeresen kiadva!\n\nA dekódolás megkezdődött a háttérben.\nKérlek, frissítsd az állapotot a gombbal az aktuális százalék lekérdezéséhez.'})
                self.emit('task_complete', {'task': 'bitlocker', 'status': '✅ Dekódolás megkezdve!'})
                # Auto update status after 2 seconds
                time.sleep(2)
                self.emit('bitlocker_status', self.get_bitlocker_status())
            else:
                self.emit('task_progress', {'task': 'bitlocker', 'log': f'❌ Hiba: {res.stderr}'})
                self.emit('task_complete', {'task': 'bitlocker', 'status': '❌ Hiba történt!'})
                
        self._safe_thread('bitlocker', worker)

    def extract_wim(self):
        logging.info("[API] extract_wim()")
        wim_path = self.select_file('Válaszd ki az install.wim fájlt', 'WIM fájlok (*.wim)|*.wim')
        if not wim_path:
            logging.info("[WIM] Mégse - nincs WIM kiválasztva")
            return
        logging.info(f"[WIM] WIM fájl: {wim_path}")
        if wim_path.lower().endswith(".esd"):
            logging.info("[WIM] ESD fájl konvertálása szükséges.")
        dest = self.select_directory('Válassz ideiglenes mappát a kicsomagoláshoz')
        if not dest:
            logging.info("[WIM] Mégse - nincs célmappa kiválasztva")
            return
        logging.info(f"[WIM] Célmappa: {dest}")

        def worker():
            logging.info("[WIM] Worker indult - WIM kinyerés...")
            self.emit('task_start', {'task': 'wim', 'title': 'WIM Driver Kinyerés'})
            wim = os.path.abspath(wim_path).replace("/", "\\")
            # A WIM csatolási mappának a C: meghajtón kell lennie (NTFS), mert a cserélhető meghajtókat (USB) a DISM visszautasítja
            is_pe = os.environ.get('SystemDrive', 'C:') == 'X:'
            if is_pe:
                # WinPE-ben a SystemDrive az X: RAM-disk - sosem szabad oda írni nagy fájlokat,
                # attól függetlenül, hogy van-e kiválasztott offline cél-OS.
                sys_temp = os.path.join(self.target_os_path, 'DV_Temp') if self.target_os_path else r'C:\DV_Temp'
            else:
                sys_temp = os.environ.get('SystemDrive', 'C:') + '\\DV_Temp'
            mount_dir = os.path.join(sys_temp, f"WIM_{int(time.time())}")
            target_folder = os.path.join(dest, f"Windows_Gyari_Alap_Driverek_{datetime.now().strftime('%Y%m%d_%H%M')}")
            logging.info(f"[WIM] Mount dir: {mount_dir}")
            logging.info(f"[WIM] Target folder: {target_folder}")

            if os.path.exists(mount_dir):
                logging.debug("[WIM] Régi mount dir törlése...")
                shutil.rmtree(mount_dir, ignore_errors=True)
            os.makedirs(mount_dir, exist_ok=True)
            os.makedirs(target_folder, exist_ok=True)

            try:
                # Cancel check before mount
                if self._check_cancel():
                    self.emit('task_complete', {'task': 'wim', 'status': '❗ Megszakítva!'})
                    return

                wim_to_mount = wim
                if wim.lower().endswith('.esd'):
                    needed_bytes = os.path.getsize(wim) * 2  # biztonsági ráhagyás a konvertált WIM méretére
                    free_bytes = shutil.disk_usage(sys_temp).free
                    if needed_bytes > free_bytes:
                        raise Exception(f"Nincs elég szabad hely a konvertáláshoz! Szükséges kb. {needed_bytes // (1024*1024)} MB, elérhető: {free_bytes // (1024*1024)} MB ({sys_temp}).")
                    self.emit('task_progress', {'task': 'wim', 'log': 'ESD -> WIM konvertálás (ez 10-15 percet is igénybe vehet!)...', 'indeterminate': True, 'counter': '1/4', 'status': 'Fájl konvertálása...'})
                    temp_wim = os.path.join(sys_temp, f"converted_{int(time.time())}.wim")
                    res_esd = self._run(["dism", "/Export-Image", f"/SourceImageFile:{wim}", "/SourceIndex:1", f"/DestinationImageFile:{temp_wim}", "/Compress:max", "/CheckIntegrity"])
                    if res_esd.returncode != 0:
                        raise Exception(f"ESD Konvertálási hiba: {res_esd.stderr}")
                    wim_to_mount = temp_wim
                    self.emit('task_progress', {'task': 'wim', 'counter': '2/4', 'status': 'Képfájl csatolása...'})
                else:
                    self.emit('task_progress', {'task': 'wim', 'log': 'WIM csatolás (ez 4-5 perc)...', 'indeterminate': True, 'counter': '1/3', 'status': 'Képfájl csatolása...'})

                logging.info("[WIM] DISM Mount-Image futtatása...")
                res = self._run(["dism", "/Mount-Image", f"/ImageFile:{wim_to_mount}", "/Index:1", f"/MountDir:{mount_dir}", "/ReadOnly"])
                if res.returncode != 0:
                    logging.error(f"[WIM] DISM Mount hiba: {res.stdout} {res.stderr}")
                    raise Exception(f"DISM Mount hiba: {res.stdout} {res.stderr}")
                
                # Cancel check after mount (will unmount in except)
                if self._check_cancel():
                    raise Exception("Megszakítva a felhasználó által")
                
                logging.info("[WIM] WIM csatolva, fájlok másolása...")

                self.emit('task_progress', {'task': 'wim', 'log': 'Fájlok másolása...', 'counter': '2/3', 'status': 'Gyári driverek másolása...'})
                
                driverstore = os.path.join(mount_dir, "Windows", "System32", "DriverStore", "FileRepository")
                target_repo = os.path.join(target_folder, "FileRepository")
                if os.path.exists(driverstore):
                    needed_bytes = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(driverstore) for f in fs if os.path.exists(os.path.join(r, f)))
                    free_bytes = shutil.disk_usage(target_folder).free
                    if needed_bytes > free_bytes:
                        raise Exception(f"Nincs elég szabad hely a célmappában! Szükséges kb. {needed_bytes // (1024*1024)} MB, elérhető: {free_bytes // (1024*1024)} MB.")
                    logging.info(f"[WIM] FileRepository másolása: {driverstore} -> {target_repo}")
                    shutil.copytree(driverstore, target_repo, dirs_exist_ok=True)
                else:
                    logging.error("[WIM] FileRepository nem található!")
                    raise Exception("FileRepository nem található a WIM-ben!")

                inf_dir = os.path.join(mount_dir, "Windows", "INF")
                target_inf = os.path.join(target_folder, "INF")
                if os.path.exists(inf_dir):
                    logging.info(f"[WIM] INF mappa másolása: {inf_dir} -> {target_inf}")
                    shutil.copytree(inf_dir, target_inf, dirs_exist_ok=True)

                logging.info("[WIM] WIM leválasztása...")
                self.emit('task_progress', {'task': 'wim', 'log': 'WIM leválasztása...', 'counter': '3/3', 'status': 'Takarítás...'})
                self._run(["dism", "/Unmount-Image", f"/MountDir:{mount_dir}", "/Discard"])
                self._run(["dism", "/Cleanup-Wim"])
                for _ in range(3):
                    try:
                        shutil.rmtree(mount_dir, ignore_errors=False)
                        break
                    except Exception:
                        time.sleep(2)
                shutil.rmtree(mount_dir, ignore_errors=True)
                if wim.lower().endswith('.esd') and 'wim_to_mount' in locals() and os.path.exists(wim_to_mount):
                    try: os.remove(wim_to_mount)
                    except Exception: pass

                logging.info(f"[WIM] Kész! Kimenet: {target_folder}")
                self.emit('task_complete', {'task': 'wim', 'status': f'✅ Gyári driverek kimentve: {target_folder}',
                                            'log': f'\n✅ Kész! Mappa: {target_folder}'})
            except Exception as e:
                logging.error(f"[WIM] Hiba: {e}")
                logging.error(traceback.format_exc())
                self._run(["dism", "/Unmount-Image", f"/MountDir:{mount_dir}", "/Discard"])
                self._run(["dism", "/Cleanup-Wim"])
                shutil.rmtree(mount_dir, ignore_errors=True)
                # Az ESD->WIM konvertált ideiglenes fájlt hiba esetén is töröljük, különben egy
                # több GB-os "converted_*.wim" örökre ott marad a DV_Temp mappában. Fontos: sosem
                # a wim_to_mount == wim (eredeti forrásfájl) esetet töröljük.
                try:
                    if wim.lower().endswith('.esd') and 'wim_to_mount' in locals() and wim_to_mount != wim and os.path.exists(wim_to_mount):
                        os.remove(wim_to_mount)
                except Exception:
                    pass
                self.emit('task_error', {'task': 'wim', 'error': str(e)})
                self.emit('task_complete', {'task': 'wim', 'status': f'❌ Hiba: {e}'})

        self._safe_thread('wim', worker)

    def open_file(self, path):
        logging.info(f"[API] open_file: {path}")
        try:
            os.startfile(path)
            return True
        except Exception as e:
            logging.error(f"Cannot open file: {e}")
            return False

    # ================================================================
    # NET BLOKKOLÓ SCRIPT (block.bat) LETÖLTÉSE
    # ================================================================
    def download_block_script(self):
        """Letölti a block.bat scriptet a C:\\DriverVarazslo mappába (csak letöltés,
        futtatás nélkül). Kicsi fájl, ezért szinkron hívás - a pywebview úgyis saját
        szálon futtatja az API-hívásokat, a UI nem fagy be tőle."""
        logging.info("[API] download_block_script()")
        try:
            path = _download_block_script(self._run)
            return {'success': True, 'path': path}
        except Exception as e:
            logging.error(f"[BLOCK-SCRIPT] Letöltési hiba: {e}")
            return {'success': False, 'error': str(e)}

    def generate_system_report(self, note=None):
        logging.info(f"[API] generate_system_report(note={'igen' if note else 'nem'})")
        try:
            # S.M.A.R.T adatok begyűjtése (smartctl - stress tools zipből)
            stress_dir = self._download_stresstools()
            smartctl_exe = None
            if stress_dir:
                for root, dirs, files in os.walk(stress_dir):
                    for f in files:
                        if f.lower() == "smartctl.exe":
                            smartctl_exe = os.path.join(root, f)
                            break
                    if smartctl_exe: break
            
            smart_data = []
            if smartctl_exe:
                # Friss letöltés/kicsomagolás után a Windows Defender (felhős ellenőrzés) néha
                # pár másodpercig blokkolja vagy üresen futtatja az új exe-t, ezért pár próbálkozást
                # engedünk, mielőtt feladnánk - lassú gépen/neten ez korábban "nem talált semmit" hibát adott.
                devices = []
                max_scan_attempts = 3
                for scan_attempt in range(1, max_scan_attempts + 1):
                    try:
                        logging.info(f"[REPORT] smartctl --scan futtatása... (próba {scan_attempt}/{max_scan_attempts})")
                        scan_res = self._run([smartctl_exe, "--scan", "-j"], encoding='utf-8')
                        scan_data = json.loads(scan_res.stdout) if scan_res.stdout.strip() else {}
                        devices = scan_data.get("devices", [])
                    except Exception as e:
                        logging.error(f"smartctl scan hiba (próba {scan_attempt}/{max_scan_attempts}): {e}")
                        devices = []

                    if devices:
                        break
                    if scan_attempt < max_scan_attempts:
                        logging.warning("[REPORT] smartctl nem talált devices tömböt, újrapróbálás 2s múlva...")
                        time.sleep(2)

                if devices:
                    try:
                        logging.info(f"[REPORT] smartctl talált eszközök száma: {len(devices)}")
                        seen_serials = set()
                        for dev in devices:
                            dev_name = dev.get("name")
                            dev_scan_type = dev.get("type", "")
                            if dev_name:
                                logging.info(f"[REPORT] Adatok lekérése: {dev_name} (type={dev_scan_type or '?'})")
                                info_data = {}
                                for info_attempt in range(1, 3):
                                    info_cmd = [smartctl_exe]
                                    if dev_scan_type:
                                        info_cmd += ["-d", dev_scan_type]
                                    info_cmd += ["-a", dev_name, "-j"]
                                    try:
                                        info_res = self._run(info_cmd, encoding='utf-8')
                                        info_data = json.loads(info_res.stdout) if info_res.stdout.strip() else {}
                                    except Exception as e:
                                        logging.error(f"smartctl -a hiba ({dev_name}, próba {info_attempt}/2): {e}")
                                        info_data = {}
                                    if info_data:
                                        break
                                    if info_attempt < 2:
                                        time.sleep(2)

                                serial = info_data.get("serial_number", "")
                                if serial and serial in seen_serials:
                                    logging.info(f"[REPORT] Duplikált lemez átugrása (serial: {serial})")
                                    continue
                                if serial:
                                    seen_serials.add(serial)
                                    
                                model = info_data.get("model_name", "Ismeretlen Meghajtó")
                                
                                size_gb = "?"
                                user_cap = info_data.get("user_capacity", {}).get("bytes", 0)
                                if user_cap:
                                    size_gb = f"{round(user_cap / (1024**3), 1)} GB"
                                    
                                dev_type = info_data.get("device", {}).get("protocol", "Unspecified")
                                rotation = info_data.get("rotation_rate", 0)
                                
                                if dev_type.lower() == "nvme":
                                    dev_type_str = "NVMe SSD"
                                elif dev_type.lower() == "ata":
                                    if rotation == 0:
                                        dev_type_str = "SATA SSD"
                                    else:
                                        dev_type_str = f"SATA HDD ({rotation} RPM)"
                                else:
                                    dev_type_str = dev_type.upper()
                                
                                hours = "?"
                                power_on = info_data.get("power_on_time", {}).get("hours")
                                if power_on is not None:
                                    try:
                                        p_hours = int(power_on)
                                        d = p_hours // 24
                                        h = p_hours % 24
                                        if d > 0:
                                            if h > 0:
                                                hours = f"{d} nap {h} óra"
                                            else:
                                                hours = f"{d} nap"
                                        else:
                                            hours = f"{h} óra"
                                    except:
                                        hours = f"{power_on} óra"
                                    
                                temp = "?"
                                temperature = info_data.get("temperature", {}).get("current")
                                if temperature is not None:
                                    temp = f"{temperature} °C"
                                    
                                health_txt = "Ismeretlen"
                                health = "-1"
                                perf = "100%"
                                
                                if dev_type.lower() == "nvme":
                                    used = info_data.get("nvme_smart_health_information_log", {}).get("percentage_used")
                                    if used is not None:
                                        health_pct = 100 - used
                                        health = f"{health_pct}%"
                                        health_txt = f"{health_pct}%"
                                else:
                                    status = info_data.get("smart_status", {}).get("passed")
                                    if status is True:
                                        health_txt = "100%"
                                        health = "100%"
                                    elif status is False:
                                        health_txt = "Hibás (SMART Fail)"
                                        health = "0%"
                                        perf = "0%"
                                        
                                    endurance = info_data.get("endurance_used", {}).get("current_percent")
                                    if endurance is not None:
                                        health_pct = 100 - endurance
                                        health = f"{health_pct}%"
                                        health_txt = f"{health_pct}%"
                                    else:
                                        attrs = info_data.get("ata_smart_attributes", {}).get("table", [])
                                        for attr in attrs:
                                            if attr.get("name") == "SSD_Life_Left" or attr.get("id") == 231 or attr.get("name") == "Media_Wearout_Indicator" or attr.get("id") == 233:
                                                val = attr.get("value")
                                                if val is not None:
                                                    health = f"{val}%"
                                                    health_txt = f"{val}%"
                                                    break
                                
                                logging.info(f"[REPORT] Lemez: {model} | Méret: {size_gb} | Típus: {dev_type_str} | Health: {health_txt} | Temp: {temp}")                    
                                smart_data.append({
                                    "Name": model.strip(),
                                    "Size": size_gb,
                                    "Health": health_txt,
                                    "Performance": perf,
                                    "Hours": str(hours),
                                    "Temp": str(temp),
                                    "Type": dev_type_str,
                                    "RawHealth": health.replace('%','').strip() if health != "-1" else "-1"
                                })
                    except Exception as e:
                        logging.error(f"smartctl futtatási hiba: {e}")
                else:
                    logging.warning("[REPORT] smartctl nem talált devices tömböt a kimenetben (több próbálkozás után sem)!")

            # Akkumulátor információk
            batt_script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$tempXML = "$env:TEMP\batt_$(Get-Random).xml"
try {
    powercfg /batteryreport /xml /output $tempXML | Out-Null
    [xml]$batt = Get-Content $tempXML -ErrorAction Stop
    if ($batt.BatteryReport.Batteries.Battery) {
        $b = $batt.BatteryReport.Batteries.Battery
        if ($b -is [array]) { $b = $b[0] }
        $des = $b.DesignCapacity
        $full = $b.FullChargeCapacity
        $name = $b.Id
        @{Name=$name; Design=$des; Full=$full} | ConvertTo-Json -Compress
    }
} catch {}
finally {
    if (Test-Path $tempXML) { Remove-Item $tempXML -Force -ErrorAction SilentlyContinue }
}
"""
            res_batt = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", batt_script], encoding='utf-8')
            batt_data = {}
            if res_batt.stdout.strip():
                try: batt_data = json.loads(res_batt.stdout.strip())
                except: pass

            # Alapvető WMI hardver adatok
            ps_script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$data = @{}
try { $data.CS = Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer, Model | ConvertTo-Json -Compress } catch {}
try { $data.BB = Get-CimInstance Win32_BaseBoard | Select-Object Manufacturer, Product | ConvertTo-Json -Compress } catch {}
try { $data.CPU = Get-CimInstance Win32_Processor | Select-Object Name, NumberOfCores, NumberOfLogicalProcessors | ConvertTo-Json -Compress } catch {}
try { $data.RAM = @(Get-CimInstance Win32_PhysicalMemory | Select-Object Capacity, Speed, Manufacturer, PartNumber) | ConvertTo-Json -Compress } catch {}
try { $data.RAMTotal = Get-CimInstance Win32_ComputerSystem | Select-Object TotalPhysicalMemory | ConvertTo-Json -Compress } catch {}
try {
    # Win32_VideoController.AdapterRAM egy 32 bites (UInt32) WMI mezo, ami max. kb. 4 GB-ot
    # tud kifejezni - 4 GB-nal tobb VRAM eseten (pl. egy 12 GB-os RTX 3060-nal) a legtobb
    # modern driver (kulonosen NVIDIA) emiatt egy 0xFFFFFFFF "tullepes" ertekkel ter vissza,
    # ami byte-ban ertelmezve pont ~4.0 GB-nak nez ki - EZ HAMIS, nem a tenyleges VRAM meret.
    # A valodi (64 bites) ertek a registry-ben van, ugyanott, ahonnan a GPU-Z/HWiNFO is
    # kiolvassa: a video-adapter osztaly (Class GUID) alatti szamozott almappak
    # HardwareInformation.qwMemorySize erteke - a megfelelo almappat a MatchingDeviceId
    # (PNPDeviceID elotag) alapjan azonositjuk.
    $gpus = Get-CimInstance Win32_VideoController | Select-Object Name, AdapterRAM, PNPDeviceID
    $result = @()
    $regBase = "HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    foreach ($gpu in $gpus) {
        $vram = [int64]0
        if ($gpu.AdapterRAM) { $vram = [int64]$gpu.AdapterRAM }
        if (Test-Path $regBase) {
            $matched = $null
            Get-ChildItem $regBase -ErrorAction SilentlyContinue | Where-Object { $_.PSChildName -match '^\d{4}$' } | ForEach-Object {
                if (-not $matched) {
                    $props = Get-ItemProperty -Path $_.PSPath -ErrorAction SilentlyContinue
                    if ($props.MatchingDeviceId -and $gpu.PNPDeviceID -and $gpu.PNPDeviceID.ToLower().StartsWith($props.MatchingDeviceId.ToLower())) {
                        if ($props.'HardwareInformation.qwMemorySize') {
                            $matched = [int64]$props.'HardwareInformation.qwMemorySize'
                        }
                    }
                }
            }
            if ($matched -and $matched -gt 0) { $vram = $matched }
        }
        $result += [PSCustomObject]@{ Name = $gpu.Name; AdapterRAM = $vram }
    }
    $data.GPU = (@($result) | ConvertTo-Json -Compress)
} catch {}
try { $data.OS = Get-CimInstance Win32_OperatingSystem | Select-Object Caption, OSArchitecture | ConvertTo-Json -Compress } catch {}
ConvertTo-Json $data
"""
            res = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script], encoding='utf-8')
            raw_data = json.loads(res.stdout.strip())

            def safe_json(k):
                try:
                    return json.loads(raw_data.get(k, "{}")) if raw_data.get(k) else {}
                except: return {}

            def safe_json_list(k):
                try:
                    parsed = json.loads(raw_data.get(k, "[]")) if raw_data.get(k) else []
                    return parsed if isinstance(parsed, list) else [parsed]
                except: return []

            cs = safe_json("CS")
            bb = safe_json("BB")
            man = (cs.get("Manufacturer") or "").strip()
            mod = (cs.get("Model") or "").strip()
            oem_junk = {"to be filled by o.e.m.", "default string", "system manufacturer", "system product name", "not applicable", ""}
            if man.lower() in oem_junk: man = (bb.get("Manufacturer") or "").strip()
            if mod.lower() in oem_junk: mod = (bb.get("Product") or "").strip()
            if man.lower() in oem_junk: man = "Ismeretlen gyártó"
            if mod.lower() in oem_junk: mod = "Ismeretlen modell"
            pc_model = f"{man} - {mod}"

            cpu = safe_json("CPU")
            if isinstance(cpu, list) and len(cpu) > 0: cpu = cpu[0]
            ram_list = safe_json_list("RAM")
            gpu_list = safe_json_list("GPU")
            os_info = safe_json("OS")

            # HTML Generator (Kompakt A4 méretre optimalizálva, 2 oszlopos grid)
            # g(): WMI/CIM néha a kulcsot megtartja null értékkel ("None" jelenne meg helyette);
            # e(): minden hardver-/firmware-eredetű string HTML-escape-elve kerül a riportba,
            # nehogy egy '<'/'>'/'&'-et tartalmazó modell-/gyártónév eltörje a HTML-t.
            def g(d, key, default='?'):
                v = d.get(key, default)
                return default if v is None else v

            def e(value):
                return html_escape(str(value))

            html = f"""<!DOCTYPE html>
<html lang="hu">
<head>
<meta charset="UTF-8">
<style>
@page {{ size: A4; margin: 15mm; }}
/* Nyomtatáskor a böngészők alapból eldobják a háttérszíneket (tintaspórolás) - enélkül a
sötét "Gyors Összefoglaló" doboz és a színes badge-ek is simán fehérré válnának nyomtatva/
PDF-be mentve, pedig pont a kontraszt a lényegük. */
* {{ -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; color-adjust: exact !important; }}
body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #fff; color: #333; margin: 0 auto; padding: 20px; font-size: 13px; overflow-x: hidden; max-width: 180mm; box-sizing: border-box; }}
h1 {{ color: #46286e; border-bottom: 2px solid #d488ff; padding-bottom: 5px; font-size: 24px; margin-bottom: 5px; }}
.subtitle {{ color: #666; font-size: 13px; margin-top: 0; margin-bottom: 20px; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; align-items: start; }}
.section {{ background: #f9f6ff; padding: 10px 14px; border-radius: 6px; border-left: 4px solid #b855ff; margin-bottom: 10px; page-break-inside: avoid; break-inside: avoid; }}
.item-block {{ page-break-inside: avoid; break-inside: avoid; }}
.section h2 {{ margin-top: 0; color: #46286e; font-size: 16px; margin-bottom: 10px; border-bottom: 1px solid #e0d8f0; padding-bottom: 4px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
th, td {{ padding: 6px 8px; text-align: left; border-bottom: 1px solid #e0d8f0; }}
th {{ background: #eee8f8; color: #46286e; width: 35%; font-weight: 600; }}
.item-title {{ font-weight: bold; color: #46286e; margin-top: 8px; margin-bottom: 4px; font-size: 13px; }}
.badge {{ display: inline-block; padding: 2px 6px; border-radius: 8px; font-size: 11px; font-weight: bold; background: #e0d8f0; color: #46286e; margin-left: 5px; }}
.health-Healthy {{ background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
.health-Warning {{ background: #fff3cd; color: #856404; border: 1px solid #ffeeba; }}
.health-Unhealthy {{ background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
.section.summary-box {{ background: #2b2b30; border: 2px solid #111; border-left: 4px solid #6b6b74; color: #eaeaea; box-shadow: 0 4px 16px rgba(0,0,0,0.3); }}
.section.summary-box h2 {{ color: #fff; border-bottom: 1px solid #55555c; }}
.summary-list {{ list-style: none; margin: 0; padding: 0; }}
.summary-list li {{ padding: 7px 0; border-bottom: 1px solid rgba(255,255,255,0.08); font-size: 12.5px; }}
.summary-list li:last-child {{ border-bottom: none; }}
.summary-list b {{ color: #d488ff; font-weight: 600; }}
.summary-list .si {{ display: inline-block; width: 22px; }}
.note-section {{ margin-top: 4px; padding: 10px 14px; border: 1px dashed #999; border-radius: 6px; background: #fafafa; page-break-inside: avoid; }}
.note-section h2 {{ margin: 0 0 6px 0; color: #46286e; font-size: 14px; }}
.note-content {{ font-family: 'Roboto', 'Segoe UI', -apple-system, sans-serif; font-weight: 500; font-size: 19px; color: #2a2a2a; }}
.note-line {{ min-height: 28px; line-height: 28px; border-bottom: 1px solid #ccc; white-space: pre-wrap; word-break: break-word; }}
</style>
</head>
<body>
<div id="page-ruler" style="position:absolute; visibility:hidden; height:267mm; width:1px; top:0; left:0;"></div>
<div id="report-root">
<h1>DriverVarázsló - Rendszer Adatlap</h1>
<p class="subtitle">Gép típusa: <strong style="color:#000; font-size:14px;">{e(pc_model)}</strong> | Generálva: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>

<div class="grid">
    <div class="col">
        <div class="section">
            <h2>💻 Operációs Rendszer</h2>
            <table>
                <tr><th>Verzió</th><td>{e(g(os_info, 'Caption', 'Ismeretlen'))} ({e(g(os_info, 'OSArchitecture', 'Ismeretlen'))})</td></tr>
            </table>
        </div>

        <div class="section">
            <h2>🧠 Processzor (CPU)</h2>
            <table>
                <tr><th>Modell</th><td>{e(g(cpu, 'Name', 'Ismeretlen'))}</td></tr>
                <tr><th>Magok / Szálak</th><td>{e(g(cpu, 'NumberOfCores'))} Mag / {e(g(cpu, 'NumberOfLogicalProcessors'))} Szál</td></tr>
            </table>
        </div>

        <div class="section">
            <h2>🧩 Memória (RAM)</h2>"""
            
            tot_gb = "Ismeretlen"
            try:
                tot = json.loads(raw_data.get("RAMTotal", "{}")).get('TotalPhysicalMemory')
                if tot: tot_gb = f"{round(int(tot)/(1024**3), 1)} GB"
            except: pass
                
            html += f"<p style='margin: 0 0 8px 0;'><strong>Összes fizikai memória:</strong> {tot_gb} ({len(ram_list)} db modul)</p>"
            if ram_list:
                jedec_map = {
                    "80AD": "SK Hynix", "80CE": "Samsung", "802C": "Micron", 
                    "0198": "Kingston", "029E": "Corsair", "04CB": "A-DATA", 
                    "00CE": "Samsung", "014F": "Transcend", "02FE": "Elpida",
                    "0D0B": "Crucial", "0298": "Kingston"
                }
                html += "<table><tr><th>Gyártó / Cikkszám</th><th>Kapacitás</th><th>Sebesség</th></tr>"
                for r in ram_list:
                    cap = r.get('Capacity')
                    cap_gb = f"{round(int(cap)/(1024**3), 1)} GB" if cap else "?"
                    
                    man = str(g(r, 'Manufacturer')).strip()
                    if len(man) >= 4 and all(c in '0123456789ABCDEFabcdef' for c in man[:4]):
                        hex_pfx = man[:4].upper()
                        if hex_pfx in jedec_map:
                            man = jedec_map[hex_pfx]
                            
                    man_part = f"{man} {g(r, 'PartNumber')}".strip()
                    html += f"<tr><td>{e(man_part)}</td><td>{e(cap_gb)}</td><td>{e(g(r, 'Speed'))} MHz</td></tr>"
                html += "</table>"
            
            html += """</div>
    </div>
    
    <div class="col">"""

            # Akku szekció ha van
            if batt_data.get('Design') and batt_data.get('Full'):
                try:
                    des = int(batt_data['Design'])
                    full = int(batt_data['Full'])
                    health_pct = round((full / des) * 100)
                    h_class = "health-Healthy" if health_pct > 80 else "health-Warning" if health_pct > 50 else "health-Unhealthy"
                    batt_icon = "🔋" if health_pct >= 20 else "🪫"
                    
                    html += f"""
        <div class="section">
            <h2>🔋 Akkumulátor Állapot</h2>
            <table>
                <tr><th>Gyári kapacitás</th><td>{des} mWh</td></tr>
                <tr><th>Jelenlegi max.</th><td>{full} mWh</td></tr>
                <tr><th>Egészség</th><td><span style="font-size:18px; vertical-align:middle;">{batt_icon}</span> <span class="badge {h_class}" style="font-size:13px; padding:4px 8px;">{health_pct}%</span></td></tr>
            </table>
        </div>"""
                except: pass

            html += """
        <div class="section">
            <h2>🎮 Videókártyák (GPU)</h2>"""
            
            gpu_summary_list = []
            if not gpu_list:
                html += "<p>Nem található dedikált/integrált videókártya.</p>"
            else:
                gpu_blocks = []
                for gd in gpu_list:
                    name = g(gd, 'Name', 'Ismeretlen')
                    ram = gd.get('AdapterRAM')
                    if ram:
                        ram_gb = round(int(ram)/(1024**3), 1)
                        ram_gb_str = f"{ram_gb} GB"
                        if ("intel" in name.lower() or "amd radeon" in name.lower() or "vega" in name.lower()) and ram_gb <= 2.0:
                            ram_gb_str += " (Megosztott / Dinamikus VRAM)"
                    else:
                        ram_gb_str = "Ismeretlen"

                    gpu_blocks.append(f"<div class='item-block'><table><tr><th>Modell</th><td>{e(name)}</td></tr><tr><th>VRAM</th><td>{e(ram_gb_str)}</td></tr></table></div>")
                    gpu_summary_list.append(f"{name} ({ram_gb_str})")
                html += "<br>".join(gpu_blocks)

            html += """</div>
        <div class="section">
            <h2>💾 Háttértárak (S.M.A.R.T. Adatok)</h2>"""

            storage_summary_list = []
            if not smart_data:
                html += "<p>Nem található háttértár információ vagy nem olvasható a S.M.A.R.T.</p>"
            else:
                smart_blocks = []
                for s in smart_data:
                    h = g(s, 'Health', 'Ismeretlen')
                    p = g(s, 'Performance', '100%')
                    h_class = ""
                    p_class = ""
                    raw_h = g(s, 'RawHealth', '-1')
                    try:
                        pct = int(raw_h)
                        if pct > 80: h_class = "health-Healthy"
                        elif pct > 40: h_class = "health-Warning"
                        elif pct >= 0: h_class = "health-Unhealthy"

                        if pct >= 0:
                            p_class = "health-Healthy" if p == "100%" else "health-Warning"
                    except: pass

                    smart_blocks.append(f"""<div class="item-block"><div class="item-title">{e(g(s, 'Name', 'Ismeretlen'))} <span class="badge">{e(g(s, 'Size'))}</span> <span class="badge">{e(g(s, 'Type'))}</span></div>
                <table>
                    <tr><th>Kond. / Telj.</th><td><span class="badge {h_class}">❤️ {e(h)}</span> <span class="badge {p_class}">⚡ {e(p)}</span></td></tr>
                    <tr><th>Üzemidő / Hőm.</th><td>{e(g(s, 'Hours'))} / {e(g(s, 'Temp'))}</td></tr>
                </table></div>""")
                    storage_summary_list.append(f"{g(s, 'Name', 'Ismeretlen')} ({g(s, 'Size', '?')}, {g(s, 'Type', '?')})")
                html += "<br>".join(smart_blocks)

            # Gyors összefoglaló doboz - szándékosan sötét/szürke, hogy elüssön a riport
            # világos "papíros" stílusától, mint egy gyors terminál-jellegű kivonat a lap alján.
            summary_rows = [
                ("🖥️", "Alaplap", pc_model),
                ("🧠", "Processzor", g(cpu, 'Name', 'Ismeretlen')),
                ("🎮", "Videokártya", ", ".join(gpu_summary_list) if gpu_summary_list else "Nincs adat"),
                ("🧩", "Memória", f"{tot_gb} ({len(ram_list)} db modul)"),
                ("💾", "Háttértár", ", ".join(storage_summary_list) if storage_summary_list else "Nincs adat"),
                ("🪟", "Operációs rendszer", f"{g(os_info, 'Caption', 'Ismeretlen')} ({g(os_info, 'OSArchitecture', 'Ismeretlen')})"),
            ]
            summary_items = "".join(
                f'<li><span class="si">{icon}</span><b>{e(label)}:</b> {e(value)}</li>'
                for icon, label, value in summary_rows
            )
            html += f"""</div>
        <div class="section summary-box">
            <h2>📋 Gyors Összefoglaló</h2>
            <ul class="summary-list">{summary_items}</ul>
        </div>"""

            html += "\n    </div>\n</div>"

            # Soronként külön <div>, mindegyik saját alsó szegéllyel (border-bottom) - EZ ad
            # egyenletes vastagságú vonalakat nyomtatásban is. Egy repeating-linear-gradient
            # háttérkép helyette a nyomtatási átméretezés (fractional DPI) miatt hol vékonyabb,
            # hol vastagabb vonalat rajzolt ki (böngészőnként/nyomtatásonként eltérő
            # kerekítéssel) - a border-bottom ezzel szemben minden sorban egyformán 1px.
            # Legalább 4 sor mindig látszik (üresen is, ha a megjegyzés rövidebb), hogy legyen
            # hely tollal írni - ha a megjegyzés hosszabb, minden sora megjelenik, plusz még
            # egy üres sor a végén a folytatáshoz.
            note_lines = (note or '').split('\n')
            while len(note_lines) < 4:
                note_lines.append('')
            note_lines.append('')
            note_lines_html = "".join(f'<div class="note-line">{e(line)}</div>' for line in note_lines)

            # Egy oldalra kényszerítő "shrink-to-fit": #page-ruler egy 267mm-es (A4 mínusz a
            # @page 15mm margói) rejtett elem, aminek a lemért px-magassága adja a valódi
            # nyomtatható területet DPI-találgatás nélkül. A root.scrollHeight ehhez képest
            # méri a tényleges tartalmat, és ha túlcsordulna, zsugorítja - de csak 0.75-ös
            # olvashatósági padlóig, az alatt inkább szépen 2. oldalra csúszik (lásd .section/
            # .item-block page-break-inside:avoid), mintsem olvashatatlanná váljon.
            # FONTOS: ez `zoom`-mal megy, NEM `transform: scale()`-lel - élesben tesztelve
            # (Chrome headless --print-to-pdf) kiderült, hogy a transform pusztán vizuális
            # (nem befolyásolja a nyomtatási lapszámítást, ami a transzformáció ELŐTTI
            # magassággal dolgozik), így a tartalom vizuálisan zsugorodna, de nyomtatáskor
            # mégis 2 oldalra törne. A `zoom` Chromium-ban valódi layout-újraszámolást vált ki
            # (a body ezért van rögzítve 180mm szélességre is - így képernyőn és nyomtatáskor
            # ugyanaz a sortördelés, nem téveszti meg a mérést egy szélesebb böngészőablak).
            html += f"""
<div class="note-section">
    <h2>📝 Megjegyzés</h2>
    <div class="note-content">{note_lines_html}</div>
</div>
</div>
<script>
(function() {{
    var root = document.getElementById('report-root');
    var ruler = document.getElementById('page-ruler');
    if (!root || !ruler) return;
    var bodyStyle = getComputedStyle(document.body);
    var vPad = parseFloat(bodyStyle.paddingTop) + parseFloat(bodyStyle.paddingBottom);
    var targetHeight = ruler.getBoundingClientRect().height - vPad;
    var actualHeight = root.scrollHeight;
    var MIN_SCALE = 0.75;
    if (targetHeight > 0 && actualHeight > targetHeight) {{
        var scale = Math.max(MIN_SCALE, targetHeight / actualHeight);
        root.style.zoom = scale;
    }}
}})();
</script>
</body></html>"""

            comp_name = os.environ.get('COMPUTERNAME', 'PC')
            safe_name = f"Rendszer_Riport_{comp_name}"

            # A DriverVarázsló saját adatmappájába mentjük (nem az exe mellé - lásd _app_data_dir).
            final_path = os.path.join(_app_data_dir(), f"{safe_name}.html")

            with open(final_path, "w", encoding="utf-8") as f:
                f.write(html)

            if os.path.exists(final_path):
                # A "Bolti nyomtatóval nyomtatás" gomb (print_via_store_printer) ebből
                # tudja, melyik fájlt kell kinyomtatnia - nem kér újra útvonalat a UI-tól.
                self._last_report_path = final_path
                return {'success': True, 'path': final_path}
            else:
                raise Exception("A fájl mentése sikertelen volt!")

        except Exception as e:
            logging.error(f"Hiba a jelentés generálásánál: {e}")
            logging.error(traceback.format_exc())
            raise Exception(str(e))

    def _cleanup_store_printer(self, printer_name, staged_driver_published_name):
        """Eltávolítja a bolti nyomtatót (és ha mi stageeltük, a drivert is) erről a
        gépről, miután a nyomtatás megtörtént - ez a program tipikusan idegen (ügyfél-)
        gépeken fut szervizelés közben, nem szabad rajta hagyni a bolti nyomtatót/drivert.
        Csak print_via_store_printer hívja, és csak akkor, ha MI adtuk hozzá ezt a
        nyomtatót ebben a futásban (we_added_printer) - a bolt saját, állandó gépén már
        eleve meglévő nyomtatóhoz ez sosem nyúl. Előbb megvárja, amíg a nyomtatási sor
        kiürül (a SumatraPDF `-exit-on-print`-je csak a nyomtatás API-hívás visszatéréséig
        vár, nem addig, amíg a spooler ténylegesen elküldi a bájtokat a hálózati
        nyomtatónak - ha a nyomtatót a job ténylegesen elküldése előtt távolítanánk el, a
        nyomtatás félbeszakadhatna)."""
        self.emit('task_progress', {'task': 'store_print', 'log': '🧹 Nyomtatási sor ürülésére várakozás...'})
        wait_ps = (
            f"$deadline = (Get-Date).AddSeconds(60); "
            f"while ((Get-Date) -lt $deadline) {{ "
            f"$jobs = Get-PrintJob -PrinterName '{_ps_quote(printer_name)}' -ErrorAction SilentlyContinue; "
            f"if (-not $jobs) {{ break }}; Start-Sleep -Milliseconds 500 }}"
        )
        self._run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', wait_ps], timeout=70)

        self.emit('task_progress', {'task': 'store_print', 'log': '🧹 Bolti nyomtató eltávolítása erről a gépről...'})
        remove_ps = (
            f"Remove-Printer -Name '{_ps_quote(printer_name)}' -ErrorAction SilentlyContinue; "
            f"Remove-PrinterPort -Name '{_ps_quote(STORE_PRINTER_PORT_NAME)}' -ErrorAction SilentlyContinue"
        )
        if staged_driver_published_name:
            remove_ps += f"; Remove-PrinterDriver -Name '{_ps_quote(STORE_PRINTER_HP_DRIVER_NAME)}' -ErrorAction SilentlyContinue"
        self._run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', remove_ps], timeout=60)

        # A Remove-PrinterDriver csak a nyomtatási alrendszerből veszi ki a drivert - az
        # INF-csomag maga még ott marad a driver store-ban, amíg pnputil /delete-driver
        # ki nem törli onnan is. Csak akkor tesszük, ha MI stageeltük (staged_driver_
        # published_name) - egy máshonnan (pl. Windows Update-ről korábban) már megvolt
        # driver csomagját nem töröljük, azt nem mi hoztuk létre.
        if staged_driver_published_name:
            self._run(['pnputil', '/delete-driver', staged_driver_published_name, '/uninstall', '/force'], timeout=60)

        self.emit('task_progress', {'task': 'store_print', 'log': '✅ Bolti nyomtató eltávolítva erről a gépről.'})

    def _find_hp_driver_inf(self, stress_dir):
        """Megkeresi a becsomagolt HP LaserJet 1320 driver INF fájlját (HPDriver mappa a
        stresstools.zip-ben, egy már működő gépről `pnputil /export-driver` exporttal
        kinyerve) - _resolve_store_printer_driver ezzel stageeli a drivert a driver
        store-ba `pnputil /add-driver`-rel, mielőtt Add-PrinterDriver hivatkozna rá."""
        for root, dirs, files in os.walk(stress_dir):
            for file in files:
                if file.lower() in HP_DRIVER_INF_FILENAMES:
                    return os.path.join(root, file)
        return None

    def _resolve_store_printer_driver(self):
        """Eldönti, melyik drivert használja a bolti nyomtató felvételéhez, ha az még nincs
        felvéve. Terepen bizonyított tapasztalat (két különböző random gépen tesztelve):
        NEM garantált, hogy a HP LaserJet 1320 drivere - vagy akár maga a referenciaként
        vett nyomtató - jelen van bármelyik gépen, ahol ez a funkció fut, ÉS az
        `Add-PrinterDriver -Name` ÖNMAGÁBAN NEM tölt le semmit a Windows Update-ről (ezt
        elsőre feltételeztük, de éles hiba - "The specified driver does not exist in the
        driver store" - bizonyította a tévedést: az interaktív "Nyomtató hozzáadása"
        varázsló automatikus driver-felismerése egy MÁS, PowerShell-ből el nem érhető
        mechanizmust használ). Emiatt egyre általánosabb, egyre kevésbé kényelmes (de még
        mindig működő) lehetőségeket próbálunk sorban:
          1) ha VÉLETLENÜL már fel van véve egy nyomtató ezzel a referencia névvel ezen a
             gépen, az ő drivere (legjobb eset - pontosan ez a modell, semmi extra munka)
          2) a stresstools.zip-be csomagolt HP driver (HPDriver mappa) `pnputil
             /add-driver ... /install`-lal a driver store-ba stageelve, majd
             Add-PrinterDriver-rel regisztrálva - ez internet nélkül, BÁRMELYIK gépen
             működik, mert nem külső forrásra (Windows Update), hanem a saját
             becsomagolt fájljainkra támaszkodik
        Ha egyik sem sikerül (a ZIP-ben sincs meg az INF, vagy a pnputil stageelés
        elhasal), Exception-t dob - ez esetben a nyomtatót egyszer manuálisan, kézzel kell
        hozzáadni ezen a gépen.

        Visszatérési érték: (driver_name, staged_published_name). staged_published_name
        None, ha a driver már eleve megvolt (1. eset) - ilyenkor a hívó (print_via_
        store_printer) NEM törölheti a drivert nyomtatás után, hiszen az nem általunk lett
        stageelve, más (pl. a referencia nyomtató) is használhatja. Ha viszont mi
        stageeltük most (2. eset), a "oemXX.inf" publikált nevet adjuk vissza, hogy a hívó
        ezzel pontosan visszatudja vonni (`pnputil /delete-driver`) - lásd a "ne maradjon
        rajta az ügyfél gépén a mi driverünk" elvárást a print_via_store_printer végén."""
        ref_ps = f"(Get-Printer -Name '{_ps_quote(STORE_PRINTER_REFERENCE_NAME)}' -ErrorAction Stop).DriverName"
        res = self._run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ref_ps], encoding='utf-8')
        driver_name = (res.stdout or '').strip() if res else ''
        if driver_name:
            self.emit('task_progress', {'task': 'store_print', 'log': f'✅ Meglévő HP driver újrahasznosítva: {driver_name}'})
            return driver_name, None

        self.emit('task_progress', {'task': 'store_print', 'log': '📦 HP driver keresése a becsomagolt fájlok között...'})
        stress_dir = self._download_stresstools()
        inf_path = self._find_hp_driver_inf(stress_dir) if stress_dir else None
        if not inf_path:
            raise Exception(
                f"Nincs meg a becsomagolt HP LaserJet 1320 driver (HPDriver mappa) a "
                f"stresstools.zip-ben, és ezen a gépen sincs máshonnan telepítve. Egyszer, "
                f"ezen a gépen, kézzel kell hozzáadni a nyomtatót (Nyomtatók és szkennerek "
                f"-> Nyomtató hozzáadása -> IP-cím: {STORE_PRINTER_IP}) - utána a program "
                f"már felismeri és újra tudja használni."
            )

        self.emit('task_progress', {'task': 'store_print', 'log': '⬇️ HP driver telepítése a driver store-ba (pnputil)...'})
        stage_res = self._run(['pnputil', '/add-driver', inf_path, '/install'], timeout=120)
        # A pnputil kilépési kódja NEM megbízható sikerjelzés: élesben tesztelve, ha a
        # driver már staged, "Driver package added successfully. (Already exists in the
        # system)" szöveggel tér vissza, miközben a kilépési kódja 5 (nem 0!) - a szöveges
        # kimenetet kell nézni, nem a returncode-ot.
        stage_out = (stage_res.stdout or '') if stage_res else ''
        if not stage_res or 'successfully' not in stage_out.lower():
            err_detail = (stage_res.stderr or stage_res.stdout or 'ismeretlen hiba') if stage_res else 'ismeretlen hiba'
            raise Exception(f"A HP driver telepítése (pnputil /add-driver) sikertelen: {err_detail}")

        # A "Published Name:  oemXX.inf" sor kell ahhoz, hogy a hívó nyomtatás után
        # pontosan EZT a stageelt csomagot tudja visszavonni (pnputil /delete-driver) -
        # az oemXX szám gépenként/futásonként más lehet, nem lehet előre feltételezni.
        published_match = re.search(r'Published Name\s*:\s*(oem\d+\.inf)', stage_out, re.IGNORECASE)
        staged_published_name = published_match.group(1) if published_match else None

        install_ps = f"Add-PrinterDriver -Name '{_ps_quote(STORE_PRINTER_HP_DRIVER_NAME)}' -ErrorAction Stop"
        ires = self._run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', install_ps], encoding='utf-8', timeout=60)
        if ires and ires.returncode == 0:
            self.emit('task_progress', {'task': 'store_print', 'log': f'✅ HP driver telepítve: {STORE_PRINTER_HP_DRIVER_NAME}'})
            return STORE_PRINTER_HP_DRIVER_NAME, staged_published_name

        err_detail = (ires.stderr or ires.stdout or 'ismeretlen hiba') if ires else 'ismeretlen hiba'
        raise Exception(
            f"A HP driver a pnputil stageelés után sem regisztrálható nyomtató-driverként: "
            f"{err_detail}\nEgyszer, ezen a gépen, kézzel kell hozzáadni a nyomtatót "
            f"(Nyomtatók és szkennerek -> Nyomtató hozzáadása -> IP-cím: {STORE_PRINTER_IP}) "
            f"- utána a program már felismeri és újra tudja használni."
        )

    def _find_msedge_exe(self):
        """Megkeresi a telepített Edge böngészőt (msedge.exe) - a riport HTML->PDF
        alakításához kell. FONTOS: ez NEM ugyanaz, mint a WebView2 Runtime, amit az app
        amúgy is megkövetel (check_webview2_runtime) - az egy beágyazható futtatókörnyezet,
        önálló msedge.exe nélkül is jelen lehet, ezért ezt külön, a szokásos telepítési
        útvonalakon keressük."""
        candidates = [
            os.path.join(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'), 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
            os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'), 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
        ]
        for c in candidates:
            if c and os.path.exists(c):
                return c
        return None

    def _find_sumatra_exe(self, stress_dir):
        """Megkeresi a SumatraPDF.exe-t a stresstools.zip kicsomagolt mappájában - a néma
        (dialógus nélküli) PDF-nyomtatáshoz kell (print_via_store_printer). Ugyanabba a
        ZIP-be kerül, mint a stabilitás-teszt eszközök, hogy ne kelljen külön letöltési
        logika/URL egy apró segédprogramért."""
        for root, dirs, files in os.walk(stress_dir):
            for file in files:
                if file.lower() in SUMATRA_PDF_FILENAMES:
                    return os.path.join(root, file)
        return None

    def print_via_store_printer(self):
        """A legutóbb generált Rendszer Riport kinyomtatása a Microstore bolti hálózati
        nyomtatójára, egyetlen kattintással. Ha a nyomtató még nincs felvéve a Windows
        nyomtatói közé, felveszi - a driver kiválasztását lásd _resolve_store_printer_driver
        (terepen bizonyítva: NEM garantált, hogy a HP LaserJet 1320 drivere - vagy akár
        maga a referencia nyomtató - jelen van azon a gépen, ahol ez fut, ezért ott több,
        egyre általánosabb lehetőséget próbálunk sorban, nem csak egyet). A nyomtatás maga
        headless Edge-dzsel PDF-be alakítja a riportot (ugyanaz a motor, ami a report
        egy-oldalas zoom-alapú tördelését is renderelte - a nyomtatott PDF pontosan azt
        adja, amit böngészőben látni), majd a SumatraPDF-fel (stresstools.zip) néma
        nyomtatással a nyomtatóra küldi."""
        logging.info("[API] print_via_store_printer()")
        report_path = self._last_report_path
        if not report_path or not os.path.exists(report_path):
            self.emit('toast', {'message': '⚠️ Nincs elérhető generált riport - előbb generáld le a Rendszer Riportot!', 'type': 'warning'})
            return

        def worker():
            self.emit('task_start', {'task': 'store_print', 'title': 'Nyomtatás a Bolti Nyomtatóra'})
            self.emit('task_progress', {'task': 'store_print', 'log': f'📡 Nyomtató keresése a hálózaton ({STORE_PRINTER_IP})...', 'indeterminate': True})

            # 1) Elérhető-e egyáltalán a nyomtató a hálózaton? A nyers nyomtatási (JetDirect,
            # 9100/tcp) porthoz csatlakozunk - ez megbízhatóbb jel, mint egy ICMP ping, mert
            # sok nyomtató blokkolja/nem válaszol pingre, de a nyomtatási portot figyeli.
            reachable = False
            try:
                with socket.create_connection((STORE_PRINTER_IP, 9100), timeout=3):
                    reachable = True
            except OSError:
                reachable = False
            if not reachable:
                raise Exception(f"A bolti nyomtató ({STORE_PRINTER_IP}) nem érhető el a hálózaton - lehet, hogy nem a bolt hálózatán vagy, vagy a nyomtató ki van kapcsolva.")

            # 2) Már fel van-e véve Windows nyomtatóként ehhez az IP-hez?
            find_ps = (
                f"$port = Get-PrinterPort | Where-Object {{ $_.PrinterHostAddress -eq '{STORE_PRINTER_IP}' }} | Select-Object -First 1; "
                "if ($port) { $p = Get-Printer | Where-Object { $_.PortName -eq $port.Name } | Select-Object -First 1; if ($p) { Write-Output $p.Name } }"
            )
            res = self._run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', find_ps], encoding='utf-8')
            existing_name = (res.stdout or '').strip() if res else ''

            # we_added_printer/staged_driver_published_name: mit hoztunk létre MI EBBEN a
            # futásban, hogy a végén pontosan azt (és csakis azt) takarítsuk el - lásd a
            # "ne maradjon rajta az ügyfél gépén a bolti nyomtató/driver" elvárást lentebb.
            # Ha a nyomtató már eleve ott volt (pl. a bolt saját, állandó gépén), ahhoz
            # NEM nyúlunk hozzá utólag sem.
            we_added_printer = False
            staged_driver_published_name = None
            printer_name = existing_name or None

            # A takarítást (5. lépés) `finally`-ben végezzük, NEM csak a sikeres út végén -
            # terepen bizonyítva: ha a nyomtató/driver hozzáadása után VALAMI MÁS lépés
            # (pl. a PDF-generálás) hasal el, az a régi kód mellett félig hozzáadott
            # állapotban hagyta a gépet (nyomtató+port megvan, de a program hibával leáll)
            # - ez nemcsak az "ügyfél gépén ne maradjon nyoma" elvárást sérti, hanem egy
            # következő próbálkozást is elront (lásd: "Add-PrinterPort: The specified port
            # already exists" - a leftover port miatt). A `finally` biztosítja, hogy amit MI
            # adtunk hozzá, az sikeres ÉS sikertelen kilépéskor is eltakarodjon.
            try:
                if existing_name:
                    self.emit('task_progress', {'task': 'store_print', 'log': f'✅ A nyomtató már fel van véve: {printer_name}'})
                else:
                    self.emit('task_progress', {'task': 'store_print', 'log': '➕ A nyomtató még nincs felvéve - driver előkészítése...'})
                    driver_name, staged_driver_published_name = self._resolve_store_printer_driver()

                    # A porthoz ÉS a nyomtató nevéhez is külön, idempotens ellenőrzés kell:
                    # terepen bizonyítva, hogy a port és a nyomtató objektum egymástól
                    # függetlenül is szinkronon kívülre kerülhet (egy korábbi, félbeszakadt
                    # próbálkozásból a port megmaradt, miközben a nyomtató objektum már nem
                    # volt megtalálható a fenti existing_name lekérdezéssel) - egy sima,
                    # feltétel nélküli `Add-PrinterPort`/`Add-Printer -ErrorAction Stop`
                    # ilyenkor rögtön elhasalna ("already exists"), mielőtt egyáltalán
                    # esélyt kapna az egyébként ártalmatlan újrahasznosításra.
                    add_ps = (
                        f"if (-not (Get-PrinterPort -Name '{_ps_quote(STORE_PRINTER_PORT_NAME)}' -ErrorAction SilentlyContinue)) "
                        f"{{ Add-PrinterPort -Name '{_ps_quote(STORE_PRINTER_PORT_NAME)}' -PrinterHostAddress '{STORE_PRINTER_IP}' -ErrorAction Stop }}; "
                        f"if (-not (Get-Printer -Name '{_ps_quote(STORE_PRINTER_NAME)}' -ErrorAction SilentlyContinue)) "
                        f"{{ Add-Printer -Name '{_ps_quote(STORE_PRINTER_NAME)}' -DriverName '{_ps_quote(driver_name)}' -PortName '{_ps_quote(STORE_PRINTER_PORT_NAME)}' -ErrorAction Stop }}"
                    )
                    ares = self._run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', add_ps], encoding='utf-8')
                    if not ares or ares.returncode != 0:
                        err_detail = (ares.stderr or ares.stdout or 'ismeretlen hiba') if ares else 'ismeretlen hiba'
                        raise Exception(f"Nem sikerült felvenni a nyomtatót: {err_detail}")
                    printer_name = STORE_PRINTER_NAME
                    we_added_printer = True
                    self.emit('task_progress', {'task': 'store_print', 'log': f'✅ Nyomtató felvéve: {printer_name}'})

                # 3) HTML -> PDF headless Edge-dzsel.
                self.emit('task_progress', {'task': 'store_print', 'log': '🖨️ PDF előállítása a riportból...'})
                msedge = self._find_msedge_exe()
                if not msedge:
                    raise Exception("Nem található az Edge böngésző (msedge.exe) ezen a gépen - a PDF-generáláshoz szükséges.")

                pdf_path = os.path.splitext(report_path)[0] + '_print.pdf'
                file_url = 'file:///' + report_path.replace('\\', '/')
                pdf_cmd = [
                    msedge, '--headless', '--disable-gpu', '--no-sandbox',
                    f'--print-to-pdf={pdf_path}', '--no-pdf-header-footer',
                    '--run-all-compositor-stages-before-draw', '--virtual-time-budget=3000',
                    file_url,
                ]
                self._run(pdf_cmd, timeout=60)
                # A msedge --print-to-pdf hívás visszatérése nem mindig jelenti azt, hogy a
                # PDF fájl írása is befejeződött (terepen bizonyítva: a subprocess ~0.6mp
                # alatt visszatért, miközben a PDF ténylegesen csak egy kicsit később jelent
                # meg a lemezen - valószínűleg egy háttérben tovább futó gyerekfolyamat
                # fejezte csak be az írást) - ezért rövid ideig újrapróbálkozunk ahelyett,
                # hogy egyetlen azonnali ellenőrzés után hibát adnánk.
                for _ in range(20):
                    if os.path.exists(pdf_path):
                        break
                    time.sleep(0.5)
                else:
                    raise Exception("A riport PDF-be alakítása sikertelen.")

                # 4) PDF -> néma nyomtatás a bolti nyomtatóra.
                self.emit('task_progress', {'task': 'store_print', 'log': '📤 Nyomtatás küldése...'})
                stress_dir = self._download_stresstools()
                sumatra = self._find_sumatra_exe(stress_dir) if stress_dir else None
                if not sumatra:
                    raise Exception("A SumatraPDF nem található (a stresstools.zip-ben kell lennie) - néma nyomtatás nem lehetséges.")

                self._run([sumatra, '-print-to', printer_name, '-silent', '-exit-on-print', pdf_path], timeout=60)
                try: os.remove(pdf_path)
                except: pass

                self.emit('task_progress', {'task': 'store_print', 'log': f'✅ Kinyomtatva: {printer_name}'})
                self.emit('task_complete', {'task': 'store_print', 'status': f'✅ Riport kinyomtatva a bolti nyomtatóra ({printer_name})!'})
            finally:
                # 5) Takarítás: az ügyfél gépén ne maradjon rajta a mi bolti nyomtatónk/
                # driverünk - csak azt távolítjuk el, amit MI adtunk hozzá ebben a futásban
                # (we_added_printer/staged_driver_published_name), a bolt saját, állandó
                # gépén már eleve ott lévő nyomtatóhoz/driverhez nem nyúlunk. Egy esetleges
                # takarítási hiba nem írja felül/nyeli el a fenti try-ban ténylegesen
                # történteket (sikert vagy hibát) - csak figyelmeztetésként logolva.
                if we_added_printer:
                    try:
                        self._cleanup_store_printer(printer_name, staged_driver_published_name)
                    except Exception as cleanup_err:
                        logging.warning(f"[STORE_PRINT] Takarítási hiba: {cleanup_err}")
                        self.emit('task_progress', {'task': 'store_print', 'log': f'⚠️ A bolti nyomtató eltávolítása ezen a gépen nem sikerült: {cleanup_err}'})

        self._safe_thread('store_print', worker)


# ================================================================
# CLI MÓD - Teljes funkcionalitás (GUI tükör)
# ================================================================
class CliApi:
    """CLI verzió API - ugyanazokat a funkciókat hívja mint a GUI, de konzolra ír."""
    
    def __init__(self):
        self.target_os_path = None
        self.sys_drive = os.environ.get('SystemDrive', 'C:') + '\\'
        self._si = subprocess.STARTUPINFO()
        self._si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        self._nw = subprocess.CREATE_NO_WINDOW
        self._cancel_flag = False
    
    def _run(self, cmd, **kwargs):
        """Parancs futtatás (CLI verzió)."""
        cmd_str = cmd if isinstance(cmd, str) else ' '.join(str(c) for c in cmd)
        logging.debug(f"[CMD_CLI] Futtatás: {cmd_str[:300]}")
        # stdin alapból DEVNULL - lásd DriverToolApi._run azonos sorát (érvénytelenné vált
        # örökölt stdin handle elleni védelem; CLI-ben konzisztencia okán ugyanígy).
        kwargs.setdefault('stdin', subprocess.DEVNULL)
        start = time.time()
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, errors='replace',
                                  startupinfo=self._si, creationflags=self._nw, **kwargs)
            elapsed = time.time() - start
            if result.returncode != 0:
                logging.warning(f"[CMD_CLI] Visszatérési kód: {result.returncode} ({elapsed:.1f}s)")
                if result.stderr:
                    logging.warning(f"[CMD_CLI] stderr: {result.stderr[:4000]}")
            else:
                logging.debug(f"[CMD_CLI] OK ({elapsed:.1f}s)")
            
            if result.stdout:
                out_txt = result.stdout.strip()
                if len(out_txt) > 4000: out_txt = out_txt[:4000] + '... [TRUNCATED]'
                logging.debug(f"[CMD_CLI] stdout: {out_txt}")
            return result
        except Exception as e:
            logging.error(f"[CMD_CLI] Kivétel: {e}")
            class DummyRes:
                returncode = 1
                stdout = ""
                stderr = str(e)
            return DummyRes()
    
    def _print_progress(self, msg, end='\n'):
        """Progress kiírás."""
        print(msg, end=end, flush=True)
    
    # ================================================================
    # DRIVER KEZELÉS
    # ================================================================
    def get_third_party_drivers(self):
        """Third-party driverek listája."""
        self._print_progress("📋 Third-party driverek lekérdezése...")
        # dism /English-lel a kimenet mindig angol, függetlenül a Windows nyelvi
        # beállításától - a pnputil-lel ellentétben nincs "csak angol/magyar kulcsot
        # ismerünk fel" locale-probléma (más nyelvű Windows-on üres listát adott volna).
        res = self._run(['dism', '/English', '/Online', '/Get-Drivers'])
        drivers = []
        current = {}
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                if current and "published" in current:
                    drivers.append(current)
                    current = {}
                continue
            parts = line.split(":", 1)
            if len(parts) == 2:
                key, val = parts[0].strip(), parts[1].strip()
                if "Published Name" in key:
                    current["published"] = val
                elif "Original File Name" in key:
                    current["original"] = val
                elif "Provider Name" in key:
                    current["provider"] = val
                elif "Class Name" in key:
                    current["class"] = val
                elif "Version" in key:
                    current["version"] = val
        if current and "published" in current:
            drivers.append(current)
        return drivers

    def get_all_drivers(self):
        """Összes driver listája (veszélyes mód)."""
        self._print_progress("📋 Összes driver lekérdezése (PowerShell)...")
        cmd = ['powershell', '-NoProfile', '-Command',
               '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Get-WindowsDriver -Online -All | Select-Object ProviderName, ClassName, Version, Driver, OriginalFileName | ConvertTo-Json -Depth 2 -WarningAction SilentlyContinue']
        res = self._run(cmd, encoding='utf-8')
        out = res.stdout.strip()
        if not out:
            return []
        try:
            data = json.loads(out)
            if isinstance(data, dict):
                data = [data]
            parsed_drivers = [{"published": d.get("Driver", ""), "original": d.get("OriginalFileName", ""),
                     "provider": d.get("ProviderName", ""), "class": d.get("ClassName", ""),
                     "version": d.get("Version", "")} for d in data]
        except Exception:
            return []

        # Szellem (force-delete-elt) driverek kiszűrése - ugyanaz a logika, mint a GUI-ban:
        # egy nem-oem publikált nevű bejegyzés csak akkor valódi, ha még van hozzá tartozó
        # mappa a DriverStore-ban, különben egy korábban force-delete-elt phantom bejegyzés.
        valid_drivers = []
        rep = os.path.join(os.environ.get('SYSTEMROOT', r'C:\Windows'), "System32", "DriverStore", "FileRepository")
        for d in parsed_drivers:
            pub = d.get("published", "")
            if not pub:
                continue
            if pub.lower().startswith("oem"):
                valid_drivers.append(d)
                continue
            if glob.glob(os.path.join(rep, f"{pub}_*")):
                valid_drivers.append(d)
        return valid_drivers
    
    def get_offline_drivers(self, all_drivers=False):
        """Offline OS driverek listája."""
        self._print_progress(f"📋 Offline driverek lekérdezése: {self.target_os_path}...")
        # /English: a GUI verzióval egyezően kényszerített angol DISM kimenet, függetlenül a
        # futtató Windows/WinPE nyelvétől - enélkül nem angol rendszeren a lenti angol
        # kulcsszavak (Published Name stb.) nem illeszkednek, és a lista némán üres marad.
        cmd = ['dism', '/English', f'/Image:{self.target_os_path}', '/Get-Drivers']
        if all_drivers:
            cmd.append('/all')
        res = self._run(cmd)
        drivers = []
        current = {}
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                if current and "published" in current:
                    drivers.append(current)
                    current = {}
                continue
            parts = line.split(":", 1)
            if len(parts) == 2:
                key, val = parts[0].strip(), parts[1].strip()
                if "Published Name" in key:
                    current["published"] = val
                elif "Original File Name" in key:
                    current["original"] = val
                elif "Provider Name" in key:
                    current["provider"] = val
                elif "Class Name" in key:
                    current["class"] = val
                elif "Version" in key:
                    current["version"] = val
        if current and "published" in current:
            drivers.append(current)
            
        valid_drivers = []
        rep = os.path.join(self.target_os_path, "Windows", "System32", "DriverStore", "FileRepository")
        for d in drivers:
            pub = d.get("published", "")
            if not pub:
                continue
            if pub.lower().startswith("oem"):
                valid_drivers.append(d)
                continue
            if glob.glob(os.path.join(rep, f"{pub}_*")):
                valid_drivers.append(d)
                
        return valid_drivers
    
    def list_drivers(self, all_drivers=False):
        """Driver lista megjelenítése."""
        if self.target_os_path:
            drivers = self.get_offline_drivers(all_drivers)
        elif all_drivers:
            drivers = self.get_all_drivers()
        else:
            drivers = self.get_third_party_drivers()
        
        if not drivers:
            print("❌ Nincs találat vagy hiba történt.")
            return []
        
        mode = "ÖSSZES" if all_drivers else "Third-party"
        loc = f" ({self.target_os_path})" if self.target_os_path else ""
        print(f"\n{'='*60}")
        print(f"  {mode} driverek{loc}: {len(drivers)} db")
        print(f"{'='*60}")
        print(f"{'#':>4}  {'Published':<18} {'Provider':<25} {'Class':<15}")
        print("-" * 70)
        for i, d in enumerate(drivers, 1):
            pub = d.get('published', '?')[:17]
            prov = d.get('provider', '?')[:24]
            cls = d.get('class', '?')[:14]
            print(f"{i:4}  {pub:<18} {prov:<25} {cls:<15}")
        print("-" * 70)
        return drivers
    
    def delete_drivers(self, drivers, list_all=False, reboot=False):
        """Driverek törlése."""
        total = len(drivers)
        print(f"\n🗑️  {total} driver törlése indul...")
        print("-" * 50)

        success = 0
        fail = 0
        is_offline = bool(self.target_os_path)

        for i, drv in enumerate(drivers, 1):
            pub = drv.get('published', '?')
            print(f"  [{i}/{total}] {pub}... ", end="", flush=True)

            is_oem = pub.lower().startswith("oem")

            if is_offline:
                res = self._run(['dism', f'/Image:{self.target_os_path}', '/Remove-Driver', f'/Driver:{pub}'])
            else:
                res = self._run(['pnputil', '/delete-driver', pub, '/uninstall', '/force'])

            if res.returncode == 0 or any(k in res.stdout.lower() for k in ['deleted', 'törölve', 'successfully']):
                print("✅")
                success += 1
            else:
                # A GUI verzióval egyezően az agresszív force-delete fallback (takeown/icacls/
                # rmtree) csak "ÖSSZES driver" módban fut le - harmadik féltől eltérő
                # (list_all=False) nézetben egy sikertelen törlés egyszerűen sikertelen marad,
                # nem próbálunk erőszakkal beleírni a DriverStore-ba.
                if list_all and not is_oem:
                    found_any = False
                    if is_offline:
                        rep = os.path.join(self.target_os_path, "Windows", "System32", "DriverStore", "FileRepository")
                        inf_dir = os.path.join(self.target_os_path, "Windows", "INF")
                    else:
                        rep = os.path.join(os.environ.get('SYSTEMROOT', r'C:\Windows'), "System32", "DriverStore", "FileRepository")
                        inf_dir = os.path.join(os.environ.get('SYSTEMROOT', r'C:\Windows'), "INF")
                    
                    dirs = glob.glob(os.path.join(rep, f"{pub}_*"))
                    if dirs:
                        for d in dirs:
                            self._run(f'takeown /f "{d}" /r /A', shell=True)
                            self._run(f'icacls "{d}" /grant *S-1-5-32-544:F /t', shell=True)
                            shutil.rmtree(d, ignore_errors=True)
                            self._run(f'rmdir /s /q "{d}"', shell=True)
                        found_any = True
                        
                    bname = os.path.splitext(pub)[0]
                    for ext in ['.inf', '.pnf', '.INF', '.PNF']:
                        fpath = os.path.join(inf_dir, bname + ext)
                        if os.path.exists(fpath):
                            self._run(f'takeown /f "{fpath}" /A', shell=True)
                            self._run(f'icacls "{fpath}" /grant *S-1-5-32-544:F', shell=True)
                            try:
                                os.remove(fpath)
                                found_any = True
                            except OSError:
                                self._run(f'del /f /q "{fpath}"', shell=True)
                                found_any = True
                    
                    if found_any:
                        print("✅ (force)")
                        success += 1
                    else:
                        print("❌")
                        fail += 1
                else:
                    print("❌")
                    fail += 1
        
        print("-" * 50)
        print(f"✅ Sikeres: {success}  |  ❌ Sikertelen: {fail}")
        
        # Post-delete scan
        if not is_offline and success > 0:
            print("\n🔄 Hardverek újraszkennelése...")
            self._run(['pnputil', '/scan-devices'])
            time.sleep(2)
            print("✅ Kész!")
            
            if reboot:
                print("\n🔄 Újraindítás 5 másodperc múlva...")
                time.sleep(5)
                self._run(['shutdown', '/r', '/t', '0', '/f'])
        
        return success, fail
    
    # ================================================================
    # MENTÉS ÉS VISSZAÁLLÍTÁS
    # ================================================================
    def backup_third_party(self, dest_folder):
        """Third-party driverek mentése."""
        folder = os.path.join(dest_folder, f"DriverVarázsló_Export_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        os.makedirs(folder, exist_ok=True)
        print("\n💾 Third-party driverek mentése...")
        print(f"   Cél: {folder}")
        print("-" * 50)
        
        if self.target_os_path:
            res = self._run(['dism', f'/Image:{self.target_os_path}', '/export-driver', f'/destination:{folder}'])
        else:
            res = self._run(['dism', '/online', '/export-driver', f'/destination:{folder}'])
        
        if res.returncode == 0:
            print("✅ Mentés sikeres!")
            return folder
        else:
            print(f"❌ Hiba: {res.stderr[:200] if res.stderr else 'Ismeretlen hiba'}")
            return None
    
    def backup_all(self, dest_folder):
        """Összes driver mentése (OEM + inbox)."""
        folder = os.path.join(dest_folder, f"DriverVarázsló_FullExport_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        os.makedirs(folder, exist_ok=True)
        print("\n💾 ÖSSZES driver mentése...")
        print(f"   Cél: {folder}")
        print("-" * 50)
        
        success = 0
        # OEM driverek
        print("1/3 OEM driverek exportálása...")
        dism_cmd = ['dism', f'/Image:{self.target_os_path}', '/export-driver', f'/destination:{folder}'] if self.target_os_path else ['dism', '/online', '/export-driver', f'/destination:{folder}']
        res = self._run(dism_cmd)
        if res.returncode == 0:
            print("   ✅ DISM export sikeres")
            success = 1
        else:
            print(f"   ❌ DISM export hiba: {res.stderr[:200]}")
        
        # FileRepository (inbox)
        print("2/3 Windows inbox driverek (FileRepository) másolása...")
        windows_dir = os.path.join(self.target_os_path, 'Windows') if self.target_os_path else os.environ.get('SYSTEMROOT', r'C:\Windows')
        driverstore = os.path.join(windows_dir, 'System32', 'DriverStore', 'FileRepository')
        inbox_folder = os.path.join(folder, '_Windows_Inbox_Drivers')
        os.makedirs(inbox_folder, exist_ok=True)
        self._run(['robocopy', driverstore, inbox_folder, '/E', '/R:0', '/W:0', '/NFL', '/NDL', '/NJH', '/NJS', '/NC', '/NS', '/NP'])
        print("   ✅ FileRepository másolva")
        
        # INF mappa
        print("3/3 Windows INF mappa másolása...")
        inf_src = os.path.join(windows_dir, 'INF')
        inbox_inf = os.path.join(folder, '_Windows_Inbox_INF')
        os.makedirs(inbox_inf, exist_ok=True)
        self._run(['robocopy', inf_src, inbox_inf, '/E', '/R:0', '/W:0', '/NFL', '/NDL', '/NJH', '/NJS', '/NC', '/NS', '/NP'])
        print("   ✅ INF mappa másolva")
        
        # Összegzés
        total_size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fns in os.walk(folder) for f in fns if os.path.exists(os.path.join(dp, f)))
        print("-" * 50)
        print(f"✅ Mentés kész! Méret: {total_size / (1024*1024):.0f} MB")
        return folder
    
    def restore_drivers(self, source_folder, online=True):
        """Driverek visszaállítása."""
        print(f"\n{'♻️'} Driverek visszaállítása...")
        print(f"   Forrás: {source_folder}")
        if not online:
            print(f"   Cél: {self.target_os_path}")
        print("-" * 50)
        
        if online and not self.target_os_path:
            # Online mód - pnputil
            print("🔄 pnputil /add-driver futtatása...")
            res = self._run(['pnputil', '/add-driver', f"{source_folder}\\*.inf", '/subdirs', '/install'])
            if res.returncode == 0:
                print("✅ Visszaállítás sikeres!")
            else:
                print("⚠️  Részleges siker vagy hiba. Részletek:")
                print(res.stdout[:500] if res.stdout else res.stderr[:500])
            
            print("\n🔄 Hardverek újraszkennelése...")
            self._run(['pnputil', '/scan-devices'])
            time.sleep(10)
            print("✅ Kész!")
        else:
            # Offline mód - DISM
            target = self.target_os_path or input("Cél OS meghajtó (pl: D:\\): ").strip()
            if not target:
                print("❌ Nincs cél megadva!")
                return False

            # Formátum-detektálás: a DISM /Add-Driver egyedül NEM tudja telepíteni az inbox
            # (Windows-natív) drivereket, mert nincs hozzájuk class installer - ezért ezeket
            # fizikailag is át kell másolni a DriverStore-ba, ugyanúgy mint a GUI verzióban.
            norm_source = os.path.normpath(source_folder)
            repo_check = os.path.join(norm_source, "FileRepository")
            inf_check = os.path.join(norm_source, "INF")
            is_wim_extract = os.path.isdir(repo_check) or os.path.isdir(inf_check)
            inbox_subfolder = os.path.join(norm_source, "_Windows_Inbox_Drivers")
            has_inbox_subfolder = os.path.isdir(inbox_subfolder)

            target_repo = os.path.join(target, "Windows", "System32", "DriverStore", "FileRepository")
            target_inf = os.path.join(target, "Windows", "INF")
            had_errors = False

            if is_wim_extract:
                print("WIM-ből kimentett gyári driverek észlelve - fizikai másolás (a DISM egyedül nem tudja telepíteni az inbox drivereket)...")
                if os.path.exists(repo_check):
                    had_errors = self._force_copy_cli(repo_check, target_repo) or had_errors
                    if os.path.exists(inf_check):
                        had_errors = self._force_copy_cli(inf_check, target_inf) or had_errors
                else:
                    had_errors = self._force_copy_cli(norm_source, target_repo) or had_errors
            elif has_inbox_subfolder:
                print("Teljes export formátum észlelve (_Windows_Inbox_Drivers) - inbox driverek fizikai másolása...")
                had_errors = self._force_copy_cli(inbox_subfolder, target_repo) or had_errors
                inbox_inf_subfolder = os.path.join(norm_source, "_Windows_Inbox_INF")
                if os.path.isdir(inbox_inf_subfolder):
                    had_errors = self._force_copy_cli(inbox_inf_subfolder, target_inf) or had_errors

            print(f"🔄 DISM /Add-Driver futtatása ({target})...")
            scratch = os.path.join(target, "Scratch")
            os.makedirs(scratch, exist_ok=True)
            res = self._run(['dism', f'/Image:{target}', '/Add-Driver', f'/Driver:{norm_source}', '/Recurse', '/ForceUnsigned', f'/ScratchDir:{scratch}'])

            if res.returncode == 0 and not had_errors:
                print("✅ Visszaállítás sikeres!")
            elif had_errors:
                print("⚠️  A DISM regisztráció lefutott, DE a fizikai másolás hibákkal fejeződött be - a napló tartalmazza a részleteket, a visszaállítás valószínűleg HIÁNYOS!")
            else:
                print("⚠️  Részleges siker vagy hiba. Néhány inbox driver nem telepíthető DISM-mel.")
                print(res.stdout[:300] if res.stdout else "")

            # === BCD JAVÍTÁS (boot loader) ===
            self._repair_bcd_cli(target)

        return True

    def _force_copy_cli(self, src, dst):
        """Robocopy-alapú kényszerített másolás jogosultság-megkerüléssel (CLI verzió a GUI
        force_copy-jának megfelelője). Visszatérési érték: True, ha hiba történt."""
        if not os.path.exists(src):
            print(f"  ⚠️  Forrás nem létezik: {src}")
            return True
        os.makedirs(dst, exist_ok=True)

        needed_bytes = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(src) for f in fs if os.path.exists(os.path.join(r, f)))
        free_bytes = shutil.disk_usage(dst).free
        if needed_bytes > free_bytes:
            print(f"  ❌ Nincs elég szabad hely! Szükséges kb. {needed_bytes // (1024*1024)} MB, elérhető: {free_bytes // (1024*1024)} MB.")
            return True

        print(f"  Robocopy: {os.path.basename(src)} -> {os.path.basename(dst)}")
        cmd = ['robocopy', src, dst, '/E', '/ZB', '/R:1', '/W:1', '/COPY:DAT', '/NC', '/NS', '/NFL', '/NDL', '/NP']
        res = self._run(cmd)
        if res.returncode < 8:
            print(f"  ✅ Sikeres robocopy ({res.returncode})")
            return False

        print(f"  ⚠️  Robocopy hiba ({res.returncode}), tartalék: mappánkénti jogszerzés (lassabb)...")
        had_error = False
        for root, _, files in os.walk(src):
            rel = os.path.relpath(root, src)
            target_dir = os.path.join(dst, rel) if rel != '.' else dst
            os.makedirs(target_dir, exist_ok=True)
            for f in files:
                sfile = os.path.join(root, f)
                dfile = os.path.join(target_dir, f)
                if os.path.exists(dfile):
                    self._run(f'takeown /f "{dfile}" /A', shell=True)
                    self._run(f'icacls "{dfile}" /grant *S-1-5-32-544:F', shell=True)
                    self._run(f'attrib -R "{dfile}"', shell=True)
                try:
                    shutil.copy2(sfile, dfile)
                except Exception as e:
                    print(f"  ❌ Hiba ({f}): {e}")
                    had_error = True
        print("  ⚠️  Fallback másolás hibákkal fejeződött be." if had_error else "  ✅ Fallback másolás befejeződött.")
        return had_error

    def _repair_bcd_cli(self, target_drive):
        """BCD újraépítése CLI módban - megkeresi a megfelelő lemezen az EFI-t."""
        print("\n" + "-" * 50)
        print("🔧 BOOT LOADER (BCD) JAVÍTÁS")
        print("-" * 50)
        
        target_drive = target_drive.rstrip('\\') + '\\'
        target_letter = target_drive[0].upper()
        windows_path = os.path.join(target_drive, 'Windows')
        
        if not os.path.exists(windows_path):
            print(f"⚠️  Windows mappa nem található: {windows_path}")
            return False
        
        print(f"Cél Windows meghajtó: {target_drive}")
        
        # 1. Megkeressük melyik DISK-en van a Windows partíció
        print("A Windows meghajtó lemezének azonosítása...")
        
        disk_number = None
        efi_letter = None
        efi_partition = None
        
        try:
            # Volume-ok listázása
            res = self._run(['diskpart'], input='list volume\n', timeout=30)
            
            if res.returncode == 0 and res.stdout:
                lines = res.stdout.splitlines()
                target_volume = None
                
                # Windows volume keresése
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 3:
                        for i, p in enumerate(parts):
                            if p.upper() == target_letter and i >= 1:
                                try:
                                    target_volume = int(parts[1])
                                except (ValueError, IndexError):
                                    pass
                                break
                
                if target_volume is not None:
                    print(f"Windows volume: {target_volume}")
                    
                    # Disk azonosítása
                    res2 = self._run(['diskpart'], input=f'select volume {target_volume}\ndetail volume\n', timeout=30)
                    
                    if res2.returncode == 0 and res2.stdout:
                        for line in res2.stdout.splitlines():
                            if 'Disk' in line and '#' not in line:
                                parts = line.split()
                                for p in parts:
                                    if p.isdigit():
                                        disk_number = int(p)
                                        break
                                if disk_number is not None:
                                    break
                    
                    if disk_number is not None:
                        print(f"Lemez: Disk {disk_number}")
                        
                        # EFI partíció keresése ezen a lemezen
                        res3 = self._run(['diskpart'], input=f'select disk {disk_number}\nlist partition\n', timeout=30)
                        
                        if res3.returncode == 0 and res3.stdout:
                            for line in res3.stdout.splitlines():
                                line_upper = line.upper()
                                if 'SYSTEM' in line_upper or 'EFI' in line_upper:
                                    parts = line.split()
                                    for i, p in enumerate(parts):
                                        if p.isdigit() and i >= 1:
                                            efi_partition = int(p)
                                            break
                                    if efi_partition:
                                        break
                        
                        if efi_partition:
                            print(f"EFI partíció: Partition {efi_partition}")
                            
                            # Szabad betűjel keresése
                            used_letters = set()
                            for line in lines:
                                parts = line.split()
                                for p in parts:
                                    if len(p) == 1 and p.isalpha():
                                        used_letters.add(p.upper())
                            
                            free_letter = None
                            for c in 'STUVWXYZ':
                                if c not in used_letters:
                                    free_letter = c
                                    break
                            
                            if free_letter:
                                res4 = self._run(['diskpart'], 
                                    input=f'select disk {disk_number}\nselect partition {efi_partition}\nassign letter={free_letter}\n',
                                    timeout=30)
                                if res4.returncode == 0:
                                    efi_letter = free_letter + ':'
                                    print(f"EFI betűjel: {efi_letter}")
        except Exception as e:
            print(f"⚠️  Lemez azonosítási hiba: {e}")
        
        # 2. bcdboot futtatása
        success = False
        
        if efi_letter:
            print(f"bcdboot {target_drive}Windows /s {efi_letter} /f UEFI")
            res = self._run(['bcdboot', f'{target_drive}Windows', '/s', efi_letter, '/f', 'UEFI'])
            if res.returncode == 0:
                success = True
                print("✅ BCD sikeresen újraépítve (UEFI)!")
            else:
                print("⚠️  UEFI bcdboot hiba, fallback...")
            
            # EFI betűjel eltávolítása
            try:
                self._run(['diskpart'], 
                    input=f'select disk {disk_number}\nselect partition {efi_partition}\nremove letter={efi_letter[0]}\n',
                    timeout=30)
            except Exception as e:
                logging.debug(e)
        
        if not success:
            # Fallback: /s nélkül
            print(f"bcdboot {target_drive}Windows /f ALL")
            res = self._run(['bcdboot', f'{target_drive}Windows', '/f', 'ALL'])
            if res.returncode == 0:
                success = True
                print("✅ BCD sikeresen újraépítve (ALL)!")
            else:
                print(f"⚠️  bcdboot hiba (0x{res.returncode:X}), bootrec parancsok...")
        
        if not success:
            print("bootrec parancsok...")
            for cmd in ['/fixmbr', '/fixboot', '/rebuildbcd']:
                print(f"  bootrec {cmd}... ", end="", flush=True)
                res = self._run(['bootrec', cmd])
                print("✅" if res.returncode == 0 else "⚠️")
        
        print("-" * 50)
        print("✅ BCD javítás befejezve!")
        return True

    def extract_wim(self, wim_path, dest_folder):
        """WIM-ből gyári driverek kinyerése."""
        print("\n📀 WIM driver kinyerés...")
        print(f"   WIM: {wim_path}")
        print(f"   Cél: {dest_folder}")
        print("-" * 50)
        
        is_pe = os.environ.get('SystemDrive', 'C:') == 'X:'
        sys_temp = r'C:\DV_Temp' if is_pe else (os.environ.get('SystemDrive', 'C:') + '\\DV_Temp')
        mount_dir = os.path.join(sys_temp, f"WIM_Mount_Temp_{int(time.time())}")
        target_folder = os.path.join(dest_folder, f"Windows_Gyari_Alap_Driverek_{datetime.now().strftime('%Y%m%d_%H%M')}")
        
        if os.path.exists(mount_dir):
            shutil.rmtree(mount_dir, ignore_errors=True)
        os.makedirs(mount_dir, exist_ok=True)
        os.makedirs(target_folder, exist_ok=True)
        
        try:
            print("1/3 WIM csatolása (ez 3-5 perc)...")
            res = self._run(["dism", "/Mount-Image", f"/ImageFile:{wim_path}", "/Index:1", f"/MountDir:{mount_dir}", "/ReadOnly"])
            if res.returncode != 0:
                raise Exception(f"Mount hiba: {res.stderr}")
            
            print("2/3 FileRepository + INF másolása...")
            driverstore = os.path.join(mount_dir, "Windows", "System32", "DriverStore", "FileRepository")
            target_repo = os.path.join(target_folder, "FileRepository")
            if os.path.exists(driverstore):
                shutil.copytree(driverstore, target_repo, dirs_exist_ok=True)
            
            inf_dir = os.path.join(mount_dir, "Windows", "INF")
            target_inf = os.path.join(target_folder, "INF")
            if os.path.exists(inf_dir):
                shutil.copytree(inf_dir, target_inf, dirs_exist_ok=True)
            
            print("3/3 WIM leválasztása...")
            self._run(["dism", "/Unmount-Image", f"/MountDir:{mount_dir}", "/Discard"])
            self._run(["dism", "/Cleanup-Wim"])
            shutil.rmtree(mount_dir, ignore_errors=True)
            
            print("-" * 50)
            print(f"✅ Gyári driverek kimentve: {target_folder}")
            return target_folder
            
        except Exception as e:
            print(f"❌ Hiba: {e}")
            self._run(["dism", "/Unmount-Image", f"/MountDir:{mount_dir}", "/Discard"])
            self._run(["dism", "/Cleanup-Wim"])
            shutil.rmtree(mount_dir, ignore_errors=True)
            return None
    
    def create_restore_point(self):
        """Visszaállítási pont létrehozása."""
        if self.target_os_path:
            print("\n❌ Hiba: Visszaállítási pont csak Élő rendszeren készíthető!")
            return False
            
        desc = f"DriverVarázsló_Backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        print("\n🛡️  Visszaállítási pont létrehozása...")
        print(f"   Név: {desc}")
        print("-" * 50)
        
        # Enable System Restore
        print("1/2 Rendszervédelem engedélyezése...")
        self._run(["powershell", "-NoProfile", "-Command", 'Enable-ComputerRestore -Drive "$($env:SystemDrive)\\" -ErrorAction SilentlyContinue'])
        self._run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore',
                   '/v', 'SystemRestorePointCreationFrequency', '/t', 'REG_DWORD', '/d', '0', '/f'])
        
        # Create restore point
        print("2/2 Visszaállítási pont létrehozása...")
        ps_cmd = f'Checkpoint-Computer -Description "{desc}" -RestorePointType "MODIFY_SETTINGS" -ErrorAction Stop'
        res = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd], encoding='utf-8')
        
        if res.returncode == 0:
            print("✅ Visszaállítási pont létrehozva!")
            return True
        else:
            print(f"❌ Hiba: {res.stderr[:200] if res.stderr else 'Ismeretlen hiba'}")
            return False
    
    def repair_bcd_standalone_cli(self):
        """Önálló BCD javítás CLI módban."""
        print("\n🔧 BCD BOOT HIBA JAVÍTÁSA")
        print("-" * 50)
        
        target = self.target_os_path
        if not target:
            target = input("Add meg a HALOTT Windows meghajtóját (pl: D:\\): ").strip()
            
        if not target:
            print("❌ Nincs meghajtó megadva!")
            return False
        
        target = target.rstrip('\\') + '\\'
        windows_path = os.path.join(target, 'Windows')
        
        if not os.path.exists(windows_path):
            print(f"❌ Windows mappa nem található: {windows_path}")
            return False
        
        return self._repair_bcd_cli(target)
    
    # ================================================================
    # WINDOWS UPDATE
    # ================================================================
    def check_wu_status_cli(self):
        """WU driver frissítés állapota."""
        policy_disabled = False
        search_disabled = False
        
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate", 0, winreg.KEY_READ) as key:
                val, _ = winreg.QueryValueEx(key, "ExcludeWUDriversInQualityUpdate")
                if val == 1:
                    policy_disabled = True
        except (FileNotFoundError, OSError):
            pass
        
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching", 0, winreg.KEY_READ) as key:
                val, _ = winreg.QueryValueEx(key, "SearchOrderConfig")
                if val == 0:
                    search_disabled = True
        except (FileNotFoundError, OSError):
            pass
        
        drv_status = "✅ ENGEDÉLYEZVE"
        if policy_disabled and search_disabled:
            drv_status = "⛔ LETILTVA (policy + eszközbeállítások)"
        elif policy_disabled:
            drv_status = "⛔ LETILTVA (policy)"
        elif search_disabled:
            drv_status = "⛔ LETILTVA (eszközbeállítások)"
            
        paused_until = None
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\WindowsUpdate\UX\Settings", 0, winreg.KEY_READ) as key:
                val, _ = winreg.QueryValueEx(key, "PauseUpdatesExpiryTime")
                if val:
                    dt = datetime.strptime(val, "%Y-%m-%dT%H:%M:%SZ")
                    if dt > datetime.now(timezone.utc).replace(tzinfo=None):
                        paused_until = val.split('T')[0] if 'T' in val else val
        except Exception:
            pass
            
        if paused_until:
            return f"SZÜNETELTETVE ({paused_until}) | Driverek: {drv_status}"
        return drv_status
    
    def disable_wu_drivers(self):
        """WU driver frissítések letiltása."""
        if self.target_os_path:
            print("\n❌ Hiba: A Windows Update beállítások csak Élő rendszeren módosíthatók!")
            return
            
        print("\n⛔ WU driver frissítések letiltása...")
        print("-" * 50)
        
        try:
            with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching", 0, winreg.KEY_WRITE) as key:
                winreg.SetValueEx(key, "SearchOrderConfig", 0, winreg.REG_DWORD, 0)
            print("  ✅ SearchOrderConfig = 0")
        except Exception as e:
            print(f"  ⚠️  {e}")
        
        self._run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching',
                   '/v', 'SearchOrderConfig', '/t', 'REG_DWORD', '/d', '0', '/f'])
        
        self._run(['reg', 'add', r'HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate',
                   '/v', 'ExcludeWUDriversInQualityUpdate', '/t', 'REG_DWORD', '/d', '1', '/f'])
        print("  ✅ ExcludeWUDriversInQualityUpdate = 1")
        
        print("  🗑️  Beragadt frissítések törlése (SoftwareDistribution)...")
        clear_cache = r"""
        Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
        Stop-Service bits -Force -ErrorAction SilentlyContinue
        Stop-Service cryptsvc -Force -ErrorAction SilentlyContinue
        Remove-Item -Path "$env:windir\SoftwareDistribution" -Recurse -Force -ErrorAction SilentlyContinue
        """
        self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", clear_cache])

        print("  🔄 WU szolgáltatás újraindítása...")
        self._run('net start wuauserv', shell=True)
        
        print("-" * 50)
        print("✅ WU driver letiltás kész (Cache ürítve)!")
    
    def enable_wu_drivers(self):
        """WU driver frissítések engedélyezése + teljes reset."""
        if self.target_os_path:
            print("\n❌ Hiba: A Windows Update beállítások csak Élő rendszeren módosíthatók!")
            return
            
        print("\n✅ WU driver frissítések engedélyezése + reset...")
        print("-" * 50)
        
        # Szüneteltetés (Pause) feloldása a registry-ből
        ps_resume = """
        $regPath = 'HKLM:\\SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings'
        Remove-ItemProperty -Path $regPath -Name 'PauseUpdatesExpiryTime' -ErrorAction SilentlyContinue
        Remove-ItemProperty -Path $regPath -Name 'PauseFeatureUpdatesEndTime' -ErrorAction SilentlyContinue
        Remove-ItemProperty -Path $regPath -Name 'PauseQualityUpdatesEndTime' -ErrorAction SilentlyContinue
        Remove-ItemProperty -Path $regPath -Name 'PauseUpdatesStartTime' -ErrorAction SilentlyContinue
        Remove-ItemProperty -Path $regPath -Name 'PauseFeatureUpdatesStartTime' -ErrorAction SilentlyContinue
        Remove-ItemProperty -Path $regPath -Name 'PauseQualityUpdatesStartTime' -ErrorAction SilentlyContinue
        """
        self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_resume])
        print("  ✅ Szüneteltetés feloldva")

        # Policy törlés
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate", 0, winreg.KEY_WRITE) as key:
                winreg.DeleteValue(key, "ExcludeWUDriversInQualityUpdate")
            print("  ✅ Policy törölve")
        except FileNotFoundError:
            print("  ℹ️  Policy nem létezett")
        except Exception as e:
            print(f"  ⚠️  {e}")
        
        # SearchOrderConfig = 1
        try:
            with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching", 0, winreg.KEY_WRITE) as key:
                winreg.SetValueEx(key, "SearchOrderConfig", 0, winreg.REG_DWORD, 1)
            print("  ✅ SearchOrderConfig = 1")
        except Exception as e:
            print(f"  ⚠️  {e}")
        
        # Szolgáltatások
        print("  🔄 WU szolgáltatások újraindítása...")
        for svc in ['wuauserv', 'bits', 'cryptsvc']:
            self._run(f'net stop {svc} /y', shell=True)
        time.sleep(2)
        
        # SoftwareDistribution törlés
        sysroot = os.environ.get('SYSTEMROOT', r'C:\Windows')
        sw_dist = os.path.join(sysroot, 'SoftwareDistribution')
        if os.path.exists(sw_dist):
            print("  🗑️  SoftwareDistribution törlése...")
            shutil.rmtree(sw_dist, ignore_errors=True)

        # Rename catroot2 (a GUI verzióval egyező mély reset - enélkül a WU komponensraktár
        # korrupciója gyakran nem javul, csak a friss frissítés-cache törlésétől)
        catroot2 = os.path.join(sysroot, 'System32', 'catroot2')
        bak = catroot2 + '.bak'
        try:
            if os.path.exists(bak):
                shutil.rmtree(bak, ignore_errors=True)
            if os.path.exists(catroot2):
                os.rename(catroot2, bak)
                print("  ✅ catroot2 átnevezve")
        except Exception as e:
            print(f"  ⚠️  catroot2: {e}")

        # WU DLL-ek újraregisztrálása
        sys32 = os.path.join(sysroot, 'System32')
        for dll in ['wuaueng.dll', 'wuapi.dll', 'wups.dll', 'wups2.dll', 'wuwebv.dll', 'wucltux.dll']:
            fp = os.path.join(sys32, dll)
            if os.path.exists(fp):
                self._run(f'regsvr32.exe /s "{fp}"', shell=True)
        print("  ✅ WU DLL-ek újraregisztrálva")

        # Winsock reset
        self._run('netsh winsock reset', shell=True)

        for svc in ['cryptsvc', 'bits', 'wuauserv']:
            self._run(f'net start {svc}', shell=True)

        self._run('wuauclt.exe /resetauthorization /detectnow', shell=True)
        self._run('UsoClient.exe StartScan', shell=True)

        print("-" * 50)
        print("✅ WU engedélyezés + reset kész!")
    
    def restart_wu_services(self):
        """WU szolgáltatások újraindítása."""
        if self.target_os_path:
            print("\n❌ Hiba: A Windows Update beállítások csak Élő rendszeren módosíthatók!")
            return
            
        print("\n🔄 WU szolgáltatások újraindítása...")
        print("-" * 50)
        
        for svc in ['wuauserv', 'bits', 'cryptsvc', 'msiserver']:
            print(f"  stop {svc}...", end=" ", flush=True)
            self._run(f'net stop {svc} /y', shell=True)
            print("✅")
        
        time.sleep(2)
        
        for svc in ['rpcss', 'cryptsvc', 'bits', 'msiserver', 'wuauserv']:
            print(f"  start {svc}...", end=" ", flush=True)
            self._run(f'net start {svc}', shell=True)
            print("✅")
        
        self._run('wuauclt.exe /resetauthorization /detectnow', shell=True)
        self._run('UsoClient.exe StartScan', shell=True)

        print("-" * 50)
        print("✅ WU szolgáltatások újraindítva!")

    def pause_wu(self, days):
        """Windows Update szüneteltetése N napra (a GUI verzió CLI megfelelője)."""
        if self.target_os_path:
            print("\n❌ Hiba: Offline módban nem elérhető!")
            return

        print(f"\n⏸️  WU szüneteltetése ({days} nap)...")
        print("-" * 50)

        ps = """
        $regPath = 'HKLM:\\SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings'
        if (!(Test-Path $regPath)) { New-Item -Path $regPath -Force | Out-Null }

        $daysToAdd = """ + str(days) + """
        $now = (Get-Date).ToUniversalTime()

        $currentPauseStr = (Get-ItemProperty -Path $regPath -Name 'PauseUpdatesExpiryTime' -ErrorAction SilentlyContinue).PauseUpdatesExpiryTime

        if ($currentPauseStr -and $daysToAdd -eq 7) {
            try {
                $currentPause = [datetime]$currentPauseStr
                if ($currentPause -lt $now) { $currentPause = $now }
            } catch {
                $currentPause = $now
            }
            $newDate = $currentPause.AddDays($daysToAdd)
        } else {
            $newDate = $now.AddDays($daysToAdd)
        }

        $dateStr = $newDate.ToString("yyyy-MM-ddTHH:mm:ssZ")
        $startStr = $now.ToString("yyyy-MM-ddTHH:mm:ssZ")

        Set-ItemProperty -Path $regPath -Name 'PauseUpdatesExpiryTime' -Value $dateStr -Type String -Force | Out-Null
        Set-ItemProperty -Path $regPath -Name 'PauseFeatureUpdatesEndTime' -Value $dateStr -Type String -Force | Out-Null
        Set-ItemProperty -Path $regPath -Name 'PauseQualityUpdatesEndTime' -Value $dateStr -Type String -Force | Out-Null
        Set-ItemProperty -Path $regPath -Name 'PauseUpdatesStartTime' -Value $startStr -Type String -Force | Out-Null
        Set-ItemProperty -Path $regPath -Name 'PauseFeatureUpdatesStartTime' -Value $startStr -Type String -Force | Out-Null
        Set-ItemProperty -Path $regPath -Name 'PauseQualityUpdatesStartTime' -Value $startStr -Type String -Force | Out-Null

        Write-Output $dateStr
        """
        res = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], encoding='utf-8')
        new_date = res.stdout.strip()

        print("  Szolgáltatások leállítása és újraindítási jelzések törlése...")
        stop_svc = r"""
        Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
        Stop-Service bits -Force -ErrorAction SilentlyContinue
        Stop-Service cryptsvc -Force -ErrorAction SilentlyContinue
        Stop-Service UsoSvc -Force -ErrorAction SilentlyContinue
        """
        self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", stop_svc])
        time.sleep(2)
        self._run(['reg', 'delete', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired', '/f'])

        print("  Beragadt frissítések és WU gyorsítótár ürítése...")
        sysroot = os.environ.get('SYSTEMROOT', r'C:\Windows')
        sw_dist = os.path.join(sysroot, 'SoftwareDistribution')
        for _ in range(4):
            try:
                if os.path.exists(sw_dist):
                    shutil.rmtree(sw_dist, ignore_errors=False)
                    break
                else:
                    break
            except Exception:
                time.sleep(3)
        self._run(["powershell", "-NoProfile", "-Command", f'Remove-Item -Path "{sw_dist}" -Recurse -Force -ErrorAction SilentlyContinue'])
        self._run('net start wuauserv', shell=True)

        print("-" * 50)
        print(f"✅ Frissítések szüneteltetve idáig: {new_date}")

    def resume_wu(self):
        """Windows Update szüneteltetésének feloldása (a GUI verzió CLI megfelelője)."""
        if self.target_os_path:
            print("\n❌ Hiba: Offline módban nem elérhető!")
            return

        print("\n▶️  WU szüneteltetés feloldása...")
        print("-" * 50)

        ps = """
        $regPath = 'HKLM:\\SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings'
        Remove-ItemProperty -Path $regPath -Name 'PauseUpdatesExpiryTime' -ErrorAction SilentlyContinue
        Remove-ItemProperty -Path $regPath -Name 'PauseFeatureUpdatesEndTime' -ErrorAction SilentlyContinue
        Remove-ItemProperty -Path $regPath -Name 'PauseQualityUpdatesEndTime' -ErrorAction SilentlyContinue
        Remove-ItemProperty -Path $regPath -Name 'PauseUpdatesStartTime' -ErrorAction SilentlyContinue
        Remove-ItemProperty -Path $regPath -Name 'PauseFeatureUpdatesStartTime' -ErrorAction SilentlyContinue
        Remove-ItemProperty -Path $regPath -Name 'PauseQualityUpdatesStartTime' -ErrorAction SilentlyContinue

        Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
        Start-Service wuauserv -ErrorAction SilentlyContinue

        try { (New-Object -ComObject Microsoft.Update.AutoUpdate).Resume() | Out-Null } catch {}
        """
        self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps])

        print("-" * 50)
        print("✅ Szüneteltetés feloldva!")

    def delete_ghost_devices(self):
        """Nem csatlakoztatott (szellem) eszközök törlése (a GUI verzió CLI megfelelője)."""
        if self.target_os_path:
            print("\n❌ Hiba: Ez a funkció csak Élő (Online) rendszeren működik!")
            return

        print("\n👻 Szellemeszközök keresése és törlése...")
        print("-" * 50)

        ps_script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ghosts = Get-PnpDevice -PresentOnly:$false | Where-Object { $_.Present -eq $false -and $_.InstanceId -ne $null -and $_.PNPClass -ne 'SoftwareDevice' -and $_.PNPClass -ne 'Net' -and $_.PNPClass -ne 'System' }
$count = 0
$total = @($ghosts).Count
if ($total -eq 0) {
    Write-Output "DONE: Nincs szellemeszköz a rendszerben."
    exit
}
Write-Output "TOTAL: $total"
foreach ($dev in $ghosts) {
    $id = $dev.PNPDeviceID
    $name = $dev.Name
    if (-not $name) { $name = "Ismeretlen eszköz" }
    Write-Output "RM: $name"
    $res = & pnputil /remove-device "$($id)" 2>&1
    if ($LASTEXITCODE -eq 0 -or $res -match "deleted" -or $res -match "törölve" -or $res -match "successfully") {
        Write-Output "OK: $name"
        $count++
    } else {
        Write-Output "FAIL: $name"
    }
}
Write-Output "DONE: Törölve: $count / $total"
"""
        res = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script], encoding='utf-8')
        success = 0
        total = 0
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("TOTAL:"):
                m = re.search(r'TOTAL:\s*(\d+)', line)
                if m:
                    total = int(m.group(1))
                print(f"Összesen {total} db szellemeszköz azonosítva...")
            elif line.startswith("RM:"):
                print(f"  🗑 Próbálkozás: {line[3:].strip()}...", end=" ", flush=True)
            elif line.startswith("OK:"):
                success += 1
                print("✅")
            elif line.startswith("FAIL:"):
                print("❌ (valószínűleg védett eszköz)")
            elif line.startswith("DONE:"):
                print(line[5:].strip())

        print("-" * 50)
        print(f"✅ Szellemeszközök törlése kész! Törölve: {success} / {total}")
        return success, total

    # ================================================================
    # TEMP FÁJLOK TÖRLÉSE (lemez felszabadítás) - a GUI clean_temp_files
    # megfelelője, szinkron kiírással, a modul-szintű _clean_folder_contents /
    # _fmt_bytes / _empty_recycle_bin segédfüggvényeket a GUI-verzióval megosztva.
    # ================================================================
    def clean_temp_files(self, thumbnail_cache=False, recycle_bin=False, **overrides):
        """Windows ideiglenes fájlok törlése (mint a GUI Temp Törlés funkciója). Csak élő
        (online) rendszeren fut - ua. az indoklás, mint a szellemeszköz-törlésnél.
        overrides: a _temp_clean_category_defs kulcsai szerint felülírható, hogy melyik
        kategória fusson (alapértelmezésben a defs-ben megjelölt 3 "alapból bepipálva"
        kategória fut - user_temp/windows_temp/wu_cache)."""
        if self.target_os_path:
            print("\n❌ Hiba: Ez a funkció csak Élő (Online) rendszeren működik!")
            return

        folder_categories = []
        for key, label, paths, services, default_checked in _temp_clean_category_defs(self.sys_drive):
            if overrides.get(key, default_checked) and paths:
                folder_categories.append((label, paths, services))

        if not folder_categories and not thumbnail_cache and not recycle_bin:
            print("\n⚠️ Nincs kiválasztva egyetlen törlendő kategória sem!")
            return

        print("\n🧹 Temp fájlok törlése...")
        print("-" * 50)
        total_freed = 0
        total_removed = 0
        total_failed = 0

        services_to_stop = sorted({s for _, _, services in folder_categories for s in services})
        if services_to_stop:
            print(f"⏸️ Szolgáltatások leállítása a cache törléséhez ({', '.join(services_to_stop)})...")
            self._run(['powershell', '-NoProfile', '-Command', f'Stop-Service {",".join(services_to_stop)} -Force -ErrorAction SilentlyContinue'])

        for label, paths, _services in folder_categories:
            cat_freed = cat_removed = cat_failed = 0
            for path in paths:
                print(f"{label} törlése ({path})...", end=" ", flush=True)
                freed, removed, failed = _clean_folder_contents(path)
                print(f"✅ {removed} elem törölve, {failed} kihagyva ({_fmt_bytes(freed)} felszabadítva).")
                cat_freed += freed
                cat_removed += removed
                cat_failed += failed
            total_freed += cat_freed
            total_removed += cat_removed
            total_failed += cat_failed

        if services_to_stop:
            print("▶️ Szolgáltatások újraindítása...")
            self._run(['powershell', '-NoProfile', '-Command', f'Start-Service {",".join(services_to_stop)} -ErrorAction SilentlyContinue'])

        if thumbnail_cache:
            print("🖼️ Miniatűr (thumbnail) gyorsítótár törlése...", end=" ", flush=True)
            freed = removed = failed = 0
            local = os.environ.get('LOCALAPPDATA')
            explorer_dir = os.path.join(local, 'Microsoft', 'Windows', 'Explorer') if local else None
            if explorer_dir and os.path.isdir(explorer_dir):
                for name in os.listdir(explorer_dir):
                    if not (name.startswith('thumbcache_') or name.startswith('iconcache_')):
                        continue
                    full = os.path.join(explorer_dir, name)
                    try:
                        size = os.path.getsize(full)
                        os.remove(full)
                        freed += size
                        removed += 1
                    except Exception:
                        failed += 1
            print(f"✅ {removed} fájl törölve, {failed} kihagyva ({_fmt_bytes(freed)} felszabadítva).")
            total_freed += freed
            total_removed += removed
            total_failed += failed

        if recycle_bin:
            print("🗑️ Lomtár ürítése...", end=" ", flush=True)
            rb_freed = _empty_recycle_bin()
            print(f"✅ Kiürítve ({_fmt_bytes(rb_freed)} felszabadítva).")
            total_freed += rb_freed
            total_removed += 1

        print("-" * 50)
        print(f"🧹 Kész! Összesen {total_removed} elem törölve, {total_failed} kihagyva. Felszabadított hely: {_fmt_bytes(total_freed)}")
        return total_freed, total_removed, total_failed

    # ================================================================
    # NET BLOKKOLÓ SCRIPT (block.bat) LETÖLTÉSE - a GUI download_block_script
    # megfelelője, a modul-szintű _download_block_script-et megosztva vele.
    # ================================================================
    def download_block_script(self):
        """Letölti a block.bat scriptet a C:\\DriverVarazslo mappába (csak letöltés,
        futtatás nélkül)."""
        print("\n🚫 Net Blokkoló script (block.bat) letöltése...")
        try:
            path = _download_block_script(self._run)
            print(f"✅ Letöltve: {path}")
            print("   (A script futtatáskor a SAJÁT mappájában és almappáiban lévő összes")
            print("   .exe kimenő internet-elérését letiltja a Windows tűzfalban - másold")
            print("   abba a mappába, amit blokkolni akarsz, és dupla kattintás: az admin")
            print("   jogot magától kéri (UAC), csak el kell fogadni.)")
        except Exception as e:
            print(f"❌ Letöltési hiba: {e}")

    # ================================================================
    # AUTOFIX (1 kattintásos driver fix)
    # ================================================================
    def autofix(self):
        """Teljes automatikus driver fix (mint a GUI-ban)."""
        if self.target_os_path:
            print("\n❌ Hiba: Az 1 Kattintásos Driver Fix (Autofix) csak Élő (Online) rendszeren futtatható!")
            return
            
        print("\n" + "=" * 60)
        print("  ⚡ 1 KATTINTÁSOS AUTOMATIKUS DRIVER FIX")
        print("=" * 60)
        print("""
Lépések:
  0️⃣  Alvó mód és Gyors Rendszerindítás kikapcsolása
  1️⃣  Visszaállítási pont létrehozása
  2️⃣  Windows Update driver keresés LETILTÁSA
  3️⃣  Szellemeszközök törlése
  4️⃣  Összes third-party driver TÖRLÉSE
  5️⃣  Hardver újraszkennelés
  6️⃣  WU driver telepítés (friss driverek)
  7️⃣  Újraindítás

Megjegyzés: ez az egymenetes CLI változat - a GUI verzióval ellentétben nem
iktat be automatikus újraindítás(oka)t a törlés és az újratelepítés közé,
ezért ha egy driver csak egy közbenső reboot után enumerálódik újra, azt
manuálisan kell majd újraszkennelni (Driverek kezelése > Hardver újraszkennelés).
""")

        confirm = input("Biztosan elindítod? (igen/nem): ").strip().lower()
        if confirm not in ['igen', 'i', 'yes', 'y']:
            print("❌ Megszakítva.")
            return

        start_time = time.time()

        # FÁZIS 0: Alvó mód + Fast Startup letiltása
        print("\n" + "=" * 50)
        print("  FÁZIS 0: Alvó mód és Gyors Rendszerindítás kikapcsolása")
        print("=" * 50)
        power_cmds = [
            ['powercfg', '/change', 'monitor-timeout-ac', '0'],
            ['powercfg', '/change', 'monitor-timeout-dc', '0'],
            ['powercfg', '/change', 'standby-timeout-ac', '0'],
            ['powercfg', '/change', 'standby-timeout-dc', '0'],
            ['powercfg', '/change', 'hibernate-timeout-ac', '0'],
            ['powercfg', '/change', 'hibernate-timeout-dc', '0']
        ]
        for cmd in power_cmds:
            self._run(cmd)
        self._run(["powercfg", "/h", "off"])
        print("  ✅ Energiagazdálkodás beállítva, Gyors Rendszerindítás kikapcsolva.")

        # FÁZIS 1: Visszaállítási pont
        print("\n" + "=" * 50)
        print("  FÁZIS 1: Visszaállítási pont létrehozása")
        print("=" * 50)
        self.create_restore_point()

        # FÁZIS 2: WU letiltás
        print("\n" + "=" * 50)
        print("  FÁZIS 2: WU driver letiltás")
        print("=" * 50)
        self.disable_wu_drivers()

        # FÁZIS 3: Szellemeszközök törlése
        print("\n" + "=" * 50)
        print("  FÁZIS 3: Szellemeszközök törlése")
        print("=" * 50)
        self.delete_ghost_devices()

        # FÁZIS 4: Third-party driverek törlése
        print("\n" + "=" * 50)
        print("  FÁZIS 4: Third-party driverek törlése")
        print("=" * 50)
        drivers = self.get_third_party_drivers()
        if drivers:
            print(f"Talált: {len(drivers)} db third-party driver")
            self.delete_drivers(drivers, reboot=False)
        else:
            print("Nincs third-party driver.")

        # FÁZIS 5: Hardver scan
        print("\n" + "=" * 50)
        print("  FÁZIS 5: Hardver újraszkennelés")
        print("=" * 50)
        print("🔄 pnputil /scan-devices...")
        self._run(['pnputil', '/scan-devices'])
        time.sleep(5)
        print("✅ Kész!")

        # FÁZIS 6: WU driver telepítés
        print("\n" + "=" * 50)
        print("  FÁZIS 6: WU driver telepítés")
        print("=" * 50)
        print("🔄 Driver frissítések keresése és telepítése...")
        print("   (Ez akár 5-10 percig is tarthat)")
        
        # A telepítő script a KÖZÖS _build_wu_install_ps-ből jön - ugyanaz, mint a GUI-s
        # manuális telepítésnél és AutoFixnél; itt a gép összes jelenlévő eszközéhez
        # párosít a scripten belül (a CLI-ben nincs Python-oldali előszűrés).
        ps_script = _build_wu_install_ps(match_system_devices=True)
        logging.debug(f"[CMD] Popen futtatása: {ps_script[:300]}...")
        process = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
            startupinfo=self._si, creationflags=self._nw)
        
        install_success = 0
        install_fail = 0
        
        # A közös script kimeneti protokollja (lásd _build_wu_install_ps docstring).
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            if line.startswith("FOUND:"):
                print(f"  📦 {line[6:].strip()}")
            elif line.startswith("TOTAL:"):
                print(f"\n  Összesen {line[6:].strip()} driver telepítése...")
            elif line.startswith("DLONE:"):
                print(f"  ⬇ {line[6:].strip()}")
            elif line.startswith("INSTONE:"):
                print(f"  ⚙ {line[8:].strip()}")
            elif line.startswith("OK:"):
                install_success += 1
                print(f"  ✅ {line[3:].strip()}")
            elif line.startswith("FAIL:"):
                install_fail += 1
                print(f"  ❌ {line[5:].strip()}")
            elif line.startswith("EMPTY:"):
                print(f"  ℹ️  {line[6:].strip()}")
            elif line.startswith("ERROR:"):
                print(f"  ❌ HIBA: {line[6:].strip()}")
            elif line.startswith("DONE:"):
                print(f"\n  Telepítés kész: ✅ {install_success} sikeres, ❌ {install_fail} sikertelen")
            elif line.startswith("INIT:") or line.startswith("SEARCH:") or line.startswith("SKIP:"):
                pass  # csendes protokoll-sorok
        
        process.wait()
        
        if install_success > 0:
            print("\n🔄 Eszközök újraszkennelése...")
            self._run(['pnputil', '/scan-devices'])
        
        # Összegzés
        elapsed = int(time.time() - start_time)
        print("\n" + "=" * 60)
        print(f"  ⚡ AUTOFIX KÉSZ! (Idő: {elapsed // 60} perc {elapsed % 60} mp)")
        print("=" * 60)
        
        # FÁZIS 7: Újraindítás
        if install_success > 0 or len(drivers) > 0:
            print("\n🔄 Újraindítás 30 másodperc múlva...")
            print("   (Ctrl+C a megszakításhoz)")
            try:
                for i in range(30, 0, -1):
                    print(f"\r   {i} másodperc...", end="", flush=True)
                    time.sleep(1)
                print("\n🔄 Újraindítás MOST!")
                self._run(['shutdown', '/r', '/t', '0', '/f'])
            except KeyboardInterrupt:
                print("\n❌ Újraindítás megszakítva.")
        else:
            print("\nNem történt változás - újraindítás nem szükséges.")


def run_cli_mode():
    """Parancssoros mód - TELJES funkcionalitás (GUI tükör)."""
    api = CliApi()
    
    def clear_screen():
        os.system('cls' if os.name == 'nt' else 'clear')
    
    def print_header():
        clear_screen()
        print("=" * 60)
        print("  ♻️  DRIVERVARÁZSLÓ - CLI MÓD")
        print("  🖥️  Tiszta rendszer (Build " + str(BUILD_NUMBER) + ")")
        print("=" * 60)
        if api.target_os_path:
            print(f"  📌 Offline mód: {api.target_os_path}")
        else:
            print("  📌 Jelenlegi rendszer (online)")
        print("=" * 60)
    
    def main_menu():
        print("""
  FŐMENÜ - Válassz kategóriát:

    💿  1. Driverek kezelése
    💾  2. Mentés és Visszaállítás
    🔄  3. Windows Update
    ⚡  4. 1 Kattintásos Driver Fix
    🧹  5. Temp fájlok törlése (lemez felszabadítás)
    🚫  6. Net Blokkoló script (block.bat) letöltése

    ⚙️   7. Cél OS váltása (offline mód)
    ℹ️   8. GUI-only funkciók (mik nem érhetők el itt)
    ❌  0. Kilépés
""")
    
    def drivers_menu():
        while True:
            print_header()
            print("""
  💿 DRIVEREK KEZELÉSE

    1. Third-party driverek listázása
    2. ÖSSZES driver listázása (veszélyes!)
    3. Driver(ek) törlése
    4. Hardver újraszkennelés
    5. Szellemeszközök (ghost device) törlése

    0. Vissza a főmenübe
""")
            choice = input("Választás: ").strip()
            
            if choice == '0':
                break
            elif choice == '1':
                drivers = api.list_drivers(all_drivers=False)
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '2':
                drivers = api.list_drivers(all_drivers=True)
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '3':
                all_mode = input("Összes driver mód? (i/n): ").strip().lower() == 'i'
                drivers = api.list_drivers(all_drivers=all_mode)
                if not drivers:
                    input("\nNyomj ENTER-t a folytatáshoz...")
                    continue
                
                sel = input("\nTörlendő sorszámok (pl: 1,3,5 vagy 'mind'): ").strip()
                if sel.lower() == 'mind':
                    to_delete = drivers
                else:
                    indices = [int(x.strip())-1 for x in sel.split(',') if x.strip().isdigit()]
                    to_delete = [drivers[i] for i in indices if 0 <= i < len(drivers)]
                
                if to_delete:
                    reboot = input("Törlés után újraindítás? (i/n): ").strip().lower() == 'i'
                    confirm = input(f"Biztosan törölsz {len(to_delete)} drivert? (i/n): ").strip().lower()
                    if confirm == 'i':
                        api.delete_drivers(to_delete, list_all=all_mode, reboot=reboot)
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '4':
                if api.target_os_path:
                    print("❌ Offline módban nem elérhető!")
                else:
                    print("🔄 Hardver újraszkennelés...")
                    api._run(['pnputil', '/scan-devices'])
                    time.sleep(2)
                    print("✅ Kész!")
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '5':
                api.delete_ghost_devices()
                input("\nNyomj ENTER-t a folytatáshoz...")

    def backup_menu():
        while True:
            print_header()
            print("""
  💾 MENTÉS ÉS VISSZAÁLLÍTÁS

    1. Third-party driverek mentése
    2. ÖSSZES driver mentése (OEM + inbox)
    3. Lementett driverek visszaállítása
    4. WIM-ből gyári driverek kinyerése
    5. Visszaállítási pont létrehozása
    6. BCD boot hiba javítása
    
    0. Vissza a főmenübe
""")
            choice = input("Választás: ").strip()
            
            if choice == '0':
                break
            elif choice == '1':
                dest = input("Mentés célmappája: ").strip()
                if dest:
                    api.backup_third_party(dest)
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '2':
                dest = input("Mentés célmappája: ").strip()
                if dest:
                    api.backup_all(dest)
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '3':
                source = input("Lementett driver mappa: ").strip()
                if source:
                    online = input("Online mód (jelenlegi rendszer)? (i/n): ").strip().lower() == 'i'
                    api.restore_drivers(source, online=online)
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '4':
                wim = input("install.wim fájl elérési útja: ").strip()
                dest = input("Kinyerés célmappája: ").strip()
                if wim and dest:
                    api.extract_wim(wim, dest)
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '5':
                if api.target_os_path:
                    print("❌ Offline módban nem elérhető!")
                else:
                    api.create_restore_point()
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '6':
                api.repair_bcd_standalone_cli()
                input("\nNyomj ENTER-t a folytatáshoz...")
    
    def wu_menu():
        while True:
            print_header()
            status = api.check_wu_status_cli()
            print(f"""
  🔄 WINDOWS UPDATE BEÁLLÍTÁSOK
  
  Jelenlegi állapot: {status}

    1. WU driver letiltás
    2. WU driver engedélyezés + reset
    3. WU szolgáltatások újraindítása
    4. WU szüneteltetése (N napra)
    5. WU szüneteltetés feloldása

    0. Vissza a főmenübe
""")
            choice = input("Választás: ").strip()

            if choice == '0':
                break
            elif choice == '1':
                api.disable_wu_drivers()
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '2':
                api.enable_wu_drivers()
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '3':
                api.restart_wu_services()
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '4':
                days_str = input("Hány napra szüneteltessük (pl: 7)? ").strip()
                days = int(days_str) if days_str.isdigit() else 7
                api.pause_wu(days)
                input("\nNyomj ENTER-t a folytatáshoz...")
            elif choice == '5':
                api.resume_wu()
                input("\nNyomj ENTER-t a folytatáshoz...")
    
    def target_menu():
        print("\n⚙️  CÉL OS VÁLTÁSA")
        print("-" * 40)
        print("Jelenlegi:", api.target_os_path or "Jelenlegi rendszer (online)")
        print()
        path = input("Új cél OS path (üres = visszaállítás jelenlegire): ").strip()
        
        if not path:
            api.target_os_path = None
            print("✅ Visszaállítva: jelenlegi rendszer")
        elif os.path.isdir(os.path.join(path, 'Windows')):
            api.target_os_path = path
            print(f"✅ Cél OS: {api.target_os_path}")
        else:
            print(f"❌ Nem található Windows mappa: {path}")
        
        input("\nNyomj ENTER-t a folytatáshoz...")
    
    # FŐCIKLUS
    while True:
        print_header()
        main_menu()
        
        try:
            choice = input("Választás: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        
        if choice == '0':
            print("\nViszlát! 👋")
            break
        elif choice == '1':
            drivers_menu()
        elif choice == '2':
            backup_menu()
        elif choice == '3':
            wu_menu()
        elif choice == '4':
            print_header()
            api.autofix()
            input("\nNyomj ENTER-t a folytatáshoz...")
        elif choice == '5':
            print_header()
            extra1 = input("Miniatűr (thumbnail) gyorsítótár törlése is? (i/n): ").strip().lower() == 'i'
            extra2 = input("Lomtár (Recycle Bin) ürítése is? (i/n): ").strip().lower() == 'i'
            extra3 = input("Egyéb extra kategóriák is (Delivery Optimization, hibajelentések, DirectX Shader Cache, CBS logok, Crash Dumpok, IE/Edge cache, színprofilok)? (i/n): ").strip().lower() == 'i'
            api.clean_temp_files(thumbnail_cache=extra1, recycle_bin=extra2,
                                  delivery_opt=extra3, wer=extra3, shader_cache=extra3,
                                  cbs_logs=extra3, crash_dumps=extra3, inet_cache=extra3,
                                  color_profiles=extra3)
            input("\nNyomj ENTER-t a folytatáshoz...")
        elif choice == '6':
            print_header()
            api.download_block_script()
            input("\nNyomj ENTER-t a folytatáshoz...")
        elif choice == '7':
            target_menu()
        elif choice == '8':
            print_header()
            print("""
  ℹ️  CSAK A GRAFIKUS FELÜLETEN (GUI) ELÉRHETŐ FUNKCIÓK

  A következő funkciók jelenleg csak a grafikus (nem --cli) módban
  érhetők el, futtasd a programot --cli kapcsoló nélkül, ha ezekre
  van szükséged:

    • BitLocker állapot lekérdezése / kikapcsolása
    • HTML hardverjelentés generálása (S.M.A.R.T. adatokkal)
    • Célzott WU driver keresés és kiválasztásos telepítés
      (a CLI Autofix csak a teljes automatikus telepítést tudja)
    • Stabilitás (stressz) teszt indítása
""")
            input("\nNyomj ENTER-t a folytatáshoz...")
        else:
            print("❌ Érvénytelen választás!")


# ================================================================
# MAIN
# ================================================================
if __name__ == "__main__":
    import ctypes
    
    # --- SINGLE INSTANCE CHECK (Csak egyszer fusson) ---
    ERROR_ALREADY_EXISTS = 183
    mutex_name = "Global\\DriverVarazslo_App_Mutex_Lock"
    hMutex = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
    if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        try:
            ctypes.windll.user32.MessageBoxW(
                None,
                "A DriverVarázsló már fut a rendszeren!\n\nKérjük, zárd be a másik ablakot, vagy ellenőrizd a tálcán.",
                "DriverVarázsló - Figyelmeztetés",
                0x30 | 0x40000  # MB_ICONWARNING | MB_TOPMOST
            )
        except Exception:
            pass
        sys.exit(0)

    import multiprocessing
    multiprocessing.freeze_support()

    def _relaunch_elevated():
        """UAC self-elevation. True, ha az emelt jogú processz sikeresen elindult;
        False, ha a felhasználó elutasította a UAC-promptot vagy hiba történt
        (ShellExecuteW <= 32 visszatérési érték = hiba, ld. WinAPI dokumentáció)."""
        params = ' '.join([f'"{arg}"' for arg in sys.argv[1:]])
        if getattr(sys, 'frozen', False):
            exe, args = sys.executable, params
        else:
            exe, args = sys.executable, f'"{sys.argv[0]}" {params}'
        result = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, None, 1)
        return result > 32

    if "--cli" in sys.argv:
        if getattr(sys, "frozen", False):
            # Attach to the parent console if running from cmd in windowed mode
            if ctypes.windll.kernel32.AttachConsole(-1):
                sys.stdout = open("CONOUT$", "w", encoding="utf-8")
                sys.stderr = open("CONOUT$", "w", encoding="utf-8")
                sys.stdin = open("CONIN$", "r", encoding="utf-8")

    # CLI mód
    if '--cli' in sys.argv:
        if not is_admin():
            print("⚠️  Rendszergazdai jogosultság szükséges, UAC-emelés kérése...")
            if _relaunch_elevated():
                sys.exit(0)
            print("❌ Az emelt jogú indítás megszakadt vagy elutasításra került!")
            print("   Futtasd manuálisan rendszergazdaként!")
            input("Nyomj ENTER-t a kilépéshez...")
            sys.exit(1)
        run_cli_mode()
        sys.exit(0)

    if not is_admin():
        if not _relaunch_elevated():
            try:
                ctypes.windll.user32.MessageBoxW(
                    None,
                    "A DriverVarázsló futtatásához rendszergazdai jogosultság szükséges.\n\n"
                    "Az emelt jogú indítás megszakadt vagy elutasításra került (UAC).\n"
                    "Indítsd el újra, és fogadd el a jogosultság-kérést.",
                    "DriverVarázsló - Jogosultság szükséges",
                    0x10 | 0x40000  # MB_ICONERROR | MB_TOPMOST
                )
            except Exception:
                pass
        sys.exit()

    # Logging - RotatingFileHandler, hogy a DEBUG-szintű, minden subprocess-kimenetet logoló
    # fájl ne nőhessen korlátlanul (egy hosszú élettartamú szerviz-USB-n/WinPE-n, sok gépen,
    # sok futtatás alatt évekig gyűlő log könnyen több száz MB-ra hízhatna rotáció nélkül).
    log_filename = os.path.join(_app_data_dir(), "DriverVarázsló_debug.log")
    try:
        from logging.handlers import RotatingFileHandler
        log_handler = RotatingFileHandler(log_filename, maxBytes=5 * 1024 * 1024, backupCount=2, encoding='utf-8')
        logging.basicConfig(level=logging.DEBUG, handlers=[log_handler],
                            format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    except Exception:
        logging.basicConfig(level=logging.DEBUG)

    def global_exception_handler(exc_type, exc_value, exc_traceback):
        err_str = str(exc_value)
        logging.exception("FATÁLIS HIBA:", exc_info=(exc_type, exc_value, exc_traceback))
        # WebView2 hibák detektálása
        if 'WebView2' in err_str or 'ICoreWebView2' in err_str or '.NET' in err_str:
            logging.error("[MAIN] WebView2 hiba detektálva exception handler-ben!")
            _webview_error.set()
    sys.excepthook = global_exception_handler

    def cleanup_zombies():
        # Nem atexit-tel regisztrálva, mert a program mindig os._exit(0)-val lép ki,
        # ami teljesen kihagyja az atexit hook-okat - ezért itt explicit hívjuk meg
        # minden os._exit(0) előtt.
        try:
            pid = os.getpid()
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            pass

    def thread_exception_handler(args):
        err_str = str(args.exc_value)
        logging.exception("HÁTTÉRSZÁL HIBA:", exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        if 'WebView2' in err_str or 'ICoreWebView2' in err_str or '.NET' in err_str:
            logging.error("[MAIN] WebView2 hiba detektálva szál exception handler-ben!")
            _webview_error.set()
    threading.excepthook = thread_exception_handler

    logging.info("=" * 50)
    logging.info("DriverVarázsló ELINDITVA")
    logging.info(f"Futtatasi konyvtar: {os.getcwd()}")
    logging.info("=" * 50)

    # WebView2 Runtime verzió ellenőrzés - ha túl régi, egyből CLI mód
    wv2_ok, wv2_info = check_webview2_runtime()
    if wv2_ok:
        logging.info(f"[INIT] WebView2 Runtime OK: v{wv2_info}")
    else:
        logging.warning(f"[INIT] WebView2 nem megfelelő: {wv2_info}")
        logging.info("[INIT] WebView2 telepítés felajánlása...")
        
        # MessageBox: telepítsük?
        MB_YESNO = 0x4
        MB_ICONQUESTION = 0x20
        MB_TOPMOST = 0x40000
        IDYES = 6
        
        result = ctypes.windll.user32.MessageBoxW(
            None,
            "A WebView2 Runtime hiányzik vagy túl régi!\n\n"
            "A DriverVarázsló GUI-hoz WebView2 v109+ szükséges.\n\n"
            "Telepítsem automatikusan?\n"
            "(~2MB letöltés, pár másodperc)",
            "DriverVarázsló - WebView2 telepítés",
            MB_YESNO | MB_ICONQUESTION | MB_TOPMOST
        )
        
        if result == IDYES:
            logging.info("[INIT] Felhasználó elfogadta a WebView2 telepítést")
            
            # Progress MessageBox (nem blokkoló)
            import urllib.request
            import tempfile
            
            try:
                # Letöltés
                logging.info("[INIT] WebView2 Bootstrapper letöltése...")
                bootstrapper_url = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
                temp_dir = tempfile.gettempdir()
                bootstrapper_path = os.path.join(temp_dir, "MicrosoftEdgeWebview2Setup.exe")
                
                # Progress ablak
                ctypes.windll.user32.MessageBoxW(
                    None,
                    "WebView2 telepítése folyamatban...\n\n"
                    "Ez pár másodpercet vesz igénybe.\n"
                    "Kattints OK-ra és várd meg!",
                    "DriverVarázsló",
                    0x40 | MB_TOPMOST  # MB_ICONINFORMATION
                )
                
                urllib.request.urlretrieve(bootstrapper_url, bootstrapper_path)
                logging.info(f"[INIT] Bootstrapper letöltve: {bootstrapper_path}")
                
                # Telepítés silent módban
                logging.info("[INIT] WebView2 telepítés indítása (silent)...")
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                result = subprocess.run(
                    [bootstrapper_path, '/silent', '/install'],
                    capture_output=True,
                    startupinfo=si,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    timeout=120
                )
                
                logging.info(f"[INIT] WebView2 telepítés kész, returncode={result.returncode}")
                
                # Törlés
                try:
                    os.remove(bootstrapper_path)
                except Exception as e:
                    logging.debug(e)
                
                # Újraellenőrzés
                wv2_ok2, wv2_info2 = check_webview2_runtime()
                if wv2_ok2:
                    logging.info(f"[INIT] WebView2 telepítés SIKERES! v{wv2_info2}")
                    ctypes.windll.user32.MessageBoxW(
                        None,
                        f"WebView2 sikeresen telepítve!\n\nVerzió: {wv2_info2}\n\n"
                        "A program most újraindul a GUI-val.",
                        "DriverVarázsló - Siker",
                        0x40 | MB_TOPMOST
                    )
                    # Program újraindítása
                    os.execv(sys.executable, [sys.executable] + sys.argv)
                else:
                    logging.error(f"[INIT] WebView2 telepítés után még mindig nem OK: {wv2_info2}")
                    ctypes.windll.user32.MessageBoxW(
                        None,
                        "WebView2 telepítés sikertelen vagy újraindítás szükséges.\n\n"
                        "Próbáld meg manuálisan:\n"
                        "https://go.microsoft.com/fwlink/p/?LinkId=2124703\n\n"
                        "Vagy használd a CLI módot.",
                        "DriverVarázsló - Hiba",
                        0x10 | MB_TOPMOST  # MB_ICONERROR
                    )
                    
            except Exception as e:
                logging.error(f"[INIT] WebView2 telepítési hiba: {e}")
                ctypes.windll.user32.MessageBoxW(
                    None,
                    f"Hiba a WebView2 telepítésekor:\n{e}\n\n"
                    "Próbáld meg manuálisan:\n"
                    "https://go.microsoft.com/fwlink/p/?LinkId=2124703\n\n"
                    "Vagy használd a CLI módot.",
                    "DriverVarázsló - Hiba",
                    0x10 | MB_TOPMOST
                )
        else:
            logging.info("[INIT] Felhasználó elutasította a WebView2 telepítést")
        
        # CLI mód indítása
        logging.info("[INIT] CLI mód indítása...")
        
        # Konzol ablak létrehozása (windowed exe-nél nincs)
        try:
            ctypes.windll.kernel32.AllocConsole()
            sys.stdin = open('CONIN$', 'r')
            sys.stdout = open('CONOUT$', 'w')
            sys.stderr = open('CONOUT$', 'w')
        except Exception as e:
            logging.debug(e)
        
        print("\n" + "=" * 60)
        print("  📋 DRIVERVARÁZSLÓ - CLI MÓD")
        print("=" * 60)
        
        run_cli_mode()
        cleanup_zombies()
        os._exit(0)

    # Hardware rendering (gyors) - az autofix progress külön ablakban jelenik meg

    api = DriverToolApi()
    html_path = resource_path('ui.html')

    window = webview.create_window(
        'DriverVarázsló',
        url=html_path,
        js_api=api,
        width=1200, height=780,
        min_size=(900, 600)
    )

    def on_start():
        api.set_window(window)

    # Watchdog: ha 15mp alatt nem indul el a GUI, bezárja az ablakot és CLI-re vált
    def webview_watchdog():
        TIMEOUT = 60  # seconds
        start = time.time()
        while time.time() - start < TIMEOUT:
            if _webview_ready.is_set():
                logging.info("[WATCHDOG] WebView2 sikeresen elindult")
                return  # GUI OK
            if _webview_error.is_set():
                logging.error("[WATCHDOG] WebView2 hiba detektálva, ablak bezárása...")
                time.sleep(0.5)  # Adj időt a log kiírására
                try:
                    window.destroy()
                except Exception as e:
                    logging.debug(e)
                return
            time.sleep(0.25)
        # Timeout
        logging.error(f"[WATCHDOG] {TIMEOUT}s timeout - WebView2 nem válaszol, ablak bezárása...")
        _webview_error.set()
        try:
            window.destroy()
        except Exception as e:
            logging.debug(e)

    watchdog_thread = threading.Thread(target=webview_watchdog, daemon=True)
    watchdog_thread.start()

    gui_failed = False
    try:
        logging.info("[MAIN] webview.start() hívása...")
        webview.start(func=on_start, debug=False)
        # webview.start() visszatért - ellenőrizzük hogy sikeres volt-e
        if not _webview_ready.is_set() or _webview_error.is_set():
            gui_failed = True
            logging.info("[MAIN] GUI nem indult el sikeresen, CLI mód következik...")
    except Exception as e:
        gui_failed = True
        logging.error(f"[MAIN] WebView indítási hiba: {e}")
        logging.error("[MAIN] Automatikus CLI mód indítása...")
    
    if gui_failed:
        # Konzol ablak létrehozása ha nincs (windowed exe-nél)
        try:
            ctypes.windll.kernel32.AllocConsole()
            # Stdin/stdout/stderr átirányítása az új konzolra
            sys.stdin = open('CONIN$', 'r')
            sys.stdout = open('CONOUT$', 'w')
            sys.stderr = open('CONOUT$', 'w')
        except Exception as e:
            logging.debug(e)
        
        print("\n" + "=" * 60)
        print("  ⚠️  GUI nem elérhető - CLI mód automatikusan aktiválva")
        print("  (Telepítsd a WebView2 Runtime-ot a GUI-hoz)")
        print("=" * 60)
        
        run_cli_mode()

    cleanup_zombies()
    os._exit(0)
