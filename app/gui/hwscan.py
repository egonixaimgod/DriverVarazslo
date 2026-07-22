"""DriverVarázsló GUI - Driver Keresés és Telepítés nézet: hardver-szken, WU/Catalog keresés, kiválasztott driverek telepítése."""

# === AUTO-IMPORTS ===
import os
import sys
import platform
import subprocess
import re
import threading
import time
import logging
import shutil
import json
import glob
import traceback
import queue
from app.common import _ps_quote
from app import dupdrivers_core
from app.wu_core import WU_PNP_QUERY_PS
from app.wu_core import WuProcessAborted
from app.wu_core import _build_wu_install_ps
from app.wu_core import _filter_wu_scan_devices
from app.wu_core import _is_inbox_driver
from app.wu_core import _iso_date_or_none
from app.wu_core import _iter_process_lines
from app.wu_core import _match_wu_updates_to_devices
from app.wu_core import _parse_driver_version
from app.wu_core import unoffered_requested_titles
# === /AUTO-IMPORTS ===


# Eszközkezelő-hibakódok emberi olvasatban (a "Problémás eszközök" szekcióhoz).
# Csak a gyakoriak - ismeretlen kódra általános szöveg megy.
PNP_ERROR_CODE_DESCRIPTIONS = {
    1: 'Nincs megfelelően konfigurálva',
    3: 'A driver sérült vagy kevés az erőforrás',
    10: 'Az eszköz nem tud elindulni',
    12: 'Nincs elég szabad erőforrás',
    14: 'Újraindítás szükséges a működéshez',
    18: 'A drivert újra kell telepíteni',
    19: 'A registry-bejegyzése sérült',
    21: 'A Windows épp eltávolítja az eszközt',
    22: 'Az eszköz le van tiltva',
    24: 'Az eszköz nincs jelen vagy hibás',
    28: 'NINCS TELEPÍTVE DRIVER',
    31: 'Nem működik megfelelően (driver-hiba)',
    32: 'A szolgáltatása le van tiltva',
    37: 'A driver inicializálása sikertelen',
    39: 'A driver sérült vagy hiányzik',
    43: 'Az eszköz hibát jelzett és leállt',
    52: 'A driver aláírása nem ellenőrizhető',
}


