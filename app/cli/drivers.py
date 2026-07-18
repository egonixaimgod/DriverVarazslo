"""DriverVarázsló CLI - CLI: driver listázás (online/offline) és törlés."""

# === AUTO-IMPORTS ===
import os
import time
import shutil
import json
import glob
# === /AUTO-IMPORTS ===


class CliDriversMixin:
    """CLI: driver listázás (online/offline) és törlés. A CliApi része (összerakás: app/cli/api.py)."""

    # ================================================================
    # DRIVER KEZELÉS
    # ================================================================
    def get_third_party_drivers(self):
        """Third-party driverek listája."""
        self._print_progress("📋 Third-party driverek lekérdezése...")
        # dism /English-lel a kimenet mindig angol, függetlenül a Windows nyelvi
        # beállításától - a pnputil-lel ellentétben nincs "csak angol/magyar kulcsot
        # ismerünk fel" locale-probléma (más nyelvű Windows-on üres listát adott volna).
        res = self._run(['dism', '/English', '/Online', '/Get-Drivers'])
        drivers = []
        current = {}
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                if current and "published" in current:
                    drivers.append(current)
                    current = {}
                continue
            parts = line.split(":", 1)
            if len(parts) == 2:
                key, val = parts[0].strip(), parts[1].strip()
                if "Published Name" in key:
                    current["published"] = val
                elif "Original File Name" in key:
                    current["original"] = val
                elif "Provider Name" in key:
                    current["provider"] = val
                elif "Class Name" in key:
                    current["class"] = val
                elif "Version" in key:
                    current["version"] = val
        if current and "published" in current:
            drivers.append(current)
        return drivers

    def get_all_drivers(self):
        """Összes driver listája (veszélyes mód)."""
        self._print_progress("📋 Összes driver lekérdezése (PowerShell)...")
        cmd = ['powershell', '-NoProfile', '-Command',
               '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Get-WindowsDriver -Online -All | Select-Object ProviderName, ClassName, Version, Driver, OriginalFileName | ConvertTo-Json -Depth 2 -WarningAction SilentlyContinue']
        res = self._run(cmd, encoding='utf-8')
        out = res.stdout.strip()
        if not out:
            return []
        try:
            data = json.loads(out)
            if isinstance(data, dict):
                data = [data]
            parsed_drivers = [{"published": d.get("Driver", ""), "original": d.get("OriginalFileName", ""),
                     "provider": d.get("ProviderName", ""), "class": d.get("ClassName", ""),
                     "version": d.get("Version", "")} for d in data]
        except Exception:
            return []

        # Szellem (force-delete-elt) driverek kiszűrése - ugyanaz a logika, mint a GUI-ban:
        # egy nem-oem publikált nevű bejegyzés csak akkor valódi, ha még van hozzá tartozó
        # mappa a DriverStore-ban, különben egy korábban force-delete-elt phantom bejegyzés.
        valid_drivers = []
        rep = os.path.join(os.environ.get('SYSTEMROOT', r'C:\Windows'), "System32", "DriverStore", "FileRepository")
        for d in parsed_drivers:
            pub = d.get("published", "")
            if not pub:
                continue
            if pub.lower().startswith("oem"):
                valid_drivers.append(d)
                continue
            if glob.glob(os.path.join(rep, f"{pub}_*")):
                valid_drivers.append(d)
        return valid_drivers
    
    def get_offline_drivers(self, all_drivers=False):
        """Offline OS driverek listája."""
        self._print_progress(f"📋 Offline driverek lekérdezése: {self.target_os_path}...")
        # /English: a GUI verzióval egyezően kényszerített angol DISM kimenet, függetlenül a
        # futtató Windows/WinPE nyelvétől - enélkül nem angol rendszeren a lenti angol
        # kulcsszavak (Published Name stb.) nem illeszkednek, és a lista némán üres marad.
        cmd = ['dism', '/English', f'/Image:{self.target_os_path}', '/Get-Drivers']
        if all_drivers:
            cmd.append('/all')
        res = self._run(cmd)
        drivers = []
        current = {}
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                if current and "published" in current:
                    drivers.append(current)
                    current = {}
                continue
            parts = line.split(":", 1)
            if len(parts) == 2:
                key, val = parts[0].strip(), parts[1].strip()
                if "Published Name" in key:
                    current["published"] = val
                elif "Original File Name" in key:
                    current["original"] = val
                elif "Provider Name" in key:
                    current["provider"] = val
                elif "Class Name" in key:
                    current["class"] = val
                elif "Version" in key:
                    current["version"] = val
        if current and "published" in current:
            drivers.append(current)
            
        valid_drivers = []
        rep = os.path.join(self.target_os_path, "Windows", "System32", "DriverStore", "FileRepository")
        for d in drivers:
            pub = d.get("published", "")
            if not pub:
                continue
            if pub.lower().startswith("oem"):
                valid_drivers.append(d)
                continue
            if glob.glob(os.path.join(rep, f"{pub}_*")):
                valid_drivers.append(d)
                
        return valid_drivers
    
    def list_drivers(self, all_drivers=False):
        """Driver lista megjelenítése."""
        if self.target_os_path:
            drivers = self.get_offline_drivers(all_drivers)
        elif all_drivers:
            drivers = self.get_all_drivers()
        else:
            drivers = self.get_third_party_drivers()
        
        if not drivers:
            print("❌ Nincs találat vagy hiba történt.")
            return []
        
        mode = "ÖSSZES" if all_drivers else "Third-party"
        loc = f" ({self.target_os_path})" if self.target_os_path else ""
        print(f"\n{'='*60}")
        print(f"  {mode} driverek{loc}: {len(drivers)} db")
        print(f"{'='*60}")
        print(f"{'#':>4}  {'Published':<18} {'Provider':<25} {'Class':<15}")
        print("-" * 70)
        for i, d in enumerate(drivers, 1):
            pub = d.get('published', '?')[:17]
            prov = d.get('provider', '?')[:24]
            cls = d.get('class', '?')[:14]
            print(f"{i:4}  {pub:<18} {prov:<25} {cls:<15}")
        print("-" * 70)
        return drivers
    
    def delete_drivers(self, drivers, list_all=False, reboot=False):
        """Driverek törlése."""
        total = len(drivers)
        print(f"\n🗑️  {total} driver törlése indul...")
        print("-" * 50)

        success = 0
        fail = 0
        is_offline = bool(self.target_os_path)

        for i, drv in enumerate(drivers, 1):
            pub = drv.get('published', '?')
            print(f"  [{i}/{total}] {pub}... ", end="", flush=True)

            is_oem = pub.lower().startswith("oem")

            if is_offline:
                res = self._run(['dism', f'/Image:{self.target_os_path}', '/Remove-Driver', f'/Driver:{pub}'])
            else:
                # ok_codes 3010: siker, de reboot kell a lezáráshoz - a szöveg-ellenőrzés lent sikeresnek veszi.
                res = self._run(['pnputil', '/delete-driver', pub, '/uninstall', '/force'], ok_codes=(0, 3010))

            if res.returncode == 0 or any(k in res.stdout.lower() for k in ['deleted', 'törölve', 'successfully']):
                print("✅")
                success += 1
            else:
                # A GUI verzióval egyezően az agresszív force-delete fallback (takeown/icacls/
                # rmtree) csak "ÖSSZES driver" módban fut le - harmadik féltől eltérő
                # (list_all=False) nézetben egy sikertelen törlés egyszerűen sikertelen marad,
                # nem próbálunk erőszakkal beleírni a DriverStore-ba.
                if list_all and not is_oem:
                    found_any = False
                    if is_offline:
                        rep = os.path.join(self.target_os_path, "Windows", "System32", "DriverStore", "FileRepository")
                        inf_dir = os.path.join(self.target_os_path, "Windows", "INF")
                    else:
                        rep = os.path.join(os.environ.get('SYSTEMROOT', r'C:\Windows'), "System32", "DriverStore", "FileRepository")
                        inf_dir = os.path.join(os.environ.get('SYSTEMROOT', r'C:\Windows'), "INF")
                    
                    dirs = glob.glob(os.path.join(rep, f"{pub}_*"))
                    if dirs:
                        for d in dirs:
                            self._run(f'takeown /f "{d}" /r /A', shell=True)
                            self._run(f'icacls "{d}" /grant *S-1-5-32-544:F /t', shell=True)
                            shutil.rmtree(d, ignore_errors=True)
                            self._run(f'rmdir /s /q "{d}"', shell=True)
                        found_any = True
                        
                    bname = os.path.splitext(pub)[0]
                    for ext in ['.inf', '.pnf', '.INF', '.PNF']:
                        fpath = os.path.join(inf_dir, bname + ext)
                        if os.path.exists(fpath):
                            self._run(f'takeown /f "{fpath}" /A', shell=True)
                            self._run(f'icacls "{fpath}" /grant *S-1-5-32-544:F', shell=True)
                            try:
                                os.remove(fpath)
                                found_any = True
                            except OSError:
                                self._run(f'del /f /q "{fpath}"', shell=True)
                                found_any = True
                    
                    if found_any:
                        print("✅ (force)")
                        success += 1
                    else:
                        print("❌")
                        fail += 1
                else:
                    print("❌")
                    fail += 1
        
        print("-" * 50)
        print(f"✅ Sikeres: {success}  |  ❌ Sikertelen: {fail}")
        
        # Post-delete scan
        if not is_offline and success > 0:
            print("\n🔄 Hardverek újraszkennelése...")
            self._run(['pnputil', '/scan-devices'])
            time.sleep(2)
            print("✅ Kész!")
            
            if reboot:
                print("\n🔄 Újraindítás 5 másodperc múlva...")
                time.sleep(5)
                self._run(['shutdown', '/r', '/t', '0', '/f'])
        
        return success, fail
    
