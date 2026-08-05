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
from app import wusettings_core
from app.ghost_core import build_ghost_ps
from app.ghost_core import parse_ghost_line
from app.wu_core import AUTOFIX_PRINTER_SKIP_CLASSES
from app.wu_core import WU_PNP_QUERY_PS
from app.wu_core import WuProcessAborted
from app.wu_core import _build_wu_install_ps
from app.wu_core import _filter_wu_downgrades
from app.wu_core import _filter_wu_scan_devices
from app.wu_core import filter_autofix_risky_devices
from app.wu_core import filter_firmware_updates
from app.wu_core import _iter_process_lines
from app.wu_core import _match_wu_updates_to_devices
from app.wu_core import _collect_printer_protection
from app.wu_core import _is_printer_protected
from app.wu_core import _collect_boot_path_protection
from app.wu_core import _is_boot_path_protected
from app.wu_core import _export_net_driver_backup
from app.wu_core import _restore_net_driver_backup
from app.wu_core import detect_wifi_state
from app.wu_core import collect_driver_usage
from app.wu_core import collect_wifi_protection
from app.wu_core import is_wifi_protected
from app.wu_core import export_wlan_profiles
from app.wu_core import restore_wlan_profiles
from app.wu_core import wlan_connect
from app.wu_core import wlan_set_autoconnect
from app.wu_core import clear_wlan_backup
from app.wu_core import WU_MAX_CONSECUTIVE_FAILURES
from app.wu_core import _filter_wu_older_duplicates
from app.wu_core import _install_abort_reason
from app.wu_core import pending_reboot_victim
from app.wu_core import is_reboot_pending
from app.wu_core import verify_failed_installs
from app.wu_core import unoffered_requested_titles
from app.wu_core import mark_generic_replace_candidates
from app.wu_core import _is_inbox_driver
from app.wu_core import HEALTH_REPORT_SKIP_INFS
from app.wu_core import is_specific_hwid
from app.wu_core import deep_catalog_candidates
from app.common import spawn_failed
from app.common import CMD_TIMEOUT_RETURNCODE
from app.drivers_core import DELETE_DRIVER_TIMEOUT
from app.gui.hwscan import PNP_ERROR_CODE_DESCRIPTIONS
from datetime import datetime
# === /AUTO-IMPORTS ===


# Hány "töröljük be a maradékot" kör futhat (mindegyik egy újraindítással). A gyakorlatban
# egy kör elég; a plafon csak a végtelen ciklus ellen véd, a tényleges leállási feltétel az,
# hogy egy kör alatt haladjunk (ha nulla csomag törlődik, azok eltávolíthatatlanok).
AUTOFIX_MAX_DELETE_ROUNDS = 3

# A katalógus-zárókör MINDEN eszközre lefut-e (True), vagy csak a hibakódosakra és a
# Windows-alapdriveren futókra (False). True esetén egy régi, de hibátlanul működő gyári
# driver is frissülhet - a WU ilyet nem ajánl, mert szerinte az eszköz rendben van.
# Ára: eszközönként max 4 HTTP-lekérdezés a katalógus felé (10 szálon, ~1 perc egy
# átlagos gépen). A _catalog_find_driver verzió-kapuja miatt downgrade-et nem hozhat.
AUTOFIX_DEEP_CATALOG = True

# Hány TELEPÍTŐ láb futhat egy láncban (a pending-reboot miatti újraindításokkal együtt).
# A lánc önmagát láncolja tovább, amíg települ valami vagy reboot van függőben, és a
# NORMÁL leállás az, hogy egy körben már NULLA új driver települ (explicit user decision,
# Build 228: "addig induljon újra, amíg már nem talál újabb drivert; amikor nem talált
# újabbat, akkor van kész"). Ez a szám ezért NEM a szokásos működés kerete, hanem egy
# magas biztonsági backstop egy patologikus, sosem gyógyuló gép ellen (ahol minden körben
# ténylegesen felkerül egy FRISS csomag, ami sosem bind-el át). Reális gépen sosem érjük
# el: a Build-228-as loop oka egy már-létező csomag téves "1 települt"-ként számolása volt,
# amit a hwscan._install_catalog_sync "already exists" no-op-ként kezel - a jelölt így a
# következő körben már nem hoz telepítést, és a lánc magától lezárul.
AUTOFIX_MAX_INSTALL_LEGS = 10

# Meddig várunk a hálózat felállására egy-egy láb elején (_wait_for_internet).
# Wi-Fi-s telepítésnél lényegesen tovább: a WLAN szolgáltatás indulása + asszociáció +
# hitelesítés + DHCP együtt terepen 15-45 mp, vállalati (802.1X) hálózaton még több.
# Kábelen is várunk valamennyit, mert lassú switch/DHCP mellett eddig szintén elhasalt
# az egyszeri, 3 mp-es próba. Ha a hálózat már él (a tipikus eset), a várakozás nem
# kerül semmibe: az első próba azonnal visszatér.
AUTOFIX_NET_WAIT_WIFI = 120
AUTOFIX_NET_WAIT_WIRED = 45