class GuiHwScanMixin:
    """Driver Keresés és Telepítés nézet: hardver-szken, WU/Catalog keresés, kiválasztott driverek telepítése. A DriverToolApi része (összerakás: app/gui/api.py)."""

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

                # Telepített driver-verziók/dátumok egyszeri felmérése: a találatok melletti
                # "Telepítve: X" kijelzéshez ÉS a katalógus-út már-telepítve szűréséhez.
                self.emit('hw_scan_progress', {'status': '📋 Telepített driver-verziók felmérése...'})
                inst_info = self._get_installed_driver_info()

                # Közvetlen WU API lekérdezés (a COM objektum ezen kulcs módosítása nélkül is látja a drivereket)
                self.emit('hw_scan_progress', {'status': '🔎 Windows Update driver-keresés folyamatban...'})
                wu_results = self._search_wu_api()
                wu_api_success = wu_results is not None

                if wu_results is None:
                    wu_results = []

                self.emit('hw_scan_progress', {'status': '📋 Eredmények feldolgozása...'})

                # Párosítás a KÖZÖS _match_wu_updates_to_devices-szel (HWID prefix + név-tartalék,
                # az AutoFix is pontosan ezt hívja - ne ide írj párosítási logikát!)
                wu_by_uid = {w.get('UpdateID'): w for w in wu_results if w.get('UpdateID')}
                matches = _match_wu_updates_to_devices(wu_results, devices_to_check)
                matched_hwids = set()
                matched_uids = set()
                for m in matches:
                    dev = m['device']
                    matched_hwids.add(dev['id'])
                    matched_uids.add(m['uid'])
                    inst = inst_info.get((dev.get('pnp_id') or '').upper()) or {}
                    wu_date = _iso_date_or_none((wu_by_uid.get(m['uid']) or {}).get('DriverVerDate')) or ''
                    inst_date = _iso_date_or_none(inst.get('date')) or ''
                    self.hw_updates_pool.append({
                        "name": dev['name'], "cat": dev['cat'], "hwid": dev['id'],
                        "wu_title": m['title'], "pnp_id": dev.get('pnp_id', ''),
                        "installed_version": inst.get('version', ''),
                        "installed_date": inst_date,
                        "wu_date": wu_date,
                        # Downgrade-jelzés a felületnek: a WU néha a telepítettnél RÉGEBBI
                        # csomagot ajánl (pl. friss gyári NVIDIA driver után) - a manuális
                        # listából nem rejtjük el, csak megjelöljük, a döntés a felhasználóé.
                        # (Az AutoFix ezzel szemben automatikusan kihagyja az ilyet, lásd
                        # wu_core._filter_wu_downgrades.)
                        # (_is_inbox_driver: a beépített generikus driver frissebb dátuma
                        # nem downgrade-jelzés - lásd wu_core._filter_wu_downgrades.)
                        "downgrade": bool(wu_date and inst_date and wu_date < inst_date
                                          and not dev.get('err_code')
                                          and not _is_inbox_driver(inst)),
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

                if not wu_api_success:
                    # Teljes katalógus-fallback: a WU API elhasalt, minden eszközt a
                    # katalógusban keresünk.
                    self.wu_api_mode = False
                    self.emit('hw_scan_progress', {'status': f'🌐 WU API hiba, katalógus keresés ({total_devs} eszköz)...'})
                    self._catalog_search(devices_to_check, installed_info=inst_info)
                else:
                    # HIBRID KIEGÉSZÍTÉS: a hibakódos (driver nélküli / hibás) eszközökre,
                    # amikre a WU nem adott semmit, még ráengedjük a katalógus-keresést is -
                    # két forrás egyesítve, hogy tényleg MINDENT megtaláljunk. A pool vegyes
                    # lesz (WU-s elemek update_id-vel, katalógusosak url-lel), a telepítő
                    # diszpécser (install_selected_wu) elemenként dönti el a módot.
                    leftover = [d for d in devices_to_check if d.get('err_code') and d['id'] not in matched_hwids]
                    if leftover:
                        self.emit('hw_scan_progress', {'status': f'🌐 Katalógus-kiegészítés {len(leftover)} problémás eszközre...'})
                        self._catalog_search(leftover, installed_info=inst_info)

                # A "telepített/naprakész" lista: minden eszköz, amire végül nincs találat.
                pool_hwids = {p.get('hwid') for p in self.hw_updates_pool}
                self._hw_installed_devs = [dev for dev in devices_to_check if dev['id'] not in pool_hwids]

                # PROBLÉMÁS ESZKÖZÖK: hibakódos eszközök kiemelése, hogy sose maradjon
                # észrevétlen lyuk - akkor is látszik, ha egyik forrás sem adott rá drivert.
                problems = []
                for dev in devices_to_check:
                    code = dev.get('err_code') or 0
                    if not code:
                        continue
                    problems.append({
                        'name': dev['name'], 'hwid': dev['id'], 'code': code,
                        'pnp_id': dev.get('pnp_id', ''),
                        'desc': PNP_ERROR_CODE_DESCRIPTIONS.get(code, f'Hibakód: {code}'),
                        'has_fix': dev['id'] in pool_hwids,
                    })
                if problems:
                    logging.info(f"[HW_SCAN] Problémás eszközök: {[(p['name'], p['code'], p['has_fix']) for p in problems]}")

                elapsed = int(time.time() - _start)
                _m, _s = divmod(elapsed, 60)
                time_str = f"{_m} perc {_s} mp" if _m else f"{_s} mp"
                mode = "WU API" if self.wu_api_mode else "Katalógus"
                found = len(self.hw_updates_pool)
                final_sys = f"{sys_info_text} | ✅ Kész ({mode})! {found} frissítés ({total_devs} eszköz)"

                self.emit('hw_scan_result', {
                    'pool': self.hw_updates_pool, 'installed': self._hw_installed_devs,
                    'problems': problems, 'sys_info': final_sys, 'time': time_str
                })
                self._hw_loaded = True

                # Gyári GPU-driver ellenőrzések (app/gui/nvidia.py + vendorgpu.py): a WU
                # hónapokkal lemarad a gyári driverektől - NVIDIA-nál letöltés+csendes
                # telepítés, AMD/Intel-nél verzió-összevetés + hivatalos oldal link-out.
                # Mindnek saját hibakezelése van, a szken eredményét sosem boríthatják.
                self._check_nvidia_driver()
                self._check_amd_driver()
                self._check_intel_driver()
                # OEM (Dell/Lenovo/HP) gépre szabott driver-oldal kártya (link-out).
                self._check_oem_driver_page()
            except Exception as e:
                logging.error(f"hw_scan crash: {e}")
                logging.error(traceback.format_exc())
                self.emit('hw_scan_progress', {'status': '❌ Hiba történt!'})
                self.emit('hw_scan_result', {'pool': [], 'installed': [], 'sys_info': '❌ Scan hiba', 'time': ''})
            finally:
                self._hw_scanning = False
                self._task_busy = None

        try:
            threading.Thread(target=worker, daemon=True, name="hw-scan").start()
        except Exception as e:
            logging.error(f"[HW_SCAN] Thread indítási hiba: {e}")
            self._hw_scanning = False
            self._task_busy = None
            self.emit('hw_scan_result', {'pool': [], 'installed': [], 'sys_info': '❌ Thread hiba', 'time': ''})

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
        $dvd = ''; try { $dvd = ([datetime]$U.DriverVerDate).ToString('yyyy-MM-dd') } catch {}
        $updates += [PSCustomObject]@{
            Title = $U.Title; DriverModel = $U.DriverModel; HardwareID = $U.DriverHardwareID
            DriverClass = $U.DriverClass; DriverProvider = $U.DriverProvider
            UpdateID = $U.Identity.UpdateID; Size = $U.MaxDownloadSize; DriverVerDate = $dvd
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
            logging.error("[WU_API] WU API timeout (300s) - szolgáltatás-újraindítás, majd azonnali továbblépés (nincs második keresési kör)...")
            self.emit('hw_scan_progress', {'status': '⚠️ A Windows Update API nem válaszol (5 perc) - áttérés a katalógus keresésre...'})
            self.emit('task_progress', {'task': 'autofix', 'log': '⚠️ Windows Update API időtúllépés! Szolgáltatások újraindítása...'})

            # A WU szolgáltatások újraindítása a GÉPET gyógyítja (a következő keresés már
            # jó eséllyel másodpercek alatt lefut), de az EREDMÉNYRE itt már nem várunk újra.
            reset_ps = r"""
            Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
            Stop-Service bits -Force -ErrorAction SilentlyContinue
            Stop-Service cryptsvc -Force -ErrorAction SilentlyContinue
            Start-Service cryptsvc -ErrorAction SilentlyContinue
            Start-Service bits -ErrorAction SilentlyContinue
            Start-Service wuauserv -ErrorAction SilentlyContinue
            """
            self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", reset_ps])
            # SZÁNDÉKOSAN NINCS újrapróbálkozás (korábban volt még egy 300s-os keresési kör):
            # terepen bizonyított (klónozott rendszer vadonatúj AM5 hardveren, 2026-07, két
            # egymás utáni szkennél is), hogy a szolgáltatás-újraindítás utáni retry ugyanúgy
            # 300s timeoutba fut - a felhasználó ~10,5 percet várt ~5,5 helyett, nulla
            # többlet-eredményért. A None visszatérésre a hívók maguktól váltanak: a manuális
            # szken a katalógus-fallbackre (start_hw_scan), az AutoFix a kör lezárására.
        except Exception as e:
            logging.error(f"[WU_API] WU API error: {e}")
        return None

    def _get_installed_driver_info(self):
        """A jelenleg telepített driverek verziója ÉS dátuma eszközönként
        (Win32_PnPSignedDriver): UPPER(eszköz instance ID) -> {'version': str, 'date':
        'yyyy-MM-dd', 'provider': str, 'inf': str} map. Fogyasztói: a katalógus-fallback már-telepítve szűrése, a
        találatok melletti "telepítve: X" kijelzés, és az AutoFix downgrade-védelme
        (wu_core._filter_wu_downgrades). A WU API útnál a szerver maga szűr az
        IsInstalled=0 feltétellel (terepen látott hiba e nélkül: a 3 perccel korábban
        telepített Realtek LAN drivert a következő szken újra felajánlotta).
        Hiba esetén üres map-pel (szűrés nélkül) folytatjuk - inkább ajánljunk fel egy már
        meglévő drivert, mint hogy elrejtsünk egy hiányzót."""
        info = {}
        try:
            # DriverProviderName + InfName is kell: ebből derül ki, hogy a jelenlegi driver
            # egy Windows-beépített (inbox) generikus-e. A downgrade-védelem ezt használja -
            # egy inbox driver "újabb dátuma" nem lehet indok a gyári csomag eldobására
            # (wu_core._filter_wu_downgrades).
            ps = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                  "Get-WmiObject Win32_PnPSignedDriver | Where-Object { $_.DeviceID -and $_.DriverVersion } | "
                  "Select-Object DeviceID, DriverVersion, DriverDate, DriverProviderName, InfName | ConvertTo-Json -Compress")
            res = self._run(["powershell", "-NoProfile", "-Command", ps], encoding='utf-8', timeout=120)
            data = json.loads(res.stdout) if res and res.stdout.strip() else []
            if isinstance(data, dict):
                data = [data]
            for d in data:
                did = (d.get('DeviceID') or '').upper()
                if not did:
                    continue
                # DriverDate WMI CIM_DATETIME formátumban jön: "20230115000000.000000+000"
                raw_date = str(d.get('DriverDate') or '')
                date = ''
                if len(raw_date) >= 8 and raw_date[:8].isdigit():
                    date = f"{raw_date[0:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
                info[did] = {'version': d.get('DriverVersion') or '', 'date': date,
                             'provider': d.get('DriverProviderName') or '',
                             'inf': d.get('InfName') or ''}
            logging.info(f"[CATALOG] Telepített driver-infó: {len(info)} eszköz")
        except Exception as e:
            logging.warning(f"[CATALOG] Telepített driver-infó lekérdezése sikertelen (verzió-szűrés nélkül folytatjuk): {e}")
        return info

    def _catalog_row_score(self, row_text_lower):
        """Egy katalógus-találati sor pontozása az AKTUÁLIS rendszerhez illés szerint (a
        sor teljes szövege alapján, ami a Products oszlopot is tartalmazza). A katalógus
        ugyanarra a HWID-re Windows 10/11/Server és amd64/arm64 sorokat is visszaad; a
        puszta "legmagasabb verzió" választás korábban rossz OS-hez/architektúrához
        tartozó csomagot is kiválaszthatott (a pnputil ezt ugyan visszadobta, de az
        eszköz "sikertelen telepítés"-ként végezte egy amúgy megtalálható driver helyett).
        None = kizárt sor (biztosan nem alkalmazható); egyébként minél nagyobb, annál jobb.
        A katalógus-szken csak élő rendszeren fut (start_hw_scan offline-t elutasít),
        ezért a host OS/architektúra a mérce."""
        t = row_text_lower
        machine = (platform.machine() or '').upper()
        if 'arm64' in t and not machine.startswith('ARM'):
            return None
        build = getattr(sys.getwindowsversion(), 'build', 0)
        if build >= 22000:  # Windows 11 host
            if 'windows 11' in t:
                return 3
            if 'windows 10' in t and 'later' in t:
                return 2  # "Windows 10 and later drivers" - Win11-re is érvényes, ez a leggyakoribb driver-sor
            if 'windows 10' in t:
                return 1
            if 'server' in t:
                return 0
            return 1
        else:  # Windows 10 host
            if 'windows 10' in t:
                return 3
            if 'windows 11' in t:
                return None  # Win11-only csomag Win10-re nem applikálható
            if 'server' in t:
                return 0
            return 1

    def _catalog_fetch_rows(self, hwid, ssl_ctx):
        """Egy HWID katalógus-keresése. Visszatérés: [(guid, cím, sor_szöveg_kisbetűs,
        dátum_iso)] - a sor_szöveg a teljes <tr> tag-mentesítve (Products oszloppal, a
        pontozáshoz), a dátum a sor "Last Updated" oszlopából (m/d/yyyy -> yyyy-MM-dd)."""
        import urllib.request, urllib.parse
        url = 'https://www.catalog.update.microsoft.com/Search.aspx?q=' + urllib.parse.quote(hwid)
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        html = urllib.request.urlopen(req, context=ssl_ctx, timeout=30).read().decode('utf-8')
        rows = []
        for row_m in re.finditer(r'<tr[^>]*>(.*?)</tr>', html, re.S):
            row_html = row_m.group(1)
            link = re.search(r"id=['\"]([a-fA-F0-9\-]+)_link['\"][^>]*>(.*?)</a>", row_html, re.S)
            if not link:
                continue
            guid = link.group(1)
            title = ' '.join(re.sub(r'<[^>]+>', ' ', link.group(2)).split())
            row_text = ' '.join(re.sub(r'<[^>]+>', ' ', row_html).split())
            date_iso = ''
            dm = re.search(r'\b(\d{1,2})/(\d{1,2})/(\d{4})\b', row_text)
            if dm:
                date_iso = f"{dm.group(3)}-{int(dm.group(1)):02d}-{int(dm.group(2)):02d}"
            rows.append((guid, title, row_text.lower(), date_iso))
        return rows

    def _catalog_find_driver(self, item, installed_info, ssl_ctx):
        """Egy eszköz legjobb katalógus-találatának felkutatása. Az eszköz ÖSSZES
        hardver-azonosítóját végigpróbálja a legspecifikusabbtól (VEN&DEV&SUBSYS&REV) az
        általánosabbig (VEN&DEV) - korábban csak az első HWID-vel kerestünk, és ha arra
        nem volt katalógus-sor, az eszköz driver nélkül maradt, pedig az általánosabb
        azonosítóra lett volna találat. Max 4 azonosítót próbál (hálózat-kímélés).
        Visszatérés: pool-elem dict vagy None."""
        hwids = [h for h in (item.get('all_hwids') or []) if h]
        if item.get('id') and item['id'] not in hwids:
            hwids.insert(0, item['id'])
        import urllib.request
        for hwid in hwids[:4]:
            try:
                logging.debug(f"[CATALOG] Keresés: {item['name']} ({hwid})")
                rows = self._catalog_fetch_rows(hwid, ssl_ctx)
            except Exception as e:
                logging.debug(f"[CATALOG] Lekérdezési hiba ({hwid}): {e}")
                rows = []
            if not rows:
                continue
            # OS/architektúra pontozás - ha minden sor kizárt, visszaesünk a teljes
            # listára (régi viselkedés), mert egy "rossz OS-ű" driver is jobb lehet a semminél.
            scored = [(sc, g, t, d) for (g, t, row_l, d) in rows
                      if (sc := self._catalog_row_score(row_l)) is not None]
            if not scored:
                scored = [(0, g, t, d) for (g, t, _row_l, d) in rows]
            best_score = max(s for s, _g, _t, _d in scored)
            cands = [c for c in scored if c[0] == best_score]
            # A legjobb pontszámúak közül a legmagasabb verziójú sor - a katalógus
            # sor-sorrendje nem garantáltan a legfrissebbel kezd.
            best = cands[0]
            best_ver = _parse_driver_version(best[2])
            for c in cands[1:]:
                v = _parse_driver_version(c[2])
                if v is not None and (best_ver is None or v > best_ver):
                    best, best_ver = c, v
            _bs, best_id, best_title, best_date = best
            # Már telepített (nem újabb) driver kiszűrése: ha az eszköznek van aktív
            # drivere ÉS a katalógus-találat verziója nem magasabb, nem ajánljuk fel.
            # Ilyenkor STOP (nem megyünk általánosabb HWID-re - az ugyanazt adná vissza).
            inst = (installed_info or {}).get((item.get('pnp_id') or '').upper()) or {}
            inst_ver_str = inst.get('version', '')
            inst_ver = _parse_driver_version(inst_ver_str)
            if best_ver is not None and inst_ver is not None and best_ver <= inst_ver:
                logging.debug(f"[CATALOG] Kihagyva (telepített {inst_ver_str} >= katalógus '{best_title}'): {item['name']}")
                return None
            dl_body = f'updateIDs=[{{"size":0,"languages":"","uidInfo":"{best_id}","updateID":"{best_id}"}}]'
            dl_req = urllib.request.Request(
                'https://www.catalog.update.microsoft.com/DownloadDialog.aspx',
                data=dl_body.encode('utf-8'),
                headers={'User-Agent': 'Mozilla/5.0', 'Content-Type': 'application/x-www-form-urlencoded'})
            try:
                dl_html = urllib.request.urlopen(dl_req, context=ssl_ctx, timeout=30).read().decode('utf-8')
            except Exception as e:
                logging.debug(f"[CATALOG] DownloadDialog hiba ({item['name']}): {e}")
                continue
            cab_link = re.search(r'downloadInformation\[0\]\.files\[0\]\.url\s*=\s*[\"\']([^\"\']+)[\"\']', dl_html)
            if cab_link:
                logging.debug(f"[CATALOG] Találat: {item['name']} ('{best_title}') - {cab_link.group(1)[:50]}...")
                return {
                    "name": item['name'], "cat": item['cat'], "hwid": item['id'],
                    "url": cab_link.group(1), "pnp_id": item.get('pnp_id', ''),
                    "installed_version": inst_ver_str,
                    "installed_date": inst.get('date', ''),
                    "wu_title": f"MS Katalógus: {best_title}",
                    "wu_date": best_date,
                }
            # Volt sor, de nincs letöltési link - próbáljuk a következő azonosítót.
        return None

    def _catalog_search_collect(self, devices_to_check, installed_info=None):
        """Microsoft Update Catalog keresés a megadott eszközökre 10 szálon. Az eredményt
        LISTAKÉNT adja vissza (nem nyúl a hw_updates_pool-hoz), így az AutoFix záró
        katalógus-köre is használhatja; a manuális szken a _catalog_search wrapperen át
        appendeli a poolhoz."""
        logging.info(f"[CATALOG] _catalog_search_collect() - {len(devices_to_check)} eszköz ellenőrzése...")
        import ssl
        ssl_ctx = ssl.create_default_context()
        if installed_info is None:
            installed_info = self._get_installed_driver_info()
        found = []
        lock = threading.Lock()
        q = queue.Queue()
        for dev in devices_to_check:
            q.put(dev)

        def cat_worker():
            while not q.empty():
                try:
                    dev = q.get_nowait()
                except Exception:
                    break
                try:
                    hit = self._catalog_find_driver(dev, installed_info, ssl_ctx)
                    if hit:
                        with lock:
                            found.append(hit)
                except Exception as e:
                    logging.debug(f"[CATALOG] Hiba: {dev.get('name')} - {e}")
                q.task_done()

        threads = [threading.Thread(target=cat_worker, daemon=True, name=f"catalog-{i}") for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        logging.info(f"[CATALOG] Kész - {len(found)} eszközre van katalógus-találat")
        return found

    def _catalog_search(self, devices_to_check, installed_info=None):
        """Katalógus-keresés a manuális szkenhez: a találatok a self.hw_updates_pool-ba
        KERÜLNEK HOZZÁ (nem törli a meglévőt, így a hibrid kiegészítő mód is ezt hívja).
        A telepített/naprakész listát a hívó számolja a teljes pool alapján."""
        found = self._catalog_search_collect(devices_to_check, installed_info)
        self.hw_updates_pool.extend(found)

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

        # DISZPÉCSER: a pool a hibrid keresés óta vegyes lehet (WU-s elemek update_id-vel,
        # katalógusosak url-lel), ezért a telepítési módot ELEMENKÉNT döntjük el, nem
        # globálisan - a régi, globális wu_api_mode-alapú elágazás vegyes poolnál a
        # katalógusos elemeket a WU-s útra küldte volna (vagy fordítva).
        if self.target_os_path:
            # A WU API (Microsoft.Update.Session COM) mindig az élő rendszert célozza meg,
            # offline cél-OS esetén ez csendben a host gépre telepítene drivert a kiválasztott
            # offline image helyett - ezért ilyenkor minden elem a dism-alapú katalógus úton megy.
            logging.warning("[WU_INSTALL] Offline cél-OS: minden elem katalógus (DISM) módban települ.")
            self.emit('toast', {'message': '⚠️ Offline célrendszer esetén a WU API mód nem elérhető - katalógus (DISM) módban folytatjuk.', 'type': 'warning'})
            wu_items, cat_items = [], selected_pool
        else:
            wu_items = [d for d in selected_pool if d.get('update_id')]
            cat_items = [d for d in selected_pool if not d.get('update_id')]
        logging.info(f"[WU_INSTALL] {len(selected_pool)} driver telepítése (WU API: {len(wu_items)}, Katalógus: {len(cat_items)})")

        def worker():
            total = len(wu_items) + len(cat_items)
            self.emit('task_start', {'task': 'wu_install', 'title': f'Driver Telepítés ({total} db)'})
            # Az OKRB (újraindítás szükséges) jelzést a _install_wu_api_sync állítja be.
            self._wu_reboot_required = False
            success = fail = 0
            cancelled = False
            # Biztonsági háló a manuális telepítés elé is (az AutoFix eddig is csinálta):
            # gyors visszaállítási pont, mielőtt driverhez nyúlunk. Élő rendszeren fut
            # csak - offline cél-OS-nél a Checkpoint-Computer a HOST gépet mentené.
            if not self.target_os_path:
                self._create_restore_point_sync(task_id='wu_install')
            if wu_items:
                s, f, cancelled = self._install_wu_api_sync(wu_items)
                success += s
                fail += f
            if cat_items and not cancelled:
                if wu_items:
                    self.emit('task_progress', {'task': 'wu_install', 'log': f'\n--- Katalógusos elemek telepítése ({len(cat_items)} db) ---'})
                s, f, cancelled = self._install_catalog_sync(cat_items)
                success += s
                fail += f
            if cancelled:
                self.emit('task_complete', {'task': 'wu_install', 'status': '❗ Megszakítva!', 'success': success, 'fail': fail})
                return
            # ZÁRÓ DriverStore-TAKARÍTÁS: egy frissen telepített driver régi verziója
            # ottmarad a DriverStore-ban - itt azonnal el is takarítjuk (közös mag:
            # dupdrivers_core.auto_cleanup_duplicates, ugyanazokkal a biztonsági
            # szabályokkal, mint a kézi takarító panel). Csak élő rendszeren - offline
            # cél-OS-nél a dup-takarítás nem értelmezett (a hívók mind elutasítják).
            if success > 0 and not self.target_os_path:
                self.emit('task_progress', {'task': 'wu_install', 'log': '\n🧹 DriverStore-takarítás: a lecserélt driverek régi verzióinak törlése...'})
                dupdrivers_core.auto_cleanup_duplicates(
                    self._run,
                    lambda m: self.emit('task_progress', {'task': 'wu_install', 'log': m}),
                    self._get_third_party_drivers,
                    check_cancel=self._check_cancel)
            reboot_needed = getattr(self, '_wu_reboot_required', False)
            msg = f'Kész! Sikeres: {success}, Sikertelen: {fail}'
            if reboot_needed:
                msg += ' — ⚠️ Újraindítás szükséges!'
                self.emit('task_progress', {'task': 'wu_install', 'log': '\n⚠️ Legalább egy driver csak ÚJRAINDÍTÁS után lép életbe!'})
                self.emit('toast', {'message': '⚠️ A telepített driverek egy része csak újraindítás után él!', 'type': 'warning'})
            self.emit('task_complete', {'task': 'wu_install', 'success': success, 'fail': fail, 'status': msg,
                                        'counter': msg, 'reboot_required': reboot_needed})
            # Chipset/USB-vezérlő driver után új eszközök bukkanhatnak elő (az AutoFix
            # ezért megy több körben) - siker esetén a felület felajánlja az új szkennelést.
            if success > 0 and not self.target_os_path:
                self.emit('offer_rescan', {'installed': success})

        self._safe_thread('wu_install', worker)

    def _install_wu_api_sync(self, selected_pool):
        """A kijelölt WU-s (update_id-s) elemek telepítése a KÖZÖS _build_wu_install_ps
        scripttel. A diszpécser (install_selected_wu) worker-szálán fut, task_start/
        task_complete NÉLKÜL. Visszatérés: (sikeres, sikertelen, megszakítva)."""
        logging.info(f"[WU_API] WU API telepítés indítása: {len(selected_pool)} driver")
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
            self.emit('task_progress', {'task': 'wu_install', 'log': '⚠️ Hiányzó azonosítók - futtass új szkennelést!'})
            return 0, 0, False

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
        # A kijelölt elemek nevei + a script FOUND: sorai: a végén ebből derül ki, ha egy
        # kiválasztott driver nem került a telepítési listába (lásd unoffered_requested_titles).
        requested_titles = [str(d.get('title') or d.get('name') or '') for d in selected_pool]
        requested_titles = [t for t in requested_titles if t]
        found_titles = []

        # A sorokat a KÖZÖS _iter_process_lines olvassa (wu_core): cancel-ellenőrzés
        # 0,5 mp-enként (nem csak új sor érkezésekor - régen a Mégse halott volt, ha a
        # scripten belüli WU-keresés beragadt), plusz watchdog: 30 perc néma folyamatot leöl.
        try:
            for line in _iter_process_lines(process, self._run, cancel_check=self._check_cancel):
                if line.startswith("INIT:") or line.startswith("SEARCH:"):
                    self.emit('task_progress', {'task': 'wu_install', 'status': line.split(":", 1)[1].strip(), 'log': line})
                elif line.startswith("FOUND:"):
                    found_titles.append(line[6:].strip())
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
                elif line.startswith("OKRB:"):
                    # Sikeres, de a WUA jelezte: a driver csak újraindítás után él.
                    success += 1
                    self._wu_reboot_required = True
                    done = success + fail
                    self.emit('task_progress', {'task': 'wu_install', 'log': f'  ✅ {line[5:].strip()} (⚠️ újraindítás szükséges)',
                                                'current': done, 'total': install_total, 'counter': f'{done}/{install_total} (✅{success} ❌{fail})'})
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
        except WuProcessAborted as ab:
            if ab.reason == 'cancel':
                self.emit('task_progress', {'task': 'wu_install', 'log': '\n❗ Megszakítva!'})
                return success, fail, True
            had_error = True
            self.emit('task_progress', {'task': 'wu_install',
                                        'log': '\n❌ A Windows Update telepítő 30 percen át nem adott életjelet - a watchdog leállította! '
                                               '(Ez tipikusan beragadt WU szolgáltatásra utal - próbáld újra, vagy a katalógus-találatokat telepítsd.)'})

        # Kijelölt, de a telepítési listába be sem került csomagok - e nélkül némán tűnnének
        # el (a felhasználó 3 drivert jelöl ki, és csak 2-ről lát visszajelzést).
        for t in unoffered_requested_titles(requested_titles, found_titles):
            self.emit('task_progress', {'task': 'wu_install',
                                        'log': f'  ⏭ {t} - a Windows Update már telepítettként látja, nincs mit telepíteni.'})

        if success > 0:
            self.emit('task_progress', {'task': 'wu_install', 'log': 'Eszközök újraszkennelése...', 'status': 'Aktiválás...'})
            self._run(['pnputil', '/scan-devices'])
            self.emit('task_progress', {'task': 'wu_install', 'log': '✅ Eszközök frissítve!'})

        if had_error and success == 0 and fail == 0:
            self.emit('task_progress', {'task': 'wu_install', 'log': '❌ A WU telepítés hibával leállt! (részletek fent a naplóban)'})
        return success, fail, False

    def _install_catalog_sync(self, selected_pool, task_id='wu_install'):
        """A kijelölt katalógusos (url-es) elemek telepítése: cab letöltés -> expand ->
        pnputil /add-driver /install (offline cél-OS-nél dism /Add-Driver); .msu csomagnál
        wusa /quiet (offline: dism /Add-Package); .exe letöltési linket kihagyunk (ismeretlen
        telepítő csendes futtatása kockázatos). A diszpécser worker-szálán fut, task_start/
        task_complete NÉLKÜL; a task_id-vel az AutoFix záró katalógus-köre is használhatja
        ('autofix' progress-csatornán). Visszatérés: (sikeres, sikertelen, megszakítva).
        Megjegyzés: a korábbi változat minden cab-ot KÉTSZER töltött le (egy elavult
        szekvenciális kör + a szálas feldolgozó) - a szekvenciális kör törölve."""
        logging.info(f"[CATALOG_INSTALL] _install_catalog_sync() - {len(selected_pool)} driver (task={task_id})")
        import urllib.request, ssl
        ssl_ctx = ssl.create_default_context()
        total = len(selected_pool)

        temp_dir = os.path.join(os.environ.get('SystemDrive', 'C:') + '\\DV_Temp', 'driverdoktor_wu')
        os.makedirs(temp_dir, exist_ok=True)
        logging.debug(f"[CATALOG_INSTALL] Temp dir: {temp_dir}")
        success = 0
        fail = 0
        skipped = 0
        cancelled = False

        try:
            import concurrent.futures

            counter_lock = threading.Lock()

            def process_catalog_driver(idx, drv):
                nonlocal success, fail, skipped
                if self._check_cancel():
                    return
                name = drv['name']
                url = drv.get('url', '')
                if not url:
                    logging.warning(f"[CATALOG_INSTALL] Kihagyás - nincs URL: {name}")
                    self.emit('task_progress', {'task': task_id, 'log': f'  [KIHAGYÁS] {name} - nincs letöltési link'})
                    with counter_lock:
                        skipped += 1
                    return

                # A katalógus letöltési linkje nem mindig .cab: .msu és .exe is előfordul.
                # A régi kód ezekre is expand-ot futtatott, ami csendben nem csinált semmit,
                # és a telepítés értelmetlen hibával bukott.
                url_file = url.split('?')[0].rsplit('/', 1)[-1].lower()
                file_ext = os.path.splitext(url_file)[1]
                if file_ext == '.exe':
                    logging.warning(f"[CATALOG_INSTALL] Kihagyás - .exe telepítő ({name}): {url[:80]}")
                    self.emit('task_progress', {'task': task_id, 'log': f'  [KIHAGYÁS] {name} - a katalógus .exe telepítőt adott, ezt biztonsági okból nem futtatjuk automatikusan'})
                    with counter_lock:
                        skipped += 1
                    return

                cab_path = os.path.join(temp_dir, f"drv_{idx}{file_ext or '.cab'}")
                ext_path = os.path.join(temp_dir, f"drv_ext_{idx}")

                self.emit('task_progress', {'task': task_id, 'log': f'-> {name} letöltése...'})
                try:
                    logging.debug(f"[CATALOG_INSTALL] Letöltés: {url[:80]}...")
                    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
                    with urllib.request.urlopen(req, context=ssl_ctx, timeout=120) as resp, open(cab_path, 'wb') as f:
                        shutil.copyfileobj(resp, f)
                    logging.debug(f"[CATALOG_INSTALL] Letöltve: {cab_path}")
                except Exception as e:
                    logging.error(f"[CATALOG_INSTALL] Letöltési hiba ({name}): {e}")
                    self.emit('task_progress', {'task': task_id, 'log': f'  ❌ {name} letöltési hiba: {e}'})
                    with counter_lock:
                        fail += 1
                    return

                if file_ext == '.msu':
                    # .msu: wusa csendes telepítés (offline cél-OS-nél dism /Add-Package).
                    self.emit('task_progress', {'task': task_id, 'log': f'  Telepítés (.msu): {name}...'})
                    if self.target_os_path:
                        res = self._run(['dism', f'/Image:{self.target_os_path}', '/Add-Package', f'/PackagePath:{cab_path}'], timeout=1800, ok_codes=(0, 3010))
                        ok = bool(res) and res.returncode in (0, 3010)
                    else:
                        res = self._run(['wusa', cab_path, '/quiet', '/norestart'], timeout=1800, ok_codes=(0, 3010))
                        ok = bool(res) and res.returncode in (0, 3010)
                    with counter_lock:
                        if ok:
                            success += 1
                        else:
                            fail += 1
                    rc = res.returncode if res else '?'
                    self.emit('task_progress', {'task': task_id, 'log': f'  {"✅" if ok else "❌"} {name} (.msu, kód={rc})'})
                    return

                os.makedirs(ext_path, exist_ok=True)
                self._run(['expand', cab_path, '-F:*', ext_path])
                for inner_cab in glob.glob(os.path.join(ext_path, '*.cab')):
                    inner_ext = inner_cab + '_ext'
                    os.makedirs(inner_ext, exist_ok=True)
                    self._run(['expand', inner_cab, '-F:*', inner_ext])

                # Ha a kicsomagolt fában sehol nincs .inf, felesleges a pnputil/dism kör -
                # értelmes hibaüzenettel bukjon (pl. sérült/üres cab).
                has_inf = False
                for _r, _d, files in os.walk(ext_path):
                    if any(fn.lower().endswith('.inf') for fn in files):
                        has_inf = True
                        break
                if not has_inf:
                    logging.error(f"[CATALOG_INSTALL] Nincs .inf a kicsomagolt csomagban: {name}")
                    self.emit('task_progress', {'task': task_id, 'log': f'  ❌ {name} - a letöltött csomagban nincs telepíthető INF (sérült vagy nem driver-csomag)'})
                    with counter_lock:
                        fail += 1
                    return

                self.emit('task_progress', {'task': task_id, 'log': f'  Telepítés: {name}...'})
                is_offline = bool(self.target_os_path)
                if is_offline:
                    cmd = ['dism', f'/Image:{self.target_os_path}', '/Add-Driver', f'/Driver:{ext_path}', '/Recurse']
                else:
                    cmd = ['pnputil', '/add-driver', f"{ext_path}\\*.inf", '/subdirs', '/install']
                res = self._run(cmd)
                # pnputil kimenet: "Added driver packages:  N". Ha N==0, semmi nem települt
                # (a csomag már a store-ban van / up-to-date, kód 259) - ezt TILOS sikernek
                # számolni: az AutoFix katalógus-záróköre soha be nem bind-elő eszközön
                # (pl. kód-28 Ismeretlen Eszköz) minden körben "1 települt"-et jelentene, és a
                # lánc végtelen reboot-loopba kerülne (field-seen: AMDIF031 amdgpio3.inf).
                added_m = re.search(r'Added driver packages?\s*:\s*(\d+)', res.stdout or '', re.IGNORECASE)
                added_zero = added_m is not None and int(added_m.group(1)) == 0
                installed_ok = (res.returncode == 0 or any(k in res.stdout for k in ["Added", "sikeres", "successfully"])) and not added_zero
                if installed_ok:
                    with counter_lock:
                        success += 1
                    self.emit('task_progress', {'task': task_id, 'log': f'  ✅ {name} telepítve!'})
                elif added_zero:
                    with counter_lock:
                        skipped += 1
                    self.emit('task_progress', {'task': task_id, 'log': f'  ↷ {name} már naprakész (nincs új csomag) - kihagyva.'})
                else:
                    with counter_lock:
                        fail += 1
                    self.emit('task_progress', {'task': task_id, 'log': f'  ❌ {name} hiba: {res.stdout[:100]}'})

            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                futures = [executor.submit(process_catalog_driver, i, drv) for i, drv in enumerate(selected_pool)]
                concurrent.futures.wait(futures)

            if self._check_cancel():
                self.emit('task_progress', {'task': task_id, 'log': '\n❗ Megszakítva!'})
                cancelled = True
                return success, fail, cancelled

            if success > 0 and not self.target_os_path:
                self.emit('task_progress', {'task': task_id, 'log': 'Eszközök újraszkennelése és Code 14 újraindítások elvégzése...'})
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
        self.emit('task_progress', {'task': task_id, 'current': total, 'total': total,
                                    'log': f'\n--- Katalógus: Sikeres: {success}, Sikertelen: {fail}' + (f', Kihagyott: {skipped}' if skipped else '') + ' ---'})
        return success, fail, cancelled

    # ================================================================
    # PROBLÉMÁS ESZKÖZÖK - EGYKATTINTÁSOS GYORSJAVÍTÁS
    # ================================================================
    def fix_problem_device(self, pnp_id, code):
        """A "Problémás eszközök" szekció gyorsjavító gombja. Kód-függő akció:
        22 (letiltva) -> Enable-PnpDevice; minden más javítható kódnál (10/14/31/43...)
        disable+enable ciklus (az Eszközkezelő "eszköz újraindítása" megfelelője).
        Szinkron fut (pár másodperc), a _task_busy-t szándékosan nem foglalja - gyors,
        izolált művelet, nem nyúl a hw_updates_pool-hoz. Visszatérés:
        {'ok': bool, 'new_code': int|None, 'error': str} - a toast/megjelenítés a JS dolga."""
        logging.info(f"[API] fix_problem_device({pnp_id!r}, code={code})")
        if self.target_os_path:
            return {'ok': False, 'new_code': None, 'error': 'Offline módban nem elérhető'}
        if not pnp_id:
            return {'ok': False, 'new_code': None, 'error': 'Hiányzó eszköz-azonosító'}
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = 0
        action = 'enable' if code == 22 else 'cycle'
        ps = (f"$id = '{_ps_quote(str(pnp_id))}'\n"
              f"$act = '{action}'\n"
              r"""
try {
    if ($act -eq 'enable') {
        Enable-PnpDevice -InstanceId $id -Confirm:$false -ErrorAction Stop
    } else {
        Disable-PnpDevice -InstanceId $id -Confirm:$false -ErrorAction Stop
        Start-Sleep -Seconds 2
        Enable-PnpDevice -InstanceId $id -Confirm:$false -ErrorAction Stop
    }
    Write-Output "ACTED"
} catch { Write-Output "ERR: $($_.Exception.Message)" }
Start-Sleep -Seconds 3
try {
    $p = (Get-PnpDeviceProperty -InstanceId $id -KeyName 'DEVPKEY_Device_ProblemCode' -ErrorAction Stop).Data
    Write-Output "CODE: $p"
} catch { Write-Output "CODE: ?" }
""")
        try:
            res = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                            encoding='utf-8', timeout=90)
            out = (res.stdout or '')
            acted = 'ACTED' in out
            err_m = re.search(r'ERR:\s*(.+)', out)
            code_m = re.search(r'CODE:\s*(\d+)', out)
            new_code = int(code_m.group(1)) if code_m else None
            error = (err_m.group(1).strip() if err_m else '')
            ok = acted and (new_code == 0 or new_code is None)
            logging.info(f"[FIX-DEVICE] {pnp_id}: acted={acted}, new_code={new_code}, err={error!r}")
            return {'ok': ok, 'new_code': new_code, 'error': error}
        except Exception as e:
            logging.error(f"[FIX-DEVICE] Hiba: {e}")
            return {'ok': False, 'new_code': None, 'error': str(e)}
