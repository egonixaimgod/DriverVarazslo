"""DriverVarázsló CLI - CLI: BitLocker állapot + kikapcsolás (a közös logika:
app/bitlocker_core.py)."""

# === AUTO-IMPORTS ===
import time
from app import bitlocker_core
# === /AUTO-IMPORTS ===


class CliBitlockerMixin:
    """CLI: BitLocker állapot lekérdezés + kikapcsolás. A CliApi része (összerakás: app/cli/api.py)."""

    def bitlocker_menu_cli(self):
        """BitLocker állapot kiírása + opcionális kikapcsolás (a GUI BitLocker
        nézetének CLI megfelelője)."""
        if self.target_os_path:
            print("\n❌ Hiba: A BitLocker kezelés csak Élő (Online) rendszeren működik!")
            return

        print("\n🔐 BITLOCKER KEZELŐ")
        print("-" * 50)
        status = bitlocker_core.get_bitlocker_status(self._run)
        print(f"Rendszermeghajtó állapota: {status['status']}")

        if status['color'] in ('disabled', 'unknown'):
            # Nincs mit kikapcsolni (vagy nem olvasható az állapot).
            return

        if status['color'] == 'warning':
            print("ℹ️  A dekódolás már folyamatban van - várd meg, míg befejeződik.")
            return

        confirm = input("\nKikapcsolod a BitLockert (dekódolás indítása)? (i/n): ").strip().lower()
        if confirm != 'i':
            print("❌ Megszakítva.")
            return

        print("Dekódolási parancs kiadása (Disable-BitLocker)...")
        ok, err = bitlocker_core.disable_bitlocker(self._run)
        if ok:
            print("✅ Parancs sikeresen kiadva! A dekódolás a háttérben fut.")
            time.sleep(2)
            status = bitlocker_core.get_bitlocker_status(self._run)
            print(f"Aktuális állapot: {status['status']}")
        else:
            print(f"❌ Hiba: {err}")
