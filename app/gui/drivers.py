"""DriverVarázsló GUI - Driverek kezelése nézet: listázás (online/offline) és törlés
(a közös listázó/parzoló/törlő logika: app/drivers_core.py)."""

# === AUTO-IMPORTS ===
import os
import threading
import time
import logging
import traceback
from app import drivers_core
# === /AUTO-IMPORTS ===


class GuiDriversMixin:
    """Driverek kezelése nézet: listázás (online/offline) és törlés. A DriverToolApi része (összerakás: app/gui/api.py)."""

    # ================================================================
    # DRIVER LISTING
    # ================================================================
    def load_drivers(self, all_drivers=False):
        logging.info(f"[API] load_drivers(all_drivers={all_drivers})")
        def worker():
            self.emit('drivers_loading')
            start = time.time()
            try:
                if self.target_os_path:
                    logging.info(f"[DRIVERS] Offline mód: {self.target_os_path}")
                    drivers = self._get_offline_drivers(all_drivers)
                elif all_drivers:
                    logging.info("[DRIVERS] Összes driver lekérdezés (élő rendszer)")
                    drivers = self._get_all_drivers()
                else:
                    logging.info("[DRIVERS] Third-party driverek lekérdezés")
                    drivers = self._get_third_party_drivers()
                elapsed = time.time() - start
                logging.info(f"[DRIVERS] Betöltve: {len(drivers)} driver ({elapsed:.1f}s)")
                self.emit('drivers_loaded', {'drivers': drivers, 'elapsed': round(elapsed, 1)})
            except Exception as e:
                logging.error(f"[DRIVERS] Betöltési hiba: {e}")
                logging.error(traceback.format_exc())
                self.emit('drivers_loaded', {'drivers': [], 'elapsed': 0, 'error': str(e)})
        threading.Thread(target=worker, daemon=True, name="drivers-load").start()

    def _get_third_party_drivers(self):
        logging.debug("[DRIVERS] dism /English /Online /Get-Drivers futtatása...")
        return drivers_core.get_third_party_drivers(self._run)

    def _get_all_drivers(self):
        logging.debug("[DRIVERS] _get_all_drivers() indult")
        drivers = drivers_core.get_all_drivers(self._run)
        logging.debug(f"[DRIVERS] _get_all_drivers: {len(drivers)} valid driver")
        return drivers

    def _get_offline_drivers(self, all_drivers=False):
        logging.debug(f"[DRIVERS] _get_offline_drivers(all_drivers={all_drivers})")
        drivers = drivers_core.get_offline_drivers(self._run, self.target_os_path, all_drivers)
        logging.debug(f"[DRIVERS] _get_offline_drivers: {len(drivers)} valid driver")
        return drivers

    # ================================================================
    # DRIVER DELETION
    # ================================================================
    def delete_drivers(self, published_names, list_all=False, reboot=False):
        logging.info(f"[API] delete_drivers() - {len(published_names)} driver, list_all={list_all}, reboot={reboot}")
        logging.info(f"[DELETE] Törlendő driverek: {published_names}")
        def worker():
            total = len(published_names)
            success = 0
            fail = 0
            logging.info(f"[DELETE] Törlés indulása: {total} db driver")
            self.emit('task_start', {'task': 'delete', 'title': f'Törlés folyamatban... ({total} driver)'})
            self.emit('task_progress', {'task': 'delete', 'log': f'Kijelölt driverek törlése indult ({total} db)'})

            cancelled = False
            for i, pub in enumerate(published_names):
                if self._cancel_flag:
                    self.emit('task_progress', {'task': 'delete', 'log': '❗ Törlés megszakítva a felhasználó által!'})
                    self.emit('task_progress', {'status': '❗ Megszakítva!', 'counter': f'{i} / {total}'})
                    cancelled = True
                    break

                self.emit('task_progress', {
                    'task': 'delete', 'current': i, 'total': total,
                    'status': f'Törlés: {pub}', 'counter': f'{i+1} / {total}',
                    'log': f'🗑 Törlés: {pub}'
                })
                try:
                    is_oem = pub.lower().startswith("oem")
                    res = drivers_core.delete_driver_package(self._run, pub, self.target_os_path)

                    if drivers_core.delete_succeeded(res):
                        success += 1
                        self.emit('task_progress', {'task': 'delete', 'log': f'  ✅ {pub} törölve'})
                    else:
                        # Az agresszív force-fallback csak "ÖSSZES driver" módban, nem-oem
                        # csomagra fut (lásd drivers_core.force_delete_driver_files).
                        if list_all and not is_oem:
                            if drivers_core.force_delete_driver_files(self._run, pub, self.target_os_path):
                                success += 1
                                self.emit('task_progress', {'task': 'delete', 'log': f'  ✅ {pub} törölve (force)'})
                            else:
                                fail += 1
                                self.emit('task_progress', {'task': 'delete', 'log': f'  ❌ {pub} sikertelen (nem található)'})
                        else:
                            fail += 1
                            self.emit('task_progress', {'task': 'delete', 'log': f'  ❌ {pub} sikertelen'})
                except Exception as e:
                    fail += 1
                    self.emit('task_progress', {'task': 'delete', 'log': f'  ❌ {pub} hiba: {e}'})

            # Post-delete scan
            is_offline = bool(self.target_os_path)
            is_pe = os.environ.get('SystemDrive', 'C:') == 'X:'
            if not is_offline and not is_pe and success > 0:
                self.emit('task_progress', {'task': 'delete', 'log': 'Hardverek újraszkennelése...', 'status': 'Hardverek újraszkennelése...'})
                self._run(['pnputil', '/scan-devices'])
                time.sleep(10)
                self.emit('task_progress', {'task': 'delete', 'log': '✅ Hardverek frissítve!'})

            if cancelled:
                self.emit('task_progress', {'task': 'delete', 'log': f'\n--- MEGSZAKÍTVA! Sikeres: {success}, Sikertelen: {fail} ---', 'current': i, 'total': total})
                self.emit('task_complete', {'task': 'delete', 'success': success, 'fail': fail,
                                            'counter': '❗ Megszakítva',
                                            'status': f'❗ Megszakítva! Sikeres: {success}, Sikertelen: {fail}'})
            else:
                self.emit('task_progress', {'task': 'delete', 'log': f'\n--- Sikeres: {success}, Sikertelen: {fail} ---', 'current': total, 'total': total})
                self.emit('task_complete', {'task': 'delete', 'success': success, 'fail': fail,
                                            'counter': f'✅ {success} / ❌ {fail}',
                                            'status': f'Kész! Sikeres: {success}, Sikertelen: {fail}'})

                # Újraindítás ha kérték
                if reboot and success > 0:
                    self.emit('task_progress', {'task': 'delete', 'log': '\n🔄 Újraindítás 5 másodperc múlva...'})
                    time.sleep(5)
                    self._run(['shutdown', '/r', '/t', '0', '/f'])

        self._safe_thread('delete', worker)
