import re

# ======================== UI.HTML JAVÍTÁS ========================
with open('ui.html', 'r', encoding='utf-8') as f:
    text = f.read()

new_actions = r'''<div class="wu-actions">
            <button class="btn btn-danger" onclick="disableWu()">⛔ WU Driver Letiltás</button>
            <button class="btn btn-success" onclick="enableWu()">✅ WU Driver Engedélyezés + Reset</button>
            <button class="btn btn-warning" onclick="restartWu()">⚡ WU Szolgáltatások Újraindítása</button>
          </div>
        </div>
        
        <div class="panel glass" style="margin-top: 16px;">
          <div class="panel-title"><span class="emoji">⏸️</span> Windows Update Frissítések Szüneteltetése</div>
          <p style="margin-bottom: 16px; color: var(--text3);">Ezekkel a gombokkal a teljes Windows Update működését (a biztonsági frissítéseket is) elhalaszthatod a jövőbe. Az 1 hetet többször is megnyomhatod a hetek halmozásához!</p>
          <div class="wu-actions">
            <button class="btn btn-primary" onclick="pauseWu(7)">⏳ +1 Hét Szünet</button>
            <button class="btn btn-danger" onclick="pauseWu(3650)">🛑 10 Év Szünet (Végleges)</button>
            <button class="btn btn-success" onclick="resumeWu()">▶️ Szünetelés Feloldása</button>
          </div>'''

# Óvatos regex csere, ami garantáltan megtalálja a panelt
if 'pauseWu' not in text:
    text = re.sub(r'<div class="wu-actions">\s*<button class="btn btn-danger" onclick="disableWu\(\)">⛔ WU Driver Letiltás</button>.*?</div>', new_actions, text, count=1, flags=re.DOTALL)

# JS csere:
js_add = '''
async function pauseWu(days) {
  try {
    await api().pause_wu(days);
    setTimeout(checkWuStatus, 1500); 
  } catch (e) {
    toast('Szüneteltetés hiba: ' + e, 'error');
  }
}

async function resumeWu() {
  try {
    await api().resume_wu();
    setTimeout(checkWuStatus, 1500);
  } catch (e) {
    toast('Feloldás hiba: ' + e, 'error');
  }
}
'''
if 'pauseWu' not in text:
    text = text.replace('async function restartWu() {', js_add + '\nasync function restartWu() {')

with open('ui.html', 'w', encoding='utf-8') as f:
    f.write(text)

# ======================== DRIVER_TOOL.PY JAVÍTÁS ========================
with open('driver_tool.py', 'r', encoding='utf-8') as f:
    pytext = f.read()

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
            $regPath = 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings'
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
            $regPath = 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings'
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

'''

if 'def pause_wu' not in pytext:
    pytext = pytext.replace('    # ================================================================\n    # BACKUP / RESTORE', py_add + '\n    # ================================================================\n    # BACKUP / RESTORE')

# wu_status csere:
wu_st_old = '''    def check_wu_status(self):
        logging.info("[API] check_wu_status()")
        if self.target_os_path:
            return {'status': 'Offline (Nem olvasható)', 'color': 'unknown'}
        try:
            policy_disabled = False
            search_disabled = False
            service_disabled = False
            try:'''

wu_st_new = '''    def check_wu_status(self):
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
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\WindowsUpdate\UX\Settings", 0, winreg.KEY_READ) as key:
                    val, _ = winreg.QueryValueEx(key, "PauseUpdatesExpiryTime")
                    if val:
                        paused_until = val.replace("T", " ").replace("Z", " (UTC)")
            except Exception:
                pass
            try:'''
pytext = pytext.replace(wu_st_old, wu_st_new)

wu_eval_old = '''            if service_disabled:
                result = {'status': 'Szolgáltatás LETILTVA (services.msc)', 'color': 'disabled'}
            elif policy_disabled and search_disabled:'''

wu_eval_new = '''            if service_disabled:
                result = {'status': 'Szolgáltatás LETILTVA (services.msc)', 'color': 'disabled'}
            elif paused_until:
                result = {'status': f'SZÜNETELTETVE idáig: {paused_until}', 'color': 'warning'}
            elif policy_disabled and search_disabled:'''
pytext = pytext.replace(wu_eval_old, wu_eval_new)

with open('driver_tool.py', 'w', encoding='utf-8') as f:
    f.write(pytext)

print("Kész az apply_pause.py")