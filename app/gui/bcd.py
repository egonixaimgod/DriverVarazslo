"""DriverVarázsló GUI - BCD / bootloader: a "BCD Boot Hiba Javítása" gomb a felhasználó
saját BootFixer.cmd tooljának letöltője (github.com/egonixaimgod/boot_javito_tool).
Az offline-visszaállítás utáni automatikus BCD-javítás a közös app/bcd_core.py-ban él,
és a visszaállítási folyamat (app/backup_core.py run_restore) hívja."""

# === AUTO-IMPORTS ===
import os
import threading
import logging
from app.common import _app_data_dir
from app.common import download_with_cert_fallback
# === /AUTO-IMPORTS ===


# A felhasználó saját, önálló boot-javító tool-ja (Batch script, kézzel futtatandó):
# meghajtó-választás után újraírja a boot partíciót. A program CSAK LETÖLTI az
# app-adatmappába (C:\DriverVarazslo), SOHA nem futtatja - a futtatás a szervizes dolga.
BOOT_FIXER_URL = "https://raw.githubusercontent.com/egonixaimgod/boot_javito_tool/main/BootFixer.cmd"
BOOT_FIXER_FILENAME = "BootFixer.cmd"


def _download_boot_fixer(run_fn):
    """A BootFixer.cmd letöltése a _app_data_dir() mappába. Ugyanaz a friss-Windows
    tanúsítvány-fallback, mint a block.bat-nál (common.download_with_cert_fallback -
    TELJES tanúsítvány-ellenőrzéssel, semmit nem kapcsolunk ki).
    Visszaadja a mentett fájl útvonalát, hibánál kivételt dob."""
    dest = os.path.join(_app_data_dir(), BOOT_FIXER_FILENAME)
    return download_with_cert_fallback(
        run_fn, BOOT_FIXER_URL, dest, timeout=60, ps_timeout=120, log_tag='BOOTFIXER',
        error_msg="A BootFixer.cmd letöltése sikertelen (nincs internet, vagy a GitHub nem elérhető).")


class GuiBcdMixin:
    """BCD / bootloader: a BootFixer.cmd letöltő gomb. A DriverToolApi része (összerakás: app/gui/api.py)."""

    def download_boot_fixer(self):
        """A "BCD Boot Hiba Javítása" gomb funkciója: a felhasználó saját BootFixer.cmd
        tooljának letöltése az app-adatmappába. A program NEM futtatja - a letöltés után
        a felület kiírja a használatot (dupla katt -> meghajtó-választás -> a tool
        újraírja a boot partíciót). A korábbi beépített bcdboot/bootrec-es gomb-logika
        a felhasználó kérésére törölve lett (a restore utáni automatikus _repair_bcd
        változatlanul él, azt a Mentés és Visszaállítás folyamata használja)."""
        logging.info("[API] download_boot_fixer()")

        def worker():
            try:
                dest = _download_boot_fixer(self._run)
                self.emit('boot_fixer_ready', {'path': dest})
                self.emit('toast', {'message': f'✅ BootFixer.cmd letöltve: {dest}', 'type': 'success'})
            except Exception as e:
                logging.error(f"[BOOTFIXER] Letöltési hiba: {e}")
                self.emit('boot_fixer_ready', {'error': str(e)})
                self.emit('toast', {'message': f'❌ BootFixer.cmd letöltése sikertelen: {e}', 'type': 'error'})

        # Gyors, izolált letöltés - a load_drivers mintájára nem foglalja a _task_busy-t.
        threading.Thread(target=worker, daemon=True, name="bootfixer-dl").start()
