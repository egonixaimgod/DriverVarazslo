"""DriverVarázsló CLI - CLI: BCD / bootloader javítás (a közös javítási lánc:
app/bcd_core.py)."""

# === AUTO-IMPORTS ===
import os
from app import bcd_core
# === /AUTO-IMPORTS ===


class CliBcdMixin:
    """CLI: BCD / bootloader javítás. A CliApi része (összerakás: app/cli/api.py)."""

    def _repair_bcd_cli(self, target_drive):
        """BCD újraépítése CLI módban - a közös bcd_core.repair_bcd (EFI-keresés +
        bcdboot + bootrec fallback) print-kiírással."""
        print("\n" + "-" * 50)
        print("🔧 BOOT LOADER (BCD) JAVÍTÁS")
        print("-" * 50)

        result = bcd_core.repair_bcd(self._run, print, target_drive)

        print("-" * 50)
        if result:
            print("✅ BCD javítás befejezve!")
        return result

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
