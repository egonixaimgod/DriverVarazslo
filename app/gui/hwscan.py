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
from app.common import _ps_quote, _app_data_dir
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
from app.wu_core import is_newer_release
from app.wu_core import release_rank
from app.wu_core import base_vendor_hwid
from app.wu_core import mark_generic_replace_candidates
from app.wu_core import deep_catalog_candidates as wu_core_deep_candidates
from app.wu_core import inf_package_applies
from app.wu_core import unoffered_requested_titles
from app.wu_core import is_specific_hwid
from app.wu_core import driver_model_rank
from app.wu_core import catalog_title_family
from app.wu_core import package_bound_to_device_family
from app.wu_core import is_composite_parent
from app.wu_core import device_risk_marker
from app.wu_core import mark_device_risk
from app.wu_core import is_firmware_update
from app.wu_core import FIRMWARE_CLASS_LABEL
from app.wu_core import FIRMWARE_CLASS_WARNING
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


# --- Microsoft Update Catalog: lapozás, rendezés, holtverseny-kezelés ---
#
# A katalógus laponként 25 sort ad (mérve: "1 - 25 of 546 (page 1 of 22)"), a lapozás
# és a rendezés pedig sima query-paraméter - a nevek a katalógus saját
# SiteConstants.aspx-éből: QueryStringPageIndex='p', QueryStringSortColumn='scol',
# QueryStringSortDirection='sdir'. Rendezhető oszlopok (a fejléc data-columnName-jei):
# Title, Products, ClassificationComputed, DateComputed, DriverVerVersion, SizeInBytes.
CATALOG_PAGE_SIZE = 25
# Legfeljebb ennyi lapot kérünk le EGY HWID-re. A dátum szerinti rendezés miatt az 1. lap
# már a legfrissebb 25 sort tartalmazza; a többi lap csak akkor kell, ha a legfrissebb
# dátumú holtverseny átlóg a lap végén. 3 lap = 75 sor, bőven fedi a mért eseteket.
CATALOG_MAX_PAGES = 3
CATALOG_SORT_QS = '&scol=DateComputed&sdir=desc'
# Ennyi holtverseny-jelöltnél kérjük le a részletlapot a "Driver Model" mezőért
# (letöltés előtti, pár KB-os alkalmasság-jelzés - lásd _catalog_driver_models). PONTOS
# névegyezésnél azonnal megállunk, tehát a jó esetben ennél jóval kevesebb kérés fut. A
# felső korlát azért kell, mert a holtverseny nagy is lehet (mérve: 45 sor a Realtek
# NIC-re) - viszont bőven megéri: pár KB-os kérésekkel kerülünk el egy rossz, akár
# 1,2 GB-os letöltést, és csak azoknál az eszközöknél fut, ahol tényleg telepítenénk.
CATALOG_MODEL_PROBE_MAX = 10
# Ha az INF-vizsgálat elveti a nyertes csomagot, ennyi TARTALÉK jelöltet próbálunk még
# (a nyertessel együtt ennyi letöltés lehet összesen). Csak azonos dátumú jelöltek
# jönnek szóba, tehát a tartalék sosem lehet régebbi kiadás - lásd _catalog_find_driver.
CATALOG_MAX_CANDIDATES = 3


