"""DriverVarázsló GUI - In-app auto-updater: BUILD_NUMBER ellenőrzés GitHubról + exe
csere (a közös logika: app/update_core.py)."""

# === AUTO-IMPORTS ===
import logging
from app import update_core
# === /AUTO-IMPORTS ===


class GuiUpdaterMixin:
    """In-app auto-updater: BUILD_NUMBER ellenőrzés GitHubról + exe csere. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def check_for_updates(self):
        """Update-ellenőrzés (a retry-logika és a CDN-cache magyarázat:
        update_core.check_for_updates)."""
        return update_core.check_for_updates()

    def perform_update(self):
        logging.info("[UPDATE] perform_update indítása...")
        def worker():
            try:
                self.emit('task_start', {'task': 'update', 'title': 'Program Frissítése'})
                bat_path = update_core.stage_update(
                    lambda msg: self.emit('task_progress', {'task': 'update', 'log': msg, 'indeterminate': True}))
                import time
                time.sleep(2)
                update_core.launch_update_and_exit(bat_path)
            except Exception as e:
                logging.error(f"[UPDATE] Hiba a letöltés/frissítés során:", exc_info=True)
                self.emit('task_error', {'task': 'update', 'error': str(e)})
        self._safe_thread('update', worker)
