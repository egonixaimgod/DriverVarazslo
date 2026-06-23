import re
import os

with open('driver_tool.py', 'r', encoding='utf-8') as f:
    text = f.read()

# 1. Powercfg hiba javítása (SetThreadExecutionState a powercfg parancsok helyett)
old_powercfg = r'''    def _disable_sleep_sync(self, task_id='autofix'):
        self.emit('task_progress', {'task': task_id, 'log': 'Alvó mód és képernyő kikapcsolás letiltása (hogy ne szakadjon meg a folyamat)...'})
        power_cmds = [
            ['powercfg', '/change', 'monitor-timeout-ac', '0'],
            ['powercfg', '/change', 'monitor-timeout-dc', '0'],
            ['powercfg', '/change', 'standby-timeout-ac', '0'],
            ['powercfg', '/change', 'standby-timeout-dc', '0'],
            ['powercfg', '/change', 'hibernate-timeout-ac', '0'],
            ['powercfg', '/change', 'hibernate-timeout-dc', '0']
        ]
        for cmd in power_cmds:
            self._run(cmd)
        self.emit('task_progress', {'task': task_id, 'log': '✅ Energiagazdálkodás beállítva.\n'})'''

new_powercfg = r'''    def _disable_sleep_sync(self, task_id='autofix'):
        self.emit('task_progress', {'task': task_id, 'log': 'Alvó mód ideiglenes blokkolása a folyamat végéig (Windows API)...'})
        try:
            # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001 | 0x00000002)
            self.emit('task_progress', {'task': task_id, 'log': '✅ Energiagazdálkodás felülbírálva.\n'})
        except Exception as e:
            self.emit('task_progress', {'task': task_id, 'log': f'⚠️ Alvás tiltása sikertelen: {e}\n'})'''
text = text.replace(old_powercfg, new_powercfg)

# 2. PowerShell letöltés Timeout -> Progress alapúra (2 helyen van)
old_ps_dl_1 = r'''        $Job = $DL.BeginDownload($null, $null, $null)
        $timeout = 300; $elapsed = 0
        while (-not $Job.IsCompleted -and $elapsed -lt $timeout) { Start-Sleep -Seconds 1; $elapsed++ }
        if (-not $Job.IsCompleted) { try { $DL.EndDownload($Job) | Out-Null } catch {}; Write-Output "FAIL: [LETÖLTÉS IDŐTÚLLÉPÉS] $t"; $f++; continue }'''

new_ps_dl_1 = r'''        $Job = $DL.BeginDownload($null, $null, $null)
        $noProgressSec = 0; $lastPct = -1
        while (-not $Job.IsCompleted) { 
            Start-Sleep -Seconds 1
            try { $pct = $DL.GetProgress().PercentComplete } catch { $pct = 0 }
            if ($pct -ne $lastPct) { $lastPct = $pct; $noProgressSec = 0 } else { $noProgressSec++ }
            if ($noProgressSec -gt 180) { break } 
        }
        if (-not $Job.IsCompleted) { try { $DL.EndDownload($Job) | Out-Null } catch {}; Write-Output "FAIL: [LETÖLTÉS FAGYÁS 3p] $t"; $f++; continue }'''
text = text.replace(old_ps_dl_1, new_ps_dl_1)

old_ps_dl_2 = r'''        $Job = $DL.BeginDownload($null, $null, $null)
        $timeout = 300; $elapsed = 0
        while (-not $Job.IsCompleted -and $elapsed -lt $timeout) { Start-Sleep -Seconds 1; $elapsed++ }
        if (-not $Job.IsCompleted) { try { $DL.EndDownload($Job) | Out-Null } catch {}; Write-Output "FAIL: $($U.Title)"; $f++; continue }'''

new_ps_dl_2 = r'''        $Job = $DL.BeginDownload($null, $null, $null)
        $noProgressSec = 0; $lastPct = -1
        while (-not $Job.IsCompleted) { 
            Start-Sleep -Seconds 1
            try { $pct = $DL.GetProgress().PercentComplete } catch { $pct = 0 }
            if ($pct -ne $lastPct) { $lastPct = $pct; $noProgressSec = 0 } else { $noProgressSec++ }
            if ($noProgressSec -gt 180) { break } 
        }
        if (-not $Job.IsCompleted) { try { $DL.EndDownload($Job) | Out-Null } catch {}; Write-Output "FAIL: $($U.Title) [FAGYÁS]"; $f++; continue }'''