class GuiHwScanMixin:
    """Driver Keresés és Telepítés nézet: hardver-szken, WU/Catalog keresés, kiválasztott driverek telepítése. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def start_hw_scan(self, deep=True):
        """Hardver-szken. deep=True (alapértelmezés): a Microsoft Update Catalogot MINDEN
        olyan eszközre megkérdezzük, amire a WU Agent nem adott ajánlatot - nem csak a
        hibakódosakra és a Windows-alapdriveren futókra. Lassabb (eszközönként max 4 HTTP
        lekérdezés, 10 szálon), cserébe ez az egyetlen mód, amivel egy RÉGI, de hibátlanul
        működő gyári driver is frissülni tud. deep=False: a korábbi, szűk kiegészítés."""
        logging.info(f"[API] start_hw_scan(deep={deep}) hívás")
        deep = bool(deep)
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
                _start = time.monotonic()
                
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
                    # KOCKÁZATI JELÖLÉS A WU-TALÁLATOKRA IS (2026-07-28). A manuális szken
                    # szerződése: tároló-/firmware-találat PIROSAN és ELŐRE BE NEM JELÖLVE
                    # jelenik meg, mert emberi döntést kíván. Ezt eddig CSAK a mély
                    # katalógus-szken tette rá (deep_catalog_candidates), így ugyanaz az
                    # NVMe-vezérlő pirosan VAGY némán, előre bepipálva jelent meg attól
                    # függően, melyik forrás találta meg - egy "mindet telepít" kattintás
                    # pedig csendben tett fel boot-kritikus drivert.
                    # A csomag-szintű firmware-vizsgálat sem elhagyható: egy SSD-firmware a
                    # TÁROLÓVEZÉRLŐHÖZ, egy dokkoló-firmware egy USB-eszközhöz párosul,
                    # tehát az eszközosztály önmagában nem fogná meg (lásd
                    # wu_core.filter_firmware_updates ugyanezt az AutoFix oldalán).
                    risky, risk_label, risk_reason = device_risk_marker(dev)
                    if not risky and is_firmware_update(wu_by_uid.get(m['uid']) or {}):
                        risky, risk_label, risk_reason = True, FIRMWARE_CLASS_LABEL, FIRMWARE_CLASS_WARNING
                    if risky:
                        logging.warning(f"[HW_SCAN] KOCKÁZATOS WU-találat, előre BE NEM jelölve: "
                                        f"{dev['name']} [{dev.get('pclass') or '?'}] - '{m['title']}'")
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
                        "update_id": m['uid'],
                        "risky": risky, "risk_label": risk_label, "risk_reason": risk_reason
                    })
                # A párosítatlan (ghost) WU-találatok kimaradnak a poolból
                for wu in wu_results:
                    if wu.get('UpdateID') not in matched_uids:
                        logging.debug(f"[WU_API] Ghost / Unmatched eszköz kihagyva: {wu.get('Title')}")

                # "GYÁRI DRIVER A GENERIKUS HELYETT": megjelöljük azokat az eszközöket,
                # amik a Windows beépített driverén futnak, pedig a chipgyártónak van
                # sajátja. A jelölést a KÖZÖS wu_core.mark_generic_replace_candidates
                # végzi - ugyanez fut az AutoFix katalógus-zárókörében is, hogy a két út
                # pontosan ugyanazokat az eszközöket találja meg.
                # allow_storage/allow_firmware=True: a MANUÁLIS szken - ahogy a hibakódos és
                # a mély körben is - megkeresi a kockázatos eszközökre is a gyári drivert,
                # de a találat pirosan és ELŐRE BE NEM JELÖLVE jelenik meg. Itt ember dönt.
                generic_devs = mark_generic_replace_candidates(
                    devices_to_check, inst_info, allow_storage=True, allow_firmware=True)
                if generic_devs:
                    logging.info(f"[CATALOG] Generikus driveren futó eszközök: {[d['name'] for d in generic_devs]}")

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
                    # A hibakódos eszközök mellé a generikus driveren futók is bekerülnek:
                    # ezekre a WU szerint "minden rendben" (ezért nem ajánl semmit), a
                    # katalógusban viszont ott a chipgyártó csomagja.
                    # A hibakódos ág eszközei is átmennek a KÖZÖS kockázati jelölőn
                    # (mark_device_risk): egy hibakódos tárolóvezérlő/firmware-eszköz eddig
                    # jelöletlenül, tehát PIROS FIGYELMEZTETÉS NÉLKÜL és ELŐRE BEJELÖLVE
                    # került a listára - miközben a mély szken ugyanazt az eszközt pirossal
                    # hozta. A jelölés így nem attól függ, melyik ág találta meg.
                    leftover = [mark_device_risk(d) for d in devices_to_check
                                if d.get('err_code') and d['id'] not in matched_hwids]
                    # MÉLY SZKEN (deep=True, alapértelmezés): a katalógust MINDEN olyan
                    # eszközre megkérdezzük, amire a WU nem adott ajánlatot - nem csak a
                    # hibakódosakra és a generikus driveresekre. Enélkül egy eszköz, ami
                    # hibátlanul fut egy RÉGI gyári driveren, sosem kapott újabbat: a WU
                    # szerint rendben van, inbox-jelölt nem lévén a katalógust meg se
                    # kérdeztük rá. A csomagok szűrése változatlan (a _catalog_find_driver
                    # verzió-kapuja csak SZIGORÚAN újabb csomagot enged át), tehát a mély
                    # szken nem hoz downgrade-et, csak lefedettséget.
                    # include_risky=True: a MANUÁLIS szken a tárolóvezérlő/lemez drivereket is
                    # megkeresi (az AutoFix soha) - de `risky` jelzővel, piros
                    # figyelmeztetéssel és ELŐRE BE NEM JELÖLVE. Itt ember dönt, és a
                    # szerelőnek látnia kell, HOGY LÉTEZIK csomag, még ha a telepítése
                    # mérlegelendő is. Lásd wu_core.DEEP_CATALOG_RISKY_CLASSES.
                    rest = wu_core_deep_candidates(
                        [d for d in devices_to_check if d['id'] not in matched_hwids],
                        inst_info, include_risky=True, include_firmware=True) if deep else []
                    todo, todo_ids = [], set()
                    for d in leftover + generic_devs + rest:
                        if d['id'] in matched_hwids or d['id'] in todo_ids:
                            continue
                        todo_ids.add(d['id'])
                        todo.append(d)
                    if todo:
                        parts = []
                        if leftover:
                            parts.append(f'{len(leftover)} problémás')
                        if generic_devs:
                            parts.append(f'{len(generic_devs)} generikus driveres')
                        primary_ids = {d['id'] for d in leftover + generic_devs}
                        extra = sum(1 for d in todo if d['id'] not in primary_ids)
                        if extra:
                            parts.append(f'{extra} mélykeresés')
                        self.emit('hw_scan_progress', {'status': f'🌐 Katalógus-kiegészítés ({" + ".join(parts)} eszköz)...'})
                        self._catalog_search(todo, installed_info=inst_info)

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

                elapsed = int(time.monotonic() - _start)
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
            # A 'autofix' csatornára CSAK akkor írunk, ha tényleg AutoFix fut: kézi
            # szkennelésnél ez a sor a logban ([EMIT:]) az AutoFix-hez tartozónak látszott,
            # és egy terepi bejelentés kivizsgálásakor pont ez viszi félre a nyomot.
            if getattr(self, '_task_busy', None) == 'autofix' or getattr(self, 'resume_mode', False) or getattr(self, 'resume_step1', False):
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

    @staticmethod
    def _catalog_parse_rows(html):
        """A találati lap sorainak kibontása: [(guid, cím, sor_szöveg_kisbetűs, dátum_iso)].
        A sor_szöveg a teljes <tr> tag-mentesítve (Products oszloppal, a pontozáshoz), a
        dátum a "Last Updated" oszlopból (m/d/yyyy -> yyyy-MM-dd). Külön (statikus)
        függvény, hogy egy elmentett lapon offline is tesztelhető legyen."""
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

    def _catalog_fetch_rows(self, hwid, ssl_ctx, max_pages=CATALOG_MAX_PAGES):
        """Egy HWID katalógus-keresése, DÁTUM SZERINT CSÖKKENŐ sorrendben, szükség esetén
        LAPOZVA. Visszatérés: [(guid, cím, sor_szöveg_kisbetűs, dátum_iso)].

        MIÉRT NEM ELÉG EGY LAPOT LEKÉRNI (terepi log + élő mérés, 2026-08-06):
        a katalógus laponként 25 sort ad, a régi kód pedig egyetlen, RENDEZETLEN lapot
        kért le - vagyis a döntésünk azon a 25 soron állt, amit a szerver épp elsőnek
        adott. Mérve ugyanezen a gépen:
            PCI\\VEN_10DE&DEV_2504            -> "1 - 25 of 546"   (22 lap)
            HDAUDIO\\FUNC_01&VEN_10EC&DEV_0892 -> "1 - 25 of 1000"  (40 lap)
        tehát a videokártyára a csomagok 4,6%-át láttuk. Rosszabb: a rendezetlen lap
        ÖSSZETÉTELE nem állandó. A 2026-08-05-i futásban ugyanarra a hangeszközre, ugyanazzal
        a 3 HWID-del, négy körben KÉT KÜLÖNBÖZŐ nyertes jött ki (6.0.9992.1 [2026-05-18]
        háromszor, 6.0.10007.1 [2026-06-22] egyszer) - a nálunk 5 héttel frissebb csomag
        háromszor egyszerűen nem került bele a mintába. Vagyis a "legfrissebb dátum nyer"
        szabály helyes volt, csak nem a teljes listára alkalmaztuk.

        A megoldás nem 22 lap letöltése (91 eszköz × 4 HWID mellett az kezelhetetlen),
        hanem a SZERVER OLDALI RENDEZÉS: a lapozás egyszerű GET (`&p=<0-alapú lapindex>`),
        a rendezés `&scol=DateComputed&sdir=desc` - mindkét paraméternév a katalógus saját
        SiteConstants.aspx-éből való (QueryStringPageIndex='p', QueryStringSortColumn='scol').
        Így az 1. lap MÁR a 25 legfrissebb sort tartalmazza, ami pontosan az, amire a
        dátum-elsődlegű döntésnek szüksége van. Következő lapot csak akkor kérünk, ha a
        legfrissebb dátumú csoport ÁTLÓG a lap végén (különben a holtverseny egy részét
        nem látnánk) - a mért két esetben ez 1 lapot jelent.

        A rendezés determinisztikus: háromszor egymás után lekérve azonos az eredmény
        (mérve). Ha a rendezett kérés bármiért elhasal, visszaesünk a rendezetlenre -
        az a régi viselkedés, nem rosszabb a mainál."""
        import urllib.request, urllib.parse
        base = ('https://www.catalog.update.microsoft.com/Search.aspx?q='
                + urllib.parse.quote(hwid))

        def fetch(url):
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            return urllib.request.urlopen(req, context=ssl_ctx, timeout=30).read().decode('utf-8')

        try:
            html = fetch(base + CATALOG_SORT_QS)
            sorted_ok = True
        except Exception as e:
            # A rendezés elvesztése nem végzetes: a régi (rendezetlen) viselkedést kapjuk.
            logging.debug(f"[CATALOG] A rendezett lekérdezés elhasalt ({hwid}): {e} - rendezetlenül próbáljuk.")
            html = fetch(base)
            sorted_ok = False

        rows = self._catalog_parse_rows(html)
        total_m = re.search(r'(\d+)\s*-\s*(\d+)\s+of\s+(\d+)', html)
        total = int(total_m.group(3)) if total_m else len(rows)
        pages, stop = 1, 'egy lap elég'
        if sorted_ok and rows:
            top_date = rows[0][3] or ''
            # Amíg a lap UTOLSÓ sora is a legfrissebb dátumú csoportba tartozik, a csoport
            # átlóghat a következő lapra - azt is le kell kérni, különben a holtverseny
            # egy részét (és talán épp az eszközhöz valót) nem is látjuk.
            while (pages < max_pages and len(rows) >= CATALOG_PAGE_SIZE * pages
                   and (rows[-1][3] or '') == top_date and len(rows) < total):
                try:
                    more = self._catalog_parse_rows(fetch(f"{base}{CATALOG_SORT_QS}&p={pages}"))
                except Exception as e:
                    stop = f'a {pages + 1}. lap nem jött le ({e})'
                    break
                if not more:
                    stop = f'a {pages + 1}. lap üres'
                    break
                seen = {r[0] for r in rows}
                rows += [r for r in more if r[0] not in seen]
                pages += 1
            else:
                if pages >= max_pages:
                    stop = f'lapkorlát ({max_pages})'
        msg = (f"[CATALOG] Lekérdezés: {hwid} -> {len(rows)} sor "
               f"(a katalógusban összesen {total}, {pages} lap, "
               f"{'dátum szerint csökkenő' if sorted_ok else 'RENDEZETLEN'}; {stop})")
        # Eszközönként max 4 ilyen hívás fut, 91 eszközre ez több száz sor lenne INFO-n
        # (a forgó log tele futna vele - lásd CLAUDE.md "hot loops"), ezért alapból DEBUG.
        # Ami viszont ritka és érdekes: ha tényleg lapoztunk, vagy ha a rendezés kiesett.
        (logging.info if (pages > 1 or not sorted_ok) else logging.debug)(msg)
        return rows

    def _catalog_driver_models(self, guid, ssl_ctx):
        """Egy katalógus-tétel részletlapjáról a "Driver Model" mező (a TÁMOGATOTT
        ESZKÖZÖK neve), kisbetűsen. Üres string, ha nincs vagy nem jött le.

        Miért éri meg egy külön kérés: a részletlap pár KB, a csomag viszont akár 1,2 GB.
        A 2026-08-05-i futásban a videokártyára 10 azonos című, azonos dátumú sor volt
        holtversenyben ('NVIDIA Display Driver Update (32.0.15.9595)'), a program vaktában
        vitte el az egyiket, 1,2 GB-ot töltött, és az INF-vizsgálat kiderítette, hogy a
        csomag nem ismeri ezt a kártyát. A részletlap viszont NÉVSZERINT felsorolja:
        "Driver Model: NVIDIA GeForce RTX 3090,...,NVIDIA GeForce RTX 3060,..." - ez az
        egyetlen olyan adat, amiből LETÖLTÉS ELŐTT eldönthető, melyik holtverseny-sor való
        ehhez a géphez."""
        import urllib.request
        try:
            req = urllib.request.Request(
                'https://www.catalog.update.microsoft.com/ScopedViewInline.aspx?updateid=' + guid,
                headers={'User-Agent': 'Mozilla/5.0'})
            html = urllib.request.urlopen(req, context=ssl_ctx, timeout=30).read().decode('utf-8')
        except Exception as e:
            logging.debug(f"[CATALOG] Részletlap nem jött le ({guid}): {e}")
            return ''
        text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ',
                                          re.sub(r'<script.*?</script>', '', html, flags=re.S)))
        m = re.search(r'Driver Model:\s*(.*?)\s*(?:Driver Provider|Driver Version|Driver Class|'
                      r'Supported products|Supported languages|Company|Architecture|Classification|$)', text)
        return (m.group(1) if m else '').strip().lower()

    def _catalog_download_url(self, guid, ssl_ctx, name=''):
        """A letöltési link feloldása egy katalógus-tétel GUID-jából (DownloadDialog).
        Külön függvény, mert a holtverseny-tartalék (lásd _install_catalog_sync) is ezen
        keresztül kéri le a KÖVETKEZŐ jelölt URL-jét, amikor az elsőt az INF-vizsgálat
        elvetette. None, ha nem sikerült."""
        import urllib.request
        dl_body = f'updateIDs=[{{"size":0,"languages":"","uidInfo":"{guid}","updateID":"{guid}"}}]'
        dl_req = urllib.request.Request(
            'https://www.catalog.update.microsoft.com/DownloadDialog.aspx',
            data=dl_body.encode('utf-8'),
            headers={'User-Agent': 'Mozilla/5.0', 'Content-Type': 'application/x-www-form-urlencoded'})
        try:
            dl_html = urllib.request.urlopen(dl_req, context=ssl_ctx, timeout=30).read().decode('utf-8')
        except Exception as e:
            logging.debug(f"[CATALOG] DownloadDialog hiba ({name or guid}): {e}")
            return None
        cab_link = re.search(r'downloadInformation\[0\]\.files\[0\]\.url\s*=\s*[\"\']([^\"\']+)[\"\']', dl_html)
        return cab_link.group(1) if cab_link else None

    @staticmethod
    def _catalog_row_is_microsoft(title):
        """Microsoft saját csomagja-e a katalógus-sor? A katalógusban a cím a
        szolgáltató nevével kezdődik ("Realtek Semiconductor Corp. - MEDIA - ..."),
        külön provider-oszlop nincs. Inbox driver cseréjénél egy Microsoft-csomag
        nem hoz semmit (az van fent), ezért az ilyen sorokat kihagyjuk."""
        return (title or '').strip().lower().startswith('microsoft')

    def _catalog_find_driver(self, item, installed_info, ssl_ctx, known_no_bind=None):
        """Egy eszköz legjobb katalógus-találatának felkutatása.

        known_no_bind: {PNP_ID_NAGYBETŰS: {korábban megbukott katalógus-GUID-ok}} - a
        tartós no-bind tárból, EGYSZER beolvasva a hívóban (nem szálanként/eszközönként).

        Az eszköz ÖSSZES hardver-azonosítóját lekérdezi a legspecifikusabbtól
        (VEN&DEV&SUBSYS&REV) az általánosabbig (VEN&DEV), max 4-et (hálózat-kímélés),
        és a sorokat EGY HALMAZBA gyűjti, abból választ.

        Miért unió, és miért nem áll meg az első találó HWID-nél: mérve (2026-07-24,
        Realtek ALC892) a specifikus és az általános azonosító MÁS csomagot ad, és
        történetesen a specifikus a régebbit:
            HDAUDIO\\...&DEV_0892&SUBSYS_18496893 -> 6.0.9136.1  (2021-03-22)
            HDAUDIO\\...&DEV_0892                 -> 6.0.9992.1  (2026-05-18)
        A régi kód az első találó azonosítónál megállt, tehát a 2021-es drivert
        választotta volna, és a kommentje szerint az általánosabb HWID "ugyanazt adná
        vissza" - ez tévedés volt.

        Visszatérés: pool-elem dict vagy None."""
        hwids, seen_hwid, generic_skipped = [], set(), []
        for h in ([item['id']] if item.get('id') else []) + list(item.get('all_hwids') or []):
            hl = (h or '').strip().lower()
            if not h or hl in seen_hwid:
                continue
            seen_hwid.add(hl)
            # TÍPUSKÓDDAL NEM KÉRDEZÜNK (wu_core.is_specific_hwid). Egy ACPI\PNP0501
            # ("soros port") vagy USB\ROOT_HUB30 kulcsra a katalógus BÁRMELYIK gyártó
            # arra a fajtára szánt csomagját visszaadja - terepen mérve egy Intel gép
            # USB-gyökérhubjára így jött vissza egy AMD-csomag, egy COM-portra pedig egy
            # LG-s. Az eszköz maga NINCS kizárva: a specifikus azonosítóival kérdezzük.
            if not is_specific_hwid(h):
                generic_skipped.append(h)
                continue
            hwids.append(h)
        if not hwids:
            # Nincs egyetlen konkrét azonosító sem - a keresés értelmetlen lenne. Ezt ki
            # KELL írni, különben a "miért nem kapott az X eszköz drivert?" kérdésre nincs
            # válasz a terepi logban (CLAUDE.md Rule 0).
            logging.debug(f"[CATALOG] Kihagyva (csak típuskódos azonosítói vannak, "
                          f"azokra bármely gyártó csomagja illeszkedne): {item['name']} {generic_skipped}")
            return None
        # A gyártó+eszköz TÖRZS-azonosító pótlása (SUBSYS/REV/CC nélkül): az eszköz saját
        # HWID-listája sokszor csak alrendszer-kötött ID-ket tartalmaz, a gyártó friss
        # csomagja viszont a törzsön van indexelve (lásd wu_core.base_vendor_hwid).
        # A keresés így is max 4 lekérdezés marad, de a törzs mindig köztük van.
        hwids = hwids[:4]
        # A törzs is átmegy a típuskód-vizsgálaton: az item['id'] lehet általános
        # azonosító is, és egy típuskódos törzzsel ugyanúgy más gyártó csomagját hoznánk be.
        base = base_vendor_hwid(hwids[0] if hwids else '')
        if base and is_specific_hwid(base) and base.lower() not in {h.lower() for h in hwids}:
            hwids = hwids[:3] + [base]

        inst = (installed_info or {}).get((item.get('pnp_id') or '').upper()) or {}
        inst_ver_str = inst.get('version', '')
        inst_ver = _parse_driver_version(inst_ver_str)
        # "Gyári driver a generikus helyett": CSAK a mark_generic_replace_candidates
        # által megjelölt eszközöknél lép életbe (lásd ott, hogy miért nem globális).
        replace_inbox = bool(item.get('generic_ok')) and _is_inbox_driver(inst)

        rows_by_guid = {}
        for hwid in hwids[:4]:
            try:
                logging.debug(f"[CATALOG] Keresés: {item['name']} ({hwid})")
                for (g, t, row_l, d) in self._catalog_fetch_rows(hwid, ssl_ctx):
                    rows_by_guid.setdefault(g, (t, row_l, d))
            except Exception as e:
                logging.debug(f"[CATALOG] Lekérdezési hiba ({hwid}): {e}")
        if not rows_by_guid:
            return None

        # OS/architektúra pontozás - ha minden sor kizárt, visszaesünk a teljes
        # listára (régi viselkedés), mert egy "rossz OS-ű" driver is jobb lehet a semminél.
        all_rows = [(g, t, row_l, d) for g, (t, row_l, d) in rows_by_guid.items()]
        scored = [(sc, g, t, d) for (g, t, row_l, d) in all_rows
                  if (sc := self._catalog_row_score(row_l)) is not None]
        if not scored:
            scored = [(0, g, t, d) for (g, t, _row_l, d) in all_rows]
        if replace_inbox:
            vendor_rows = [c for c in scored if not self._catalog_row_is_microsoft(c[2])]
            if not vendor_rows:
                logging.debug(f"[CATALOG] {item['name']}: csak Microsoft-csomag van a katalógusban, a generikus csere értelmetlen - kihagyva.")
                return None
            scored = vendor_rows

        best_score = max(s for s, _g, _t, _d in scored)
        cands = [c for c in scored if c[0] == best_score]
        # KORÁBBAN MÁR MEGBUKOTT CSOMAGOK KIHAGYÁSA: amit egy előző futásban ugyanerre az
        # eszközre letöltöttünk és az INF-vizsgálat elvetett, azt nem töltjük le újra
        # (egy videokártya-csomag 1,2 GB). A jelölést a tartós no-bind tár őrzi, GUID
        # szerint - a cím ehhez kevés, mert a katalógusban 10 azonos című sor is lehet.
        bad_guids = (known_no_bind or {}).get((item.get('pnp_id') or '').upper()) or set()
        if bad_guids:
            usable = [c for c in cands if c[1] not in bad_guids]
            if usable and len(usable) != len(cands):
                logging.debug(f"[CATALOG] {item['name']}: {len(cands) - len(usable)} jelölt kihagyva "
                              f"(korábban letöltöttük, és nem erre az eszközre való volt).")
                cands = usable
        # A legjobb pontszámúak közül a LEGFRISSEBB DÁTUMÚ sor nyer, és csak azonos
        # dátumnál dönt a verziószám. (A katalógus sor-sorrendje nem newest-first.)
        #
        # Miért a dátum az elsődleges: ugyanaz a gyártó ugyanarra az eszközre több,
        # egymással összehasonlíthatatlan verziósémát is használ. Mérve a gépen, a
        # Realtek NIC-re: "Realtek - Net - 1168.19.704.2024" (2024-07-03) és
        # "Realtek Net Driver Update (10.79.50.1003)" (2025-10-02). Verzió szerint az
        # 1168.19.704.2024 "nyerne" - pedig több mint egy évvel régebbi csomag. A
        # kiadási dátum viszont mindkét sémán át értelmes, és a két terepi esetben
        # (Realtek audio + Realtek LAN) is a helyes csomagot választja.
        ordered = sorted(cands, key=lambda c: ((c[3] or ''), _parse_driver_version(c[2]) or ()),
                         reverse=True)
        best = ordered[0]
        best_ver = _parse_driver_version(best[2])
        _bs, best_id, best_title, best_date = best
        # EGY összegző sor a teljes választásról (a soronkénti pontozás szándékosan nem
        # logol - lásd common._CALL_LOG_EXCLUDE). Ebből visszafejthető, MIÉRT ez a csomag
        # nyert: hány sorból, hány HWID-ről, milyen pontszámmal, és mik voltak a közeli
        # versenytársak (cím + dátum). Egy rossz választásnál pontosan ez a sor kell.
        rivals = ', '.join(f"{t[:40]}|{d or '?'}" for _s, _g, t, d in
                           sorted(cands, key=lambda c: (c[3] or ''), reverse=True)[1:4])
        logging.info(f"[CATALOG] Döntés: {item['name']} - {len(rows_by_guid)} sor / {len(hwids[:4])} HWID, "
                     f"legjobb pont={best_score}, {len(cands)} holtverseny -> NYERTES: '{best_title}' "
                     f"[{best_date or '?'}] v={best_ver}" + (f" | közeli: {rivals}" if rivals else ""))

        if replace_inbox:
            # SZÁNDÉKOSAN NINCS verzió-összehasonlítás: a beépített driver verziója a
            # Windows buildje (10.0.26100.8457), a gyárié meg saját sémájú (6.0.9992.1),
            # tehát a generikus MINDIG "újabbnak" látszana, és pont a jobb drivert
            # dobnánk el. Ugyanez a csapda a WU-ágon már kezelve van
            # (wu_core._filter_wu_downgrades / _is_inbox_driver).
            # Nem pörög körbe: telepítés után az eszköz oemNN.inf-en, gyári providerrel
            # fut, így a következő szkennen már nem jelölt (is_generic_replace_candidate).
            logging.info(f"[CATALOG] Generikus -> gyári csere jelölt: {item['name']} "
                         f"(most: {inst.get('provider') or '?'} {inst_ver_str} / {inst.get('inf') or '?'}) -> '{best_title}'")
        elif _is_inbox_driver(inst):
            # A telepített driver a Windows BEÉPÍTETT generikusa, de az eszköz nem jelölt a
            # gyári cserére (különben a fenti `replace_inbox` ág vitte volna). Ilyenkor a
            # dátum-szabályt NEM alkalmazzuk: az inbox driver dátuma a Windowsé (a
            # `input.inf` pl. 2006-06-21-et visel), így minden gyári csomag "újabbnak"
            # látszana, és a mély szken csendben átvenné a generikus->gyári csere
            # szerepét - épp azokon az osztályokon (HID, billentyűzet, egér), amiket a
            # mark_generic_replace_candidates SZÁNDÉKOSAN kihagy, és rollback-ellenőrzés
            # nélkül. Ezt a döntést ott kell meghozni, nem itt.
            if best_ver is not None and inst_ver is not None and best_ver <= inst_ver:
                logging.debug(f"[CATALOG] Kihagyva (Windows-alapdriveren fut, nem gyári-csere jelölt; "
                              f"telepített {inst_ver_str} >= katalógus '{best_title}'): {item['name']}")
                return None
        else:
            # ÚJABB-E EGYÁLTALÁN? DÁTUM DÖNT, a verzió csak azonos dátumnál (közös mag:
            # wu_core.is_newer_release). A régi, tisztán verzió-alapú kapu a gyártói
            # verziósémaváltásoknál bizonyítottan a rossz csomagot tartotta meg: terepen
            # (2026-07-27) az AMD SMBus 5.12.0.38 / 2017-08-30 "nagyobb" volt, mint a
            # katalógus 2.0.0.26 / 2025-12-03 csomagja, így a gép egy 2017-es driveren
            # maradt. A WU-ág (wu_core._filter_wu_downgrades) és a katalógus SOR-választása
            # már régóta dátum-alapú - ez a kapu volt az utolsó verzió-alapú döntés a
            # telepítési úton.
            newer = is_newer_release(best_date, best_title, inst.get('date'), inst_ver_str)
            if newer is False:
                logging.debug(f"[CATALOG] Kihagyva (nem újabb kiadás - telepített {inst_ver_str} "
                              f"[{inst.get('date') or '?'}] vs katalógus '{best_title}' [{best_date or '?'}]): {item['name']}")
                return None
            if newer is None:
                logging.info(f"[CATALOG] Nem eldönthető, melyik újabb (telepített {inst_ver_str} "
                             f"[{inst.get('date') or '?'}] vs '{best_title}' [{best_date or '?'}]) - felajánljuk: {item['name']}")
            elif _parse_driver_version(best_title) is not None and inst_ver is not None \
                    and _parse_driver_version(best_title) <= inst_ver:
                # Pont az a helyzet, amiért a szabály átállt: dátum szerint újabb, verzió
                # szerint nem. Ez INFO-szintű, mert egy "miért települt rá kisebb verzió?"
                # kérdésre ez az egyetlen válasz a terepi logból.
                logging.info(f"[CATALOG] Dátum szerint ÚJABB, verzió szerint nem - a dátum dönt: "
                             f"{item['name']} - telepített {inst_ver_str} [{inst.get('date') or '?'}] "
                             f"-> '{best_title}' [{best_date or '?'}]")

        # HOLTVERSENY ELDÖNTÉSE A RÉSZLETLAPRÓL, LETÖLTÉS ELŐTT.
        # Idáig csak akkor jutunk el, ha tényleg fel is akarjuk ajánlani a csomagot (a
        # kiadás-kapun túl vagyunk), tehát ez körönként néhány eszközt érint, nem az
        # összeset. Csak az AZONOS DÁTUMÚ jelöltek versenyeznek: a kiadás-kapu rájuk
        # ugyanazt mondja, tehát a köztük való választás nem ronthat a döntésen - viszont
        # pont ez a 10-es NVIDIA holtverseny, ahol eddig vaktában választottunk.
        tie = [c for c in ordered if (c[3] or '') == (best_date or '')]
        if len(tie) > 1:
            ranked = []
            for cand in tie[:CATALOG_MODEL_PROBE_MAX]:
                rank = driver_model_rank(item['name'], self._catalog_driver_models(cand[1], ssl_ctx))
                ranked.append((rank, cand))
                if rank >= 2:
                    # Pontos névegyezés: nincs értelme több részletlapot lekérdezni. E nélkül
                    # a korai kilépés nélkül egy 45 tételes holtverseny (mérve: Realtek NIC)
                    # 45 kérést jelentene, a korlát pedig kizárhatná a jó jelöltet - az
                    # offline teszt pont ezt kapta el, amikor a 8. sor volt a helyes.
                    break
            probed = {c[1] for _r, c in ranked}
            ranked += [(0, c) for c in tie if c[1] not in probed]
            ranked.sort(key=lambda r: (r[0], _parse_driver_version(r[1][2]) or ()), reverse=True)
            if ranked[0][0] > 0 and ranked[0][1][1] != best_id:
                logging.info(f"[CATALOG] Holtverseny eldöntve a részletlap alapján: {item['name']} - "
                             f"'{ranked[0][1][2]}' (a támogatott eszközök közt szerepel), "
                             f"a korábbi vak választás helyett '{best_title}'.")
                best = ranked[0][1]
                _bs, best_id, best_title, best_date = best
                best_ver = _parse_driver_version(best_title)
            elif ranked[0][0] == 0:
                logging.debug(f"[CATALOG] {item['name']}: {len(tie)} holtverseny-jelölt, de a "
                              f"részletlapok 'Driver Model' mezője egyiknél sem mond semmit - "
                              f"marad a dátum/verzió szerinti sorrend.")
            # A tartalék: a maradék azonos dátumú jelölt. Ha a nyertes INF-jéről kiderül,
            # hogy nem ehhez az eszközhöz való, a telepítő ezekkel próbálkozik tovább
            # ahelyett, hogy feladná (lásd _install_catalog_sync). Azonos dátum miatt
            # tartalékként sem kerülhet fel régebbi kiadás.
            alts = [(c[1], c[2], c[3]) for _r, c in ranked if c[1] != best_id][:CATALOG_MAX_CANDIDATES - 1]
        else:
            alts = []

        cab_url = self._catalog_download_url(best_id, ssl_ctx, item['name'])
        if not cab_url:
            return None
        logging.debug(f"[CATALOG] Találat: {item['name']} ('{best_title}') - {cab_url[:50]}...")
        return {
            "name": item['name'], "cat": item['cat'], "hwid": item['id'],
            "url": cab_url, "pnp_id": item.get('pnp_id', ''),
            # A tétel katalógus-GUID-ja + a tartalék jelöltek [(guid, cím, dátum)]: a
            # telepítő ezekből tud továbblépni, a no-bind tár pedig GUID szerint jegyzi
            # meg, melyik konkrét csomag bukott meg ezen az eszközön.
            "cat_guid": best_id,
            "alt_candidates": alts,
            "installed_version": inst_ver_str,
            "installed_date": inst.get('date', ''),
            # A telepítés UTÁNI kötés-ellenőrzéshez: az eszköz ÖSSZES valódi hardver-
            # azonosítója (a csomag alkalmazhatóságához) és a MOSTANI INF-je (ha telepítés
            # után sem változik, a csomag felment ugyan, de az eszköz nem vette át).
            "all_hwids": list(item.get('all_hwids') or []),
            "installed_inf": (inst.get('inf') or '').strip().lower(),
            "wu_title": f"MS Katalógus: {best_title}",
            "wu_date": best_date,
            # A felület ezt jelöli meg külön ("most Microsoft alapdriver"), és a
            # telepítő ezeknél futtat utóellenőrzést + szükség esetén visszaállítást.
            "generic_replace": replace_inbox,
            "installed_provider": inst.get('provider', ''),
            # KOCKÁZATOS (tárolóvezérlő/lemez/firmware) találat: a felület pirossal jelöli
            # és NEM jelöli be előre. Alapesetben csak a manuális szkenben fordulhat elő -
            # az AutoFix ilyen eszközt csak akkor kérdez meg, ha a felhasználó a fix indító
            # dialógusán engedélyezte (wu_core.filter_autofix_risky_devices + a
            # deep_catalog_candidates include_risky/include_firmware kapcsolói).
            # A risk_label a listába való RÖVID felirat: a felület korábban minden `risky`
            # találatra a tárolóvezérlős szöveget írta ki, firmware-re is.
            "risky": bool(item.get('risky')),
            "risk_label": item.get('risk_label') or '',
            "risk_reason": item.get('risk_reason') or '',
        }

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
        # A tartós no-bind tár EGYSZER olvasva (nem eszközönként/szálanként): eszközönként
        # azok a katalógus-GUID-ok, amiket egy korábbi futás már letöltött és az INF-vizsgálat
        # elvetett. Ezeket a jelöltválasztás átugorja - így nem tölthetjük le másodszor
        # ugyanazt az 1,2 GB-ot ugyanarra a kártyára.
        known_records = self._no_bind_load()
        bad_by_pnp = {}
        for rec in known_records:
            if rec.get('guid'):
                bad_by_pnp.setdefault((rec.get('pnp') or '').upper(), set()).add(rec['guid'])
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
                    hit = self._catalog_find_driver(dev, installed_info, ssl_ctx,
                                                    known_no_bind=bad_by_pnp)
                    if hit:
                        with lock:
                            found.append(hit)
                except Exception as e:
                    logging.debug(f"[CATALOG] Hiba: {dev.get('name')} - {e}")
                q.task_done()

        threads = [threading.Thread(target=cat_worker, daemon=True, name=f"catalog-{i}") for i in range(10)]
        for t in threads:
            t.start()
        # A join-plafon az ESZKÖZSZÁMHOZ igazodik. A fix 120 mp a szűk kiegészítéshez
        # (1-5 eszköz) készült; a mély szken 25-30 eszközt ad, eszközönként max 4 HTTP
        # lekérdezéssel - ott a régi plafon lejárt volna, MIELŐTT a szálak végeznek, és a
        # `found` lista hiányosan (ráadásul még írás közben) került volna vissza.
        # ~4 mp/eszköz 10 szálon bőven tartalékos, a 900 mp abszolút ceiling.
        join_timeout = min(900, max(120, len(devices_to_check) * 4))
        for t in threads:
            t.join(timeout=join_timeout)
        alive = [t for t in threads if t.is_alive()]
        if alive:
            logging.warning(f"[CATALOG] {len(alive)} szál még fut a {join_timeout}s plafon után - "
                            f"a találati lista hiányos lehet ({len(found)} db).")
        # UGYANAZ A CSOMAG TÖBB ESZKÖZRE: egy chipset-csomag jellemzően több PnP-eszközt
        # szolgál ki (élő mérés: az "AMD PCI" kétszer szerepelt, azonos letöltési URL-lel),
        # és enélkül ugyanazt a cab-ot kétszer töltenénk le és telepítenénk. A második
        # telepítés amúgy is "already exists" no-op lenne, csak sávszélességbe kerül.
        by_url, deduped = {}, []
        for hit in found:
            u = hit.get('url') or ''
            if u and u in by_url:
                by_url[u].append(hit.get('name'))
                continue
            by_url[u] = []
            deduped.append(hit)
        for u, extra in by_url.items():
            if extra:
                logging.info(f"[CATALOG] Azonos csomag több eszközre, egyszer telepítjük - "
                             f"kihagyott duplikátumok: {extra}")
        # TARTÓS NO-BIND JELÖLÉS: amit egy korábbi futás már feltett/kipróbált, de az
        # eszköz nem vette át (catalog_no_bind.json), azt megjelöljük - a felület így
        # nem ajánlja fel ELŐRE BEJELÖLVE ugyanazt a más gépre szabott csomagot minden
        # AutoFix után (terepi visszajelzés, 2026-07-28). Csak jelölés: a felhasználó
        # bejelölheti, az AutoFix saját (láncon belüli) tiltólistáját nem érinti.
        # A jelölés HÁROM úton illeszkedhet, és mindegyikre szükség van:
        #  (a) ugyanaz a katalógus-GUID (a legpontosabb - egy címhez 10 sor is tartozhat);
        #  (b) ugyanaz a cím (a régi kulcs, a korábbi bejegyzésekhez);
        #  (c) UGYANAZ A CSOMAG RÉGEBBI/AZONOS KIADÁSA. Ez utóbbi a 2026-08-05-i eset:
        #      a záró kör a Realtek audiót 6.0.9992.1 [2026-05-18] néven ajánlotta, míg a
        #      feljegyzett bukás a 6.0.10007.1 [2026-06-22] volt - más cím, tehát a régi
        #      kulcs nem fogta, és a program letöltötte ugyanazt a Clevo-csomagot még
        #      egyszer. Egy ÚJABB kiadás viszont továbbra sem maszkolható (szándékos:
        #      lehet, hogy a gyártó épp kijavította) - ezért a release_rank-összevetés.
        if known_records:
            marked = []
            for hit in deduped:
                pnp = (hit.get('pnp_id') or '').upper()
                title = hit.get('wu_title') or ''
                hit_rank = release_rank(hit.get('wu_date'), title)
                for rec in known_records:
                    if (rec.get('pnp') or '').upper() != pnp:
                        continue
                    same = (rec.get('guid') and rec['guid'] == hit.get('cat_guid')) or \
                           (rec.get('title') or '') == title
                    older_variant = (
                        not same and rec.get('title')
                        and catalog_title_family(rec['title']) == catalog_title_family(title)
                        and hit_rank <= release_rank(rec.get('date'), rec.get('title')))
                    if same or older_variant:
                        hit['prev_no_bind'] = True
                        hit['prev_no_bind_reason'] = rec.get('reason') or ''
                        marked.append(hit.get('name'))
                        break
            if marked:
                logging.info(f"[CATALOG] {len(marked)} találat megjelölve (korábbi futásban az eszköz "
                             f"nem vette át, nem lesz előre bejelölve): {marked}")
        logging.info(f"[CATALOG] Kész - {len(deduped)} eszközre van katalógus-találat"
                     + (f" ({len(found) - len(deduped)} duplikált csomag összevonva)" if len(found) != len(deduped) else ""))
        return deduped

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
        # Ez volt a projekt EGYETLEN olyan Popen-je, ami nem írta ki a futtatott parancsot
        # (a többi mind logol egy "[CMD] Popen futtatása:" sort) - ráadásul pont a manuális
        # telepítési úton, ami történetileg a "AutoFix megy, a manuális némán törött"
        # hibaosztály helyszíne (Build ~192). A kért UpdateID-k/HWID-ek nélkül egy
        # "semmit nem telepített" bejelentést nem lehet kivizsgálni.
        logging.info(f"[WU_INSTALL] Kért UpdateID-k ({len(pool_uids)}): {pool_uids}")
        logging.info(f"[WU_INSTALL] Kért HWID-ek ({len(pool_hwids)}): {pool_hwids}")
        logging.debug(f"[CMD] Popen futtatása: {ps_script[:300]}...")
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

    # ------------------------------------------------------------------
    # FUTÁSOKON ÁTÍVELŐ "nem kötött rá" emlékezet (catalog_no_bind.json).
    # Miért kell az autofix_stats.json-beli lista MELLÉ: az a lánccal együtt törlődik,
    # így a kézi szken az AutoFix után újra felajánlotta (előre bejelölve!) azokat a
    # csomagokat, amikről a lánc már bizonyította, hogy más gépre szabottak vagy az
    # eszköz nem veszi át őket. A fájl CSAK jelölésre való (a felület nem jelöli be
    # előre + jelvényt tesz rá) - telepítést nem tilt, az AutoFix köreit nem szűri:
    # egy friss lánc a törlés/újratelepítés után szándékosan újra próbálkozhat, és egy
    # esetleg tévesen feljegyzett csomagot a felhasználó kézzel bármikor feltehet.
    # Kulcs: (pnp_id, csomagcím) - egy ÚJABB katalógus-kiadás címe eltér, azt tehát
    # semmi nem jelöli meg. Sikeres, IGAZOLTAN átvett telepítés törli a bejegyzést.
    # ------------------------------------------------------------------
    def _no_bind_store_path(self):
        return os.path.join(_app_data_dir(), 'catalog_no_bind.json')

    def _no_bind_load(self):
        """A tartós no-bind lista beolvasása; hibánál üres lista (a jelölés elmaradása
        nem hiba, csak a kényelmi funkció esik ki)."""
        try:
            with open(self._no_bind_store_path(), 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except FileNotFoundError:
            return []
        except Exception as e:
            logging.debug(f"[CATALOG] catalog_no_bind.json nem olvasható: {e}")
            return []

    def _no_bind_record(self, no_bind_items, bound_ok_items=None):
        """Nem-kötő csomagok feljegyzése + igazoltan átvett telepítések kivezetése."""
        try:
            existing = self._no_bind_load()
            bound_keys = {((d.get('pnp_id') or '').upper(), d.get('wu_title') or '')
                          for d in (bound_ok_items or [])}
            removed = [t.get('name') or t.get('title') for t in existing
                       if ((t.get('pnp') or '').upper(), t.get('title') or '') in bound_keys]
            if removed:
                existing = [t for t in existing
                            if ((t.get('pnp') or '').upper(), t.get('title') or '') not in bound_keys]
                logging.info(f"[CATALOG] {len(removed)} bejegyzés törölve a tartós no-bind listáról "
                             f"(az eszköz most már átvette a csomagot): {removed}")
            keys = {((t.get('pnp') or '').upper(), t.get('title') or '') for t in existing}
            added = []
            for d in (no_bind_items or []):
                k = ((d.get('pnp_id') or '').upper(), d.get('wu_title') or '')
                if not k[1] or k in keys:
                    continue
                keys.add(k)
                # A GUID és a KIADÁS DÁTUMA is elmegy a bejegyzésbe: a GUID-ról ismerhető
                # fel újra pontosan ugyanaz a katalógus-sor (egy címhez tíz is tartozhat),
                # a dátum pedig ahhoz kell, hogy a csomag RÉGEBBI kiadását se töltsük le
                # újra - miközben egy ÚJABB kiadás továbbra sem maszkolódik le.
                existing.append({'pnp': k[0], 'title': k[1], 'name': d.get('name') or '',
                                 'guid': d.get('cat_guid') or '',
                                 # A LETÖLTÉSI URL a legerősebb kulcs: mérve (2026-08-06) a
                                 # katalógus UGYANAZT a cab-ot 25 külön bejegyzésként (25 GUID)
                                 # listázza, tehát a GUID-tiltás önmagában nem akadályozza meg,
                                 # hogy a következő futás egy másik bejegyzésen keresztül
                                 # ugyanazt az 1,2 GB-ot letöltse. Az URL tartalom-hasht
                                 # tartalmaz, így egy ÚJABB kiadás automatikusan más URL-t kap.
                                 'url': d.get('url') or '',
                                 'date': d.get('wu_date') or '',
                                 'reason': d.get('no_bind_reason') or '',
                                 'recorded': time.strftime('%Y-%m-%d')})
                added.append(d.get('name') or k[1])
            if not added and not removed:
                return
            existing = existing[-100:]   # ne nőhessen korlátlanul
            with open(self._no_bind_store_path(), 'w', encoding='utf-8') as f:
                json.dump(existing, f, ensure_ascii=False, indent=1)
            if added:
                logging.info(f"[CATALOG] {len(added)} nem-kötő csomag feljegyezve a tartós emlékezetbe "
                             f"({self._no_bind_store_path()}): {added}")
        except Exception as e:
            logging.warning(f"[CATALOG] A tartós no-bind lista frissítése nem sikerült: {e}")

    # ------------------------------------------------------------------
    # ELHALASZTOTT INF-KIVEZETÉS (pending_inf_cleanup.json).
    # Miért kell: a reboot-igényes (3010) telepítésnél a kivezetés jogosan marad ki -
    # a kötés csak a következő bootnál dől el, most nem ítélkezhetünk. Csakhogy
    # korábban EZZEL VÉGE IS VOLT: senki nem tért vissza rájuk, így a fel nem használt
    # INF-ek örökre a DriverStore-ban maradtak. Terepen (2026-08-05, Dell Latitude 7400):
    # az `Intel(R) PCI Express Root Port #13 - 9DB4` katalógus-cab a TELJES Intel
    # chipset-INF gyűjteményt hordozza (ApolloLake, Avoton, Baytrail, Braswell,
    # Broadwell, ColetoCreek, CougarPoint, Crystalwell, Denverton, FPGA, Haswell,
    # IceLake, IvyBridge, IvyTown, JakeTown, KabyLake...), a pnputil 3010-nel tért
    # vissza, a kivezetés kimaradt - és a gép 23 third-party csomagja 163-ra hízott
    # (a záró duplikátum-takarítás után is 143 maradt, mert ezek nem duplikátumok:
    # mindnek külön eredeti INF-neve van). Mellékhatás: a `dism /Get-Drivers` 0,6 mp-ről
    # 77 mp-re lassult. Ezért a kihagyott csomag INF-jeit ide jegyezzük fel, és a
    # KÖVETKEZŐ LÁB elején (tehát egy valódi újraindítás után) fejezzük be a munkát.
    # A fájl szándékosan NEM az autofix_stats.json (az a lánccal együtt törlődik):
    # ha a halasztás az utolsó lábban történt, a bejegyzés túléli a láncot, és a
    # program következő indulásakor takarítunk.
    # ------------------------------------------------------------------
    def _deferred_inf_cleanup_path(self):
        return os.path.join(_app_data_dir(), 'pending_inf_cleanup.json')

    def _deferred_inf_cleanup_load(self):
        """Az elhalasztott kivezetés-lista; hibánál üres (a takarítás elmaradása nem
        végzetes, csak a DriverStore marad szemetesebb)."""
        try:
            with open(self._deferred_inf_cleanup_path(), 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except FileNotFoundError:
            return []
        except Exception as e:
            logging.debug(f"[CATALOG_INSTALL] pending_inf_cleanup.json nem olvasható: {e}")
            return []

    def _deferred_inf_cleanup_save(self, items):
        """Üres listánál a fájl TÖRLŐDIK - így a "van-e elhalasztott munka?" kérdés a
        következő induláskor egy olcsó fájl-létezés-vizsgálat."""
        try:
            p = self._deferred_inf_cleanup_path()
            if not items:
                if os.path.exists(p):
                    os.remove(p)
                return
            with open(p, 'w', encoding='utf-8') as f:
                json.dump(items[-500:], f, ensure_ascii=False, indent=1)
        except Exception as e:
            logging.warning(f"[CATALOG_INSTALL] pending_inf_cleanup.json írása nem sikerült: {e}")

    def _defer_inf_cleanup(self, entries, name):
        """A reboot-igényes csomag MINDEN publikált INF-jét feljegyzi későbbi elbírálásra.

        Szándékosan az összeset (nem csak a most használatlanokat): a kötés a bootnál
        dől el, tehát a mostani "used" jelzés még nem ítélet. A következő láb friss
        rendszerállapotból dönt, nem ebből a listából."""
        try:
            items = self._deferred_inf_cleanup_load()
            have = {(i.get('published') or '').lower() for i in items}
            added = []
            for inf, pub, _used in entries:
                if pub in have:
                    continue
                have.add(pub)
                items.append({'published': pub,
                              'original': inf.split('\\')[-1].split('/')[-1].lower(),
                              'package': name,
                              'recorded': time.strftime('%Y-%m-%d')})
                added.append(pub)
            if not added:
                return
            self._deferred_inf_cleanup_save(items)
            logging.info(f"[CATALOG_INSTALL] {name}: újraindítást igénylő telepítés - a csomag "
                         f"{len(added)} INF-jének elbírálása a következő lábra halasztva "
                         f"({self._deferred_inf_cleanup_path()}): {added}")
        except Exception as e:
            logging.warning(f"[CATALOG_INSTALL] Az elhalasztott kivezetés feljegyzése nem sikerült ({name}): {e}")

    def _finish_deferred_inf_cleanup(self, task_id='autofix'):
        """A korábban elhalasztott INF-kivezetés befejezése FRISS BOOT után.

        Három biztonsági szabály, mindegyik ugyanazt a hibát zárja ki (nehogy egy
        használatban lévő vagy időközben újraszámozott csomagot lőjünk ki):
          - az aktív publikált INF-ek listája (Win32_PnPSignedDriver) a döntő; ha a
            lekérdezés hibázik (None), NEM törlünk semmit és a lista is megmarad;
          - csak akkor törlünk, ha a publikált oemNN.inf MÉG MINDIG ugyanahhoz az
            eredeti INF-névhez tartozik, mint a feljegyzéskor (újraszámozás ellen);
          - a törlés sima `pnputil /delete-driver` (se /uninstall, se /force): ha bármi
            mégis használja, a pnputil elutasítja és a csomag marad.
        Visszatérés: hány csomag lett kivezetve."""
        items = self._deferred_inf_cleanup_load()
        if not items:
            return 0
        logging.info(f"[CATALOG_INSTALL] {len(items)} elhalasztott INF-bejegyzés elbírálása a friss boot után...")
        active = dupdrivers_core.get_active_published_infs(self._run)
        if active is None:
            logging.warning("[CATALOG_INSTALL] Az aktív INF-lista nem kérdezhető le - az elhalasztott "
                            "kivezetés kimarad, a lista megmarad a következő alkalomra.")
            return 0
        current = {(d.get('published') or '').lower(): (d.get('original') or '').lower()
                   for d in self._get_third_party_drivers()}
        todo, in_use, gone, renumbered = [], [], 0, []
        for it in items:
            pub = (it.get('published') or '').lower()
            if pub not in current:
                gone += 1                      # már nincs a gépen - a bejegyzés elévült
            elif current[pub] != (it.get('original') or '').lower():
                renumbered.append(pub)         # időközben más csomag kapta ezt a nevet
            elif pub in active:
                in_use.append(pub)             # a boot után mégis rákötött egy eszköz
            else:
                todo.append(it)
        if renumbered:
            logging.warning(f"[CATALOG_INSTALL] {len(renumbered)} bejegyzés kihagyva, mert a publikált név "
                            f"időközben másik csomagé lett (újraszámozás): {renumbered}")
        logging.info(f"[CATALOG_INSTALL] Elhalasztott kivezetés mérlege: {len(todo)} törlendő, "
                     f"{len(in_use)} a boot után mégis használatba került ({in_use}), "
                     f"{gone} már nincs a gépen, {len(renumbered)} újraszámozott.")
        if not todo:
            self._deferred_inf_cleanup_save([])
            return 0
        self.emit('task_progress', {'task': task_id, 'log': f'🧹 {len(todo)} fel nem használt INF kivezetése a DriverStore-ból (az előző kör újraindítás-igényes csomagjaiból)...'})
        done, refused = 0, 0
        for i, it in enumerate(todo):
            if getattr(self, '_cancel_flag', False):
                self._deferred_inf_cleanup_save(todo[i:])
                raise Exception("Magyar_Megszakit_Flag")
            pub = it['published']
            dres = self._run(['pnputil', '/delete-driver', pub], ok_codes=(0, 3010))
            if dres and dres.returncode in (0, 3010):
                done += 1
            else:
                refused += 1
                logging.info(f"[CATALOG_INSTALL] Kivezetés elutasítva (marad): {pub} "
                             f"({it.get('original')}, {it.get('package')}), rc={getattr(dres, 'returncode', '?')}")
        self._deferred_inf_cleanup_save([])
        logging.info(f"[CATALOG_INSTALL] Elhalasztott kivezetés kész: {done} törölve, {refused} elutasítva.")
        self.emit('task_progress', {'task': task_id, 'log': f'✅ DriverStore-takarítás: {done} fel nem használt INF kivezetve.\n'})
        return done

    def _cleanup_unused_staged_infs(self, pnp_out, name, task_id, defer=False):
        """Több-INF-es katalógus-csomag FEL NEM HASZNÁLT INF-jeinek kivezetése a DriverStore-ból.

        Miért: a `pnputil /add-driver <mappa>\\*.inf /subdirs /install` a csomag MINDEN
        INF-jét stage-eli, akkor is, ha az eszközhöz csak egy (vagy nulla) illik. Terepen
        (2026-07-28, dev gép): a Razer katalógus-cab a teljes Razer termékpaletta INF-jeit
        hordozza (rz0001dev...rz0f2cdev, egerek/billentyűzetek/headsetek külön INF-fel),
        és egyetlen AutoFix-futás után a gép 19 third-party csomagja 219-re hízott
        (mérve: 201 rz*-INF a DriverStore-ban, egyik sincs eszközhöz kötve). Funkcionálisan
        ártalmatlan, de a DriverStore-t szemeteli és a driverlistát használhatatlanná teszi.

        Döntési szabály: a pnputil kimenete blokkonként elárulja az egyes INF-ek sorsát -
        amelyik "installed on device" / "up-to-date on device" sort kapott, azt jelen lévő
        eszköz használja, MARAD; a csak stage-elt többi megy. Csak több-INF-es csomagnál
        fut (az egy-INF-es nem-kötő esetet a bind-check kezeli no-bindként). A törlés sima
        `pnputil /delete-driver` (se /uninstall, se /force): ha bármi mégis használja,
        a pnputil elutasítja és a csomag marad - ezt a kimenetel-logika elviseli.

        `defer=True` (reboot-igényes telepítés): MOST nem ítélkezünk - a kötés a következő
        bootnál dől el -, de a csomagot NEM ejtjük: feljegyezzük, és a következő láb
        (`_finish_deferred_inf_cleanup`) friss rendszerállapotból dönt róla. Korábban itt
        egyszerűen véget ért a történet, és a fel nem használt INF-ek örökre bent maradtak."""
        try:
            blocks = re.split(r'(?=Adding driver package)', pnp_out or '')
            entries = []
            for b in blocks:
                m = re.search(r'Adding driver package\s*:?\s*(\S+)', b)
                p = re.search(r'Published Name\s*:\s*(oem\d+\.inf)', b, re.IGNORECASE)
                if not (m and p):
                    continue
                used = bool(re.search(r'installed on device|up-to-date on device', b, re.IGNORECASE))
                entries.append((m.group(1), p.group(1).lower(), used))
            if len(entries) <= 1:
                return
            if defer:
                self._defer_inf_cleanup(entries, name)
                return
            unused = [(inf, pub) for inf, pub, used in entries if not used]
            if not unused:
                return
            logging.info(f"[CATALOG_INSTALL] {name}: a csomag {len(entries)} INF-jéből {len(unused)} egyetlen "
                         f"jelen lévő eszközön sem használt - kivezetés a DriverStore-ból: "
                         f"{[inf for inf, _pub in unused]}")
            self.emit('task_progress', {'task': task_id, 'log': f'  🧹 {name}: a csomag {len(unused)} fel nem használt INF-jének kivezetése a DriverStore-ból...'})
            refused = 0
            for inf, pub in unused:
                dres = self._run(['pnputil', '/delete-driver', pub], ok_codes=(0, 3010))
                if not dres or dres.returncode not in (0, 3010):
                    refused += 1
                    logging.info(f"[CATALOG_INSTALL] Kivezetés elutasítva (marad): {pub} ({inf}), rc={getattr(dres, 'returncode', '?')}")
            logging.info(f"[CATALOG_INSTALL] {name}: kivezetve {len(unused) - refused}/{len(unused)} fel nem használt INF.")
        except Exception as e:
            logging.warning(f"[CATALOG_INSTALL] A fel nem használt INF-ek kivezetése nem sikerült ({name}): {e}")

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
        # Generikus -> gyári cserék: (pool-elem, [publikált oemNN.inf]) párok. A telepítés
        # UTÁN ellenőrizzük őket, és ha az eszköz hibakódos lett, visszaállunk (lásd
        # _verify_generic_replacements). Csak itt gyűjtjük, a kiértékelés a szálak után fut.
        generic_installs = []
        # Sikeresnek látszó telepítések, amiknél MEG KELL NÉZNI, hogy az eszköz tényleg
        # átvette-e a drivert (bind). Elemei: (pool-elem, reboot_pending).
        bind_checks = []
        # Azok az elemek, amikre a csomag NEM alkalmazható / nem kötött rá. A hívó (AutoFix)
        # ezt átviszi a következő lábra, hogy ne töltse le újra ugyanazt.
        no_bind = []
        self._catalog_no_bind = no_bind
        # Azok az elemek, amiknél a kötés-ellenőrzés IGAZOLTA, hogy az eszköz átvette a
        # drivert - ezek kulcsát a tartós no-bind emlékezetből törölni kell (ha egy
        # korábban nem-kötő csomag most mégis felment, a jelölése elavult).
        bound_ok = []
        # KORÁBBAN MÁR BIZONYÍTOTTAN NEM IDE VALÓ CSOMAGOK, eszközönként, LETÖLTÉSI URL
        # szerint. Ez az egyetlen kulcs, ami tényleg megfogja az ismétlést: a katalógus
        # ugyanazt a cab-ot több tucat külön bejegyzésként (külön GUID-dal, külön címmel)
        # listázza, tehát a következő futás simán "másik" jelöltet választana ugyanarra a
        # fájlra. Az URL feloldása pár KB-os kérés, a letöltés viszont akár 1,2 GB.
        prev_bad_urls = {}
        if not self.target_os_path:
            for rec in self._no_bind_load():
                if rec.get('url'):
                    prev_bad_urls.setdefault((rec.get('pnp') or '').upper(), set()).add(rec['url'])

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

                # HOLTVERSENY-TARTALÉK: a nyertes mellett a vele AZONOS DÁTUMÚ jelöltek is
                # itt vannak (lásd _catalog_find_driver). Ha a nyertes INF-jéről kiderül,
                # hogy nem ehhez az eszközhöz való, továbblépünk a következőre ahelyett,
                # hogy feladnánk. Terep (2026-08-05): a videokártyára 10 azonos című sor
                # közül vaktában vittünk el egyet, 1,2 GB letöltés után derült ki, hogy nem
                # ismeri ezt a kártyát - és a maradék 9-et meg se néztük, a gép pedig úgy
                # zárta a láncot, hogy "a katalógusban nincs jobb driver".
                candidates = [(drv.get('cat_guid') or '', drv.get('wu_title') or '',
                               drv.get('wu_date') or '', url)]
                for (g, t, d) in (drv.get('alt_candidates') or [])[:CATALOG_MAX_CANDIDATES - 1]:
                    candidates.append((g, t, d, None))

                self.emit('task_progress', {'task': task_id, 'log': f'-> {name} letöltése...'})
                # ÚJRAPRÓBÁLKOZÁS: a katalógus cab-jai százmegásak (egy videokártya-csomag
                # 1,1 GB), és egy ekkora letöltés alatt egy megszakadt kapcsolat teljesen
                # hétköznapi. Terepen (2026-07-27) az NVIDIA-csomag [WinError 10054]
                # ("a távoli gép bontotta a kapcsolatot") hibával elhasalt 18 másodperc
                # után, egyetlen próbálkozás után véglegesen sikertelenként könyvelve -
                # pedig a következő lábon ugyanaz az URL simán lejött. A félbemaradt fájlt
                # minden kör elején töröljük (ugyanaz a szabály, mint a stresstools.zip-nél:
                # a maradék épp azt a helyet enné el, ami az újrapróbáláshoz kell).
                #
                # A kör a KICSOMAGOLÁST is magában foglalja (2026-07-28, terepi log): a
                # szerver a kapcsolat bontását nem mindig jelzi hibával - a http.client a
                # darabolt olvasásnál kivétel NÉLKÜL ad vissza rövid fájlt, így az NVIDIA
                # 1,22 GB-os cab-jából 139 MB jött le "sikeresen", majd az expand kód=1-gyel
                # elhasalt (amit senki nem nézett), és a hiba "nincs INF a csomagban"-ként,
                # VÉGLEGES bukásként jelent meg - a pont erre épített retry egyszer sem
                # indult el. Ezért: (a) a letöltött méretet a Content-Length-hez mérjük,
                # (b) az expand hibája és a hiányzó INF is újrapróbálást vált ki (sérült
                # cab), nem végleges hibát.
                CATALOG_DL_ATTEMPTS = 3
                chosen = None        # (guid, cím, dátum, url) - amit végül telepítünk
                # UGYANAZT A CSOMAGOT NEM TÖLTJÜK LE KÉTSZER (lásd lent) - és amit egy
                # KORÁBBI FUTÁS már bizonyítottan elvetett erre az eszközre, azt sem.
                known_bad = set(prev_bad_urls.get((drv.get('pnp_id') or '').upper()) or ())
                tried_urls = set(known_bad)
                for cand_i, (cand_guid, cand_title, cand_date, cand_url) in enumerate(candidates):
                    if self._check_cancel():
                        return
                    if cand_url is None:
                        # A tartalék URL-jét csak akkor oldjuk fel, ha tényleg kell.
                        cand_url = self._catalog_download_url(cand_guid, ssl_ctx, name)
                        if not cand_url:
                            logging.warning(f"[CATALOG_INSTALL] {name}: a(z) {cand_i + 1}. jelölt "
                                            f"('{cand_title}') letöltési linkje nem oldható fel - kihagyva.")
                            continue
                    # UGYANAZ A CSOMAG TÖBB KATALÓGUS-BEJEGYZÉSKÉNT. Mérve (2026-08-06,
                    # élő katalógus): a `PCI\VEN_10DE&DEV_2504` legfrissebb dátumú 25 sora
                    # KÖZÜL AZ ELSŐ ÖT MIND UGYANARRA a cab-ra mutat (azonos fájlnév, azonos
                    # 1165,1 MB méret, azonos INF-lista) - a katalógus OS-ágakként külön
                    # bejegyzésként listázza ugyanazt a csomagot. A tartalék-logika enélkül
                    # háromszor töltené le ugyanazt az 1,2 GB-ot, ami rosszabb a hibánál,
                    # amit javítani akar. A DownloadDialog-kérés pár KB, tehát az URL
                    # feloldása után derül ki - és onnan már ingyen ugorjuk át.
                    if cand_url in tried_urls:
                        why = ("egy KORÁBBI futásban már bizonyítottan nem ehhez az eszközhöz való"
                               if cand_url in known_bad else
                               "UGYANARRA a csomagra mutat, mint egy már kipróbált jelölt")
                        logging.info(f"[CATALOG_INSTALL] {name}: a(z) {cand_i + 1}. jelölt "
                                     f"('{cand_title}') {why} - nem töltjük le újra.")
                        continue
                    tried_urls.add(cand_url)
                    if cand_i:
                        self.emit('task_progress', {'task': task_id, 'log': f'  ↻ {name}: következő katalógus-jelölt próbája ({cand_title})...'})
                    url = cand_url
                    pkg_ok, last_err = False, None
                    for attempt in range(1, CATALOG_DL_ATTEMPTS + 1):
                        if self._check_cancel():
                            return
                        try:
                            logging.debug(f"[CATALOG_INSTALL] Letöltés ({attempt}/{CATALOG_DL_ATTEMPTS}): {url[:80]}...")
                            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
                            with urllib.request.urlopen(req, context=ssl_ctx, timeout=120) as resp, open(cab_path, 'wb') as f:
                                expected_len = resp.headers.get('Content-Length')
                                shutil.copyfileobj(resp, f)
                            got_len = os.path.getsize(cab_path)
                            if expected_len and expected_len.isdigit() and got_len != int(expected_len):
                                raise IOError(f"csonka letöltés: {got_len}/{expected_len} byte jött le")
                            logging.debug(f"[CATALOG_INSTALL] Letöltve: {cab_path} ({got_len} byte, "
                                          f"{attempt}. próbálkozásra)")
                            if file_ext == '.msu':
                                pkg_ok = True   # az .msu-t a wusa/dism ellenőrzi, expand-kör nincs
                                break
                            # Kicsomagolás + INF-jelenlét még a próbálkozás-körön BELÜL: egy
                            # sérült cab tünete pont ez a kettő, és mindkettőre a friss
                            # újraletöltés a gyógyszer, nem a végleges hiba.
                            if os.path.isdir(ext_path):
                                shutil.rmtree(ext_path, ignore_errors=True)   # előző kör maradéka
                            os.makedirs(ext_path, exist_ok=True)
                            exp_res = self._run(['expand', cab_path, '-F:*', ext_path])
                            if not exp_res or exp_res.returncode != 0:
                                rc = exp_res.returncode if exp_res else '?'
                                raise IOError(f"az expand nem tudta kicsomagolni (kód={rc}) - valószínűleg sérült cab")
                            for inner_cab in glob.glob(os.path.join(ext_path, '*.cab')):
                                inner_ext = inner_cab + '_ext'
                                os.makedirs(inner_ext, exist_ok=True)
                                self._run(['expand', inner_cab, '-F:*', inner_ext])
                            has_inf = False
                            for _r, _d, files in os.walk(ext_path):
                                if any(fn.lower().endswith('.inf') for fn in files):
                                    has_inf = True
                                    break
                            if not has_inf:
                                raise IOError("a kicsomagolt csomagban nincs .inf - sérült vagy nem driver-csomag")
                            pkg_ok = True
                            break
                        except Exception as e:
                            last_err = e
                            logging.warning(f"[CATALOG_INSTALL] Letöltési/kicsomagolási hiba ({name}, {attempt}/{CATALOG_DL_ATTEMPTS}): {e}")
                            try:
                                if os.path.exists(cab_path):
                                    os.remove(cab_path)
                            except Exception as ce:
                                logging.debug(f"[CATALOG_INSTALL] A félbemaradt fájl törlése sikertelen ({cab_path}): {ce}")
                            shutil.rmtree(ext_path, ignore_errors=True)
                            if attempt < CATALOG_DL_ATTEMPTS:
                                self.emit('task_progress', {'task': task_id, 'log': f'  ↻ {name} letöltése megszakadt ({e}) - újrapróbálás ({attempt + 1}/{CATALOG_DL_ATTEMPTS})...'})
                                time.sleep(3)
                    if not pkg_ok:
                        # A LETÖLTÉS bukása nem "rossz csomag" - itt nincs értelme a következő
                        # jelöltnek (a hálózat a hibás), ezért az eredeti viselkedés marad.
                        logging.error(f"[CATALOG_INSTALL] Letöltés/kicsomagolás VÉGLEG sikertelen {CATALOG_DL_ATTEMPTS} próbálkozás után ({name}): {last_err}")
                        self.emit('task_progress', {'task': task_id, 'log': f'  ❌ {name} letöltési/kicsomagolási hiba {CATALOG_DL_ATTEMPTS} próbálkozás után: {last_err}'})
                        with counter_lock:
                            fail += 1
                        return

                    # ALKALMAZHATÓSÁG-ELLENŐRZÉS a telepítés ELŐTT (lásd wu_core.inf_package_applies):
                    # a katalógus a törzs-HWID-re más gépgyártóra szabott változatot is adhat,
                    # ami feltelepül, de sosem köt rá az eszközre. Ilyet meg se próbálunk -
                    # helyette a következő azonos dátumú jelölttel folytatjuk.
                    if file_ext == '.msu' or self.target_os_path or not drv.get('all_hwids'):
                        chosen = (cand_guid, cand_title, cand_date, cand_url)
                        break
                    if inf_package_applies(ext_path, drv.get('all_hwids')) is False:
                        logging.warning(f"[CATALOG_INSTALL] Nem alkalmazható csomag ({cand_i + 1}/{len(candidates)}), "
                                        f"kihagyva: {name} ({cand_title})")
                        self.emit('task_progress', {'task': task_id, 'log': f'  ↷ {name}: a(z) „{cand_title}” csomag más gépre/alaplapra készült (az INF nem ismeri ezt az eszközt) - kihagyva.'})
                        # MINDEN megbukott jelölt bekerül a no-bind emlékezetbe (GUID-dal),
                        # így a következő futás nem tölti le újra ugyanezt a csomagot.
                        with counter_lock:
                            no_bind.append(dict(drv, wu_title=cand_title, wu_date=cand_date,
                                                cat_guid=cand_guid,
                                                no_bind_reason='nem alkalmazható (más gépre szabott INF)'))
                        continue
                    chosen = (cand_guid, cand_title, cand_date, cand_url)
                    break

                if chosen is None:
                    logging.warning(f"[CATALOG_INSTALL] {name}: mind a(z) {len(candidates)} katalógus-jelölt "
                                    f"INF-je más eszközre való - nincs telepíthető csomag.")
                    self.emit('task_progress', {'task': task_id, 'log': f'  ↷ {name}: a katalógus {len(candidates)} jelöltjéből egyik sem ehhez az eszközhöz való - kihagyva.'})
                    with counter_lock:
                        skipped += 1
                    return
                if chosen[1] != (drv.get('wu_title') or ''):
                    # Ez a sor a bizonyíték, hogy a tartalék-logika dolgozott: enélkül a
                    # terepi logból nem derülne ki, miért MÁS csomag ment fel, mint amit a
                    # keresés nyertesként kiírt.
                    logging.info(f"[CATALOG_INSTALL] {name}: a nyertes csomag nem volt alkalmazható, "
                                 f"a tartalék jelölt megy fel: '{chosen[1]}' [{chosen[2] or '?'}]")
                    self.emit('task_progress', {'task': task_id, 'log': f'  ✔ {name}: a tartalék katalógus-csomag illik az eszközre ({chosen[1]}).'})
                drv['cat_guid'], drv['wu_title'], drv['wu_date'], drv['url'] = \
                    chosen[0], chosen[1], chosen[2], chosen[3]
                url = chosen[3]

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

                self.emit('task_progress', {'task': task_id, 'log': f'  Telepítés: {name}...'})
                is_offline = bool(self.target_os_path)
                if is_offline:
                    cmd = ['dism', f'/Image:{self.target_os_path}', '/Add-Driver', f'/Driver:{ext_path}', '/Recurse']
                    res = self._run(cmd)
                else:
                    cmd = ['pnputil', '/add-driver', f"{ext_path}\\*.inf", '/subdirs', '/install']
                    # 259 = a csomag már fent van / nincs rá kötő eszköz (lentebb no-op),
                    # 3010 = siker, de reboot kell - mindkettő VÁRT kimenet, WARNING nélkül
                    # (terepi log, 2026-07-28: 4 hamis WARNING egy hibátlan futásban).
                    res = self._run(cmd, ok_codes=(0, 259, 3010))
                # pnputil kimenet: "Added driver packages:  N". Ha N==0, semmi nem települt
                # (a csomag már a store-ban van / up-to-date, kód 259) - ezt TILOS sikernek
                # számolni: az AutoFix katalógus-záróköre soha be nem bind-elő eszközön
                # (pl. kód-28 Ismeretlen Eszköz) minden körben "1 települt"-et jelentene, és a
                # lánc végtelen reboot-loopba kerülne (field-seen: AMDIF031 amdgpio3.inf).
                added_m = re.search(r'Added driver packages?\s*:\s*(\d+)', res.stdout or '', re.IGNORECASE)
                added_zero = added_m is not None and int(added_m.group(1)) == 0
                # "(Already exists in the system)": a csomag MÁR a DriverStore-ban van egy
                # korábbi körből, a pnputil mégis "Added driver packages: N"-t ír (N>0), az
                # added_zero-guard tehát NEM fog rá. Ha MINDEN "added successfully" sor
                # már-létező, akkor SEMMI új nem került fel - a generikus->gyári jelölt
                # (pl. Realtek UAD audio, ami a hdaudio.inf-en ragad és nem bind-el át)
                # különben minden körben "1 települt"-et jelentene, végtelen reboot-loopot
                # okozva (terepen, Build 228: az audio 3 körön át újratelepült). Ezt is
                # no-opként kell kezelni, pontosan mint az added_zero-t.
                add_ok_lines = len(re.findall(r'added successfully', res.stdout or '', re.IGNORECASE))
                already_exists = len(re.findall(r'already exists in the system', res.stdout or '', re.IGNORECASE))
                all_already = add_ok_lines > 0 and already_exists >= add_ok_lines
                no_op = added_zero or all_already
                installed_ok = (res.returncode == 0 or any(k in res.stdout for k in ["Added", "sikeres", "successfully"])) and not no_op
                if installed_ok:
                    with counter_lock:
                        success += 1
                        # "reboot kell" esetén az eszköz csak a következő indulásnál veszi
                        # át a drivert - ilyenkor a kötés-ellenőrzés MOST még hamis negatív
                        # lenne (terepen: AMD PSP 3010-nel jött fel, és a reboot után rendben
                        # átkötött). Ezért a reboot-jelzést külön visszük.
                        reboot_pending = (res.returncode == 3010
                                          or 'reboot is needed' in (res.stdout or '').lower())
                        if drv.get('pnp_id') and drv.get('installed_inf') and not is_offline:
                            # A pnputil kimenete is elmegy: abból derül ki, ha a csomag egy
                            # GYEREK-INTERFÉSZRE kötött rá (composite USB), miközben maga a
                            # lekérdezett szülő - helyesen - usb.inf-en maradt.
                            bind_checks.append((drv, reboot_pending, res.stdout or ''))
                        if drv.get('generic_replace'):
                            # A pnputil kiírja, milyen néven publikálta a csomagot
                            # ("Published Name: oem42.inf") - visszaálláskor pontosan ezt
                            # kell törölni, semmi mást.
                            generic_installs.append(
                                (drv, re.findall(r'Published Name\s*:\s*(oem\d+\.inf)', res.stdout or '', re.IGNORECASE)))
                    self.emit('task_progress', {'task': task_id, 'log': f'  ✅ {name} telepítve!'})
                elif no_op:
                    with counter_lock:
                        skipped += 1
                    reason = 'már a rendszerben van' if all_already else 'nincs új csomag'
                    self.emit('task_progress', {'task': task_id, 'log': f'  ↷ {name} már naprakész ({reason}) - kihagyva.'})
                else:
                    with counter_lock:
                        fail += 1
                    self.emit('task_progress', {'task': task_id, 'log': f'  ❌ {name} hiba: {res.stdout[:100]}'})

                # Több-INF-es csomag fel nem használt INF-jeinek kivezetése (Razer-eset,
                # lásd _cleanup_unused_staged_infs). Reboot-igényes telepítésnél MOST nem
                # ítélkezünk (a kötés a következő bootnál dől el), de a csomagot nem
                # ejtjük: a defer=True feljegyzi a következő lábnak. Korábban itt egy
                # `if not pkg_reboot` állt, és a kihagyott csomag INF-jei örökre bent
                # maradtak - így hízott egy gép 23 csomagról 143-ra (2026-08-05, Latitude).
                if not is_offline:
                    pkg_reboot = (res.returncode == 3010
                                  or 'reboot is needed' in (res.stdout or '').lower())
                    self._cleanup_unused_staged_infs(res.stdout or '', name, task_id, defer=pkg_reboot)

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

                # KÖTÉS-ELLENŐRZÉS: a pnputil "Added driver packages: N" (N>0) csak annyit
                # jelent, hogy a csomag bekerült a DriverStore-ba - NEM azt, hogy az eszköz
                # át is vette. Terepen mérve (2026-07-25) két ilyen is volt egy futásban:
                # a Clevo-változatú Realtek hangdriver és egy NVIDIA katalógus-csomag
                # (32.0.15.9595), ami a nála SPECIFIKUSABB HWID-en álló 32.0.15.9186 mögött
                # maradt. Mindkettő "✅ telepítve"-ként jelent meg, a lánc pedig minden
                # lábon újra letöltötte őket. Sikernek csak a ténylegesen ÁTVETT driver
                # számít; a többi "kihagyva", és a hívó a következő lábra átviszi.
                if bind_checks:
                    now_info = self._get_installed_driver_info()
                    stuck = []
                    for drv, reboot_pending, pnp_out in bind_checks:
                        if reboot_pending:
                            continue   # csak a következő bootnál dől el - most nem ítélkezünk
                        cur = (now_info.get((drv.get('pnp_id') or '').upper()) or {})
                        cur_inf = (cur.get('inf') or '').strip().lower()
                        if cur_inf and cur_inf == drv.get('installed_inf'):
                            # A SZÜLŐ INF-je nem változott - de ez composite USB-nél NEM
                            # bukás: ott a gyári driver a &MI_xx gyerek-interfészre megy, a
                            # szülő pedig marad usbccgp-n, mert az a helyes driver rajta. A
                            # pnputil ilyenkor a gyereket nevezi meg ("installed/up-to-date
                            # on device: USB\VID_041E&PID_3274&MI_00\..."), és 2026-08-05-ig
                            # pont ezt az esetet könyveltük el "az eszköz nem vette át"-ként.
                            if package_bound_to_device_family(pnp_out, drv):
                                logging.info(f"[CATALOG_INSTALL] {drv.get('name')}: a csomag az eszköz "
                                             f"gyerek-interfészére kötött rá (a szülő marad "
                                             f"{drv.get('installed_inf')}, ez így helyes).")
                                bound_ok.append(drv)
                            else:
                                stuck.append(drv)
                        else:
                            bound_ok.append(drv)
                    for drv in stuck:
                        logging.warning(f"[CATALOG_INSTALL] A csomag felment, de az eszköz NEM vette át: "
                                        f"{drv.get('name')} - marad {drv.get('installed_inf')} "
                                        f"({drv.get('wu_title')})")
                        self.emit('task_progress', {'task': task_id, 'log': f'  ⚠️ {drv.get("name")}: a csomag feltelepült, de az eszköz TOVÁBBRA IS a régi driverén fut ({drv.get("installed_inf")}) - a Windows nem ezt választotta.'})
                        drv['no_bind_reason'] = 'felment, de az eszköz nem vette át'
                        no_bind.append(drv)
                    if stuck:
                        success -= len(stuck)
                        skipped += len(stuck)

                # Generikus -> gyári cserék utóellenőrzése (és szükség esetén visszaállítás).
                if generic_installs:
                    rolled_back = self._verify_generic_replacements(generic_installs, task_id)
                    if rolled_back:
                        success -= rolled_back
                        fail += rolled_back

            # TARTÓS NO-BIND EMLÉKEZET (catalog_no_bind.json): az autofix_stats.json-beli
            # lánc-szintű lista a lánc végén törlődik, ezért a kézi szken minden AutoFix
            # után újra ELŐRE BEJELÖLVE ajánlotta fel ugyanazokat a bizonyítottan nem-kötő
            # csomagokat (terepi log 2026-07-28: 5 katalógus-találatból 4 ilyen volt, és a
            # felhasználó jogosan hitte, hogy az AutoFix hagyott ki drivereket). Élő
            # rendszeren frissítjük; offline képnél kötés-ellenőrzés sincs, nincs adat.
            if not self.target_os_path and (no_bind or bound_ok):
                self._no_bind_record(no_bind, bound_ok)

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

    def _verify_generic_replacements(self, generic_installs, task_id='wu_install'):
        """A generikus -> gyári drivercserék utóellenőrzése, szükség esetén VISSZAÁLLÁS.

        Miért vállalható egyáltalán a csere: a Windows beépített drivere sosem tűnik el,
        csak háttérbe kerül - ha a frissen telepített gyári csomagot töröljük és
        újraszkennelünk, a PnP AUTOMATIKUSAN visszaköti a generikusat. Vagyis a művelet
        visszafordítható... DE csak olyan eszközön, ami futás közben újraköthető.

        EZ A HÁLÓ NEM FEDI A TÁROLÓVEZÉRLŐT. Ott a hiba a KÖVETKEZŐ bootnál jelentkezik
        (INACCESSIBLE_BOOT_DEVICE), amikor ez az ellenőrzés már rég lefutott - visszaállni
        csak helyreállító médiáról lehet. A tároló ezért 2026-07-28-ig fixen tiltva volt
        ezen a körön; azóta a fix indító dialógusának tároló-jelölőnégyzete engedheti be
        (wu_core.is_generic_replace_candidate allow_storage), alapból KI, és a felület
        piros figyelmeztetéssel, előre be nem jelölve hozza. Ha ide mégis érkezik
        tároló-eszköz, azt a felhasználó tájékozottan engedte - de a "sikeres" verdikt
        ilyenkor csak annyit jelent, hogy FUTÁS KÖZBEN nem lett hibás.

        Döntési szabály eszközönként:
          - hibakód 0            -> siker, marad a gyári driver;
          - hibakód != 0         -> a most telepített csomag törlése + rescan, majd
                                    egyetlen utóellenőrzés, és jelentés a felhasználónak;
          - az eszköz nincs a listában -> NEM állunk vissza (jellemzően kihúzott USB-s
                                    eszköz), csak jelezzük - egy lekérdezési hiba miatt
                                    kár lenne eldobni egy jó gyári drivert.
        Visszatérés: a visszaállított (tehát végül sikertelen) cserék száma."""
        logging.info(f"[GENERIC] {len(generic_installs)} generikus->gyári csere ellenőrzése...")
        self.emit('task_progress', {'task': task_id, 'log': '\n🔎 Gyári driverek ellenőrzése (visszaállítás, ha bármelyik hibás lett)...'})
        # A PnP-nek kell pár másodperc, amíg az új driverre átköti az eszközt.
        time.sleep(8)

        def device_error_codes():
            """{PNPDeviceID(nagybetűs): hibakód} a JELENLÉVŐ eszközökről."""
            try:
                ps = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                      "Get-WmiObject Win32_PnPEntity | Where-Object { $_.Present -eq $true } | "
                      "Select-Object PNPDeviceID, ConfigManagerErrorCode | ConvertTo-Json -Compress")
                res = self._run(["powershell", "-NoProfile", "-Command", ps], encoding='utf-8', timeout=120)
                data = json.loads(res.stdout) if (res.stdout or '').strip() else []
                if isinstance(data, dict):
                    data = [data]
                out = {}
                for d in data:
                    pid = (d.get('PNPDeviceID') or '').upper()
                    if pid:
                        try:
                            out[pid] = int(d.get('ConfigManagerErrorCode') or 0)
                        except (TypeError, ValueError):
                            out[pid] = 0
                return out
            except Exception as e:
                logging.warning(f"[GENERIC] Eszköz-állapot lekérdezése sikertelen: {e}")
                return None

        # A hibakód mellé a TÉNYLEGESEN betöltött INF is kell: a hibakód 0 csak annyit
        # jelent, hogy az eszköz működik - azt nem, hogy a gyári drivert vette át. Terepen
        # (2026-07-25) ez pontosan félrevezetett: a Realtek hangcsomag felment, a hibakód 0
        # maradt, a felület "✅ gyári driver működik"-et írt, miközben az eszköz végig a
        # Microsoft hdaudio.inf-jén futott (ugyanabban a futásban a záró egészségjelentés
        # már helyesen jelezte). Sikernek csak az számít, ha az INF is kicserélődött.
        inf_now = {}
        try:
            inf_now = self._get_installed_driver_info() or {}
        except Exception as e:
            logging.warning(f"[GENERIC] Telepített driver-infó lekérdezése sikertelen: {e}")

        codes = device_error_codes()
        if codes is None:
            # Nem tudjuk eldönteni - inkább hagyjuk állni a gyári drivert, de mondjuk meg.
            self.emit('task_progress', {'task': task_id, 'log': '⚠️ Az eszközök állapotát nem sikerült ellenőrizni - a gyári driverek fent maradtak. Nézd meg az Eszközkezelőt!'})
            return 0

        rolled_back = 0
        for drv, published_infs in generic_installs:
            name = drv.get('name') or '?'
            pnp_id = (drv.get('pnp_id') or '').upper()
            code = codes.get(pnp_id)
            if code is None:
                logging.warning(f"[GENERIC] {name}: az eszköz nincs a jelenlévők között, visszaállítás nélkül jelezzük.")
                self.emit('task_progress', {'task': task_id, 'log': f'  ⚠️ {name}: az eszköz eltűnt a listából (kihúzva?) - a gyári driver fent maradt.'})
                continue
            if code == 0:
                cur = (inf_now.get(pnp_id) or {})
                if cur and _is_inbox_driver(cur):
                    # Feltelepült, de az eszköz maradt a Windows driverén - NEM siker.
                    # Visszaállítani nincs mit (a generikus eleve rajta van), de kimondjuk.
                    logging.warning(f"[GENERIC] {name}: a gyári csomag felment, de az eszköz "
                                    f"a Windows driverén maradt ({cur.get('inf')}) - nem valódi csere.")
                    self.emit('task_progress', {'task': task_id, 'log': f'  ⚠️ {name}: a gyári csomag feltelepült, de az eszköz TOVÁBBRA IS a Windows driverén fut ({cur.get("inf")}) - valószínűleg más gépre szabott változat.'})
                    continue
                logging.info(f"[GENERIC] {name}: gyári driver OK (hibakód 0, INF: {cur.get('inf') or '?'}).")
                self.emit('task_progress', {'task': task_id, 'log': f'  ✅ {name}: gyári driver működik (eddig Microsoft alapdriver volt).'})
                continue

            desc = PNP_ERROR_CODE_DESCRIPTIONS.get(code, f'hibakód {code}')
            logging.warning(f"[GENERIC] {name}: a gyári driver után hibakód {code} - VISSZAÁLLÁS a Windows driverére.")
            self.emit('task_progress', {'task': task_id, 'log': f'  ⚠️ {name}: a gyári driver nem működik ({desc}) - visszaállás a Windows driverére...'})
            if not published_infs:
                # Nem tudjuk, mit publikált a pnputil - törölni sem tudunk pontosan.
                self.emit('task_progress', {'task': task_id, 'log': f'  ❌ {name}: nem sikerült azonosítani a telepített csomagot, kézi visszaállítás kellhet (Eszközkezelő -> Driver visszaállítása).'})
                rolled_back += 1
                continue
            for inf in published_infs:
                self._run(['pnputil', '/delete-driver', inf, '/uninstall', '/force'], ok_codes=(0, 3010), timeout=180)
            self._run(['pnputil', '/scan-devices'], timeout=180)
            time.sleep(5)
            after = device_error_codes() or {}
            new_code = after.get(pnp_id)
            rolled_back += 1
            if new_code == 0:
                self.emit('task_progress', {'task': task_id, 'log': f'  ↩️ {name}: visszaállítva a Windows alapdriverére, az eszköz újra működik.'})
            else:
                self.emit('task_progress', {'task': task_id, 'log': f'  ❌ {name}: a visszaállítás után is hibás (kód {new_code}) - indítsd újra a gépet, majd nézd meg az Eszközkezelőben!'})
        if rolled_back:
            self.emit('task_progress', {'task': task_id, 'log': f'ℹ️ {rolled_back} db gyári driver nem vált be, azoknál maradt a Windows alapdrivere.'})
        return rolled_back

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
