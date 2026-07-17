"""DriverVarázsló GUI - BCD / bootloader javítás (önálló gomb + offline-visszaállítás utáni futtatás)."""

# === AUTO-IMPORTS ===
import os
import re
import logging
# === /AUTO-IMPORTS ===


class GuiBcdMixin:
    """BCD / bootloader javítás (önálló gomb + offline-visszaállítás utáni futtatás). A DriverToolApi része (összerakás: app/gui/api.py)."""

    # ================================================================
    # BCD REPAIR (boot loader javítás offline restore után)
    # ================================================================
    def _repair_bcd(self, target_drive):
        """BCD újraépítése offline restore után - megakadályozza a boot hibákat."""
        logging.info(f"[BCD] BCD javítás indítása: {target_drive}")
        self.emit('task_progress', {'task': 'restore', 'log': '\n--- BOOT LOADER (BCD) JAVÍTÁS ---'})
        
        target_drive = target_drive.rstrip('\\') + '\\'
        windows_path = os.path.join(target_drive, 'Windows')
        
        if not os.path.exists(windows_path):
            self.emit('task_progress', {'task': 'restore', 'log': f'⚠️ Windows mappa nem található: {windows_path}'})
            return False
            
        success = False
        
        # 1. Próbáljuk a legegyszerűbb módszert (ALL)
        self.emit('task_progress', {'task': 'restore', 'log': f'bcdboot {target_drive}Windows /f ALL'})
        res = self._run(['bcdboot', f'{target_drive}Windows', '/f', 'ALL'])
        if res.returncode == 0:
            success = True
            self.emit('task_progress', {'task': 'restore', 'log': '✅ BCD sikeresen újraépítve (ALL)!'})
        else:
            err_msg = res.stderr.strip() if res.stderr else res.stdout.strip() if res.stdout else f'Exit code: {res.returncode}'
            self.emit('task_progress', {'task': 'restore', 'log': f'⚠️ bcdboot hiba (0x{res.returncode:X}): {err_msg[:300]}'})
            
        # 2. bootrec parancsok (ha a bcdboot nem sikerült teljesen)
        if not success:
            self.emit('task_progress', {'task': 'restore', 'log': 'bootrec parancsok futtatása...'})
            for cmd in ['/fixmbr', '/fixboot', '/rebuildbcd']:
                res = self._run(['bootrec', cmd])
                if res.returncode == 0:
                    self.emit('task_progress', {'task': 'restore', 'log': f'  bootrec {cmd}: ✅'})
                else:
                    self.emit('task_progress', {'task': 'restore', 'log': f'  bootrec {cmd}: ⚠️ (nem elérhető)'})
        
        logging.info(f"[BCD] Javítás befejezve, success={success}")
        return success

    def repair_bcd_standalone(self):
        """Önálló BCD javítás - a felhasználó kiválasztja a meghajtót."""
        logging.info("[API] repair_bcd_standalone()")
        target = self.select_directory('Válaszd ki a HALOTT WINDOWS meghajtóját (ahol a Windows mappa van)')
        if not target:
            logging.info("[BCD] Mégse - nincs cél kiválasztva")
            return
        target = os.path.splitdrive(os.path.abspath(target))[0] + "\\"
        logging.info(f"[BCD] Standalone BCD javítás: {target}")
        
        def worker():
            self.emit('task_start', {'task': 'bcd', 'title': 'BCD Boot Hiba Javítása'})
            self.emit('task_progress', {'task': 'bcd', 'log': f'Kiválasztott meghajtó: {target}\n', 'indeterminate': True})
            
            # Ellenőrzés - van-e Windows mappa
            windows_path = os.path.join(target, 'Windows')
            if not os.path.exists(windows_path):
                self.emit('task_progress', {'task': 'bcd', 'log': f'❌ Hiba: Windows mappa nem található!\n   Elérési út: {windows_path}'})
                self.emit('task_complete', {'task': 'bcd', 'status': '❌ Windows mappa nem található!'})
                return
            
            # BCD javítás (ugyanaz a kód mint a restore után)
            self._repair_bcd_for_task(target, 'bcd')
            
            self.emit('task_progress', {'task': 'bcd', 'log': '\n==== BCD JAVÍTÁS BEFEJEZVE ===='})
            self.emit('task_complete', {'task': 'bcd', 'status': '✅ BCD javítás befejezve!'})
        
        self._safe_thread('bcd', worker)
    
    def _repair_bcd_for_task(self, target_drive, task_name):
        """BCD javítás közös logika - használható restore-ból vagy önállóan is."""
        target_drive = target_drive.rstrip('\\') + '\\'
        
        self.emit('task_progress', {'task': task_name, 'log': '\n--- BOOT LOADER (BCD) JAVÍTÁS ---'})
        self.emit('task_progress', {'task': task_name, 'log': f'Cél Windows meghajtó: {target_drive}'})
        self.emit('task_progress', {'task': task_name, 'log': 'A Windows meghajtó lemezének azonosítása (PowerShell)...'})
        
        ps_script = f"""
$TargetDrive = "{target_drive[0]}"
try {{
    $winVol = Get-Partition | Where-Object {{ $_.DriveLetter -eq $TargetDrive }}
    if (-not $winVol) {{ Write-Output "FAIL: Nem található a Windows partíció ($TargetDrive:)"; exit }}
    
    $diskNum = $winVol.DiskNumber
    Write-Output "INFO: Lemez azonosítva: Disk $diskNum"
    
    $efiPart = Get-Partition -DiskNumber $diskNum | Where-Object {{ $_.Type -eq 'System' -or $_.GptType -eq '{{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}}' }}
    if (-not $efiPart) {{ Write-Output "FAIL: Nem található EFI System partíció ezen a lemezen!"; exit }}
    
    Write-Output "INFO: EFI Partíció azonosítva: Partition $($efiPart.PartitionNumber)"
    
    $used = (Get-Volume).DriveLetter
    $free = (65..90 | ForEach-Object {{ [char]$_ }}) | Where-Object {{ $used -notcontains $_ }} | Select-Object -First 1
    if (-not $free) {{ Write-Output "FAIL: Nincs szabad betűjel!"; exit }}
    
    Set-Partition -DiskNumber $diskNum -PartitionNumber $efiPart.PartitionNumber -NewDriveLetter $free | Out-Null
    Write-Output "EFI:$free"
}} catch {{
    Write-Output "ERROR: $($_.Exception.Message)"
}}
"""
        res = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script], encoding='utf-8')
        
        success = False
        efi_letter = None
        disk_number = None
        efi_partition = None
        
        if res.stdout:
            for line in res.stdout.splitlines():
                line = line.strip()
                if line.startswith("INFO:"):
                    self.emit('task_progress', {'task': task_name, 'log': line[6:]})
                    if "Disk" in line:
                        m = re.search(r'Disk (\d+)', line)
                        if m: disk_number = m.group(1)
                    if "Partition" in line:
                        m = re.search(r'Partition (\d+)', line)
                        if m: efi_partition = m.group(1)
                elif line.startswith("EFI:"):
                    efi_letter = line[4:].strip() + ":"
                    self.emit('task_progress', {'task': task_name, 'log': f'EFI betűjel hozzárendelve: {efi_letter}'})
                elif line.startswith("FAIL:") or line.startswith("ERROR:"):
                    self.emit('task_progress', {'task': task_name, 'log': f'⚠️ {line}'})

        if efi_letter:
            self.emit('task_progress', {'task': task_name, 'log': f'bcdboot {target_drive}Windows /s {efi_letter} /f UEFI'})
            boot_res = self._run(['bcdboot', f'{target_drive}Windows', '/s', efi_letter, '/f', 'UEFI'])
            if boot_res.returncode == 0:
                success = True
                self.emit('task_progress', {'task': task_name, 'log': '✅ BCD sikeresen újraépítve (UEFI)!'})
            else:
                self.emit('task_progress', {'task': task_name, 'log': '⚠️ UEFI bcdboot hiba, fallback...'})
            
            # EFI betűjel eltávolítása PowerShell-el
            if disk_number and efi_partition:
                rm_ps = f"Remove-PartitionAccessPath -DiskNumber {disk_number} -PartitionNumber {efi_partition} -AccessPath '{efi_letter}\\'"
                self._run(["powershell", "-NoProfile", "-Command", f"for ($i=0; $i -lt 3; $i++) {{ try {{ Invoke-Expression \"{rm_ps}\"; break }} catch {{ Start-Sleep -Seconds 2 }} }}"])
                # Fallback diskpart
                dp_cmd = f"select disk {disk_number}\nselect partition {efi_partition}\nremove letter={efi_letter[0]}\n"
                self._run(['diskpart'], input=dp_cmd, timeout=30)
                
        if not success:
            self.emit('task_progress', {'task': task_name, 'log': f'bcdboot {target_drive}Windows /f ALL'})
            res_fb = self._run(['bcdboot', f'{target_drive}Windows', '/f', 'ALL'])
            if res_fb.returncode == 0:
                success = True
                self.emit('task_progress', {'task': task_name, 'log': '✅ BCD sikeresen újraépítve (ALL fallback)!'})
            else:
                err_msg = res_fb.stderr.strip() if res_fb.stderr else res_fb.stdout.strip() if res_fb.stdout else f'Exit code: {res_fb.returncode}'
                self.emit('task_progress', {'task': task_name, 'log': f'⚠️ bcdboot hiba (0x{res_fb.returncode:X}): {err_msg[:300]}'})
        
        if not success:
            self.emit('task_progress', {'task': task_name, 'log': 'bootrec parancsok futtatása...'})
            for cmd in ['/fixmbr', '/fixboot', '/rebuildbcd']:
                br_res = self._run(['bootrec', cmd])
                status = '✅' if br_res.returncode == 0 else '⚠️ (nem elérhető)'
                self.emit('task_progress', {'task': task_name, 'log': f'  bootrec {cmd}: {status}'})
        
        return success
