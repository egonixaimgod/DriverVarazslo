import re

# 1. UI HTML módosítása
with open('ui.html', 'r', encoding='utf-8') as f:
    html = f.read()

old_wu_html = r'''      <!-- VIEW: Windows Update -->
      <div class="view" id="view-wu">
        <div class="panel glass">
          <div class="panel-title"><span class="emoji">🔄</span> Windows Update Driver Frissítések Beállításai</div>
          <div class="wu-status unknown" id="wu-status">Állapot: Ismeretlen</div>
          <div class="wu-actions">
            <button class="btn btn-danger" onclick="disableWu()">⛔ WU Driver Letiltás</button>
            <button class="btn btn-success" onclick="enableWu()">✅ WU Driver Engedélyezés + Reset</button>
            <button class="btn btn-warning" onclick="restartWu()">⚡ WU Szolgáltatások Újraindítása</button>
          </div>
        </div>
      </div>'''

new_wu_html = r'''      <!-- VIEW: Windows Update -->
      <div class="view" id="view-wu">
        <div class="panel glass">
          <div class="panel-title"><span class="emoji">🔄</span> Windows Update Driver Frissítések Beállításai</div>
          <div class="wu-status unknown" id="wu-status">Állapot: Ismeretlen</div>
          <div class="wu-actions">
            <button class="btn btn-danger" onclick="disableWu()">⛔ WU Driver Letiltás</button>
            <button class="btn btn-success" onclick="enableWu()">✅ WU Driver Engedélyezés + Reset</button>
            <button class="btn btn-warning" onclick="restartWu()">⚡ WU Szolgáltatások Újraindítása</button>
          </div>
        </div>
        
        <div class="panel glass">
          <div class="panel-title"><span class="emoji">⏸️</span> Windows Update Frissítések Szüneteltetése</div>
          <p style="margin-bottom: 16px; color: var(--text3);">Ezekkel a gombokkal a teljes Windows Update működését (a biztonsági frissítéseket is) elhalaszthatod a jövőbe. Az 1 hetet többször is megnyomhatod a hetek halmozásához!</p>
          <div class="wu-actions">
            <button class="btn btn-primary" onclick="pauseWu(7)">⏳ +1 Hét Szünet</button>
            <button class="btn btn-danger" onclick="pauseWu(3650)">🛑 10 Év Szünet (Végleges)</button>
            <button class="btn btn-success" onclick="resumeWu()">▶️ Szünetelés Feloldása</button>
          </div>
        </div>
      </div>'''

html = html.replace(old_wu_html, new_wu_html)

old_js = r'''async function restartWu() {
  try {
    await api().restart_wu();
  } catch (e) {
    toast('WU újraindítás hiba: ' + e, 'error');
  }
}'''

new_js = r'''async function restartWu() {
  try {
    await api().restart_wu();
  } catch (e) {
    toast('WU újraindítás hiba: ' + e, 'error');
  }
}

async function pauseWu(days) {
  try {
    await api().pause_wu(days);
    setTimeout(checkWuStatus, 1500); // Vár egy picit, hogy érvénybe lépjen a Registry
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
}'''

html = html.replace(old_js, new_js)

with open('ui.html', 'w', encoding='utf-8') as f:
    f.write(html)

# 2. Python Backend módosítása
with open('driver_tool.py', 'r', encoding='utf-8') as f:
    py_text = f.read()

old_wu_status = r'''    def check_wu_status(self):
        logging.info("[API] check_wu_status()")
        if self.target_os_path:
            return {'status': 'Offline (Nem olvasható)', 'color': 'unknown'}
        try:
            policy_disabled = False
            search_disabled = False
            service_disabled = False'''

new_wu_status = r'''    def check_wu_status(self):
        logging.info("[API] check_wu_status()")
        if self.target_os_path:
            return {'status': 'Offline (Nem olvasható)', 'color': 'unknown'}
        try:
            policy_disabled = False
            search_disabled = False
            service_disabled = False
            paused_until = None
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\WindowsUpdate\UX\Settings", 0, winreg.KEY_READ) as key:
                    val, _ = winreg.QueryValueEx(key, "PauseUpdatesExpiryTime")
                    if val:
                        paused_until = val.replace("T", " ").replace("Z", " (UTC)")
                        logging.debug(f"[WU_STATUS] PauseUpdatesExpiryTime = {val}")
            except Exception:
                pass'''
py_text = py_text.replace(old_wu_status, new_wu_status)

old_wu_status_eval = r'''            if service_disabled:
                result = {'status': 'Szolgáltatás LETILTVA (services.msc)', 'color': 'disabled'}
            elif policy_disabled and search_disabled:
                result = {'status': 'Teljesen LETILTVA', 'color': 'disabled'}'''

new_wu_status_eval = r'''            if service_disabled:
                result = {'status': 'Szolgáltatás LETILTVA (services.msc)', 'color': 'disabled'}
            elif paused_until:
                result = {'status': f'SZÜNETELTETVE idáig: {paused_until}', 'color': 'warning'}
            elif policy_disabled and search_disabled:
                result = {'status': 'Teljesen LETILTVA', 'color': 'disabled'}'''
py_text = py_text.replace(old_wu_status_eval, new_wu_status_eval)


new_wu_methods = r'''
    def pause_wu(self, days):
        logging.info(f"[API] pause_wu(days={days})")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: Offline módban nem elérhető!', 'type': 'error'})
            return
            
        def worker():
            self.emit('task_start', {'task': 'pause_wu', 'title': f'WU Szüneteltetés ({days} nap)'})
            self.emit('task_progress', {'task': 'pause_wu', 'log': f'{days} nap hozzáadása a Windows Update szüneteltetéséhez...', 'indeterminate': True})
            
            ps = f"""
            $regPath = 'HKLM:\\SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings'
            if (!(Test-Path $regPath)) {{ New-Item -Path $regPath -Force | Out-Null }}
            
            $daysToAdd = {days}
            $now = (Get-Date).ToUniversalTime()
            
            $currentPauseStr = (Get-ItemProperty -Path $regPath -Name 'PauseUpdatesExpiryTime' -ErrorAction SilentlyContinue).PauseUpdatesExpiryTime
            
            if ($currentPauseStr -and $daysToAdd -eq 7) {{
                try {{
                    $currentPause = [datetime]$currentPauseStr
                    if ($currentPause -lt $now) {{ $currentPause = $now }}
                }} catch {{
                    $currentPause = $now
                }}
                $newDate = $currentPause.AddDays($daysToAdd)
            }} else {{
                $newDate = $now.AddDays($daysToAdd)
            }}
            
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
            
            ps = '''
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
            '''
            self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps])
            
            self.emit('task_progress', {'task': 'resume_wu', 'log': '✅ Szüneteltetés feloldva!'})
            self.emit('task_complete', {'task': 'resume_wu', 'status': '✅ Szüneteltetés feloldva!'})
            
        self._safe_thread('resume_wu', worker)

    # ================================================================
    # BACKUP / RESTORE
'''

py_text = py_text.replace("    # ================================================================\n    # BACKUP / RESTORE\n", new_wu_methods)

with open('driver_tool.py', 'w', encoding='utf-8') as f:
    f.write(py_text)

print("HTML és Python backend frissítve!")
