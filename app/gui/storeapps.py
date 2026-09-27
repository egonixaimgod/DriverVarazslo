"""DriverVarázsló GUI - a driverekhez tartozó Microsoft Store-alkalmazások szinkronja
(közös mag: app/storeapps_core.py). Hívói: az 1 kattintásos fix vége, a kézi telepítés
vége, és a Driver Keresés nézet "Driver-alkalmazások telepítése" gombja. A CliApi
ugyanezt a mixint kapja."""

# === AUTO-IMPORTS ===
import logging
import os
from app import storeapps_core, dupdrivers_core
from app.common import fetch_text_with_cert_fallback
# === /AUTO-IMPORTS ===


class GuiStoreAppsMixin:
    """Driver-alkalmazások (Hardware Support App) a Microsoft Store-ból. A DriverToolApi és a
    CliApi része (összerakás: app/gui/api.py, app/cli/api.py)."""

    def _store_fetch(self, url):
        return fetch_text_with_cert_fallback(url, run_fn=self._run, timeout=20,
                                             log_tag='STOREAPP')

    def _driver_infs_for_store_apps(self, installed_info=None):
        """A jelen lévő eszközök által HASZNÁLT publikált INF-ek. Ha a szken már lekérdezte
        a driver-listát (installed_info), azt használjuk - nincs második WMI-kör."""
        if installed_info:
            infs = {(v.get('inf') or '').lower() for v in installed_info.values()}
            infs = {i for i in infs if i.startswith('oem') and i.endswith('.inf')}
            if infs:
                return infs
        act = dupdrivers_core.get_active_published_infs(self._run)
        if act:
            return act
        # A lekérdezés elbukott: inkább nézzük MINDEN telepített gyári INF-et, mint hogy
        # egy kért alkalmazás kimaradjon. (Telepíteni itt nincs kockázat - a Store úgyis
        # elutasítja, ami nem ehhez a géphez való, lásd a 0x803FB005 mérést.)
        inf_dir = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'INF')
        try:
            allinf = {f.lower() for f in os.listdir(inf_dir)
                      if f.lower().startswith('oem') and f.lower().endswith('.inf')}
        except OSError as e:
            logging.warning(f"[STOREAPP] Az INF-mappa nem olvasható: {e}")
            allinf = set()
        logging.warning(f"[STOREAPP] A használt INF-ek listája nem kérdezhető le - "
                        f"mind a {len(allinf)} gyári INF-et megnézzük.")
        return allinf

    def _sync_driver_store_apps(self, task_id, update_all=False):
        """A driverek által kért Store-alkalmazások telepítése/frissítése, képernyő-sorokkal.
        `update_all`: a végén az ÖSSZES fent lévő Store-alkalmazás frissítés-keresése is
        (az 1 kattintásos fix záró lépése). Soha nem dob - a driverek addigra fent vannak."""
        def log(m):
            self.emit('task_progress', {'task': task_id, 'log': m})
        if self.target_os_path:
            logging.info("[STOREAPP] Offline cél-OS: a Store-alkalmazások a futó gépre mennének - kihagyva.")
            return None
        report = None
        try:
            log('\n🛍️ A driverekhez tartozó Microsoft Store-alkalmazások (vezérlőpultok) ellenőrzése...')
            infs = self._driver_infs_for_store_apps()
            report = storeapps_core.sync_driver_store_apps(
                self._run, log, infs, self._store_fetch,
                check_cancel=getattr(self, '_check_cancel', None),
                present_hwid_sets=self._store_present_hwids(fresh=True))
            if not report['required']:
                log('   ✅ A telepített driverek egyike sem kér Store-alkalmazást.')
            elif report['failed']:
                log(f"   ⚠️ {len(report['failed'])} driver-alkalmazás nem került fel - a fenti "
                    f"sorok megmondják, melyik és miért. Kézzel: Microsoft Store → a név keresése.")
            else:
                log(f"   ✅ Mind a(z) {len(report['required'])} driver-alkalmazás fent van a kért verzióban.")
            if report.get('unverified'):
                log('   ⚠️ A telepített alkalmazások listája nem volt lekérdezhető - a fenti '
                    'eredmény NEM ellenőrzött.')
        except Exception as e:
            logging.warning(f"[STOREAPP] A driver-alkalmazások szinkronja elhasalt: {e}", exc_info=True)
            log(f'   ⚠️ A driver-alkalmazások ellenőrzése nem sikerült ({e}) - a driverek fent vannak.')
        if update_all:
            log('⏳ Az összes Store-alkalmazás frissítés-keresése (legfeljebb 10 perc)...')
            how, detail = storeapps_core.run_store_update_scan(self._run)
            logging.info(f"[STOREAPP] Általános Store-frissítés: {how} {detail}")
            if how == 'ok':
                log('✅ A Store frissítés-keresése lefutott - a talált frissítéseket a Store a '
                    'háttérben telepíti.')
            elif how == 'timeout':
                log('ℹ️ A Store frissítés-keresése 10 perc alatt nem végzett - a Windows a '
                    'háttérben folytatja.')
            else:
                log(f'⚠️ A Store frissítés-keresése NEM sikerült ({detail[:120]}). A driverek és '
                    f'a fenti driver-alkalmazások ettől függetlenül fent vannak.')
        return report

    def _store_present_hwids(self, fresh=False, devices=None):
        """A jelen lévő eszközök hardver-azonosító halmazai - ebből derül ki, hogy egy
        extension/komponens INF a gép valamelyik eszközére való-e. `devices`: a szken
        már meglévő eszközlistája (nincs második lekérdezés). `fresh`: a telepítés UTÁN
        új eszközök (pl. SWC-komponensek) jelenhettek meg, ezért a gyorsítótár eldobása."""
        if devices:
            return [d['all_hwids'] for d in devices if d.get('all_hwids')]
        if fresh:
            self._present_hwids_cache = None
        try:
            return self._present_hwid_sets({})[1:]
        except Exception as e:
            logging.warning(f"[STOREAPP] Az eszközlista nem kérdezhető le: {e}")
            return None

    def check_driver_store_apps(self, installed_info=None, devices=None):
        """Hálózat nélküli ellenőrzés: melyik, a driverek által kért alkalmazás hiányzik.
        Lista (lásd storeapps_core.missing_store_apps), vagy None, ha nem eldönthető."""
        if self.target_os_path:
            return []
        try:
            return storeapps_core.missing_store_apps(
                self._run, self._driver_infs_for_store_apps(installed_info),
                present_hwid_sets=self._store_present_hwids(devices=devices))
        except Exception as e:
            logging.warning(f"[STOREAPP] A hiányzó alkalmazások ellenőrzése elhasalt: {e}")
            return None

    def install_driver_store_apps(self):
        """A Driver Keresés nézet gombja: a hiányzó/elavult driver-alkalmazások telepítése."""
        def worker():
            self.emit('task_start', {'task': 'storeapps', 'title': 'Driver-alkalmazások (Microsoft Store)'})
            rep = self._sync_driver_store_apps('storeapps') or {}
            fail = len(rep.get('failed') or [])
            ok = len(rep.get('installed') or []) + len(rep.get('ok') or [])
            status = (f'Kész! {ok} rendben' + (f', {fail} nem sikerült' if fail else ''))
            self.emit('task_complete', {'task': 'storeapps', 'success': ok, 'fail': fail,
                                        'status': status})
            self.emit('store_apps_state', {'missing': self.check_driver_store_apps() or []})
        self._safe_thread('storeapps', worker)
