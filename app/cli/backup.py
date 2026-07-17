"""DriverVarázsló CLI - CLI: driver backup/restore, WIM-kinyerés, visszaállítási pont."""

# === AUTO-IMPORTS ===
import os
import time
import shutil
from datetime import datetime
# === /AUTO-IMPORTS ===


class CliBackupMixin:
    """CLI: driver backup/restore, WIM-kinyerés, visszaállítási pont. A CliApi része (összerakás: app/cli/api.py)."""

    # ================================================================
    # MENTÉS ÉS VISSZAÁLLÍTÁS
    # ================================================================
    def backup_third_party(self, dest_folder):
        """Third-party driverek mentése."""
        folder = os.path.join(dest_folder, f"DriverVarázsló_Export_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        os.makedirs(folder, exist_ok=True)
        print("\n💾 Third-party driverek mentése...")
        print(f"   Cél: {folder}")
        print("-" * 50)
        
        if self.target_os_path:
            res = self._run(['dism', f'/Image:{self.target_os_path}', '/export-driver', f'/destination:{folder}'])
        else:
            res = self._run(['dism', '/online', '/export-driver', f'/destination:{folder}'])
        
        if res.returncode == 0:
            print("✅ Mentés sikeres!")
            return folder
        else:
            print(f"❌ Hiba: {res.stderr[:200] if res.stderr else 'Ismeretlen hiba'}")
            return None
    
    def backup_all(self, dest_folder):
        """Összes driver mentése (OEM + inbox)."""
        folder = os.path.join(dest_folder, f"DriverVarázsló_FullExport_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        os.makedirs(folder, exist_ok=True)
        print("\n💾 ÖSSZES driver mentése...")
        print(f"   Cél: {folder}")
        print("-" * 50)
        
        success = 0
        # OEM driverek
        print("1/3 OEM driverek exportálása...")
        dism_cmd = ['dism', f'/Image:{self.target_os_path}', '/export-driver', f'/destination:{folder}'] if self.target_os_path else ['dism', '/online', '/export-driver', f'/destination:{folder}']
        res = self._run(dism_cmd)
        if res.returncode == 0:
            print("   ✅ DISM export sikeres")
            success = 1
        else:
            print(f"   ❌ DISM export hiba: {res.stderr[:200]}")
        
        # FileRepository (inbox)
        print("2/3 Windows inbox driverek (FileRepository) másolása...")
        windows_dir = os.path.join(self.target_os_path, 'Windows') if self.target_os_path else os.environ.get('SYSTEMROOT', r'C:\Windows')
        driverstore = os.path.join(windows_dir, 'System32', 'DriverStore', 'FileRepository')
        inbox_folder = os.path.join(folder, '_Windows_Inbox_Drivers')
        os.makedirs(inbox_folder, exist_ok=True)
        self._run(['robocopy', driverstore, inbox_folder, '/E', '/R:0', '/W:0', '/NFL', '/NDL', '/NJH', '/NJS', '/NC', '/NS', '/NP'])
        print("   ✅ FileRepository másolva")
        
        # INF mappa
        print("3/3 Windows INF mappa másolása...")
        inf_src = os.path.join(windows_dir, 'INF')
        inbox_inf = os.path.join(folder, '_Windows_Inbox_INF')
        os.makedirs(inbox_inf, exist_ok=True)
        self._run(['robocopy', inf_src, inbox_inf, '/E', '/R:0', '/W:0', '/NFL', '/NDL', '/NJH', '/NJS', '/NC', '/NS', '/NP'])
        print("   ✅ INF mappa másolva")
        
        # Összegzés
        total_size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fns in os.walk(folder) for f in fns if os.path.exists(os.path.join(dp, f)))
        print("-" * 50)
        print(f"✅ Mentés kész! Méret: {total_size / (1024*1024):.0f} MB")
        return folder
    
    def restore_drivers(self, source_folder, online=True):
        """Driverek visszaállítása."""
        print(f"\n{'♻️'} Driverek visszaállítása...")
        print(f"   Forrás: {source_folder}")
        if not online:
            print(f"   Cél: {self.target_os_path}")
        print("-" * 50)
        
        if online and not self.target_os_path:
            # Online mód - pnputil
            print("🔄 pnputil /add-driver futtatása...")
            res = self._run(['pnputil', '/add-driver', f"{source_folder}\\*.inf", '/subdirs', '/install'])
            if res.returncode == 0:
                print("✅ Visszaállítás sikeres!")
            else:
                print("⚠️  Részleges siker vagy hiba. Részletek:")
                print(res.stdout[:500] if res.stdout else res.stderr[:500])
            
            print("\n🔄 Hardverek újraszkennelése...")
            self._run(['pnputil', '/scan-devices'])
            time.sleep(10)
            print("✅ Kész!")
        else:
            # Offline mód - DISM
            target = self.target_os_path or input("Cél OS meghajtó (pl: D:\\): ").strip()
            if not target:
                print("❌ Nincs cél megadva!")
                return False

            # Formátum-detektálás: a DISM /Add-Driver egyedül NEM tudja telepíteni az inbox
            # (Windows-natív) drivereket, mert nincs hozzájuk class installer - ezért ezeket
            # fizikailag is át kell másolni a DriverStore-ba, ugyanúgy mint a GUI verzióban.
            norm_source = os.path.normpath(source_folder)
            repo_check = os.path.join(norm_source, "FileRepository")
            inf_check = os.path.join(norm_source, "INF")
            is_wim_extract = os.path.isdir(repo_check) or os.path.isdir(inf_check)
            inbox_subfolder = os.path.join(norm_source, "_Windows_Inbox_Drivers")
            has_inbox_subfolder = os.path.isdir(inbox_subfolder)

            target_repo = os.path.join(target, "Windows", "System32", "DriverStore", "FileRepository")
            target_inf = os.path.join(target, "Windows", "INF")
            had_errors = False

            if is_wim_extract:
                print("WIM-ből kimentett gyári driverek észlelve - fizikai másolás (a DISM egyedül nem tudja telepíteni az inbox drivereket)...")
                if os.path.exists(repo_check):
                    had_errors = self._force_copy_cli(repo_check, target_repo) or had_errors
                    if os.path.exists(inf_check):
                        had_errors = self._force_copy_cli(inf_check, target_inf) or had_errors
                else:
                    had_errors = self._force_copy_cli(norm_source, target_repo) or had_errors
            elif has_inbox_subfolder:
                print("Teljes export formátum észlelve (_Windows_Inbox_Drivers) - inbox driverek fizikai másolása...")
                had_errors = self._force_copy_cli(inbox_subfolder, target_repo) or had_errors
                inbox_inf_subfolder = os.path.join(norm_source, "_Windows_Inbox_INF")
                if os.path.isdir(inbox_inf_subfolder):
                    had_errors = self._force_copy_cli(inbox_inf_subfolder, target_inf) or had_errors

            print(f"🔄 DISM /Add-Driver futtatása ({target})...")
            scratch = os.path.join(target, "Scratch")
            os.makedirs(scratch, exist_ok=True)
            res = self._run(['dism', f'/Image:{target}', '/Add-Driver', f'/Driver:{norm_source}', '/Recurse', '/ForceUnsigned', f'/ScratchDir:{scratch}'])

            if res.returncode == 0 and not had_errors:
                print("✅ Visszaállítás sikeres!")
            elif had_errors:
                print("⚠️  A DISM regisztráció lefutott, DE a fizikai másolás hibákkal fejeződött be - a napló tartalmazza a részleteket, a visszaállítás valószínűleg HIÁNYOS!")
            else:
                print("⚠️  Részleges siker vagy hiba. Néhány inbox driver nem telepíthető DISM-mel.")
                print(res.stdout[:300] if res.stdout else "")

            # === BCD JAVÍTÁS (boot loader) ===
            self._repair_bcd_cli(target)

        return True

    def _force_copy_cli(self, src, dst):
        """Robocopy-alapú kényszerített másolás jogosultság-megkerüléssel (CLI verzió a GUI
        force_copy-jának megfelelője). Visszatérési érték: True, ha hiba történt."""
        if not os.path.exists(src):
            print(f"  ⚠️  Forrás nem létezik: {src}")
            return True
        os.makedirs(dst, exist_ok=True)

        needed_bytes = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(src) for f in fs if os.path.exists(os.path.join(r, f)))
        free_bytes = shutil.disk_usage(dst).free
        if needed_bytes > free_bytes:
            print(f"  ❌ Nincs elég szabad hely! Szükséges kb. {needed_bytes // (1024*1024)} MB, elérhető: {free_bytes // (1024*1024)} MB.")
            return True

        print(f"  Robocopy: {os.path.basename(src)} -> {os.path.basename(dst)}")
        cmd = ['robocopy', src, dst, '/E', '/ZB', '/R:1', '/W:1', '/COPY:DAT', '/NC', '/NS', '/NFL', '/NDL', '/NP']
        res = self._run(cmd)
        if res.returncode < 8:
            print(f"  ✅ Sikeres robocopy ({res.returncode})")
            return False

        print(f"  ⚠️  Robocopy hiba ({res.returncode}), tartalék: mappánkénti jogszerzés (lassabb)...")
        had_error = False
        for root, _, files in os.walk(src):
            rel = os.path.relpath(root, src)
            target_dir = os.path.join(dst, rel) if rel != '.' else dst
            os.makedirs(target_dir, exist_ok=True)
            for f in files:
                sfile = os.path.join(root, f)
                dfile = os.path.join(target_dir, f)
                if os.path.exists(dfile):
                    self._run(f'takeown /f "{dfile}" /A', shell=True)
                    self._run(f'icacls "{dfile}" /grant *S-1-5-32-544:F', shell=True)
                    self._run(f'attrib -R "{dfile}"', shell=True)
                try:
                    shutil.copy2(sfile, dfile)
                except Exception as e:
                    print(f"  ❌ Hiba ({f}): {e}")
                    had_error = True
        print("  ⚠️  Fallback másolás hibákkal fejeződött be." if had_error else "  ✅ Fallback másolás befejeződött.")
        return had_error

    def extract_wim(self, wim_path, dest_folder):
        """WIM-ből gyári driverek kinyerése."""
        print("\n📀 WIM driver kinyerés...")
        print(f"   WIM: {wim_path}")
        print(f"   Cél: {dest_folder}")
        print("-" * 50)
        
        is_pe = os.environ.get('SystemDrive', 'C:') == 'X:'
        sys_temp = r'C:\DV_Temp' if is_pe else (os.environ.get('SystemDrive', 'C:') + '\\DV_Temp')
        mount_dir = os.path.join(sys_temp, f"WIM_Mount_Temp_{int(time.time())}")
        target_folder = os.path.join(dest_folder, f"Windows_Gyari_Alap_Driverek_{datetime.now().strftime('%Y%m%d_%H%M')}")
        
        if os.path.exists(mount_dir):
            shutil.rmtree(mount_dir, ignore_errors=True)
        os.makedirs(mount_dir, exist_ok=True)
        os.makedirs(target_folder, exist_ok=True)
        
        try:
            print("1/3 WIM csatolása (ez 3-5 perc)...")
            res = self._run(["dism", "/Mount-Image", f"/ImageFile:{wim_path}", "/Index:1", f"/MountDir:{mount_dir}", "/ReadOnly"])
            if res.returncode != 0:
                raise Exception(f"Mount hiba: {res.stderr}")
            
            print("2/3 FileRepository + INF másolása...")
            driverstore = os.path.join(mount_dir, "Windows", "System32", "DriverStore", "FileRepository")
            target_repo = os.path.join(target_folder, "FileRepository")
            if os.path.exists(driverstore):
                shutil.copytree(driverstore, target_repo, dirs_exist_ok=True)
            
            inf_dir = os.path.join(mount_dir, "Windows", "INF")
            target_inf = os.path.join(target_folder, "INF")
            if os.path.exists(inf_dir):
                shutil.copytree(inf_dir, target_inf, dirs_exist_ok=True)
            
            print("3/3 WIM leválasztása...")
            self._run(["dism", "/Unmount-Image", f"/MountDir:{mount_dir}", "/Discard"])
            self._run(["dism", "/Cleanup-Wim"])
            shutil.rmtree(mount_dir, ignore_errors=True)
            
            print("-" * 50)
            print(f"✅ Gyári driverek kimentve: {target_folder}")
            return target_folder
            
        except Exception as e:
            print(f"❌ Hiba: {e}")
            self._run(["dism", "/Unmount-Image", f"/MountDir:{mount_dir}", "/Discard"])
            self._run(["dism", "/Cleanup-Wim"])
            shutil.rmtree(mount_dir, ignore_errors=True)
            return None
    
    def create_restore_point(self):
        """Visszaállítási pont létrehozása."""
        if self.target_os_path:
            print("\n❌ Hiba: Visszaállítási pont csak Élő rendszeren készíthető!")
            return False
            
        desc = f"DriverVarázsló_Backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        print("\n🛡️  Visszaállítási pont létrehozása...")
        print(f"   Név: {desc}")
        print("-" * 50)
        
        # Enable System Restore
        print("1/2 Rendszervédelem engedélyezése...")
        self._run(["powershell", "-NoProfile", "-Command", 'Enable-ComputerRestore -Drive "$($env:SystemDrive)\\" -ErrorAction SilentlyContinue'])
        self._run(['reg', 'add', r'HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore',
                   '/v', 'SystemRestorePointCreationFrequency', '/t', 'REG_DWORD', '/d', '0', '/f'])
        
        # Create restore point
        print("2/2 Visszaállítási pont létrehozása...")
        ps_cmd = f'Checkpoint-Computer -Description "{desc}" -RestorePointType "MODIFY_SETTINGS" -ErrorAction Stop'
        res = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd], encoding='utf-8')
        
        if res.returncode == 0:
            print("✅ Visszaállítási pont létrehozva!")
            return True
        else:
            print(f"❌ Hiba: {res.stderr[:200] if res.stderr else 'Ismeretlen hiba'}")
            return False
    
