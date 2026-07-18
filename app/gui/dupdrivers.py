"""DriverVarázsló GUI - DriverStore duplikátum-takarítás nézet (a közös logika és a
biztonsági szabályok: app/dupdrivers_core.py)."""

# === AUTO-IMPORTS ===
import logging
import threading
from app import dupdrivers_core
# === /AUTO-IMPORTS ===


class GuiDupDriversMixin:
    """DriverStore duplikátum-takarítás. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def list_duplicate_drivers(self):
        """Duplikátum-csoportok összegyűjtése háttérszálon; eredmény a
        'dup_drivers_loaded' eventben (a csoportosítás és a biztonsági szabályok:
        dupdrivers_core.build_duplicate_groups)."""
        logging.info("[API] list_duplicate_drivers()")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ A duplikátum-takarítás csak Élő rendszeren működik!', 'type': 'error'})
            self.emit('dup_drivers_loaded', {'groups': [], 'error': 'offline'})
            return

        def worker():
            try:
                drivers = self._get_third_party_drivers()
                active_infs = dupdrivers_core.get_active_published_infs(self._run)
                result, deletable = dupdrivers_core.build_duplicate_groups(drivers, active_infs)
                self.emit('dup_drivers_loaded', {'groups': result, 'deletable': deletable})
            except Exception as e:
                logging.error(f"[DUPDRV] Listázási hiba: {e}", exc_info=True)
                self.emit('dup_drivers_loaded', {'groups': [], 'error': str(e)})

        # Read-only listázás - a load_drivers mintájára nem foglalja a _task_busy-t.
        threading.Thread(target=worker, daemon=True, name="dup-list").start()

    def delete_duplicate_drivers(self, published_names):
        """A kijelölt régi duplikátum-verziók törlése (dupdrivers_core.delete_duplicate_packages).
        Védelem: a lista újra-ellenőrzésre kerül az aktív inf-ek ellen (a felület
        állapota elavulhatott), gyári (nem oemXX) név sosem törlődik."""
        logging.info(f"[API] delete_duplicate_drivers({published_names})")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ A duplikátum-takarítás csak Élő rendszeren működik!', 'type': 'error'})
            return
        names = [str(n).strip() for n in (published_names or []) if str(n).strip().lower().startswith('oem')]
        if not names:
            self.emit('toast', {'message': '⚠️ Nincs törölhető elem kijelölve!', 'type': 'warning'})
            return

        def worker():
            self.emit('task_start', {'task': 'dupclean', 'title': f'Driver-duplikátumok törlése ({len(names)} db)'})
            active_infs = dupdrivers_core.get_active_published_infs(self._run)
            if active_infs is None:
                self.emit('task_progress', {'task': 'dupclean', 'log': '❌ Az aktívan használt driverek listája nem kérdezhető le - biztonsági okból NEM törlünk.'})
                self.emit('task_complete', {'task': 'dupclean', 'status': '❌ Megszakítva (biztonsági ellenőrzés sikertelen)'})
                return
            ok, fail, skipped = dupdrivers_core.delete_duplicate_packages(
                self._run,
                lambda msg: self.emit('task_progress', {'task': 'dupclean', 'log': msg}),
                names, active_infs, self._check_cancel)
            msg = f'Kész! Törölve: {ok}, Sikertelen: {fail}' + (f', Kihagyva: {skipped}' if skipped else '')
            self.emit('task_complete', {'task': 'dupclean', 'status': msg})
            # Friss listák: a duplikátum-nézet és a fő driver-lista is változott.
            self.list_duplicate_drivers()

        self._safe_thread('dupclean', worker)