text = text.replace(old_ps_dl_2, new_ps_dl_2)

# 3. Takeown /d y -> /A csere a mappáknál
text = text.replace('takeown /f "{d}" /r /d y', 'takeown /f "{d}" /r /A')

# 4. RunOnce TEMP könyvtár biztonsági mentés (Autofix)
# Van egy a 0. fázisnál és egy a 4. fázisnál. A Python logikát cserélem a _run(['reg', 'add' előtt
runonce_search = r'''                    exe_path = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
                    if getattr(sys, 'frozen', False):
                        cmd_str = f'"{exe_path}" --resume-autofix'
                    else:
                        cmd_str = f'"{sys.executable}" "{exe_path}" --resume-autofix'
                    self._run(['reg', 'add', r'HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce', '/v', 'DriverVarázslóResume', '/t', 'REG_SZ', '/d', cmd_str, '/f'])'''

runonce_replace = r'''                    exe_path = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
                    temp_env = os.environ.get('TEMP', '!!').lower()
                    # Biztonsági másolat, ha temp könyvtárból fut a program
                    if temp_env in exe_path.lower():
                        try:
                            public_dir = os.environ.get('PUBLIC', 'C:\\Users\\Public')
                            safe_exe = os.path.join(public_dir, "DriverVarazslo_Resume.exe" if getattr(sys, 'frozen', False) else "DriverVarazslo_Resume.py")
                            shutil.copy2(exe_path, safe_exe)
                            exe_path = safe_exe
                            self.emit('task_progress', {'task': 'autofix', 'log': 'ℹ️ Temp mappából futás detektálva. Biztonsági másolat készítve a Public mappába.'})
                        except Exception as e:
                            logging.error(f"[AUTOFIX] Biztonsági másolat hiba: {e}")
                    
                    if getattr(sys, 'frozen', False):
                        cmd_str = f'"{exe_path}" --resume-autofix'
                    else:
                        cmd_str = f'"{sys.executable}" "{exe_path}" --resume-autofix'
                    self._run(['reg', 'add', r'HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce', '/v', 'DriverVarázslóResume', '/t', 'REG_SZ', '/d', cmd_str, '/f'])'''
text = text.replace(runonce_search, runonce_replace)

# 5. MAX_PATH rövid ideiglenes könyvtár (WIM kinyerés és Catalog letöltés)
text = text.replace("os.environ.get('TEMP', 'C:\\\\Temp')", "os.environ.get('SystemDrive', 'C:') + '\\\\DV_Temp'")

# 6. HWID Contains -> StartsWith
old_hwid_ps = r'''                foreach ($sys_hid in $systemHWIDs) {
                    if ($sys_hid.Contains($hUpper) -or $hUpper.Contains($sys_hid)) {
                        $matchFound = $true; break
                    }
                }'''
new_hwid_ps = r'''                foreach ($sys_hid in $systemHWIDs) {
                    if ($sys_hid.StartsWith($hUpper) -or $hUpper.StartsWith($sys_hid)) {
                        $matchFound = $true; break
                    }
                }'''
text = text.replace(old_hwid_ps, new_hwid_ps)

old_hwid_py = r'''                            for wu_h in hwids_upper:
                                for dev_h in dev_hwids_upper:
                                    if dev_h in wu_h or wu_h in dev_h:
                                        match = True
                                        break
                                if match or (wu_h in dev_pnp_upper):
                                    match = True
                                    break'''
new_hwid_py = r'''                            for wu_h in hwids_upper:
                                for dev_h in dev_hwids_upper:
                                    if wu_h.startswith(dev_h) or dev_h.startswith(wu_h):
                                        match = True
                                        break
                                if match or dev_pnp_upper.startswith(wu_h) or wu_h.startswith(dev_pnp_upper):
                                    match = True
                                    break'''
text = text.replace(old_hwid_py, new_hwid_py)

with open('driver_tool.py', 'w', encoding='utf-8') as f:
    f.write(text)

print("Kész!")
