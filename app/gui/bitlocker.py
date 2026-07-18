"""DriverVarázsló GUI - BitLocker Kezelő nézet: állapot lekérdezés + kikapcsolás
(a közös logika: app/bitlocker_core.py)."""

# === AUTO-IMPORTS ===
import time
import logging
from app import bitlocker_core
# === /AUTO-IMPORTS ===


class GuiBitlockerMixin:
    """BitLocker Kezelő nézet: állapot lekérdezés + kikapcsolás (dekódolás). A DriverToolApi része (összerakás: app/gui/api.py)."""

    def get_bitlocker_status(self):
        logging.info("[API] get_bitlocker_status()")
        if self.target_os_path:
            return {'status': 'Offline', 'color': 'unknown'}
        return bitlocker_core.get_bitlocker_status(self._run)

    def disable_bitlocker(self):
        logging.info("[API] disable_bitlocker()")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: Offline módban nem elérhető!', 'type': 'error'})
            return

        def worker():
            self.emit('task_start', {'task': 'bitlocker', 'title': 'BitLocker Végleges Kikapcsolása'})
            self.emit('task_progress', {'task': 'bitlocker', 'log': 'Dekódolási parancs kiadása a rendszernek (Disable-BitLocker)...', 'indeterminate': True})

            ok, err = bitlocker_core.disable_bitlocker(self._run)

            if ok:
                self.emit('task_progress', {'task': 'bitlocker', 'log': '✅ Parancs sikeresen kiadva!\n\nA dekódolás megkezdődött a háttérben.\nKérlek, frissítsd az állapotot a gombbal az aktuális százalék lekérdezéséhez.'})
                self.emit('task_complete', {'task': 'bitlocker', 'status': '✅ Dekódolás megkezdve!'})
                # Auto update status after 2 seconds
                time.sleep(2)
                self.emit('bitlocker_status', self.get_bitlocker_status())
            else:
                self.emit('task_progress', {'task': 'bitlocker', 'log': f'❌ Hiba: {err}'})
                self.emit('task_complete', {'task': 'bitlocker', 'status': '❌ Hiba történt!'})

        self._safe_thread('bitlocker', worker)
