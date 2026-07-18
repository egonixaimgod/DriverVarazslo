"""DriverVarázsló GUI - Mentés és Visszaállítás nézet: driver backup/restore,
visszaállítási pont, WIM-kinyerés (a közös logika: app/backup_core.py)."""

# === AUTO-IMPORTS ===
import os
import subprocess
import re
import logging
from datetime import datetime
from app import backup_core
# === /AUTO-IMPORTS ===


class GuiBackupMixin:
    """Mentés és Visszaállítás nézet: driver backup/restore, visszaállítási pont, WIM-kinyerés. A DriverToolApi része (összerakás: app/gui/api.py)."""

    # ================================================================
    # BACKUP / RESTORE
    # ================================================================
    def backup_third_party(self):
        logging.info("[API] backup_third_party()")
        dest = self.select_directory('Válassz mappát a driverek kimentéséhez')
        if not dest:
            logging.info("[BACKUP] Mégse - nincs mappa kiválasztva")
            return
        logging.info(f"[BACKUP] Third-party backup indítása -> {dest}")

        def worker():
            folder = os.path.join(dest, f"DriverVarázsló_Export_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            logging.info(f"[BACKUP] Célmappa létrehozása: {folder}")
            os.makedirs(folder, exist_ok=True)
            self.emit('task_start', {'task': 'backup', 'title': 'Driver Exportálás'})
            self.emit('task_progress', {'task': 'backup', 'log': f'Célmappa: {folder}\nExportálás indítása...', 'indeterminate': True})

            logging.info("[BACKUP] DISM export-driver futtatása...")
            dism_cmd = backup_core.export_drivers_cmd(self.target_os_path, folder)
            logging.debug(f"[CMD] Popen futtatása: {' '.join(dism_cmd)}")
            process = subprocess.Popen(
                dism_cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                startupinfo=self._si, creationflags=self._nw, errors='replace')

            cancelled = False
            for line in process.stdout:
                if self._check_cancel():
                    self._run(['taskkill', '/F', '/T', '/PID', str(process.pid)])
                    process.wait()  # Prevent zombie process
                    cancelled = True
                    break
                line = line.strip()
                if not line:
                    continue
                logging.debug(f"[BACKUP] DISM: {line[:100]}")
                m = re.search(r'(\d+)\s*(?:/|of)\s*(\d+)', line, re.I)
                if m:
                    self.emit('task_progress', {'task': 'backup', 'current': int(m.group(1)), 'total': int(m.group(2)),
                                                'counter': f'{m.group(1)}/{m.group(2)}', 'status': line[:60]})
                self.emit('task_progress', {'task': 'backup', 'log': line})
            process.wait()

            if cancelled:
                self.emit('task_complete', {'task': 'backup', 'status': '❗ Megszakítva!', 'log': '\n--- MEGSZAKÍTVA! ---'})
                return

            success = process.returncode == 0
            logging.info(f"[BACKUP] DISM befejezve, returncode={process.returncode}")
            self.emit('task_complete', {'task': 'backup',
                                        'status': f'{"✅ Sikeres export!" if success else "❌ Hiba!"} Mappa: {folder}',
                                        'log': f'\n--- {"Sikeres" if success else "Hibás"} export: {folder} ---'})
        self._safe_thread('backup', worker)

    def backup_all(self):
        logging.info("[API] backup_all()")
        dest = self.select_directory('Válassz mappát az ÖSSZES driver kimentéséhez')
        if not dest:
            logging.info("[BACKUP_ALL] Mégse - nincs mappa kiválasztva")
            return
        logging.info(f"[BACKUP_ALL] Összes driver backup indítása -> {dest}")

        def worker():
            self.emit('task_start', {'task': 'backup', 'title': 'ÖSSZES Driver Exportálása'})
            result = backup_core.backup_all_drivers(
                self._run,
                lambda msg: self.emit('task_progress', {'task': 'backup', 'log': msg, 'indeterminate': True}),
                self._check_cancel,
                dest, self.target_os_path)

            if result['status'] == 'cancelled':
                self.emit('task_complete', {'task': 'backup', 'status': '❗ Megszakítva!', 'log': '\n--- MEGSZAKÍTVA! ---'})
            elif result['status'] == 'no_space':
                self.emit('task_complete', {'task': 'backup', 'status': '❌ Nincs elég szabad hely!'})
            else:
                size_mb = result['size_mb']
                self.emit('task_complete', {'task': 'backup',
                                            'status': f'✅ Kész! OEM: {"Sikeres" if result["dism_ok"] else "Sikertelen"}, Inbox másolva. Méret: {size_mb:.0f} MB',
                                            'log': f'\n--- Export kész: {result["folder"]} ({size_mb:.0f} MB) ---'})
        self._safe_thread('backup', worker)

    def create_restore_point(self):
        logging.info("[API] create_restore_point()")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: Visszaállítási pont csak Élő rendszeren készíthető!', 'type': 'error'})
            return
        def worker():
            logging.info("[RESTORE_POINT] Worker indult - visszaállítási pont létrehozása...")
            self.emit('task_start', {'task': 'rp', 'title': 'Visszaállítási Pont'})
            status, desc = backup_core.create_restore_point(
                self._run,
                lambda msg: self.emit('task_progress', {'task': 'rp', 'log': msg, 'indeterminate': True}))
            if status == 'ok':
                self.emit('task_complete', {'task': 'rp', 'status': f'✅ Visszaállítási pont létrehozva: {desc}'})
            elif status == 'ok_unverified':
                self.emit('task_complete', {'task': 'rp', 'status': '⚠ Visszaállítási pont létrehozás elindítva (ellenőrzés később)'})
            elif status == 'enable_failed':
                self.emit('task_complete', {'task': 'rp', 'status': '❌ Rendszervédelem nem kapcsolható be!'})
            else:
                self.emit('task_complete', {'task': 'rp', 'status': '❌ Hiba a visszaállítási pont létrehozásánál!'})
        self._safe_thread('rp', worker)

    def restore_online(self):
        logging.info("[API] restore_online()")
        source = self.select_directory('ÉLŐ MÓD: Válassz kimentett driver mappát')
        if not source:
            logging.info("[RESTORE] Mégse - nincs forrás kiválasztva")
            return
        logging.info(f"[RESTORE] Online restore indítása: source={source}")
        self._run_restore(online=True, source=source, target=None)

    def restore_offline(self):
        logging.info("[API] restore_offline()")
        target = self.select_directory('OFFLINE MÓD: 1. Válaszd ki a HALOTT WINDOWS meghajtóját')
        if not target:
            logging.info("[RESTORE] Mégse - nincs cél kiválasztva")
            return
        target = os.path.splitdrive(os.path.abspath(target))[0] + "\\"
        logging.info(f"[RESTORE] Offline target: {target}")
        source = self.select_directory('OFFLINE MÓD: 2. Válassz kimentett driver mappát')
        if not source:
            logging.info("[RESTORE] Mégse - nincs forrás kiválasztva")
            return
        logging.info(f"[RESTORE] Offline restore indítása: source={source}, target={target}")
        self._run_restore(online=False, source=source, target=target)

    def _run_restore(self, online, source, target):
        logging.info(f"[RESTORE] _run_restore: online={online}, source={source}, target={target}")
        def worker():
            mode = 'Élő' if online else 'Offline'
            logging.info(f"[RESTORE] Worker indult - {mode} mód")
            self.emit('task_start', {'task': 'restore', 'title': f'Driver Visszaállítás ({mode})'})
            self.emit('task_progress', {'task': 'restore', 'log': f'=== {mode.upper()} RESTORE ===\nForrás: {source}\nCél: {target or "jelenlegi rendszer"}\n', 'indeterminate': True})

            result = backup_core.run_restore(
                self._run,
                lambda msg: self.emit('task_progress', {'task': 'restore', 'log': msg}),
                self._check_cancel,
                self._si, self._nw,
                online, source, target)

            if result == 'cancelled':
                self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
            elif result == 'errors':
                self.emit('task_complete', {'task': 'restore', 'status': '⚠️ Visszaállítás hibákkal fejeződött be!'})
            else:
                self.emit('task_complete', {'task': 'restore', 'status': '✅ Visszaállítás befejezve!'})

        self._safe_thread('restore', worker)

    def extract_wim(self):
        logging.info("[API] extract_wim()")
        wim_path = self.select_file('Válaszd ki az install.wim fájlt', 'WIM fájlok (*.wim)|*.wim')
        if not wim_path:
            logging.info("[WIM] Mégse - nincs WIM kiválasztva")
            return
        logging.info(f"[WIM] WIM fájl: {wim_path}")
        if wim_path.lower().endswith(".esd"):
            logging.info("[WIM] ESD fájl konvertálása szükséges.")
        dest = self.select_directory('Válassz ideiglenes mappát a kicsomagoláshoz')
        if not dest:
            logging.info("[WIM] Mégse - nincs célmappa kiválasztva")
            return
        logging.info(f"[WIM] Célmappa: {dest}")

        def worker():
            logging.info("[WIM] Worker indult - WIM kinyerés...")
            self.emit('task_start', {'task': 'wim', 'title': 'WIM Driver Kinyerés'})

            # A lépésszámláló/fázis-üzenetek a core log-sorai alapján frissülnek.
            step_state = {'esd': wim_path.lower().endswith('.esd')}

            def log(msg):
                extra = {}
                if msg.startswith('ESD -> WIM'):
                    extra = {'indeterminate': True, 'counter': '1/4', 'status': 'Fájl konvertálása...'}
                elif msg.startswith('WIM csatolás'):
                    extra = {'indeterminate': True, 'counter': '1/3', 'status': 'Képfájl csatolása...'}
                elif msg.startswith('Fájlok másolása'):
                    extra = {'counter': '3/4' if step_state['esd'] else '2/3', 'status': 'Gyári driverek másolása...'}
                elif msg.startswith('WIM leválasztása'):
                    extra = {'counter': '4/4' if step_state['esd'] else '3/3', 'status': 'Takarítás...'}
                self.emit('task_progress', {'task': 'wim', 'log': msg, **extra})

            try:
                target_folder = backup_core.extract_wim(
                    self._run, log, self._check_cancel,
                    wim_path, dest, self.target_os_path)
                self.emit('task_complete', {'task': 'wim', 'status': f'✅ Gyári driverek kimentve: {target_folder}',
                                            'log': f'\n✅ Kész! Mappa: {target_folder}'})
            except backup_core.RestoreCancelled:
                self.emit('task_complete', {'task': 'wim', 'status': '❗ Megszakítva!'})
            except Exception as e:
                logging.error(f"[WIM] Hiba: {e}", exc_info=True)
                self.emit('task_error', {'task': 'wim', 'error': str(e)})
                self.emit('task_complete', {'task': 'wim', 'status': f'❌ Hiba: {e}'})

        self._safe_thread('wim', worker)
