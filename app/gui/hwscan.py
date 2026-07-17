"""DriverVarázsló GUI - Driver Keresés és Telepítés nézet: hardver-szken, WU/Catalog keresés, kiválasztott driverek telepítése."""

# === AUTO-IMPORTS ===
import os
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
from app.wu_core import WU_PNP_QUERY_PS
from app.wu_core import _build_wu_install_ps
from app.wu_core import _filter_wu_scan_devices
from app.wu_core import _match_wu_updates_to_devices
# === /AUTO-IMPORTS ===


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
