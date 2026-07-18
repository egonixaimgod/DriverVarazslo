"""DriverVarázsló GUI - Windows Update nézet: driver-push tiltás/engedélyezés,
szüneteltetés, szolgáltatás-újraindítás (a közös logika: app/wusettings_core.py)."""

# === AUTO-IMPORTS ===
import logging
from app import wusettings_core
# === /AUTO-IMPORTS ===


class GuiWuMixin:
    """Windows Update nézet: driver-push tiltás/engedélyezés, szüneteltetés, szolgáltatás-újraindítás. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def _set_wu_pause(self, pause=True):
        wusettings_core.set_wu_pause(self._run, pause)

    # ================================================================
    # WU MANAGEMENT
    # ================================================================
    def check_wu_status(self):
        logging.info("[API] check_wu_status()")
        if self.target_os_path:
            return {'status': 'Offline (Nem olvasható)', 'color': 'unknown'}
        try:
            st = wusettings_core.read_wu_status(self._run)

            drv_status = "ENGEDÉLYEZVE"
            if st['policy_disabled'] and st['search_disabled']:
                drv_status = "Teljesen LETILTVA"
            elif st['policy_disabled']:
                drv_status = "Házirend által LETILTVA"
            elif st['search_disabled']:
                drv_status = "Eszközbeáll. LETILTVA"

            if st['service_disabled']:
                result = {'status': 'Szolgáltatás LETILTVA (services.msc)', 'color': 'disabled'}
            elif st['paused_until']:
                paused_until = st['paused_until']
                date_only = paused_until.split('T')[0] if 'T' in paused_until else paused_until
                result = {'status': f'SZÜNET idáig: {date_only} | Driverek: {drv_status}', 'color': 'warning'}
            else:
                color = 'disabled' if 'LETILTVA' in drv_status else 'enabled'
                result = {'status': f'Driver frissítés: {drv_status}', 'color': color}

            logging.info(f"[WU_STATUS] Eredmény: {result['status']}")
            return result
        except Exception as e:
            logging.error(f"[WU_STATUS] Hiba: {e}")
            return {'status': 'Ismeretlen', 'color': 'unknown'}

    def disable_wu(self):
        logging.info("[API] disable_wu()")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: A Windows Update beállítások csak Élő rendszeren módosíthatók!', 'type': 'error'})
            return
        def worker():
            logging.info("[WU] WU driver letiltás indítása...")
            self.emit('task_start', {'task': 'disable_wu', 'title': 'WU Driver Letiltás'})
            wusettings_core.disable_wu_full(
                self._run,
                lambda msg: self.emit('task_progress', {'task': 'disable_wu', 'log': msg}))
            self.emit('task_complete', {'task': 'disable_wu', 'status': '✅ WU driver letiltás kész (Cache ürítve)!'})
        self._safe_thread('disable_wu', worker)

    def enable_wu(self):
        logging.info("[API] enable_wu()")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: A Windows Update beállítások csak Élő rendszeren módosíthatók!', 'type': 'error'})
            return
        def worker():
            logging.info("[WU_ENABLE] Worker indult - WU engedélyezés és reset...")
            self.emit('task_start', {'task': 'enable_wu', 'title': 'WU Driver Engedélyezés + Reset'})
            self.emit('task_progress', {'task': 'enable_wu', 'log': 'WU driver engedélyezés + teljes reset...', 'indeterminate': True})
            wusettings_core.enable_wu_reset(
                self._run,
                lambda msg: self.emit('task_progress', {'task': 'enable_wu', 'log': msg}))
            logging.info("[WU_ENABLE] Kész!")
            self.emit('task_complete', {'task': 'enable_wu', 'status': '✅ WU engedélyezés + reset kész!'})

        self._safe_thread('enable_wu', worker)

    def restart_wu(self):
        logging.info("[API] restart_wu()")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: A Windows Update szolgáltatások csak Élő rendszeren indíthatók újra!', 'type': 'error'})
            return
        def worker():
            logging.info("[WU_RESTART] Worker indult - szolgáltatások újraindítása...")
            self.emit('task_start', {'task': 'restart_wu', 'title': 'WU Szolgáltatások Újraindítása'})
            self.emit('task_progress', {'task': 'restart_wu', 'log': 'WU szolgáltatások újraindítása...', 'indeterminate': True})
            wusettings_core.restart_wu_services(
                self._run,
                lambda msg: self.emit('task_progress', {'task': 'restart_wu', 'log': msg}))
            logging.info("[WU_RESTART] Kész!")
            self.emit('task_complete', {'task': 'restart_wu', 'status': '✅ WU szolgáltatások újraindítva!'})

        self._safe_thread('restart_wu', worker)

    def pause_wu(self, days):
        logging.info(f"[API] pause_wu(days={days})")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: Offline módban nem elérhető!', 'type': 'error'})
            return

        def worker():
            self.emit('task_start', {'task': 'pause_wu', 'title': f'WU Szüneteltetés ({days} nap)'})
            self.emit('task_progress', {'task': 'pause_wu', 'log': f'{days} nap hozzáadása a Windows Update szüneteltetéséhez...', 'indeterminate': True})
            new_date = wusettings_core.pause_wu(
                self._run,
                lambda msg: self.emit('task_progress', {'task': 'pause_wu', 'log': msg}),
                days)
            self.emit('task_progress', {'task': 'pause_wu', 'log': f'✅ Frissítések sikeresen szüneteltetve idáig: {new_date}'})
            self.emit('task_complete', {'task': 'pause_wu', 'status': f'✅ Szüneteltetve idáig: {new_date}'})

        self._safe_thread('pause_wu', worker)

    def resume_wu(self):
        logging.info("[API] resume_wu()")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: Offline módban nem elérhető!', 'type': 'error'})
            return

        def worker():
            self.emit('task_start', {'task': 'resume_wu', 'title': 'WU Szüneteltetés Feloldása'})
            self.emit('task_progress', {'task': 'resume_wu', 'log': 'Windows Update szüneteltetés törlése...', 'indeterminate': True})
            wusettings_core.resume_wu(
                self._run,
                lambda msg: self.emit('task_progress', {'task': 'resume_wu', 'log': msg}))
            self.emit('task_complete', {'task': 'resume_wu', 'status': '✅ Szüneteltetés feloldva!'})

        self._safe_thread('resume_wu', worker)
