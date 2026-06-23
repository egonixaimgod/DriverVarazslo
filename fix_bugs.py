import re
import os
import time

with open('driver_tool.py', 'r', encoding='utf-8') as f:
    text = f.read()

# 1. Mappadetektálás
old_wim_check = 'is_wim_extract = not online and "Windows_Gyari_Alap_Driverek" in norm_source'
new_wim_check = '''is_wim_extract = False
            if not online:
                repo_check = os.path.join(norm_source, "FileRepository")
                inf_check = os.path.join(norm_source, "INF")
                if os.path.isdir(repo_check) or os.path.isdir(inf_check):
                    is_wim_extract = True'''
text = text.replace(old_wim_check, new_wim_check)

# 2. PowerShell aszinkron letöltés
old_ps_dl_1 = r'''        $DL = $Session.CreateUpdateDownloader(); $DL.Updates = $SC
        try { $DR = $DL.Download() } catch { Write-Output "FAIL: [LETÖLTÉS HIBA] $t"; $f++; continue }
        if ($DR.ResultCode -ne 2 -and $DR.ResultCode -ne 3) { Write-Output "FAIL: [LETÖLTÉS HIBA kód=$($DR.ResultCode)] $t"; $f++; continue }'''
        
new_ps_dl_1 = r'''        $DL = $Session.CreateUpdateDownloader(); $DL.Updates = $SC
        $Job = $DL.BeginDownload($null, $null, $null)
        $timeout = 300; $elapsed = 0
        while (-not $Job.IsCompleted -and $elapsed -lt $timeout) { Start-Sleep -Seconds 1; $elapsed++ }
        if (-not $Job.IsCompleted) { try { $DL.EndDownload($Job) | Out-Null } catch {}; Write-Output "FAIL: [LETÖLTÉS IDŐTÚLLÉPÉS] $t"; $f++; continue }
        try { $DR = $DL.EndDownload($Job) } catch { Write-Output "FAIL: [LETÖLTÉS HIBA] $t"; $f++; continue }
        if ($DR.ResultCode -ne 2 -and $DR.ResultCode -ne 3) { Write-Output "FAIL: [LETÖLTÉS HIBA kód=$($DR.ResultCode)] $t"; $f++; continue }'''
text = text.replace(old_ps_dl_1, new_ps_dl_1)

old_ps_dl_2 = r'''        $DL = $Session.CreateUpdateDownloader(); $DL.Updates = $SC
        try { $DR = $DL.Download() } catch { Write-Output "FAIL: $($U.Title)"; $f++; continue }
        if ($DR.ResultCode -ne 2 -and $DR.ResultCode -ne 3) { Write-Output "FAIL: $($U.Title)"; $f++; continue }'''

new_ps_dl_2 = r'''        $DL = $Session.CreateUpdateDownloader(); $DL.Updates = $SC
        $Job = $DL.BeginDownload($null, $null, $null)
        $timeout = 300; $elapsed = 0
        while (-not $Job.IsCompleted -and $elapsed -lt $timeout) { Start-Sleep -Seconds 1; $elapsed++ }
        if (-not $Job.IsCompleted) { try { $DL.EndDownload($Job) | Out-Null } catch {}; Write-Output "FAIL: $($U.Title)"; $f++; continue }
        try { $DR = $DL.EndDownload($Job) } catch { Write-Output "FAIL: $($U.Title)"; $f++; continue }
        if ($DR.ResultCode -ne 2 -and $DR.ResultCode -ne 3) { Write-Output "FAIL: $($U.Title)"; $f++; continue }'''
text = text.replace(old_ps_dl_2, new_ps_dl_2)

# 3. ESD támogatás a Python-ban
old_esd = r'''        if wim_path.lower().endswith(".esd"):
            logging.error("[WIM] ESD fájl nem támogatott!")
            self.emit('alert', {'title': 'Hiba', 'message': 'ESD fájl nem támogatott. Kérlek, használj install.wim fájlt!'})
            return'''
new_esd = r'''        if wim_path.lower().endswith(".esd"):
            logging.info("[WIM] ESD fájl konvertálása szükséges.")'''
