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
from app import dupdrivers_core
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
from app.wu_core import WU_MAX_CONSECUTIVE_FAILURES
from app.wu_core import _filter_wu_older_duplicates
from app.wu_core import _install_abort_reason
from app.wu_core import is_reboot_pending
from app.wu_core import verify_failed_installs
from app.wu_core import unoffered_requested_titles
from app.common import spawn_failed
from app.drivers_core import DELETE_DRIVER_TIMEOUT
from app.gui.hwscan import PNP_ERROR_CODE_DESCRIPTIONS
from datetime import datetime
# === /AUTO-IMPORTS ===


# Hány "töröljük be a maradékot" kör futhat (mindegyik egy újraindítással). A gyakorlatban
# egy kör elég; a plafon csak a végtelen ciklus ellen véd, a tényleges leállási feltétel az,
# hogy egy kör alatt haladjunk (ha nulla csomag törlődik, azok eltávolíthatatlanok).
AUTOFIX_MAX_DELETE_ROUNDS = 3

# Hány TELEPÍTŐ láb futhat egy láncban (a pending-reboot miatti újraindításokkal együtt).
# A lánc önmagát láncolja tovább, amíg települ valami vagy reboot van függőben - ez a
# plafon zárja ki a végtelen újraindulás-ciklust egy sosem gyógyuló gépen.
AUTOFIX_MAX_INSTALL_LEGS = 3


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
        """Third-party csomagok törlése. Visszatérés: 'ok' (végigért) vagy 'wedged'
        (beragadt eszközverem - a hívónak újra kell indítania és FOLYTATNIA a törlést;
        lásd drivers_core.delete_stalled)."""
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
            stalled_streak = 0
            deferred = []   # beragadt csomagok - az újraindítás utáni lábon próbáljuk újra
            for i, drv in enumerate(drivers):
                if self._cancel_flag: raise Exception("Magyar_Megszakit_Flag")
                name = drv.get('published', '')
                if not name: continue
                self.emit('task_progress', {'task': task_id, 'log': f'🗑 Törlés ({i+1}/{total}): {name}', 'current': i+1, 'total': total})
                # A közös törlő (drivers_core) - 3010 = siker, de reboot kell; az AutoFix úgyis újraindít.
                # timeout: egy nem válaszoló eszközverem (terepen: Intel RST tárolóvezérlő)
                # különben percekig lógatja a pnputil-t és megakasztja az egész lábat.
                res = drivers_core.delete_driver_package(self._run, name, timeout=DELETE_DRIVER_TIMEOUT)

                # BERAGADT ESZKÖZVEREM: nem őrlünk tovább csomagonként ~1,5-2,5 percet.
                # Ha közben pending-reboot is áll (a tárolóvezérlő törlése után ez a tipikus),
                # az újraindítás bizonyítottan feloldja: a terepi logban ugyanaz a csomag a
                # reboot utáni lábon 0,5 mp alatt törlődött. A 2. egymást követő beragadás
                # akkor is megállít, ha a reboot-jelző valamiért nem áll.
                if drivers_core.delete_stalled(res):
                    stalled_streak += 1
                    # A beragadt csomag MINDIG a "későbbre" listára kerül - akkor is, ha most
                    # továbbmegyünk. E nélkül (terepi futás, Build 224) az iastorhsa_ext.inf
                    # egyszerűen kimaradt: a törlés nem sikerült, a WU meg telepítettként látta,
                    # így az a driver sosem cserélődött ki. Az újraindítás utáni söprésben
                    # viszont 0,5 mp alatt lemegy.
                    deferred.append(drv)
                    self.emit('task_progress', {'task': task_id, 'log': f'⏱️ {name}: az eszköz nem válaszol a törlési kérésre - az újraindítás utánra halasztva.'})
                    if stalled_streak >= 2 or is_reboot_pending(self._run):
                        # NEM indítunk újra itt, és NEM őrlünk tovább: a maradék csomag
                        # mindegyike ugyanígy beragadna (csomagonként ~1,5-2,5 perc). A lánc
                        # úgyis újraindul pár lépéssel lejjebb - a maradékot a KÖVETKEZŐ láb
                        # elején söpörjük be, ahol friss boot után 0,5 mp/csomag (terepen mérve).
                        pending = deferred + [d for d in drivers[i + 1:] if d.get('published')]
                        self._autofix_stats_set('pending_deletes', pending)
                        self.emit('task_progress', {'task': task_id, 'log': f'\n⚠️ A Windows eszközkezelője beragadt (újraindítás nélkül ezek nem távolíthatók el).'})
                        self.emit('task_progress', {'task': task_id, 'log': f'⏭️ A maradék {len(pending)} csomagot NEM erőltetjük most (csomagonként percekbe telne) - az újraindítás után, másodpercek alatt törlődnek.\n'})
                        return 'wedged'
                    continue
                stalled_streak = 0

                if spawn_failed(res):
                    # A folyamat EL SEM INDULT (0xC0000142) - a session szétesett, minden
                    # további törlés garantált no-op lenne. NEM megyünk tovább és NEM
                    # jelentünk sikert: korábban pont ez a néma hamis siker vitte rá a
                    # láncot, hogy 17 nem törölt csomag után "✅ Driverek eltávolítva"-t
                    # írjon ki és újrainduljon (terepi log, Build 218).
                    raise Exception(
                        f"A Windows nem tud több folyamatot indítani (0xC0000142) a(z) {name} törlésénél - "
                        "a rendszer eszközkezelője szétesett vagy leállás alatt van. "
                        "A driver-törlés FÉLBEMARADT. Indítsd újra a gépet, majd futtasd újra az 1 kattintásos fixet!")
            if deferred:
                # Végigértünk, de maradt beragadt csomag - az újraindítás utáni láb söpri be.
                self._autofix_stats_set('pending_deletes', deferred)
                self.emit('task_progress', {'task': task_id, 'log': f'✅ Driverek eltávolítva ({len(deferred)} db az újraindítás után fejeződik be).\n'})
                return 'ok'
            self.emit('task_progress', {'task': task_id, 'log': '✅ Driverek eltávolítva.\n'})
        else:
            self.emit('task_progress', {'task': task_id, 'log': '✅ Nincs third-party driver a rendszerben.\n'})
        return 'ok'

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
        # Telepítés-hibával (nem letöltési hibával) bukott UpdateID-k: ezeket NEM próbáljuk
        # újra a következő körökben. Field-seen (Build 214, Dell OptiPlex): 8 driver code=4-gyel
        # bukott, mindegyik ~2,5 perc, és a régi 1-retry politika miatt a 2. kör újra végigment
        # rajtuk (~+20 perc a semmiért). Egy code=4 telepítés-hiba ugyanabban a session-ben
        # gyakorlatilag sosem gyógyul retry-ra; a letöltési hiba (átmeneti hálózat) viszont
        # kaphat egy retry-t az attempt_counts-on keresztül, ezért azt itt nem szűrjük.
        install_failed_uids = set()
        devices_to_check = []
        watchdog_tripped = False
        # Igaz, ha a kört pending-reboot (vagy sorozatos hiba) miatt szakítottuk meg: ilyenkor
        # a hívó (run_autofix) akkor is újraindít és láncol egy újabb telepítő lábat, ha
        # egyetlen driver sem települt ebben a lábban - a maradék ott fog tisztán felmenni.
        self._autofix_reboot_pending = False

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

            exclude_uids = {uid for uid, c in attempt_counts.items() if c >= 2} | install_failed_uids
            matches = _match_wu_updates_to_devices(wu_results, devices_to_check, exclude_uids=exclude_uids)

            # DOWNGRADE-VÉDELEM (közös mag: wu_core._filter_wu_downgrades): a WU néha a
            # telepítettnél RÉGEBBI csomagot ajánl (pl. friss gyári NVIDIA driver után) -
            # hibátlan eszközön az ilyet kihagyjuk, hibakódos eszközön sosem szűrünk.
            wu_by_uid = {w.get('UpdateID'): w for w in wu_results if w.get('UpdateID')}
            installed_info = self._get_installed_driver_info()
            matches, downgrades = _filter_wu_downgrades(matches, wu_by_uid, installed_info)
            for d in downgrades:
                self.emit('task_progress', {'task': task_id, 'log': f'[KIHAGYVA] Downgrade-védelem: {d["title"]} - {d["reason"]}'})

            # CSAK A LEGÚJABB VERZIÓ csomagcsaládonként (közös mag: _filter_wu_older_duplicates).
            # A WU a teljes csomag-történetet felajánlja (terepen 10 db iigd_ext Intel UHD 630
            # Extension 2018-tól), amiből régen mind fel is települt - feleslegesen.
            matches, older_dups = _filter_wu_older_duplicates(matches, wu_by_uid)
            if older_dups:
                self.emit('task_progress', {'task': task_id, 'log': f'📦 {len(older_dups)} db elavult verzió kihagyva (csomagcsaládonként csak a legújabb települ).'})
                for d in older_dups:
                    logging.debug(f"[AUTOFIX] Régebbi verzió kihagyva: {d['title']} - {d['reason']}")

            matched_updates = [m['uid'] for m in matches]
            for uid in matched_updates:
                attempt_counts[uid] = attempt_counts.get(uid, 0) + 1
            # A telepítő script a Title-t írja vissza a FAIL/OK sorokban (nem az UpdateID-t),
            # ezért a bukott UID kiszűréséhez Title -> UpdateID visszakeresés kell.
            title_to_uid = {m['title']: m['uid'] for m in matches}

            if not matched_updates:
                self.emit('task_progress', {'task': task_id, 'log': '✅ Szerveren nincs újabb valós illesztőprogram.'})
                self.emit('task_progress', {'task': task_id, 'log': 'Minden elérhető driver telepítve! Keresési lánc befejezve.'})
                break

            self.emit('task_progress', {'task': task_id, 'log': f'✅ Telepítendő driverek száma: {len(matched_updates)}'})

            # A kör ELŐTTI csomaglista a "sikertelen" telepítések utóellenőrzéséhez
            # (verify_failed_installs): a WUA orcFailed(4)-et ad olyan csomagokra is,
            # amelyeket a PnP közben rendben letett a DriverStore-ba.
            pkgs_before = self._get_third_party_drivers()
            round_failed_titles = []
            round_found_titles = []   # amikre a script FOUND: sort adott (lásd a kör végi ellenőrzést)
            consecutive_failures = 0
            reboot_pending = False
            check_reboot_after_line = False

            def _abort_check():
                """Az _iter_process_lines soronként hívja. A (PowerShell-es, ~0,5 mp)
                pending-reboot lekérdezés CSAK telepítési hiba után fut le: az az egyetlen
                jel, ami a "mérgezett session"-t bizonyítja. Ha ilyenkor áll a reboot-jelző,
                a maradék csomag is darabonként ~2,5 perc után hamis hibát adna - kör vége."""
                nonlocal reboot_pending, check_reboot_after_line
                if check_reboot_after_line:
                    check_reboot_after_line = False
                    if is_reboot_pending(self._run):
                        reboot_pending = True
                return _install_abort_reason(consecutive_failures, reboot_pending)

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
            aborted_reason = None
            try:
                for line in _iter_process_lines(process, self._run,
                                                cancel_check=lambda: getattr(self, '_cancel_flag', False),
                                                abort_check=_abort_check):
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
                        # SZÁNDÉKOSAN nem szakítjuk meg itt a kört, pedig ilyenkor már áll a
                        # pending-reboot jelző: a terepi logban az OKRB-s Display driver UTÁN
                        # még három csomag simán felment. Amíg sikerülnek a telepítések, megyünk
                        # tovább; a megszakítás jele a HIBA (lásd a FAIL ágat), nem a reboot-igény.
                        total_installed_in_session += 1
                        consecutive_failures = 0
                        self.emit('task_progress', {'task': task_id, 'log': f'[OK] SIKERES (újraindítás után él): {line[5:].strip()}'})
                    elif line.startswith("OK:"):
                        total_installed_in_session += 1
                        consecutive_failures = 0
                        self.emit('task_progress', {'task': task_id, 'log': f'[OK] SIKERES: {line[3:].strip()}'})
                    elif line.startswith("FAIL:"):
                        fail_text = line[5:].strip()  # pl. "[kód=4] Intel..." vagy "[LETÖLTÉS HIBA] ..."
                        # A telepítés-hibás címeket csak GYŰJTJÜK; hogy tényleg hiba volt-e,
                        # a kör után a DriverStore dönti el (verify_failed_installs) - a WUA
                        # ugyanis hamis orcFailed(4)-et is ad ténylegesen felkerült csomagra.
                        # A letöltési hiba átmeneti lehet, azt az attempt_counts 1-retry-ja fedi.
                        if 'LETÖLTÉS HIBA' not in fail_text:
                            round_failed_titles.append(re.sub(r'^\[[^\]]*\]\s*', '', fail_text))
                            consecutive_failures += 1
                            check_reboot_after_line = True
                        self.emit('task_progress', {'task': task_id, 'log': f'[HIBA] SIKERTELEN: {fail_text}'})
                    elif line.startswith("EMPTY:"):
                        self.emit('task_progress', {'task': task_id, 'log': f'[FIGYELMEZTETES] {line[6:].strip()}'})
                    elif line.startswith("ERROR:"):
                        logging.error(f"[AUTOFIX-WU] PowerShell hiba: {line[6:].strip()}")
                        self.emit('task_progress', {'task': task_id, 'log': f'[HIBA] {line[6:].strip()}'})
                    elif line.startswith("DONE:"):
                        self.emit('task_progress', {'task': task_id, 'log': f'--- {line[5:].strip()} ---'})
                    elif line.startswith("FOUND:"):
                        # Csak gyűjtjük: a kör végén ebből derül ki, ha egy KÉRT csomag
                        # nem került be a telepítési listába (unoffered_requested_titles).
                        round_found_titles.append(line[6:].strip())
                    elif line.startswith("INIT:") or line.startswith("SEARCH:") or line.startswith("SKIP:"):
                        pass  # protokoll-sorok, a kör elején már kiírtuk az összesítést
                    else:
                        self.emit('task_progress', {'task': task_id, 'log': line})
            except WuProcessAborted as ab:
                aborted_reason = ab.reason
                if ab.reason == 'cancel':
                    self.emit('task_progress', {'task': task_id, 'log': '\n❗ Megszakítva!'})
                    raise Exception("Magyar_Megszakit_Flag")
                elif ab.reason == 'reboot':
                    # A gép pending-reboot állapotba került (jellemzően egy tárolóvezérlő /
                    # chipset driver telepítése után). Innentől a WUA minden további
                    # csomagra ~2,5 perc várakozás után hamis hibát adna - kör vége.
                    self.emit('task_progress', {'task': task_id, 'log': '\n🔄 A rendszer újraindítást igényel - a maradék driver ebben az állapotban nem tud rendesen települni.'})
                    self.emit('task_progress', {'task': task_id, 'log': 'A telepítés az újraindítás után automatikusan folytatódik!'})
                elif ab.reason == 'failstreak':
                    self.emit('task_progress', {'task': task_id, 'log': f'\n⚠️ {WU_MAX_CONSECUTIVE_FAILURES} egymást követő telepítési hiba - a Windows Update ebben az állapotban nem tud tovább dolgozni.'})
                    self.emit('task_progress', {'task': task_id, 'log': 'Újraindítás után újrapróbáljuk a maradékot!'})
                else:
                    # Watchdog: a WU telepítő 30 percig néma volt. Nincs értelme újabb WU
                    # körnek (az is beragadna) - kilépünk a körökből, jöhet a katalógus-zárókör.
                    watchdog_tripped = True
                    self.emit('task_progress', {'task': task_id, 'log': '\n[HIBA] A WU telepítő 30 percen át nem adott életjelet - a watchdog leállította. Áttérés a katalógus-keresésre...'})

            # --- ELTŰNT CSOMAGOK: amit kértünk, de a script már nem talált meg ---
            # E nélkül a "Telepítendő driverek száma: 3" után némán 2 települt (terepi log).
            if not aborted_reason:
                unoffered = unoffered_requested_titles(title_to_uid.keys(), round_found_titles)
                for t in unoffered:
                    self.emit('task_progress', {'task': task_id, 'log': f'[KIHAGYVA] {t} - a Windows Update már telepítettként látja, nincs mit telepíteni.'})

            # --- KÖR UTÁNI UTÓELLENŐRZÉS: mi bukott el VALÓJÁBAN? ---
            # A WUA hamis orcFailed(4)-et is ad olyan csomagra, amit a PnP közben rendben
            # letett a DriverStore-ba (terepen mind a 8 "bukott" driver felkerült). Amit a
            # csomaglista igazol, az siker: beleszámít, és NEM kerül a végleges tiltólistára.
            if round_failed_titles:
                verified = verify_failed_installs(round_failed_titles, pkgs_before, self._get_third_party_drivers())
                if verified:
                    total_installed_in_session += len(verified)
                    self.emit('task_progress', {'task': task_id, 'log': f'\nℹ️ Utóellenőrzés: {len(verified)} "sikertelen" driver valójában FELKERÜLT a rendszerre (a Windows Update jelentése félrevezető volt):'})
                    for t in sorted(verified):
                        self.emit('task_progress', {'task': task_id, 'log': f'   ✅ {t}'})
                for title in round_failed_titles:
                    if title in verified:
                        continue
                    fuid = title_to_uid.get(title)
                    if fuid:
                        install_failed_uids.add(fuid)

            if aborted_reason in ('reboot', 'failstreak'):
                # A hívó (run_autofix) ebből tudja, hogy akkor is újra kell indítani és
                # láncolni a következő telepítő lábat, ha 0 driver települt ebben a körben.
                self._autofix_reboot_pending = True
                break
            if aborted_reason == 'hang':
                break

        # --- KATALÓGUS-ZÁRÓKÖR ---
        # A WU API után a Microsoft Update Catalog-ot is ráengedjük a MÉG MINDIG hibakódos
        # (driver nélküli / hibás) eszközökre - a manuális szken hibrid kiegészítésének
        # AutoFix-megfelelője. Korábban az AutoFix kizárólag WU-ból dolgozott, és ha a WU
        # nem adott semmit egy eszközre, az hibásan maradt, pedig a katalógusban lett
        # volna driver. A már-telepített verzió-szűrő (a _catalog_find_driver-ben)
        # garantálja, hogy a lánc nem pörög végtelenségig ugyanazon a csomagon.
        # Pending-reboot állapotban a katalógus-telepítés is ugyanabba a falba futna
        # (és percekbe kerülne) - kihagyjuk, az újraindítás utáni láb újra nekifut.
        if getattr(self, '_autofix_reboot_pending', False):
            self.emit('task_progress', {'task': task_id, 'log': 'ℹ️ Katalógus-zárókör elhalasztva az újraindítás utánra.'})
            return total_installed_in_session

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
    def _schedule_autofix_resume(self, resume_flag, task_id='autofix'):
        """Az ÚJRAINDÍTÁS UTÁNI folytatás beütemezése (DriverVarazsloResume feladat).

        Mindhárom láncolási pont (A láb -> --resume-step1, B láb -> --resume-autofix,
        telepítő láb -> --resume-autofix) ezen keresztül megy: korábban ugyanez a ~25 sor
        háromszor szerepelt, és bármelyik módosítása után szétcsúszhatott a másik kettő.

        A feladat AtLogOn triggerrel, interaktív + legmagasabb jogosultsággal fut - a
        folytatást ténylegesen az ui.html indítja el (get_init_data resume flag-jei
        alapján), ezért a GUI-nak láthatóan és adminként kell elindulnia."""
        exe_path = _app_exe_path()
        temp_env = os.environ.get('TEMP', '!!').lower()
        # Ha temp mappából fut a program, a következő indulásig törlődhet alóla az exe -
        # ilyenkor a Public mappába másolt példányt ütemezzük.
        if temp_env in exe_path.lower():
            try:
                public_dir = os.environ.get('PUBLIC', 'C:\\Users\\Public')
                safe_exe = os.path.join(public_dir, "DriverVarazslo_Resume.exe" if getattr(sys, 'frozen', False) else "DriverVarazslo_Resume.py")
                shutil.copy2(exe_path, safe_exe)
                exe_path = safe_exe
                self.emit('task_progress', {'task': task_id, 'log': 'ℹ️ Temp mappából futás detektálva. Biztonsági másolat készítve a Public mappába.'})
            except Exception as e:
                logging.error(f"[AUTOFIX] Biztonsági másolat hiba: {e}")

        if getattr(sys, 'frozen', False):
            exec_path, args = exe_path, resume_flag
        else:
            exec_path, args = sys.executable, f'"{exe_path}" {resume_flag}'

        # Az idézőjelek egyszeresek: a _ps_quote nélkül egy aposztrófos felhasználónév
        # (C:\Users\O'Brien\...) széttörné a generált parancsot és megölné a láncot.
        task_ps = f'''
        $action = New-ScheduledTaskAction -Execute '{_ps_quote(exec_path)}' -Argument '{_ps_quote(args)}'
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
        Register-ScheduledTask -TaskName "DriverVarazsloResume" -Action $action -Trigger $trigger -Principal $principal -Force
        '''
        self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", task_ps])

    def _reboot_or_cancel(self, status, task_id='autofix'):
        """A lánc újraindítási pontja: 5 mp türelmi idő, ALATTA a Mégse gomb még megfog.

        Korábban a `time.sleep(5)` után feltétel nélkül jött a `shutdown` - terepen
        bizonyított (Build 224): a felhasználó megnyomta a "Folyamat Leállítása" gombot,
        a megszakítás rögzült, és a gép 5 másodperccel később MÉGIS újraindult.

        Megszakításkor az ütemezett feladatot is töröljük, különben a lánc a következő
        bejelentkezéskor magától folytatódna - vagyis a Mégse csak látszólag állítaná meg."""
        self.emit('task_complete', {'task': task_id, 'status': status})
        for _ in range(10):          # 10 x 0,5 mp = a régi 5 mp-es ablak
            if getattr(self, '_cancel_flag', False):
                self._run(["powershell", "-NoProfile", "-Command",
                           'Unregister-ScheduledTask -TaskName "DriverVarazsloResume" -Confirm:$false -ErrorAction SilentlyContinue'],
                          ok_codes=(0, 1))
                self.emit('task_progress', {'task': task_id, 'log': '\n❗ Megszakítva - az újraindítás elmarad, a folytatás törölve.'})
                raise Exception("Magyar_Megszakit_Flag")
            time.sleep(0.5)
        self._run(['shutdown', '/r', '/t', '0', '/f'])

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

    def _autofix_stats_set(self, key, value):
        """Tetszőleges kulcs eltárolása a lánc-állapot JSON-ban (a lábak KÜLÖN processzek,
        ezért csak fájlon át tudnak üzenni egymásnak). Hibát elnyel."""
        try:
            p = self._autofix_stats_path()
            data = {}
            if os.path.exists(p):
                try:
                    with open(p, 'r', encoding='utf-8') as f:
                        data = json.load(f) or {}
                except Exception:
                    data = {}
            data[key] = value
            with open(p, 'w', encoding='utf-8') as f:
                json.dump(data, f)
        except Exception as e:
            logging.debug(f"[AUTOFIX-STATS] '{key}' mentése sikertelen: {e}")

    def _autofix_stats_get(self, key, default=None):
        """A _autofix_stats_set párja. Hibánál/hiányzó kulcsnál a default."""
        try:
            p = self._autofix_stats_path()
            if os.path.exists(p):
                with open(p, 'r', encoding='utf-8') as f:
                    data = json.load(f) or {}
                return data.get(key, default)
        except Exception as e:
            logging.debug(f"[AUTOFIX-STATS] '{key}' olvasása sikertelen: {e}")
        return default

    def _finish_pending_deletes(self, task_id='autofix'):
        """A beragadt eszközverem miatt félbehagyott törlések BEFEJEZÉSE a friss boot után.

        Az előző láb (0. LÉPÉS) csak akkor hagy itt listát, ha a pnputil beleakadt egy
        eltávolíthatatlan csomagba - ilyenkor a maradékot nem erőltette, mert újraindítás
        előtt csomagonként ~1,5-2,5 percbe telt volna. Újraindítás után ugyanaz a csomag
        0,5 mp alatt törlődik (terepen mérve), tehát itt fut le gyorsan, EXTRA ÚJRAINDÍTÁS
        NÉLKÜL - ez a lánc amúgy is meglévő reboot-ját használja ki.

        Biztonsági korlát: csak azokat a csomagokat törli, amelyek MÉG MINDIG ugyanazzal az
        eredeti INF-névvel szerepelnek a DriverStore-ban - így egy időközben újraszámozott
        oemXX.inf semmiképp nem egy friss drivert töröl le.

        Visszatérés: (állapot, hány csomag törlődött) - az állapot 'ok' (végigért) vagy
        'wedged' (megint beragadt; a hívó dönt az újabb reboot-körről)."""
        pending = self._autofix_stats_get('pending_deletes') or []
        if not pending:
            return 'ok', 0
        self._autofix_stats_set('pending_deletes', [])

        current = {d.get('published', '').lower(): d for d in self._get_third_party_drivers()}
        todo = []
        for p in pending:
            pub = (p.get('published') or '').lower()
            cur = current.get(pub)
            # Csak akkor töröljük, ha ugyanaz az EREDETI INF név van most is a helyén.
            if cur and (cur.get('original') or '').lower() == (p.get('original') or '').lower():
                todo.append(p)
        if not todo:
            self.emit('task_progress', {'task': task_id, 'log': 'ℹ️ A korábban félbehagyott csomagok már nincsenek a rendszerben.\n'})
            return 'ok', 0

        self.emit('task_progress', {'task': task_id, 'log': f'🗑 Az újraindítás előtt beragadt {len(todo)} driver törlésének befejezése...'})
        done = 0
        for i, p in enumerate(todo):
            if getattr(self, '_cancel_flag', False):
                raise Exception("Magyar_Megszakit_Flag")
            name = p['published']
            res = drivers_core.delete_driver_package(self._run, name, timeout=DELETE_DRIVER_TIMEOUT)
            if spawn_failed(res):
                self.emit('task_progress', {'task': task_id, 'log': f'⚠️ {name}: a Windows nem tud több folyamatot indítani - a törlés itt megáll.'})
                self._autofix_stats_set('pending_deletes', todo[i:])
                return 'wedged', done
            if drivers_core.delete_stalled(res):
                # Megint beragadt egy csomagon: a maradékot ismét félretesszük, a hívó
                # dönti el, hogy megéri-e még egy reboot-kör (lásd AUTOFIX_MAX_DELETE_ROUNDS).
                self.emit('task_progress', {'task': task_id, 'log': f'⏱️ {name}: ismét beragadt eszközverem - a maradék {len(todo) - i} csomag későbbre marad.'})
                self._autofix_stats_set('pending_deletes', todo[i:])
                return 'wedged', done
            if drivers_core.delete_succeeded(res):
                done += 1
        self.emit('task_progress', {'task': task_id, 'log': f'✅ Befejezve: {done}/{len(todo)} maradék driver törölve.\n'})
        return 'ok', done

    def _autofix_leg_count(self):
        """Hány TELEPÍTŐ láb futott már le ebben a láncban (az AUTOFIX_MAX_INSTALL_LEGS
        plafonhoz). Hibánál 0 - a plafon ilyenkor nem lép közbe, de a lánc a szokásos
        "nincs több telepíthető driver" feltétellel akkor is leáll."""
        try:
            p = self._autofix_stats_path()
            if os.path.exists(p):
                with open(p, 'r', encoding='utf-8') as f:
                    data = json.load(f) or {}
                return len(data.get('legs', []))
        except Exception as e:
            logging.debug(f"[AUTOFIX-STATS] Láb-számlálás sikertelen: {e}")
        return 0

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

                    # A nyomtató-kihagyás választása MÁSIK PROCESSZBE megy át, ezért nem
                    # paraméter, hanem a feladat argumentumába épített flag (lásd CLAUDE.md).
                    resume_flag = '--resume-step1'
                    if skip_printers:
                        resume_flag += ' --skip-printer-drivers'
                    self._schedule_autofix_resume(resume_flag)

                    self._reboot_or_cancel('Újraindulás felkészítve (-1. lépés)...')
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

                    self._schedule_autofix_resume('--resume-autofix')

                    self._reboot_or_cancel('Újraindulás felkészítve...')
                    return
                else:
                    self._run(["powershell", "-NoProfile", "-Command", 'Unregister-ScheduledTask -TaskName "DriverVarazsloResume" -Confirm:$false -ErrorAction SilentlyContinue'], ok_codes=(0, 1))  # 1: a feladat már nem létezik (idempotens duplatörlés)
                    self.emit('task_progress', {'task': 'autofix', 'log': 'Láncolt folytatás gépújraindítás után. Régi driverek törlése kihagyva, hogy ne töröljünk friss drivereket.\n'})
                    self._disable_sleep_sync()

                    # Az egyetlen kivétel a "nem törlünk ezen a lábon" szabály alól: a 0. LÉPÉS
                    # által NÉV SZERINT itt hagyott, beragadt csomagok befejezése. Ezek még a
                    # törlési fázisból maradtak (semmi frisset nem érinthet, mert a telepítés
                    # csak ezután indul), és friss boot után másodpercek alatt lemennek.
                    del_status, del_done = self._finish_pending_deletes()
                    if getattr(self, '_cancel_flag', False): raise Exception("Magyar_Megszakit_Flag")

                    # "Addig töröljük, amíg össze nem jön": ha a besöprés MEGINT beragadt,
                    # de közben HALADT (törölt legalább egy csomagot), megér még egy
                    # reboot-kört - a következő induláskor folytatja ugyanitt.
                    # Leállási feltételek (hogy sose pörögjön a végtelenségig):
                    #  - egy kör alatt NULLA csomag törlődött -> ezek eltávolíthatatlanok
                    #    (pl. a használatban lévő nyomtató-INF), újraindítás sem segít;
                    #  - elértük az AUTOFIX_MAX_DELETE_ROUNDS kört.
                    if del_status == 'wedged':
                        rounds = (self._autofix_stats_get('delete_rounds') or 0) + 1
                        self._autofix_stats_set('delete_rounds', rounds)
                        if del_done > 0 and rounds < AUTOFIX_MAX_DELETE_ROUNDS:
                            self.emit('task_progress', {'task': 'autofix', 'log': f'\n🔄 Maradt még törlendő - újraindulás és folytatás ({rounds}. kör)...'})
                            self._schedule_autofix_resume('--resume-autofix')
                            self._reboot_or_cancel('Újraindulás a törlés befejezéséhez...')
                            return
                        if del_done == 0:
                            self.emit('task_progress', {'task': 'autofix', 'log': '⚠️ A maradék csomagok újraindítással sem távolíthatók el (használatban vannak) - továbblépünk a telepítésre.\n'})
                        else:
                            self.emit('task_progress', {'task': 'autofix', 'log': f'⚠️ {AUTOFIX_MAX_DELETE_ROUNDS} törlési kör után is maradt csomag - továbblépünk a telepítésre.\n'})
                        self._autofix_stats_set('pending_deletes', [])

                    elif del_done > 0:
                        # A TÖRLÉS MOST ÉRT VÉGET (ez a lépés csak akkor fut le, ha a 0. LÉPÉS
                        # hagyott itt maradékot). Mielőtt bármit telepítenénk: ÚJRAINDÍTÁS.
                        # Két okból: (1) a friss boot zárja le a most törölt csomagok
                        # eltávolítását és építi újra az eszközfát, (2) e nélkül a törlés
                        # pending-reboot állapotban hagyná a gépet, és a telepítés első
                        # csomagja azonnal a [kód=4]-es falba futna.
                        self.emit('task_progress', {'task': 'autofix', 'log': '\n✅ Minden törlendő driver eltávolítva!'})
                        self.emit('task_progress', {'task': 'autofix', 'log': '🔄 Újraindulás, és utána indul a TELEPÍTÉS!'})
                        self._schedule_autofix_resume('--resume-autofix')
                        self._reboot_or_cancel('Újraindulás a telepítés előtt...')
                        return

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

                # Újraindulunk és láncolunk egy újabb telepítő lábat, ha (a) települt valami
                # (a friss driverek új eszközöket hozhatnak elő), VAGY (b) a kört pending-reboot
                # / sorozatos hiba miatt vágtuk el - ilyenkor a maradék csomag CSAK újraindítás
                # után tud rendesen felmenni (ez volt a ~20 perces, 8 hamis hibás terepi eset).
                reboot_needed = getattr(self, '_autofix_reboot_pending', False)
                should_chain = installed_count > 0 or reboot_needed
                if should_chain:
                    # A láb-statisztika a záró összefoglalóhoz ÉS a plafon számlálójához kell.
                    self._autofix_stats_add(installed_count)
                    if self._autofix_leg_count() >= AUTOFIX_MAX_INSTALL_LEGS:
                        should_chain = False
                        self.emit('task_progress', {'task': 'autofix', 'log': f'\n⚠️ Elértük a maximális újraindítás-számot ({AUTOFIX_MAX_INSTALL_LEGS} telepítő kör) - a lánc itt lezárul, hogy a gép ne induljon újra a végtelenségig.'})
                        if reboot_needed:
                            self.emit('task_progress', {'task': 'autofix', 'log': 'ℹ️ A rendszer még újraindítást igényel: a maradék driver a következő kézi újraindítás után lép életbe.'})

                if should_chain:
                    if reboot_needed and installed_count == 0:
                        self.emit('task_progress', {'task': 'autofix', 'log': '\n🔄 A rendszer újraindítást igényel a hátralévő driverek telepítéséhez!\nA folyamat az újraindulás után automatikusan folytatódik!'})
                    else:
                        self.emit('task_progress', {'task': 'autofix', 'log': f'\n🔄 EBBEN A KÖRBEN {installed_count} DRIVER TELEPÜLT!\nTovább láncolt hardverek aktiválásához újabb automatikus újraindítás szükséges!\nA rendszer az újraindulás után folytatja a szkennelést!'})

                    self._schedule_autofix_resume('--resume-autofix')

                    self._reboot_or_cancel('Újraindulás felkészítve...')
                    return
                else:
                    if installed_count > 0:
                        # Ide a láb-plafon miatt jutottunk (települt driver, de már nem
                        # indítunk újabb kört) - ne írjunk "nulla új drivert".
                        self.emit('task_progress', {'task': 'autofix', 'log': f'\n🎉 KÉSZ! Ebben a körben {installed_count} driver települt, a lánc lezárul.'})
                    else:
                        self.emit('task_progress', {'task': 'autofix', 'log': '\n🎉 KÉSZ! Nulla újonnan fellelt driver, a konfiguráció végigért.'})
                    self._run(["powershell", "-NoProfile", "-Command", 'Unregister-ScheduledTask -TaskName "DriverVarazsloResume" -Confirm:$false -ErrorAction SilentlyContinue'], ok_codes=(0, 1))  # 1: a feladat már nem létezik (idempotens duplatörlés)

                    # ZÁRÓ DriverStore-TAKARÍTÁS: a lánc alatt telepített driverek régi
                    # verzióinak eltakarítása (közös mag: dupdrivers_core.auto_cleanup_duplicates,
                    # a kézi takarító panel biztonsági szabályaival - hibája sosem
                    # akasztja meg a lánc lezárását, a core mindent elnyel).
                    self.emit('task_progress', {'task': 'autofix', 'log': '\n🧹 DriverStore-takarítás: elavult driver-verziók törlése...'})
                    dupdrivers_core.auto_cleanup_duplicates(
                        self._run,
                        lambda m: self.emit('task_progress', {'task': 'autofix', 'log': m}),
                        self._get_third_party_drivers)

                    # ZÁRÓ ÖSSZEFOGLALÓ: lánc-szintű telepítés-szám + maradék hibakódos eszközök.
                    self._emit_autofix_summary(self._autofix_stats_total_and_clear())

                    self.emit('task_progress', {'task': 'autofix', 'log': 'DCH alkalmazások (Microsoft Store) frissítésének elindítása...'})
                    try:
                        # A DCH-driverekhez tartozó Store-alkalmazások (Intel Graphics Command
                        # Center, Realtek Audio Console...) frissítése. Ez NEM driver-telepítés,
                        # a driverek addigra mind fent vannak - ezért nem is várunk rá.
                        #
                        # SOHA ne _run-nal hívd: az UpdateScanMethod egy SZINKRON rendszerhívás,
                        # ami végigfuttatja a teljes Store-app ellenőrzést - terepen 112 mp-ig
                        # blokkolt, jóval a "MINDEN LÉPÉS KÉSZ" kiírás UTÁN, és a felhasználó
                        # joggal hitte, hogy lefagyott. Popen + nem várunk rá = tényleg háttér.
                        ws_script = r"Get-CimInstance -Namespace 'Root\cimv2\mdm\dmmap' -ClassName 'MDM_EnterpriseModernAppManagement_AppManagement01' | Invoke-CimMethod -MethodName UpdateScanMethod"
                        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ws_script],
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         startupinfo=self._si, creationflags=self._nw)
                        self.emit('task_progress', {'task': 'autofix', 'log': '✅ Store App-ok szinkronizálása a háttérben elindítva (nem várunk rá).'})
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
