"""DriverVarázsló CLI - CLI: frissítés-ellenőrzés + frissítés (a közös logika:
app/update_core.py)."""

# === AUTO-IMPORTS ===
from app import common
from app import update_core
# === /AUTO-IMPORTS ===


class CliUpdaterMixin:
    """CLI: frissítés-ellenőrzés + frissítés. A CliApi része (összerakás: app/cli/api.py)."""

    def check_for_updates_cli(self):
        """Frissítés keresése GitHubon; ha van újabb build, megerősítés után letölti
        és lecseréli az exe-t (a csere-.bat újraindítja a programot)."""
        print("\n🔄 FRISSÍTÉS KERESÉSE")
        print("-" * 50)
        print(f"Jelenlegi build: {common.BUILD_NUMBER}")
        print("Ellenőrzés a GitHubon (ez pár másodperc)...")

        result = update_core.check_for_updates()
        if not result.get('has_update'):
            print("✅ A program naprakész (nincs újabb build).")
            print("ℹ️  Megjegyzés: friss kiadás után a GitHub CDN pár percig még a régi")
            print("   verziót adhatja vissza - ilyenkor próbáld újra kicsit később.")
            return

        new_version = result['new_version']
        print(f"⬆️  Új verzió érhető el: Build {new_version} (jelenlegi: {common.BUILD_NUMBER})")
        confirm = input("Letöltöd és frissíted most? A program újraindul! (i/n): ").strip().lower()
        if confirm != 'i':
            print("❌ Frissítés kihagyva.")
            return

        try:
            bat_path = update_core.stage_update(lambda msg: print(f"  {msg}"))
        except Exception as e:
            print(f"❌ Hiba a letöltés során: {e}")
            return

        print("🔄 A program most bezárul és a frissített verzió indul el...")
        update_core.launch_update_and_exit(bat_path)
