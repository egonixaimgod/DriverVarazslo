"""DriverVarázsló CLI - CLI: Windows Update driver-tiltás/engedélyezés/szüneteltetés."""

# === AUTO-IMPORTS ===
import os
import time
import shutil
import winreg
from datetime import datetime, timezone
# === /AUTO-IMPORTS ===


class CliWuMixin:
    """CLI: Windows Update driver-tiltás/engedélyezés/szüneteltetés. A CliApi része (összerakás: app/cli/api.py)."""

    # ================================================================
    # WINDOWS UPDATE
    # ================================================================
    def check_wu_status_cli(self):
        """WU driver frissítés állapota."""
        policy_disabled = False
        search_disabled = False
        
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate", 0, winreg.KEY_READ) as key:
                val, _ = winreg.QueryValueEx(key, "ExcludeWUDriversInQualityUpdate")
                if val == 1:
                    policy_disabled = True
        except (FileNotFoundError, OSError):
            pass
        
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching", 0, winreg.KEY_READ) as key:
                val, _ = winreg.QueryValueEx(key, "SearchOrderConfig")
                if val == 0:
                    search_disabled = True
        except (FileNotFoundError, OSError):
            pass
        
        drv_status = "✅ ENGEDÉLYEZVE"
        if policy_disabled and search_disabled:
            drv_status = "⛔ LETILTVA (policy + eszközbeállítások)"
        elif policy_disabled:
            drv_status = "⛔ LETILTVA (policy)"
        elif search_disabled:
            drv_status = "⛔ LETILTVA (eszközbeállítások)"
            
        paused_until = None
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\WindowsUpdate\UX\Settings", 0, winreg.KEY_READ) as key:
                val, _ = winreg.QueryValueEx(key, "PauseUpdatesExpiryTime")
                if val:
                    dt = datetime.strptime(val, "%Y-%m-%dT%H:%M:%SZ")
                    if dt > datetime.now(timezone.utc).replace(tzinfo=None):
                        paused_until = val.split('T')[0] if 'T' in val else val
        except Exception:
            pass
            
        if paused_until:
            return f"SZÜNETELTETVE ({paused_until}) | Driverek: {drv_status}"
        return drv_status
    
    def disable_wu_drivers(self):
        """WU driver frissítések letiltása."""
        if self.target_os_path:
            print("\n❌ Hiba: A Windows Update beállítások csak Élő rendszeren módosíthatók!")
            return
            
        print("\n⛔ WU driver frissítések letiltása...")
        print("-" * 50)
        
        try:
            with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching", 0, winreg.KEY_WRITE) as key:
                winreg.SetValueEx(key, "SearchOrderConfig", 0, winreg.REG_DWORD, 0)
            print("  ✅ SearchOrderConfig = 0")
        except Exception as e:
            print(f"  ⚠️  {e}")
        
        self._run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching',
                   '/v', 'SearchOrderConfig', '/t', 'REG_DWORD', '/d', '0', '/f'])
        
        self._run(['reg', 'add', r'HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate',
                   '/v', 'ExcludeWUDriversInQualityUpdate', '/t', 'REG_DWORD', '/d', '1', '/f'])
        print("  ✅ ExcludeWUDriversInQualityUpdate = 1")
        
        print("  🗑️  Beragadt frissítések törlése (SoftwareDistribution)...")
        clear_cache = r"""
        Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
        Stop-Service bits -Force -ErrorAction SilentlyContinue
        Stop-Service cryptsvc -Force -ErrorAction SilentlyContinue
        Remove-Item -Path "$env:windir\SoftwareDistribution" -Recurse -Force -ErrorAction SilentlyContinue
        """
        self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", clear_cache])

        print("  🔄 WU szolgáltatás újraindítása...")
        self._run('net start wuauserv', shell=True)
        
        print("-" * 50)
        print("✅ WU driver letiltás kész (Cache ürítve)!")
    
    def enable_wu_drivers(self):
        """WU driver frissítések engedélyezése + teljes reset."""
        if self.target_os_path:
            print("\n❌ Hiba: A Windows Update beállítások csak Élő rendszeren módosíthatók!")
            return
            
        print("\n✅ WU driver frissítések engedélyezése + reset...")
        print("-" * 50)
        
        # Szüneteltetés (Pause) feloldása a registry-ből
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
        print("  ✅ Szüneteltetés feloldva")

        # Policy törlés
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate", 0, winreg.KEY_WRITE) as key:
                winreg.DeleteValue(key, "ExcludeWUDriversInQualityUpdate")
            print("  ✅ Policy törölve")
        except FileNotFoundError:
            print("  ℹ️  Policy nem létezett")
        except Exception as e:
            print(f"  ⚠️  {e}")
        
        # SearchOrderConfig = 1
        try:
            with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching", 0, winreg.KEY_WRITE) as key:
                winreg.SetValueEx(key, "SearchOrderConfig", 0, winreg.REG_DWORD, 1)
            print("  ✅ SearchOrderConfig = 1")
        except Exception as e:
            print(f"  ⚠️  {e}")
        
        # Szolgáltatások
        print("  🔄 WU szolgáltatások újraindítása...")
        for svc in ['wuauserv', 'bits', 'cryptsvc']:
            self._run(f'net stop {svc} /y', shell=True)
        time.sleep(2)
        
        # SoftwareDistribution törlés
        sysroot = os.environ.get('SYSTEMROOT', r'C:\Windows')
        sw_dist = os.path.join(sysroot, 'SoftwareDistribution')
        if os.path.exists(sw_dist):
            print("  🗑️  SoftwareDistribution törlése...")
            shutil.rmtree(sw_dist, ignore_errors=True)

        # Rename catroot2 (a GUI verzióval egyező mély reset - enélkül a WU komponensraktár
        # korrupciója gyakran nem javul, csak a friss frissítés-cache törlésétől)
        catroot2 = os.path.join(sysroot, 'System32', 'catroot2')
        bak = catroot2 + '.bak'
        try:
            if os.path.exists(bak):
                shutil.rmtree(bak, ignore_errors=True)
            if os.path.exists(catroot2):
                os.rename(catroot2, bak)
                print("  ✅ catroot2 átnevezve")
        except Exception as e:
            print(f"  ⚠️  catroot2: {e}")

        # WU DLL-ek újraregisztrálása
        sys32 = os.path.join(sysroot, 'System32')
        for dll in ['wuaueng.dll', 'wuapi.dll', 'wups.dll', 'wups2.dll', 'wuwebv.dll', 'wucltux.dll']:
            fp = os.path.join(sys32, dll)
            if os.path.exists(fp):
                self._run(f'regsvr32.exe /s "{fp}"', shell=True)
        print("  ✅ WU DLL-ek újraregisztrálva")

        # Winsock reset
        self._run('netsh winsock reset', shell=True)

        for svc in ['cryptsvc', 'bits', 'wuauserv']:
            self._run(f'net start {svc}', shell=True)

        self._run('wuauclt.exe /resetauthorization /detectnow', shell=True)
        self._run('UsoClient.exe StartScan', shell=True)

        print("-" * 50)
        print("✅ WU engedélyezés + reset kész!")
    
    def restart_wu_services(self):
        """WU szolgáltatások újraindítása."""
        if self.target_os_path:
            print("\n❌ Hiba: A Windows Update beállítások csak Élő rendszeren módosíthatók!")
            return
            
        print("\n🔄 WU szolgáltatások újraindítása...")
        print("-" * 50)
        
        for svc in ['wuauserv', 'bits', 'cryptsvc', 'msiserver']:
            print(f"  stop {svc}...", end=" ", flush=True)
            self._run(f'net stop {svc} /y', shell=True)
            print("✅")
        
        time.sleep(2)
        
        for svc in ['rpcss', 'cryptsvc', 'bits', 'msiserver', 'wuauserv']:
            print(f"  start {svc}...", end=" ", flush=True)
            self._run(f'net start {svc}', shell=True)
            print("✅")
        
        self._run('wuauclt.exe /resetauthorization /detectnow', shell=True)
        self._run('UsoClient.exe StartScan', shell=True)

        print("-" * 50)
        print("✅ WU szolgáltatások újraindítva!")

    def pause_wu(self, days):
        """Windows Update szüneteltetése N napra (a GUI verzió CLI megfelelője)."""
        if self.target_os_path:
            print("\n❌ Hiba: Offline módban nem elérhető!")
            return

        print(f"\n⏸️  WU szüneteltetése ({days} nap)...")
        print("-" * 50)

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

        print("  Szolgáltatások leállítása és újraindítási jelzések törlése...")
        stop_svc = r"""
        Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
        Stop-Service bits -Force -ErrorAction SilentlyContinue
        Stop-Service cryptsvc -Force -ErrorAction SilentlyContinue
        Stop-Service UsoSvc -Force -ErrorAction SilentlyContinue
        """
        self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", stop_svc])
        time.sleep(2)
        self._run(['reg', 'delete', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired', '/f'])

        print("  Beragadt frissítések és WU gyorsítótár ürítése...")
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

        print("-" * 50)
        print(f"✅ Frissítések szüneteltetve idáig: {new_date}")

    def resume_wu(self):
        """Windows Update szüneteltetésének feloldása (a GUI verzió CLI megfelelője)."""
        if self.target_os_path:
            print("\n❌ Hiba: Offline módban nem elérhető!")
            return

        print("\n▶️  WU szüneteltetés feloldása...")
        print("-" * 50)

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

        print("-" * 50)
        print("✅ Szüneteltetés feloldva!")
