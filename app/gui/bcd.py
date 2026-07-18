"""DriverVarázsló GUI - BCD / bootloader: a "BCD Boot Hiba Javítása" gomb a felhasználó
saját BootFixer.cmd tooljának letöltője (github.com/egonixaimgod/boot_javito_tool),
plusz az offline-visszaállítás utáni automatikus BCD-javítás (_repair_bcd)."""

# === AUTO-IMPORTS ===
import os
import shutil
import threading
import logging
from app.common import _app_data_dir
from app.common import _ps_quote
# === /AUTO-IMPORTS ===


# A felhasználó saját, önálló boot-javító tool-ja (Batch script, kézzel futtatandó):
# meghajtó-választás után újraírja a boot partíciót. A program CSAK LETÖLTI az
# app-adatmappába (C:\DriverVarazslo), SOHA nem futtatja - a futtatás a szervizes dolga.
BOOT_FIXER_URL = "https://raw.githubusercontent.com/egonixaimgod/boot_javito_tool/main/BootFixer.cmd"
BOOT_FIXER_FILENAME = "BootFixer.cmd"


def _download_boot_fixer(run_fn):
    """A BootFixer.cmd letöltése a _app_data_dir() mappába. Ugyanaz a friss-Windows
    tanúsítvány-fallback, mint a block.bat-nál (CERTIFICATE_VERIFY_FAILED esetén
    PowerShell/schannel letöltés, TELJES ellenőrzéssel - semmit nem kapcsolunk ki).
    Visszaadja a mentett fájl útvonalát, hibánál kivételt dob."""
    import urllib.request, urllib.error, ssl
    dest = os.path.join(_app_data_dir(), BOOT_FIXER_FILENAME)
    logging.info(f"[BOOTFIXER] Letöltés innen: {BOOT_FIXER_URL}")
    ssl_ctx = ssl.create_default_context()
    req = urllib.request.Request(BOOT_FIXER_URL, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
    try:
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=60) as resp, open(dest, 'wb') as f:
            shutil.copyfileobj(resp, f)
    except urllib.error.URLError as dl_err:
        if 'CERTIFICATE_VERIFY_FAILED' not in str(dl_err):
            raise
        logging.warning(f"[BOOTFIXER] Python SSL tanúsítvány-hiba ({dl_err}) - PowerShell (schannel) letöltés, teljes ellenőrzéssel...")
        ps_cmd = ("$ProgressPreference='SilentlyContinue'; "
                  "[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072; "
                  f"Invoke-WebRequest -Uri '{_ps_quote(BOOT_FIXER_URL)}' -OutFile '{_ps_quote(dest)}' -UseBasicParsing")
        result = run_fn(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps_cmd], timeout=120)
        if not result or result.returncode != 0 or not os.path.exists(dest):
            raise Exception("A letöltés sikertelen (nincs internet, vagy a GitHub nem elérhető).")
    if not os.path.exists(dest) or os.path.getsize(dest) == 0:
        raise Exception("A letöltött BootFixer.cmd üres vagy hiányzik.")
    logging.info(f"[BOOTFIXER] Letöltve: {dest}")
    return dest


class GuiBcdMixin:
    """BCD / bootloader javítás (önálló gomb + offline-visszaállítás utáni futtatás). A DriverToolApi része (összerakás: app/gui/api.py)."""

    # ================================================================
    # BCD REPAIR (boot loader javítás offline restore után)
    # ================================================================
    def _repair_bcd(self, target_drive):
        """BCD újraépítése offline restore után - megakadályozza a boot hibákat."""
        logging.info(f"[BCD] BCD javítás indítása: {target_drive}")
        self.emit('task_progress', {'task': 'restore', 'log': '\n--- BOOT LOADER (BCD) JAVÍTÁS ---'})
        
        target_drive = target_drive.rstrip('\\') + '\\'
        windows_path = os.path.join(target_drive, 'Windows')
        
        if not os.path.exists(windows_path):
            self.emit('task_progress', {'task': 'restore', 'log': f'⚠️ Windows mappa nem található: {windows_path}'})
            return False
            
        success = False
        
        # 1. Próbáljuk a legegyszerűbb módszert (ALL)
        self.emit('task_progress', {'task': 'restore', 'log': f'bcdboot {target_drive}Windows /f ALL'})
        res = self._run(['bcdboot', f'{target_drive}Windows', '/f', 'ALL'])
        if res.returncode == 0:
            success = True
            self.emit('task_progress', {'task': 'restore', 'log': '✅ BCD sikeresen újraépítve (ALL)!'})
        else:
            err_msg = res.stderr.strip() if res.stderr else res.stdout.strip() if res.stdout else f'Exit code: {res.returncode}'
            self.emit('task_progress', {'task': 'restore', 'log': f'⚠️ bcdboot hiba (0x{res.returncode:X}): {err_msg[:300]}'})
            
        # 2. bootrec parancsok (ha a bcdboot nem sikerült teljesen)
        if not success:
            self.emit('task_progress', {'task': 'restore', 'log': 'bootrec parancsok futtatása...'})
            for cmd in ['/fixmbr', '/fixboot', '/rebuildbcd']:
                res = self._run(['bootrec', cmd])
                if res.returncode == 0:
                    self.emit('task_progress', {'task': 'restore', 'log': f'  bootrec {cmd}: ✅'})
                else:
                    self.emit('task_progress', {'task': 'restore', 'log': f'  bootrec {cmd}: ⚠️ (nem elérhető)'})
        
        logging.info(f"[BCD] Javítás befejezve, success={success}")
        return success

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
