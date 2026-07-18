"""WU DRIVER KERESÉS / TELEPÍTÉS - KÖZÖS MAG. Az eszköz-szűrés, a WU-találat<->eszköz
párosítás és a telepítő PowerShell script EGYETLEN példánya - a GUI manuális telepítés,
a GUI AutoFix és a CLI AutoFix is EZT hívja. NE másold vissza osztályba (lásd CLAUDE.md)!"""

# === AUTO-IMPORTS ===
import os
import re
import json
import time
import queue
import shutil
import logging
import threading
from app.common import _ps_quote
from app.common import _app_data_dir
# === /AUTO-IMPORTS ===




# AutoFix-nál opcionálisan kihagyható driver-osztályok (nyomtató + szkenner/multifunkciós) -
# ezek gyakran csak gyári driverrel működnek jól, a WU nem mindig telepíti vissza automatikusan.
AUTOFIX_PRINTER_SKIP_CLASSES = {'Printer', 'PrintQueue', 'Image'}


class WuProcessAborted(Exception):
    """A WU telepítő PowerShell folyamat idő előtt leállítva. reason='cancel' (felhasználói
    megszakítás) vagy 'hang' (a watchdog ölte meg, mert túl sokáig nem jött kimenet)."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _iter_process_lines(process, run_fn, cancel_check=None, inactivity_timeout=1800):
    """A telepítő PowerShell stdout-jának CANCEL-KÉPES, WATCHDOG-OS olvasása - mindhárom
    fogyasztó (GUI manuális, GUI AutoFix, CLI AutoFix) ezen keresztül olvassa a sorokat.

    A régi, közvetlen `for line in process.stdout` minta két terepi hibát hordozott:
    (1) a megszakítás-ellenőrzés csak új sor érkezésekor futott le, így ha a scripten
    belüli $Searcher.Search() végleg beragadt (arra ott nincs timeout, csak a külön
    _search_wu_api-nak van), a Mégse gomb halott volt; (2) a beragadt folyamatot semmi
    nem ölte meg, a feladat örökre "futott". Itt a tényleges olvasás egy háttérszálon
    történik queue-ba, a fogyasztó 0,5 mp-enként ellenőrzi a cancel-t, és ha
    inactivity_timeout másodpercig egyetlen sor sem érkezik, taskkill-lel leállítja a
    folyamatot. A timeout szándékosan hosszú (alapból 30 perc): egyetlen nagy driver
    letöltése lassú neten percekig ad nulla kimenetet - inkább későn ölünk, mint egy
    élő letöltést.

    Kivétel: WuProcessAborted('cancel' | 'hang') - a folyamat ilyenkor már le van ölve."""
    q = queue.Queue()

    def _reader():
        try:
            for raw in process.stdout:
                q.put(raw)
        except Exception as e:
            logging.debug(f"[WU-READER] stdout olvasási hiba: {e}")
        finally:
            q.put(None)

    threading.Thread(target=_reader, daemon=True).start()

    def _kill(why):
        logging.warning(f"[WU-WATCHDOG] Telepítő folyamat leállítása (PID={process.pid}, ok={why})")
        try:
            run_fn(['taskkill', '/F', '/T', '/PID', str(process.pid)])
        except Exception as e:
            logging.error(f"[WU-WATCHDOG] taskkill hiba: {e}")
        try:
            process.wait(timeout=10)
        except Exception:
            pass

    last_output = time.time()
    while True:
        if cancel_check and cancel_check():
            _kill('cancel')
            raise WuProcessAborted('cancel')
        try:
            item = q.get(timeout=0.5)
        except queue.Empty:
            if time.time() - last_output > inactivity_timeout:
                logging.error(f"[WU-WATCHDOG] {inactivity_timeout}s óta nincs kimenet - a WU folyamat beragadt.")
                _kill('hang')
                raise WuProcessAborted('hang')
            continue
        if item is None:
            break
        last_output = time.time()
        line = item.strip()
        if line:
            yield line
    process.wait()



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
# A ConfigManagerErrorCode is jön: a hibakódos eszközök (28 = nincs driver, 10 = nem indul,
# stb.) a manuális szken "Problémás eszközök" szekciójához és a hibrid katalógus-
# kiegészítéshez kellenek.
WU_PNP_QUERY_PS = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                   "Get-WmiObject Win32_PnPEntity | Where-Object { $_.Present -eq $true -and $_.ConfigManagerErrorCode -ne 45 } | "
                   "Select-Object Name, PNPClass, PNPDeviceID, HardwareID, ConfigManagerErrorCode | ConvertTo-Json -Compress")


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

        try:
            err_code = int(d.get("ConfigManagerErrorCode") or 0)
        except (TypeError, ValueError):
            err_code = 0

        devices.append({"cat": cat, "name": n, "id": hwid_clean, "pnp_id": pid,
                        "all_hwids": hwids_list, "err_code": err_code})
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


_ISO_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _iso_date_or_none(s):
    """'yyyy-MM-dd' formátumú dátum-string vagy None. Az ilyen stringek sima
    string-összehasonlítással helyesen rendeződnek, nem kell datetime."""
    s = (s or '').strip()[:10]
    return s if _ISO_DATE_RE.match(s) else None


def _filter_wu_downgrades(matches, wu_by_uid, installed_info):
    """DOWNGRADE-VÉDELEM (AutoFix): kiszűri azokat a párosított WU-találatokat, amelyek
    bizonyíthatóan RÉGEBBIEK az eszköz éppen telepített driverénél. Terepi kockázat:
    gyári (pl. NVIDIA) driver telepítése után a WU IsInstalled=0-val felajánl egy
    hónapokkal korábbi csomagot, és az AutoFix gondolkodás nélkül visszabutítaná.

    Szabályok (szándékosan konzervatív, csak BIZONYÍTOTT downgrade esik ki):
    - hibakódos eszközt SOSEM szűrünk - egy driver nélküli/hibás eszköznek egy régebbi
      driver is jobb, mint a semmi;
    - csak akkor szűrünk, ha a WU DriverVerDate ÉS a telepített driver dátuma is
      értelmezhető, és a WU-é szigorúan korábbi;
    - egyenlő vagy újabb dátum, hiányzó adat -> marad a találat.

    matches: a _match_wu_updates_to_devices kimenete; wu_by_uid: UpdateID -> nyers
    WU-találat dict (DriverVerDate mezővel); installed_info: UPPER(pnp instance id) ->
    {'version','date'} map (GUI: _get_installed_driver_info). Visszatérés:
    (megtartott matches, kiszűrt [{'title','reason'}] lista - a hívó logolja)."""
    kept = []
    skipped = []
    for m in matches:
        dev = m.get('device') or {}
        if dev.get('err_code'):
            kept.append(m)
            continue
        wu = wu_by_uid.get(m.get('uid')) or {}
        wu_date = _iso_date_or_none(wu.get('DriverVerDate'))
        inst = installed_info.get((dev.get('pnp_id') or '').upper()) or {}
        inst_date = _iso_date_or_none(inst.get('date'))
        if wu_date and inst_date and wu_date < inst_date:
            skipped.append({'title': m.get('title', ''),
                            'reason': f"WU driver dátuma ({wu_date}) régebbi a telepítettnél ({inst_date})"})
            continue
        kept.append(m)
    return kept, skipped


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
    INSTONE/OK/OKRB/FAIL/EMPTY/DONE/ERROR prefixű sorok. Az OKRB ugyanaz mint az OK,
    de a WUA jelezte, hogy a driver csak ÚJRAINDÍTÁS után él ($IR.RebootRequired) -
    a sikeres számlálóba beleszámít, a hívó dönt a reboot-jelzés megjelenítéséről."""
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
        $rb = $false; try { $rb = [bool]$IR.RebootRequired } catch {}
        if ($rc -eq 2 -or $rc -eq 3) {
            if ($rb) { Write-Output "OKRB: $t" } else { Write-Output "OK: $t" }
            $s++
        } else { Write-Output "FAIL: [kód=$rc] $t"; $f++ }
    }
    Write-Output "DONE: Sikeres=$s, Sikertelen=$f"
} catch { Write-Output "ERROR: $($_.Exception.Message)" }
"""


# ============================================================================
# NYOMTATÓ-VÉDELEM 2.0 - KÖZÖS MAG (GUI AutoFix + CLI AutoFix)
# Terepi igény: az ügyfélgépeken a nyomtatónak a driver-fix UTÁN is működnie
# kell. A puszta osztály-alapú kihagyás (Printer/PrintQueue/Image) NEM elég:
# egy multifunkciós HP/Canon csomag segéd-driverei USB/Ports/SYSTEM osztályba
# esnek (pl. mvusbews.inf, hppscnd.inf, hpbuio70l.inf - valós gépről), amiket a
# régi szűrő törölt, és a WU nem feltétlenül rakja vissza a gyári csomagot.
# ============================================================================

# Nyomtató-gyártó kulcsszavak: ha egy jelenlévő nyomtatási/szkennelési komponens
# szolgáltatója (provider) ezek egyikére illik, akkor a gépen lévő ÖSSZES ilyen
# szolgáltatójú third-party csomag védetté válik. Szándékosan túl-védő: pl. HP
# laptopon HP nyomtatóval a HP rendszer-driverek is megmaradnak - ezeket a WU
# úgyis visszarakná, a nyomtató működése viszont pótolhatatlan.
PRINTER_VENDOR_KEYWORDS = [
    'hewlett', 'hp inc', 'canon', 'epson', 'seiko', 'brother', 'samsung',
    'lexmark', 'kyocera', 'ricoh', 'xerox', 'oki ', 'okidata', 'zebra',
    'pantum', 'konica', 'minolta', 'dymo', 'star micronics', 'citizen',
    'bixolon', 'godex', 'tsc ', 'sagem', 'olivetti', 'toshiba tec', 'sharp',
]

_PRINTER_PROTECT_PS = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$out = @{ Infs = @(); Providers = @() }
try {
    Get-PrinterDriver -ErrorAction SilentlyContinue | ForEach-Object {
        if ($_.InfPath) { $out.Infs += [System.IO.Path]::GetFileName("$($_.InfPath)") }
        if ($_.Manufacturer) { $out.Providers += "$($_.Manufacturer)" }
    }
} catch {}
try {
    $devs = Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | Where-Object { $_.Class -in @('Printer','PrintQueue','Image') }
    foreach ($d in $devs) {
        try {
            $inf = (Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName 'DEVPKEY_Device_DriverInfPath' -ErrorAction SilentlyContinue).Data
            if ($inf) { $out.Infs += "$inf" }
            $prov = (Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName 'DEVPKEY_Device_DriverProvider' -ErrorAction SilentlyContinue).Data
            if ($prov) { $out.Providers += "$prov" }
        } catch {}
    }
} catch {}
$out | ConvertTo-Json -Compress
"""


