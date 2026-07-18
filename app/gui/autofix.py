"""DriverVarázsló GUI - 1 Kattintásos Driver Fix: a 3-lábú, reboot-láncolt AutoFix folyamat."""

# === AUTO-IMPORTS ===
import ctypes
import ctypes.wintypes
import os
import sys
import subprocess
import re
import time
import logging
import shutil
import json
from app.common import _app_data_dir
from app.common import _app_exe_path
from app.common import _ps_quote
from app import backup_core
from app import drivers_core
from app import nicpack_core
from app import wusettings_core
from app.ghost_core import build_ghost_ps
from app.ghost_core import parse_ghost_line
from app.wu_core import AUTOFIX_PRINTER_SKIP_CLASSES
from app.wu_core import WU_PNP_QUERY_PS
from app.wu_core import WuProcessAborted
from app.wu_core import _build_wu_install_ps
from app.wu_core import _filter_wu_downgrades
from app.wu_core import _filter_wu_scan_devices
from app.wu_core import _iter_process_lines
from app.wu_core import _match_wu_updates_to_devices
from app.wu_core import _collect_printer_protection
from app.wu_core import _is_printer_protected
from app.wu_core import _export_net_driver_backup
from app.wu_core import _restore_net_driver_backup
from app.gui.hwscan import PNP_ERROR_CODE_DESCRIPTIONS
from datetime import datetime
# === /AUTO-IMPORTS ===


