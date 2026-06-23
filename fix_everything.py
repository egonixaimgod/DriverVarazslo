import os

with open('driver_tool.py', 'r', encoding='utf-8') as f:
    text = f.read()

# 1. Hozzáadás: pause_wu, resume_wu
py_add = r'''
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
            
            Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
            Start-Service wuauserv -ErrorAction SilentlyContinue
            
            Write-Output $dateStr
            """
            res = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], encoding='utf-8')
            
            new_date = res.stdout.strip()
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

    # ================================================================
    # BACKUP / RESTORE
    # ================================================================'''

if "def pause_wu" not in text:
    target = "    # ================================================================\n    # BACKUP / RESTORE\n    # ================================================================"
    text = text.replace(target, py_add)

# 2. check_wu_status módosítások
old_st = '''    def check_wu_status(self):
        logging.info("[API] check_wu_status()")
        if self.target_os_path:
            return {'status': 'Offline (Nem olvasható)', 'color': 'unknown'}
        try:
            policy_disabled = False
            search_disabled = False
            service_disabled = False
            try:'''

new_st = '''    def check_wu_status(self):
        logging.info("[API] check_wu_status()")
        if self.target_os_path:
            return {'status': 'Offline (Nem olvasható)', 'color': 'unknown'}
        try:
            policy_disabled = False
            search_disabled = False
            service_disabled = False
            paused_until = None
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings", 0, winreg.KEY_READ) as key:
                    val, _ = winreg.QueryValueEx(key, "PauseUpdatesExpiryTime")
                    if val:
                        paused_until = val.replace("T", " ").replace("Z", " (UTC)")
                        logging.debug(f"[WU_STATUS] PauseUpdatesExpiryTime = {val}")
            except Exception:
                pass
            try:'''

text = text.replace(old_st, new_st)

old_eval = '''            if service_disabled:
                result = {'status': 'Szolgáltatás LETILTVA (services.msc)', 'color': 'disabled'}
            elif policy_disabled and search_disabled:
                result = {'status': 'Teljesen LETILTVA', 'color': 'disabled'}
            elif policy_disabled:
                result = {'status': 'Házirend által LETILTVA', 'color': 'disabled'}
            elif search_disabled:
                result = {'status': 'Eszközbeállításokban LETILTVA', 'color': 'disabled'}
            else:
                result = {'status': 'Driver frissítés ENGEDÉLYEZVE', 'color': 'enabled'}'''

new_eval = '''            if service_disabled:
                result = {'status': 'Szolgáltatás LETILTVA (services.msc)', 'color': 'disabled'}
            elif paused_until:
                result = {'status': f'SZÜNETELTETVE idáig: {paused_until}', 'color': 'warning'}
            elif policy_disabled and search_disabled:
                result = {'status': 'Teljesen LETILTVA', 'color': 'disabled'}
            elif policy_disabled:
                result = {'status': 'Házirend által LETILTVA', 'color': 'disabled'}
            elif search_disabled:
                result = {'status': 'Eszközbeállításokban LETILTVA', 'color': 'disabled'}
            else:
                result = {'status': 'Driver frissítés ENGEDÉLYEZVE', 'color': 'enabled'}'''

text = text.replace(old_eval, new_eval)

with open('driver_tool.py', 'w', encoding='utf-8') as f:
    f.write(text)

with open('verify_check.txt', 'w', encoding='utf-8') as f:
    if "def pause_wu" in text:
        f.write("A pause_wu SIKERESEN HOZZÁADVA.\n")
    else:
        f.write("HIBA: A pause_wu nem került hozzáadásra.\n")
        
    if "paused_until = val.replace(" in text:
        f.write("A check_wu_status SIKERESEN MÓDOSÍTVA.\n")
    else:
        f.write("HIBA: A check_wu_status nem módosult.\n")