def _collect_printer_protection(run_fn):
    """Összegyűjti, hogy a gépen JELENLÉVŐ nyomtatási/szkennelési komponensek ténylegesen
    melyik driver-csomagokat használják. Visszatérés: (védett INF-nevek halmaza kisbetűvel,
    pl. {'oem113.inf'}, érintett nyomtató-gyártó kulcsszavak halmaza). Forrás: minden felvett
    nyomtató drivere (Get-PrinterDriver InfPath) + minden jelenlévő Printer/PrintQueue/Image
    eszköz aktív INF-je és szolgáltatója. Hiba esetén üres halmazok - olyankor csak a
    hagyományos osztály-alapú védelem él."""
    protected_infs = set()
    printing_vendors = set()
    try:
        res = run_fn(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", _PRINTER_PROTECT_PS],
                     encoding='utf-8', timeout=120)
        data = json.loads(res.stdout) if res and (res.stdout or '').strip() else {}
        infs = data.get('Infs') or []
        provs = data.get('Providers') or []
        if isinstance(infs, str):
            infs = [infs]
        if isinstance(provs, str):
            provs = [provs]
        for inf in infs:
            base = os.path.basename(str(inf)).strip().lower()
            if base.endswith('.inf'):
                protected_infs.add(base)
        for p in provs:
            pl = str(p).lower()
            for kw in PRINTER_VENDOR_KEYWORDS:
                if kw in pl:
                    printing_vendors.add(kw)
        logging.info(f"[PRINTER-PROTECT] Védett INF-ek: {sorted(protected_infs)}, nyomtató-gyártók: {sorted(printing_vendors)}")
    except Exception as e:
        logging.warning(f"[PRINTER-PROTECT] Védett lista gyűjtése sikertelen (marad az osztály-alapú védelem): {e}")
    return protected_infs, printing_vendors


