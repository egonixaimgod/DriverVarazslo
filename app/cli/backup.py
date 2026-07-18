"""DriverVarázsló CLI - CLI: driver backup/restore, WIM-kinyerés, visszaállítási pont
(a közös logika: app/backup_core.py)."""

# === AUTO-IMPORTS ===
import os
from datetime import datetime
from app import backup_core
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

        res = self._run(backup_core.export_drivers_cmd(self.target_os_path, folder))

        if res.returncode == 0:
            print("✅ Mentés sikeres!")
            return folder
        else:
            print(f"❌ Hiba: {res.stderr[:200] if res.stderr else 'Ismeretlen hiba'}")
            return None

    def backup_all(self, dest_folder):
        """Összes driver mentése (OEM + inbox)."""
        print("\n💾 ÖSSZES driver mentése...")
        print("-" * 50)

        result = backup_core.backup_all_drivers(
            self._run, lambda msg: print(f"  {msg}"), None,
            dest_folder, self.target_os_path)

        print("-" * 50)
        if result['status'] == 'no_space':
            print("❌ Nincs elég szabad hely a célmeghajtón!")
            return None
        print(f"✅ Mentés kész! Méret: {result['size_mb']:.0f} MB")
        print(f"   Mappa: {result['folder']}")
        return result['folder']

    def restore_drivers(self, source_folder, online=True):
        """Driverek visszaállítása (a közös backup_core.run_restore-ral - élő rendszerre
        pnputil, halott Windowsra fizikai másolás + DISM + BCD javítás + rescan script)."""
        print(f"\n{'♻️'} Driverek visszaállítása...")
        print(f"   Forrás: {source_folder}")

        if online and not self.target_os_path:
            target = None
        else:
            online = False
            target = self.target_os_path or input("Cél OS meghajtó (pl: D:\\): ").strip()
            if not target:
                print("❌ Nincs cél megadva!")
                return False
            print(f"   Cél: {target}")
        print("-" * 50)

        result = backup_core.run_restore(
            self._run, print, None,
            self._si, self._nw,
            online, source_folder, target)

        print("-" * 50)
        if result == 'errors':
            print("⚠️  A visszaállítás hibákkal fejeződött be - a napló tartalmazza a részleteket!")
        else:
            print("✅ Visszaállítás befejezve!")
        return True

    def extract_wim(self, wim_path, dest_folder):
        """WIM/ESD-ből gyári driverek kinyerése."""
        print("\n📀 WIM driver kinyerés...")
        print(f"   WIM: {wim_path}")
        print(f"   Cél: {dest_folder}")
        print("-" * 50)

        try:
            target_folder = backup_core.extract_wim(
                self._run, lambda msg: print(f"  {msg}"), None,
                wim_path, dest_folder, self.target_os_path)
            print("-" * 50)
            print(f"✅ Gyári driverek kimentve: {target_folder}")
            return target_folder
        except Exception as e:
            print(f"❌ Hiba: {e}")
            return None

    def create_restore_point(self):
        """Visszaállítási pont létrehozása (rendszervédelem-fallbackkal és utólagos
        ellenőrzéssel - a közös backup_core.create_restore_point)."""
        if self.target_os_path:
            print("\n❌ Hiba: Visszaállítási pont csak Élő rendszeren készíthető!")
            return False

        print("\n🛡️  Visszaállítási pont létrehozása...")
        print("-" * 50)

        status, desc = backup_core.create_restore_point(self._run, lambda msg: print(f"  {msg}"))

        print("-" * 50)
        if status == 'ok':
            print(f"✅ Visszaállítási pont létrehozva: {desc}")
            return True
        if status == 'ok_unverified':
            print("⚠️  Visszaállítási pont létrehozás elindítva (ellenőrzés később).")
            return True
        print("❌ Hiba a visszaállítási pont létrehozásánál!")
        return False
