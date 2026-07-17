"""DriverVarázsló GUI - BitLocker Kezelő nézet: állapot lekérdezés + kikapcsolás (dekódolás)."""

# === AUTO-IMPORTS ===
import time
import logging
# === /AUTO-IMPORTS ===


class GuiBitlockerMixin:
    """BitLocker Kezelő nézet: állapot lekérdezés + kikapcsolás (dekódolás). A DriverToolApi része (összerakás: app/gui/api.py)."""

    def get_bitlocker_status(self):
        logging.info("[API] get_bitlocker_status()")
        if self.target_os_path:
            return {'status': 'Offline', 'color': 'unknown'}
        try:
            ps_script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$vol = Get-BitLockerVolume -MountPoint $env:SystemDrive -ErrorAction SilentlyContinue
if (-not $vol) { Write-Output "Ismeretlen"; exit }
$ps = $vol.ProtectionStatus
$vs = $vol.VolumeStatus
$pct = $vol.EncryptionPercentage
if ($ps -eq 'On' -or $vs -eq 'FullyEncrypted') {
    Write-Output "Titkosítva (Aktív)"
} elseif ($vs -eq 'EncryptionInProgress') {
    Write-Output "Titkosítás folyamatban ($pct%)"
} elseif ($vs -eq 'DecryptionInProgress') {
    Write-Output "Dekódolás folyamatban ($pct%)"
} elseif ($ps -eq 'Off' -or $vs -eq 'FullyDecrypted') {
    Write-Output "Nincs titkosítva (Kikapcsolva)"
} else {
    Write-Output "Állapot: $vs ($pct%)"
}
"""
            res = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script], encoding='utf-8')
            status_text = res.stdout.strip()
            
            color = 'unknown'
            if 'Titkosítva' in status_text: color = 'enabled'
            elif 'Dekódolás folyamatban' in status_text: color = 'warning'
            elif 'Nincs titkosítva' in status_text: color = 'disabled'
            
            return {'status': status_text, 'color': color}
        except Exception as e:
            logging.error(f"[BITLOCKER] Status hiba: {e}")
            return {'status': f'Hiba: {e}', 'color': 'unknown'}

    def disable_bitlocker(self):
        logging.info("[API] disable_bitlocker()")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: Offline módban nem elérhető!', 'type': 'error'})
            return
            
        def worker():
            self.emit('task_start', {'task': 'bitlocker', 'title': 'BitLocker Végleges Kikapcsolása'})
            self.emit('task_progress', {'task': 'bitlocker', 'log': 'Dekódolási parancs kiadása a rendszernek (Disable-BitLocker)...', 'indeterminate': True})
            
            ps_cmd = r"Disable-BitLocker -MountPoint $env:SystemDrive"
            res = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd])
            
            if res.returncode == 0:
                self.emit('task_progress', {'task': 'bitlocker', 'log': '✅ Parancs sikeresen kiadva!\n\nA dekódolás megkezdődött a háttérben.\nKérlek, frissítsd az állapotot a gombbal az aktuális százalék lekérdezéséhez.'})
                self.emit('task_complete', {'task': 'bitlocker', 'status': '✅ Dekódolás megkezdve!'})
                # Auto update status after 2 seconds
                time.sleep(2)
                self.emit('bitlocker_status', self.get_bitlocker_status())
            else:
                self.emit('task_progress', {'task': 'bitlocker', 'log': f'❌ Hiba: {res.stderr}'})
                self.emit('task_complete', {'task': 'bitlocker', 'status': '❌ Hiba történt!'})
                
        self._safe_thread('bitlocker', worker)
