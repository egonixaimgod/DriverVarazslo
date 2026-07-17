"""DriverVarázsló GUI - Net Blokkoló Script nézet: block.bat letöltése (a közös logika: app/blockscript_core.py)."""

# === AUTO-IMPORTS ===
import logging
from app.blockscript_core import _download_block_script
# === /AUTO-IMPORTS ===


class GuiBlockScriptMixin:
    """Net Blokkoló Script nézet: block.bat letöltése (a közös logika: app/blockscript_core.py). A DriverToolApi része (összerakás: app/gui/api.py)."""

    # ================================================================
    # NET BLOKKOLÓ SCRIPT (block.bat) LETÖLTÉSE
    # ================================================================
    def download_block_script(self):
        """Letölti a block.bat scriptet a C:\\DriverVarazslo mappába (csak letöltés,
        futtatás nélkül). Kicsi fájl, ezért szinkron hívás - a pywebview úgyis saját
        szálon futtatja az API-hívásokat, a UI nem fagy be tőle."""
        logging.info("[API] download_block_script()")
        try:
            path = _download_block_script(self._run)
            return {'success': True, 'path': path}
        except Exception as e:
            logging.error(f"[BLOCK-SCRIPT] Letöltési hiba: {e}")
            return {'success': False, 'error': str(e)}
