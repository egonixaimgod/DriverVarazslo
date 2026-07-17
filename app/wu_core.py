"""WU DRIVER KERESÉS / TELEPÍTÉS - KÖZÖS MAG. Az eszköz-szűrés, a WU-találat<->eszköz
párosítás és a telepítő PowerShell script EGYETLEN példánya - a GUI manuális telepítés,
a GUI AutoFix és a CLI AutoFix is EZT hívja. NE másold vissza osztályba (lásd CLAUDE.md)!"""

# === AUTO-IMPORTS ===
from app.common import _ps_quote
# === /AUTO-IMPORTS ===




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
