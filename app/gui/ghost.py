"""DriverVarázsló GUI - Szellemeszközök nézet: nem jelenlévő (ghost) eszközök törlése."""

# === AUTO-IMPORTS ===
import subprocess
import re
import logging
# === /AUTO-IMPORTS ===


class GuiGhostMixin:
    """Szellemeszközök nézet: nem jelenlévő (ghost) eszközök törlése. A DriverToolApi része (összerakás: app/gui/api.py)."""

    # ================================================================
    # HARDWARE SCAN
    # ================================================================
    def delete_ghost_devices(self):
        logging.info("[API] delete_ghost_devices()")
        if self.target_os_path:
            self.emit('toast', {'message': '❌ Hiba: Ez a funkció csak Élő (Online) rendszeren működik!', 'type': 'error'})
            return
        def worker():
            logging.info("[GHOST] Szellemeszközök törlésének indítása...")
            self.emit('task_start', {'task': 'ghost', 'title': 'Szellemeszközök Törlése'})
            self.emit('task_progress', {'task': 'ghost', 'log': 'Nem csatlakoztatott (fantom) eszközök azonosítása...', 'indeterminate': True})

            ps_script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ghosts = Get-PnpDevice -PresentOnly:$false | Where-Object { $_.Present -eq $false -and $_.InstanceId -ne $null -and $_.PNPClass -ne 'SoftwareDevice' -and $_.PNPClass -ne 'Net' -and $_.PNPClass -ne 'System' }
$count = 0
$total = @($ghosts).Count
if ($total -eq 0) {
    Write-Output "DONE: Nincs szellemeszköz a rendszerben."
    exit
}
Write-Output "TOTAL: $total"
foreach ($dev in $ghosts) {
    $id = $dev.PNPDeviceID
    $name = $dev.Name
    if (-not $name) { $name = "Ismeretlen eszköz" }
    Write-Output "RM: $name"
    $res = & pnputil /remove-device "$($id)" 2>&1
    if ($LASTEXITCODE -eq 0 -or $res -match "deleted" -or $res -match "törölve" -or $res -match "successfully") {
        Write-Output "OK: $name"
        $count++
    } else {
        Write-Output "FAIL: $name"
    }
}
Write-Output "DONE: Törölve: $count / $total"
"""
            process = subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                startupinfo=self._si, creationflags=self._nw)
            
            success = 0
            total = 0
            
            for line in process.stdout:
                if self._check_cancel():
                    self._run(['taskkill', '/F', '/T', '/PID', str(process.pid)])
                    process.wait()
                    self.emit('task_progress', {'task': 'ghost', 'log': '\n❗ Megszakítva!'})
                    self.emit('task_complete', {'task': 'ghost', 'status': '❗ Megszakítva!', 'success': success, 'fail': total-success})
                    return
                line = line.strip()
                if not line:
                    continue
                if line.startswith("TOTAL:"):
                    m = re.search(r'TOTAL:\s*(\d+)', line)
                    if m:
                        total = int(m.group(1))
                    self.emit('task_progress', {'task': 'ghost', 'log': f'Összesen {total} db szellemeszköz azonosítva...\n', 'total': total, 'current': 0, 'counter': f'0 / {total}'})
                elif line.startswith("RM:"):
                    self.emit('task_progress', {'task': 'ghost', 'log': f'  🗑 Próbálkozás: {line[3:].strip()}', 'status': f'Eltávolítás: {line[3:].strip()}'})
                elif line.startswith("OK:"):
                    success += 1
                    self.emit('task_progress', {'task': 'ghost', 'log': f'  ✅ Sikeresen törölve: {line[3:].strip()}', 'current': success, 'counter': f'{success} / {total}'})
                elif line.startswith("FAIL:"):
                    self.emit('task_progress', {'task': 'ghost', 'log': f'  ❌ Sikertelen (valószínűleg védett eszköz): {line[5:].strip()}', 'current': success, 'counter': f'{success} / {total}'})
                elif line.startswith("DONE:"):
                    self.emit('task_progress', {'task': 'ghost', 'log': f'\n{line[5:].strip()}'})
                else:
                    self.emit('task_progress', {'task': 'ghost', 'log': line})
            
            process.wait()
            self.emit('task_progress', {'task': 'ghost', 'log': '✅ Szellemeszközök törlése befejeződött.'})
            self.emit('task_complete', {'task': 'ghost', 'status': f'Kész! Törölve: {success} / {total}'})

        self._safe_thread('ghost', worker)
