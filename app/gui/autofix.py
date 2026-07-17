"""DriverVarázsló GUI - 1 Kattintásos Driver Fix: a 3-lábú, reboot-láncolt AutoFix folyamat."""

# === AUTO-IMPORTS ===
import ctypes
import ctypes.wintypes
import os
import sys
import subprocess
import re
import time
import logging
import shutil
import json
from app.common import _app_exe_path
from app.common import _ps_quote
from app.wu_core import AUTOFIX_PRINTER_SKIP_CLASSES
from app.wu_core import WU_PNP_QUERY_PS
from app.wu_core import _build_wu_install_ps
from app.wu_core import _filter_wu_scan_devices
from app.wu_core import _match_wu_updates_to_devices
from datetime import datetime
# === /AUTO-IMPORTS ===


class GuiAutofixMixin:
    """1 Kattintásos Driver Fix: a 3-lábú, reboot-láncolt AutoFix folyamat. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def _create_restore_point_sync(self, task_id='autofix'):
        desc = "DriverVarázsló AutoFix - " + datetime.now().strftime("%Y-%m-%d %H:%M")
        self.emit('task_progress', {'task': task_id, 'log': 'Registry Mentés (Restore Point) készítése folyamatban...', 'indeterminate': True})
        self._run(["powershell", "-NoProfile", "-Command", 'Enable-ComputerRestore -Drive "$($env:SystemDrive)\\" -ErrorAction SilentlyContinue'])
        self._run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore', '/v', 'SystemRestorePointCreationFrequency', '/t', 'REG_DWORD', '/d', '0', '/f'])
        ps_cmd = f'Checkpoint-Computer -Description "{desc}" -RestorePointType "MODIFY_SETTINGS" -ErrorAction SilentlyContinue'
        res1 = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd], encoding='utf-8')
        if res1.returncode == 0:
            self.emit('task_progress', {'task': task_id, 'log': '✅ Registry mentés / Visszaállítási pont elkészült.\n'})
        else:
            self.emit('task_progress', {'task': task_id, 'log': '⚠️ Visszaállítási pont elutasítva a rendszer által. - FOLYTATÁS...\n'})

    def _disable_sleep_sync(self, task_id='autofix'):
        self.emit('task_progress', {'task': task_id, 'log': 'Alvó mód ideiglenes blokkolása a folyamat végéig (Windows API)...'})
        try:
            # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001 | 0x00000002)
            self.emit('task_progress', {'task': task_id, 'log': '✅ Energiagazdálkodás felülbírálva.\n'})
        except Exception as e:
            self.emit('task_progress', {'task': task_id, 'log': f'⚠️ Alvás tiltása sikertelen: {e}\n'})

    def _disable_wu_sync(self, task_id='autofix'):
        self.emit('task_progress', {'task': task_id, 'log': 'Windows automata driver frissítések letiltása a Registryben...', 'indeterminate': True})
        reg_cmd = ['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching', '/v', 'SearchOrderConfig', '/t', 'REG_DWORD', '/d', '0', '/f']
        self._run(reg_cmd)
        
        # Ez a registry kulcs megakadályozza, hogy a Gépház "Frissítések keresése" gomb megnyomásakor a rendszer drivereket is lehúzzon
        self._run(['reg', 'add', r'HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate', '/v', 'ExcludeWUDriversInQualityUpdate', '/t', 'REG_DWORD', '/d', '1', '/f'])
        
        self.emit('task_progress', {'task': task_id, 'log': '✅ Automatikus driver telepítés letiltva.\n'})

    def _delete_ghost_devices_sync(self, task_id='autofix', skip_classes=None):
        self.emit('task_progress', {'task': task_id, 'log': 'Nem csatlakoztatott (fantom) eszközök azonosítása és törlése...', 'indeterminate': True})
        skip_classes = skip_classes or set()
        # A skip_classes mindig a hardcodeolt AUTOFIX_PRINTER_SKIP_CLASSES konstansból jön
        # (nem felhasználói inputból), ezért biztonságos a PowerShell scriptbe fűzni.
        extra_exclusions = ''.join(f" -and $_.PNPClass -ne '{c}'" for c in sorted(skip_classes))
        if skip_classes:
            skip_match = ' -or '.join(f"$_.PNPClass -eq '{c}'" for c in sorted(skip_classes))
            skipped_count_expr = f"@(Get-PnpDevice -PresentOnly:$false | Where-Object {{ $_.Present -eq $false -and $_.InstanceId -ne $null -and ({skip_match}) }}).Count"
        else:
            skipped_count_expr = "0"
        ps_script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$skippedGhosts = """ + skipped_count_expr + r"""
Write-Output "SKIPPED: $skippedGhosts"
$ghosts = Get-PnpDevice -PresentOnly:$false | Where-Object { $_.Present -eq $false -and $_.InstanceId -ne $null -and $_.PNPClass -ne 'SoftwareDevice' -and $_.PNPClass -ne 'Net' -and $_.PNPClass -ne 'System'""" + extra_exclusions + r""" }
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
    $res = & pnputil /remove-device "$($id)" 2>&1
    if ($LASTEXITCODE -eq 0 -or $res -match "deleted" -or $res -match "törölve" -or $res -match "successfully") {
        $count++
    }
}
Write-Output "DONE: Törölve: $count / $total"
"""
        logging.debug(f"[CMD] Popen futtatása: {ps_script[:300]}...")
        process = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
            startupinfo=self._si, creationflags=self._nw)
        
        for line in process.stdout:
            if getattr(self, '_cancel_flag', False):
                self._run(['taskkill', '/F', '/T', '/PID', str(process.pid)])
                process.wait()
                raise Exception("Magyar_Megszakit_Flag")
            line = line.strip()
            if not line:
                continue
            if line.startswith("SKIPPED:"):
                m = re.search(r'SKIPPED:\s*(\d+)', line)
                if m and int(m.group(1)) > 0:
                    self.emit('task_progress', {'task': task_id, 'log': f'ℹ️ {m.group(1)} db nyomtató/szkenner szellemeszköz kihagyva.\n'})
            elif line.startswith("TOTAL:"):
                m = re.search(r'TOTAL:\s*(\d+)', line)
                if m:
                    total = int(m.group(1))
                self.emit('task_progress', {'task': task_id, 'log': f'{total} db szellemeszköz azonosítva. Törlés folyamatban...\n'})
            elif line.startswith("DONE:"):
                self.emit('task_progress', {'task': task_id, 'log': f'✅ {line[5:].strip()}\n'})
        
        process.wait()

    def _delete_third_party_sync(self, task_id='autofix', skip_classes=None):
        self.emit('task_progress', {'task': task_id, 'log': 'Third-party driverek összegyűjtése és törlése...', 'indeterminate': True})
        drivers = self._get_third_party_drivers()
        skip_classes = skip_classes or set()
        if skip_classes:
            skipped = [d for d in drivers if d.get('class', '') in skip_classes]
            drivers = [d for d in drivers if d.get('class', '') not in skip_classes]
            if skipped:
                self.emit('task_progress', {'task': task_id, 'log': f'ℹ️ {len(skipped)} db nyomtató/szkenner driver kihagyva (felhasználói beállítás szerint).\n'})
        total = len(drivers)
        if total > 0:
            self.emit('task_progress', {'task': task_id, 'log': f'{total} db third-party driver eltávolítása...\n'})
            for i, drv in enumerate(drivers):
                if self._cancel_flag: raise Exception("Magyar_Megszakit_Flag")
                name = drv.get('published', '')
                if not name: continue
                self.emit('task_progress', {'task': task_id, 'log': f'🗑 Törlés ({i+1}/{total}): {name}', 'current': i+1, 'total': total})
                self._run(['pnputil', '/delete-driver', name, '/uninstall', '/force'])
            self.emit('task_progress', {'task': task_id, 'log': '✅ Driverek eltávolítva.\n'})
        else:
            self.emit('task_progress', {'task': task_id, 'log': '✅ Nincs third-party driver a rendszerben.\n'})

    def _scan_and_install_wu_sync(self, task_id='autofix'):
        max_loops = 4
        total_installed_in_session = 0

        attempted_uids = set()
        
        for loop_idx in range(1, max_loops + 1):
            if getattr(self, '_cancel_flag', False):
                break
            self.emit('task_progress', {'task': task_id, 'log': f'\n--- DRIVER KERESÉS KÖR: {loop_idx} / {max_loops} ---'})
            self.emit('task_progress', {'task': task_id, 'log': 'Új eszközök szkennelése PnP Util-lal...', 'indeterminate': True})
            self._run(['pnputil', '/scan-devices'])
            time.sleep(10)
            self.emit('task_progress', {'task': task_id, 'log': 'Hivatalos driverek keresése és egyeztetése (Windows Update). Ez percekig is eltarthat...'})
            
            # Eszköz-lekérdezés és párosítás a KÖZÖS magból (_filter_wu_scan_devices +
            # _match_wu_updates_to_devices) - pontosan ugyanaz fut, mint a manuális
            # hardver-szkennelésnél, ne ide írj szűrési/párosítási logikát!
            res = self._run(["powershell", "-NoProfile", "-Command", WU_PNP_QUERY_PS], encoding='utf-8')
            pnp_data = []
            if res.stdout:
                try:
                    pnp_data = json.loads(res.stdout)
                except Exception:
                    pass
            devices_to_check = _filter_wu_scan_devices(pnp_data)

            self.emit('task_progress', {'task': task_id, 'log': f'✅ {len(devices_to_check)} hardverelem azonosítva. Egyeztetés...'})
            wu_results = self._search_wu_api() or []

            matches = _match_wu_updates_to_devices(wu_results, devices_to_check, exclude_uids=attempted_uids)
            matched_updates = [m['uid'] for m in matches]
            attempted_uids.update(matched_updates)

            if not matched_updates:
                self.emit('task_progress', {'task': task_id, 'log': '✅ Szerveren nincs újabb valós illesztőprogram.'})
                self.emit('task_progress', {'task': task_id, 'log': 'Minden elérhető driver telepítve! Keresési lánc befejezve.'})
                break
                
            self.emit('task_progress', {'task': task_id, 'log': f'✅ Telepítendő driverek száma: {len(matched_updates)}'})

            # A telepítő script a KÖZÖS _build_wu_install_ps-ből jön - ugyanaz, mint a
            # manuális telepítésnél, csak itt a kör összes párosított UpdateID-jával fut.
            install_ps = _build_wu_install_ps(target_uids=matched_updates)
            logging.debug(f"[CMD] Popen futtatása: {install_ps[:300]}...")
            process = subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", install_ps],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                startupinfo=self._si, creationflags=self._nw)

            for line in process.stdout:
                if getattr(self, '_cancel_flag', False):
                    self._run(['taskkill', '/F', '/T', '/PID', str(process.pid)])
                    process.wait()
                    self.emit('task_progress', {'task': task_id, 'log': '\n❗ Megszakítva!'})
                    raise Exception("Magyar_Megszakit_Flag")
                line = line.strip()
                if not line:
                    continue
                # A közös script kimeneti protokollja (INIT/SEARCH/FOUND/SKIP/TOTAL/DLONE/
                # INSTONE/OK/FAIL/EMPTY/DONE/ERROR) - lásd _build_wu_install_ps docstring.
                if line.startswith("TOTAL:"):
                    self.emit('task_progress', {'task': task_id, 'log': '--- LETÖLTÉS ÉS TELEPÍTÉS ---'})
                elif line.startswith("DLONE:"):
                    self.emit('task_progress', {'task': task_id, 'log': f'[LETOLTES] {line[6:].strip()}'})
                elif line.startswith("INSTONE:"):
                    self.emit('task_progress', {'task': task_id, 'log': f'[TELEPITES] Telepítés alatt: {line[8:].strip()}'})
                elif line.startswith("OK:"):
                    total_installed_in_session += 1
                    self.emit('task_progress', {'task': task_id, 'log': f'[OK] SIKERES: {line[3:].strip()}'})
                elif line.startswith("FAIL:"):
                    self.emit('task_progress', {'task': task_id, 'log': f'[HIBA] SIKERTELEN: {line[5:].strip()}'})
                elif line.startswith("EMPTY:"):
                    self.emit('task_progress', {'task': task_id, 'log': f'[FIGYELMEZTETES] {line[6:].strip()}'})
                elif line.startswith("ERROR:"):
                    logging.error(f"[AUTOFIX-WU] PowerShell hiba: {line[6:].strip()}")
                    self.emit('task_progress', {'task': task_id, 'log': f'[HIBA] {line[6:].strip()}'})
                elif line.startswith("DONE:"):
                    self.emit('task_progress', {'task': task_id, 'log': f'--- {line[5:].strip()} ---'})
                elif line.startswith("INIT:") or line.startswith("SEARCH:") or \
                        line.startswith("FOUND:") or line.startswith("SKIP:"):
                    pass  # protokoll-sorok, a kör elején már kiírtuk az összesítést
                else:
                    self.emit('task_progress', {'task': task_id, 'log': line})
            process.wait()
                        
        return total_installed_in_session

    def run_autofix(self, skip_printer_drivers=True):
        logging.info(f"[API] run_autofix() indítása (skip_printer_drivers={skip_printer_drivers})")
        if self.target_os_path:
            self.emit('toast', {'message': 'Az 1 kattintásos fix csak az Élő (jelenlegi) rendszeren futtatható le biztonságosan!', 'type': 'error'})
            return

        def worker():
            is_resume_step1 = getattr(self, 'resume_step1', False)
            is_resume_mode = getattr(self, 'resume_mode', False)
            # Resume lábakon (új processz, a dialógus meg sem jelenik újra) a JS-paraméter
            # irreleváns - az A láb által a Scheduled Task argumentumába épített flag-et kell
            # sys.argv-ből visszaolvasni (lásd __init__: self.skip_printer_drivers).
            if is_resume_step1 or is_resume_mode:
                skip_printers = getattr(self, 'skip_printer_drivers', True)
            else:
                skip_printers = skip_printer_drivers

            task_title = '1 Katt. Fix (RESTART UTÁNI LÁNC FOLYTATÁSA!)' if (is_resume_mode or is_resume_step1) else '1 Kattintásos Driver Javítás és Frissítés'
            self.emit('task_start', {'task': 'autofix', 'title': task_title})
            try:
                # Internet ellenőrzés autofix elején (ha nem resume mód)
                if not is_resume_mode and not is_resume_step1:
                    self.emit('task_progress', {'task': 'autofix', 'log': '⏳ Internetkapcsolat ellenőrzése...'})
                    if not self._check_internet():
                        self.emit('toast', {'message': '❌ Nincs internetkapcsolat! Kérlek csatlakozz egy hálózathoz az Autofix előtt!', 'type': 'error'})
                        self.emit('task_complete', {'task': 'autofix', 'status': '❌ Nincs Internetkapcsolat!'})
                        return

                # ÚJ LÉPÉS (-1. LÉPÉS)
                if not is_resume_mode and not is_resume_step1:
                    self.emit('task_progress', {'task': 'autofix', 'log': '-1. LÉPÉS: Windows Update szüneteltetése és újraindítás...'})
                    
                    self._disable_sleep_sync()
                    
                    self.emit('task_progress', {'task': 'autofix', 'log': 'WU szüneteltetése 1 hétre...'})
                    ps_pause = r"""
                    $pauseDate = (Get-Date).AddDays(7).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
                    $nowDate = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
                    Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseUpdatesExpiryTime' -Value $pauseDate -Type String -Force | Out-Null
                    Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseFeatureUpdatesEndTime' -Value $pauseDate -Type String -Force | Out-Null
                    Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseQualityUpdatesEndTime' -Value $pauseDate -Type String -Force | Out-Null
                    Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseUpdatesStartTime' -Value $nowDate -Type String -Force | Out-Null
                    Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseFeatureUpdatesStartTime' -Value $nowDate -Type String -Force | Out-Null
                    Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseQualityUpdatesStartTime' -Value $nowDate -Type String -Force | Out-Null
                    """
                    self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_pause])
                    self.emit('task_progress', {'task': 'autofix', 'log': '✅ WU szüneteltetve 1 hétre.\n'})
                    
                    self.emit('task_progress', {'task': 'autofix', 'log': '🔄 A számítógép újraindul, majd a folyamat a rendszer előkészítésével folytatódik!'})
                    
                    exe_path = _app_exe_path()
                    temp_env = os.environ.get('TEMP', '!!').lower()
                    if temp_env in exe_path.lower():
                        try:
                            public_dir = os.environ.get('PUBLIC', 'C:\\Users\\Public')
                            safe_exe = os.path.join(public_dir, "DriverVarazslo_Resume.exe" if getattr(sys, 'frozen', False) else "DriverVarazslo_Resume.py")
                            shutil.copy2(exe_path, safe_exe)
                            exe_path = safe_exe
                            self.emit('task_progress', {'task': 'autofix', 'log': 'ℹ️ Temp mappából futás detektálva. Biztonsági másolat készítve a Public mappába.'})
                        except Exception as e:
                            logging.error(f"[AUTOFIX] Biztonsági másolat hiba: {e}")
                    
                    resume_flag = '--resume-step1'
                    if skip_printers:
                        resume_flag += ' --skip-printer-drivers'

                    if getattr(sys, 'frozen', False):
                        exec_path = exe_path
                        args = resume_flag
                    else:
                        exec_path = sys.executable
                        args = f'"{exe_path}" {resume_flag}'

                    task_ps = f'''
                    $action = New-ScheduledTaskAction -Execute '{_ps_quote(exec_path)}' -Argument '{_ps_quote(args)}'
                    $trigger = New-ScheduledTaskTrigger -AtLogOn
                    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
                    Register-ScheduledTask -TaskName "DriverVarazsloResume" -Action $action -Trigger $trigger -Principal $principal -Force
                    '''
                    self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", task_ps])

                    self.emit('task_complete', {'task': 'autofix', 'status': 'Újraindulás felkészítve (-1. lépés)...'})
                    time.sleep(5)
                    self._run(['shutdown', '/r', '/t', '0', '/f'])
                    return

                if not is_resume_mode:
                    if is_resume_step1:
                        self._run(["powershell", "-NoProfile", "-Command", 'Unregister-ScheduledTask -TaskName "DriverVarazsloResume" -Confirm:$false -ErrorAction SilentlyContinue'])
                    self.emit('task_progress', {'task': 'autofix', 'log': '0. LÉPÉS: Rendszer előkészítése és régi driverek törlése...'})
                    
                    self._disable_sleep_sync()
                    
                    self.emit('task_progress', {'task': 'autofix', 'log': 'Windows Gyors Rendszerindítás (Fast Startup) kikapcsolása...'})
                    self._run(["powercfg", "/h", "off"])
                    
                    self._disable_wu_sync()
                    if getattr(self, '_cancel_flag', False): raise Exception("Magyar_Megszakit_Flag")
                    
                    self._create_restore_point_sync()
                    if getattr(self, '_cancel_flag', False): raise Exception("Magyar_Megszakit_Flag")

                    skip_cls = AUTOFIX_PRINTER_SKIP_CLASSES if skip_printers else None

                    self._delete_ghost_devices_sync(skip_classes=skip_cls)
                    if getattr(self, '_cancel_flag', False): raise Exception("Magyar_Megszakit_Flag")

                    self._delete_third_party_sync(skip_classes=skip_cls)
                    if getattr(self, '_cancel_flag', False): raise Exception("Magyar_Megszakit_Flag")
                    
                    self.emit('task_progress', {'task': 'autofix', 'log': 'Szolgáltatások leállítása és újraindítási jelzések (Pending Reboot) törlése...'})
                    self._run(['reg', 'delete', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired', '/f'])

                    self.emit('task_progress', {'task': 'autofix', 'log': 'Beragadt frissítések és WU gyorsítótár (SoftwareDistribution) ürítése...'})
                    clear_cache = r"""
                    Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
                    Stop-Service bits -Force -ErrorAction SilentlyContinue
                    Stop-Service cryptsvc -Force -ErrorAction SilentlyContinue
                    Stop-Service UsoSvc -Force -ErrorAction SilentlyContinue
                    """
                    self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", clear_cache])
                    
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
                            logging.warning(f"[AUTOFIX] Cache törlés újrapróbálás: {e}")
                            time.sleep(3)
                            
                    if not deleted_cache:
                        self._run(["powershell", "-NoProfile", "-Command", f'Remove-Item -Path "{sw_dist}" -Recurse -Force -ErrorAction SilentlyContinue'])
                    
                    self.emit('task_progress', {'task': 'autofix', 'log': 'WU szüneteltetése 1 hétre...'})
                    ps_pause = r"""
                    $pauseDate = (Get-Date).AddDays(7).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
                    $nowDate = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
                    Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseUpdatesExpiryTime' -Value $pauseDate -Type String -Force | Out-Null
                    Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseFeatureUpdatesEndTime' -Value $pauseDate -Type String -Force | Out-Null
                    Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseQualityUpdatesEndTime' -Value $pauseDate -Type String -Force | Out-Null
                    Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseUpdatesStartTime' -Value $nowDate -Type String -Force | Out-Null
                    Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseFeatureUpdatesStartTime' -Value $nowDate -Type String -Force | Out-Null
                    Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -Name 'PauseQualityUpdatesStartTime' -Value $nowDate -Type String -Force | Out-Null
                    """
                    self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_pause])
                    self.emit('task_progress', {'task': 'autofix', 'log': '✅ WU gyorsítótár ürítve és szüneteltetve 1 hétre.\n'})
                    
                    self.emit('task_progress', {'task': 'autofix', 'log': '🔄 A számítógép újraindul, majd a folyamat automatikusan a TELEPÍTÉSSEL folytatódik!'})
                    
                    exe_path = _app_exe_path()
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
                        exec_path = exe_path
                        args = '--resume-autofix'
                    else:
                        exec_path = sys.executable
                        args = f'"{exe_path}" --resume-autofix'
                    
                    task_ps = f'''
                    $action = New-ScheduledTaskAction -Execute '{_ps_quote(exec_path)}' -Argument '{_ps_quote(args)}'
                    $trigger = New-ScheduledTaskTrigger -AtLogOn
                    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
                    Register-ScheduledTask -TaskName "DriverVarazsloResume" -Action $action -Trigger $trigger -Principal $principal -Force
                    '''
                    self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", task_ps])
                    
                    self.emit('task_complete', {'task': 'autofix', 'status': 'Újraindulás felkészítve...'})
                    time.sleep(5)
                    self._run(['shutdown', '/r', '/t', '0', '/f'])
                    return
                else:
                    self._run(["powershell", "-NoProfile", "-Command", 'Unregister-ScheduledTask -TaskName "DriverVarazsloResume" -Confirm:$false -ErrorAction SilentlyContinue'])
                    self.emit('task_progress', {'task': 'autofix', 'log': 'Láncolt folytatás gépújraindítás után. Régi driverek törlése kihagyva, hogy ne töröljünk friss drivereket.\n'})
                    self._disable_sleep_sync()

                # 4. Átmenetileg engedélyezzük a WU-t és unpause a driverkereséshez
                self.emit('task_progress', {'task': 'autofix', 'log': 'Windows Update ideiglenes felébresztése a szükséges driverek lekéréséhez...', 'indeterminate': True})
                # BIZTOSÍTÉK: Teljesen letiltjuk a háttérben futó Automatikus Frissítéseket (Group Policy)
                self._run(['reg', 'add', r'HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU', '/v', 'NoAutoUpdate', '/t', 'REG_DWORD', '/d', '1', '/f'])
                self._set_wu_pause(pause=False)

                # 4. Keresés és visszaépítés
                # A finally garantálja, hogy az 5. lépés (WU letiltás/szüneteltetés visszaállítása)
                # akkor is lefusson, ha a scan/install kivétellel elszáll - különben a WU
                # véglegesen (NoAutoUpdate=1) letiltva maradna a gépen, ütemezett feladat nélkül,
                # ami ezt valaha visszaállítaná.
                try:
                    installed_count = self._scan_and_install_wu_sync()
                finally:
                    # 5. Végső WU letiltás és szüneteltetés visszaállítása
                    self._run(['reg', 'delete', r'HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU', '/v', 'NoAutoUpdate', '/f'])
                    self._disable_wu_sync()
                    self._set_wu_pause(pause=True)

                self.emit('task_progress', {'task': 'autofix', 'log': '\n🎉 MINDEN LÉPÉS KÉSZ!'})
                
                if installed_count > 0:
                    self.emit('task_progress', {'task': 'autofix', 'log': f'\n🔄 EBBEN A KÖRBEN {installed_count} DRIVER TELEPÜLT!\nTovább láncolt hardverek aktiválásához újabb automatikus újraindítás szükséges!\nA rendszer az újraindulás után folytatja a szkennelést!'})
                    # Set RunOnce
                    exe_path = _app_exe_path()
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
                        exec_path = exe_path
                        args = '--resume-autofix'
                    else:
                        exec_path = sys.executable
                        args = f'"{exe_path}" --resume-autofix'
                    
                    task_ps = f'''
                    $action = New-ScheduledTaskAction -Execute '{_ps_quote(exec_path)}' -Argument '{_ps_quote(args)}'
                    $trigger = New-ScheduledTaskTrigger -AtLogOn
                    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
                    Register-ScheduledTask -TaskName "DriverVarazsloResume" -Action $action -Trigger $trigger -Principal $principal -Force
                    '''
                    self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", task_ps])
                    
                    self.emit('task_complete', {'task': 'autofix', 'status': 'Újraindulás felkészítve...'})
                    time.sleep(5)
                    self._run(['shutdown', '/r', '/t', '0', '/f'])
                    return
                else:
                    self.emit('task_progress', {'task': 'autofix', 'log': '\n🎉 KÉSZ! Nulla újonnan fellelt driver, a konfiguráció végigért.'})
                    self._run(["powershell", "-NoProfile", "-Command", 'Unregister-ScheduledTask -TaskName "DriverVarazsloResume" -Confirm:$false -ErrorAction SilentlyContinue'])

                    self.emit('task_progress', {'task': 'autofix', 'log': 'DCH alkalmazások (Microsoft Store) frissítésének kényszerítése...'})
                    try:
                        # Ez aszinkron elindítja a Store App-ok (pl. Realtek Audio Console) szinkronizálását a háttérben
                        ws_script = r"Get-CimInstance -Namespace 'Root\cimv2\mdm\dmmap' -ClassName 'MDM_EnterpriseModernAppManagement_AppManagement01' | Invoke-CimMethod -MethodName UpdateScanMethod"
                        self._run(["powershell", "-WindowStyle", "Hidden", "-Command", ws_script])
                        self.emit('task_progress', {'task': 'autofix', 'log': '✅ Store App-ok szinkronizálása a háttérben elindítva.'})
                    except Exception as e:
                        logging.debug(f"[AUTOFIX] Store App sync error: {e}")
                    
                    try:
                        self.emit('task_progress', {'task': 'autofix', 'log': '\nA FOLYAMAT SIKERESEN BEFEJEZŐDÖTT!'})
                    except Exception:
                        pass
                    
                    # If we were in resume mode, it means this was an automated post-boot check that found nothing.
                    # We can close the app or leave it open. Let's just finish the task.
                    self.emit('task_complete', {'task': 'autofix', 'status': 'Teljesen befejezve'})
                    if not getattr(self, 'resume_mode', False):
                        time.sleep(1)
                        self.emit('ask_reboot', None)

            except Exception as e:
                if str(e) == "Magyar_Megszakit_Flag":
                    self.emit('task_error', {'task': 'autofix', 'error': 'Felhasználó által megszakítva.'})
                else:
                    logging.error(f"[AUTOFIX] Hiba: {e}")
                    self.emit('task_error', {'task': 'autofix', 'error': str(e)})
                    
        self._safe_thread('autofix', worker)