def _is_printer_protected(drv, protected_infs, printing_vendors, skip_classes):
    """Egy dism-listás third-party driver-bejegyzésről eldönti, hogy nyomtató-védelem alá
    esik-e: (1) osztály szerint (a régi viselkedés), (2) a jelenlévő nyomtatási komponensek
    által TÉNYLEGESEN használt INF-ek szerint, (3) a gépen nyomtatóval jelen lévő gyártó
    minden csomagja szerint. Az INF-egyeztetés a publikált (oemXX.inf) ÉS az eredeti
    (pl. hpc1320u.inf) névvel is fut: a Get-PrinterDriver InfPath-ja az EREDETI nevet
    adja, a PnP-eszközök DriverInfPath-ja viszont a publikáltat - élesben mindkét forma
    előfordul a védett halmazban."""
    if drv.get('class', '') in (skip_classes or set()):
        return True
    if (drv.get('published', '') or '').lower() in protected_infs:
        return True
    if (drv.get('original', '') or '').lower() in protected_infs:
        return True
    prov = (drv.get('provider', '') or '').lower()
    return any(kw in prov for kw in printing_vendors)


# ============================================================================
# HÁLÓZATI DRIVER MENTŐÖV - KÖZÖS MAG (GUI AutoFix + CLI AutoFix)
# Terepen látott kockázat: az AutoFix a LAN/Wi-Fi drivert is törli, és ha sem a
# beépített, sem a WU-s driver nem fedi le az adott kártyát (valós eset: friss
# AM5-ös gép Realtek 2.5GbE-vel), a gép internet nélkül ragad - miközben a lánc
# folytatása pont internetből dolgozna. Ezért törlés ELŐTT a Net-osztályú
# drivereket pnputil /export-driver-rel elmentjük, és ha a folytatásnál nincs
# net, visszatöltjük őket.
# ============================================================================