text = text.replace(old_esd, new_esd)

old_esd_mount = r'''                logging.info("[WIM] DISM Mount-Image futtatása...")
                self.emit('task_progress', {'task': 'wim', 'log': 'WIM csatolás (ez 4-5 perc)...', 'indeterminate': True,
                                            'counter': '1/3', 'status': 'Képfájl csatolása...'})
                res = self._run(["dism", "/Mount-Image", f"/ImageFile:{wim}", "/Index:1", f"/MountDir:{mount_dir}", "/ReadOnly"])'''

new_esd_mount = r'''                wim_to_mount = wim
                if wim.lower().endswith('.esd'):
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
                res = self._run(["dism", "/Mount-Image", f"/ImageFile:{wim_to_mount}", "/Index:1", f"/MountDir:{mount_dir}", "/ReadOnly"])'''
text = text.replace(old_esd_mount, new_esd_mount)

# WIM cleanup-hoz: ha esd, torolni a wimet
old_esd_cleanup = r'''                self._run(["dism", "/Cleanup-Wim"])
                shutil.rmtree(mount_dir, ignore_errors=True)

                logging.info(f"[WIM] Kész! Kimenet: {target_folder}")'''
new_esd_cleanup = r'''                self._run(["dism", "/Cleanup-Wim"])
                shutil.rmtree(mount_dir, ignore_errors=True)
                if wim.lower().endswith('.esd') and 'wim_to_mount' in locals() and os.path.exists(wim_to_mount):
                    os.remove(wim_to_mount)

                logging.info(f"[WIM] Kész! Kimenet: {target_folder}")'''
text = text.replace(old_esd_cleanup, new_esd_cleanup)


# 4. Cancel taskkill megszüntetése (DISM biztonság) - restore_offline run_dism_add_driver rész
old_taskkill = r'''                for line in process.stdout:
                    if self._check_cancel():
                        self._run(['taskkill', '/F', '/T', '/PID', str(process.pid)])
                        cancelled = True
                        break'''
new_taskkill = r'''                for line in process.stdout:
                    if self._check_cancel():
                        # Nem lőjük ki a processzt erőszakosan, hogy ne korrumpálódjon a Windows
                        cancelled = True
                        self.emit('task_progress', {'task': 'restore', 'log': '⚠️ Megszakítás kérve, várakozás a biztonságos leállásra...'})
                        break'''
text = text.replace(old_taskkill, new_taskkill)


# 5. Internet check
old_ping = r'''    def _check_internet(self):
        """Egyszerű ping alapú internet ellenőrzés."""
        try:
            # -n 1 = 1 ping, -w 1500 = 1.5 mp timeout
            res = subprocess.run(['ping', '8.8.8.8', '-n', '1', '-w', '1500'], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            return res.returncode == 0
        except Exception:
            return False'''

new_ping = r'''    def _check_internet(self):
        """Megbízható TCP port alapú internet ellenőrzés."""
        import socket
        try:
            socket.setdefaulttimeout(3.0)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect(("8.8.8.8", 53))
            return True
        except Exception:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.connect(("www.microsoft.com", 80))
                return True
            except Exception:
                return False'''
text = text.replace(old_ping, new_ping)

# 6. Watchdog timeout
text = text.replace("TIMEOUT = 15  # seconds", "TIMEOUT = 60  # seconds")

with open('driver_tool.py', 'w', encoding='utf-8') as f:
    f.write(text)

# ui.html ESD frissítése
with open('ui.html', 'r', encoding='utf-8') as f2:
    html = f2.read()

html = html.replace("Válaszd ki az install.wim fájlt", "Válaszd ki az install.wim vagy install.esd fájlt")
html = html.replace("WIM fájlok (*.wim)|*.wim", "Képfájlok (*.wim;*.esd)|*.wim;*.esd")
html = html.replace("install.wim fájljából bányásszuk ki", "install.wim vagy install.esd fájljából bányásszuk ki")
html = html.replace("ISO / WIM Alap Driver", "ISO / WIM / ESD Alap Driver")

with open('ui.html', 'w', encoding='utf-8') as f2:
    f2.write(html)

print('Sikeresen modositva minden!')