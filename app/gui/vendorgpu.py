"""DriverVarázsló GUI - AMD és Intel gyári GPU-driver ellenőrzés (az nvidia.py testvére).

A WU a gyári GPU-driverektől hónapokkal le van maradva. Az NVIDIA-val ellentétben
az AMD és az Intel NEM ad stabil publikus letöltés-lekérdező API-t, ezért itt a
megbízhatóan gépiesen olvasható forrásokból csak a LEGFRISSEBB VERZIÓT állapítjuk
meg, és a hivatalos letöltőoldalt nyitjuk meg (link-out) - telepítőt NEM töltünk
le automatikusan (az AMD/Intel a referer nélküli exe-letöltést blokkolja, és egy
törékeny scrape-elt telepítő-link admin-futtatása kockázatos lenne).

Források:
  - AMD: a GPUOpen-Drivers/amd-vulkan-versions HIVATALOS AMD repo amdversions.xml-je
    (gépileg olvasható; a <windows-version> mező közvetlenül összevethető a telepített
    Win32_VideoController.DriverVersion-nel, pl. "32.0.31021.5001").
  - Intel: a stabil azonosítójú hivatalos letöltőoldal (785597 = Arc & Iris Xe Windows
    driver) szövegéből a legmagasabb "xx.0.101.xxxx" verzió. Csak az új, egyesített
    driver-családot (gen11+ iGPU / Arc, verzió-minta \\d+.0.101.*) ajánljuk - régi
    (legacy) iGPU-ra a friss csomag nem is applikálható.

Minden lépés hibatűrő: hiba esetén csak logol és a kártya nem jelenik meg - a szken
eredményét sosem boríthatja (ugyanaz az elv, mint az nvidia.py-ban)."""

# === AUTO-IMPORTS ===
import os
import re
import time
import json
import logging
from app.common import _app_data_dir
# === /AUTO-IMPORTS ===


AMD_VERSIONS_URL = 'https://raw.githubusercontent.com/GPUOpen-Drivers/amd-vulkan-versions/master/amdversions.xml'
AMD_CACHE_DAYS = 3
AMD_FALLBACK_PAGE = 'https://www.amd.com/en/support/download/drivers.html'

INTEL_DRIVER_PAGE = 'https://www.intel.com/content/www/us/en/download/785597/intel-arc-iris-xe-graphics-windows.html'


def _ver_tuple(v):
    """'32.0.31021.5001' -> (32, 0, 31021, 5001); értelmezhetetlen -> None."""
    try:
        parts = tuple(int(p) for p in str(v).strip().split('.'))
        return parts if len(parts) >= 3 else None
    except Exception:
        return None