class GuiAutofixMixin:
    """1 Kattintásos Driver Fix: a 3-lábú, reboot-láncolt AutoFix folyamat. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def _cleanup_leftover_autofix_policy(self):
        """INDULÁSKORI TAKARÍTÁS: a telepítő láb által ideiglenesen beállított
        NoAutoUpdate=1 csoportházirend eltávolítása, ha egy megszakadt lánc ott hagyta.

        Miért kell: a telepítő láb a WU-keresés idejére beállítja a
        HKLM\\SOFTWARE\\Policies\\...\\WindowsUpdate\\AU\\NoAutoUpdate=1 értéket, és egy
        `finally` blokkban törli. A `finally` viszont NEM fut le, ha a gépet BSOD, áramszünet
        vagy kényszerleállás viszi el a telepítés közben (a lánc leghosszabb, legkockázatosabb
        szakasza) - és ez az érték nem "driver-frissítés tiltás", hanem a TELJES Windows
        Update letiltása. Ott felejtve az ügyfél gépe soha többé nem kapna biztonsági
        frissítést sem, teljesen némán.

        SZŰK a feltétel, szándékosan: csak akkor nyúlunk hozzá, ha (a) nem egy futó lánc
        resume lábában vagyunk, és (b) van félbehagyott lánc-állapotfájl - vagyis
        BIZONYÍTHATÓAN a mi megszakadt futásunk hagyta ott. Egy rendszergazda által
        szándékosan beállított házirendet így nem írunk felül. Mindent naplóz, hibát elnyel.
        """
        try:
            if getattr(self, 'resume_mode', False) or getattr(self, 'resume_step1', False):
                logging.debug("[AUTOFIX-CLEANUP] Resume láb - a NoAutoUpdate ellenőrzése kihagyva (élő lánc).")
                return
            if not os.path.exists(self._autofix_stats_path()):
                logging.debug("[AUTOFIX-CLEANUP] Nincs félbehagyott lánc-állapotfájl - nincs mit takarítani.")
                return
            res = self._run(['reg', 'query', r'HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU',
                             '/v', 'NoAutoUpdate'], ok_codes=(0, 1))
            if res.returncode != 0 or 'NoAutoUpdate' not in (res.stdout or ''):
                logging.info("[AUTOFIX-CLEANUP] Nincs beragadt NoAutoUpdate házirend.")
                return
            logging.warning("[AUTOFIX-CLEANUP] BERAGADT NoAutoUpdate=1 házirend találva egy megszakadt AutoFix lánc után "
                            f"(reg query kimenet: {(res.stdout or '').strip()[:200]}) - eltávolítás...")
            del_res = self._run(['reg', 'delete', r'HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU',
                                 '/v', 'NoAutoUpdate', '/f'], ok_codes=(0, 1))
            if del_res.returncode == 0:
                logging.warning("[AUTOFIX-CLEANUP] A beragadt NoAutoUpdate házirend eltávolítva - a Windows Update újra működhet.")
            else:
                logging.error(f"[AUTOFIX-CLEANUP] A NoAutoUpdate eltávolítása SIKERTELEN (returncode={del_res.returncode}).")
        except Exception as e:
            logging.warning(f"[AUTOFIX-CLEANUP] Induláskori házirend-takarítás hiba (nem kritikus): {e}")

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
            # A per-eszköz események a FELÜLETRE szándékosan nem mennek ki (az AutoFix itt
            # csendes), de a LOGBA igen: enélkül csak annyi maradt, hogy "10 db törölve",
            # és egy "eltűnt az eszközöm" bejelentésnél semmi nyom nem volt arról, MELYIK
            # 10 eszközt szedtük ki. Ez törlés - a nevének látszania kell.
            elif event == 'rm':
                logging.info(f"[GHOST] Törlés indul: {data}")
            elif event == 'ok':
                logging.info(f"[GHOST] Törölve: {data}")
            elif event == 'fail':
                logging.warning(f"[GHOST] Törlés SIKERTELEN: {data}")
            elif event == 'other':
                logging.debug(f"[GHOST] script: {data}")

        process.wait()
        logging.info(f"[GHOST] Szellemeszköz-törlő script vége (returncode={process.returncode}).")

    def _delete_third_party_sync(self, task_id='autofix', skip_classes=None):
        """Third-party csomagok törlése. Visszatérés: 'ok' (végigért) vagy 'wedged'
        (beragadt eszközverem - a hívónak újra kell indítania és FOLYTATNIA a törlést;
        lásd drivers_core.delete_stalled)."""
        self.emit('task_progress', {'task': task_id, 'log': 'Third-party driverek összegyűjtése és törlése...', 'indeterminate': True})
        drivers = self._get_third_party_drivers()
        logging.info(f"[AUTOFIX-DELETE] Törlési fázis indul: {len(drivers)} third-party csomag a listán "
                     f"(nyomtató-kihagyás osztályok: {sorted(skip_classes) if skip_classes else 'nincs'}).")
        skip_classes = skip_classes or set()

        # BOOT-PATH VÉDELEM (mindig fut, a nyomtató-checkboxtól FÜGGETLENÜL): a
        # rendszerlemezt hordozó eszközlánc third-party drivereit nem töröljük. Enélkül
        # egy Intel VMD / RST RAID mögötti rendszerlemeznél a /force-os pnputil leszedi a
        # boot-kritikus vezérlődrivert, és a KÖVETKEZŐ bootnál INACCESSIBLE_BOOT_DEVICE
        # jön - olyan állapot, amit már semmilyen visszaállításunk nem tud helyrehozni.
        # Lásd wu_core._collect_boot_path_protection (ott a fail-safe ág magyarázata is).
        boot_infs, boot_chain, boot_detected = _collect_boot_path_protection(self._run)
        if boot_detected:
            chain_txt = ' → '.join(f"{c.get('Name') or '?'} [{c.get('Class') or '?'}]" for c in boot_chain) or '?'
            self.emit('task_progress', {'task': task_id, 'log': f'💾 Rendszerlemez útvonala: {chain_txt}'})
        else:
            self.emit('task_progress', {'task': task_id, 'log': '⚠️ A rendszerlemez eszközlánca nem azonosítható - biztonságból MINDEN tárolóvezérlő-driver védve marad.'})
        boot_protected = [d for d in drivers if _is_boot_path_protected(d, boot_infs, boot_detected)]
        if boot_protected:
            boot_keys = {id(d) for d in boot_protected}
            drivers = [d for d in drivers if id(d) not in boot_keys]
            self.emit('task_progress', {'task': task_id, 'log': f'🛡️ {len(boot_protected)} db boot-kritikus tárolódriver VÉDVE (nem töröljük, különben a gép nem indulna el):'})
            for d in boot_protected:
                line = f"{d.get('published', '?')} ({d.get('original', '?')}) - {d.get('provider', '?')} [{d.get('class', '?')}]"
                self.emit('task_progress', {'task': task_id, 'log': f'   • {line}'})
                logging.warning(f"[BOOT-PROTECT] Törlésből KIZÁRVA (boot-kritikus): {line}")
        else:
            logging.info("[BOOT-PROTECT] A törlési listán nincs a rendszerlemez útvonalához tartozó csomag.")

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
                # A "hova tűnt a nyomtatóm drivere?" kérdés csak akkor válaszolható meg a
                # logból, ha NÉV SZERINT látszik, mit hagytunk ki (CLAUDE.md: a
                # listaszűkítő döntéseknek meg kell nevezniük, mit dobtak el).
                for d in skipped:
                    logging.info(f"[PRINTER-PROTECT] Törlésből kizárva: {d.get('published', '?')} "
                                 f"({d.get('original', '?')}) - {d.get('provider', '?')} [{d.get('class', '?')}]")

        # WI-FI-VÉDELEM: a "Wi-Fi-s telepítés" checkboxszal a tech kimondta, hogy a gép
        # vezeték nélkül lóg a hálózaton. Ilyenkor a CSATLAKOZOTT Wi-Fi adapter driverét
        # megtartjuk - kábelnél a beépített LAN-driver átveszi a törölt gyárit, Wi-Finél
        # viszont sok kártyához nincs használható inbox driver, és a gép internet nélkül
        # ragadna, miközben a lánc folytatása pont internetből dolgozna. A "mindent
        # törlünk" alapszabály alóli kivételt itt is a FELHASZNÁLÓ adja meg, ugyanúgy,
        # mint a nyomtató-checkboxnál (lásd CLAUDE.md: nem szabad automatikus,
        # felhasználó nélküli szelektív törlést bevezetni).
        # KÉZI KIVÉTELEK: amit a technikus a megerősítő dialógus jobb oldalán kivett a
        # törlésből. Ez NEM az automatikus szelektív törlés visszahozása (azt a CLAUDE.md
        # tiltja, és jó okkal) - itt minden egyes kivételt a FELHASZNÁLÓ jelölt meg, név
        # szerint, a képernyőn. Ugyanaz az elv, mint a nyomtató-checkboxnál, csak
        # csomag-szinten. Az EREDETI INF-nevet is ellenőrizzük: a listát az előző lábon
        # állította össze a tech, és bár a csomagok azóta nem települtek újra, egy
        # átszámozott oemNN.inf semmiképp ne védjen meg egy másik csomagot.
        keep_list = self._autofix_stats_get('keep_packages') or []
        if keep_list:
            keep_map = {(k.get('published') or '').lower(): (k.get('original') or '').lower()
                        for k in keep_list}
            kept, mismatched = [], []
            for d in drivers:
                pub = (d.get('published') or '').lower()
                if pub not in keep_map:
                    continue
                if keep_map[pub] and keep_map[pub] != (d.get('original') or '').lower():
                    mismatched.append(pub)
                    continue
                kept.append(d)
            if mismatched:
                logging.warning(f"[AUTOFIX-DELETE] {len(mismatched)} kézi kivétel figyelmen kívül hagyva, "
                                f"mert időközben másik csomagé lett a publikált név: {mismatched}")
            if kept:
                kept_keys = {id(d) for d in kept}
                drivers = [d for d in drivers if id(d) not in kept_keys]
                self.emit('task_progress', {'task': task_id, 'log': f'🔒 {len(kept)} db driver kihagyva a törlésből (kézi választás a fix indításakor).\n'})
                for d in kept:
                    logging.info(f"[AUTOFIX-DELETE] Kézi kivétel - törlésből kizárva: {d.get('published', '?')} "
                                 f"({d.get('original', '?')}) - {d.get('provider', '?')} [{d.get('class', '?')}]")

        wifi_protected_pkgs = []
        if getattr(self, '_autofix_wifi_mode', False):
            wifi_infs, wifi_state = collect_wifi_protection(self._run)
            # Az aktív SSID-t eltesszük: a vész-újracsatlakozás ezt próbálja először.
            # Az aktív SSID-t a LÁNC-ÁLLAPOTBA is elmentjük, nem csak a példányra: a
            # további lábak külön processzek, ott a self-attribútum már üres lenne, és a
            # vész-újracsatlakozás nem tudná, MELYIK hálózathoz kell visszatérni.
            self._autofix_wifi_ssid = wifi_state.get('ssid', '')
            if self._autofix_wifi_ssid:
                self._autofix_stats_set('wifi_ssid', self._autofix_wifi_ssid)
            wifi_skipped = [d for d in drivers if is_wifi_protected(d, wifi_infs)]
            wifi_protected_pkgs = list(wifi_skipped)
            # A megtartott csomag azonosítóit átvisszük a ZÁRÓ lábra (külön processz!):
            # ott derül ki, hogy a lánc közben kapott-e az eszköz újabb Wi-Fi drivert, és
            # ha igen, ez a régi már kivezethető (lásd _replace_old_wifi_driver).
            if wifi_skipped:
                self._autofix_stats_set('wifi_old_driver', [
                    {'published': d.get('published', ''), 'original': d.get('original', ''),
                     'provider': d.get('provider', ''), 'version': d.get('version', '')}
                    for d in wifi_skipped])
            if wifi_skipped:
                wifi_keys = {id(d) for d in wifi_skipped}
                drivers = [d for d in drivers if id(d) not in wifi_keys]
                self.emit('task_progress', {'task': task_id, 'log': f'📶 A Wi-Fi kártya drivere védve a törléstől ({wifi_state.get("adapter") or "Wi-Fi adapter"}) - így a kapcsolat a lánc alatt sem szakad meg.\n'})
                for d in wifi_skipped:
                    logging.info(f"[WIFI-PROTECT] Törlésből kizárva: {d.get('published', '?')} "
                                 f"({d.get('original', '?')}) - {d.get('provider', '?')} [{d.get('class', '?')}]")
            elif wifi_state.get('wifi'):
                # Van Wi-Fi kapcsolat, de a driverét nem tudtuk azonosítani (vagy inbox
                # driveren fut, tehát nincs is third-party csomagja) - ezt ki kell mondani,
                # mert a kapcsolat elvesztésének kockázata a technikusé.
                self.emit('task_progress', {'task': task_id, 'log': '⚠️ A Wi-Fi driver nem azonosítható a törlési listán (lehet, hogy Windows-alapdriveren fut) - a kapcsolat elveszhet a törlés után.\n'})
                logging.warning("[WIFI-PROTECT] Wi-Fi mód bekapcsolva, de a törlési listán nincs "
                                f"az adapterhez tartozó csomag (INF: {wifi_infs or 'ismeretlen'}).")
            # WLAN-profilok mentése: másodlagos háló arra az esetre, ha a kapcsolat
            # mégis elveszne (pl. a driver újraszámozása után árván maradt profil).
            saved = export_wlan_profiles(self._run)
            if saved:
                logging.info(f"[WLAN-BACKUP] {len(saved)} Wi-Fi profil elmentve: {[s['ssid'] for s in saved]}")
                self.emit('task_progress', {'task': task_id, 'log': f'🛟 {len(saved)} Wi-Fi hálózat (jelszóval együtt) megjegyezve a lánc idejére - a mentés a végén törlődik.\n'})
            # A lánc 3-4 felügyelet nélküli újraindítást csinál: ha az ügyfél profilja
            # KÉZI csatlakozásra van állítva, a gép minden boot után hálózat nélkül jön
            # fel, és a lánc megáll - hiába van meg a jelszó. Ezért az aktív hálózatot
            # automatikus csatlakozásra állítjuk.
            if self._autofix_wifi_ssid:
                if wlan_set_autoconnect(self._run, self._autofix_wifi_ssid):
                    self.emit('task_progress', {'task': task_id, 'log': f'📶 A(z) "{self._autofix_wifi_ssid}" hálózat automatikus csatlakozásra állítva, hogy az újraindítások után magától visszakapcsolódjon.\n'})

        total = len(drivers)
        logging.info(f"[AUTOFIX-DELETE] Ténylegesen törlendő csomagok: {total} db.")
        if total > 0:
            # 🛟 Hálózati mentőöv: a Net-driverek exportja törlés előtt - ha a lánc
            # folytatásánál nem lenne internet (a WU/beépített driver nem fedi le a
            # hálózati kártyát), ebből állítjuk vissza őket.
            # A VÉDETT Wi-Fi csomagot is exportáljuk, pedig nem töröljük: ez a
            # visszaállási példány. Ha bármi mégis kilövi (vagy egy későbbi lépés
            # lecseréli és rosszul sül el), csak akkor van honnan visszahozni, ha
            # most elmentettük - a mentés pár másodperc, a hiánya viszont egy
            # internet nélkül maradt ügyfélgép.
            backed_up = _export_net_driver_backup(self._run, drivers + wifi_protected_pkgs)
            if backed_up:
                self.emit('task_progress', {'task': task_id, 'log': f'🛟 {backed_up} db hálózati driver biztonsági mentése kész (vész-visszaállításhoz).\n'})
            self.emit('task_progress', {'task': task_id, 'log': f'{total} db third-party driver eltávolítása...\n'})
            # A törlésre kijelölt csomagok listája a lánc-állapotba: a záró láb ebből
            # tudja megmondani, MELYIK csomag nem került vissza a fix végére (a WU nem
            # ismer minden gyári/alkalmazás-drivert - terepen a ViGEmBus és az AMD
            # amdafd.inf némán, örökre eltűnt). Lásd _emit_autofix_summary.
            #
            # ÁTHOZOTT LISTA: ha egy korábbi lánc a törlési fázisban szakadt meg (Mégse,
            # összeomlás), az általa MÁR TÖRÖLT csomagok most nincsenek benne a gép
            # csomaglistájában, tehát a friss lista nem tudna róluk - és a záró jelentésből
            # némán kimaradnának, pedig pont ezek a legveszélyeztetettebbek. Az A láb ezért
            # a régi listát 'carry_pre_packages' néven átmenti (lásd run_autofix).
            carry = self._autofix_stats_get('carry_pre_packages') or []
            cur_pkgs = [{'original': d.get('original', ''), 'provider': d.get('provider', ''),
                         'version': d.get('version', ''), 'class': d.get('class', '')}
                        for d in drivers if d.get('original')]
            cur_names = {(p.get('original') or '').strip().lower() for p in cur_pkgs}
            carried = [c for c in carry if (c.get('original') or '').strip().lower() not in cur_names]
            if carried:
                logging.info(f"[AUTOFIX-DELETE] {len(carried)} csomag áthozva egy korábbi, félbeszakadt láncból "
                             f"a 'nem jött vissza' jelentéshez: {[c.get('original') for c in carried]}")
            self._autofix_stats_set('pre_packages', cur_pkgs + carried)
            deleted_ok = 0
            stalled_streak = 0
            failed = []     # nem törölhető csomagok (pl. használatban lévő INF) - jelentjük
            deferred = []   # beragadt csomagok - az újraindítás utáni lábon próbáljuk újra
            for i, drv in enumerate(drivers):
                if self._cancel_flag:
                    # MEGSZAKÍTÁS A TÖRLÉSI FÁZISBAN: ez a lánc legkényesebb pontja. A gép
                    # itt FÉLIG lecsupaszított állapotban marad, és a hiba-ág (run_autofix
                    # except) törli a folytató feladatot - vagyis magától SEMMI nem megy
                    # tovább. Ezt ki KELL mondani, különben a szerelő azt hiszi, a Mégse
                    # visszaállított mindent.
                    logging.warning(f"[AUTOFIX-DELETE] Felhasználói megszakítás a törlési fázisban: "
                                    f"{deleted_ok} csomag már törölve, {total - i} érintetlen.")
                    self.emit('task_progress', {'task': task_id, 'log': f'\n❗ MEGSZAKÍTVA a törlési fázisban - {deleted_ok} db driver MÁR TÖRÖLVE lett!'})
                    self.emit('task_progress', {'task': task_id, 'log': '⚠️ A gép jelenleg HIÁNYOS driverekkel fut, és a folyamat magától NEM folytatódik.'})
                    self.emit('task_progress', {'task': task_id, 'log': '👉 TEENDŐ: indítsd újra az 1 kattintásos fixet, hogy a hiányzó driverek visszakerüljenek!'})
                    raise Exception("Magyar_Megszakit_Flag")
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

                # NEM beragadt, de sikertelen törlés (tipikusan: "legalább egy eszköz
                # használja a megadott INF fájlt"). Eddig ez némán elveszett, és a fázis
                # végén a felhasználó tiszta "✅ Driverek eltávolítva"-t látott, pedig
                # csomagok maradtak vissza - pont az a néma hamis siker, amit a Build 218
                # óta kerülünk. Nem hiba, csak jelentendő tény: a lánc megy tovább.
                if not drivers_core.delete_succeeded(res):
                    failed.append(f"{name} ({drv.get('original', '')})")
                    logging.warning(f"[AUTOFIX-DELETE] SIKERTELEN törlés: {name} ({drv.get('original', '')}) - "
                                    f"{drv.get('provider', '?')} [{drv.get('class', '?')}], returncode={res.returncode}")
                else:
                    deleted_ok += 1
                    # Törlés = destruktív művelet, a nevének látszania kell a logban (CLAUDE.md).
                    logging.info(f"[AUTOFIX-DELETE] Törölve ({deleted_ok}/{total}): {name} ({drv.get('original', '')}) - "
                                 f"{drv.get('provider', '?')} [{drv.get('class', '?')}]")

            logging.info(f"[AUTOFIX-DELETE] Törlési fázis vége: {deleted_ok} sikeres, {len(failed)} sikertelen, "
                         f"{len(deferred)} halasztott (összesen {total} csomag).")
            if failed:
                self.emit('task_progress', {'task': task_id, 'log': f'\nℹ️ {len(failed)} db csomagot nem lehetett eltávolítani (jellemzően használatban lévő eszköz tartja):'})
                for f in failed:
                    self.emit('task_progress', {'task': task_id, 'log': f'   • {f}'})
                self.emit('task_progress', {'task': task_id, 'log': 'Ez általában nem gond: ezek a driverek maradnak, a folyamat megy tovább.\n'})
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
        #
        # A lista a LÁNC EGÉSZÉRE él, nem csak erre a lábra: a lábak külön processzek, és
        # amíg ez csak memóriában volt, minden telepítő láb ÚJRA nekifutott ugyanannak a
        # bukott csomagnak (lábanként ~2,5 perc a semmiért). A katalógus-oldalon a
        # 'catalog_no_bind' már pontosan így, az autofix_stats.json-ban őrzi ugyanezt -
        # itt is az a helyes minta. A fájl a lánc végén (és az A lábon) törlődik, tehát
        # egy ÚJ fix újra megpróbálja őket.
        install_failed_uids = set(self._autofix_stats_get('wu_failed_uids') or [])
        if install_failed_uids:
            logging.info(f"[AUTOFIX-WU] {len(install_failed_uids)} korábbi lábon bukott UpdateID kizárva "
                         f"ebből a lábból: {sorted(install_failed_uids)}")
            self.emit('task_progress', {'task': task_id, 'log': f'↷ {len(install_failed_uids)} csomag kihagyva: egy korábbi körben már megbukott a telepítése (nem próbáljuk újra).'})
        # Az attempt_counts SZÁNDÉKOSAN marad lábon belüli: az "egy retry" annak szól, ha
        # egy letöltés átmenetileg elhasal ugyanabban a sessionben. Lábak közt viszont pont
        # az újraindítás a gyógyír (a pending-reboot miatt bukott csomag a friss booton
        # simán felmegy), ezért ott a számlálót nem visszük tovább.
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
            # TÁROLÓVEZÉRLŐ-KAPU: a fix indításakor bejelölt engedély nélkül a
            # SCSIAdapter/HDC/DiskDrive eszközök NEM kerülnek a WU-egyeztetésbe. Enélkül
            # az AutoFix felügyelet nélkül tett fel NVMe/AHCI vezérlődrivert, miközben a
            # katalógus-ágakon ugyanez tiltva volt (lásd wu_core.filter_autofix_risky_devices).
            devices_to_check, risky_skipped = filter_autofix_risky_devices(
                devices_to_check,
                allow_storage=getattr(self, '_autofix_allow_storage', False),
                allow_firmware=getattr(self, '_autofix_allow_firmware', False))
            for _label, _items in risky_skipped.items():
                if _items:
                    self.emit('task_progress', {'task': task_id, 'log': f'🛡️ {len(_items)} {_label}-eszköz kihagyva (a fix indításakor nem engedélyezted).'})

            self.emit('task_progress', {'task': task_id, 'log': f'✅ {len(devices_to_check)} hardverelem azonosítva. Egyeztetés...'})
            # A _search_wu_api HÁROM külön kimenetelt ad, és ezeket NEM szabad összemosni:
            #   None -> a keresés ELBUKOTT (5 perces időtúllépés vagy WUA hiba),
            #   []   -> a keresés lefutott, de a szerver nem ajánl semmit,
            #   [..] -> vannak találatok.
            # A régi `or []` a None-t üres listává mosta, és a lenti "nincs találat" ág
            # ilyenkor azt írta ki, hogy "Minden elérhető driver telepítve!" - vagyis egy
            # döglött WU Agent után a felhasználó (és a terepi log) SIKERT látott. Ez a
            # legrosszabb fajta hiba: néma, és pont az ellenkezőjét állítja a valóságnak.
            wu_search_raw = self._search_wu_api()
            wu_search_failed = wu_search_raw is None
            wu_results = wu_search_raw or []
            if wu_search_failed:
                logging.warning(f"[AUTOFIX-WU] A WU keresés elbukott a(z) {loop_idx}. körben "
                                "(időtúllépés vagy WUA hiba) - a kör a katalógus-zárókörrel folytatódik.")
                self.emit('task_progress', {'task': task_id, 'log': '\n⚠️ A Windows Update nem válaszolt (időtúllépés vagy hibás WU-ügynök).'})
                self.emit('task_progress', {'task': task_id, 'log': 'A WU-ból most NEM tudunk drivert telepíteni - áttérés a Microsoft Update Catalog keresésre.'})
            else:
                logging.info(f"[AUTOFIX-WU] A(z) {loop_idx}. kör WU keresése lefutott: {len(wu_results)} nyers találat.")

            exclude_uids = {uid for uid, c in attempt_counts.items() if c >= 2} | install_failed_uids
            matches = _match_wu_updates_to_devices(wu_results, devices_to_check, exclude_uids=exclude_uids)

            # DOWNGRADE-VÉDELEM (közös mag: wu_core._filter_wu_downgrades): a WU néha a
            # telepítettnél RÉGEBBI csomagot ajánl (pl. friss gyári NVIDIA driver után) -
            # hibátlan eszközön az ilyet kihagyjuk, hibakódos eszközön sosem szűrünk.
            wu_by_uid = {w.get('UpdateID'): w for w in wu_results if w.get('UpdateID')}
            # FIRMWARE-KAPU csomag-szinten is: egy firmware-csomag nem feltétlenül a
            # `Firmware` OSZTÁLYÚ eszközhöz párosul (SSD-firmware a tárolóvezérlőhöz,
            # dokkoló-firmware egy USB-eszközhöz), ezért az eszközszűrő nem elég.
            matches, fw_skipped = filter_firmware_updates(
                matches, wu_by_uid, getattr(self, '_autofix_allow_firmware', False))
            for d in fw_skipped:
                self.emit('task_progress', {'task': task_id, 'log': f'🛡️ [KIHAGYVA] {d["title"]} - {d["reason"]}'})
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
                # A megfogalmazás attól függ, MIÉRT nincs találat - egy elbukott keresésre
                # nem szabad "minden telepítve"-t írni (lásd a wu_search_failed ágat fent).
                if wu_search_failed:
                    self.emit('task_progress', {'task': task_id, 'log': '⚠️ A Windows Update keresés nem adott eredményt (nem válaszolt) - a WU-s driverek egy része HIÁNYOZHAT.'})
                    self.emit('task_progress', {'task': task_id, 'log': 'A katalógus-zárókör még megpróbálja pótolni; utána érdemes kézi szkennelést futtatni a "Driver Keresés és Telepítés" menüben.'})
                    logging.warning("[AUTOFIX-WU] A kör WU-találat nélkül zárul, mert a keresés elbukott (NEM azért, mert minden telepítve van).")
                else:
                    self.emit('task_progress', {'task': task_id, 'log': '✅ Szerveren nincs újabb valós illesztőprogram.'})
                    self.emit('task_progress', {'task': task_id, 'log': 'Minden elérhető driver telepítve! Keresési lánc befejezve.'})
                    logging.info("[AUTOFIX-WU] A WU keresés lefutott és nem ajánl több drivert - a WU körök lezárulnak.")
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
                # PENDING-REBOOT KEGYELEM: a pending-reboot miatt megszakadt kör utolsó
                # bukott csomagja nem a saját hibájából bukott - nem tiltjuk ki véglegesen,
                # a gyógyír rá pont az az újraindítás, ami közvetlenül utána következik.
                # A szabály és a terepi bizonyíték a közös magban: wu_core.pending_reboot_victim.
                reboot_victim = pending_reboot_victim(round_failed_titles, aborted_reason)
                newly_failed = []
                for title in round_failed_titles:
                    if title in verified:
                        continue
                    if title == reboot_victim:
                        logging.info(f"[AUTOFIX-WU] NEM kizárva (a pending-reboot miatt megszakadt kör "
                                     f"utolsó csomagja, újraindítás után újrapróbáljuk): {title}")
                        self.emit('task_progress', {'task': task_id, 'log': f'↻ {title} - az újraindítás után újrapróbáljuk (ez a hiba a reboot-váró állapot miatt jött).'})
                        continue
                    fuid = title_to_uid.get(title)
                    if fuid:
                        install_failed_uids.add(fuid)
                        newly_failed.append((title, fuid))
                if newly_failed:
                    # A KÖVETKEZŐ LÁBNAK is szólnia kell róla (külön processz!) - lásd a
                    # install_failed_uids inicializálását a metódus elején.
                    self._autofix_stats_set('wu_failed_uids', sorted(install_failed_uids))
                    for t, u in newly_failed:
                        logging.warning(f"[AUTOFIX-WU] Véglegesen kizárt (telepítés-hiba, utóellenőrzés sem igazolta): {t} [{u}]")

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
                # TÁROLÓ-/FIRMWARE-KAPU A ZÁRÓKÖR EGÉSZÉRE (2026-07-28).
                #
                # Eddig ez a kapu csak a mély szkenre (deep_catalog_candidates) volt
                # ráhúzva, a `problem_devs` ág viszont NYERSEN a hibakódos eszközöket
                # vette - és sem a _catalog_search_collect, sem a _catalog_find_driver nem
                # szűr osztály szerint. Vagyis egy hibakódos NVMe/AHCI vezérlőre vagy egy
                # Firmware osztályú eszközre az AutoFix FELÜGYELET NÉLKÜL feltett egy
                # katalógus-csomagot akkor is, ha a felhasználó egyik jelölőnégyzetet sem
                # pipálta be - pont azt, amit a projekt legerősebb szabálya tilt
                # (INACCESSIBLE_BOOT_DEVICE a következő bootnál, illetve visszafordíthatatlan
                # firmware-írás). A checkbox tehát megkerülhető volt egy hibakódon keresztül.
                #
                # A szűrés itt, a lista TETEJÉN történik, nem áganként: így a zárókör
                # HÁROM forrása (hibakódos + generikus csere + mély szken) garantáltan
                # ugyanazt a kaput kapja, és egy jövőbeli negyedik ág sem tudja megkerülni.
                # (A generikus-csere ág amúgy is kizárja ezeket az osztályokat, a mély szken
                # pedig a saját include_risky/include_firmware kapcsolóján - a dupla szűrés
                # idempotens, csak a `problem_devs`-nél változik érdemben a viselkedés.)
                devices_now, cat_risky_skipped = filter_autofix_risky_devices(
                    devices_now,
                    allow_storage=getattr(self, '_autofix_allow_storage', False),
                    allow_firmware=getattr(self, '_autofix_allow_firmware', False),
                    log_tag='AUTOFIX-CAT', context='a katalógus-zárókörből')
                for _label, _items in cat_risky_skipped.items():
                    if _items:
                        self.emit('task_progress', {'task': task_id, 'log': f'🛡️ {len(_items)} {_label}-eszköz kihagyva a katalógus-keresésből is (a fix indításakor nem engedélyezted).'})
                problem_devs = [d for d in devices_now if d.get('err_code')]
                # A hibás eszközök mellé a GENERIKUS (Windows-beépített) driveren futók is
                # bekerülnek a zárókörbe: a WU ezekre semmit nem ajánl (szerinte rendben
                # vannak), a katalógusban viszont ott a chipgyártó saját csomagja - és a
                # szerviz-cél az, hogy ne maradjon alaplapi hang/LAN a generikus driveren.
                # A kiválasztás a KÖZÖS wu_core.mark_generic_replace_candidates-szel megy,
                # pontosan úgy, ahogy a manuális szkennél (app/gui/hwscan.py) - a tároló-
                # vezérlők és a videokártya szándékosan ki vannak zárva, lásd ott.
                inst_info = self._get_installed_driver_info()
                # A két kockázati kapcsoló ezt a kört is vezérli (2026-07-28): korábban a
                # generikus->gyári csere SAJÁT osztály-tiltólistával dolgozott, amiben a
                # tároló és a firmware fixen benne volt - vagyis a felhasználó akkor sem
                # kapta meg őket, ha kifejezetten bejelölte. A devices_now amúgy is át van
                # szűrve fent, ez a dupla kapu csak explicitté teszi a szándékot.
                generic_devs = mark_generic_replace_candidates(
                    devices_now, inst_info,
                    allow_storage=getattr(self, '_autofix_allow_storage', False),
                    allow_firmware=getattr(self, '_autofix_allow_firmware', False))
                # MÉLY KATALÓGUS-KÖR (AUTOFIX_DEEP_CATALOG): a hibás és a generikus
                # driveres eszközök mellé MINDEN eszköz bekerül. Enélkül egy eszköz, ami
                # egy régi gyári driveren hibátlanul fut, sosem kapott újabbat: a WU
                # szerint rendben van, inbox-jelölt nem lévén a katalógust meg se kérdeztük
                # rá. A verzió-kapu (_catalog_find_driver) csak SZIGORÚAN újabb csomagot
                # enged át, tehát ez nem hozhat downgrade-et - és pont ez a kapu az, ami a
                # kört is véget vet: a második lábon már semmi nem lesz újabb.
                # include_risky: a fix indításakor bejelölt tároló-engedély a KATALÓGUS-ágra
                # is hat, nem csak a WU-ra - a felhasználó egy kapcsolóval dönt a
                # tárolódriverekről, nem forrásonként külön (explicit user decision).
                deep_devs = deep_catalog_candidates(
                    devices_now, inst_info,
                    include_risky=getattr(self, '_autofix_allow_storage', False),
                    include_firmware=getattr(self, '_autofix_allow_firmware', False)
                ) if AUTOFIX_DEEP_CATALOG else []
                cat_devs, cat_ids = [], set()
                for d in problem_devs + generic_devs + deep_devs:
                    if d['id'] not in cat_ids:
                        cat_ids.add(d['id'])
                        cat_devs.append(d)
                if watchdog_tripped and not cat_devs:
                    # A WU elhasalt, de nincs se hibakódos, se generikus driveres eszköz.
                    self.emit('task_progress', {'task': task_id, 'log': 'ℹ️ Nincs hibakódos eszköz, a katalógus-keresés kihagyva.'})
                elif cat_devs:
                    # A felirat a TÉNYLEGES kört írja le. Korábban csak a hibás + generikus
                    # eszközöket sorolta fel, így a mély körnél "0 hibás + 1 Windows-
                    # alapdriveres eszköz keresése" jelent volna meg, miközben 16 eszköz
                    # lekérdezése futott - a log ([EMIT:]) is ezt a téves számot őrizte volna.
                    primary_ids = {d['id'] for d in problem_devs + generic_devs}
                    deep_extra = sum(1 for d in cat_devs if d['id'] not in primary_ids)
                    detail = []
                    if problem_devs:
                        detail.append(f'{len(problem_devs)} hibás')
                    if generic_devs:
                        detail.append(f'{len(generic_devs)} Windows-alapdriveres')
                    if deep_extra:
                        detail.append(f'{deep_extra} mélykeresés')
                    logging.info(f"[AUTOFIX] Katalógus-zárókör: {len(cat_devs)} eszköz "
                                 f"(hibás={len(problem_devs)}, generikus={len(generic_devs)}, mély={deep_extra})")
                    self.emit('task_progress', {'task': task_id, 'log': f'\n--- KATALÓGUS-ZÁRÓKÖR: {" + ".join(detail)} eszköz keresése a Microsoft Update Catalogban ({len(cat_devs)} db)... ---'})
                    found = self._catalog_search_collect(cat_devs, inst_info)
                    # MÁR MEGBUKOTT CSOMAGOK KISZŰRÉSE (lábakon átívelő emlékezet). Ha egy
                    # csomag egy korábbi lábon feltelepült, de az eszköz nem vette át (más
                    # gépre szabott változat, vagy egy specifikusabb HWID-en álló driver
                    # verte), akkor a katalógus MINDEN további lábon újra megtalálja - a
                    # verzió-kapu szerint ugyanis a telepített verzió változatlanul régebbi.
                    # Terepen (2026-07-25) ez az NVIDIA-csomag KÉTSZERI letöltését jelentette
                    # (~1,2 GB, 2,5 perc a semmiért). A tiltólista a lánc végéig él.
                    tried = self._autofix_stats_get('catalog_no_bind') or []
                    tried_keys = {(t.get('pnp', ''), t.get('title', '')) for t in tried}
                    skipped_known = 0
                    if tried_keys:
                        before = len(found)
                        found = [h for h in found
                                 if ((h.get('pnp_id') or '').upper(), h.get('wu_title') or '') not in tried_keys]
                        skipped_known = before - len(found)
                        if skipped_known:
                            self.emit('task_progress', {'task': task_id, 'log': f'↷ {skipped_known} csomag kihagyva: egy korábbi körben már felment, de az eszköz nem vette át (nem töltjük le újra).'})
                    if found:
                        self.emit('task_progress', {'task': task_id, 'log': f'✅ A katalógusban {len(found)} eszközre van driver - telepítés...'})
                        s, _f, _c = self._install_catalog_sync(found, task_id=task_id)
                        total_installed_in_session += s
                        # Amit most nem vett át az eszköz, azt jegyezzük fel a következő lábnak.
                        fresh = [{'pnp': (d.get('pnp_id') or '').upper(), 'title': d.get('wu_title') or '',
                                  'name': d.get('name') or ''}
                                 for d in (getattr(self, '_catalog_no_bind', None) or [])]
                        if fresh:
                            self._autofix_stats_set('catalog_no_bind', tried + fresh)
                            logging.info(f"[AUTOFIX] {len(fresh)} nem-kötő katalógus-csomag feljegyezve a következő lábnak: "
                                         f"{[f['name'] for f in fresh]}")
                    elif not skipped_known:
                        self.emit('task_progress', {'task': task_id, 'log': 'ℹ️ A katalógusban sincs telepíthető driver ezekre az eszközökre.'})
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

        A NYOMTATÓ-KIHAGYÁS FLAG-JÉT IS ITT FŰZZÜK HOZZÁ, nem a hívási helyeken: a lábak
        külön processzek, a felhasználó választása kizárólag a feladat argumentumában él
        tovább (lásd CLAUDE.md), és a hét hívási helyből eddig CSAK az A láb tette hozzá.
        A B láb és a telepítő lábak ezért `--resume-autofix`-ot ütemeztek flag nélkül, így
        a lánc 2. lábától a beállítás némán elveszett: a terepi logban a bekapcsolt
        checkbox mellett is `Nyomtató-kihagyás (érvényes érték): False` állt volna a
        későbbi lábakon. Ma ennek látható következménye nincs (csak a B láb töröl), de a
        log így ÖNMAGÁNAK MOND ELLENT, és az első nyomtató-érzékeny lépés a telepítő lábon
        némán rossz ágra futna. Egy helyen kezelve ez nem felejthető el.

        A feladat AtLogOn triggerrel, interaktív + legmagasabb jogosultsággal fut - a
        folytatást ténylegesen az ui.html indítja el (get_init_data resume flag-jei
        alapján), ezért a GUI-nak láthatóan és adminként kell elindulnia."""
        if getattr(self, '_autofix_skip_printers', False) and '--skip-printer-drivers' not in resume_flag:
            resume_flag += ' --skip-printer-drivers'
        if getattr(self, '_autofix_allow_storage', False) and '--allow-storage-drivers' not in resume_flag:
            resume_flag += ' --allow-storage-drivers'
        if getattr(self, '_autofix_allow_firmware', False) and '--allow-firmware' not in resume_flag:
            resume_flag += ' --allow-firmware'
        if getattr(self, '_autofix_wifi_mode', False) and '--wifi-mode' not in resume_flag:
            resume_flag += ' --wifi-mode'
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
        #
        # A -Settings KÖTELEZŐ, nem díszítés (terepen bizonyított, 2026-08-05, Dell
        # Latitude 7400): a Register-ScheduledTask -Settings nélkül a Windows
        # alapértelmezését kapja, abban pedig `DisallowStartIfOnBatteries = True` -
        # vagyis a feladat AKKUMULÁTORRÓL EL SEM INDUL. A tünet néma és félrevezető:
        # a regisztráció sikeres ("State: Ready"), a gép szabályosan újraindul, aztán
        # a lánc egyszerűen nem folytatódik, és a `schtasks /query /v` szerint a feladat
        # "Last Result: 267011" (= soha nem futott). Semmi hibaüzenet, sehol.
        # Addig nem derült ki, amíg a gépek a szerelőpulton, hálózati kábellel ÉS
        # töltőn álltak; a Wi-Fi-s telepítés viszont pont azt hozta magával, hogy a
        # laptop hálózati kábel nélkül - így gyakran töltő nélkül is - fut.
        #   -AllowStartIfOnBatteries    : induljon el akkuról is (ez a tényleges javítás)
        #   -DontStopIfGoingOnBatteries : ha menet közben húzzák ki a töltőt, ne álljon le
        #   -StartWhenAvailable         : ha a bejelentkezéskori indítás valamiért kimaradt,
        #                                 pótolja, amint teheti
        #   -ExecutionTimeLimit 0       : nincs időkorlát (az alapértelmezett 72 óra egy
        #                                 megakadt lábnál elvágná a láncot)
        task_ps = f'''
        $action = New-ScheduledTaskAction -Execute '{_ps_quote(exec_path)}' -Argument '{_ps_quote(args)}'
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)
        Register-ScheduledTask -TaskName "DriverVarazsloResume" -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force
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

    def _emit_missing_packages(self, pre_packages, task_id='autofix'):
        """A fix ELŐTT meglévő, de a végére VISSZA NEM KERÜLT driver-csomagok kiírása.

        Miért kell: az AutoFix minden third-party csomagot töröl, és abból indul ki, hogy
        amit a gép tényleg használ, azt a Windows Update visszaadja. Ez nem mindig igaz -
        terepen (2026-07, X670) a `vigembus.inf` (ViGEmBus, egy FELHASZNÁLÓ ÁLTAL telepített
        program drivere, a WU-ban nem is létezik) és az `amdafd.inf` (generikus CC_ HWID-je
        miatt a párosítás sosem találja meg) örökre eltűnt, teljesen némán. Nem tudjuk
        automatikusan visszatenni őket (nincs honnan), de a szerviznek TUDNIA kell róla,
        hogy a program újratelepítésével pótolható. Csak tájékoztat, semmit nem módosít.

        A párosítás az EREDETI INF-néven megy (`original`), nem a publikált oemXX.inf-en:
        utóbbi újratelepítés után más sorszámot kap (terepen a wireguard.inf oem1 maradt,
        de az amdgpio2.inf oem25-ből oem13 lett)."""
        if not pre_packages:
            return
        try:
            current = [d for d in (self._get_third_party_drivers() or []) if d.get('original')]
            now = {(d.get('original') or '').strip().lower() for d in current}
            gone = [p for p in pre_packages
                    if (p.get('original') or '').strip().lower() not in now]
            if not gone:
                self.emit('task_progress', {'task': task_id, 'log': '✅ Minden korábbi driver-csomag visszakerült (vagy újabbra cserélődött).'})
                return

            # HÁROM CSOPORT, nem egy. A puszta "ez az INF-név nincs meg" félrevezet: terepen
            # (2026-07-25) a listára került az `nv_dispi.inf` (helyette az `nvle.inf` van fent,
            # ugyanattól a gyártótól, ugyanabban az osztályban) és 11 db Razer csomag, holott
            # az egér VÉGIG BE VOLT DUGVA és működik - a WU csak egy másik, általánosabb Razer
            # csomagot rakott fel. A régi szöveg ezekre azt tanácsolta, hogy "dugd be az
            # eszközt", ami ilyenkor konkrétan téves. Ezért:
            #   - lecserélve: ugyanattól a gyártótól UGYANABBAN az osztályban van csomag;
            #   - más csomagra váltott: a gyártótól van csomag, de más osztályban;
            #   - tényleg eltűnt: a gyártótól semmi nincs fent -> ez az igazi teendő.
            def _prov(p):
                return (p.get('provider') or '').strip().lower()

            def _cls(p):
                return (p.get('class') or '').strip().lower()

            def _same_vendor(a, b):
                a, b = _prov(a), _prov(b)
                return bool(a) and bool(b) and (a.startswith(b) or b.startswith(a))

            replaced, vendor_other, missing = [], [], []
            for p in gone:
                same_vendor = [c for c in current if _same_vendor(p, c)]
                if any(_cls(c) == _cls(p) for c in same_vendor):
                    replaced.append(p)
                elif same_vendor:
                    vendor_other.append(p)
                else:
                    missing.append(p)

            def _line(p):
                prov = p.get('provider') or 'ismeretlen gyártó'
                ver = p.get('version') or '?'
                cls = p.get('class') or ''
                return f"   • {p.get('original')} - {prov} ({ver}){f' [{cls}]' if cls else ''}"

            # A két JÓINDULATÚ csoport képernyő-listája felül van korlátozva: terepen
            # (2026-07-28, a Razer-csomagbloat utáni első futás) a "lecserélődött" csoport
            # 175 tételes fala az összefoglaló minden hasznos sorát kigörgette a képernyőről.
            # A teljes névsor ilyenkor EGY log-sorba kerül (a törlési fázis amúgy is nevén
            # nevezett minden csomagot); a "tényleg eltűnt" csoport szándékosan NINCS
            # levágva - az a teendős lista, ott minden név a felhasználóé.
            BUCKET_SCREEN_MAX = 15

            def _emit_bucket(items, header):
                self.emit('task_progress', {'task': task_id, 'log': header})
                for p in items[:BUCKET_SCREEN_MAX]:
                    self.emit('task_progress', {'task': task_id, 'log': _line(p)})
                if len(items) > BUCKET_SCREEN_MAX:
                    self.emit('task_progress', {'task': task_id, 'log': f'   … és további {len(items) - BUCKET_SCREEN_MAX} hasonló csomag (a teljes névsor a debug logban).'})
                    logging.info(f"[AUTOFIX] Teljes csoport-névsor ({len(items)} db): "
                                 f"{sorted(p.get('original') or '?' for p in items)}")

            if replaced:
                _emit_bucket(replaced, f'\nℹ️ {len(replaced)} csomag LECSERÉLŐDÖTT ugyanannak a gyártónak egy másik csomagjára (az eszköz működik):')
            if vendor_other:
                _emit_bucket(vendor_other, f'\nℹ️ {len(vendor_other)} csomag helyett a gyártó MÁSIK csomagja került fel (jellemzően általánosabb; az eszköz működik, de a gyártói szoftver extra funkciói hiányozhatnak):')
            if not missing:
                self.emit('task_progress', {'task': task_id, 'log': '✅ Nincs olyan csomag, ami nyom nélkül eltűnt volna.'})
                return
            self.emit('task_progress', {'task': task_id, 'log': f'\n⚠️ {len(missing)} db csomag NEM került vissza a fix után:'})
            for p in missing:
                self.emit('task_progress', {'task': task_id, 'log': _line(p)})
            # A leggyakoribb ok NEM az, hogy a WU "nem ismeri" a csomagot, hanem hogy az
            # eszköz nem volt CSATLAKOZTATVA a fix alatt: a WU (és a mi párosításunk is)
            # csak JELENLÉVŐ hardverre ad drivert, tehát a kihúzott bluetooth-dongle /
            # kontroller / nyomtató drivere törlődik, de nem tud visszajönni. Ez nem hiba
            # (a teljes törlés a fix lényege), csak tudni kell róla - és van rá teendő.
            self.emit('task_progress', {'task': task_id, 'log': 'Ennek két oka lehet:'})
            self.emit('task_progress', {'task': task_id, 'log': '   1) Az eszköz NEM VOLT BEDUGVA a fix alatt - a Windows Update csak a jelenlévő hardverhez ad drivert.'})
            self.emit('task_progress', {'task': task_id, 'log': '   2) Külön telepített program drivere volt (pl. VPN, kontroller-emulátor), amit a WU nem is szállít.'})
            self.emit('task_progress', {'task': task_id, 'log': '👉 TEENDŐ: dugd be az eszközt, majd futtass egy szkennelést a "Driver Keresés és Telepítés" menüben - a program megkeresi hozzá a drivert. Programhoz tartozó drivernél telepítsd újra az adott programot.'})
        except Exception as e:
            logging.warning(f"[AUTOFIX] Eltűnt csomagok összevetése sikertelen (nem kritikus): {e}")

    @staticmethod
    def _health_report_worth_listing(dev, inst):
        """Érdemes-e ezt az inbox-driveres eszközt KIÍRNI a záró egészségjelentésbe?

        Nem szűrő a keresés felé: a katalógust minden eszközre megkérdezzük. Ez kizárólag
        azt dönti el, hogy a szerelőnek van-e ezzel TEENDŐJE - lásd _emit_driver_health.

        Két feltétel, mindkettő kell:
          1) konkrét, gyártó-kódos hardver-azonosító (is_specific_hwid). Egy típuskódos
             azonosítóra (ACPI\\VEN_PNP&DEV_0100 - rendszeridőzítő, ROOT\\..., *PNP...)
             eleve nem létezik gyári csomag, és a katalógus is csak vaktalálatot adna rá.
          2) az eszköz nem Windows busz-/beviteli-/szoftver-INF-en fut
             (HEALTH_REPORT_SKIP_INFS): PCI-híd, USB-gyökérhub, WAN Miniport, HID-egér.
        """
        if not is_specific_hwid(dev.get('id') or ''):
            return False
        if (inst.get('inf') or '').strip().lower() in HEALTH_REPORT_SKIP_INFS:
            return False
        return True

    def _emit_driver_health(self, devices, task_id='autofix'):
        """DRIVER-EGÉSZSÉGJELENTÉS: mely eszközök maradtak a fix végén a Windows BEÉPÍTETT
        (inbox) driverén, gyári helyett.

        Miért kell: eddig a záró összefoglaló csak azt mondta meg, mi települt, mi tűnt el
        és mi maradt hibakódos - a legcsendesebb hiányt viszont nem: azt az eszközt, ami
        hibakód nélkül, "működőnek látszva" fut a Microsoft generikus driverén, mert sem a
        WU, sem a katalógus nem adott rá gyárit. Terepen mérve (2026-07-24, ASRock B450M):
        egy hibátlanul lefutott lánc után az alaplapi hang és a LAN is így maradt - a
        szerviznek pont ezt kell tudnia, mert ilyenkor az alaplapgyártó oldaláról kell
        kézzel pótolni.

        A JELENTÉS SZŰRŐJE SZÁNDÉKOSAN SZŰKEBB, MINT A KERESÉSÉ (2026-07-28). Sokáig
        `is_generic_replace_candidate` volt a feltétel, de aznap abból kikerült az
        osztály-whitelist, a busz-enumerátor INF-tiltás és a gyártó-kódos HWID követelménye
        (a KERESÉS mostantól mindent megkérdez - lásd wu_core). A jelentés viszont ettől
        használhatatlanná vált: terepi futásban (2026-07-28, AMD Ryzen 7 5700X, 108 eszköz)
        **87 sort** írt ki "gyári driver jobb lenne" címmel - PCI-hidakat, DMA-vezérlőt,
        rendszeridőzítőt, WAN Miniportokat -, miközben az "ez így helyes" rovatba mindössze
        5 eszköz került. Pont a fordítottja annak, ami hasznos.
        Ez ugyanaz a hiba, amit egy korábbi mérés (2026-07, 99 eszköz) már megmutatott: a
        puszta "inbox driveren fut" feltétel 42 sorból 40 használhatatlant adott.

        A kereséstől eltérően itt tehát MEGMARAD a szűkítés, mert más a kérdés:
          - a keresés kérdése: "megéri-e megkérdezni a katalógust?" -> mindenre igen, egy
            eredménytelen lekérdezés ára néhány másodperc;
          - a jelentés kérdése: "van-e ezzel a szerelőnek TEENDŐJE?" -> csak ott, ahol
            egyáltalán LÉTEZHET gyári driver. Egy pci.inf-en futó PCI-hídhoz nem létezik,
            így az nem teendő, hanem zaj.
        A feltétel: konkrét (nem típuskódos) hardver-azonosító ÉS nem Windows busz-INF.

        A többi inbox-driveres eszköz csak összesített számként jelenik meg. Semmit nem
        módosít, minden hibát elnyel."""
        try:
            inst_info = self._get_installed_driver_info()
            worth, by_design = [], 0
            for dev in devices or []:
                if dev.get('err_code'):
                    continue   # a hibakódosakat a másik szekció listázza
                inst = inst_info.get((dev.get('pnp_id') or '').upper()) or {}
                if not inst or not _is_inbox_driver(inst):
                    continue
                if self._health_report_worth_listing(dev, inst):
                    worth.append((dev, inst))
                else:
                    by_design += 1
            if not worth and not by_design:
                self.emit('task_progress', {'task': task_id, 'log': '✅ Nincs olyan eszköz, ami Windows-alapdriveren maradt.'})
                return
            if worth:
                self.emit('task_progress', {'task': task_id, 'log': f'\n🏭 {len(worth)} eszköz Windows-alapdriveren maradt (gyári driver jobb lenne):'})
                for dev, inst in worth:
                    ver = inst.get('version') or '?'
                    inf = inst.get('inf') or '?'
                    self.emit('task_progress', {'task': task_id, 'log': f"   • {dev['name']} [{dev.get('cat', '')}] - {inf} {ver}"})
                self.emit('task_progress', {'task': task_id, 'log': '👉 Ezekhez sem a Windows Update, sem a Microsoft Update Catalog nem adott gyári csomagot. Alaplapi hang/LAN/chipset esetén az alaplap- vagy gépgyártó letöltőoldaláról pótolható (lásd a "Driver Keresés és Telepítés" menü gyártói kártyáit).'})
            if by_design:
                self.emit('task_progress', {'task': task_id, 'log': f'ℹ️ További {by_design} eszköz a Windows beépített driverén fut, és ez így HELYES: PCI-hidak, ACPI-csomópontok, USB-gyökérhubok, WAN Miniportok, billentyűzet/egér - ezekhez gyári driver nem is létezik, a gyártók ide szoftvert adnak, nem drivert.'})
        except Exception as e:
            logging.warning(f"[AUTOFIX] Driver-egészségjelentés hiba (nem kritikus): {e}")

    def _emit_fast_startup_note(self, task_id='autofix'):
        """Kiírja, hogy a Gyors Rendszerindítás (hiberboot) kikapcsolva maradt-e a lánc után.

        A 0. LÉPÉS `powercfg /h off`-ot futtat, és ezt SEHOL nem kapcsolja vissza. Ez
        szándékos (hiberboot mellett a reboot nem építi újra a PnP vermet), de tartós
        változás az ügyfél gépén, amiről eddig semmilyen visszajelzés nem ment ki. Az
        állapotot nem feltételezzük, hanem LEKÉRDEZZÜK - így ha a powercfg valamiért
        elbukott, nem írunk ki valótlant. Hibát elnyel.

        KÉT registry-értéket kell nézni, és sokáig csak az egyiket néztük (2026-07-28-i
        terepi log): a `powercfg /h off` a `HibernateEnabled`-et nullázza (Control\\Power),
        a Gyors Rendszerindítás checkbox-át (`HiberbootEnabled`, Session Manager\\Power)
        NEM bántja - az 1-en marad, csak hatástalan, mert a Fast Startup hibernálásra épül.
        Az addigi kód csak a HiberbootEnabled-et olvasta, így egy TÖKÉLETESEN sikeres
        `powercfg /h off` (returncode=0) után is azt hitte, hogy a Fast Startup vissza van
        kapcsolva: WARNING ment a naplóba, a felhasználó pedig NEM kapta meg a tájékoztatót
        a maradandó változásról - épp azt, amiért ez a függvény készült.
        Igaz állítás: a Gyors Rendszerindítás akkor és csak akkor működik, ha MINDKÉT
        érték 1."""
        try:
            res = self._run(['reg', 'query', r'HKLM\SYSTEM\CurrentControlSet\Control\Power',
                             '/v', 'HibernateEnabled'], ok_codes=(0, 1))
            hib = None
            m = re.search(r'HibernateEnabled\s+REG_DWORD\s+0x([0-9a-fA-F]+)', res.stdout or '')
            if m:
                hib = int(m.group(1), 16) != 0

            res2 = self._run(['reg', 'query', r'HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Power',
                              '/v', 'HiberbootEnabled'], ok_codes=(0, 1))
            boot = None
            m2 = re.search(r'HiberbootEnabled\s+REG_DWORD\s+0x([0-9a-fA-F]+)', res2.stdout or '')
            if m2:
                boot = int(m2.group(1), 16) != 0

            # A checkbox önmagában nem árul el semmit: hibernálás nélkül hatástalan.
            if hib is False:
                enabled = False           # powercfg /h off megvolt -> Fast Startup nem működik
            elif hib is True and boot is not None:
                enabled = boot            # hibernálás él -> a checkbox dönt
            else:
                enabled = None            # nem sikerült megállapítani
            logging.info(f"[AUTOFIX] Gyors Rendszerindítás a lánc végén: {enabled} "
                         f"(HibernateEnabled={hib} rc={res.returncode}, "
                         f"HiberbootEnabled={boot} rc={res2.returncode}).")

            # Az üzenetet CSAK akkor küldjük ki, ha a mi beavatkozásunk hatása látszik
            # (HibernateEnabled=0). Ha a hibernálás vissza van kapcsolva, akkor a lánc
            # `powercfg /h off` lépése nem érvényesült - ilyenkor azt állítani, hogy "mi
            # kapcsoltuk ki, így kapcsolod vissza", valótlan lenne, és a javasolt
            # `powercfg /h on` sem azt csinálná, amit a szöveg ígér.
            if hib is False:
                self.emit('task_progress', {'task': task_id, 'log': '\nℹ️ A Gyors Rendszerindítás (Fast Startup) KIKAPCSOLVA maradt - erre a driverek megbízható újra-felismeréséhez volt szükség.'})
                self.emit('task_progress', {'task': task_id, 'log': '   Ez így biztonságosabb, a hidegindítás viszont pár másodperccel lassabb lehet. Visszakapcsolás (rendszergazdaként): powercfg /h on'})
            elif hib is True:
                logging.warning(f"[AUTOFIX] A hibernálás VISSZA van kapcsolva (HibernateEnabled=1, "
                                f"Fast Startup checkbox={boot}) - a lánc powercfg /h off lépése vagy "
                                f"elbukott, vagy valami visszaállította. Tájékoztató nem megy ki.")
        except Exception as e:
            logging.debug(f"[AUTOFIX] Fast Startup állapot lekérdezése sikertelen (nem kritikus): {e}")

    def _emit_catalog_no_bind(self, no_bind, task_id='autofix'):
        """Jelentés azokról a katalógus-csomagokról, amiket a lánc MEGTALÁLT, de az eszköz
        végül nem kapott meg (a csomag nem erre a gépre való, vagy nem kötött rá).

        Miért kell: ezek a lánc alatt bizonyítottan LÉTEZŐ, újabb driverek, amik némán
        elvesztek. A 'catalog_no_bind' lista eddig kizárólag a lábak közti ismétlés
        megelőzésére szolgált, majd a lánc végén az állapotfájllal együtt törlődött -
        a szerelő sosem tudta meg, hogy pl. az RTX 3060-hoz volt egy újabb csomag a
        katalógusban (terepi futás, 2026-07-27: 32.0.15.9595 a telepített 32.0.15.9186
        helyett), csak épp nem sikerült ráadni. Márpedig ezekre van kézi megoldás:
        a gyártói (NVIDIA/AMD/Intel) kártya a manuális szkenben."""
        if not no_bind:
            return
        try:
            names = []
            for nb in no_bind:
                nm = (nb.get('name') or '?').strip()
                ttl = (nb.get('title') or '').strip()
                names.append(f"{nm} - {ttl}" if ttl else nm)
            logging.info(f"[AUTOFIX] A katalógusban volt csomag, de az eszköz nem kapta meg: {names}")
            self.emit('task_progress', {'task': task_id, 'log': f'\n📎 {len(names)} db csomagot megtaláltunk a Microsoft Update Catalogban, de az eszköz végül NEM vette át:'})
            for n in names:
                self.emit('task_progress', {'task': task_id, 'log': f'   • {n}'})
            self.emit('task_progress', {'task': task_id, 'log': 'Ezek jellemzően más gépgyártóra szabott változatok, vagy a Windows egy nála pontosabban illeszkedő drivert részesített előnyben.'})
            self.emit('task_progress', {'task': task_id, 'log': '👉 TEENDŐ: videokártyánál a "Driver Keresés és Telepítés" menü gyártói (NVIDIA/AMD/Intel) kártyája adja a legfrissebb drivert; alaplapi eszköznél az alaplapgyártó letöltőoldala.'})
        except Exception as e:
            logging.warning(f"[AUTOFIX] A nem-kötő katalógus-csomagok jelentése hiba (nem kritikus): {e}")

    def _emit_autofix_summary(self, chain_total, pre_packages=None, task_id='autofix', no_bind=None):
        """ZÁRÓ ÖSSZEFOGLALÓ a lánc legvégén: hány driver települt a TELJES lánc alatt,
        MELY csomagok nem kerültek vissza, mely katalógus-csomagokat nem vett át az eszköz,
        és mely eszközök maradtak hibakódosak (hogy a maradék lyuk sose legyen néma).
        Minden hibát elnyel - az összefoglaló sosem akaszthatja meg a lezárást."""
        try:
            self.emit('task_progress', {'task': task_id, 'log': f'\n📊 ÖSSZEFOGLALÓ: a teljes AutoFix lánc alatt összesen {chain_total} driver települt.'})
            self._emit_missing_packages(pre_packages, task_id)
            self._emit_catalog_no_bind(no_bind, task_id)
            res = self._run(["powershell", "-NoProfile", "-Command", WU_PNP_QUERY_PS], encoding='utf-8')
            pnp_data = []
            if res.stdout:
                try:
                    pnp_data = json.loads(res.stdout)
                except Exception as e:
                    logging.warning(f"[AUTOFIX] PnP JSON értelmezési hiba (a maradék hibás eszközök listája üres marad): {e}")
            all_devs = _filter_wu_scan_devices(pnp_data)
            problems = [d for d in all_devs if d.get('err_code')]
            if problems:
                self.emit('task_progress', {'task': task_id, 'log': f'⚠️ Továbbra is hibakódos eszköz: {len(problems)} db'})
                for p in problems:
                    desc = PNP_ERROR_CODE_DESCRIPTIONS.get(p['err_code'], f"Hibakód: {p['err_code']}")
                    self.emit('task_progress', {'task': task_id, 'log': f"   • {p['name']} - {desc} (kód {p['err_code']})"})
                self.emit('task_progress', {'task': task_id, 'log': 'Ezekhez a "Driver Keresés és Telepítés" menü Problémás eszközök szekciója adhat még megoldást.'})
            else:
                self.emit('task_progress', {'task': task_id, 'log': '✅ Nem maradt hibakódos eszköz a rendszerben!'})
            # Egészségjelentés: mi maradt Windows-alapdriveren (a leg csendesebb hiány).
            self._emit_driver_health(all_devs, task_id)
            # TARTÓS RENDSZERVÁLTOZÁS BEJELENTÉSE: a 0. LÉPÉS kikapcsolja a Gyors
            # Rendszerindítást (`powercfg /h off`), mert hiberboot mellett a "újraindítás"
            # nem építi újra rendesen a PnP vermet, és a driver-törlés utáni újra-
            # felismerés megbízhatatlan. Ez a beállítás a lánc után is KIKAPCSOLVA marad -
            # eddig szó nélkül, pedig ez az ügyfél gépének tartós, észrevehető változása
            # (lassabb hidegindítás). Nem kapcsoljuk vissza automatikusan (a szerviz
            # szempontjából a kikapcsolt hiberboot a helyesebb állapot), de kimondjuk.
            self._emit_fast_startup_note(task_id)
            # WI-FI MÓD ZÁRÁSA: kimondjuk, hogy a Wi-Fi driver szándékosan maradt a helyén
            # (a "mindent törlünk" elvtől való eltérést mindig ki kell mondani - ugyanúgy,
            # mint a nyomtatóknál), és eltakarítjuk a WLAN-profil mentést, hogy ügyfélgépen
            # ne maradjon hátra hálózati profil-fájl.
            if getattr(self, '_autofix_wifi_mode', False):
                if getattr(self, '_autofix_wifi_swapped', False):
                    self.emit('task_progress', {'task': task_id, 'log': '\n📶 Wi-Fi-s telepítés: az eszköz ÚJABB Wi-Fi drivert kapott, és a régi csomagot a végén eltávolítottuk - így minden driver kicserélődött.'})
                else:
                    self.emit('task_progress', {'task': task_id, 'log': '\n📶 Wi-Fi-s telepítés: a Wi-Fi kártya drivere MEGMARADT, hogy a kapcsolat a lánc alatt ne szakadjon meg.'})
                    self.emit('task_progress', {'task': task_id, 'log': 'A Windows Update és a katalógus sem kínált hozzá újabbat, tehát a jelenlegi a legfrissebb elérhető. Ha mindenképp tiszta újratelepítést akarsz, tedd kábelre a gépet, és futtasd újra a fixet Wi-Fi mód nélkül.'})
                clear_wlan_backup()
            # A WU videokártya-driverei jellemzően hónapokkal a gyári kiadás mögött járnak,
            # az AutoFix pedig szándékosan CSAK a WU-ból dolgozik (a gyártói ellenőrzés a
            # manuális szken része, lásd app/gui/nvidia.py + vendorgpu.py). A szerviz-
            # munkafolyamat záró lépése ezért egy manuális szken.
            self.emit('task_progress', {'task': task_id, 'log': '\n💡 TIPP: a videokártyához a Windows Update rendszerint nem a legfrissebb drivert adja. A "Driver Keresés és Telepítés" menüben futtatott szken az NVIDIA/AMD/Intel gyári legújabb verzióját is ellenőrzi.'})
        except Exception as e:
            logging.warning(f"[AUTOFIX] Összefoglaló hiba (nem kritikus): {e}")

    # ------------------------------------------------------------------
    # WI-FI-S TELEPÍTÉS (a dialógus nagy checkboxa). A közös mag a wu_core-ban:
    # detect_wifi_state / collect_wifi_protection / export_wlan_profiles /
    # restore_wlan_profiles / clear_wlan_backup - ott van az indoklás is.
    # ------------------------------------------------------------------
    def get_autofix_delete_preview(self):
        """A megerősítő dialógus jobb oldali listája: MI FOG TÖRLŐDNI, és mi mihez tartozik.

        MIÉRT: eddig a tech vakon nyomott Igent egy "minden third-party driver törlődik"
        mondatra. Terepen (2026-08-05) egy távoli asztali program drivere tűnt el, és
        utólag nem lehetett kideríteni, mihez tartozott. Ezért itt NÉV SZERINT látszik
        minden csomag, mellette hogy MELYIK JELEN LÉVŐ ESZKÖZ használja - és
        kipipálható/kivehető egyenként.

        A védettségeket SZÁNDÉKOSAN ugyanazok a függvények számolják, amiket a törlési
        fázis is használ (_collect_printer_protection, collect_wifi_protection,
        _collect_boot_path_protection) - ha az előnézet és a valóság külön logikán
        futna, az előbb-utóbb hazudna a technikusnak.

        Csoportok: 'boot' (rendszerlemez útja - MINDIG védett, a checkboxoktól
        függetlenül), 'printer', 'wifi', 'normal'. A felület ezek alapján szürkíti ki
        a sorokat a bal oldali kapcsolók állása szerint.

        Lassú (több WMI-lekérdezés), ezért a JS aszinkron hívja: a lista a már betöltött
        driver-adatokból AZONNAL megjelenik, ez a hívás csak kiegészíti."""
        try:
            drivers = self._get_third_party_drivers()
            usage = collect_driver_usage(self._run)
            printer_infs, printer_vendors = _collect_printer_protection(self._run)
            wifi_infs, wifi_state = collect_wifi_protection(self._run)
            # A boot-védelem HÁRMAST ad vissza, és a `detected=False` ág (nem sikerült
            # felderíteni a rendszerlemez láncát) fail-safe módon az egész
            # BOOT_FALLBACK_PROTECT_CLASSES-t védi - ezt a döntést nem szabad itt
            # újraírni, ezért a törléssel KÖZÖS _is_boot_path_protected dönt.
            boot_infs, _boot_chain, boot_detected = _collect_boot_path_protection(self._run)
            out = []
            for d in drivers:
                pub = (d.get('published') or '').lower()
                if _is_boot_path_protected(d, boot_infs, boot_detected):
                    group = 'boot'
                elif is_wifi_protected(d, wifi_infs):
                    group = 'wifi'
                elif _is_printer_protected(d, printer_infs, printer_vendors, AUTOFIX_PRINTER_SKIP_CLASSES):
                    group = 'printer'
                else:
                    group = 'normal'
                out.append({
                    'published': d.get('published', ''), 'original': d.get('original', ''),
                    'provider': d.get('provider', ''), 'version': d.get('version', ''),
                    'class': d.get('class', ''), 'date': d.get('date', ''),
                    'devices': usage.get(pub, []), 'group': group,
                })
            groups = {}
            for r in out:
                groups[r['group']] = groups.get(r['group'], 0) + 1
            logging.info(f"[PREVIEW] Törlési előnézet: {len(out)} csomag, csoportok: {groups}; "
                         f"eszközhöz kötött: {sum(1 for r in out if r['devices'])}; "
                         f"boot-lánc felderítve: {boot_detected}")
            # A `boot_detected` a felületnek is kell: a 'boot' zárolásnak KÉT külön oka
            # lehet, és nem mindegy, melyiket írjuk ki. Felderített láncnál a csomag
            # tényleg a rendszerlemez útvonalán van; felderítetlennél viszont a fail-safe
            # ág véd MINDEN tároló-osztályt (BOOT_FALLBACK_PROTECT_CLASSES), és ilyenkor
            # a "a rendszerlemez útvonalán van" állítás túlmutatna a bizonyítékon.
            return {'drivers': out, 'wifi_adapter': wifi_state.get('adapter', ''),
                    'boot_detected': boot_detected}
        except Exception as e:
            logging.warning(f"[PREVIEW] A törlési előnézet összeállítása sikertelen: {e}")
            return {'drivers': [], 'wifi_adapter': '', 'error': str(e)}

    def _replace_old_wifi_driver(self, task_id='autofix'):
        """ZÁRÓ LÉPÉS Wi-Fi módban: a fölöslegessé vált RÉGI Wi-Fi driver kivezetése.

        A sorrend szándékosan ez (explicit user decision, 2026-08-05), és ez a biztonságos
        irány: a lánc alatt a Wi-Fi eszköz a többivel együtt megkapja a legújabb drivert
        (a Wi-Fi mód CSAK a törlést hagyja ki, a keresést nem - az adapter végig benne van
        a WU-egyeztetésben és a katalógus-zárókörben), és MOST, a legvégén nézzük meg, hogy
        tényleg átvett-e újat. Ha igen és a kapcsolat működik, a régi csomag már csak
        szemét a DriverStore-ban - az mehet. Így minden driver kicserélődik, DE egyetlen
        pillanatra sincs a gép hálózat nélkül: nem előbb törlünk és utána reménykedünk,
        hanem az új már fent van és bizonyítottan működik.

        A "letöltöm előre, aztán törlök és cserélek" alternatívát pont ezért nem építettük
        meg: ott van egy ablak, amikor a gép se régi, se új driverrel nem áll - ügyfélgépen
        ez a rosszabb kimenetel.

        Biztonsági szabályok (bármelyik sérül -> a régi MARAD, ez sosem hiba):
          - csak akkor törlünk, ha az eszköz MÁS INF-en fut, mint a védett régi;
          - csak akkor, ha van internet (a csere bizonyítottan működik);
          - csak akkor, ha a régi csomag már nem szerepel az AKTÍV INF-ek közt
            (get_active_published_infs; None -> nem törlünk);
          - sima `pnputil /delete-driver`: se /uninstall, se /force. Ha bármi mégis
            használja, a pnputil elutasítja és a csomag marad.

        Megjegyzés: a záró duplikátum-takarítás (auto_cleanup_duplicates) az AZONOS eredeti
        INF-nevű régi verziót amúgy is eltakarítja. Ez a lépés arra az esetre kell, amikor
        a gyártó NEVET is váltott (pl. netwtw08.inf -> netwtw10.inf) - olyankor a két csomag
        külön csoportba esik, és a régi különben örökre bent maradna."""
        old_pkgs = self._autofix_stats_get('wifi_old_driver') or []
        if not old_pkgs:
            return
        state = detect_wifi_state(self._run)
        current_inf = (state.get('inf') or '').lower()
        if not current_inf:
            logging.info("[WIFI-SWAP] A Wi-Fi eszköz aktuális INF-je nem olvasható ki - a régi driver marad.")
            return
        present = {(d.get('published') or '').lower(): d for d in self._get_third_party_drivers()}
        candidates = []
        for p in old_pkgs:
            pub = (p.get('published') or '').lower()
            if pub == current_inf:
                logging.info(f"[WIFI-SWAP] Az eszköz TOVÁBBRA IS a régi Wi-Fi driveren fut ({pub}) - "
                             "nem kapott újabbat, marad.")
                continue
            if pub not in present:
                logging.info(f"[WIFI-SWAP] A régi Wi-Fi csomag ({pub}) már nincs a DriverStore-ban "
                             "(a duplikátum-takarítás elvitte) - nincs teendő.")
                continue
            if (present[pub].get('original') or '').lower() != (p.get('original') or '').lower():
                logging.warning(f"[WIFI-SWAP] {pub} időközben MÁSIK csomagé lett "
                                f"({present[pub].get('original')} != {p.get('original')}) - nem nyúlunk hozzá.")
                continue
            candidates.append(p)
        if not candidates:
            return
        if not self._check_internet():
            logging.warning("[WIFI-SWAP] Nincs internet a lánc végén - a régi Wi-Fi drivert biztonságból MEGTARTJUK.")
            self.emit('task_progress', {'task': task_id, 'log': '⚠️ A régi Wi-Fi driver megmarad: a kapcsolat most nem ellenőrizhető.'})
            return
        active = dupdrivers_core.get_active_published_infs(self._run)
        if active is None:
            logging.warning("[WIFI-SWAP] Az aktív INF-lista nem kérdezhető le - a régi Wi-Fi driver marad.")
            return
        self.emit('task_progress', {'task': task_id, 'log': f'\n📶 A Wi-Fi eszköz újabb drivert kapott ({current_inf}) és a kapcsolat működik - a régi csomag kivezetése...'})
        removed = []
        for p in candidates:
            pub = p['published']
            if pub.lower() in active:
                logging.info(f"[WIFI-SWAP] {pub} még AKTÍV (más eszköz használja) - marad.")
                continue
            logging.warning(f"[WIFI-SWAP] Régi Wi-Fi driver kivezetése: {pub} ({p.get('original')}) - "
                            f"{p.get('provider')} {p.get('version')}")
            res = self._run(['pnputil', '/delete-driver', pub], ok_codes=(0, 3010))
            if res and res.returncode in (0, 3010):
                removed.append(f"{p.get('original') or pub} ({p.get('version') or '?'})")
            else:
                logging.info(f"[WIFI-SWAP] A kivezetést a pnputil elutasította (marad): {pub}, "
                             f"rc={getattr(res, 'returncode', '?')}")
        if not removed:
            return
        self._autofix_wifi_swapped = True
        # A törlés UTÁN is meg kell néznünk a kapcsolatot: ha bármi félresikerült, azt a
        # technikusnak látnia kell, nem a következő ügyfélnek.
        still_ok = self._wait_for_internet(60, task_id, 'a régi Wi-Fi driver kivezetése után')
        # Ha mégis elment a kapcsolat, itt is a megjegyzett jelszóval próbálunk vissza -
        # ugyanaz az út, mint az újraindítások után (a mentés még megvan, a lánc végi
        # törlése csak az összefoglaló után jön).
        if not still_ok:
            still_ok = self._autofix_recover_wifi(task_id)
        for r in removed:
            self.emit('task_progress', {'task': task_id, 'log': f'   🗑 Régi Wi-Fi driver törölve: {r}'})
        if still_ok:
            self.emit('task_progress', {'task': task_id, 'log': '✅ A Wi-Fi az ÚJ driveren fut, a régi eltávolítva - így minden driver kicserélődött.\n'})
        else:
            logging.error("[WIFI-SWAP] A régi Wi-Fi driver törlése UTÁN megszűnt az internet!")
            self.emit('task_progress', {'task': task_id, 'log': '⚠️ A régi Wi-Fi driver törlése után megszűnt a kapcsolat! Ha nem jön vissza magától, dugj a gépbe USB-RJ45 átalakítót, és futtass egy szkennelést a "Driver Keresés és Telepítés" menüben.\n'})

    def _net_wait_seconds(self):
        """Mennyit várjunk a hálózatra ebben a lábban. Wi-Fi módban lényegesen többet."""
        return AUTOFIX_NET_WAIT_WIFI if getattr(self, '_autofix_wifi_mode', False) else AUTOFIX_NET_WAIT_WIRED

    def get_autofix_net_state(self):
        """A megerősítő dialógus hívja: van-e aktív Wi-Fi / vezetékes kapcsolat.

        Ebből jelöli be előre a "Wi-Fi-s telepítés" checkboxot (explicit user decision:
        automatikus felismerés) - ha a gép Wi-Fin lóg és NINCS aktív vezetékes link,
        akkor a fixnek Wi-Fi módban kell futnia, és ezt ne kelljen fejben tartani.
        Aszinkron hívás a JS-ből: a dialógus azonnal megjelenik, a pipa utólag kerül be."""
        try:
            state = detect_wifi_state(self._run)
            state['suggest'] = bool(state.get('wifi') and not state.get('wired'))
            logging.info(f"[WIFI] Dialógus-javaslat: wifi-mód előre bejelölve={state['suggest']}")
            return state
        except Exception as e:
            logging.warning(f"[WIFI] A dialógus hálózat-felismerése sikertelen: {e}")
            return {'wifi': False, 'wired': False, 'adapter': '', 'ssid': '', 'suggest': False}

    def _autofix_recover_wifi(self, task_id='autofix'):
        """Utolsó esély Wi-Fi módban: a mentett WLAN-profilok visszaimportálása és
        csatlakozás, majd újabb várakozás. Csak akkor fut, ha a türelmes várakozás után
        SINCS internet - vagyis vagy a driver, vagy a profil, vagy a hálózat maga hiányzik.
        Visszatérés: lett-e internet."""
        # A megjegyzett hálózat neve a LÁNC-ÁLLAPOTBÓL jön: a lábak külön processzek, a
        # példány-attribútum itt (a telepítő lábon) már üres - a nevet a törlési fázis
        # tette el az autofix_stats.json-ba.
        ssid = (getattr(self, '_autofix_wifi_ssid', '') or self._autofix_stats_get('wifi_ssid') or '')
        # 1. GYORS ÚT: a profil és a jelszó jellemzően megvan, a Windows csak nem
        #    kapcsolódott vissza magától. Ehhez semmit nem kell importálni.
        if ssid:
            self.emit('task_progress', {'task': task_id, 'log': f'📶 Újracsatlakozás a megjegyzett hálózathoz: "{ssid}"...'})
            wlan_set_autoconnect(self._run, ssid)
            if wlan_connect(self._run, ssid) and self._wait_for_internet(45, task_id, 'újracsatlakozás'):
                self.emit('task_progress', {'task': task_id, 'log': '✅ Wi-Fi kapcsolat helyreállt (a megjegyzett jelszóval).\n'})
                return True
        # 2. TELJES ÚT: a profil elveszett (jellemzően azért, mert a driver újratelepítése
        #    után az interfész új GUID-ot kapott, és a profilok árván maradtak) - a mentett
        #    profilokat visszaimportáljuk a JELSZAVAKKAL együtt, és újra csatlakozunk.
        self.emit('task_progress', {'task': task_id, 'log': '📶 A mentett Wi-Fi profilok (jelszavakkal) visszatöltése...'})
        try:
            tried = restore_wlan_profiles(self._run, ssid)
            if ssid:
                wlan_set_autoconnect(self._run, ssid)
        except Exception as e:
            logging.warning(f"[WIFI] A WLAN-profilok visszaállítása elhasalt: {e}")
            tried = False
        if not tried:
            self.emit('task_progress', {'task': task_id, 'log': '⚠️ Nincs mentett Wi-Fi profil, amivel újra lehetne csatlakozni.'})
            return False
        ok = self._wait_for_internet(AUTOFIX_NET_WAIT_WIFI, task_id, 'Wi-Fi újracsatlakozás után')
        self.emit('task_progress', {'task': task_id,
                                    'log': ('✅ Wi-Fi kapcsolat helyreállítva!\n' if ok
                                            else '⚠️ A Wi-Fi újracsatlakozás nem sikerült.')})
        return ok

    def run_autofix(self, skip_printer_drivers=True, allow_storage_drivers=False, allow_firmware=False,
                    wifi_mode=False, keep_packages=None):
        logging.info(f"[API] run_autofix() indítása (skip_printer_drivers={skip_printer_drivers}, "
                     f"allow_storage_drivers={allow_storage_drivers}, allow_firmware={allow_firmware}, "
                     f"wifi_mode={wifi_mode}, keep_packages={len(keep_packages or [])} db)")
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
                allow_storage = getattr(self, 'allow_storage_drivers', False)
                allow_fw = getattr(self, 'allow_firmware_updates', False)
                wifi = getattr(self, 'wifi_mode', False)
            else:
                skip_printers = skip_printer_drivers
                allow_storage = bool(allow_storage_drivers)
                allow_fw = bool(allow_firmware)
                wifi = bool(wifi_mode)
            # A belépési log a JS-paramétert írja ki, ami a resume lábakon a frontend
            # ALAPÉRTÉKE (mindig True), nem a felhasználó választása - egy nyomtató-panasz
            # kivizsgálásánál pont ez a mező vinne félre. Ezért a FELOLDOTT értéket is
            # kilogoljuk, forrás-megjelöléssel.
            logging.info(f"[AUTOFIX] Nyomtató-kihagyás (érvényes érték): {skip_printers} "
                         f"(forrás: {'sys.argv --skip-printer-drivers' if (is_resume_step1 or is_resume_mode) else 'GUI dialógus'})")
            logging.info(f"[AUTOFIX] Tárolóvezérlő-driverek engedélyezve: {allow_storage} "
                         f"(forrás: {'sys.argv --allow-storage-drivers' if (is_resume_step1 or is_resume_mode) else 'GUI dialógus'})")
            # A feloldott értékek innentől a _schedule_autofix_resume-é: MINDEN további láb
            # ütemezésekor ő fűzi hozzá a flageket, hogy a választás ne veszhessen el a
            # lánc közepén (lásd ott a részletes indoklást).
            logging.info(f"[AUTOFIX] Firmware-frissítések engedélyezve: {allow_fw} "
                         f"(forrás: {'sys.argv --allow-firmware' if (is_resume_step1 or is_resume_mode) else 'GUI dialógus'})")
            logging.info(f"[AUTOFIX] Wi-Fi-s telepítés: {wifi} "
                         f"(forrás: {'sys.argv --wifi-mode' if (is_resume_step1 or is_resume_mode) else 'GUI dialógus'})")
            self._autofix_skip_printers = skip_printers
            self._autofix_allow_storage = allow_storage
            self._autofix_allow_firmware = allow_fw
            self._autofix_wifi_mode = wifi

            task_title = '1 Katt. Fix (RESTART UTÁNI LÁNC FOLYTATÁSA!)' if (is_resume_mode or is_resume_step1) else '1 Kattintásos Driver Javítás és Frissítés'
            self.emit('task_start', {'task': 'autofix', 'title': task_title})
            try:
                # Internet ellenőrzés autofix elején (ha nem resume mód). Wi-Fi módban
                # türelmesebben: a lánc indítása pillanatában is előfordulhat, hogy a
                # Wi-Fi épp most áll fel (a tech az imént kapcsolt hálózatot).
                if not is_resume_mode and not is_resume_step1:
                    self.emit('task_progress', {'task': 'autofix', 'log': '⏳ Internetkapcsolat ellenőrzése...'})
                    if not self._wait_for_internet(self._net_wait_seconds(), 'autofix', 'a fix indítása előtt'):
                        self.emit('toast', {'message': '❌ Nincs internetkapcsolat! Kérlek csatlakozz egy hálózathoz az Autofix előtt!', 'type': 'error'})
                        self.emit('task_complete', {'task': 'autofix', 'status': '❌ Nincs Internetkapcsolat!'})
                        return

                # ÚJ LÉPÉS (-1. LÉPÉS)
                if not is_resume_mode and not is_resume_step1:
                    self.emit('task_progress', {'task': 'autofix', 'log': '-1. LÉPÉS: Windows Update szüneteltetése és újraindítás...'})
                    # Friss lánc indul - egy esetleges korábbi (félbehagyott) lánc
                    # statisztikája ne számítson bele az összefoglalóba.
                    #
                    # EGY KIVÉTEL: a korábbi lánc 'pre_packages' listája. Ha az előző futás
                    # a törlési fázisban szakadt meg, az akkor MÁR letörölt csomagok ma már
                    # nincsenek a gépen, tehát az új lánc friss listája nem tudna róluk -
                    # és a záró "nem jött vissza" jelentésből némán kimaradnának, pedig
                    # pont ezek a legveszélyeztetettebb csomagok. Ezért a törlés előtt
                    # átmentjük őket (a törlési fázis olvassa: 'carry_pre_packages').
                    prev_pre = self._autofix_stats_get('pre_packages') or []
                    self._autofix_stats_clear()
                    if prev_pre:
                        logging.warning(f"[AUTOFIX] Egy korábbi, be nem fejezett lánc {len(prev_pre)} csomagot hagyott hátra - "
                                        f"átvisszük az új lánc jelentésébe: {[p.get('original') for p in prev_pre]}")
                        self._autofix_stats_set('carry_pre_packages', prev_pre)
                        self.emit('task_progress', {'task': 'autofix', 'log': f'ℹ️ Egy korábbi, félbeszakadt fix {len(prev_pre)} csomagot érintett - ezeket is figyeljük a záró jelentésben.'})

                    # A dialógus jobb oldalán KÉZZEL kivett csomagok. A törlés a KÖVETKEZŐ
                    # lábon fut (külön processz), ezért a lánc-állapotba kell tenni - és
                    # csak a fenti _autofix_stats_clear() UTÁN, különben azonnal elveszne.
                    # Név szerint naplózzuk: a "hova tűnt / miért maradt meg X" kérdés
                    # később csak így válaszolható meg a logból.
                    if keep_packages:
                        keep_clean = [{'published': (k.get('published') or ''),
                                       'original': (k.get('original') or '')}
                                      for k in keep_packages if k.get('published')]
                        if keep_clean:
                            self._autofix_stats_set('keep_packages', keep_clean)
                            logging.info(f"[AUTOFIX] A technikus {len(keep_clean)} csomagot vett ki a törlésből: "
                                         f"{[k['original'] or k['published'] for k in keep_clean]}")
                            self.emit('task_progress', {'task': 'autofix', 'log': f'🔒 {len(keep_clean)} db drivert kézzel kivettél a törlésből - ezek megmaradnak.'})

                    self._disable_sleep_sync()
                    
                    self.emit('task_progress', {'task': 'autofix', 'log': 'Windows Update szüneteltetése (~10 év)...'})
                    # Fix (nem hosszabbító) szünet a közös builderből, AUTOFIX_WU_PAUSE_DAYS
                    # hosszan (~10 év) - explicit user decision, lásd a konstans indoklását.
                    self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                               wusettings_core.build_wu_pause_ps(
                                   wusettings_core.AUTOFIX_WU_PAUSE_DAYS, additive=False)])
                    self.emit('task_progress', {'task': 'autofix', 'log': '✅ Windows Update szüneteltetve (~10 év - a Beállításokban egy kattintással folytatható).\n'})

                    self.emit('task_progress', {'task': 'autofix', 'log': '🔄 A számítógép újraindul, majd a folyamat a rendszer előkészítésével folytatódik!'})

                    # A nyomtató-kihagyás választása MÁSIK PROCESSZBE megy át, ezért nem
                    # paraméter, hanem a feladat argumentumába épített flag (lásd CLAUDE.md).
                    # A flag hozzáfűzése a _schedule_autofix_resume dolga (self._autofix_skip_printers),
                    # hogy a lánc egyetlen későbbi lábán se maradhasson le.
                    self._schedule_autofix_resume('--resume-step1')

                    self._reboot_or_cancel('Újraindulás felkészítve (-1. lépés)...')
                    return

                if not is_resume_mode:
                    if is_resume_step1:
                        # ÖSSZEOMLÁS-BIZTOSÍTÁS: az ütemezett feladatot NEM töröljük, hanem
                        # AZONNAL átállítjuk a KÖVETKEZŐ lábra (--resume-autofix).
                        #
                        # Ez a láb törli az összes third-party drivert; ha közben elszáll a
                        # gép (BSOD, áramszünet, kényszerleállás), a régi kód állapotában
                        # egyáltalán nem maradt ütemezett feladat - a lánc némán megszakadt,
                        # a gép meg félig lecsupaszított driverekkel maradt, és a
                        # felhasználónak kézzel kellett újraindítania az egészet.
                        # Terepen bizonyított (2026-07, Win10 + R9 200): a leállás a
                        # törlési fázisban történt, 9 csomag már törölve volt, és az
                        # újraindulás után SEMMI nem folytatta.
                        #
                        # Azért a KÖVETKEZŐ lábra állítjuk (és nem erre a lábra vissza), mert
                        # ha éppen a driver-törlés vitte el a gépet, egy újrapróbálás
                        # végtelen újraindulás-hurokba vinne; a telepítő láb viszont a
                        # meglévő állapotból is értelmesen folytatja.
                        # (Register-ScheduledTask -Force: felülírja a meglévő bejegyzést.)
                        self._schedule_autofix_resume('--resume-autofix')
                    self.emit('task_progress', {'task': 'autofix', 'log': '0. LÉPÉS: Rendszer előkészítése és régi driverek törlése...'})
                    
                    self._disable_sleep_sync()
                    
                    self.emit('task_progress', {'task': 'autofix', 'log': 'Windows Gyors Rendszerindítás (Fast Startup) kikapcsolása...'})
                    # TARTÓS változás: a lánc után sem kapcsoljuk vissza (a záró összefoglaló
                    # ki is mondja - lásd _emit_fast_startup_note). Naplózzuk az eredményt,
                    # mert ha ez elbukik, a reboot utáni PnP újra-felismerés megbízhatatlan
                    # lesz, és az a hiba máshol, később üt vissza.
                    hib_res = self._run(["powercfg", "/h", "off"])
                    logging.info(f"[AUTOFIX] powercfg /h off returncode={hib_res.returncode} "
                                 f"(a Gyors Rendszerindítás kikapcsolása a PnP verem újraépüléséhez kell).")
                    if hib_res.returncode != 0:
                        logging.warning(f"[AUTOFIX] A Gyors Rendszerindítás kikapcsolása NEM sikerült: {(hib_res.stdout or hib_res.stderr or '')[:200]}")
                    
                    self._disable_wu_sync()
                    if getattr(self, '_cancel_flag', False): raise Exception("Magyar_Megszakit_Flag")
                    
                    self._create_restore_point_sync()
                    if getattr(self, '_cancel_flag', False): raise Exception("Magyar_Megszakit_Flag")

                    skip_cls = AUTOFIX_PRINTER_SKIP_CLASSES if skip_printers else None

                    self._delete_ghost_devices_sync(skip_classes=skip_cls)
                    if getattr(self, '_cancel_flag', False): raise Exception("Magyar_Megszakit_Flag")

                    # AZ AUTOFIX SEMMILYEN MÓDON NEM NYÚL A SZÍNPROFILOKHOZ (explicit user
                    # decision, 2026-07-31). Itt korábban lefutott a színprofil-visszaállítás;
                    # kivéve, mert (a) a színkezelésnek azóta van saját nézete (Kijelző &
                    # Színkezelés), ahol a szerelő LÁTJA, mit csinál, és külön kérésre indítja,
                    # (b) a driver-rendberakás és a színkezelés két különböző dolog: egy néma,
                    # a fix mellékhatásaként lefutó színprofil-törlés a felhasználó számára
                    # kideríthetetlen képváltozást okoz. Terepi bizonyíték mindkét irányból:
                    # egy szélesgamutos OLED-en a hozzárendelés eltűnése látványos javulás volt,
                    # ugyanaz a lépés egy TN Dellen viszont használhatatlanul kiégett fehéret
                    # hagyott. Ezt a döntést a felhasználónak kell meghoznia, nem a láncnak.
                    # NE tedd vissza ide, és ne hívd innen a színprofil-visszaállító magot.

                    # A visszatérési értéket NEM dobjuk el: a 'wedged' azt jelenti, hogy a
                    # maradék csomagokat a következő láb söpri be (_finish_pending_deletes).
                    # A lánc menete ettől nem változik (a reboot úgyis jön), de a logban és
                    # a felhasználó felé látszania kell, hogy a törlés még NEM fejeződött be.
                    del_state = self._delete_third_party_sync(skip_classes=skip_cls)
                    logging.info(f"[AUTOFIX] A törlési fázis állapota: {del_state}")
                    if del_state == 'wedged':
                        self.emit('task_progress', {'task': 'autofix', 'log': 'ℹ️ A törlés az újraindítás után fejeződik be (beragadt eszközverem).\n'})
                    if getattr(self, '_cancel_flag', False): raise Exception("Magyar_Megszakit_Flag")
                    
                    self.emit('task_progress', {'task': 'autofix', 'log': 'Szolgáltatások leállítása és újraindítási jelzések (Pending Reboot) törlése...'})
                    self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", wusettings_core.WU_STOP_SERVICES_PS])
                    # ok_codes=(0, 1): az 1-es kód a "kulcs nem létezik" - nincs beragadt reboot-jelzés, várt eset.
                    self._run(['reg', 'delete', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired', '/f'], ok_codes=(0, 1))

                    self.emit('task_progress', {'task': 'autofix', 'log': 'Beragadt frissítések és WU gyorsítótár (SoftwareDistribution) ürítése...'})
                    wusettings_core._clear_software_distribution(self._run)

                    self.emit('task_progress', {'task': 'autofix', 'log': 'Windows Update szüneteltetése (~10 év)...'})
                    # Fix (nem hosszabbító) szünet a közös builderből, AUTOFIX_WU_PAUSE_DAYS
                    # hosszan (~10 év) - explicit user decision, lásd a konstans indoklását.
                    self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                               wusettings_core.build_wu_pause_ps(
                                   wusettings_core.AUTOFIX_WU_PAUSE_DAYS, additive=False)])
                    self.emit('task_progress', {'task': 'autofix', 'log': '✅ WU gyorsítótár ürítve, a Windows Update szüneteltetve (~10 év).\n'})
                    
                    self.emit('task_progress', {'task': 'autofix', 'log': '🔄 A számítógép újraindul, majd a folyamat automatikusan a TELEPÍTÉSSEL folytatódik!'})

                    self._schedule_autofix_resume('--resume-autofix')

                    self._reboot_or_cancel('Újraindulás felkészítve...')
                    return
                else:
                    # ÖSSZEOMLÁS-BIZTOSÍTÁS a TELEPÍTŐ lábon is - ugyanaz az indok, mint a
                    # 0. lépésnél: ez a lánc leghosszabb és legkockázatosabb szakasza (itt
                    # mennek fel a tároló-/chipset-driverek, itt él a "mérgezett session"
                    # jelenség), és eddig a láb elején TÖRÖLTÜK a feladatot, tehát egy
                    # összeomlás itt is némán megszakította a láncot - félig telepített
                    # driverekkel és automatika nélkül.
                    #
                    # A számláló a boot-hurok ellen véd: MINDEN belépést számolunk (a
                    # tervezett újraindításokat és az összeomlás utáni folytatásokat is),
                    # és a plafon felett már nem hagyunk magunk után feladatot - egy soha
                    # meg nem gyógyuló gép így legfeljebb eggyel többször indul újra, mint
                    # a normál lánc, aztán megáll. A számláló az autofix_stats.json-nal
                    # együtt törlődik a lánc végén (és az A lábon).
                    starts = (self._autofix_stats_get('install_leg_starts') or 0) + 1
                    self._autofix_stats_set('install_leg_starts', starts)
                    if starts <= AUTOFIX_MAX_INSTALL_LEGS + 1:
                        self._schedule_autofix_resume('--resume-autofix')
                    else:
                        self._run(["powershell", "-NoProfile", "-Command", 'Unregister-ScheduledTask -TaskName "DriverVarazsloResume" -Confirm:$false -ErrorAction SilentlyContinue'], ok_codes=(0, 1))  # 1: a feladat már nem létezik (idempotens duplatörlés)
                        logging.warning(f"[AUTOFIX] {starts}. belépés a telepítő lábba - az összeomlás-biztosítás kikapcsolva, hogy a gép ne indulhasson újra a végtelenségig.")
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

                    # Az ELŐZŐ láb újraindítás-igényes (3010) katalógus-csomagjainak fel nem
                    # használt INF-jei: most, friss boot után dől el, hogy rákötött-e rájuk
                    # eszköz. Ami nem kötött, az kivezethető a DriverStore-ból. E nélkül
                    # örökre bent maradtak (2026-08-05, Latitude 7400: az Intel chipset-cab
                    # ~100 idegen platformra szánt INF-je hízlalta a gépet 143 csomagra).
                    self._finish_deferred_inf_cleanup()
                    if getattr(self, '_cancel_flag', False): raise Exception("Magyar_Megszakit_Flag")

                    # 🛟 Hálózati mentőöv: ha a driver-törlés után a gép internet nélkül
                    # maradt (a WU/beépített driver nem fedte le a hálózati kártyát -
                    # terepen látott eset friss AM5-ös Realtek 2.5GbE-vel), a törlés előtt
                    # elmentett Net-drivereket visszatöltjük, különben a lánc WU-keresése
                    # esélytelen lenne.
                    #
                    # ELŐBB VISZONT MEGVÁRJUK A HÁLÓZATOT: az itteni ellenőrzés eddig egyetlen
                    # 3 mp-es próba volt, közvetlenül a bejelentkezés után - Wi-Fin ez jóval
                    # a kapcsolat felállása ELŐTT fut le, és a lánc feleslegesen esett a
                    # mentőöv-ágra (ez volt a "wifivel nem megy" fő oka, 2026-08-05).
                    net_ok = self._wait_for_internet(self._net_wait_seconds(), 'autofix', 'a driver-törlés után')
                    # Wi-Fi módban a WLAN-profil az első esély: ha a driver megvan (védve
                    # volt), jellemzően csak az újracsatlakozás hiányzik - ez olcsóbb és
                    # célzottabb, mint a teljes Net-driver visszatöltés.
                    if not net_ok and getattr(self, '_autofix_wifi_mode', False):
                        net_ok = self._autofix_recover_wifi('autofix')
                    if not net_ok:
                        self.emit('task_progress', {'task': 'autofix', 'log': '🛟 Nincs internet a driver-törlés után! Mentett hálózati driverek visszaállítása...'})
                        if _restore_net_driver_backup(self._run):
                            self._run(['pnputil', '/scan-devices'])
                            net_ok = self._wait_for_internet(self._net_wait_seconds(), 'autofix', 'a hálózati driver visszaállítása után')
                            if net_ok:
                                self.emit('task_progress', {'task': 'autofix', 'log': '✅ Hálózat helyreállítva a mentett driverekből!\n'})
                        else:
                            self.emit('task_progress', {'task': 'autofix', 'log': '⚠️ Nincs mentett hálózati driver.'})
                        if not net_ok:
                            # A nicpack.zip-es "utolsó esély" ág 2026-07-28-án, kifejezett
                            # felhasználói döntésre TÖRÖLVE (a szervizben USB-RJ45 átalakítóval
                            # oldják meg, aminek beépített drivere van) - az üzenet ezért ezt
                            # tanácsolja.
                            self.emit('task_progress', {'task': 'autofix', 'log': '⚠️ A hálózat továbbra sem él - a WU keresés így valószínűleg üres lesz. Ellenőrizd a kábelt/Wi-Fi-t, vagy dugj a gépbe USB-RJ45 átalakítót (annak beépített drivere van)!\n'})

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

                # Újraindulunk és láncolunk egy újabb telepítő lábat, ha (a) települt valami
                # (a friss driverek új eszközöket hozhatnak elő), VAGY (b) a kört pending-reboot
                # / sorozatos hiba miatt vágtuk el - ilyenkor a maradék csomag CSAK újraindítás
                # után tud rendesen felmenni (ez volt a ~20 perces, 8 hamis hibás terepi eset).
                reboot_needed = getattr(self, '_autofix_reboot_pending', False)
                should_chain = installed_count > 0 or reboot_needed
                # A "MINDEN LÉPÉS KÉSZ" CSAK akkor igaz, ha nem jön még egy láb. Korábban
                # a kör végén mindig kiment, majd közvetlenül utána a gép újraindult -
                # a felhasználó felé ez befejezett folyamat + váratlan reboot volt.
                if not should_chain:
                    self.emit('task_progress', {'task': 'autofix', 'log': '\n🎉 EZ A KÖR KÉSZ!'})
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

                    # WI-FI ZÁRÓ CSERE: a törlésből védett RÉGI Wi-Fi driver kivezetése,
                    # ha az eszköz időközben újabbat kapott és a kapcsolat működik. Csak
                    # Wi-Fi módban van mit tennie, és szándékosan a duplikátum-takarítás
                    # UTÁN fut: az az azonos eredeti INF-nevű régi verziót amúgy is elviszi,
                    # ide már csak a gyártói NÉVVÁLTÁS esete marad (lásd a metódus doc-ját).
                    if getattr(self, '_autofix_wifi_mode', False):
                        try:
                            self._replace_old_wifi_driver('autofix')
                        except Exception as e:
                            logging.warning(f"[WIFI-SWAP] A régi Wi-Fi driver kivezetése nem sikerült (nem kritikus): {e}")

                    # ZÁRÓ ÖSSZEFOGLALÓ: lánc-szintű telepítés-szám + vissza nem került
                    # csomagok + maradék hibakódos eszközök. A pre_packages-t a stats-fájl
                    # TÖRLÉSE ELŐTT kell kiolvasni (_autofix_stats_total_and_clear utána
                    # már nem találná).
                    pre_packages = self._autofix_stats_get('pre_packages') or []
                    # Ugyanígy a stats-fájl TÖRLÉSE ELŐTT: a lánc alatt megtalált, de az
                    # eszköz által át nem vett katalógus-csomagok (lásd _emit_catalog_no_bind).
                    no_bind = self._autofix_stats_get('catalog_no_bind') or []
                    self._emit_autofix_summary(self._autofix_stats_total_and_clear(),
                                               pre_packages=pre_packages, no_bind=no_bind)

                    self.emit('task_progress', {'task': 'autofix', 'log': 'DCH alkalmazások (Microsoft Store) frissítésének elindítása...'})
                    try:
                        # A DCH-driverekhez tartozó Store-alkalmazások (Intel Graphics Command
                        # Center, Realtek Audio Console...) frissítése. Ez NEM driver-telepítés,
                        # a driverek addigra fent vannak.
                        #
                        # Explicit user decision (Build 228): VÁRUNK rá, max 10 percig. A régi
                        # fire-and-forget Popen azért volt rossz, mert a lánc végén NINCS
                        # reboot, a felhasználó pedig előbb-utóbb bezárja az appot - az
                        # exitkori cleanup_zombies() `taskkill /F /T` viszont a teljes
                        # process-fát kilövi, így a háttérbe küldött Store-sync gyakran
                        # sosem futott végig. Ezért most SZINKRON, a saját 10 perces
                        # időkorlátjával (_run elnyeli a TimeoutExpired-et -> CMD_TIMEOUT_
                        # RETURNCODE, nem dob kivételt): ha időben végez, "kész"; ha a 10 percet
                        # túllépi, jelezzük, hogy a háttérben folytatódhat, és továbblépünk.
                        ws_script = r"Get-CimInstance -Namespace 'Root\cimv2\mdm\dmmap' -ClassName 'MDM_EnterpriseModernAppManagement_AppManagement01' | Invoke-CimMethod -MethodName UpdateScanMethod"
                        self.emit('task_progress', {'task': 'autofix', 'log': '⏳ Store App-ok szinkronizálása folyamatban (legfeljebb 10 percig várunk rá)...', 'indeterminate': True})
                        ws_res = self._run(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ws_script], timeout=600)
                        if ws_res.returncode == CMD_TIMEOUT_RETURNCODE:
                            self.emit('task_progress', {'task': 'autofix', 'log': 'ℹ️ A Store-szinkron 10 perc alatt nem fejeződött be - a Windows a háttérben magától folytatja. Továbblépünk.'})
                        else:
                            self.emit('task_progress', {'task': 'autofix', 'log': '✅ Store App-ok szinkronizálása kész.'})
                    except Exception as e:
                        logging.debug(f"[AUTOFIX] Store App sync error: {e}")
                        self.emit('task_progress', {'task': 'autofix', 'log': 'ℹ️ A Store-szinkront nem sikerült elindítani (nem kritikus, a driverek fent vannak).'})
                    
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
                # A 0. lépés elején beregisztrált összeomlás-biztosító feladat (lásd ott)
                # csak a VÁRATLAN leállásra szól. Ha a láb hibával vagy megszakítással ér
                # véget, a lánc itt lezárul - e nélkül a gép a következő bejelentkezéskor
                # magától nekiállna a telepítő lábnak egy olyan folyamat után, amit a
                # felhasználó épp leállított (vagy ami hibára futott).
                try:
                    self._run(["powershell", "-NoProfile", "-Command",
                               'Unregister-ScheduledTask -TaskName "DriverVarazsloResume" -Confirm:$false -ErrorAction SilentlyContinue'],
                              ok_codes=(0, 1))  # 1: már nem létezik - idempotens
                except Exception as ue:
                    logging.warning(f"[AUTOFIX] A folytató feladat törlése sikertelen a hiba-ágon: {ue}")
                if str(e) == "Magyar_Megszakit_Flag":
                    self.emit('task_error', {'task': 'autofix', 'error': 'Felhasználó által megszakítva.'})
                else:
                    logging.error(f"[AUTOFIX] Hiba: {e}")
                    self.emit('task_error', {'task': 'autofix', 'error': str(e)})
                    
        self._safe_thread('autofix', worker)
