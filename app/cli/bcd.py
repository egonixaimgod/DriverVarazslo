"""DriverVarázsló CLI - CLI: BCD / bootloader javítás."""

# === AUTO-IMPORTS ===
import os
import logging
# === /AUTO-IMPORTS ===


class CliBcdMixin:
    """CLI: BCD / bootloader javítás. A CliApi része (összerakás: app/cli/api.py)."""

    def _repair_bcd_cli(self, target_drive):
        """BCD újraépítése CLI módban - megkeresi a megfelelő lemezen az EFI-t."""
        print("\n" + "-" * 50)
        print("🔧 BOOT LOADER (BCD) JAVÍTÁS")
        print("-" * 50)
        
        target_drive = target_drive.rstrip('\\') + '\\'
        target_letter = target_drive[0].upper()
        windows_path = os.path.join(target_drive, 'Windows')
        
        if not os.path.exists(windows_path):
            print(f"⚠️  Windows mappa nem található: {windows_path}")
            return False
        
        print(f"Cél Windows meghajtó: {target_drive}")
        
        # 1. Megkeressük melyik DISK-en van a Windows partíció
        print("A Windows meghajtó lemezének azonosítása...")
        
        disk_number = None
        efi_letter = None
        efi_partition = None
        
        try:
            # Volume-ok listázása
            res = self._run(['diskpart'], input='list volume\n', timeout=30)
            
            if res.returncode == 0 and res.stdout:
                lines = res.stdout.splitlines()
                target_volume = None
                
                # Windows volume keresése
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 3:
                        for i, p in enumerate(parts):
                            if p.upper() == target_letter and i >= 1:
                                try:
                                    target_volume = int(parts[1])
                                except (ValueError, IndexError):
                                    pass
                                break
                
                if target_volume is not None:
                    print(f"Windows volume: {target_volume}")
                    
                    # Disk azonosítása
                    res2 = self._run(['diskpart'], input=f'select volume {target_volume}\ndetail volume\n', timeout=30)
                    
                    if res2.returncode == 0 and res2.stdout:
                        for line in res2.stdout.splitlines():
                            if 'Disk' in line and '#' not in line:
                                parts = line.split()
                                for p in parts:
                                    if p.isdigit():
                                        disk_number = int(p)
                                        break
                                if disk_number is not None:
                                    break
                    
                    if disk_number is not None:
                        print(f"Lemez: Disk {disk_number}")
                        
                        # EFI partíció keresése ezen a lemezen
                        res3 = self._run(['diskpart'], input=f'select disk {disk_number}\nlist partition\n', timeout=30)
                        
                        if res3.returncode == 0 and res3.stdout:
                            for line in res3.stdout.splitlines():
                                line_upper = line.upper()
                                if 'SYSTEM' in line_upper or 'EFI' in line_upper:
                                    parts = line.split()
                                    for i, p in enumerate(parts):
                                        if p.isdigit() and i >= 1:
                                            efi_partition = int(p)
                                            break
                                    if efi_partition:
                                        break
                        
                        if efi_partition:
                            print(f"EFI partíció: Partition {efi_partition}")
                            
                            # Szabad betűjel keresése
                            used_letters = set()
                            for line in lines:
                                parts = line.split()
                                for p in parts:
                                    if len(p) == 1 and p.isalpha():
                                        used_letters.add(p.upper())
                            
                            free_letter = None
                            for c in 'STUVWXYZ':
                                if c not in used_letters:
                                    free_letter = c
                                    break
                            
                            if free_letter:
                                res4 = self._run(['diskpart'], 
                                    input=f'select disk {disk_number}\nselect partition {efi_partition}\nassign letter={free_letter}\n',
                                    timeout=30)
                                if res4.returncode == 0:
                                    efi_letter = free_letter + ':'
                                    print(f"EFI betűjel: {efi_letter}")
        except Exception as e:
            print(f"⚠️  Lemez azonosítási hiba: {e}")
        
        # 2. bcdboot futtatása
        success = False
        
        if efi_letter:
            print(f"bcdboot {target_drive}Windows /s {efi_letter} /f UEFI")
            res = self._run(['bcdboot', f'{target_drive}Windows', '/s', efi_letter, '/f', 'UEFI'])
            if res.returncode == 0:
                success = True
                print("✅ BCD sikeresen újraépítve (UEFI)!")
            else:
                print("⚠️  UEFI bcdboot hiba, fallback...")
            
            # EFI betűjel eltávolítása
            try:
                self._run(['diskpart'], 
                    input=f'select disk {disk_number}\nselect partition {efi_partition}\nremove letter={efi_letter[0]}\n',
                    timeout=30)
            except Exception as e:
                logging.debug(e)
        
        if not success:
            # Fallback: /s nélkül
            print(f"bcdboot {target_drive}Windows /f ALL")
            res = self._run(['bcdboot', f'{target_drive}Windows', '/f', 'ALL'])
            if res.returncode == 0:
                success = True
                print("✅ BCD sikeresen újraépítve (ALL)!")
            else:
                print(f"⚠️  bcdboot hiba (0x{res.returncode:X}), bootrec parancsok...")
        
        if not success:
            print("bootrec parancsok...")
            for cmd in ['/fixmbr', '/fixboot', '/rebuildbcd']:
                print(f"  bootrec {cmd}... ", end="", flush=True)
                res = self._run(['bootrec', cmd])
                print("✅" if res.returncode == 0 else "⚠️")
        
        print("-" * 50)
        print("✅ BCD javítás befejezve!")
        return True

    def repair_bcd_standalone_cli(self):
        """Önálló BCD javítás CLI módban."""
        print("\n🔧 BCD BOOT HIBA JAVÍTÁSA")
        print("-" * 50)
        
        target = self.target_os_path
        if not target:
            target = input("Add meg a HALOTT Windows meghajtóját (pl: D:\\): ").strip()
            
        if not target:
            print("❌ Nincs meghajtó megadva!")
            return False
        
        target = target.rstrip('\\') + '\\'
        windows_path = os.path.join(target, 'Windows')
        
        if not os.path.exists(windows_path):
            print(f"❌ Windows mappa nem található: {windows_path}")
            return False
        
        return self._repair_bcd_cli(target)
    