class GuiVendorGpuMixin:
    """AMD/Intel gyári GPU-driver ellenőrzés. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def _gpu_controllers(self):
        """A gépben lévő videokártyák listája (név, DriverVersion, PNPDeviceID) -
        virtuális kijelzők kiszűrve. Hiba esetén üres lista."""
        try:
            ps = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                  "Get-WmiObject Win32_VideoController | Select-Object Name, DriverVersion, PNPDeviceID | ConvertTo-Json -Compress")
            res = self._run(["powershell", "-NoProfile", "-Command", ps], encoding='utf-8', timeout=60)
            data = json.loads(res.stdout) if res and (res.stdout or '').strip() else []
            if isinstance(data, dict):
                data = [data]
            out = []
            for gpu in data:
                name = (gpu.get('Name') or '').strip()
                if 'virtual' in name.lower():
                    continue
                out.append({'name': name, 'version': (gpu.get('DriverVersion') or '').strip(),
                            'pnp': (gpu.get('PNPDeviceID') or '').upper()})
            return out
        except Exception as e:
            logging.debug(f"[VENDORGPU] GPU-lista lekérdezési hiba: {e}")
            return []

    # ================================================================
    # AMD
    # ================================================================
    def _amd_fetch_versions_xml(self):
        """Az amdversions.xml letöltése (3 napig cache-elve az app-adatmappában)."""
        import urllib.request
        import ssl
        cache_path = os.path.join(_app_data_dir(), 'amd_versions_cache.xml')
        try:
            if os.path.exists(cache_path) and (time.time() - os.path.getmtime(cache_path)) < AMD_CACHE_DAYS * 86400:
                with open(cache_path, 'r', encoding='utf-8', errors='replace') as f:
                    return f.read()
        except Exception as e:
            logging.debug(f"[VENDORGPU] AMD cache olvasása sikertelen (friss letöltés jön): {e}")
        ssl_ctx = ssl.create_default_context()
        req = urllib.request.Request(AMD_VERSIONS_URL, headers={'User-Agent': 'Mozilla/5.0'})
        xml = urllib.request.urlopen(req, context=ssl_ctx, timeout=20).read().decode('utf-8', errors='replace')
        try:
            with open(cache_path, 'w', encoding='utf-8') as f:
                f.write(xml)
        except Exception as e:
            logging.debug(f"[VENDORGPU] AMD cache írása sikertelen (cache nélkül folytatunk): {e}")
        return xml

    def _amd_latest_driver(self):
        """A legfrissebb Windows-os WHQL AMD driver az amdversions.xml-ből:
        (publikus verzió, windows-verzió, letöltő/release-notes URL, dátum) vagy None-ok.
        A bejegyzéseket release-date szerint rendezzük, nem bízunk a fájlbeli sorrendben."""
        xml = self._amd_fetch_versions_xml()
        entries = []
        for m in re.finditer(r'<driver\s+version="([^"]+)"\s+operating-system="Windows"[^>]*>(.*?)</driver>', xml, re.S):
            pub_ver, body = m.group(1), m.group(2)

            def tag(name):
                t = re.search(rf'<{name}>([^<]*)</{name}>', body)
                return (t.group(1).strip() if t else '')

            entries.append({'public': pub_ver, 'whql': tag('whql'), 'url': tag('download-url'),
                            'win_ver': tag('windows-version'), 'date': tag('release-date')})
        whql = [e for e in entries if e['whql'].upper() == 'WHQL' and _ver_tuple(e['win_ver'])]
        if not whql:
            whql = [e for e in entries if _ver_tuple(e['win_ver'])]
        if not whql:
            return None
        return max(whql, key=lambda e: (e.get('date') or '', _ver_tuple(e['win_ver'])))

    def _check_amd_driver(self):
        """A hardver-szkennelés végén hívódik. Ha van AMD GPU és a gyári driver újabb a
        telepítettnél, 'amd_driver_info' eventtel kártyát küld (link-out). Hibatűrő."""
        try:
            amd = next((g for g in self._gpu_controllers()
                        if 'VEN_1002' in g['pnp'] and ('AMD' in g['name'].upper() or 'RADEON' in g['name'].upper())), None)
            if not amd:
                logging.debug("[AMD] Nincs AMD GPU, ellenőrzés kihagyva.")
                return
            logging.info(f"[AMD] GPU: {amd['name']}, telepített driver: {amd['version'] or 'ismeretlen'}")
            self.emit('hw_scan_progress', {'status': f"🎮 AMD gyári driver ellenőrzése ({amd['name']})..."})

            latest = self._amd_latest_driver()
            if not latest:
                logging.info("[AMD] Nem sikerült a legfrissebb verziót megállapítani.")
                return
            inst_t = _ver_tuple(amd['version'])
            latest_t = _ver_tuple(latest['win_ver'])
            newer = bool(inst_t and latest_t and latest_t > inst_t)
            logging.info(f"[AMD] Telepítve: {amd['version']}, gyári legfrissebb: {latest['public']} "
                         f"({latest['win_ver']}, {latest['date']}), újabb: {newer}")
            self.emit('amd_driver_info', {
                'gpu': amd['name'], 'installed': amd['version'] or '?',
                'latest': latest['public'], 'latest_win_ver': latest['win_ver'],
                'date': latest['date'], 'update_available': newer,
                'url': latest['url'] or AMD_FALLBACK_PAGE,
            })
        except Exception as e:
            logging.warning(f"[AMD] Gyári driver ellenőrzés sikertelen (nem kritikus): {e}")

    # ================================================================
    # INTEL
    # ================================================================
    def _intel_latest_version(self):
        """A legfrissebb egyesített Intel GPU-driver verzió (pl. '32.0.101.8935') a
        hivatalos letöltőoldal szövegéből - a legmagasabb x.0.101.xxxx mintájú számot
        vesszük, mert az oldal a régebbi verziókat is felsorolhatja."""
        import urllib.request
        import ssl
        ssl_ctx = ssl.create_default_context()
        req = urllib.request.Request(INTEL_DRIVER_PAGE, headers={'User-Agent': 'Mozilla/5.0'})
        html = urllib.request.urlopen(req, context=ssl_ctx, timeout=20).read().decode('utf-8', errors='replace')
        best = None
        for m in re.findall(r'\b(\d{2})\.0\.101\.(\d{3,5})\b', html):
            t = (int(m[0]), 0, 101, int(m[1]))
            if best is None or t > best:
                best = t
        return '.'.join(str(p) for p in best) if best else None

    def _check_intel_driver(self):
        """Intel GPU gyári driver ellenőrzés - CSAK az új, egyesített driver-családra
        (telepített verzió x.0.101.* mintájú VAGY Arc kártya): régi (legacy) iGPU-ra a
        friss csomag nem applikálható, ott nem ajánlunk semmit. Hibatűrő."""
        try:
            intel = next((g for g in self._gpu_controllers() if 'VEN_8086' in g['pnp']), None)
            if not intel:
                logging.debug("[INTEL] Nincs Intel GPU, ellenőrzés kihagyva.")
                return
            inst_t = _ver_tuple(intel['version'])
            modern = bool(re.match(r'^\d+\.0\.101\.', intel['version'] or '')) or 'ARC' in intel['name'].upper()
            if not modern:
                logging.info(f"[INTEL] Régi (legacy) driver-család ({intel['version']}) - az egyesített csomag nem applikálható, kihagyva.")
                return
            logging.info(f"[INTEL] GPU: {intel['name']}, telepített driver: {intel['version'] or 'ismeretlen'}")
            self.emit('hw_scan_progress', {'status': f"🎮 Intel gyári driver ellenőrzése ({intel['name']})..."})

            latest = self._intel_latest_version()
            if not latest:
                logging.info("[INTEL] Nem sikerült a legfrissebb verziót megállapítani.")
                return
            latest_t = _ver_tuple(latest)
            newer = bool(inst_t and latest_t and latest_t > inst_t)
            logging.info(f"[INTEL] Telepítve: {intel['version']}, gyári legfrissebb: {latest}, újabb: {newer}")
            self.emit('intel_driver_info', {
                'gpu': intel['name'], 'installed': intel['version'] or '?',
                'latest': latest, 'update_available': newer,
                'url': INTEL_DRIVER_PAGE,
            })
        except Exception as e:
            logging.warning(f"[INTEL] Gyári driver ellenőrzés sikertelen (nem kritikus): {e}")

    def open_vendor_driver_page(self, url):
        """A gyártói letöltőoldal megnyitása az alapértelmezett böngészőben - csak
        http(s) URL-t fogadunk el (a JS-ből jön, de védekezünk)."""
        logging.info(f"[API] open_vendor_driver_page({url})")
        u = str(url or '')
        if not (u.startswith('https://') or u.startswith('http://')):
            return False
        try:
            os.startfile(u)
            return True
        except Exception as e:
            logging.error(f"[VENDORGPU] Oldal megnyitási hiba: {e}")
            return False
