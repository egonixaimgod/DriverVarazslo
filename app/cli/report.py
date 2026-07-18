"""DriverVarázsló CLI - CLI: HTML rendszer-riport generálás (a teljes adatgyűjtés +
HTML-generálás: app/report_core.py)."""

# === AUTO-IMPORTS ===
from app import report_core
# === /AUTO-IMPORTS ===


class CliReportMixin:
    """CLI: HTML rendszer-riport generálás. A CliApi része (összerakás: app/cli/api.py)."""

    def generate_system_report_cli(self):
        """HTML hardver-riport generálása (a GUI Rendszer Riport nézetének CLI
        megfelelője). A S.M.A.R.T. adatokhoz a smartctl a stresstools.zip-ből jön -
        a CLI nem tölti le, csak a már kicsomagolt példányt használja (ha nincs,
        a riport S.M.A.R.T. szekció nélkül készül)."""
        if self.target_os_path:
            print("\n❌ Hiba: A riport csak Élő (Online) rendszerről készíthető!")
            return None

        print("\n📄 RENDSZER RIPORT GENERÁLÁSA")
        print("-" * 50)

        smartctl_exe = report_core.find_existing_smartctl()
        if smartctl_exe:
            print("S.M.A.R.T. adatok: smartctl megtalálva, lemez-adatok begyűjtése...")
        else:
            print("ℹ️  smartctl nem található (stresstools.zip nincs kicsomagolva) -")
            print("   a riport S.M.A.R.T. lemez-adatok nélkül készül el.")

        note = input("Megjegyzés a riportra (üres = nincs): ").strip() or None

        print("Hardver-adatok begyűjtése (WMI + registry), ez pár másodperc...")
        try:
            final_path = report_core.generate_system_report(self._run, smartctl_exe, note)
        except Exception as e:
            print(f"❌ Hiba a riport generálásánál: {e}")
            return None

        print("-" * 50)
        print(f"✅ Riport elkészült: {final_path}")
        print("   (Böngészőben megnyitva nyomtatható / PDF-be menthető.)")
        return final_path
