import sys
import re

with open('driver_tool.py', 'r', encoding='utf-8') as f:
    text = f.read()

# Remove resume_step1 from __init__
text = text.replace("self.resume_step1 = '--resume-autofix-step1' in sys.argv\n", "")
text = text.replace("'resume_step1': getattr(self, 'resume_step1', False)", "")

# Add _disable_sleep_sync
sleep_sync = """    def _disable_sleep_sync(self, task_id='autofix'):
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
        self.emit('task_progress', {'task': task_id, 'log': '✅ Energiagazdálkodás beállítva.\\n'})

    def _disable_wu_sync(self, task_id='autofix'):"""
text = text.replace("    def _disable_wu_sync(self, task_id='autofix'):", sleep_sync)

# Completely replace run_autofix worker body
old_autofix_start = "            task_title = '1 Katt. Fix (RESTART UTÁNI LÁNC FOLYTATÁSA!)' if (getattr(self, 'resume_mode', False) or getattr(self, 'resume_step1', False)) else '1 Kattintásos Driver Javítás és Frissítés'"
old_autofix_end = "# 4. Átmenetileg engedélyezzük a WU-t és unpause a driverkereséshez"

new_autofix = """            task_title = '1 Katt. Fix (RESTART UTÁNI LÁNC FOLYTATÁSA!)' if getattr(self, 'resume_mode', False) else '1 Kattintásos Driver Javítás és Frissítés'
            self.emit('task_start', {'task': 'autofix', 'title': task_title})
            try:
                if not getattr(self, 'resume_mode', False):
                    self.emit('task_progress', {'task': 'autofix', 'log': '0. LÉPÉS: Rendszer előkészítése és régi driverek törlése...'})
                    
                    self._disable_sleep_sync()
                    
                    self._disable_wu_sync()
                    if getattr(self, '_cancel_flag', False): raise Exception("Magyar_Megszakit_Flag")
                    
                    self._create_restore_point_sync()
                    if getattr(self, '_cancel_flag', False): raise Exception("Magyar_Megszakit_Flag")

                    self._delete_ghost_devices_sync()
                    if getattr(self, '_cancel_flag', False): raise Exception("Magyar_Megszakit_Flag")

                    self._delete_third_party_sync()
                    if getattr(self, '_cancel_flag', False): raise Exception("Magyar_Megszakit_Flag")
                    
                    self.emit('task_progress', {'task': 'autofix', 'log': 'Beragadt frissítések törlése és WU szüneteltetése 1 hétre...'})
                    clear_cache = r\"\"\"
                    Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
                    Stop-Service bits -Force -ErrorAction SilentlyContinue
                    Stop-Service cryptsvc -Force -ErrorAction SilentlyContinue
                    Remove-Item -Path "$env:windir\\SoftwareDistribution" -Recurse -Force -ErrorAction SilentlyContinue
                    \"\"\"
                    self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", clear_cache])
                    
                    ps_pause = r\"\"\"
                    $pauseDate = (Get-Date).AddDays(7).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
                    $nowDate = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
                    Set-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings' -Name 'PauseUpdatesExpiryTime' -Value $pauseDate -Type String -Force | Out-Null
                    Set-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings' -Name 'PauseFeatureUpdatesEndTime' -Value $pauseDate -Type String -Force | Out-Null
                    Set-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings' -Name 'PauseQualityUpdatesEndTime' -Value $pauseDate -Type String -Force | Out-Null
                    Set-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings' -Name 'PauseUpdatesStartTime' -Value $nowDate -Type String -Force | Out-Null
                    Set-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings' -Name 'PauseFeatureUpdatesStartTime' -Value $nowDate -Type String -Force | Out-Null
                    Set-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings' -Name 'PauseQualityUpdatesStartTime' -Value $nowDate -Type String -Force | Out-Null
                    \"\"\"
                    self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_pause])
                    self.emit('task_progress', {'task': 'autofix', 'log': '✅ WU gyorsítótár ürítve és szüneteltetve 1 hétre.\\n'})
                    
                    self.emit('task_progress', {'task': 'autofix', 'log': '🔄 A számítógép újraindul, majd a folyamat automatikusan a TELEPÍTÉSSEL folytatódik!'})
                    
                    exe_path = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
                    if getattr(sys, 'frozen', False):
                        cmd_str = f'"{exe_path}" --resume-autofix'
                    else:
                        cmd_str = f'"{sys.executable}" "{exe_path}" --resume-autofix'
                    self._run(['reg', 'add', r'HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\RunOnce', '/v', 'DriverVarázslóResume', '/t', 'REG_SZ', '/d', cmd_str, '/f'])
                    
                    self.emit('task_complete', {'task': 'autofix', 'status': 'Újraindulás felkészítve...'})
                    time.sleep(5)
                    self._run(['shutdown', '/r', '/t', '0', '/f'])
                    return
                else:
                    self._run(['reg', 'delete', r'HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\RunOnce', '/v', 'DriverVarázslóResume', '/f'])
                    self.emit('task_progress', {'task': 'autofix', 'log': 'Láncolt folytatás gépújraindítás után. Régi driverek törlése kihagyva, hogy ne töröljünk friss drivereket.\\n'})
                    self._disable_sleep_sync()

                # 4. Átmenetileg engedélyezzük a WU-t és unpause a driverkereséshez"""

start_pos = text.find(old_autofix_start)
end_pos = text.find(old_autofix_end)

if start_pos != -1 and end_pos != -1:
    text = text[:start_pos] + new_autofix + text[end_pos + len(old_autofix_end):]

with open('driver_tool.py', 'w', encoding='utf-8') as f:
    f.write(text)
