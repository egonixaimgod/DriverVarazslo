"""DriverVarázsló GUI - In-app auto-updater: BUILD_NUMBER ellenőrzés GitHubról + exe
csere (a közös logika: app/update_core.py)."""

# === AUTO-IMPORTS ===
import logging
from app import update_core
# === /AUTO-IMPORTS ===


class GuiUpdaterMixin:
    """In-app auto-updater: BUILD_NUMBER ellenőrzés GitHubról + exe csere. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def check_for_updates(self):
        """Update-ellenőrzés (források, sorrend és a CDN-cache magyarázat:
        update_core.check_for_updates)."""
        # FRISS BOOT UTÁN A DNS MÉG NEM ÁLL (2026-11-17, MSI MS-7B61): a 3 próba 6 mp alatt
        # elfogyott, a hálózat ~66 mp múlva állt fel (a fix saját várakozása mérte), a gép
        # a régi Build 415-ön maradt. A keret a lánc Wi-Fi-kerete; ha van net, azonnal
        # visszatér. Háttérhívás, a felületet nem fogja meg.
        self._wait_for_internet(120, None, 'frissítés-ellenőrzés')
        info = update_core.check_for_updates()
        # A letöltendő exe címét ÉS a build-számát eltesszük: így a rákövetkező
        # perform_update nem kérdezi le újra az API-t (ami óránként 60 kérést enged
        # IP-nként), a build-szám pedig a védőháló - lásd stage_update(expect_build).
        self._update_exe_url = (info or {}).get('exe_url')
        self._update_new_build = (info or {}).get('new_version')
        return info

    def perform_update(self):
        logging.info("[UPDATE] perform_update indítása...")
        def worker():
            try:
                self.emit('task_start', {'task': 'update', 'title': 'Program Frissítése'})
                bat_path = update_core.stage_update(
                    lambda msg: self.emit('task_progress', {'task': 'update', 'log': msg, 'indeterminate': True}),
                    exe_url=getattr(self, '_update_exe_url', None),
                    expect_build=getattr(self, '_update_new_build', None))
                import time
                time.sleep(2)
                update_core.launch_update_and_exit(bat_path)
            except Exception as e:
                logging.error(f"[UPDATE] Hiba a letöltés/frissítés során:", exc_info=True)
                self.emit('task_error', {'task': 'update', 'error': str(e)})
        self._safe_thread('update', worker)
