"""DriverVarázsló GUI - NVIDIA gyári driver ellenőrzés és telepítés.

A Windows Update videókártya-driverei tipikusan hónapokkal lemaradnak a gyári
legfrissebbtől. Ez a mixin a hardver-szkennelés végén az NVIDIA hivatalos
lekérdezőjével megnézi, van-e újabb GYÁRI driver a telepítettnél, és ha igen,
külön kártyán ajánlja fel a letöltést + csendes telepítést. Minden lépése
hibatűrő: ha az NVIDIA szolgáltatása nem elérhető vagy a GPU nem azonosítható,
csak logol és eltűnik - a szken eredményét sosem boríthatja.
"""

# === AUTO-IMPORTS ===
import os
import sys
import re
import time
import json
import shutil
import logging
from app.common import _app_data_dir
# === /AUTO-IMPORTS ===


# Az NVIDIA hivatalos, publikus lekérdezői (ugyanezeket használja a nvidia.com
# "Manuális driver keresés" oldala is):
#  - lookup XML: terméklista (pfid = termék-azonosító, ParentID = sorozat psid)
#  - AjaxDriverService: a legfrissebb driver adott pfid/psid/os kombinációra
NV_LOOKUP_URL = 'https://www.nvidia.com/Download/API/lookupValueSearch.aspx?TypeID=3'
NV_DRIVER_URL = ('https://gfwsl.geforce.com/services_toolkit/services/com/nvidia/services/AjaxDriverService.php'
                 '?func=DriverManualLookup&psid={psid}&pfid={pfid}&osID={osid}&languageCode=1033'
                 '&beta=0&isWHQL=1&dltype=-1&dch={dch}&upCRD=0&qnf=0&sort1=0&numberOfResults=1')
NV_LOOKUP_CACHE_DAYS = 7


def _nv_version_from_driverversion(dv):
    """A Windows-os DriverVersion ("32.0.15.6636") átalakítása NVIDIA-verzióvá ("566.36").
    A szabály: az utolsó két mező összefűzve ("15"+"6636"="156636"), annak utolsó 5
    számjegye ("56636"), pont az utolsó 2 előtt -> "566.36"."""
    try:
        parts = str(dv).split('.')
        if len(parts) < 4:
            return None
        raw = (parts[-2] + parts[-1])[-5:]
        if len(raw) < 5 or not raw.isdigit():
            return None
        return f"{raw[:3]}.{raw[3:]}"
    except Exception:
        return None