def _net_backup_dir():
    return os.path.join(_app_data_dir(), 'netdrv_backup')


def _export_net_driver_backup(run_fn, drivers):
    """A törlésre váró listából a Net-osztályú driver-csomagokat exportálja a
    _net_backup_dir()-be (előtte üríti, hogy ne keveredjen régi mentéssel).
    Visszaadja a sikeresen exportált csomagok számát."""
    net_drivers = [d for d in drivers if (d.get('class', '') or '').lower() == 'net' and d.get('published')]
    if not net_drivers:
        return 0
    dest = _net_backup_dir()
    try:
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(dest, exist_ok=True)
    except Exception as e:
        logging.warning(f"[NET-BACKUP] Mentési mappa előkészítése sikertelen: {e}")
        return 0
    exported = 0
    for d in net_drivers:
        res = run_fn(['pnputil', '/export-driver', d['published'], dest], timeout=300)
        if res and res.returncode == 0:
            exported += 1
        else:
            logging.warning(f"[NET-BACKUP] Export sikertelen: {d.get('published')} ({d.get('original')})")
    logging.info(f"[NET-BACKUP] {exported}/{len(net_drivers)} hálózati driver elmentve ide: {dest}")
    return exported


def _restore_net_driver_backup(run_fn):
    """A korábban elmentett Net-driverek visszatelepítése (pnputil /add-driver /install).
    Visszaadja, hogy volt-e egyáltalán mit visszatölteni (bool)."""
    src = _net_backup_dir()
    if not os.path.isdir(src):
        logging.info("[NET-BACKUP] Nincs mentett hálózati driver, visszaállítás kihagyva.")
        return False
    has_inf = False
    for _root, _dirs, files in os.walk(src):
        if any(f.lower().endswith('.inf') for f in files):
            has_inf = True
            break
    if not has_inf:
        logging.info("[NET-BACKUP] A mentési mappa üres, visszaállítás kihagyva.")
        return False
    res = run_fn(['pnputil', '/add-driver', os.path.join(src, '*.inf'), '/subdirs', '/install'], timeout=600)
    ok = bool(res) and ('successfully' in (res.stdout or '').lower() or res.returncode in (0, 259, 3010))
    logging.info(f"[NET-BACKUP] Visszaállítás {'sikeres' if ok else 'részben/nem sikerült'} innen: {src}")
    return True
