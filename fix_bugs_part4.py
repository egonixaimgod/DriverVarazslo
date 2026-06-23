import re
import os

with open('driver_tool.py', 'r', encoding='utf-8') as f:
    text = f.read()

# 1. wuauserv állapot ellenőrzés
old_status = r'''            if policy_disabled and search_disabled:
                result = {'status': 'Teljesen LETILTVA', 'color': 'disabled'}'''
new_status = r'''            service_disabled = False
            try:
                res = self._run(['powershell', '-NoProfile', '-Command', '(Get-Service wuauserv).StartType'], encoding='utf-8')
                if res.stdout and 'Disabled' in res.stdout:
                    service_disabled = True
            except Exception:
                pass

            if service_disabled:
                result = {'status': 'Szolgáltatás LETILTVA (services.msc)', 'color': 'disabled'}
            elif policy_disabled and search_disabled:
                result = {'status': 'Teljesen LETILTVA', 'color': 'disabled'}'''
text = text.replace(old_status, new_status)

# 2. backup_all: dism /online /export-driver használata
old_backup_all = r'''            if self.target_os_path:
                self.emit('task_progress', {'task': 'backup', 'log': 'DISM export indítása a kiválasztott rendszerből...'})
                res = self._run(['dism', f'/Image:{self.target_os_path}', '/export-driver', f'/destination:{folder}'])
                if res.returncode == 0:
                    success += 1
                else:
                    fail += 1
            else:
                enum_res = self._run(['pnputil', '/enum-drivers'])
                all_infs = re.findall(r'(oem\d+\.inf)', enum_res.stdout, re.I)
                self.emit('task_progress', {'task': 'backup', 'log': f'OEM driverek: {len(all_infs)} db'})

                for i, inf in enumerate(all_infs):
                    if self._check_cancel():
                        cancelled = True
                        break
                    inf_folder = os.path.join(folder, inf.replace('.inf', ''))
                    os.makedirs(inf_folder, exist_ok=True)
                    res = self._run(['pnputil', '/export-driver', inf, inf_folder])
                    if res.returncode == 0:
                        success += 1
                    else:
                        fail += 1
                    self.emit('task_progress', {'task': 'backup', 'current': i + 1, 'total': len(all_infs),
                                                'counter': f'{i+1}/{len(all_infs)}', 'status': f'Export: {inf}'})'''
new_backup_all = r'''            self.emit('task_progress', {'task': 'backup', 'log': 'DISM driver exportálás indítása... (Ez eltarthat egy ideig)', 'indeterminate': True})
            dism_cmd = ['dism', f'/Image:{self.target_os_path}', '/export-driver', f'/destination:{folder}'] if self.target_os_path else ['dism', '/online', '/export-driver', f'/destination:{folder}']
            res = self._run(dism_cmd)
            if res.returncode == 0:
                success += 1
                self.emit('task_progress', {'task': 'backup', 'log': '✅ DISM exportálás sikeres!'})
            else:
                fail += 1
                self.emit('task_progress', {'task': 'backup', 'log': f'❌ Hiba az exportálásnál: {res.stderr[:300]}'})'''
text = text.replace(old_backup_all, new_backup_all)

# Szintén a CliApi-ban a backup_all
old_cli_backup = r'''        if self.target_os_path:
            res = self._run(['dism', f'/Image:{self.target_os_path}', '/export-driver', f'/destination:{folder}'])
            if res.returncode == 0:
                print("   ✅ OEM export sikeres (DISM offline)")
                success = 1
            else:
                print("   ❌ OEM export hiba")
        else:
            enum_res = self._run(['pnputil', '/enum-drivers'])
            all_infs = re.findall(r'(oem\d+\.inf)', enum_res.stdout, re.I)
            
            for i, inf in enumerate(all_infs, 1):
                print(f"  [{i}/{len(all_infs)}] {inf}... ", end="", flush=True)
                inf_folder = os.path.join(folder, inf.replace('.inf', ''))
                os.makedirs(inf_folder, exist_ok=True)
                res = self._run(['pnputil', '/export-driver', inf, inf_folder])
                if res.returncode == 0:
                    print("✅")
                    success += 1
                else:
                    print("❌")
            
            print(f"   OEM: {success}/{len(all_infs)} exportálva")'''
new_cli_backup = r'''        dism_cmd = ['dism', f'/Image:{self.target_os_path}', '/export-driver', f'/destination:{folder}'] if self.target_os_path else ['dism', '/online', '/export-driver', f'/destination:{folder}']
        res = self._run(dism_cmd)
        if res.returncode == 0:
            print("   ✅ DISM export sikeres")
            success = 1
        else:
            print(f"   ❌ DISM export hiba: {res.stderr[:200]}")'''
text = text.replace(old_cli_backup, new_cli_backup)

# 4. BCD EFI betűjel biztos eltávolítása (force diskpart/PS)
old_rm_ps = r'''            if disk_number and efi_partition:
                rm_ps = f"Remove-PartitionAccessPath -DiskNumber {disk_number} -PartitionNumber {efi_partition} -AccessPath '{efi_letter}\\'"
                self._run(["powershell", "-NoProfile", "-Command", rm_ps])'''
new_rm_ps = r'''            if disk_number and efi_partition:
                rm_ps = f"Remove-PartitionAccessPath -DiskNumber {disk_number} -PartitionNumber {efi_partition} -AccessPath '{efi_letter}\\'"
                self._run(["powershell", "-NoProfile", "-Command", f"for ($i=0; $i -lt 3; $i++) {{ try {{ Invoke-Expression \"{rm_ps}\"; break }} catch {{ Start-Sleep -Seconds 2 }} }}"])
                # Fallback diskpart
                dp_cmd = f"select disk {disk_number}\nselect partition {efi_partition}\nremove letter={efi_letter[0]}\n"
                self._run(['diskpart'], input=dp_cmd, timeout=30)'''
text = text.replace(old_rm_ps, new_rm_ps)


# 6. is_zipfile ellenőrzés a stress_test zip fájl letöltése után
old_zip = r'''                    urllib.request.urlretrieve(download_url, zip_path)
                    
                    self.emit('task_progress', {'task': 'stress', 'log': '📦 Fájlok kicsomagolása...'})'''
new_zip = r'''                    urllib.request.urlretrieve(download_url, zip_path)
                    
                    if not zipfile.is_zipfile(zip_path):
                        raise Exception("A letöltött fájl sérült (Helytelen ZIP / CRC hiba).")
                        
                    self.emit('task_progress', {'task': 'stress', 'log': '📦 Fájlok kicsomagolása...'})'''
text = text.replace(old_zip, new_zip)

# Extra Escaping a powershell Remove-Device "$id" részhez (biztos ami biztos)
old_rm_ghost = r'''    $res = & pnputil /remove-device "$id" 2>&1'''
new_rm_ghost = r'''    $res = & pnputil /remove-device "$($id)" 2>&1'''
text = text.replace(old_rm_ghost, new_rm_ghost)

with open('driver_tool.py', 'w', encoding='utf-8') as f:
    f.write(text)

print("Kész a part3 bug fix!")
