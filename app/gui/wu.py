"""DriverVarázsló GUI - Windows Update nézet: driver-push tiltás/engedélyezés, szüneteltetés, szolgáltatás-újraindítás."""

# === AUTO-IMPORTS ===
import os
import time
import logging
import shutil
import winreg
from datetime import datetime, timezone
# === /AUTO-IMPORTS ===


class GuiWuMixin:
    """Windows Update nézet: driver-push tiltás/engedélyezés, szüneteltetés, szolgáltatás-újraindítás. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def _set_wu_pause(self, pause=True):
        if pause:
            ps = r'''
            $pauseDate = (Get-Date).AddDays(7).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
            $nowDate = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
            if (!(Test-Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings')) { New-Item -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Force | Out-Null }
            Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseUpdatesExpiryTime' -Value $pauseDate -Type String -Force | Out-Null
            Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseFeatureUpdatesEndTime' -Value $pauseDate -Type String -Force | Out-Null
            Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseQualityUpdatesEndTime' -Value $pauseDate -Type String -Force | Out-Null
            Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseUpdatesStartTime' -Value $nowDate -Type String -Force | Out-Null
            Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseFeatureUpdatesStartTime' -Value $nowDate -Type String -Force | Out-Null
            Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseQualityUpdatesStartTime' -Value $nowDate -Type String -Force | Out-Null
            '''
            self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps])
            self._run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching', '/v', 'SearchOrderConfig', '/t', 'REG_DWORD', '/d', '0', '/f'])
            self._run(['reg', 'add', r'HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate', '/v', 'ExcludeWUDriversInQualityUpdate', '/t', 'REG_DWORD', '/d', '1', '/f'])
        else:
            ps = r'''
            Remove-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseUpdatesExpiryTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseFeatureUpdatesEndTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseQualityUpdatesEndTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseUpdatesStartTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseFeatureUpdatesStartTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseQualityUpdatesStartTime' -ErrorAction SilentlyContinue
            Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
            Start-Service wuauserv -ErrorAction SilentlyContinue
            '''
            self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps])
            self._run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching', '/v', 'SearchOrderConfig', '/t', 'REG_DWORD', '/d', '1', '/f'])
            self._run(['reg', 'delete', r'HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate', '/v', 'ExcludeWUDriversInQualityUpdate', '/f'])

    # ================================================================
    # WU MANAGEMENT
    # ================================================================
    def check_wu_status(self):
        logging.info("[API] check_wu_status()")
        if self.target_os_path:
            return {'status': 'Offline (Nem olvasható)', 'color': 'unknown'}
        try:
            policy_disabled = False
            search_disabled = False
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate", 0, winreg.KEY_READ) as key:
                    val, _ = winreg.QueryValueEx(key, "ExcludeWUDriversInQualityUpdate")
                    if val == 1: policy_disabled = True
                    logging.debug(f"[WU_STATUS] ExcludeWUDriversInQualityUpdate = {val}")
            except FileNotFoundError:
                logging.debug("[WU_STATUS] ExcludeWUDriversInQualityUpdate kulcs nem létezik")
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching", 0, winreg.KEY_READ) as key:
                    val, _ = winreg.QueryValueEx(key, "SearchOrderConfig")
                    if val == 0: search_disabled = True
                    logging.debug(f"[WU_STATUS] SearchOrderConfig = {val}")
            except FileNotFoundError:
                logging.debug("[WU_STATUS] SearchOrderConfig kulcs nem létezik")

            service_disabled = False
            try:
                res = self._run(['powershell', '-NoProfile', '-Command', '(Get-Service wuauserv).StartType'], encoding='utf-8')
                if res.stdout and 'Disabled' in res.stdout:
                    service_disabled = True
            except Exception:
                pass

            paused_until = None
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\WindowsUpdate\UX\Settings", 0, winreg.KEY_READ) as key:
                    val, _ = winreg.QueryValueEx(key, "PauseUpdatesExpiryTime")
                    if val:
                        dt = datetime.strptime(val, "%Y-%m-%dT%H:%M:%SZ")
                        if dt > datetime.now(timezone.utc).replace(tzinfo=None):
                            paused_until = val
            except Exception:
                pass

            drv_status = "ENGEDÉLYEZVE"
            if policy_disabled and search_disabled:
                drv_status = "Teljesen LETILTVA"
            elif policy_disabled:
                drv_status = "Házirend által LETILTVA"
            elif search_disabled:
                drv_status = "Eszközbeáll. LETILTVA"

            if service_disabled:
                result = {'status': 'Szolgáltatás LETILTVA (services.msc)', 'color': 'disabled'}
            elif paused_until:
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
            self._disable_wu_sync()
            
            self.emit('task_progress', {'task': 'disable_wu', 'log': 'Szolgáltatások leállítása és újraindítási jelzések (Pending Reboot) törlése...'})
            stop_svc = r"""
            Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
            Stop-Service bits -Force -ErrorAction SilentlyContinue
            Stop-Service cryptsvc -Force -ErrorAction SilentlyContinue
            Stop-Service UsoSvc -Force -ErrorAction SilentlyContinue
            """
            self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", stop_svc])
            time.sleep(2)
            self._run(['reg', 'delete', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired', '/f'])
            
            self.emit('task_progress', {'task': 'disable_wu', 'log': 'Beragadt frissítések és WU gyorsítótár (SoftwareDistribution) ürítése...'})
            sysroot = os.environ.get('SYSTEMROOT', r'C:\Windows')
            sw_dist = os.path.join(sysroot, 'SoftwareDistribution')
            deleted_cache = False
            for _ in range(4):
                try:
                    if os.path.exists(sw_dist):
                        shutil.rmtree(sw_dist, ignore_errors=False)
                        deleted_cache = True
                        break
                    else:
                        deleted_cache = True
                        break
                except Exception as e:
                    logging.warning(f"[WU_DISABLE] Cache törlés újrapróbálás: {e}")
                    time.sleep(3)
                    
            if not deleted_cache:
                self._run(["powershell", "-NoProfile", "-Command", f'Remove-Item -Path "{sw_dist}" -Recurse -Force -ErrorAction SilentlyContinue'])
            
            self.emit('task_progress', {'task': 'disable_wu', 'log': '✅ Gyorsítótár törölve.'})
            self._run('net start wuauserv', shell=True)
            self.emit('task_progress', {'task': 'disable_wu', 'log': '✅ WU szolgáltatás újraindítva'})
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

            # Szüneteltetés (Pause) feloldása a registry-ből
            self.emit('task_progress', {'task': 'enable_wu', 'log': 'Szüneteltetés (Pause) feloldása...'})
            ps_resume = """
            $regPath = 'HKLM:\\SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings'
            Remove-ItemProperty -Path $regPath -Name 'PauseUpdatesExpiryTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path $regPath -Name 'PauseFeatureUpdatesEndTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path $regPath -Name 'PauseQualityUpdatesEndTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path $regPath -Name 'PauseUpdatesStartTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path $regPath -Name 'PauseFeatureUpdatesStartTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path $regPath -Name 'PauseQualityUpdatesStartTime' -ErrorAction SilentlyContinue
            """
            self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_resume])
            self.emit('task_progress', {'task': 'enable_wu', 'log': '✅ Szüneteltetés törölve'})

            # Delete policy
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate", 0, winreg.KEY_WRITE) as key:
                    winreg.DeleteValue(key, "ExcludeWUDriversInQualityUpdate")
                logging.info("[WU_ENABLE] ExcludeWUDrivers policy törölve")
                self.emit('task_progress', {'task': 'enable_wu', 'log': '✅ ExcludeWUDrivers policy törölve'})
            except FileNotFoundError:
                logging.debug("[WU_ENABLE] Policy nem létezett")
                self.emit('task_progress', {'task': 'enable_wu', 'log': '  Policy nem létezett'})
            except Exception as e:
                logging.warning(f"[WU_ENABLE] Policy törlés hiba: {e}")
                self.emit('task_progress', {'task': 'enable_wu', 'log': f'⚠ {e}'})

            # SearchOrderConfig = 1
            try:
                with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching", 0, winreg.KEY_WRITE) as key:
                    winreg.SetValueEx(key, "SearchOrderConfig", 0, winreg.REG_DWORD, 1)
                logging.info("[WU_ENABLE] SearchOrderConfig = 1")
                self.emit('task_progress', {'task': 'enable_wu', 'log': '✅ SearchOrderConfig = 1'})
            except Exception as e:
                logging.warning(f"[WU_ENABLE] SearchOrderConfig hiba: {e}")
                self.emit('task_progress', {'task': 'enable_wu', 'log': f'⚠ {e}'})

            self._run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching',
                       '/v', 'SearchOrderConfig', '/t', 'REG_DWORD', '/d', '1', '/f'])

            # Stop services
            logging.info("[WU_ENABLE] Szolgáltatások leállítása...")
            for svc in ['wuauserv', 'bits', 'cryptsvc']:
                self._run(f'net stop {svc} /y', shell=True)
            time.sleep(2)

            # Delete SoftwareDistribution
            sysroot = os.environ.get('SYSTEMROOT', r'C:\Windows')
            sw_dist = os.path.join(sysroot, 'SoftwareDistribution')
            logging.info(f"[WU_ENABLE] SoftwareDistribution törlése: {sw_dist}")
            self.emit('task_progress', {'task': 'enable_wu', 'log': 'SoftwareDistribution törlése...'})
            for _ in range(3):
                try:
                    if os.path.exists(sw_dist):
                        shutil.rmtree(sw_dist, ignore_errors=False)
                        logging.info("[WU_ENABLE] SoftwareDistribution törölve")
                        self.emit('task_progress', {'task': 'enable_wu', 'log': '  ✅ Törölve'})
                        break
                except Exception as e:
                    logging.warning(f"[WU_ENABLE] SoftwareDistribution törlés újrapróbálás: {e}")
                    self.emit('task_progress', {'task': 'enable_wu', 'log': f'  ⚠ Újrapróbálás: {e}'})
                    time.sleep(3)

            # Rename catroot2
            catroot2 = os.path.join(sysroot, 'System32', 'catroot2')
            bak = catroot2 + '.bak'
            try:
                if os.path.exists(bak):
                    shutil.rmtree(bak, ignore_errors=True)
                if os.path.exists(catroot2):
                    os.rename(catroot2, bak)
                    logging.info("[WU_ENABLE] catroot2 átnevezve")
                    self.emit('task_progress', {'task': 'enable_wu', 'log': '✅ catroot2 átnevezve'})
            except Exception as e:
                logging.warning(f"[WU_ENABLE] catroot2 hiba: {e}")
                self.emit('task_progress', {'task': 'enable_wu', 'log': f'⚠ catroot2: {e}'})

            # Re-register DLLs
            logging.info("[WU_ENABLE] WU DLL-ek újraregisztrálása...")
            sys32 = os.path.join(sysroot, 'System32')
            for dll in ['wuaueng.dll', 'wuapi.dll', 'wups.dll', 'wups2.dll', 'wuwebv.dll', 'wucltux.dll']:
                fp = os.path.join(sys32, dll)
                if os.path.exists(fp):
                    self._run(f'regsvr32.exe /s "{fp}"', shell=True)
            self.emit('task_progress', {'task': 'enable_wu', 'log': '✅ WU DLL-ek újraregisztrálva'})

            # Winsock reset
            logging.info("[WU_ENABLE] Winsock reset...")
            self._run('netsh winsock reset', shell=True)

            # Start services
            logging.info("[WU_ENABLE] Szolgáltatások indítása...")
            for svc in ['cryptsvc', 'bits', 'wuauserv']:
                for _ in range(3):
                    res = self._run(f'net start {svc}', shell=True)
                    if res.returncode == 0 or 'already' in (res.stdout + res.stderr).lower():
                        break
                    time.sleep(3)

            self._run('wuauclt.exe /resetauthorization /detectnow', shell=True)
            self._run('UsoClient.exe StartScan', shell=True)
            logging.info("[WU_ENABLE] Kész!")
            self.emit('task_progress', {'task': 'enable_wu', 'log': '✅ Frissítés-keresés elindítva'})
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

            logging.info("[WU_RESTART] Szolgáltatások leállítása...")
            for svc in ['wuauserv', 'bits', 'cryptsvc', 'msiserver']:
                self._run(f'net stop {svc} /y', shell=True)
                self.emit('task_progress', {'task': 'restart_wu', 'log': f'  stop {svc}'})
            time.sleep(2)
            logging.info("[WU_RESTART] Szolgáltatások indítása...")
            for svc in ['rpcss', 'cryptsvc', 'bits', 'msiserver', 'wuauserv']:
                for _ in range(3):
                    res = self._run(f'net start {svc}', shell=True)
                    if res.returncode == 0 or 'already' in (res.stdout + res.stderr).lower():
                        break
                    time.sleep(3)
                self.emit('task_progress', {'task': 'restart_wu', 'log': f'  start {svc}'})
            self._run('wuauclt.exe /resetauthorization /detectnow', shell=True)
            self._run('UsoClient.exe StartScan', shell=True)
            logging.info("[WU_RESTART] Kész!")
            self.emit('task_progress', {'task': 'restart_wu', 'log': '✅ Frissítés-keresés elindítva'})
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
            
            ps = """
            $regPath = 'HKLM:\\SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings'
            if (!(Test-Path $regPath)) { New-Item -Path $regPath -Force | Out-Null }
            
            $daysToAdd = """ + str(days) + """
            $now = (Get-Date).ToUniversalTime()
            
            $currentPauseStr = (Get-ItemProperty -Path $regPath -Name 'PauseUpdatesExpiryTime' -ErrorAction SilentlyContinue).PauseUpdatesExpiryTime
            
            if ($currentPauseStr -and $daysToAdd -eq 7) {
                try {
                    $currentPause = [datetime]$currentPauseStr
                    if ($currentPause -lt $now) { $currentPause = $now }
                } catch {
                    $currentPause = $now
                }
                $newDate = $currentPause.AddDays($daysToAdd)
            } else {
                $newDate = $now.AddDays($daysToAdd)
            }
            
            $dateStr = $newDate.ToString("yyyy-MM-ddTHH:mm:ssZ")
            $startStr = $now.ToString("yyyy-MM-ddTHH:mm:ssZ")
            
            Set-ItemProperty -Path $regPath -Name 'PauseUpdatesExpiryTime' -Value $dateStr -Type String -Force | Out-Null
            Set-ItemProperty -Path $regPath -Name 'PauseFeatureUpdatesEndTime' -Value $dateStr -Type String -Force | Out-Null
            Set-ItemProperty -Path $regPath -Name 'PauseQualityUpdatesEndTime' -Value $dateStr -Type String -Force | Out-Null
            Set-ItemProperty -Path $regPath -Name 'PauseUpdatesStartTime' -Value $startStr -Type String -Force | Out-Null
            Set-ItemProperty -Path $regPath -Name 'PauseFeatureUpdatesStartTime' -Value $startStr -Type String -Force | Out-Null
            Set-ItemProperty -Path $regPath -Name 'PauseQualityUpdatesStartTime' -Value $startStr -Type String -Force | Out-Null
            
            Write-Output $dateStr
            """
            res = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], encoding='utf-8')
            new_date = res.stdout.strip()
            
            self.emit('task_progress', {'task': 'pause_wu', 'log': 'Szolgáltatások leállítása és újraindítási jelzések törlése...'})
            stop_svc = r"""
            Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
            Stop-Service bits -Force -ErrorAction SilentlyContinue
            Stop-Service cryptsvc -Force -ErrorAction SilentlyContinue
            Stop-Service UsoSvc -Force -ErrorAction SilentlyContinue
            """
            self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", stop_svc])
            time.sleep(2)
            self._run(['reg', 'delete', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired', '/f'])
            
            self.emit('task_progress', {'task': 'pause_wu', 'log': 'Beragadt frissítések és WU gyorsítótár ürítése...'})
            sysroot = os.environ.get('SYSTEMROOT', r'C:\Windows')
            sw_dist = os.path.join(sysroot, 'SoftwareDistribution')
            for _ in range(4):
                try:
                    if os.path.exists(sw_dist):
                        shutil.rmtree(sw_dist, ignore_errors=False)
                        break
                    else:
                        break
                except Exception:
                    time.sleep(3)
            self._run(["powershell", "-NoProfile", "-Command", f'Remove-Item -Path "{sw_dist}" -Recurse -Force -ErrorAction SilentlyContinue'])
            self._run('net start wuauserv', shell=True)
            
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
            
            ps = """
            $regPath = 'HKLM:\\SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings'
            Remove-ItemProperty -Path $regPath -Name 'PauseUpdatesExpiryTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path $regPath -Name 'PauseFeatureUpdatesEndTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path $regPath -Name 'PauseQualityUpdatesEndTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path $regPath -Name 'PauseUpdatesStartTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path $regPath -Name 'PauseFeatureUpdatesStartTime' -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path $regPath -Name 'PauseQualityUpdatesStartTime' -ErrorAction SilentlyContinue
            
            Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
            Start-Service wuauserv -ErrorAction SilentlyContinue
            
            try { (New-Object -ComObject Microsoft.Update.AutoUpdate).Resume() | Out-Null } catch {}
            """
            self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps])
            
            self.emit('task_progress', {'task': 'resume_wu', 'log': '✅ Szüneteltetés feloldva!'})
            self.emit('task_complete', {'task': 'resume_wu', 'status': '✅ Szüneteltetés feloldva!'})
            
        self._safe_thread('resume_wu', worker)
