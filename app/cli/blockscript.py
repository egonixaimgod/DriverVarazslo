"""DriverVarázsló CLI - CLI: block.bat letöltése (a közös logika: app/blockscript_core.py)."""

# === AUTO-IMPORTS ===
from app.blockscript_core import _download_block_script
# === /AUTO-IMPORTS ===


class CliBlockScriptMixin:
    """CLI: block.bat letöltése (a közös logika: app/blockscript_core.py). A CliApi része (összerakás: app/cli/api.py)."""

    # ================================================================
    # NET BLOKKOLÓ SCRIPT (block.bat) LETÖLTÉSE - a GUI download_block_script
    # megfelelője, a modul-szintű _download_block_script-et megosztva vele.
    # ================================================================
    def download_block_script(self):
        """Letölti a block.bat scriptet a C:\\DriverVarazslo mappába (csak letöltés,
        futtatás nélkül)."""
        print("\n🚫 Net Blokkoló script (block.bat) letöltése...")
        try:
            path = _download_block_script(self._run)
            print(f"✅ Letöltve: {path}")
            print("   (A script futtatáskor a SAJÁT mappájában és almappáiban lévő összes")
            print("   .exe kimenő internet-elérését letiltja a Windows tűzfalban - másold")
            print("   abba a mappába, amit blokkolni akarsz, és dupla kattintás: az admin")
            print("   jogot magától kéri (UAC), csak el kell fogadni.)")
        except Exception as e:
            print(f"❌ Letöltési hiba: {e}")
