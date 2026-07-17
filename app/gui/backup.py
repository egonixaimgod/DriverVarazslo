"""DriverVarázsló GUI - Mentés és Visszaállítás nézet: driver backup/restore, visszaállítási pont, WIM-kinyerés."""

# === AUTO-IMPORTS ===
import os
import subprocess
import re
import time
import logging
import shutil
import traceback
from datetime import datetime
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
            dism_cmd = ['dism', f'/Image:{self.target_os_path}', '/export-driver', f'/destination:{folder}'] if self.target_os_path else ['dism', '/online', '/export-driver', f'/destination:{folder}']
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
            folder = os.path.join(dest, f"DriverVarázsló_FullExport_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            os.makedirs(folder, exist_ok=True)
            self.emit('task_start', {'task': 'backup', 'title': 'ÖSSZES Driver Exportálása'})
            self.emit('task_progress', {'task': 'backup', 'log': 'Driver lista lekérdezése...', 'indeterminate': True})

            success = 0
            fail = 0
            cancelled = False

            self.emit('task_progress', {'task': 'backup', 'log': 'DISM driver exportálás indítása... (Ez eltarthat egy ideig)', 'indeterminate': True})
            dism_cmd = ['dism', f'/Image:{self.target_os_path}', '/export-driver', f'/destination:{folder}'] if self.target_os_path else ['dism', '/online', '/export-driver', f'/destination:{folder}']
            res = self._run(dism_cmd)
            if res.returncode == 0:
                success += 1
                self.emit('task_progress', {'task': 'backup', 'log': '✅ DISM exportálás sikeres!'})
            else:
                fail += 1
                self.emit('task_progress', {'task': 'backup', 'log': f'❌ Hiba az exportálásnál: {res.stderr[:300]}'})

            if cancelled:
                self.emit('task_complete', {'task': 'backup', 'status': f'❗ Megszakítva! OEM: {success} db exportálva',
                                            'log': f'\n--- MEGSZAKÍTVA! Sikeres: {success}, Sikertelen: {fail} ---'})
                return

            # Copy inbox drivers (FileRepository + INF)
            if self._check_cancel():
                self.emit('task_complete', {'task': 'backup', 'status': '❗ Megszakítva!', 'log': '\n--- MEGSZAKÍTVA! ---'})
                return
            self.emit('task_progress', {'task': 'backup', 'log': 'Windows inbox driverek másolása (FileRepository)...', 'indeterminate': True})
            windows_dir = os.path.join(self.target_os_path, 'Windows') if self.target_os_path else os.environ.get('SYSTEMROOT', r'C:\Windows')
            driverstore = os.path.join(windows_dir, 'System32', 'DriverStore', 'FileRepository')
            inbox_folder = os.path.join(folder, '_Windows_Inbox_Drivers')
            os.makedirs(inbox_folder, exist_ok=True)

            needed_bytes = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(driverstore) for f in fs if os.path.exists(os.path.join(r, f))) if os.path.exists(driverstore) else 0
            free_bytes = shutil.disk_usage(dest).free
            if needed_bytes > free_bytes:
                self.emit('task_progress', {'task': 'backup', 'log': f'❌ Nincs elég szabad hely a célmeghajtón! Szükséges kb. {needed_bytes // (1024*1024)} MB, elérhető: {free_bytes // (1024*1024)} MB.'})
                self.emit('task_complete', {'task': 'backup', 'status': '❌ Nincs elég szabad hely!'})
                return

            self._run(['robocopy', driverstore, inbox_folder, '/E', '/R:0', '/W:0', '/NFL', '/NDL', '/NJH', '/NJS', '/NC', '/NS', '/NP'])

            if self._check_cancel():
                self.emit('task_complete', {'task': 'backup', 'status': '❗ Megszakítva!', 'log': '\n--- MEGSZAKÍTVA! ---'})
                return
            self.emit('task_progress', {'task': 'backup', 'log': 'Windows INF mappa másolása...'})
            inf_src = os.path.join(windows_dir, 'INF')
            inbox_inf_folder = os.path.join(folder, '_Windows_Inbox_INF')
            os.makedirs(inbox_inf_folder, exist_ok=True)
            self._run(['robocopy', inf_src, inbox_inf_folder, '/E', '/R:0', '/W:0', '/NFL', '/NDL', '/NJH', '/NJS', '/NC', '/NS', '/NP'])

            total_size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fns in os.walk(folder) for f in fns
                             if os.path.exists(os.path.join(dp, f)))
            size_mb = total_size / (1024 * 1024)
            self.emit('task_complete', {'task': 'backup',
                                        'status': f'✅ Kész! OEM: {"Sikeres" if success else "Sikertelen"}, Inbox másolva. Méret: {size_mb:.0f} MB',
                                        'log': f'\n--- Export kész: {folder} ({size_mb:.0f} MB) | Sikeres: {success}, Sikertelen: {fail} ---'})
        self._safe_thread('backup', worker)

    def create_restore_point(self):
        logging.info("[API] create_restore_point()")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: Visszaállítási pont csak Élő rendszeren készíthető!', 'type': 'error'})
            return
        def worker():
            logging.info("[RESTORE_POINT] Worker indult - visszaállítási pont létrehozása...")
            desc = f"DriverVarázsló_Backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            logging.info(f"[RESTORE_POINT] Név: {desc}")
            self.emit('task_start', {'task': 'rp', 'title': 'Visszaállítási Pont'})
            self.emit('task_progress', {'task': 'rp', 'log': 'Rendszervédelem engedélyezése...', 'indeterminate': True})

            # 1) Enable System Restore on C: (force enable even if disabled)
            logging.info("[RESTORE_POINT] Rendszervédelem engedélyezése...")
            enable_ps = '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; try { Enable-ComputerRestore -Drive "$($env:SystemDrive)\\" -ErrorAction Stop; Write-Output "OK" } catch { Write-Output "FAIL: $($_.Exception.Message)" }'
            enable_res = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", enable_ps], encoding='utf-8')
            enable_out = (enable_res.stdout or '').strip()
            if 'FAIL' in enable_out:
                logging.warning(f"[RESTORE_POINT] Enable-ComputerRestore hiba: {enable_out}")
                # Try via registry + vssadmin as fallback
                self.emit('task_progress', {'task': 'rp', 'log': f'⚠ Enable-ComputerRestore hiba: {enable_out}\nRegistry + vssadmin fallback...'})
                self._run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore', '/v', 'DisableSR', '/t', 'REG_DWORD', '/d', '0', '/f'])
                self._run(['vssadmin', 'resize', 'shadowstorage', f'/for={os.environ.get("SystemDrive", "C:")}', f'/on={os.environ.get("SystemDrive", "C:")}', '/maxsize=5%'])
                # Retry enable
                enable_res2 = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", enable_ps], encoding='utf-8')
                enable_out2 = (enable_res2.stdout or '').strip()
                if 'FAIL' in enable_out2:
                    logging.error(f"[RESTORE_POINT] Rendszervédelem nem kapcsolható be: {enable_out2}")
                    self.emit('task_complete', {'task': 'rp', 'status': f'❌ Rendszervédelem nem kapcsolható be: {enable_out2}'})
                    return
                logging.info("[RESTORE_POINT] Rendszervédelem bekapcsolva (fallback)")
                self.emit('task_progress', {'task': 'rp', 'log': '✅ Rendszervédelem bekapcsolva (fallback)'})
            else:
                logging.info("[RESTORE_POINT] Rendszervédelem bekapcsolva")
                self.emit('task_progress', {'task': 'rp', 'log': '✅ Rendszervédelem bekapcsolva'})

            # 2) Disable 24-hour frequency limit
            logging.info("[RESTORE_POINT] 24 órás limit feloldása...")
            self._run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore', 
                       '/v', 'SystemRestorePointCreationFrequency', '/t', 'REG_DWORD', '/d', '0', '/f'])

            # 3) Create restore point
            logging.info("[RESTORE_POINT] Checkpoint-Computer futtatása...")
            self.emit('task_progress', {'task': 'rp', 'log': f'Visszaállítási pont: {desc}', 'status': 'Pont létrehozása...'})
            create_ps = f'[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; try {{ Checkpoint-Computer -Description "{desc}" -RestorePointType "MODIFY_SETTINGS" -ErrorAction Stop; Write-Output "OK" }} catch {{ Write-Output "FAIL: $($_.Exception.Message)" }}'
            res = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", create_ps], encoding='utf-8')
            create_out = (res.stdout or '').strip()
            logging.debug(f"[RESTORE_POINT] Checkpoint result: {create_out}")

            # 4) Verify
            logging.info("[RESTORE_POINT] Ellenőrzés...")
            verify_ps = f'[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; (Get-ComputerRestorePoint | Where-Object {{ $_.Description -eq "{desc}" }}).Description'
            verify_res = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", verify_ps], encoding='utf-8')
            verified = desc in (verify_res.stdout or '')
            logging.debug(f"[RESTORE_POINT] Verified: {verified}")

            if 'OK' in create_out and verified:
                logging.info(f"[RESTORE_POINT] Sikeresen létrehozva: {desc}")
                self.emit('task_complete', {'task': 'rp', 'status': f'✅ Visszaállítási pont létrehozva: {desc}'})
            elif 'OK' in create_out:
                logging.warning("[RESTORE_POINT] Lefutott de nem ellenőrizhető (késleltetett létrehozás?)")
                self.emit('task_complete', {'task': 'rp', 'status': '⚠ Visszaállítási pont létrehozás elindítva (ellenőrzés később)'})
            else:
                logging.error(f"[RESTORE_POINT] Hiba: {create_out}")
                self.emit('task_complete', {'task': 'rp', 'status': f'❌ Hiba: {create_out}'})
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

            restore_had_errors = False
            norm_source = os.path.normpath(source)
            norm_target = os.path.normpath(target) if target else None
            logging.debug(f"[RESTORE] norm_source={norm_source}, norm_target={norm_target}")

            # Detect source type
            is_wim_extract = False
            if not online:
                repo_check = os.path.join(norm_source, "FileRepository")
                inf_check = os.path.join(norm_source, "INF")
                if os.path.isdir(repo_check) or os.path.isdir(inf_check):
                    is_wim_extract = True
            inbox_subfolder = os.path.join(norm_source, "_Windows_Inbox_Drivers") if not online else None
            has_inbox_subfolder = inbox_subfolder and os.path.isdir(inbox_subfolder)
            logging.info(f"[RESTORE] Típus detektálás: is_wim_extract={is_wim_extract}, has_inbox_subfolder={has_inbox_subfolder}")

            def force_copy(src, dst):
                """Robocopy-based forced copy with fallback for inbox/system drivers.
                Visszatérési érték: True, ha a másolás közben bármilyen hiba történt
                (a hívónak ezt a végső "sikeres" összegzésbe be KELL számítania,
                különben egy ténylegesen hiányos másolás is sikeresnek tűnik)."""
                logging.debug(f"[RESTORE] force_copy: {src} -> {dst}")
                if not os.path.exists(src):
                    logging.warning(f"[RESTORE] Forrás nem létezik: {src}")
                    self.emit('task_progress', {'task': 'restore', 'log': f'  ❌ Forrás nem létezik: {src}'})
                    return True
                os.makedirs(dst, exist_ok=True)

                free_bytes = shutil.disk_usage(dst).free
                needed_bytes = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(src) for f in fs if os.path.exists(os.path.join(r, f)))
                if needed_bytes > free_bytes:
                    msg = (f'  ❌ Nincs elég szabad hely a célmeghajtón! Szükséges: {needed_bytes // (1024*1024)} MB, '
                           f'elérhető: {free_bytes // (1024*1024)} MB.')
                    self.emit('task_progress', {'task': 'restore', 'log': msg})
                    return True

                self.emit('task_progress', {'task': 'restore', 'log': f'\n  Robocopy indul: {os.path.basename(src)} -> {os.path.basename(dst)}\n  (Backup mód - Windows jogosultságok megkerülése)'})
                cmd = ['robocopy', src, dst, '/E', '/ZB', '/R:1', '/W:1', '/COPY:DAT', '/NC', '/NS', '/NFL', '/NDL', '/NP']
                res = self._run(cmd)

                if res.returncode < 8:
                    logging.info(f"[RESTORE] Robocopy sikeres, returncode={res.returncode}")
                    self.emit('task_progress', {'task': 'restore', 'log': f'  ✅ Sikeres robocopy kényszerítés ({res.returncode})'})
                    return False
                else:
                    self.emit('task_progress', {'task': 'restore', 'log': f'  ⚠️ Robocopy hiba ({res.returncode}), végső tartalék: mappánkénti jogszerzés (lassabb)...'})
                    had_error = False
                    for root, _, files in os.walk(src):
                        if self._cancel_flag: return had_error
                        rel = os.path.relpath(root, src)
                        target_dir = os.path.join(dst, rel) if rel != '.' else dst
                        os.makedirs(target_dir, exist_ok=True)

                        for f in files:
                            if self._cancel_flag: return had_error
                            sfile = os.path.join(root, f)
                            dfile = os.path.join(target_dir, f)
                            if os.path.exists(dfile):
                                self._run(f'takeown /f "{dfile}" /A', shell=True)
                                self._run(f'icacls "{dfile}" /grant *S-1-5-32-544:F', shell=True)
                                self._run(f'attrib -R "{dfile}"', shell=True)
                            try:
                                shutil.copy2(sfile, dfile)
                            except Exception as e:
                                self.emit('task_progress', {'task': 'restore', 'log': f'❌ Hiba ({f}): {e}'})
                                had_error = True
                    if had_error:
                        self.emit('task_progress', {'task': 'restore', 'log': '  ⚠️ Fallback másolás hibákkal fejeződött be.'})
                    else:
                        self.emit('task_progress', {'task': 'restore', 'log': '  ✅ Fallback másolás befejeződött.'})
                    return had_error

            def run_dism_add_driver(driver_path, label=""):
                """Run DISM /Add-Driver on a folder with /Recurse. Returns (returncode, cancelled)."""
                scratch = os.path.join(norm_target, "Scratch")
                os.makedirs(scratch, exist_ok=True)
                cmd = ['dism', f'/Image:{norm_target}', '/Add-Driver', f'/Driver:{driver_path}', '/Recurse', '/ForceUnsigned', f'/ScratchDir:{scratch}']
                self.emit('task_progress', {'task': 'restore', 'log': f'{label}Parancs: {" ".join(cmd)}'})
                process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                           startupinfo=self._si, creationflags=self._nw, errors='replace')
                cancelled = False
                for line in process.stdout:
                    if self._check_cancel():
                        # Nem lőjük ki a processzt erőszakosan, hogy ne korrumpálódjon a Windows
                        cancelled = True
                        self.emit('task_progress', {'task': 'restore', 'log': '⚠️ Megszakítás kérve, várakozás a biztonságos leállásra...'})
                        break
                    stripped = line.strip()
                    if stripped:
                        self.emit('task_progress', {'task': 'restore', 'log': stripped})
                process.wait()
                if not cancelled:
                    self.emit('task_progress', {'task': 'restore', 'log': f'Return code: {process.returncode}'})
                return (process.returncode, cancelled)

            if online:
                cmd = ['pnputil', '/add-driver', f"{norm_source}\\*.inf", '/subdirs', '/install']
                self.emit('task_progress', {'task': 'restore', 'log': f'Parancs: {" ".join(cmd)}'})
                process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                           startupinfo=self._si, creationflags=self._nw, errors='replace')
                cancelled = False
                for line in process.stdout:
                    if self._check_cancel():
                        # Nem lőjük ki a processzt erőszakosan, hogy ne korrumpálódjon a Windows
                        cancelled = True
                        self.emit('task_progress', {'task': 'restore', 'log': '⚠️ Megszakítás kérve, várakozás a biztonságos leállásra...'})
                        break
                    self.emit('task_progress', {'task': 'restore', 'log': line.strip()})
                process.wait()
                if cancelled:
                    self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                    return
                self.emit('task_progress', {'task': 'restore', 'log': f'\nReturn code: {process.returncode}'})
            elif is_wim_extract:
                # WIM-ből kimentett driverek (Windows_Gyari_Alap_Driverek_*)
                # Ezek FileRepository + INF formátumban vannak
                self.emit('task_progress', {'task': 'restore', 'log': 'WIM-ből kimentett gyári driverek visszaállítása...'})
                new_format_repo = os.path.join(norm_source, "FileRepository")
                new_format_inf = os.path.join(norm_source, "INF")
                target_repo = os.path.join(norm_target, "Windows", "System32", "DriverStore", "FileRepository")
                target_inf = os.path.join(norm_target, "Windows", "INF")

                try:
                    if os.path.exists(new_format_repo):
                        self.emit('task_progress', {'task': 'restore', 'log': '1/2 FileRepository és INF fizikai másolása...'})
                        restore_had_errors = force_copy(new_format_repo, target_repo) or restore_had_errors
                        if self._check_cancel():
                            self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                            return
                        if os.path.exists(new_format_inf):
                            restore_had_errors = force_copy(new_format_inf, target_inf) or restore_had_errors
                            if self._check_cancel():
                                self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                                return
                    else:
                        self.emit('task_progress', {'task': 'restore', 'log': '1/2 DriverStore fizikai másolása...'})
                        restore_had_errors = force_copy(norm_source, target_repo) or restore_had_errors
                        if self._check_cancel():
                            self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                            return

                    if restore_had_errors:
                        self.emit('task_progress', {'task': 'restore', 'log': '⚠️ Fizikai másolás hibákkal fejeződött be!'})
                    else:
                        self.emit('task_progress', {'task': 'restore', 'log': '✅ Fizikai másolás kész!'})
                except Exception as e:
                    err_msg = str(e)
                    if len(err_msg) > 300: err_msg = err_msg[:300] + "..."
                    self.emit('task_progress', {'task': 'restore', 'log': f'❌ Másolási hiba: {err_msg}'})
                    restore_had_errors = True

                # DISM regisztrálás a fizikai másolás után
                self.emit('task_progress', {'task': 'restore', 'log': '\n2/2 DISM driver regisztrálás (inbox drivereknél sok hiba normális)...'})
                _, dism_cancelled = run_dism_add_driver(norm_source, "")
                if dism_cancelled:
                    self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                    return
                self.emit('task_progress', {'task': 'restore', 'log': '✅ A fizikai másolás + DISM regisztrálás kész. Az inbox driverek a másolásnak köszönhetően elérhetőek.'})

            elif has_inbox_subfolder:
                # DriverVarázsló_FullExport / ALL_Driver_Backup formátum: _Windows_Inbox_Drivers + oem almappák
                self.emit('task_progress', {'task': 'restore', 'log': 'Teljes export formátum észlelve (DriverVarázsló_FullExport / ALL_Driver_Backup).\n'
                                            'Az inbox drivereket fizikailag másoljuk (DISM nem tudja telepíteni őket),\n'
                                            'az OEM drivereket DISM-mel regisztráljuk.\n'})

                # 1) Inbox driverek fizikai másolása (FileRepository + INF)
                target_repo = os.path.join(norm_target, "Windows", "System32", "DriverStore", "FileRepository")
                target_inf = os.path.join(norm_target, "Windows", "INF")
                inbox_inf_subfolder = os.path.join(norm_source, "_Windows_Inbox_INF")
                self.emit('task_progress', {'task': 'restore', 'log': '--- 1. LÉPÉS: Inbox driverek fizikai másolása a DriverStore-ba ---'})
                try:
                    restore_had_errors = force_copy(inbox_subfolder, target_repo) or restore_had_errors
                    if self._check_cancel():
                        self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                        return
                    if os.path.isdir(inbox_inf_subfolder):
                        self.emit('task_progress', {'task': 'restore', 'log': 'Windows INF mappa visszamásolása (új formátumú backup)...'})
                        restore_had_errors = force_copy(inbox_inf_subfolder, target_inf) or restore_had_errors
                        if self._check_cancel():
                            self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                            return
                    else:
                        # Régi backup: nincs _Windows_Inbox_INF, ezért a FileRepository almappáiból
                        # kiszedjük az .inf fájlokat és bemásoljuk a Windows\INF-be
                        self.emit('task_progress', {'task': 'restore', 'log': 'Régi backup formátum: _Windows_Inbox_INF nem található.\n'
                                                    'INF fájlok kinyerése a FileRepository almappáiból...'})
                        os.makedirs(target_inf, exist_ok=True)
                        inf_count = 0
                        for repo_dir in os.listdir(inbox_subfolder):
                            repo_path = os.path.join(inbox_subfolder, repo_dir)
                            if not os.path.isdir(repo_path):
                                continue
                            for fname in os.listdir(repo_path):
                                if fname.lower().endswith('.inf'):
                                    src_inf = os.path.join(repo_path, fname)
                                    dst_inf = os.path.join(target_inf, fname)
                                    try:
                                        shutil.copy2(src_inf, dst_inf)
                                        inf_count += 1
                                    except Exception as e:
                                        logging.debug(e)
                        self.emit('task_progress', {'task': 'restore', 'log': f'✅ {inf_count} db .inf fájl kinyerve a Windows\\INF mappába (.pnf-eket a Windows legenerálja bootoláskor).'})
                    if restore_had_errors:
                        self.emit('task_progress', {'task': 'restore', 'log': '⚠️ Inbox driverek fizikai másolása hibákkal fejeződött be!'})
                    else:
                        self.emit('task_progress', {'task': 'restore', 'log': '✅ Inbox driverek fizikai másolása kész!'})
                except Exception as e:
                    err_msg = str(e)
                    if len(err_msg) > 300: err_msg = err_msg[:300] + "..."
                    self.emit('task_progress', {'task': 'restore', 'log': f'❌ Inbox másolási hiba: {err_msg}'})
                    restore_had_errors = True

                # 2) OEM driverek DISM-mel (almappák, amik nem _Windows_Inbox_Drivers)
                oem_folders = []
                for item in os.listdir(norm_source):
                    item_path = os.path.join(norm_source, item)
                    if os.path.isdir(item_path) and item not in ("_Windows_Inbox_Drivers", "_Windows_Inbox_INF"):
                        # Check if folder contains any .inf files (directly or in subfolders)
                        has_inf = any(f.lower().endswith('.inf') for _, _, fns in os.walk(item_path) for f in fns)
                        if has_inf:
                            oem_folders.append(item_path)

                if oem_folders:
                    self.emit('task_progress', {'task': 'restore', 'log': f'\n--- 2. LÉPÉS: {len(oem_folders)} db OEM driver mappa DISM regisztrálása ---'})
                    for i, oem_path in enumerate(oem_folders):
                        if self._check_cancel():
                            self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                            return
                        self.emit('task_progress', {'task': 'restore', 'log': f'\n[{i+1}/{len(oem_folders)}] {os.path.basename(oem_path)}:'})
                        _, dism_cancelled = run_dism_add_driver(oem_path, "  ")
                        if dism_cancelled:
                            self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                            return
                    self.emit('task_progress', {'task': 'restore', 'log': '\n✅ OEM driverek DISM regisztrálása kész!'})
                else:
                    self.emit('task_progress', {'task': 'restore', 'log': '\nNincs OEM driver mappa a backup-ban.'})

            else:
                # Egyéb mappa (pl. DriverVarázsló_Export / Driver_Backup third-party export) — tisztán DISM
                _, dism_cancelled = run_dism_add_driver(norm_source, "")
                if dism_cancelled:
                    self.emit('task_complete', {'task': 'restore', 'status': '❗ Megszakítva!'})
                    return

            # Post-install
            if online:
                is_pe = os.environ.get('SystemDrive', 'C:') == 'X:'
                if not is_pe:
                    self.emit('task_progress', {'task': 'restore', 'log': 'Hardverváltozások keresése...'})
                    time.sleep(1.5)
                    self._run(['pnputil', '/scan-devices'])
                    time.sleep(10)
                    self.emit('task_progress', {'task': 'restore', 'log': '✅ Scan kész!'})
            else:
                # === BCD JAVÍTÁS (boot loader) ===
                self._repair_bcd(norm_target)
                
                # Automata PnP rescan beállítása az asztal betöltésére
                self.emit('task_progress', {'task': 'restore', 'log': '\nElső bejelentkezési rescan script beállítása...'})
                startup_dir = os.path.join(target, "ProgramData", "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
                os.makedirs(startup_dir, exist_ok=True)
                bat_path = os.path.join(startup_dir, "auto_pnputil_scan.bat")
                bat_content = (
                    '@echo off\n'
                    'set LOGFILE="%SystemDrive%\\Users\\Public\\driver_startup_log.txt"\n'
                    'echo [%DATE% %TIME%] Boot rescan indult... >> %LOGFILE%\n'
                    'pnputil /scan-devices >> %LOGFILE% 2>&1\n'
                    'echo [%DATE% %TIME%] Kesz! >> %LOGFILE%\n'
                    'ping 127.0.0.1 -n 3 > nul\n'
                    '(goto) 2>nul & del "%~f0"\n'
                )
                try:
                    with open(bat_path, 'w', encoding='utf-8') as f:
                        f.write(bat_content)
                    self.emit('task_progress', {'task': 'restore', 'log': '✅ Startup script elhelyezve.'})
                except Exception as e:
                    self.emit('task_progress', {'task': 'restore', 'log': f'⚠ Script írási hiba: {e}'})

            self.emit('task_progress', {'task': 'restore', 'log': '\n==== BEFEJEZVE ===='})
            if restore_had_errors:
                self.emit('task_progress', {'task': 'restore', 'log': '⚠️ A visszaállítás hibákkal fejeződött be - egyes driverek fizikai másolása sikertelen volt, a napló tartalmazza a részleteket!'})
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
            wim = os.path.abspath(wim_path).replace("/", "\\")
            # A WIM csatolási mappának a C: meghajtón kell lennie (NTFS), mert a cserélhető meghajtókat (USB) a DISM visszautasítja
            is_pe = os.environ.get('SystemDrive', 'C:') == 'X:'
            if is_pe:
                # WinPE-ben a SystemDrive az X: RAM-disk - sosem szabad oda írni nagy fájlokat,
                # attól függetlenül, hogy van-e kiválasztott offline cél-OS.
                sys_temp = os.path.join(self.target_os_path, 'DV_Temp') if self.target_os_path else r'C:\DV_Temp'
            else:
                sys_temp = os.environ.get('SystemDrive', 'C:') + '\\DV_Temp'
            mount_dir = os.path.join(sys_temp, f"WIM_{int(time.time())}")
            target_folder = os.path.join(dest, f"Windows_Gyari_Alap_Driverek_{datetime.now().strftime('%Y%m%d_%H%M')}")
            logging.info(f"[WIM] Mount dir: {mount_dir}")
            logging.info(f"[WIM] Target folder: {target_folder}")

            if os.path.exists(mount_dir):
                logging.debug("[WIM] Régi mount dir törlése...")
                shutil.rmtree(mount_dir, ignore_errors=True)
            os.makedirs(mount_dir, exist_ok=True)
            os.makedirs(target_folder, exist_ok=True)

            try:
                # Cancel check before mount
                if self._check_cancel():
                    self.emit('task_complete', {'task': 'wim', 'status': '❗ Megszakítva!'})
                    return

                wim_to_mount = wim
                if wim.lower().endswith('.esd'):
                    needed_bytes = os.path.getsize(wim) * 2  # biztonsági ráhagyás a konvertált WIM méretére
                    free_bytes = shutil.disk_usage(sys_temp).free
                    if needed_bytes > free_bytes:
                        raise Exception(f"Nincs elég szabad hely a konvertáláshoz! Szükséges kb. {needed_bytes // (1024*1024)} MB, elérhető: {free_bytes // (1024*1024)} MB ({sys_temp}).")
                    self.emit('task_progress', {'task': 'wim', 'log': 'ESD -> WIM konvertálás (ez 10-15 percet is igénybe vehet!)...', 'indeterminate': True, 'counter': '1/4', 'status': 'Fájl konvertálása...'})
                    temp_wim = os.path.join(sys_temp, f"converted_{int(time.time())}.wim")
                    res_esd = self._run(["dism", "/Export-Image", f"/SourceImageFile:{wim}", "/SourceIndex:1", f"/DestinationImageFile:{temp_wim}", "/Compress:max", "/CheckIntegrity"])
                    if res_esd.returncode != 0:
                        raise Exception(f"ESD Konvertálási hiba: {res_esd.stderr}")
                    wim_to_mount = temp_wim
                    self.emit('task_progress', {'task': 'wim', 'counter': '2/4', 'status': 'Képfájl csatolása...'})
                else:
                    self.emit('task_progress', {'task': 'wim', 'log': 'WIM csatolás (ez 4-5 perc)...', 'indeterminate': True, 'counter': '1/3', 'status': 'Képfájl csatolása...'})

                logging.info("[WIM] DISM Mount-Image futtatása...")
                res = self._run(["dism", "/Mount-Image", f"/ImageFile:{wim_to_mount}", "/Index:1", f"/MountDir:{mount_dir}", "/ReadOnly"])
                if res.returncode != 0:
                    logging.error(f"[WIM] DISM Mount hiba: {res.stdout} {res.stderr}")
                    raise Exception(f"DISM Mount hiba: {res.stdout} {res.stderr}")
                
                # Cancel check after mount (will unmount in except)
                if self._check_cancel():
                    raise Exception("Megszakítva a felhasználó által")
                
                logging.info("[WIM] WIM csatolva, fájlok másolása...")

                self.emit('task_progress', {'task': 'wim', 'log': 'Fájlok másolása...', 'counter': '2/3', 'status': 'Gyári driverek másolása...'})
                
                driverstore = os.path.join(mount_dir, "Windows", "System32", "DriverStore", "FileRepository")
                target_repo = os.path.join(target_folder, "FileRepository")
                if os.path.exists(driverstore):
                    needed_bytes = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(driverstore) for f in fs if os.path.exists(os.path.join(r, f)))
                    free_bytes = shutil.disk_usage(target_folder).free
                    if needed_bytes > free_bytes:
                        raise Exception(f"Nincs elég szabad hely a célmappában! Szükséges kb. {needed_bytes // (1024*1024)} MB, elérhető: {free_bytes // (1024*1024)} MB.")
                    logging.info(f"[WIM] FileRepository másolása: {driverstore} -> {target_repo}")
                    shutil.copytree(driverstore, target_repo, dirs_exist_ok=True)
                else:
                    logging.error("[WIM] FileRepository nem található!")
                    raise Exception("FileRepository nem található a WIM-ben!")

                inf_dir = os.path.join(mount_dir, "Windows", "INF")
                target_inf = os.path.join(target_folder, "INF")
                if os.path.exists(inf_dir):
                    logging.info(f"[WIM] INF mappa másolása: {inf_dir} -> {target_inf}")
                    shutil.copytree(inf_dir, target_inf, dirs_exist_ok=True)

                logging.info("[WIM] WIM leválasztása...")
                self.emit('task_progress', {'task': 'wim', 'log': 'WIM leválasztása...', 'counter': '3/3', 'status': 'Takarítás...'})
                self._run(["dism", "/Unmount-Image", f"/MountDir:{mount_dir}", "/Discard"])
                self._run(["dism", "/Cleanup-Wim"])
                for _ in range(3):
                    try:
                        shutil.rmtree(mount_dir, ignore_errors=False)
                        break
                    except Exception:
                        time.sleep(2)
                shutil.rmtree(mount_dir, ignore_errors=True)
                if wim.lower().endswith('.esd') and 'wim_to_mount' in locals() and os.path.exists(wim_to_mount):
                    try: os.remove(wim_to_mount)
                    except Exception: pass

                logging.info(f"[WIM] Kész! Kimenet: {target_folder}")
                self.emit('task_complete', {'task': 'wim', 'status': f'✅ Gyári driverek kimentve: {target_folder}',
                                            'log': f'\n✅ Kész! Mappa: {target_folder}'})
            except Exception as e:
                logging.error(f"[WIM] Hiba: {e}")
                logging.error(traceback.format_exc())
                self._run(["dism", "/Unmount-Image", f"/MountDir:{mount_dir}", "/Discard"])
                self._run(["dism", "/Cleanup-Wim"])
                shutil.rmtree(mount_dir, ignore_errors=True)
                # Az ESD->WIM konvertált ideiglenes fájlt hiba esetén is töröljük, különben egy
                # több GB-os "converted_*.wim" örökre ott marad a DV_Temp mappában. Fontos: sosem
                # a wim_to_mount == wim (eredeti forrásfájl) esetet töröljük.
                try:
                    if wim.lower().endswith('.esd') and 'wim_to_mount' in locals() and wim_to_mount != wim and os.path.exists(wim_to_mount):
                        os.remove(wim_to_mount)
                except Exception:
                    pass
                self.emit('task_error', {'task': 'wim', 'error': str(e)})
                self.emit('task_complete', {'task': 'wim', 'status': f'❌ Hiba: {e}'})

        self._safe_thread('wim', worker)