class GuiNvidiaMixin:
    """NVIDIA gyári driver ellenőrzés/telepítés. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def _nvidia_installed_info(self):
        """A gépben lévő valódi (PCI-s, nem virtuális) NVIDIA GPU neve és a telepített
        driver NVIDIA-verziója. Nincs NVIDIA GPU -> (None, None)."""
        ps = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
              "Get-WmiObject Win32_VideoController | Select-Object Name, DriverVersion, PNPDeviceID | ConvertTo-Json -Compress")
        res = self._run(["powershell", "-NoProfile", "-Command", ps], encoding='utf-8', timeout=60)
        data = json.loads(res.stdout) if res and (res.stdout or '').strip() else []
        if isinstance(data, dict):
            data = [data]
        for gpu in data:
            name = (gpu.get('Name') or '').strip()
            pnp = (gpu.get('PNPDeviceID') or '').upper()
            # Csak valódi NVIDIA PCI GPU (VEN_10DE) - a TeamViewer/USB-s virtuális
            # kijelzők és a "NVIDIA" nevű szoftver-eszközök kizárva.
            if 'NVIDIA' in name.upper() and 'VEN_10DE' in pnp:
                nv_ver = _nv_version_from_driverversion(gpu.get('DriverVersion'))
                return name, nv_ver
        return None, None

    def _nvidia_lookup_ids(self, gpu_name):
        """GPU-név -> (psid, pfid) az NVIDIA lookup XML-ből (7 napig cache-elve az
        app-adatmappában, mert a lista nagy és ritkán változik)."""
        import urllib.request
        import ssl
        cache_path = os.path.join(_app_data_dir(), 'nv_lookup_cache.xml')
        xml = None
        try:
            if os.path.exists(cache_path) and (time.time() - os.path.getmtime(cache_path)) < NV_LOOKUP_CACHE_DAYS * 86400:
                with open(cache_path, 'r', encoding='utf-8', errors='replace') as f:
                    xml = f.read()
        except Exception:
            xml = None
        if not xml:
            ssl_ctx = ssl.create_default_context()
            req = urllib.request.Request(NV_LOOKUP_URL, headers={'User-Agent': 'Mozilla/5.0'})
            xml = urllib.request.urlopen(req, context=ssl_ctx, timeout=20).read().decode('utf-8', errors='replace')
            try:
                with open(cache_path, 'w', encoding='utf-8') as f:
                    f.write(xml)
            except Exception as e:
                logging.debug(f"[NVIDIA] Lookup-cache írása sikertelen (cache nélkül folytatunk): {e}")

        rows = re.findall(r'<LookupValue[^>]*ParentID="(\d+)"[^>]*>.*?<Name>([^<]+)</Name>.*?<Value>(\d+)</Value>',
                          xml, re.S)
        target = gpu_name.upper().replace('NVIDIA', '').strip()
        # Pontos egyezés előnyben; utána a leghosszabb terméknév, ami benne van a GPU
        # nevében (így a "GeForce RTX 3060" nem akad össze a "GeForce RTX 3060 Ti"-vel:
        # a Ti-s gépen mindkettő illik, de a hosszabb - pontosabb - nyer).
        best = None
        best_len = 0
        for psid, prod_name, pfid in rows:
            pn = prod_name.upper().replace('NVIDIA', '').strip()
            if not pn:
                continue
            if pn == target:
                return psid, pfid
            if pn in target and len(pn) > best_len:
                best = (psid, pfid)
                best_len = len(pn)
        return best if best else (None, None)

    def _nvidia_query_latest(self, psid, pfid):
        """A legfrissebb WHQL driver (verzió, letöltési URL) az adott termékre. Először
        DCH (modern) csomagot kér, ha arra nincs találat, standard-ot."""
        import urllib.request
        import ssl
        ssl_ctx = ssl.create_default_context()
        build = getattr(sys.getwindowsversion(), 'build', 0)
        osid = 135 if build >= 22000 else 57  # Windows 11 / Windows 10 64-bit
        for dch in ('1', '0'):
            url = NV_DRIVER_URL.format(psid=psid, pfid=pfid, osid=osid, dch=dch)
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                raw = urllib.request.urlopen(req, context=ssl_ctx, timeout=20).read().decode('utf-8', errors='replace')
                data = json.loads(raw)
                ids = data.get('IDS') or []
                if ids:
                    dl = ids[0].get('downloadInfo') or {}
                    version = (dl.get('Version') or '').strip()
                    dl_url = (dl.get('DownloadURL') or '').strip()
                    if version and dl_url:
                        return version, dl_url
            except Exception as e:
                logging.debug(f"[NVIDIA] Lekérdezés hiba (dch={dch}): {e}")
        return None, None

    def _check_nvidia_driver(self):
        """A hardver-szkennelés végén hívódik. Ha van NVIDIA GPU és a gyári driver újabb
        a telepítettnél, 'nvidia_driver_info' eventtel felajánlja. Minden hibát elnyel."""
        try:
            gpu_name, installed_ver = self._nvidia_installed_info()
            if not gpu_name:
                logging.debug("[NVIDIA] Nincs NVIDIA GPU a gépben, ellenőrzés kihagyva.")
                self._nvidia_offer = None
                return
            logging.info(f"[NVIDIA] GPU: {gpu_name}, telepített driver: {installed_ver or 'ismeretlen'}")
            self.emit('hw_scan_progress', {'status': f'🎮 NVIDIA gyári driver ellenőrzése ({gpu_name})...'})

            psid, pfid = self._nvidia_lookup_ids(gpu_name)
            if not pfid:
                logging.info(f"[NVIDIA] A GPU nem azonosítható az NVIDIA terméklistában: {gpu_name}")
                self._nvidia_offer = None
                return
            latest_ver, dl_url = self._nvidia_query_latest(psid, pfid)
            if not latest_ver:
                logging.info("[NVIDIA] Nem jött használható válasz az NVIDIA drivere-lekérdezőtől.")
                self._nvidia_offer = None
                return

            def _as_tuple(v):
                try:
                    return tuple(int(x) for x in str(v).split('.'))
                except Exception:
                    return None

            newer = True
            iv, lv = _as_tuple(installed_ver), _as_tuple(latest_ver)
            if iv is not None and lv is not None:
                newer = lv > iv
            logging.info(f"[NVIDIA] Telepítve: {installed_ver}, gyári legfrissebb: {latest_ver}, újabb: {newer}")
            if not newer:
                self._nvidia_offer = None
                self.emit('nvidia_driver_info', {'gpu': gpu_name, 'installed': installed_ver or '?',
                                                 'latest': latest_ver, 'update_available': False})
                return

            self._nvidia_offer = {'gpu': gpu_name, 'installed': installed_ver or '?',
                                  'latest': latest_ver, 'url': dl_url}
            self.emit('nvidia_driver_info', {'gpu': gpu_name, 'installed': installed_ver or '?',
                                             'latest': latest_ver, 'update_available': True})
        except Exception as e:
            logging.warning(f"[NVIDIA] Gyári driver ellenőrzés sikertelen (nem kritikus): {e}")
            self._nvidia_offer = None

    def install_nvidia_driver(self):
        """A felajánlott gyári NVIDIA driver letöltése és CSENDES telepítése (-s -noreboot).
        A letöltés nagy (600-800 MB), MB-pontos progress megy a felületre; a telepítő
        futása közben a megszakítás szándékosan nem él (félbevágott GPU-driver telepítés
        rosszabb, mint egy lassú) - a letöltés alatt viszont igen."""
        logging.info("[API] install_nvidia_driver()")
        offer = getattr(self, '_nvidia_offer', None)
        if not offer:
            self.emit('toast', {'message': '⚠️ Nincs felajánlott NVIDIA driver - futtass előbb hardver-szkennelést!', 'type': 'warning'})
            return

        def worker():
            import urllib.request
            import ssl
            self.emit('task_start', {'task': 'nvidia', 'title': f"NVIDIA gyári driver telepítés ({offer['latest']})"})
            temp_dir = os.path.join(os.environ.get('SystemDrive', 'C:') + '\\DV_Temp', 'nvidia')
            os.makedirs(temp_dir, exist_ok=True)
            exe_path = os.path.join(temp_dir, f"nvidia_{offer['latest'].replace('.', '_')}.exe")
            try:
                self.emit('task_progress', {'task': 'nvidia', 'log': f"⬇ Letöltés: {offer['url']}", 'indeterminate': True})
                ssl_ctx = ssl.create_default_context()
                req = urllib.request.Request(offer['url'], headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, context=ssl_ctx, timeout=120) as resp, open(exe_path, 'wb') as f:
                    total = int(resp.headers.get('Content-Length') or 0)
                    done = 0
                    last_pct = -1
                    while True:
                        if self._check_cancel():
                            raise Exception("Magyar_Megszakit_Flag")
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if total:
                            pct = int(done * 100 / total)
                            if pct != last_pct:
                                last_pct = pct
                                self.emit('task_progress', {'task': 'nvidia', 'current': done // (1024 * 1024),
                                                            'total': total // (1024 * 1024),
                                                            'counter': f'{done // (1024*1024)} / {total // (1024*1024)} MB',
                                                            'status': f'⬇ Letöltés: {pct}%'})
                self.emit('task_progress', {'task': 'nvidia', 'log': f'✅ Letöltve ({done // (1024*1024)} MB). Csendes telepítés indul - ez több percig tarthat, a képernyő villoghat!', 'indeterminate': True})

                res = self._run([exe_path, '-s', '-noreboot'], timeout=2400)
                if res and res.returncode in (0, 1):  # 1 = kész, de újraindítás javasolt
                    self.emit('task_progress', {'task': 'nvidia', 'log': f"✅ NVIDIA driver {offer['latest']} telepítve!" + (' (Újraindítás javasolt.)' if res.returncode == 1 else '')})
                    self.emit('task_complete', {'task': 'nvidia', 'status': f"✅ NVIDIA {offer['latest']} telepítve!"})
                    self._nvidia_offer = None
                    self.emit('nvidia_driver_info', {'gpu': offer['gpu'], 'installed': offer['latest'],
                                                     'latest': offer['latest'], 'update_available': False})
                else:
                    rc = res.returncode if res else '?'
                    self.emit('task_progress', {'task': 'nvidia', 'log': f'❌ A telepítő {rc} kóddal tért vissza - részletek az NVIDIA telepítési naplójában.'})
                    self.emit('task_complete', {'task': 'nvidia', 'status': f'❌ NVIDIA telepítő hibakód: {rc}'})
            except Exception as e:
                if str(e) == "Magyar_Megszakit_Flag":
                    self.emit('task_complete', {'task': 'nvidia', 'status': '❗ Megszakítva!'})
                else:
                    logging.error(f"[NVIDIA] Telepítési hiba: {e}", exc_info=True)
                    self.emit('task_error', {'task': 'nvidia', 'error': str(e)})
                    self.emit('task_complete', {'task': 'nvidia', 'status': f'❌ Hiba: {e}'})
            finally:
                try:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                except Exception as e:
                    logging.debug(f"[NVIDIA] Temp mappa törlése sikertelen: {e}")

        self._safe_thread('nvidia', worker)