class GuiAutofixMixin:
    """1 Kattintásos Driver Fix: a 3-lábú, reboot-láncolt AutoFix folyamat. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def _create_restore_point_sync(self, task_id='autofix'):
        desc = "DriverVarázsló AutoFix - " + datetime.now().strftime("%Y-%m-%d %H:%M")
        self.emit('task_progress', {'task': task_id, 'log': 'Registry Mentés (Restore Point) készítése folyamatban...', 'indeterminate': True})
        # Gyors (nem ellenőrzött) változat a közös backup_core-ból - az AutoFix egy
        # elutasított pont miatt nem áll meg.
        if backup_core.create_restore_point_quick(self._run, desc):
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
        # A registry-értékek a közös wusettings_core-ból (SearchOrderConfig=0 +
        # ExcludeWUDriversInQualityUpdate=1 - utóbbi akadályozza meg, hogy a Gépház
        # "Frissítések keresése" gombja drivereket is lehúzzon).
        wusettings_core.set_wu_driver_policy(self._run, disabled=True)
        self.emit('task_progress', {'task': task_id, 'log': '✅ Automatikus driver telepítés letiltva.\n'})

    def _delete_ghost_devices_sync(self, task_id='autofix', skip_classes=None):
        self.emit('task_progress', {'task': task_id, 'log': 'Nem csatlakoztatott (fantom) eszközök azonosítása és törlése...', 'indeterminate': True})
        # A közös scriptet használjuk (app/ghost_core.py) - az AutoFix csendesebb: a
        # per-eszköz rm/ok/fail eseményeket nem írja ki, csak az összegzőket.
        ps_script = build_ghost_ps(skip_classes)
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
            parsed = parse_ghost_line(line)
            if not parsed:
                continue
            event, data = parsed
            if event == 'skipped':
                if data > 0:
                    self.emit('task_progress', {'task': task_id, 'log': f'ℹ️ {data} db nyomtató/szkenner szellemeszköz kihagyva.\n'})
            elif event == 'total':
                self.emit('task_progress', {'task': task_id, 'log': f'{data} db szellemeszköz azonosítva. Törlés folyamatban...\n'})
            elif event == 'done':
                self.emit('task_progress', {'task': task_id, 'log': f'✅ {data}\n'})

        process.wait()

    def _delete_third_party_sync(self, task_id='autofix', skip_classes=None):
        self.emit('task_progress', {'task': task_id, 'log': 'Third-party driverek összegyűjtése és törlése...', 'indeterminate': True})
        drivers = self._get_third_party_drivers()
        skip_classes = skip_classes or set()
        if skip_classes:
            # Nyomtató-védelem 2.0: az osztály-alapú kihagyás mellett a jelenlévő
            # nyomtatási/szkennelési komponensek által TÉNYLEGESEN használt INF-eket és
            # a nyomtatóval jelen lévő gyártók összes csomagját is védjük - a multifunkciós
            # csomagok segéd-driverei (USB/Ports/SYSTEM osztály) különben törlődnének,
            # és az ügyfél nyomtatója/szkennere a fix után megsérülhetne.
            protected_infs, printing_vendors = _collect_printer_protection(self._run)
            skipped = [d for d in drivers if _is_printer_protected(d, protected_infs, printing_vendors, skip_classes)]
            skipped_keys = {id(d) for d in skipped}
            drivers = [d for d in drivers if id(d) not in skipped_keys]
            if skipped:
                self.emit('task_progress', {'task': task_id, 'log': f'🖨️ {len(skipped)} db nyomtatóhoz/szkennerhez tartozó driver védve (osztály + INF + gyártó alapú védelem).\n'})
        total = len(drivers)
        if total > 0:
            # 🛟 Hálózati mentőöv: a Net-driverek exportja törlés előtt - ha a lánc
            # folytatásánál nem lenne internet (a WU/beépített driver nem fedi le a
            # hálózati kártyát), ebből állítjuk vissza őket.
            backed_up = _export_net_driver_backup(self._run, drivers)
            if backed_up:
                self.emit('task_progress', {'task': task_id, 'log': f'🛟 {backed_up} db hálózati driver biztonsági mentése kész (vész-visszaállításhoz).\n'})
            self.emit('task_progress', {'task': task_id, 'log': f'{total} db third-party driver eltávolítása...\n'})
            for i, drv in enumerate(drivers):
                if self._cancel_flag: raise Exception("Magyar_Megszakit_Flag")
                name = drv.get('published', '')
                if not name: continue
                self.emit('task_progress', {'task': task_id, 'log': f'🗑 Törlés ({i+1}/{total}): {name}', 'current': i+1, 'total': total})
                # A közös törlő (drivers_core) - 3010 = siker, de reboot kell; az AutoFix úgyis újraindít.
                drivers_core.delete_driver_package(self._run, name)
            self.emit('task_progress', {'task': task_id, 'log': '✅ Driverek eltávolítva.\n'})
        else:
            self.emit('task_progress', {'task': task_id, 'log': '✅ Nincs third-party driver a rendszerben.\n'})

    def _scan_and_install_wu_sync(self, task_id='autofix'):
        max_loops = 4
        total_installed_in_session = 0

        # Kísérlet-számláló UpdateID-nként. A SIKERESEN telepített csomagot a következő
        # körben maga a WU szerver szűri ki (IsInstalled=0), ezért itt csak loop-védelem
        # kell: ami már 2x felajánlódott (tehát legalább egyszer elbukott vagy nem tudott
        # érvényesülni), azt nem próbáljuk tovább. A régi viselkedés (első felajánláskor
        # végleges kizárás) egy átmeneti letöltési hiba után a drivert véglegesen
        # kihagyta a maradék körökből.
        attempt_counts = {}
        devices_to_check = []
        watchdog_tripped = False

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
                except Exception as e:
                    # Nem néma: üres pnp_data = üres eszközlista = a WU-egyeztetés csendben kihagyna mindent.
                    logging.warning(f"[AUTOFIX] PnP JSON értelmezési hiba (üres eszközlistával folytatunk): {e}")
            devices_to_check = _filter_wu_scan_devices(pnp_data)

            self.emit('task_progress', {'task': task_id, 'log': f'✅ {len(devices_to_check)} hardverelem azonosítva. Egyeztetés...'})
            wu_results = self._search_wu_api() or []

            exclude_uids = {uid for uid, c in attempt_counts.items() if c >= 2}
            matches = _match_wu_updates_to_devices(wu_results, devices_to_check, exclude_uids=exclude_uids)

            # DOWNGRADE-VÉDELEM (közös mag: wu_core._filter_wu_downgrades): a WU néha a
            # telepítettnél RÉGEBBI csomagot ajánl (pl. friss gyári NVIDIA driver után) -
            # hibátlan eszközön az ilyet kihagyjuk, hibakódos eszközön sosem szűrünk.
            wu_by_uid = {w.get('UpdateID'): w for w in wu_results if w.get('UpdateID')}
            installed_info = self._get_installed_driver_info()
            matches, downgrades = _filter_wu_downgrades(matches, wu_by_uid, installed_info)
            for d in downgrades:
                self.emit('task_progress', {'task': task_id, 'log': f'[KIHAGYVA] Downgrade-védelem: {d["title"]} - {d["reason"]}'})

            matched_updates = [m['uid'] for m in matches]
            for uid in matched_updates:
                attempt_counts[uid] = attempt_counts.get(uid, 0) + 1

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

            # A sorokat a KÖZÖS _iter_process_lines olvassa (wu_core): cancel-ellenőrzés
            # fél másodpercenként + watchdog (30 perc néma folyamat leölve). A régi
            # közvetlen stdout-olvasás beragadt WU-keresésnél örökre blokkolt.
            try:
                for line in _iter_process_lines(process, self._run,
                                                cancel_check=lambda: getattr(self, '_cancel_flag', False)):
                    # A közös script kimeneti protokollja (INIT/SEARCH/FOUND/SKIP/TOTAL/DLONE/
                    # INSTONE/OK/OKRB/FAIL/EMPTY/DONE/ERROR) - lásd _build_wu_install_ps docstring.
                    if line.startswith("TOTAL:"):
                        self.emit('task_progress', {'task': task_id, 'log': '--- LETÖLTÉS ÉS TELEPÍTÉS ---'})
                    elif line.startswith("DLONE:"):
                        self.emit('task_progress', {'task': task_id, 'log': f'[LETOLTES] {line[6:].strip()}'})
                    elif line.startswith("INSTONE:"):
                        self.emit('task_progress', {'task': task_id, 'log': f'[TELEPITES] Telepítés alatt: {line[8:].strip()}'})
                    elif line.startswith("OKRB:"):
                        # Sikeres, újraindítás-igényes - az AutoFix lánc úgyis reboot-tal folytatódik.
                        total_installed_in_session += 1
                        self.emit('task_progress', {'task': task_id, 'log': f'[OK] SIKERES (újraindítás után él): {line[5:].strip()}'})
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
            except WuProcessAborted as ab:
                if ab.reason == 'cancel':
                    self.emit('task_progress', {'task': task_id, 'log': '\n❗ Megszakítva!'})
                    raise Exception("Magyar_Megszakit_Flag")
                # Watchdog: a WU telepítő 30 percig néma volt. Nincs értelme újabb WU
                # körnek (az is beragadna) - kilépünk a körökből, jöhet a katalógus-zárókör.
                watchdog_tripped = True
                self.emit('task_progress', {'task': task_id, 'log': '\n[HIBA] A WU telepítő 30 percen át nem adott életjelet - a watchdog leállította. Áttérés a katalógus-keresésre...'})
                break

        # --- KATALÓGUS-ZÁRÓKÖR ---
        # A WU API után a Microsoft Update Catalog-ot is ráengedjük a MÉG MINDIG hibakódos
        # (driver nélküli / hibás) eszközökre - a manuális szken hibrid kiegészítésének
        # AutoFix-megfelelője. Korábban az AutoFix kizárólag WU-ból dolgozott, és ha a WU
        # nem adott semmit egy eszközre, az hibásan maradt, pedig a katalógusban lett
        # volna driver. A már-telepített verzió-szűrő (a _catalog_find_driver-ben)
        # garantálja, hogy a lánc nem pörög végtelenségig ugyanazon a csomagon.
        try:
            if not getattr(self, '_cancel_flag', False):
                res = self._run(["powershell", "-NoProfile", "-Command", WU_PNP_QUERY_PS], encoding='utf-8')
                pnp_data = []
                if res.stdout:
                    try:
                        pnp_data = json.loads(res.stdout)
                    except Exception as e:
                        logging.warning(f"[AUTOFIX] PnP JSON értelmezési hiba (előző körös eszközlistával folytatunk): {e}")
                devices_now = _filter_wu_scan_devices(pnp_data) or devices_to_check
                problem_devs = [d for d in devices_now if d.get('err_code')]
                if watchdog_tripped and not problem_devs:
                    # A WU elhasalt, de hibakódos eszköz sincs - nincs mit keresni.
                    self.emit('task_progress', {'task': task_id, 'log': 'ℹ️ Nincs hibakódos eszköz, a katalógus-keresés kihagyva.'})
                elif problem_devs:
                    self.emit('task_progress', {'task': task_id, 'log': f'\n--- KATALÓGUS-ZÁRÓKÖR: {len(problem_devs)} még hibás eszköz keresése a Microsoft Update Catalogban... ---'})
                    inst_info = self._get_installed_driver_info()
                    found = self._catalog_search_collect(problem_devs, inst_info)
                    if found:
                        self.emit('task_progress', {'task': task_id, 'log': f'✅ A katalógusban {len(found)} eszközre van driver - telepítés...'})
                        s, _f, _c = self._install_catalog_sync(found, task_id=task_id)
                        total_installed_in_session += s
                    else:
                        self.emit('task_progress', {'task': task_id, 'log': 'ℹ️ A katalógusban sincs driver a maradék hibás eszközökre.'})
        except Exception as e:
            logging.warning(f"[AUTOFIX] Katalógus-zárókör hiba (nem kritikus): {e}")
            self.emit('task_progress', {'task': task_id, 'log': f'⚠️ Katalógus-zárókör hiba (a folyamat megy tovább): {e}'})

        return total_installed_in_session

    # ================================================================
    # LÁNC-STATISZTIKA (a záró összefoglalóhoz)
    # A 3-lábú lánc minden lába KÜLÖN processz, ezért a lábankénti telepítés-számot
    # egy app-adatmappabeli JSON-ban visszük át a reboot-okon; a záró láb összesíti.
    # ================================================================
    def _autofix_stats_path(self):
        return os.path.join(_app_data_dir(), 'autofix_stats.json')

    def _autofix_stats_clear(self):
        try:
            os.remove(self._autofix_stats_path())
        except OSError:
            pass

    def _autofix_stats_add(self, installed):
        """Egy láb telepítés-számának hozzáfűzése (hiba esetén csendben kimarad -
        az összefoglaló ilyenkor alulbecsül, de a láncot sosem akasztja meg)."""
        try:
            p = self._autofix_stats_path()
            data = {'legs': []}
            if os.path.exists(p):
                try:
                    with open(p, 'r', encoding='utf-8') as f:
                        data = json.load(f) or {'legs': []}
                except Exception:
                    data = {'legs': []}
            data.setdefault('legs', []).append({'installed': int(installed),
                                                'time': datetime.now().isoformat(timespec='seconds')})
            with open(p, 'w', encoding='utf-8') as f:
                json.dump(data, f)
        except Exception as e:
            logging.debug(f"[AUTOFIX-STATS] Mentés sikertelen: {e}")

    def _autofix_stats_total_and_clear(self):
        """A korábbi lábak összesített telepítés-száma; a fájl törlődik (a következő
        lánc tiszta lappal indul)."""
        total = 0
        try:
            p = self._autofix_stats_path()
            if os.path.exists(p):
                with open(p, 'r', encoding='utf-8') as f:
                    data = json.load(f) or {}
                total = sum(int(leg.get('installed') or 0) for leg in data.get('legs', []))
                os.remove(p)
        except Exception as e:
            logging.debug(f"[AUTOFIX-STATS] Összesítés sikertelen: {e}")
        return total

    def _emit_autofix_summary(self, chain_total, task_id='autofix'):
        """ZÁRÓ ÖSSZEFOGLALÓ a lánc legvégén: hány driver települt a TELJES lánc alatt,
        és mely eszközök maradtak hibakódosak (hogy a maradék lyuk sose legyen néma).
        Minden hibát elnyel - az összefoglaló sosem akaszthatja meg a lezárást."""
        try:
            self.emit('task_progress', {'task': task_id, 'log': f'\n📊 ÖSSZEFOGLALÓ: a teljes AutoFix lánc alatt összesen {chain_total} driver települt.'})
            res = self._run(["powershell", "-NoProfile", "-Command", WU_PNP_QUERY_PS], encoding='utf-8')
            pnp_data = []
            if res.stdout:
                try:
                    pnp_data = json.loads(res.stdout)
                except Exception as e:
                    logging.warning(f"[AUTOFIX] PnP JSON értelmezési hiba (a maradék hibás eszközök listája üres marad): {e}")
            problems = [d for d in _filter_wu_scan_devices(pnp_data) if d.get('err_code')]
            if problems:
                self.emit('task_progress', {'task': task_id, 'log': f'⚠️ Továbbra is hibakódos eszköz: {len(problems)} db'})
                for p in problems:
                    desc = PNP_ERROR_CODE_DESCRIPTIONS.get(p['err_code'], f"Hibakód: {p['err_code']}")
                    self.emit('task_progress', {'task': task_id, 'log': f"   • {p['name']} - {desc} (kód {p['err_code']})"})
                self.emit('task_progress', {'task': task_id, 'log': 'Ezekhez a "Driver Keresés és Telepítés" menü Problémás eszközök szekciója adhat még megoldást.'})
            else:
                self.emit('task_progress', {'task': task_id, 'log': '✅ Nem maradt hibakódos eszköz a rendszerben!'})
        except Exception as e:
            logging.warning(f"[AUTOFIX] Összefoglaló hiba (nem kritikus): {e}")

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
                    # Friss lánc indul - egy esetleges korábbi (félbehagyott) lánc
                    # statisztikája ne számítson bele az összefoglalóba.
                    self._autofix_stats_clear()

                    self._disable_sleep_sync()
                    
                    self.emit('task_progress', {'task': 'autofix', 'log': 'WU szüneteltetése 1 hétre...'})
                    # Fix (nem hosszabbító) 7 napos szünet a közös builderből.
                    self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                               wusettings_core.build_wu_pause_ps(7, additive=False)])
                    self.emit('task_progress', {'task': 'autofix', 'log': '✅ WU szüneteltetve 1 hétre.\n'})

                    self.emit('task_progress', {'task': 'autofix', 'log': '🔄 A számítógép újraindul, majd a folyamat a rendszer előkészítésével folytatódik!'})
                    
                    exe_path = _app_exe_path()
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
                        self._run(["powershell", "-NoProfile", "-Command", 'Unregister-ScheduledTask -TaskName "DriverVarazsloResume" -Confirm:$false -ErrorAction SilentlyContinue'], ok_codes=(0, 1))  # 1: a feladat már nem létezik (idempotens duplatörlés)
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
                    self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", wusettings_core.WU_STOP_SERVICES_PS])
                    # ok_codes=(0, 1): az 1-es kód a "kulcs nem létezik" - nincs beragadt reboot-jelzés, várt eset.
                    self._run(['reg', 'delete', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired', '/f'], ok_codes=(0, 1))

                    self.emit('task_progress', {'task': 'autofix', 'log': 'Beragadt frissítések és WU gyorsítótár (SoftwareDistribution) ürítése...'})
                    wusettings_core._clear_software_distribution(self._run)

                    self.emit('task_progress', {'task': 'autofix', 'log': 'WU szüneteltetése 1 hétre...'})
                    # Fix (nem hosszabbító) 7 napos szünet a közös builderből.
                    self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                               wusettings_core.build_wu_pause_ps(7, additive=False)])
                    self.emit('task_progress', {'task': 'autofix', 'log': '✅ WU gyorsítótár ürítve és szüneteltetve 1 hétre.\n'})
                    
                    self.emit('task_progress', {'task': 'autofix', 'log': '🔄 A számítógép újraindul, majd a folyamat automatikusan a TELEPÍTÉSSEL folytatódik!'})
                    
                    exe_path = _app_exe_path()
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
                    self._run(["powershell", "-NoProfile", "-Command", 'Unregister-ScheduledTask -TaskName "DriverVarazsloResume" -Confirm:$false -ErrorAction SilentlyContinue'], ok_codes=(0, 1))  # 1: a feladat már nem létezik (idempotens duplatörlés)
                    self.emit('task_progress', {'task': 'autofix', 'log': 'Láncolt folytatás gépújraindítás után. Régi driverek törlése kihagyva, hogy ne töröljünk friss drivereket.\n'})
                    self._disable_sleep_sync()

                    # 🛟 Hálózati mentőöv: ha a driver-törlés után a gép internet nélkül
                    # maradt (a WU/beépített driver nem fedte le a hálózati kártyát -
                    # terepen látott eset friss AM5-ös Realtek 2.5GbE-vel), a törlés előtt
                    # elmentett Net-drivereket visszatöltjük, különben a lánc WU-keresése
                    # esélytelen lenne.
                    if not self._check_internet():
                        self.emit('task_progress', {'task': 'autofix', 'log': '🛟 Nincs internet a driver-törlés után! Mentett hálózati driverek visszaállítása...'})
                        net_ok = False
                        if _restore_net_driver_backup(self._run):
                            self._run(['pnputil', '/scan-devices'])
                            time.sleep(15)
                            net_ok = self._check_internet()
                            if net_ok:
                                self.emit('task_progress', {'task': 'autofix', 'log': '✅ Hálózat helyreállítva a mentett driverekből!\n'})
                        else:
                            self.emit('task_progress', {'task': 'autofix', 'log': '⚠️ Nincs mentett hálózati driver.'})
                        if not net_ok:
                            # UTOLSÓ ESÉLY: a NIC mentőcsomag (nicpack_core) HELYI forrásból
                            # (exe mellett / app-adatmappa) - letöltésre net nélkül úgysincs
                            # mód. Ugyanaz a csomag, amit a "LAN Mentőcsomag" gomb használ.
                            try:
                                if nicpack_core._find_nicpack_zip():
                                    self.emit('task_progress', {'task': 'autofix', 'log': '🛟 NIC mentőcsomag (nicpack.zip) megtalálva helyben - telepítés...'})
                                    nicpack_core._install_nicpack(
                                        self._run,
                                        lambda t: self.emit('task_progress', {'task': 'autofix', 'log': t}))
                                    time.sleep(15)
                                    net_ok = self._check_internet()
                                    if net_ok:
                                        self.emit('task_progress', {'task': 'autofix', 'log': '✅ Hálózat helyreállítva a NIC mentőcsomagból!\n'})
                                else:
                                    self.emit('task_progress', {'task': 'autofix', 'log': 'ℹ️ nicpack.zip sincs az exe mellett / a DriverVarazslo mappában - ezt sem tudjuk bevetni.'})
                            except Exception as e:
                                logging.warning(f"[AUTOFIX] NIC mentőcsomag telepítési hiba: {e}")
                                self.emit('task_progress', {'task': 'autofix', 'log': f'⚠️ NIC mentőcsomag telepítése sikertelen: {e}'})
                        if not net_ok:
                            self.emit('task_progress', {'task': 'autofix', 'log': '⚠️ A hálózat továbbra sem él - a WU keresés így valószínűleg üres lesz. Ellenőrizd a kábelt/Wi-Fi-t!\n'})

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
                    # Láb-statisztika a záró összefoglalóhoz (reboot-okon átívelő számláló).
                    self._autofix_stats_add(installed_count)
                    # Set RunOnce
                    exe_path = _app_exe_path()
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
                    self._run(["powershell", "-NoProfile", "-Command", 'Unregister-ScheduledTask -TaskName "DriverVarazsloResume" -Confirm:$false -ErrorAction SilentlyContinue'], ok_codes=(0, 1))  # 1: a feladat már nem létezik (idempotens duplatörlés)

                    # ZÁRÓ ÖSSZEFOGLALÓ: lánc-szintű telepítés-szám + maradék hibakódos eszközök.
                    self._emit_autofix_summary(self._autofix_stats_total_and_clear())

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
                    except Exception as e:
                        logging.debug(f"[AUTOFIX] Záró emit sikertelen (ablak már bezárva?): {e}")
                    
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
