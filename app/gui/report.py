"""DriverVarázsló GUI - Rendszer Riport (PDF) nézet: HTML hardver-riport generálás
S.M.A.R.T. adatokkal (a teljes adatgyűjtés + HTML-generálás: app/report_core.py)."""

# === AUTO-IMPORTS ===
import logging
import traceback
from app import report_core
# === /AUTO-IMPORTS ===


class GuiReportMixin:
    """Rendszer Riport (PDF) nézet: HTML hardver-riport generálás S.M.A.R.T. adatokkal. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def generate_system_report(self, note=None):
        logging.info(f"[API] generate_system_report(note={'igen' if note else 'nem'})")
        try:
            # S.M.A.R.T adatok begyűjtése (smartctl - stress tools zipből; a GUI le is
            # tölti a zipet, ha még nincs meg)
            stress_dir = self._download_stresstools()
            smartctl_exe = report_core.find_smartctl(stress_dir)

            final_path = report_core.generate_system_report(self._run, smartctl_exe, note)

            # A "Bolti nyomtatóval nyomtatás" gomb (print_via_store_printer) ebből
            # tudja, melyik fájlt kell kinyomtatnia - nem kér újra útvonalat a UI-tól.
            self._last_report_path = final_path
            return {'success': True, 'path': final_path}
        except Exception as e:
            logging.error(f"Hiba a jelentés generálásánál: {e}")
            logging.error(traceback.format_exc())
            raise Exception(str(e))
